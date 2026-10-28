"""One bounded read-back worker. Only the controller publishes metadata/files."""
from queue import Queue, Empty
import threading
from ..session import file_fingerprint


class Verifier:
    def __init__(self, capacity=4, fingerprint=file_fingerprint):
        self.jobs, self.results = Queue(capacity), Queue(capacity)
        self.pending, self.capacity = 0, capacity
        self.fingerprint = fingerprint
        self.thread = threading.Thread(target=self._work, name='gnss-file-verifier', daemon=False)
        self.thread.start()

    def _work(self):
        while True:
            task = self.jobs.get()
            if task is None:
                return
            try:
                actual = self.fingerprint(task['path'])
                if actual != dict(bytes=task['bytes'], sha256=task['sha256']):
                    raise OSError('Derived read-back fingerprint mismatch')
                result = (task, None)
            except Exception as error:
                result = (task, error)
            self.results.put(result)

    def submit(self, task):
        if self.pending >= self.capacity:
            raise BufferError('Bounded verification queue exhausted; primary must still drain')
        self.jobs.put_nowait(task)
        self.pending += 1

    def take(self, wait=False):
        try:
            value = self.results.get(timeout=None if wait else 0)
        except Empty:
            return None
        self.pending -= 1
        return value

    def close(self):
        if self.pending:
            raise RuntimeError('Verification results must be drained before close')
        self.jobs.put(None)
        self.thread.join()
