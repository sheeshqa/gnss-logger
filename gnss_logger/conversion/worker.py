"""Optional sequential conversion of committed recording artifacts."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

from .service import ConversionRequest, execute
from .selection import Source


class SegmentWorker:
    def __init__(self, config=None, receiver_model=None, capacity=32, *, settings=None):
        from dataclasses import replace
        self.settings = replace(settings) if settings is not None else None
        self.request = ConversionRequest(config=config)
        self.args = self.request
        self.receiver_model = receiver_model
        self.queue = Queue(maxsize=capacity)
        self.closed = Event()
        self.enabled = Event()
        self.enabled.set()
        self._lock = Lock()
        self._current_cancel = None
        self.counts = Counter()
        self.deferred = 0
        self.last_report = None
        self.last_error = None
        self.thread = Thread(target=self._run, name='gnss-convbin')
        self.thread.start()

    def submit(self, artifact):
        with self._lock:
            if self.closed.is_set() or not self.enabled.is_set():
                return False
            return self._submit(artifact)

    def set_enabled(self, enabled):
        """Control admission only; queued and current work finish normally."""
        with self._lock:
            self.enabled.set() if enabled else self.enabled.clear()

    def cancel_current(self):
        with self._lock:
            if self._current_cancel is not None:
                self._current_cancel.set()
                return True
            return False

    def snapshot(self):
        """Detached bounded auto-queue state; no percentage or CLI parsing."""
        with self._lock:
            return deepcopy(dict(enabled=self.enabled.is_set(), closed=self.closed.is_set(),
                active=self._current_cancel is not None, queued=self.queue.qsize(),
                statuses=dict(self.counts), deferred=self.deferred,
                last_report=self.last_report, last_error=self.last_error))

    def _submit(self, artifact):
        source = Source(Path(artifact['path']), run_id=artifact['run_id'],
                        attempt_id=artifact['attempt_id'], artifact_id=artifact['artifact_id'],
                        size=artifact['bytes'], sha256=artifact['sha256'],
                        utc_day=artifact['utc_day'], receiver_model=self.receiver_model)
        try:
            self.queue.put_nowait(source)
            return True
        except Full:
            self.deferred += 1
            return False

    def _run(self):
        while True:
            try:
                source = self.queue.get(timeout=0.25)
            except Empty:
                if self.closed.is_set():
                    return
                continue
            try:
                with self._lock:
                    self._current_cancel = Event()
                report = execute(self.request, sources=[source], cancel=self._current_cancel,
                                 **({'settings': self.settings} if self.settings is not None else {}))
                errors = report['errors'] + report['segments'][0]['errors'] if report['segments'] else report['errors']
                with self._lock:
                    self.counts[report['status']] += 1
                    self.last_report = report.get('report_path')
                    if errors:
                        self.last_error = errors[-1]
            except Exception as error:
                with self._lock:
                    self.counts['FAILED'] += 1
                    self.last_error = str(error)
            finally:
                with self._lock:
                    self._current_cancel = None

    def finish(self):
        with self._lock:
            self.closed.set()
        self.thread.join()
        return dict(statuses=dict(self.counts), deferred=self.deferred,
                    last_report=self.last_report, last_error=self.last_error)
