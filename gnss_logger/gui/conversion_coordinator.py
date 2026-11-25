"""Independent manual-conversion lifecycle over the shared Application API."""
from copy import deepcopy
from threading import Event

from PySide6.QtCore import QObject, QTimer, Qt, Signal, Slot

from .. import application as api
from .workers import MonitorWorker, TaskWorker


class ConversionMonitorWorker(MonitorWorker):
    completed = Signal(object)

    def run(self):
        try:
            self.start_gate.wait()
            if self.start_error is not None:
                raise RuntimeError(self.start_error)
            super().run()
            terminal = self.monitor.terminal
            if terminal is None or terminal['data'].get('api_error'):
                self.monitor.handle.cancel()
            result = self.monitor.handle.result(wait=True)
            outcome = dict(value=result, error=None)
        except BaseException as error:
            outcome = dict(value=None, error=type(error).__name__ + ': ' + str(error))
        self.completed.emit(outcome)


class ConversionCoordinator(QObject):
    state_changed = Signal(object)
    settings_loaded = Signal(object)
    events_ready = Signal(object)
    result_ready = Signal(object)
    error_raised = Signal(str)
    ready_to_close = Signal()

    def __init__(self, backend, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.closing = False
        self.phase = 'IDLE'
        self.settings = self.result = None
        self._loader = self._job = self._poller = None
        self._load_delivered = self._completed = False
        self._cancel_requested = False
        self._shutdown_complete = False
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._check_completion)
        self._timer.start()

    @property
    def busy(self):
        return self._loader is not None or self._job is not None

    @property
    def shutdown_complete(self):
        return self._shutdown_complete

    def view(self):
        return dict(busy=self.busy, closing=self.closing, phase=self.phase,
                    job_active=self._job is not None, cancel_requested=self._cancel_requested)

    def _notify(self):
        self.state_changed.emit(self.view())

    def load(self, path=None):
        if self.closing or self._loader is not None:
            return False
        self._load_delivered = False
        worker = TaskWorker(lambda: self.backend.load_settings(path), 0, self)
        self._loader = worker
        worker.outcome.connect(self._loaded, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._check_completion, Qt.ConnectionType.QueuedConnection)
        try:
            worker.start()
        except Exception as error:
            self._loader = None
            worker.deleteLater()
            self.error_raised.emit(str(error))
            return False
        self._notify()
        return True

    @Slot(object)
    def _loaded(self, outcome):
        self._load_delivered = True
        if not self.closing:
            if outcome['error']:
                self.error_raised.emit(outcome['error'])
            else:
                self.settings = outcome['value']
                self.settings_loaded.emit(self.settings)
        self._notify()

    def start(self, request, settings):
        if self.busy or self.closing:
            return False
        try:
            if not settings.executable.is_file():
                raise ValueError('convbin отсутствует: выберите существующий executable')
            job = self.backend.create_job(request, settings)
            poller = ConversionMonitorWorker(api.OperationMonitor(job, 'conversion'), 250, self)
            poller.start_gate = Event()
            poller.start_error = None
            poller.batch.connect(self._receive, Qt.ConnectionType.QueuedConnection)
            poller.failed.connect(self._failed, Qt.ConnectionType.QueuedConnection)
            poller.completed.connect(self._finished, Qt.ConnectionType.QueuedConnection)
            poller.finished.connect(self._check_completion, Qt.ConnectionType.QueuedConnection)
        except Exception as error:
            self.error_raised.emit(str(error))
            return False
        self._job, self._poller = job, poller
        self._completed = self._cancel_requested = False
        self.phase = 'RUNNING'
        try:
            # Start the observer first; if start fails no conversion was launched.
            poller.start()
        except Exception as error:
            self._job = self._poller = None
            poller.deleteLater()
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
            self._notify()
            return False
        try:
            job.start()
        except BaseException as error:
            # A job whose thread could not start cannot signal completion.
            self._completed = True
            poller.start_error = str(error)
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
        finally:
            poller.start_gate.set()
        self._notify()
        return self.phase != 'FAILED'

    def cancel(self):
        if self._job is None or self._completed:
            return False
        self._cancel_requested = True
        self._job.cancel()
        self._notify()
        return True

    @Slot(object)
    def _receive(self, events):
        try:
            for event in events:
                self.phase = event['lifecycle']
            self.events_ready.emit(deepcopy(events))
            self._notify()
        finally:
            if self._poller is not None:
                self._poller.acknowledge()

    @Slot(str)
    def _failed(self, error):
        self.error_raised.emit(error)
        self.cancel()

    @Slot(object)
    def _finished(self, outcome):
        self._completed = True
        if outcome['error']:
            self.phase = 'FAILED'
            self.error_raised.emit(outcome['error'])
        elif outcome['value'] is not None:
            self.result = deepcopy(outcome['value'])
            self.result_ready.emit(api.present_result(self.result, 'conversion'))
        self._notify()

    def request_shutdown(self, cancel=False):
        self.closing = True
        if cancel:
            self.cancel()
        self._notify()
        self._check_completion()

    @Slot()
    def _check_completion(self):
        if self._loader is not None and self._load_delivered and self._loader.isFinished():
            self._loader.deleteLater()
            self._loader = None
            self._notify()
        if self._job is not None and self._completed and self._poller.isFinished():
            self._poller.deleteLater()
            self._poller = self._job = None
            self._notify()
        if self.closing and not self.busy and not self._shutdown_complete:
            self._shutdown_complete = True
            self._timer.stop()
            self.ready_to_close.emit()
