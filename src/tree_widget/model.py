from qgis.PyQt.QtCore import (
    QAbstractItemModel, QCoreApplication, QModelIndex, QObject, pyqtSignal, QThread, QVariant,
)

from ..ngw_api.core import NGWGroupResource
from ..ngw_api.qt.qt_ngw_resource_model_job import NGWRootResourcesLoader, NGWResourceUpdater
from ..ngw_api.utils import log  # TODO REMOVE

from .item import QModelItem, QNGWResourceItem


__all__ = ["QNGWResourceTreeModel"]


class NGWResourcesModelJob(QObject):
    started = pyqtSignal()
    statusChanged = pyqtSignal(str)
    warningOccurred = pyqtSignal(object)
    errorOccurred = pyqtSignal(object)
    finished = pyqtSignal()

    def __init__(self, parent, worker):
        super().__init__(parent)
        self.__result = None
        self.__worker = worker
        self.__job_id = self.__worker.id
        self.__error = None

        self.__worker.started.connect(self.started.emit)
        self.__worker.dataReceived.connect(self.__rememberResult)
        self.__worker.statusChanged.connect(self.statusChanged.emit)
        self.__worker.errorOccurred.connect(self.processJobError)
        self.__worker.warningOccurred.connect(self.processJobWarnings)

        self.model_response = None

    def setResponseObject(self, resp):
        self.model_response = resp
        self.model_response.job_id = self.__job_id

    def __rememberResult(self, result):
        self.__result = result

    def getJobId(self):
        return self.__job_id

    def getResult(self):
        return self.__result

    def error(self):
        return self.__error

    def processJobError(self, job_error):
        self.__error = job_error
        self.errorOccurred.emit(job_error)

    def processJobWarnings(self, job_error):
        if self.model_response:
            self.model_response._warnings.append(job_error)
        # self.warningOccurred.emit(job_error)

    def start(self):
        self.__thread = QThread(self)
        self.__worker.moveToThread(self.__thread)
        self.__worker.finished.connect(self.finishProcess)
        self.__thread.started.connect(self.__worker.run)

        self.__thread.start()

    def finishProcess(self):
        self.__worker.started.disconnect()
        self.__worker.dataReceived.disconnect()
        self.__worker.statusChanged.disconnect()
        self.__worker.errorOccurred.disconnect()
        self.__worker.warningOccurred.disconnect()
        self.__worker.finished.disconnect()

        self.__thread.quit()
        self.__thread.wait()

        self.finished.emit()


class QNGWResourceTreeModelBase(QAbstractItemModel):
    jobStarted = pyqtSignal(str)
    jobStatusChanged = pyqtSignal(str, str)
    errorOccurred = pyqtSignal(str, object)
    warningOccurred = pyqtSignal(str, object)
    jobFinished = pyqtSignal(str)
    indexesLocked = pyqtSignal()
    indexesUnlocked = pyqtSignal()

    ngw_version = None

    def __init__(self, parent):
        super().__init__(parent)

        self.__ngw_connection_settings = None
        self._ngw_connection = None

        self.jobs = []
        self.root_item = QModelItem()

        self.__indexes_locked_by_jobs = {}
        self.__indexes_locked_by_job_errors = {}

    # TODO: rework same connection identify
    def isCurrentConnectionSame(self, other):
        return False
        return self.__ngw_connection_settings == other

    def isCurruntConnectionSameWoProtocol(self, other):
        return False
        if self.__ngw_connection_settings is None:
            if other is None:
                return True
            return False
        return self.__ngw_connection_settings.equalWoProtocol(other)

    def resetModel(self, ngw_connection):
        self.__indexes_locked_by_jobs = {}
        self.__indexes_locked_by_job_errors = {}

        self._ngw_connection = ngw_connection
        self._ngw_connection.setParent(self)

        self.__cleanModel()
        self.beginResetModel()

        self.root_item = QModelItem()

        # Get NGW version.
        self._get_ngw_version()

        self.endResetModel()
        self.modelReset.emit()

    def cleanModel(self):
        self.__cleanModel()

    def __cleanModel(self):
        c = self.root_item.childCount()
        self.beginRemoveRows(QModelIndex(), 0, c - 1)
        for i in range(c - 1, -1, -1):
            self.root_item.removeChild(self.root_item.child(i))
        self.endRemoveRows()

    def item(self, index):
        return index.internalPointer() if index and index.isValid() else self.root_item

    def index(self, row, column, parent):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parent_item = self.item(parent)
        child_item = parent_item.child(row)
        assert child_item is not None
        return self.createIndex(row, column, child_item)

    def parent(self, index):
        item = self.item(index)
        assert item is not self.root_item
        parent_item = item.parent()
        if parent_item is self.root_item:
            return QModelIndex()
        if parent_item is None:  # TODO: should not be without QTreeWidgetItem
            return QModelIndex()
        assert parent_item is not None
        return self.createIndex(
            parent_item.parent().indexOfChild(parent_item),
            0,
            parent_item
        )

    def columnCount(self, parent):
        return 1

    def rowCount(self, parent):
        parent_item = self.item(parent)
        return parent_item.childCount()

    def canFetchMore(self, parent):
        if self._isIndexLockedByJob(parent) or self._isIndexLockedByJobError(parent):
            return False

        item = self.item(parent)

        if item is self.root_item:
            if self._ngw_connection is None:
                return False
            return item.childCount() == 0  # We expect only one root resource group
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)
        if ngw_resource.common.children and ngw_resource.children_count is not None:
            return ngw_resource.children_count > item.childCount()
        return ngw_resource.common.children and item.childCount() == 0

    def fetchMore(self, parent):
        parent_item = self.item(parent)
        assert isinstance(parent_item, QModelItem)
        if parent_item is self.root_item:
            worker = NGWRootResourcesLoader(self._ngw_connection)
        else:
            ngw_resource = parent_item.data(QNGWResourceItem.NGWResourceRole)
            worker = NGWResourceUpdater(ngw_resource)
        self._startJob(worker, parent)

    def data(self, index, role):
        item = self.item(index)
        return item.data(role)

    def hasChildren(self, parent):
        parent_item = self.item(parent)
        if isinstance(parent_item, QNGWResourceItem):
            ngw_resource = parent_item.data(QNGWResourceItem.NGWResourceRole)
            return ngw_resource.common.children

        return parent_item.childCount() > 0

    def flags(self, index):
        item = self.item(index)
        return item.flags()

    # TODO job должен уметь не стартовать, например есди запущен job обновления дочерних ресурсов - нельзя запускать обновление
    def _startJob(self, worker, index=None):
        job = NGWResourcesModelJob(self, worker)
        job.started.connect(self.__jobStartedProcess)
        job.statusChanged.connect(self.__jobStatusChangedProcess)
        job.finished.connect(self.__jobFinishedProcess)
        job.errorOccurred.connect(self.__jobErrorOccurredProcess)
        job.warningOccurred.connect(self.__jobWarningOccurredProcess)

        self.jobs.append(job)

        if index is not None:
            self._lockIndexByJob(index, job)

        job.start()

        return job

    def __jobStartedProcess(self):
        job = self.sender()
        self.jobStarted.emit(job.getJobId())

    def __jobStatusChangedProcess(self, new_status):
        job = self.sender()
        self.jobStatusChanged.emit(job.getJobId(), new_status)

    def __jobFinishedProcess(self):
        job = self.sender()

        self.processJobResult(job)
        self._unlockIndexesByJob(job)

        self.jobFinished.emit(job.getJobId())
        job.deleteLater()
        self.jobs.remove(job)

    def __jobErrorOccurredProcess(self, error):
        job = self.sender()
        self.errorOccurred.emit(job.getJobId(), error)

    def __jobWarningOccurredProcess(self, error):
        job = self.sender()
        self.warningOccurred.emit(job.getJobId(), error)

    def addNGWResourceToTree(self, parent, ngw_resource):
        parent_item = self.item(parent)

        new_item = QNGWResourceItem(ngw_resource)
        i = -1
        for i in range(parent_item.childCount()):
            item = parent_item.child(i)
            if new_item.more_priority(item):
                break
        else:
            i += 1

        self.beginInsertRows(parent, i, i)
        parent_item.insertChild(i, new_item)
        self.endInsertRows()

        return self.index(i, 0, parent)

    def _lockIndexByJob(self, index, job):
        if job not in self.__indexes_locked_by_jobs:
            self.__indexes_locked_by_jobs[job] = []
        self.__indexes_locked_by_jobs[job].append(index)

        item = self.item(index)
        #self.beginInsertRows(index, item.childCount(), item.childCount())
        item.lock()
        #self.endInsertRows()

        QCoreApplication.processEvents()

        self.indexesLocked.emit()

    def _unlockIndexesByJob(self, job):
        indexes = self.__indexes_locked_by_jobs.get(job, [])
        self.__indexes_locked_by_jobs[job] = []

        for index in indexes:
            item = self.item(index)

            #self.beginRemoveRows(index, item.childCount(), item.childCount())
            item.unlock()
            #self.endRemoveRows()

            if job.error() is not None:
                self.__indexes_locked_by_job_errors[index] = job.error()

        QCoreApplication.processEvents()

        self.indexesUnlocked.emit()

    def _isIndexLockedByJob(self, index):
        for indexes in self.__indexes_locked_by_jobs.values():
            if index in indexes:
                return True
        return False

    def _isIndexLockedByJobError(self, index):
        return index in self.__indexes_locked_by_job_errors

    def getIndexByNGWResourceId(self, ngw_resource_id, parent=None):
        if parent is None:
            parent = self.index(0, 0, QModelIndex())
        item = parent.internalPointer()

        if isinstance(item, QNGWResourceItem):
            if item.ngw_resource_id() == ngw_resource_id:
                return parent

        for i in range(item.childCount()):
            index = self.getIndexByNGWResourceId(
                ngw_resource_id,
                self.index(i, 0, parent)
            )

            if index is not None:
                return index

    def processJobResult(self, job):
        job_result = job.getResult()

        if job_result is None:
            # TODO Exception
            return

        indexes = {}
        for ngw_resource in job_result.added_resources:
            if ngw_resource.common.parent is None:
                index = QModelIndex()
                new_index = self.addNGWResourceToTree(index, ngw_resource)
            else:
                parent_id = ngw_resource.common.parent.id
                if parent_id not in indexes:
                    indexes[parent_id] = self.getIndexByNGWResourceId(parent_id)
                index = indexes[parent_id]

                item = index.internalPointer()
                current_ids = [item.child(i).ngw_resource_id() for i in range(item.childCount()) if isinstance(item.child(i), QNGWResourceItem)]
                if ngw_resource.common.id not in current_ids:
                    new_index = self.addNGWResourceToTree(index, ngw_resource)
                else:
                    continue

            if job_result.main_resource_id == ngw_resource.common.id:
                if job.model_response is not None:
                    job.model_response.done.emit(new_index)

        for ngw_resource in job_result.edited_resources:
            if ngw_resource.common.parent is None:
                self.cleanModel() # remove root item
                index = QModelIndex()
            else:
                index = self.getIndexByNGWResourceId(
                    ngw_resource.common.parent.id,
                )
                item = index.internalPointer()

                for i in range(item.childCount()):
                    if item.child(i).ngw_resource_id() == ngw_resource.common.id:
                        self.beginRemoveRows(index, i, i)
                        item.removeChild(item.child(i))
                        self.endRemoveRows()
                        break
                else:
                    # TODO exception: not find deleted resource in corrent tree
                    return

            new_index = self.addNGWResourceToTree(index, ngw_resource)

            if job.model_response is not None:
                job.model_response.done.emit(new_index)

        for ngw_resource in job_result.deleted_resources:
            index = self.getIndexByNGWResourceId(
                ngw_resource.common.parent.id,
            )
            item = index.internalPointer()

            for i in range(item.childCount()):
                if item.child(i).ngw_resource_id() == ngw_resource.common.id:
                    self.beginRemoveRows(index, i, i)
                    item.removeChild(item.child(i))
                    self.endRemoveRows()
                    break
            else:
                # TODO exception: not find deleted resource in corrent tree
                return

            ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)
            ngw_resource.update()

            if job.model_response is not None:
                job.model_response.done.emit(index)

    def _get_ngw_version(self):
        try:
            self.ngw_version = self._ngw_connection.get_version()
        except:
            self.ngw_version = None


from ..ngw_api.qgis.ngw_resource_model_4qgis import (
    QGISResourcesImporter,
    QGISStyleUpdater,
    QGISStyleAdder,
    CurrentQGISProjectImporter,
    MapForLayerCreater,
    NGWCreateWMSForVector,
    NGWUpdateVectorLayer,
    CurrentQGISGroupImporter,
)
from ..ngw_api.qt.qt_ngw_resource_model_job import (
    NGWCreateMapForStyle, NGWCreateWFSForVector, NGWGroupCreater, NGWRenameResource,
    NGWResourceDelete,
)


class NGWResourceModelResponse(QObject):
    ErrorCritical = 0
    ErrorWarrning = 1

    done = pyqtSignal(object)

    def __init__(self, parent):
        super().__init__(parent)

        self.job_id = None
        self.__errors = {}
        self._warnings = []

    def errors(self):
        return self.__errors

    def warnings(self):
        return self._warnings


def modelRequest():
    def modelRequestDecorator(method):
        def wrapper(self, *args, **kwargs):
            job = method(self, *args, **kwargs)
            response = NGWResourceModelResponse(self)
            job.setResponseObject(response)
            return response
        return wrapper
    return modelRequestDecorator


class QNGWResourceTreeModel(QNGWResourceTreeModelBase):
    def _nearest_ngw_group_resource_parent(self, index):
        checking_index = index

        item = checking_index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        while not isinstance(ngw_resource, NGWGroupResource):
            checking_index = self.parent(checking_index)
            checking_item = checking_index.internalPointer()
            ngw_resource = checking_item.data(QNGWResourceItem.NGWResourceRole)

        return checking_index

    @modelRequest()
    def tryCreateNGWGroup(self, new_group_name, parent_index):
        if not parent_index.isValid():
            parent_index = self.index(0, 0, parent_index)

        parent_index = self._nearest_ngw_group_resource_parent(parent_index)

        parent_item = parent_index.internalPointer()
        ngw_resource_parent = parent_item.data(parent_item.NGWResourceRole)

        return self._startJob(
            NGWGroupCreater(new_group_name, ngw_resource_parent)
        )

    @modelRequest()
    def deleteResource(self, index):
        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            NGWResourceDelete(ngw_resource)
        )

    @modelRequest()
    def createWFSForVector(self, index, ret_obj_num):
        if not index.isValid():
            index = self.index(0, 0, index)

        parent_index = self._nearest_ngw_group_resource_parent(index)

        parent_item = parent_index.internalPointer()
        ngw_parent_resource = parent_item.data(QNGWResourceItem.NGWResourceRole)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            NGWCreateWFSForVector(ngw_resource, ngw_parent_resource, ret_obj_num)
        )

    @modelRequest()
    def createMapForStyle(self, index):
        if not index.isValid():
            index = self.index(0, 0, index)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            NGWCreateMapForStyle(ngw_resource)
        )

    @modelRequest()
    def renameResource(self, index, new_name):
        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            NGWRenameResource(ngw_resource, new_name)
        )


    @modelRequest()
    def createNGWLayers(self, qgs_map_layers, parent_index):
        if not parent_index.isValid():
            parent_index = self.index(0, 0, parent_index)

        parent_index = self._nearest_ngw_group_resource_parent(parent_index)
        parent_item = parent_index.internalPointer()
        ngw_group = parent_item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            QGISResourcesImporter(qgs_map_layers, ngw_group, self.ngw_version),
        )


    @modelRequest()
    def updateQGISStyle(self, qgs_map_layer, index):
        if not index.isValid():
            index = self.index(0, 0, index)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            QGISStyleUpdater(qgs_map_layer, ngw_resource)
        )

    @modelRequest()
    def addQGISStyle(self, qgs_map_layer, index):
        if not index.isValid():
            index = self.index(0, 0, index)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            QGISStyleAdder(qgs_map_layer, ngw_resource)
        )


    @modelRequest()
    def tryImportCurentQGISProject(self, ngw_group_name, index, iface):
        if not index.isValid():
            index = self.index(0, 0, index)

        index = self._nearest_ngw_group_resource_parent(index)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            CurrentQGISProjectImporter(ngw_group_name, ngw_resource, iface, self.ngw_version),
        )
    
    @modelRequest()
    # Group import
    # 2023-03-28
    def tryImportCurentQGISGroup(self,
                                  #ngw_group_name,
                                  index, iface):
        if not index.isValid():
            index = self.index(0, 0, index)

        index = self._nearest_ngw_group_resource_parent(index)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            CurrentQGISGroupImporter(#ngw_group_name,
                                     ngw_resource, iface, self.ngw_version),
        )

    @modelRequest()
    def createMapForLayer(self, index, ngw_style_id):
        if not index.isValid():
            index = self.index(0, 0, index)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            MapForLayerCreater(ngw_resource, ngw_style_id)
        )

    @modelRequest()
    def createWMSForVector(self, index, ngw_resource_style_id):
        if not index.isValid():
            index = self.index(0, 0, index)

        parent_index = self._nearest_ngw_group_resource_parent(index)

        parent_item = parent_index.internalPointer()
        ngw_parent_resource = parent_item.data(QNGWResourceItem.NGWResourceRole)

        item = index.internalPointer()
        ngw_resource = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            NGWCreateWMSForVector(ngw_resource, ngw_parent_resource, ngw_resource_style_id),
        )

    @modelRequest()
    def updateNGWLayer(self, index, qgs_vector_layer):
        if not index.isValid():
            index = self.index(0, 0, index)

        item = index.internalPointer()
        ngw_vector_layer = item.data(QNGWResourceItem.NGWResourceRole)

        return self._startJob(
            NGWUpdateVectorLayer(ngw_vector_layer, qgs_vector_layer),
        )
