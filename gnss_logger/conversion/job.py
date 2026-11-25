"""Threaded conversion lifecycle; report statuses remain the wire protocol."""
from copy import deepcopy
from threading import Event, Lock, Thread
from uuid import uuid4

from .service import ConversionRequest, execute


class ConversionJob:
    """One-shot job. result() returns None while pending; timeout never cancels.

    FAILED includes incomplete navigation (exit 3); inspect the unchanged wire
    report for details. Cancellation is cooperative around snapshot/fingerprint
    operations and before commit; an atomic commit is never interrupted.
    """

    def __init__(self, request=None, sources=None, *, settings=None, args=None, execute_call=None):
        if args is not None:
            if request is not None:
                raise TypeError('Specify request or args, not both')
            request = args
        self.request = ConversionRequest.from_args(request)
        self.operation_id = uuid4().hex
        self.args = self.request
        self.sources = None if sources is None else tuple(sources)
        self.settings = settings
        self._execute_call = execute_call
        self._cancel = Event()
        self._done = Event()
        self._lock = Lock()
        self._state = 'PENDING'
        self._progress = None
        self._result = None
        self._error = None
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError('ConversionJob has already started')
            self._state = 'RUNNING'
            self._thread = Thread(target=self._run, name='gnss-conversion')
            self._thread.start()
        return self

    def cancel(self):
        self._cancel.set()

    def _update(self, report):
        published = deepcopy(report)
        with self._lock:
            self._progress = published

    def _run(self):
        try:
            report = (self._execute_call or execute)(self.request, self.sources, settings=self.settings,
                             cancel=self._cancel, progress=self._update, operation_id=self.operation_id)
            published = deepcopy(report)
            with self._lock:
                self._result = report
                self._progress = published
                self._state = ('CANCELLED' if report['status']=='INTERRUPTED' else
                               'SUCCEEDED' if report['exit_code']==0 else 'FAILED')
        except BaseException as error:
            with self._lock:
                self._error = error
                self._state = 'FAILED'
        finally:
            self._done.set()

    def snapshot(self):
        with self._lock:
            state = dict(state=self._state, cancel_requested=self._cancel.is_set(),
                        progress=self._progress,
                        error=str(self._error) if self._error is not None else None)
        return deepcopy(state)

    def result(self, wait=False, timeout=None):
        if wait:
            self._done.wait(timeout)
        if not self._done.is_set():
            return None
        with self._lock:
            if self._error is not None:
                raise self._error
            report = self._result
        return deepcopy(report)
