"""Explicit UI test doubles; no GNSS processing, ports, files or convbin.

Demo steps are UI events, NOT RAWX epochs or scientific measurements. The
operation implements the snapshot/result protocol consumed by OperationMonitor.
It is deliberately not an alternative recording or conversion implementation.
"""
from copy import deepcopy
from threading import Event, Lock, Thread
from uuid import uuid4


class MockProvider:
    def devices(self):
        return [dict(device='MOCK-X20P', description='Синтетический X20P (не MON-VER)'),
                dict(device='MOCK-F9P', description='Синтетический F9P (не MON-VER)')]

    def operation(self, scenario='normal'):
        return DemoOperation(scenario=scenario)


class DemoOperation:
    def __init__(self, scenario='normal', steps=20, interval=0.1):
        if scenario not in ('normal', 'error'):
            raise ValueError('Unknown mock scenario')
        if type(steps) is not int or steps < 1 or not 0 < interval <= 1:
            raise ValueError('Invalid demo duration')
        self.operation_id = uuid4().hex
        self._scenario, self._steps, self._interval = scenario, steps, interval
        self._lock, self._stop, self._done = Lock(), Event(), Event()
        self._thread = None
        self._snapshot = dict(lifecycle='IDLE', simulated=True, demo_steps=0,
                              stop_requested=False, progress=None)
        self._report = self._error = None

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError('Demo operation is single-use')
            self._snapshot['lifecycle'] = 'STARTING'
            self._thread = Thread(target=self._run, name='gnss-gui-demo')
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _publish(self, phase, step):
        with self._lock:
            self._snapshot.update(lifecycle=phase, demo_steps=step,
                                  stop_requested=self._stop.is_set())

    def _run(self):
        step = 0
        try:
            for step in range(1, self._steps + 1):
                self._publish('RUNNING', step)
                if self._scenario == 'error' and step == 2:
                    raise RuntimeError('MOCK: синтетическая ошибка фоновой операции')
                if self._stop.wait(self._interval):
                    self._publish('STOPPING', step)
                    break
            self._publish('FINALIZING', step)
            Event().wait(self._interval)
            report = dict(status='SUCCESS_WITH_WARNINGS', exit_code=0, simulated=True,
                          errors=[], warnings=['MOCK: данные не записывались; RINEX не создавался'],
                          segments=[], artifacts=[], demo_steps=step,
                          stop_reason='stop_requested' if self._stop.is_set() else 'demo_complete')
            with self._lock:
                self._report = report
        except Exception as error:
            with self._lock:
                self._error = error
        finally:
            self._publish('FAILED' if self._error is not None else 'FINISHED', step)
            self._done.set()

    def snapshot(self):
        with self._lock:
            return deepcopy(self._snapshot)

    def result(self):
        if not self._done.is_set():
            return None
        with self._lock:
            if self._error is not None:
                raise self._error
            return deepcopy(self._report)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()
