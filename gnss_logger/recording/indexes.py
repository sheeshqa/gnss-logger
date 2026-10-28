"""Append-only, fsynced JSONL indexes; memory independent of session duration."""
import hashlib
import json
import os


class IndexWriter:
    def __init__(self, path):
        self.path = path
        self.handle = path.open('xb', buffering=0)
        self.size = 0
        self.hash = hashlib.sha256()
        self.synced = dict(path=str(path.resolve()), bytes=0, sha256=self.hash.hexdigest())

    def append(self, value):
        from .schema import validate_record
        validate_record(value)
        data = (json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))+'\n').encode('utf-8')
        count = self.handle.write(data)
        if count != len(data):
            raise OSError('Short index write; prefix is not committed')
        self.size += count
        self.hash.update(data)

    def sync(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.synced = dict(path=str(self.path.resolve()), bytes=self.size, sha256=self.hash.hexdigest())
        return dict(self.synced)

    def close(self):
        try:
            self.sync()
        finally:
            self.handle.close()


class ReceiptCursor:
    def __init__(self, path):
        self.handle = path.open('rb')
        self.current = None
        self.frontier = 0

    def at(self, end):
        while self.current is None or end > self.current['end']:
            line = self.handle.readline(2048)
            if not line or not line.endswith(b'\n'):
                raise OSError('Missing committed receipt for frame')
            current = json.loads(line)
            if current['start'] != self.frontier or current['end'] <= self.frontier:
                raise OSError('Receipt offset discontinuity')
            self.frontier, self.current = current['end'], current
        return self.current

    def close(self):
        self.handle.close()
