"""Stage 4.3 receiver workflow over the shared Application API, UI-thread owned."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot, Qt

from .. import application as api
from .workers import TaskWorker, RecordingMonitorWorker


class ReceiverCoordinator(QObject):
    state_changed = Signal(object)
    devices_changed = Signal(object)
    identity_changed = Signal(object)
    events_ready = Signal(object)
    record_result = Signal(object)
    auto_result = Signal(object)
    error_raised = Signal(str)
    ready_to_close = Signal()

    def __init__(self, provider, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.devices = []
        self.selected_device = None
        self.baud = 115200
        self.phase = 'IDLE'
        self.identity = self.binding = self.diagnostic_report = None
        self.closing = False
        self.interval_ms = 250
        self._generation = 0
        self._task = self._poller = self._handle = None
        self._task_delivered = False
        self._record_complete = self._terminal_received = False
        self._stop_requested = False
        self._monitor_error = False
        self._auto_settings = None
        self._auto_enabled = False
        self._auto_capable = False
        self._auto_snapshot = None
        self._auto_report_task = None
        self._auto_report_delivered = False
        self._auto_report_path = None
        self._shutdown_complete = False
        self._housekeeping = QTimer(self)
        self._housekeeping.setInterval(50)
        self._housekeeping.timeout.connect(self._check_completion)
        self._housekeeping.start()

    @property
    def busy(self):
        return any(value is not None for value in (self._task, self._handle, self._poller, self._auto_report_task))

    @property
    def shutdown_complete(self):
        return self._shutdown_complete

    def view(self):
        return dict(phase=self.phase, busy=self.busy, closing=self.closing,
                    mock=self.provider.is_mock, selected_device=self.selected_device,
                    can_start=bool(self.binding) and not self.busy and not self.closing,
                    binding=asdict(self.binding) if self.binding else None,
                    identity=deepcopy(self.identity), baud=self.baud,
                    recording_active=self._handle is not None,
                    auto_capable=self._auto_capable if self._handle is not None else self._auto_settings is not None,
                    auto_enabled=self._auto_enabled, auto_conversion=deepcopy(self._auto_snapshot))

    def configure_auto(self, settings=None, enabled=False):
        if self.busy or self.closing:
            return False
        if enabled and settings is None:
            raise ValueError('Перед Auto On выберите профиль конвертации')
        self._auto_settings, self._auto_enabled = settings, bool(enabled)
        self._notify()
        return True

    def set_auto_conversion(self, enabled):
        if self._handle is None or not self._auto_capable or self.closing:
            return False
        self._handle.set_auto_conversion(bool(enabled))
        self._auto_enabled = bool(enabled)
        self._notify()
        return True

    def cancel_conversion(self):
        if self._handle is None or not self._auto_capable:
            return False
        return self._handle.cancel_conversion()

    def refresh_auto_report(self):
        """Explicit retry after a metadata read failure, never an automatic loop."""
        if self.closing or self._auto_report_task is not None:
            return False
        self._auto_report_path = None
        self._read_auto_report(self._auto_snapshot)
        self._notify()
        return self._auto_report_task is not None

    def _read_auto_report(self, automatic):
        path = automatic.get('last_report') if automatic else None
        if not path or path == self._auto_report_path or self._auto_report_task is not None:
            return
        self._auto_report_path = path
        self._auto_report_delivered = False

        def read():
            history = api.HistoryService([], [Path(path).parent])
            for row in history.list_conversions():
                if Path(row['path']).resolve() == Path(path).resolve():
                    return api.present_result(row['raw'], 'conversion')
            raise ValueError('Не удалось прочитать отчёт Auto: ' + path)

        worker = TaskWorker(read, 0, self)
        self._auto_report_task = worker
        worker.outcome.connect(self._auto_report_loaded, Qt.ConnectionType.QueuedConnection)
        try:
            worker.start()
        except Exception as error:
            self._auto_report_task = None
            worker.deleteLater()
            self.error_raised.emit(str(error))

    @Slot(object)
    def _auto_report_loaded(self, outcome):
        self._auto_report_delivered = True
        if outcome['error']:
            self.error_raised.emit(outcome['error'])
        elif outcome['value'] is not None:
            self.auto_result.emit(outcome['value'])

    def _notify(self):
        self.state_changed.emit(self.view())

    def _invalidate(self):
        self.identity = self.binding = self.diagnostic_report = None
        self.identity_changed.emit(dict(identity=None, binding=None, report=None))

    def _launch_task(self, function, slot, phase):
        self._generation += 1
        self._task_delivered = False
        self._task = TaskWorker(function, self._generation, self)
        self._task.outcome.connect(slot, Qt.ConnectionType.QueuedConnection)
        self._task.finished.connect(self._check_completion, Qt.ConnectionType.QueuedConnection)
        self.phase = phase
        self._notify()
        try:
            self._task.start()
        except Exception as error:
            self._task.deleteLater()
            self._task = None
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
            self._notify()
            return False
        return True

    @Slot()
    def refresh_devices(self):
        if self.closing or self.busy:
            return False
        self._invalidate()  # inventory refresh never preserves stale MON-VER/binding
        return self._launch_task(self.provider.devices, self._devices_result, 'DISCOVERING')

    @Slot(object)
    def _devices_result(self, outcome):
        self._task_delivered = True
        if self.closing or outcome['token'] != self._generation:
            return
        try:
            if outcome['error']:
                raise ValueError(outcome['error'])
            rows = outcome['value']
            if not isinstance(rows, list) or any(not isinstance(row, dict) or
                    not isinstance(row.get('device'), str) or not row['device'] for row in rows):
                raise ValueError('Invalid receiver inventory')
            names = [row['device'].casefold() for row in rows]
            if len(names) != len(set(names)):
                raise ValueError('Ambiguous COM port inventory')
            self.devices = deepcopy(rows)
            if self.selected_device not in [row['device'] for row in rows]:
                self.selected_device = rows[0]['device'] if rows else None
            self.phase = 'IDLE'
        except (TypeError, ValueError) as error:
            self.devices, self.selected_device = [], None
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
        self.devices_changed.emit(deepcopy(self.devices))
        self._notify()

    def select_device(self, device):
        if self.closing or self.busy or device not in [row['device'] for row in self.devices]:
            return False
        if device != self.selected_device:
            self.selected_device = device
            self._invalidate()
        self._notify()
        return True

    def set_baud(self, baud):
        if self.closing or self.busy:
            return False
        if type(baud) is not int or baud <= 0:
            self._invalidate()
            self.error_raised.emit('Baud must be a positive integer')
            self._notify()
            return False
        if self.baud != baud:
            self.baud = baud
            self._invalidate()
        self._notify()
        return True

    def identify(self, seconds=5):
        return self._diagnostic(seconds, inspect=False)

    def diagnose(self, seconds=10):
        return self._diagnostic(seconds, inspect=True)

    def _diagnostic(self, seconds, inspect):
        if self.closing or self.busy or not self.selected_device:
            return False
        try:
            request = api.DiagnosticRequest(self.selected_device, baud=self.baud, seconds=seconds,
                                            inspect=inspect, poll_mon_ver=True,
                                            binding=self.binding if inspect else None)
        except ValueError as error:
            self.error_raised.emit(str(error))
            return False
        self._invalidate()
        return self._launch_task(lambda: self.provider.diagnose(request), self._identity_result,
                                 'DIAGNOSING' if inspect else 'IDENTIFYING')

    def _verified_binding(self, report):
        if not isinstance(report, dict) or report.get('errors'):
            raise ValueError('Receiver operation failed; inspect diagnostic errors')
        identity = report.get('identity') or {}
        usb = report.get('usb_identity') or {}
        if not isinstance(identity, dict) or not isinstance(usb, dict):
            raise ValueError('Invalid receiver identity/USB report structure')
        row = next(row for row in self.devices if row['device'] == self.selected_device)
        for key, observed in (('vid', 'vid'), ('pid', 'pid'),
                              ('serial_number', 'usb_serial'), ('location', 'usb_location')):
            if row.get(key) is not None and row[key] != usb.get(observed):
                raise ValueError('USB identity changed; refresh and identify the receiver again')
        selector = ({'usb_serial': usb['usb_serial']} if usb.get('usb_serial')
                    else {'usb_location': usb.get('usb_location')})
        return api.DeviceBinding(identity.get('model'), usb.get('vid'), usb.get('pid'), **selector)

    @Slot(object)
    def _identity_result(self, outcome):
        self._task_delivered = True
        if self.closing or outcome['token'] != self._generation:
            return
        report = outcome['value']
        try:
            if outcome['error']:
                raise ValueError(outcome['error'])
            binding = self._verified_binding(report)
            self.identity = deepcopy(report['identity'])
            self.binding = binding
            self.diagnostic_report = deepcopy(report)
            self.phase = 'IDLE'
        except (ValueError, TypeError, KeyError, StopIteration) as error:
            self._invalidate()
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
        self.identity_changed.emit(dict(identity=deepcopy(self.identity),
            binding=asdict(self.binding) if self.binding else None, report=deepcopy(report)))
        self._notify()

    def start_recording(self, parameters):
        if not self.view()['can_start']:
            return False
        try:
            settings = api.RecordSettings(initial_port=self.selected_device, binding=self.binding,
                                           baud=self.baud, **parameters)
            if not settings.output.is_absolute() or (settings.daily_root is not None and
                                                     not settings.daily_root.is_absolute()):
                raise ValueError('Recording directories must be absolute paths')
            handle = (self.provider.create_recording(settings, conversion_settings=self._auto_settings)
                      if self._auto_settings is not None else self.provider.create_recording(settings))
            self._auto_capable = self._auto_settings is not None
            self._auto_snapshot = None
            if self._auto_capable:
                handle.set_auto_conversion(self._auto_enabled)
        except (ValueError, TypeError, OSError) as error:
            self.error_raised.emit(str(error))
            return False
        self._handle = handle
        self._record_complete = self._terminal_received = False
        self._stop_requested = False
        self._monitor_error = False
        self.phase = 'STARTING'
        try:
            self._poller = RecordingMonitorWorker(api.OperationMonitor(handle, 'recording'), self.interval_ms, self)
            self._poller.batch.connect(self._receive, Qt.ConnectionType.QueuedConnection)
            self._poller.failed.connect(self._monitor_failed, Qt.ConnectionType.QueuedConnection)
            self._poller.completed.connect(self._record_finished, Qt.ConnectionType.QueuedConnection)
            self._poller.finished.connect(self._check_completion, Qt.ConnectionType.QueuedConnection)
        except Exception as error:
            if self._poller is not None:
                self._poller.deleteLater()
            self._poller = self._handle = None
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
            self._notify()
            return False
        try:
            self._poller.start()
        except Exception as error:
            self._poller.deleteLater()
            self._poller = self._handle = None
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
            self._notify()
            return False
        try:
            handle.start()
        except Exception as error:
            # RecordingHandle records start failure in result(); keep its observer
            # alive until the failure outcome and worker completion are delivered.
            self.phase = 'FAILED'
            self.error_raised.emit(str(error))
            self._notify()
            return False
        self._notify()
        return True

    @Slot(object)
    def _record_finished(self, outcome):
        self._record_complete = True
        if outcome['error']:
            self.phase = 'FAILED'
            self.error_raised.emit(outcome['error'])
        else:
            report = outcome['value']
            automatic = report.get('extensions', {}).get('gnss_logger.conversion')
            if automatic is not None:
                self._auto_snapshot = deepcopy(automatic)
                self._read_auto_report(automatic)
            self.record_result.emit(deepcopy(report))
        if self._monitor_error:
            self.phase = 'FAILED'
        self._notify()

    @Slot(object)
    def _receive(self, events):
        try:
            for event in events:
                phase = event['lifecycle']
                data = event['data']
                if 'auto_conversion' in data:
                    self._auto_snapshot = deepcopy(data['auto_conversion'])
                    self._read_auto_report(self._auto_snapshot)
                raw = data.get('recording', {})
                if phase == 'RUNNING' and raw.get('state') in ('IDENTIFYING', 'RECORDING'):
                    phase = raw['state']
                if self._stop_requested and phase in ('PENDING', 'RUNNING', 'STARTING', 'IDENTIFYING', 'RECORDING'):
                    phase = 'STOPPING'
                self.phase = phase
                if event['kind'] == 'result':
                    self._terminal_received = True
                    if data.get('api_error'):
                        self._monitor_error = True
            self.events_ready.emit(deepcopy(events))
            self._notify()
        finally:
            if self._poller is not None:
                self._poller.acknowledge()

    @Slot(str)
    def _monitor_failed(self, message):
        self._monitor_error = True
        self._terminal_received = True
        self.error_raised.emit(message)
        self.stop_recording()
        self.phase = 'FAILED'
        self._notify()

    @Slot()
    def stop_recording(self):
        if (self._handle is None or self._record_complete
                or (self._terminal_received and not self._monitor_error)):
            return False
        self._handle.stop()
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
        self.stop_recording()
        self._notify()
        self._check_completion()

    @Slot()
    def _check_completion(self):
        if (self._auto_report_task is not None and self._auto_report_delivered
                and self._auto_report_task.isFinished()):
            self._auto_report_task.deleteLater()
            self._auto_report_task = None
            self._read_auto_report(self._auto_snapshot)
            self._notify()
        if self._task is not None and self._task_delivered and self._task.isFinished():
            self._task.deleteLater()
            self._task = None
            self._notify()
        if (self._handle is not None and self._record_complete and self._terminal_received
                and self._poller.isFinished()):
            self._poller.deleteLater()
            self._poller = self._handle = None
            self._notify()
        if self.closing and not self.busy and not self._shutdown_complete:
            self._shutdown_complete = True
            self._housekeeping.stop()
            self.ready_to_close.emit()
