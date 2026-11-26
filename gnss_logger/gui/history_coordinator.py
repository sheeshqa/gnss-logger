"""Bounded, read-only history delivery over the Application API."""
from PySide6.QtCore import QObject, Qt, Signal, Slot

from ..application.history import HistoryService
from .workers import TaskWorker


class HistoryCoordinator(QObject):
    state_changed = Signal(object)
    snapshot_ready = Signal(object)
    detail_ready = Signal(object)
    error_raised = Signal(str)
    ready_to_close = Signal()

    def __init__(self, parent=None, service_factory=HistoryService):
        super().__init__(parent)
        self._factory = service_factory
        self._roots = ((), (), ())
        self._worker = None
        self._pending = None
        self._generation = 0
        self._outcome = None
        self._selected = None
        self.closing = False
        self._shutdown_complete = False

    @property
    def busy(self):
        return self._worker is not None

    @property
    def shutdown_complete(self):
        return self._shutdown_complete

    def view(self):
        return dict(busy=self.busy, closing=self.closing)

    def configure_roots(self, recording, conversion, stage1=()):
        if self.closing:
            return False
        self._roots = tuple(tuple(str(p) for p in roots) for roots in (recording, conversion, stage1))
        self._selected = None
        self.detail_ready.emit(None)
        return self.refresh()

    def refresh(self):
        return self._request('snapshot', None)

    def select(self, session_key):
        if self.closing:
            return False
        self._selected = str(session_key)
        self.detail_ready.emit(None)
        return self._request('detail', self._selected)

    def _request(self, kind, key):
        if self.closing:
            return False
        self._generation += 1
        self._pending = (self._generation, kind, key, self._roots)
        if not self.busy:
            self._start_pending()
        return True

    def _start_pending(self):
        token, kind, key, roots = self._pending
        self._pending = None
        self._outcome = None

        def query():
            service = self._factory(*roots)
            if kind == 'detail':
                return service.get_session(key)
            sessions = service.list_sessions()
            diagnostics = service.diagnostics
            conversions = service.list_conversions()
            for diagnostic in service.diagnostics:
                if diagnostic not in diagnostics:
                    diagnostics.append(diagnostic)
            return dict(sessions=sessions, conversions=conversions, diagnostics=diagnostics)

        worker = TaskWorker(query, (token, kind), self)
        self._worker = worker
        worker.outcome.connect(self._received, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        try:
            worker.start()
        except Exception as error:
            self._worker = None
            worker.deleteLater()
            self.error_raised.emit(str(error))
        self.state_changed.emit(self.view())

    @Slot(object)
    def _received(self, outcome):
        self._outcome = outcome
        self._complete_if_finished()

    @Slot()
    def _finished(self):
        self._complete_if_finished()

    def _complete_if_finished(self):
        if self._worker is None or self._worker.isRunning() or self._outcome is None:
            return
        outcome = self._outcome
        worker, self._worker = self._worker, None
        self._outcome = None
        worker.deleteLater()
        token, kind = outcome['token']
        if not self.closing and token == self._generation:
            if outcome['error']:
                self.error_raised.emit(outcome['error'])
            elif kind == 'snapshot':
                self.snapshot_ready.emit(outcome['value'])
                if self._selected and self._pending is None:
                    self._request('detail', self._selected)
            else:
                self.detail_ready.emit(outcome['value'])
        if self._pending is not None and not self.closing and not self.busy:
            self._start_pending()
        self.state_changed.emit(self.view())
        self._check_shutdown()

    def request_shutdown(self):
        self.closing = True
        self._generation += 1
        self._pending = None
        self.state_changed.emit(self.view())
        self._check_shutdown()

    def _check_shutdown(self):
        if self.closing and not self.busy and not self._shutdown_complete:
            self._shutdown_complete = True
            self.ready_to_close.emit()
