"""Exclusive primary journal and schema-v1 metadata; no recovery or rotation."""
from collections import deque
import hashlib
import os
from pathlib import Path
import threading

from ..session import file_fingerprint, write_new_json
from .schema import validate_record
from .contracts import WriteReceipt


def publish_exclusive(source, target):
    if os.name == 'nt':
        os.rename(source,target)  # Windows refuses to replace an existing target.
    else:
        os.link(source,target)
        source.unlink()


class ByteQueue:
    """Byte-limited queue plus ONE emergency returned block; never drops silently."""
    def __init__(self, capacity):
        self.capacity = capacity
        self.condition = threading.Condition()
        self.items = deque()
        self.bytes = self.peak = 0
        self.overflow = None
        self.done = False

    def put(self, chunk):
        with self.condition:
            if self.bytes + len(chunk.data) > self.capacity:
                if self.overflow is not None:
                    raise RuntimeError("Reader continued after queue overflow")
                self.overflow = chunk
                self.condition.notify_all()
                return False
            self.items.append(chunk)
            self.bytes += len(chunk.data)
            self.peak = max(self.peak, self.bytes)
            self.condition.notify_all()
            return True

    def finish(self):
        with self.condition:
            self.done = True
            self.condition.notify_all()

    def take(self, timeout=0.05):
        with self.condition:
            if not self.items and not self.done:
                self.condition.wait(timeout)
            if self.items:
                item = self.items.popleft()
                self.bytes -= len(item.data)
                return item
            # Emergency block follows every earlier queued byte, not before them.
            if self.done and self.overflow is not None:
                item, self.overflow = self.overflow, None
                return item
            return None

    def empty_finished(self):
        with self.condition:
            return self.done and not self.items and self.overflow is None


class PrimaryJournal:
    def __init__(self, path, opener=None, fsync=os.fsync):
        self.path = Path(path)
        self.handle = (opener or (lambda p: p.open('xb', buffering=0)))(self.path)
        self.fsync = fsync
        self.written = self.synced = 0
        self.last_sequence = None
        self.digest = hashlib.sha256()

    def append(self, chunk):
        if chunk.offset != self.written:
            raise OSError("Non-contiguous journal offset")
        count = self.handle.write(chunk.data)
        if type(count) is not int or not 0 <= count <= len(chunk.data):
            raise OSError("Invalid write result; file verification required")
        self.written += count
        self.digest.update(chunk.data[:count])
        if count != len(chunk.data):
            raise OSError(f"Short write: {count}/{len(chunk.data)} bytes; remaining bytes are unarchived")
        self.last_sequence = chunk.sequence
        return WriteReceipt(count,self.written)

    def sync(self):
        self.handle.flush()
        self.fsync(self.handle.fileno())
        self.synced = self.written

    def checkpoint(self):
        self.sync()
        return dict(synced_offset=self.synced,last_chunk_sequence=self.last_sequence,prefix_sha256=self.digest.hexdigest())

    def close(self,reason):
        artifact,errors,actual=self.finalize(reason in ('duration','source_exhausted','stop_requested'))
        return dict(artifact=artifact,failures=[dict(component=k,error=e) for k,e in errors],fingerprint=actual)

    def finalize(self, complete, fingerprint=file_fingerprint):
        errors = []
        for component, action in (('flush', self.handle.flush),
                                  ('fsync', lambda: self.fsync(self.handle.fileno())),
                                  ('close', self.handle.close)):
            try:
                action()
                if component == 'fsync':
                    self.synced = self.written
            except Exception as error:
                errors.append((component, error))
        verified = False
        actual = None
        try:
            actual = fingerprint(self.path)
            verified = (actual['bytes'] == self.written and actual['sha256'] == self.digest.hexdigest())
            if not verified:
                errors.append(('readback', OSError('File bytes/hash differ from accepted write prefix')))
        except Exception as error:
            errors.append(('readback', error))
        closed = bool(self.handle.closed)
        if not closed:
            errors.append(('close', OSError('Primary journal handle remains open')))
        success = complete and closed and verified and not errors
        # Normal publication is exclusive on Windows. POSIX uses a no-replace link.
        if success:
            target = self.path.with_suffix('')  # .ubx.part -> .ubx
            try:
                publish_exclusive(self.path,target)
                self.path = target
            except Exception as error:
                errors.append(('publish', error))
                success = False
        state = 'CLOSED_COMPLETE' if success else 'CLOSED_PARTIAL' if closed and verified else 'UNVERIFIED'
        return dict(path=str(self.path.resolve()), bytes=self.written,
                    sha256=actual['sha256'] if verified else None, state=state), errors, actual


class Metadata:
    """All output paths belong to a newly-created attempt, all files exclusive."""
    def __init__(self, directory):
        self.directory = directory
        self.events = (directory / 'events.jsonl').open('x', encoding='utf-8', newline='\n')

    def event(self, record):
        import json
        validate_record(record)
        self.events.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        self.events.flush()

    def save(self, name, record):
        validate_record(record)
        target=self.directory/name
        temporary=target.with_name(target.name+'.tmp')
        write_new_json(temporary,record)
        publish_exclusive(temporary,target)

    def close(self):
        errors = []
        for component, action in (('events_flush', self.events.flush),
                                  ('events_sync', lambda: os.fsync(self.events.fileno())),
                                  ('events_close', self.events.close)):
            try:
                action()
            except Exception as error:
                errors.append((component, error))
        return errors
