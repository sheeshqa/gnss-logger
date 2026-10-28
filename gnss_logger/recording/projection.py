"""Bounded projection of the durable primary prefix; never owns a serial port."""
from pathlib import Path

from ..capture.daily import DailyWriter
from ..capture.framing import Framer
from ..session import write_new_json
from .indexes import IndexWriter, ReceiptCursor
from .storage import publish_exclusive
from .time_policy import TimeResolver
from .verification import Verifier


class Projection:
    def __init__(self, directory, root, station, owners, journal_path, expected_hz, emit,
                 writer_factory=DailyWriter, verifier_factory=Verifier):
        self.directory, self.root, self.station = directory, Path(root).resolve(), station
        self.owners, self.journal_path, self.emit = owners, journal_path, emit
        self.writer_factory, self.verifier_factory = writer_factory, verifier_factory
        self.artifact_directory = directory/'artifacts'
        self.artifact_directory.mkdir()
        self.receipts = IndexWriter(directory/'receipts.jsonl')
        self.ranges = self.reader = self.receipt_reader = self.verifier = None
        try:
            self.ranges = IndexWriter(directory/'projection-ranges.jsonl')
            self.reader = journal_path.open('rb', buffering=0)
            self.receipt_reader = ReceiptCursor(self.receipts.path)
            self.verifier = verifier_factory()
        except BaseException:
            self.release_handles()
            raise
        self.time = TimeResolver(expected_hz)
        self.framer = Framer()
        self.read_offset = self.cursor = self.receipt_frontier = self.received_end = 0
        self.primary_end = 0
        self.eligible = self.projected = self.opaque = self.tail_bytes = 0
        self.closed = self.verified = 0
        self.active = self.held = self.last_range = None
        self.failed = self.finished = False
        self.last_record = None

    def event(self, kind, reason, day=None, previous_day=None, artifact_id=None, offset=None):
        self.emit(kind, reason, dict(artifact_id=artifact_id, source_offset=self.cursor if offset is None else offset,
            day=day, previous_day=previous_day, generation=self.time.generation, message=reason))

    def receipt(self, chunk, count):
        if self.failed:
            return
        if chunk.offset != self.received_end:
            raise OSError('Non-contiguous receipt index')
        end = chunk.offset + count
        self.receipts.append(dict(schema_id='gnss_logger.stage2.receipt',schema_version=2,
            contract_version='stage2.3/v2',**self.owners,
            sequence=chunk.sequence, start=chunk.offset, end=end,
            received_utc=chunk.received_utc, monotonic_ns=chunk.monotonic_ns))
        self.received_end = end

    def sync_receipts(self, primary_synced):
        if self.failed:
            return
        self.receipts.sync()
        self.receipt_frontier = min(primary_synced, self.received_end)

    def _range(self, start, end, kind, artifact=None, destination=None):
        record = dict(schema_id='gnss_logger.stage2.range',schema_version=2,contract_version='stage2.3/v2',
                      **self.owners,start=start, end=end, kind=kind,
                      artifact_id=artifact, destination_offset=destination)
        prev = self.last_range
        if (prev and prev['end'] == start and prev['kind'] == kind and prev['artifact_id'] == artifact
                and (destination is None or prev['destination_offset']+prev['end']-prev['start'] == destination)):
            prev['end'] = end
        else:
            self._flush_range()
            self.last_range = record

    def _flush_range(self):
        if self.last_range:
            self.ranges.append(self.last_range)
            self.last_range = None

    def _poll(self, wait=False):
        first_error = None
        while self.verifier.pending:
            result = self.verifier.take(wait)
            if result is None:
                break
            task, error = result
            self.last_record = task
            if error is None:
                try:
                    if task['state'] == 'CLOSED_COMPLETE':
                        publish_exclusive(Path(task['path']), Path(task['final_path']))
                        task['path'] = task['final_path']
                    from .schema import validate_record
                    validate_record(task)
                    write_new_json(self.artifact_directory/(task['artifact_id']+'.json'), task)
                    self.verified += 1
                    self.event('SEGMENT_CLOSED', task['close_reason'], task['utc_day'],
                               artifact_id=task['artifact_id'], offset=task['source_end'])
                except Exception as failure:
                    error = failure
            if error is not None:
                task.update(state='UNVERIFIED', error=str(error))
                # Preserve the intent and original data even if metadata publication failed.
                try:
                    write_new_json(self.artifact_directory/(task['artifact_id']+'.failed.json'), task)
                except Exception:
                    pass
                if first_error is None:
                    first_error = error
            if not wait:
                continue
        if first_error:
            raise first_error

    def _close_active(self, complete, reason):
        if self.active is None:
            return
        writer, self.active = self.active, None
        self.last_record = writer.record
        try:
            task = writer.close(complete, reason)
            self.closed += 1
            self.verifier.submit(task)
        except Exception as error:
            writer.record.update(state='UNVERIFIED',error=str(error))
            try:
                write_new_json(self.artifact_directory/(writer.record['artifact_id']+'.failed.json'),writer.record)
            except Exception:
                pass
            raise

    def _route(self, start, original, frame, day, generation):
        if frame is None:
            self._range(start,start+len(original),'OPAQUE')
            return
        self._poll()
        if self.active and (self.active.record['utc_day'], self.active.record['generation']) != (day,generation):
            previous = self.active.record['utc_day']
            self._close_active(True,'utc_day_or_time_generation')
            self.event('ROTATION','utc_day_or_time_generation',day,previous,offset=start)
        if self.active is None:
            self.active = self.writer_factory(self.root,self.station,day,generation,self.owners,
                                               self.artifact_directory,start)
            self.event('SEGMENT_OPENED','new_segment',day,artifact_id=self.active.record['artifact_id'],offset=start)
        destination = self.active.written
        self.active.append(original,frame,start+len(original))
        self.projected += len(original)
        self._range(start,start+len(original),'FRAME',self.active.record['artifact_id'],destination)

    def _release_held(self, day):
        if self.held is None:
            return
        start, end, generation = self.held
        framer = Framer(start)
        # Separate read-only handle; never move the main cursor backwards.
        with self.journal_path.open('rb', buffering=0) as handle:
            handle.seek(start)
            while handle.tell() < end:
                data = handle.read(min(4096,end-handle.tell()))
                if not data:
                    raise OSError('Primary prefix vanished during projection')
                for offset, original, frame in framer.feed(data):
                    self._route(offset,original,frame,day,generation)
        if framer.parser.buffer:
            raise OSError('Held range did not end at a frame boundary')
        self.held = None

    def advance(self, budget=65536):
        if self.failed:
            return
        self._poll()
        remaining = min(budget,self.receipt_frontier-self.read_offset)
        while remaining > 0:
            data = self.reader.read(min(4096,remaining))
            if not data:
                raise OSError('Missing synced primary prefix')
            self.read_offset += len(data)
            remaining -= len(data)
            for start, original, frame in self.framer.feed(data):
                self.cursor = start+len(original)
                if frame is None:
                    self.opaque += len(original)
                    if self.held:
                        self.held[1] = self.cursor
                    else:
                        self._route(start,original,None,None,self.time.generation)
                    continue
                self.eligible += len(original)
                receipt = self.receipt_reader.at(self.cursor)
                before = self.time.snapshot()
                decision = self.time.update(frame,receipt['monotonic_ns'])
                if decision['release']:
                    self._release_held(decision['release_day'])
                if self.time.snapshot() != before and (self.time.state != before['state'] or self.time.generation != before['generation']):
                    self.event('TIME_BINDING',self.time.reason,decision['day'],offset=start)
                if decision['hold']:
                    if self.held is None:
                        self.held = [start,self.cursor,self.time.generation]
                    else:
                        self.held[1] = self.cursor
                else:
                    self._route(start,original,frame,decision['day'],self.time.generation)

    def checkpoint(self):
        if not self.failed:
            if self.active:
                self.active.sync()
            self._flush_range()
            self.ranges.sync()
        return self.snapshot()

    def snapshot(self):
        return dict(root=str(self.root),cursor=self.cursor,eligible_bytes=self.eligible,
            projected_bytes=self.projected,opaque_bytes=self.opaque,tail_bytes=self.tail_bytes,
            pending_bytes=self.eligible-self.projected,
            backlog_bytes=self.primary_end-self.cursor,receipt_frontier=self.receipt_frontier,
            receipt_index=self.receipts.synced,range_index=self.ranges.synced if self.ranges else None,
            artifact_directory=str(self.artifact_directory.resolve()),closed_artifacts=self.closed,
            verified_artifacts=self.verified,verification_pending=self.verifier.pending if self.verifier else 0,
            failed=self.failed,finished=self.finished,time_state=self.time.state,generation=self.time.generation,
            time_policy=self.time.policy(),time_checkpoint=dict(self.time.snapshot(),held=self.held,
                read_offset=self.read_offset,framer_pending_offset=self.framer.offset),
            active=dict(self.active.record) if self.active else self.last_record)

    def finish(self, complete, reason):
        error = None
        try:
            if not self.failed:
                while self.read_offset < self.receipt_frontier:
                    self.advance()
                self._release_held(None)
                start, tail = self.framer.tail()
                if tail:
                    self.tail_bytes += len(tail)
                    self.cursor = start+len(tail)
                    self._range(start,self.cursor,'TAIL')
            self._close_active(complete and not self.failed,reason)
            self.checkpoint()
        except Exception as failure:
            error = failure
        finally:
            # Always close active descriptor and drain verification, including disk failures.
            if self.active:
                try:
                    self._close_active(False,'projection_error')
                except Exception as failure:
                    error = error or failure
            try:
                self._poll(wait=True)
            except Exception as failure:
                error = error or failure
            if self.verifier:
                self.verifier.close()
            try:
                self.release_handles()
            except Exception as failure:
                error = error or failure
        self.failed |= error is not None
        self.finished = (not self.failed and self.cursor == self.primary_end
                         and self.eligible == self.projected and self.closed == self.verified)
        self.failed |= not self.finished
        if error:
            raise error

    def release_handles(self):
        errors = []
        for resource in (self.reader,self.receipt_reader,self.receipts,self.ranges):
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:
                    errors.append(error)
        if errors:
            raise errors[0]


def empty_snapshot(root, directory, expected_hz):
    time = TimeResolver(expected_hz)
    return dict(root=str(Path(root).resolve()), cursor=0, eligible_bytes=0, projected_bytes=0,
        opaque_bytes=0,tail_bytes=0,pending_bytes=0,backlog_bytes=0,receipt_frontier=0,
        receipt_index=None,range_index=None,artifact_directory=str((directory/'artifacts').resolve()) if directory else str(Path(root).resolve()),
        closed_artifacts=0,verified_artifacts=0,verification_pending=0,failed=False,finished=False,
        time_state='UNDATED',generation=0,time_policy=time.policy(),time_checkpoint=time.snapshot(),active=None)
