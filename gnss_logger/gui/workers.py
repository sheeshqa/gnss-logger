"""Qt delivery adapters. No serial I/O or recorder/converter algorithms."""
from copy import deepcopy
from threading import Event

from PySide6.QtCore import QThread, Signal


class TaskWorker(QThread):
    """One bounded task; a single detached outcome is delivered to the UI."""
    outcome = Signal(object)

    def __init__(self, task, token, parent=None):
        super().__init__(parent)
        self.task, self.token = task, token

    def run(self):
        try:
            result = dict(token=self.token, value=deepcopy(self.task()), error=None)
        except BaseException as error:
            result = dict(token=self.token, value=None,
                          error=type(error).__name__ + ': ' + str(error))
        self.outcome.emit(result)


class MonitorWorker(QThread):
    """At most one unacknowledged batch, including terminal/critical events.

    While delivery is pending the monitor is not drained. Its sticky slots and
    bounded queue preserve critical/result data. A GUI slot must acknowledge in
    finally, including during shutdown. No widget is accessed on this thread.
    """
    batch = Signal(object)
    failed = Signal(str)

    def __init__(self, monitor, interval_ms=250, parent=None):
        super().__init__(parent)
        if type(interval_ms) is not int or not 250 <= interval_ms <= 2000:
            raise ValueError('Polling interval must be 250..2000 ms')
        self.monitor = monitor
        self.interval = interval_ms / 1000
        self._ack = Event()
        self._ack.set()
        self._halt = Event()

    def acknowledge(self):
        self._ack.set()

    def request_stop(self):
        self._halt.set()
        self._ack.set()

    def run(self):
        try:
            while not self._halt.is_set():
                if self._ack.is_set():
                    self.monitor.refresh()
                    events = self.monitor.drain()
                    if events:
                        self._ack.clear()
                        self.batch.emit(events)
                    if self.monitor.terminal is not None:
                        return
                self._halt.wait(self.interval)
        except BaseException as error:
            self.failed.emit(type(error).__name__ + ': ' + str(error))


class RecordingMonitorWorker(MonitorWorker):
    """Wait for actual handle completion off-UI, also after monitoring failure."""
    completed = Signal(object)

    def run(self):
        try:
            super().run()
            terminal = self.monitor.terminal
            if terminal is None or terminal['data'].get('api_error'):
                self.monitor.handle.stop()
            report = self.monitor.handle.result(wait=True)
            outcome = dict(value=deepcopy(report), error=None)
        except BaseException as error:
            outcome = dict(value=None, error=type(error).__name__ + ': ' + str(error))
        self.completed.emit(outcome)
