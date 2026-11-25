"""Recording lifecycle adapters; the existing controller owns all recording I/O."""
from copy import deepcopy
from threading import Event, Lock, Thread

from ..recording.controller import RecordController
from ..conversion.worker import SegmentWorker
from ..session import write_new_json


def run_recording(settings, *, controller=None, stop=None, convert_config=None,
                  conversion_settings=None, worker=None, runner=None):
    """Blocking structured service. runner is an optional external CLI boundary."""
    controller = controller if controller is not None else RecordController()
    if (convert_config is not None or conversion_settings is not None or worker is not None) and settings.daily_root is None:
        raise ValueError('Automatic conversion requires daily_root')
    if worker is None and (convert_config is not None or conversion_settings is not None):
        worker = SegmentWorker(convert_config, receiver_model=settings.binding.model,
                               settings=conversion_settings)
    if worker is not None:
        controller.on_segment = worker.submit
    try:
        report = controller.run(settings, stop) if runner is None else runner(controller,settings)[1]
    finally:
        if worker is not None:
            conversion = worker.finish()
    if worker is not None:
        report['extensions']['gnss_logger.conversion'] = conversion
        if controller.directory is not None:
            summary = controller.directory/'conversion-summary.json'
            try:
                write_new_json(summary, conversion)
            except OSError as error:
                conversion['summary_error'] = str(error)
            else:
                conversion['summary_path'] = str(summary.resolve())
    return report


class RecordingHandle:
    """Single-use asynchronous owner. Stop is a request; result marks completion."""
    def __init__(self, settings, *, controller=None, convert_config=None, conversion_settings=None, worker_factory=None):
        if (convert_config is not None or conversion_settings is not None) and settings.daily_root is None:
            raise ValueError('Automatic conversion requires daily_root')
        self.settings = settings
        self.controller = controller if controller is not None else RecordController()
        self.convert_config = convert_config
        self.conversion_settings = conversion_settings
        self._worker_factory = worker_factory
        self._stop = Event()
        self._done = Event()
        self._lock = Lock()
        self._thread = None
        self._worker = None
        self._auto_enabled = True
        self._report = None
        self._error = None

    @property
    def operation_id(self):
        return self.controller.run_id

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError('RecordingHandle has already started')
            self._thread = Thread(target=self._run, name='gnss-recording')
            try:
                self._thread.start()
            except Exception as error:
                self._error = error
                self._done.set()
                raise
        return self

    def stop(self):
        self._stop.set()
        self.controller.request_stop('stop_requested')

    def set_auto_conversion(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be boolean')
        with self._lock:
            self._auto_enabled = enabled
            if self._worker is not None:
                self._worker.set_enabled(enabled)

    def cancel_conversion(self):
        with self._lock:
            if self._worker is not None:
                return self._worker.cancel_current()
            return False

    def _run(self):
        try:
            with self._lock:
                if self.convert_config is not None or self.conversion_settings is not None:
                    self._worker = (self._worker_factory or SegmentWorker)(self.convert_config, receiver_model=self.settings.binding.model,
                                                 settings=self.conversion_settings)
                    self._worker.set_enabled(self._auto_enabled)
            report = run_recording(self.settings, controller=self.controller,
                                   stop=self._stop, worker=self._worker)
            with self._lock:
                self._report = deepcopy(report)
        except BaseException as error:
            with self._lock:
                self._error = error
        finally:
            self._done.set()

    def snapshot(self):
        recording = self.controller.snapshot()
        with self._lock:
            error = str(self._error) if self._error is not None else None
            worker = self._worker
        phase = recording['extensions']['gnss_logger.recording']['monitor_phase']
        lifecycle = ('FAILED' if error else 'FINISHED') if self._done.is_set() else phase
        if not self._done.is_set() and phase == 'FINISHED':
            lifecycle = 'FINALIZING'  # automatic conversions/summary still draining
        return dict(operation_id=self.controller.run_id, operation_type='recording',
                    lifecycle=lifecycle, stop_requested=self._stop.is_set(),
                    recording=recording, error=error,
                    auto_conversion=worker.snapshot() if worker is not None else None)

    def result(self, wait=False, timeout=None):
        if wait:
            self._done.wait(timeout)
        if not self._done.is_set():
            return None
        with self._lock:
            error, report = self._error, self._report
        if error is not None:
            raise error
        return deepcopy(report)

    def shutdown(self, wait=False, timeout=None):
        self.stop()
        with self._lock:
            if self._thread is None:
                return None  # nothing was opened; no worker can signal completion
        return self.result(wait=wait, timeout=timeout)


def start_recording(settings, **options):
    return RecordingHandle(settings, **options).start()
