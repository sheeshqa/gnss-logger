"""One exclusively reserved derived segment, streamed hash and original bytes."""
import hashlib
import os
import uuid
from .names import reserve
from ..device.ubx import rawx
from ..session import write_new_json
from ..timebase import rawx_utc


class DailyWriter:
    def __init__(self, root, station, day, generation, owners, artifact_directory, source_start):
        self.handle, self.part, self.final = reserve(root, station, day)
        self.digest = hashlib.sha256()
        self.written = self.synced = 0
        self.write_failed = False
        self.record = dict(schema_id='gnss_logger.stage2.artifact', schema_version=2,
            contract_version='stage2.3/v2', artifact_id=uuid.uuid4().hex, **owners,
            path=str(self.part.resolve()), final_path=str(self.final.resolve()),
            kind='DAILY' if day else 'UNDATED', utc_day=day, generation=generation,
            state='OPEN', bytes=0, synced_bytes=0, sha256=None, synced_sha256=self.digest.hexdigest(), error=None, first_rawx_utc=None,
            last_rawx_utc=None, source_start=source_start, source_end=source_start,
            partial_day=True, close_reason=None)
        try:
            from ..recording.schema import validate_record
            validate_record(self.record)
            write_new_json(artifact_directory/(self.record['artifact_id']+'.open.json'),self.record)
        except BaseException:
            self.handle.close()
            raise

    def append(self, original, frame, end):
        count = self.handle.write(original)
        if type(count) is not int or not 0 <= count <= len(original):
            self.write_failed = True
            raise OSError('Invalid daily write count')
        self.digest.update(original[:count])
        self.written += count
        self.record['bytes'] = self.written
        if count != len(original):
            self.write_failed = True
            raise OSError('Short daily write; original bytes remain in primary')
        self.record['source_end'] = end
        if self.record['utc_day'] and frame[:2] == (2,0x15):
            stamp = rawx_utc(rawx(frame[2])).isoformat()
            if self.record['first_rawx_utc'] is None:
                self.record['first_rawx_utc'] = stamp
            self.record['last_rawx_utc'] = stamp

    def sync(self):
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.synced = self.written
        self.record['synced_bytes'] = self.synced
        self.record['synced_sha256'] = self.digest.hexdigest()

    def close(self, complete, reason):
        try:
            self.sync()
        finally:
            self.handle.close()
        if self.write_failed:
            raise OSError('Derived file contains an incomplete write; original is in primary')
        self.record.update(bytes=self.written, sha256=self.digest.hexdigest(),
                           close_reason=reason, state='CLOSED_COMPLETE' if complete else 'CLOSED_PARTIAL')
        return dict(self.record)
