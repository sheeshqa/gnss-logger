"""UI-facing admission and asynchronous shutdown for the Stage 4.2 mock shell."""
from copy import deepcopy

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt

from ..application import OperationMonitor
from .workers import MonitorWorker, TaskWorker


class Coordinator(QObject):
    state_changed = Signal(object)
    devices_changed = Signal(object)
    events_ready = Signal(object)
    error_raised = Signal(str)
    ready_to_close = Signal()

    def __init__(self, provider=None, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.selected_device = None
        self.devices = []
        self.phase = 'IDLE'
        self.closing = False
        self.interval_ms = 250
        self._generation = 0
        self._task = self._poller = self._operation = None
        self._task_delivered = False
        self._terminal_received = False
        self._stop_requested = False
        self._shutdown_complete = False
        self._housekeeping = QTimer(self)
        self._housekeeping.setInterval(50)
        self._housekeeping.timeout.connect(self._check_completion)
        self._housekeeping.start()

    @property
    def busy(self):
        return self._operation is not None or self._task is not None

    @property
    def shutdown_complete(self):
        return self._shutdown_complete

    def view(self):
        return dict(phase=self.phase, busy=self.busy, closing=self.closing,
                    mock=self.provider is not None, selected_device=self.selected_device)

    def _notify(self):
        self.state_changed.emit(self.view())

    @Slot()
    def refresh_devices(self):
        if self.closing or self.busy:
            return False
        self._generation += 1
        self._task_delivered = False
        task = self.provider.devices if self.provider is not None else lambda: []
        self._task = TaskWorker(task, self._generation, self)
        self._task.outcome.connect(self._devices_result, Qt.ConnectionType.QueuedConnection)
        self._task.finished.connect(self._check_completion, Qt.ConnectionType.QueuedConnection)
        self.phase = 'DISCOVERING'
        self._notify()
        try:
            self._task.start()
        except Exception as error:
            self._task.deleteLater()
            self._task = None
            self.phase = 'FAILED'
            self.error_raised.emit(type(error).__name__ + ': ' + str(error))
            self._notify()
            return False
        return True

    @Slot(object)
    def _devices_result(self, outcome):
        self._task_delivered = True
        if outcome['token'] != self._generation or self.closing:
            return
        if outcome['error']:
            self.phase = 'FAILED'
            self.error_raised.emit(outcome['error'])
        else:
            self.devices = deepcopy(outcome['value'])
            names = {row['device'] for row in self.devices}
            if self.selected_device not in names:
                self.selected_device = self.devices[0]['device'] if self.devices else None
            self.devices_changed.emit(deepcopy(self.devices))
            self.phase = 'IDLE'
        self._notify()

    def select_device(self, device):
        if self.closing or self.busy:
            return False
        if device is not None and device not in {row['device'] for row in self.devices}:
            return False
        self.selected_device = device
        self._notify()
        return True

    def start_demo(self, scenario='normal'):
        if self.closing or self.busy or self.provider is None or not self.selected_device:
            return False
        try:
            self._operation = self.provider.operation(scenario)
            self._terminal_received = False
            self._stop_requested = False
            self.phase = 'STARTING'
            self._operation.start()
            self._poller = MonitorWorker(OperationMonitor(self._operation, 'demo'),
                                         self.interval_ms, self)
            self._poller.batch.connect(self._receive, Qt.ConnectionType.QueuedConnection)
            self._poller.failed.connect(self._monitor_failed, Qt.ConnectionType.QueuedConnection)
            self._poller.finished.connect(self._check_completion, Qt.ConnectionType.QueuedConnection)
            self._notify()
            self._poller.start()
        except Exception as error:
            if self._poller is not None and not self._poller.isRunning():
                self._poller.deleteLater()
                self._poller = None
            self._monitor_failed(type(error).__name__ + ': ' + str(error))
        return True

    @Slot(object)
    def _receive(self, events):
        try:
            for event in events:
                self.phase = event['lifecycle']
                if self._stop_requested and self.phase in ('PENDING', 'RUNNING', 'STARTING'):
                    self.phase = 'STOPPING'
                if event['kind'] == 'result':
                    self._terminal_received = True
            self.events_ready.emit(deepcopy(events))
            self._notify()
        finally:
            if self._poller is not None:
                self._poller.acknowledge()

    @Slot(str)
    def _monitor_failed(self, message):
        self.phase = 'FAILED'
        self.error_raised.emit(message)
        if self._operation is not None:
            self._operation.stop()
        self._terminal_received = True
        self._notify()

    @Slot()
    def stop_demo(self):
        if self._operation is None:
            return False
        self._operation.stop()
        self._stop_requested = True
        self.phase = 'STOPPING'
        self._notify()
        return True

    @Slot()
    def request_shutdown(self):
        if self.closing:
            return
        self.closing = True
        self._generation += 1
        self.stop_demo()
        self._notify()
        self._check_completion()

    @Slot()
    def _check_completion(self):
        # Never wait/join in the UI. Keep ownership until actual workers end.
        if self._task is not None and self._task_delivered and self._task.isFinished():
            self._task.deleteLater()
            self._task = None
            self._notify()
        if (self._operation is not None and self._terminal_received
                and not self._operation.is_running()
                and (self._poller is None or self._poller.isFinished())):
            if self._poller is not None:
                self._poller.deleteLater()
            self._poller = self._operation = None
            self._notify()
        if self.closing and not self.busy and not self._shutdown_complete:
            self._shutdown_complete = True
            self._housekeeping.stop()
            self.ready_to_close.emit()
