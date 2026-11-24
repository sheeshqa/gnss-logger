"""Pull-only monitoring of existing handles, Futures and synchronous report dicts.

No threads or callbacks are installed. refresh reads nonblocking source APIs on
the caller's thread. poll consumes one event; drain consumes all queued events.
At most capacity transient state/progress events plus two sticky slots are
queued. critical is the latest cumulative diagnostic snapshot (including the
source's bounded errors and errors_total), NOT an infinite individual error log.
The source must publish cumulative errors; discarded source errors cannot be
reconstructed here. Critical and terminal events survive progress overflow and
remain rereadable through critical and terminal even after drain/poll.
Warnings are embedded in result data. The monitor ID is fixed at construction
(explicit ID, public handle ID if available, otherwise a generated UUID).
Source exceptions retain the latest safe report/progress in terminal data;
api_error describes the adapter failure without changing raw report values.
All returned data is deeply detached. Lifecycle values are API values, never
substitutes for raw wire report statuses. Concurrent callers are serialized.
"""
from collections import deque
from concurrent.futures import CancelledError
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from .results import present_result, result_lifecycle


class OperationMonitor:
    def __init__(self, handle, operation_type, operation_id=None, capacity=8):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self.handle = handle
        self.operation_type = operation_type
        self.operation_id = operation_id or getattr(handle, 'operation_id', None) or str(uuid4())
        self._queue = deque(maxlen=capacity)
        self._lock = RLock()
        self._sequence = 0
        self._last = None
        self._critical = self._terminal = None
        self._critical_pending = self._terminal_pending = False

    def _event(self, kind, lifecycle, data):
        self._sequence += 1
        return dict(operation_id=self.operation_id, operation_type=self.operation_type,
                    lifecycle=lifecycle, utc=datetime.now(timezone.utc).isoformat(),
                    sequence=self._sequence, kind=kind, data=deepcopy(data))

    @property
    def critical(self):
        with self._lock:
            return deepcopy(self._critical)

    @property
    def terminal(self):
        with self._lock:
            return deepcopy(self._terminal)

    @property
    def queued_count(self):
        with self._lock:
            return len(self._queue) + self._critical_pending + self._terminal_pending

    def refresh(self):
        """Publish changed snapshots and, when ready, a terminal presentation."""
        with self._lock:
            if self._terminal is not None:
                return deepcopy(self._terminal)
            report = None
            failure = None
            api_error = None
            snapshot = deepcopy(self._last) if self._last is not None else {}
            try:
                if isinstance(self.handle, dict):
                    report = deepcopy(self.handle)
                    snapshot = {}
                elif hasattr(self.handle, 'snapshot'):
                    snapshot = deepcopy(self.handle.snapshot())
                    report = self.handle.result()
                else:
                    snapshot = {'state': 'RUNNING' if self.handle.running() else 'PENDING'}
                    if self.handle.done():
                        report = self.handle.result()
            except CancelledError as error:
                failure = str(error) or 'Cancelled'
                failure_lifecycle = 'CANCELLED'
                api_error = dict(component='monitor_source', error_type=type(error).__name__, message=failure)
            except Exception as error:
                failure = str(error)
                failure_lifecycle = 'FAILED'
                api_error = dict(component='monitor_source', error_type=type(error).__name__, message=failure)
            lifecycle = snapshot.get('lifecycle', snapshot.get('state', 'PENDING'))
            lifecycle = {'IDLE': 'PENDING', 'STARTING': 'RUNNING', 'SUCCEEDED': 'FINISHED'}.get(lifecycle, lifecycle)
            if report is not None:
                lifecycle = result_lifecycle(report)
            elif snapshot.get('stop_requested') and lifecycle == 'RUNNING':
                lifecycle = 'STOPPING'
            if failure is not None:
                lifecycle = failure_lifecycle
            source = report if report is not None else snapshot.get('recording', snapshot.get('progress') or {})
            detail = source.get('extensions', {}).get('gnss_logger.recording', {})
            automatic = snapshot.get('auto_conversion') or source.get('extensions', {}).get('gnss_logger.conversion', {})
            errors = detail.get('errors', source.get('errors', []))
            critical = dict(errors=errors, errors_total=detail.get('errors_total', len(errors)),
                            error=failure or snapshot.get('error') or automatic.get('last_error'))
            if self._critical is not None:
                prior = self._critical['data']
                if critical['errors_total'] < prior['errors_total'] or not errors:
                    critical['errors'] = prior['errors']
                critical['errors_total'] = max(critical['errors_total'], prior['errors_total'])
                critical['error'] = critical['error'] or prior['error']
            if any((critical['errors'], critical['errors_total'], critical['error'])):
                if self._critical is None or critical != self._critical['data']:
                    self._critical = self._event('critical', lifecycle, critical)
                    self._critical_pending = True
            if snapshot != self._last or lifecycle != getattr(self, '_last_lifecycle', None):
                kind = 'state' if self._last is None or lifecycle != self._last_lifecycle else 'progress'
                self._queue = deque((event for event in self._queue if event['kind'] != kind), maxlen=self._queue.maxlen)
                self._queue.append(self._event(kind, lifecycle, snapshot))
                self._last, self._last_lifecycle = deepcopy(snapshot), lifecycle
            if report is not None or failure is not None:
                data = present_result(source, self.operation_type, lifecycle=lifecycle)
                if api_error is not None:
                    data['api_error'] = api_error
                self._terminal = self._event('result', lifecycle, data)
                self._terminal_pending = True
            return deepcopy(self._terminal or (self._queue[-1] if self._queue else None))

    def poll(self):
        """Consume the oldest pending event, without refreshing the source."""
        with self._lock:
            candidates = list(self._queue)[:1]
            if self._critical_pending:
                candidates.append(self._critical)
            if self._terminal_pending:
                candidates.append(self._terminal)
            if not candidates:
                return None
            event = min(candidates, key=lambda item: item['sequence'])
            if event is self._critical:
                self._critical_pending = False
            elif event is self._terminal:
                self._terminal_pending = False
            else:
                self._queue.popleft()
            return deepcopy(event)

    def drain(self):
        with self._lock:
            events = []
            while self.queued_count:
                events.append(self.poll())
            return events
