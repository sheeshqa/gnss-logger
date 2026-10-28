"""One reader, bounded primary ingress; opt-in Stage 2.3 daily projection."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import signal
import sys
import threading
import time
import uuid

from ..diagnostics import Diagnostics
from ..session import versions
from . import CONTRACT_VERSION
from .contracts import ByteAccounting, InputChunk, State, require_transition
from .reader import READ_SIZE, make_reader
from .schema import validate_record
from .storage import ByteQueue, Metadata, PrimaryJournal


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class RecordController:
    def __init__(self, reader_factory=make_reader, journal_factory=PrimaryJournal,
                 metadata_factory=Metadata, clock=time.monotonic, utc_clock=utc_now,
                 queue_bytes=8*1024*1024, sync_interval=1.0, identity_timeout=5.0,
                 projection_factory=None):
        if type(queue_bytes) is not int or queue_bytes <= 0:
            raise ValueError('queue_bytes must be a positive integer')
        import math
        if any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in (sync_interval,identity_timeout)):
            raise ValueError('Sync interval and identity timeout must be finite and positive')
        self.reader_factory, self.journal_factory, self.metadata_factory = reader_factory, journal_factory, metadata_factory
        self.clock, self.utc_clock = clock, utc_clock
        self.sync_interval, self.identity_timeout = sync_interval, identity_timeout
        self.queue = ByteQueue(queue_bytes)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.interrupted = False
        self.run_id, self.attempt_id, self.connection_id, self.journal_id = [uuid.uuid4().hex for _ in range(4)]
        self.state = State.STARTING
        self.received = self.read_calls = self.empty_reads = 0
        self.received_hash = hashlib.sha256()
        self.last_data = self.last_read = None
        self.overrun = 0.0
        self.reader_errors = []
        self.errors = []
        self.error_count = 0
        self.reason = None
        self.directory = self.metadata = self.journal = self.reader = None
        self.connections = []
        self.artifacts = []
        self.sequence = self.checkpoints = self.framed = 0
        self.storage_failed = self.matched = self.simulated = False
        self.primary_write_failed = False
        self.identity_timed_out = False
        self.identity_started_clock = None
        self.unarchived = 0
        self.actual_fingerprint = None
        self.last_sync_utc = None
        self.last_rawx_utc=self.last_sfrbx_utc=None
        self.last_rawx_clock=self.last_sfrbx_clock=None
        self.d = None
        self.projection = None
        self.projection_factory = projection_factory

    def request_stop(self, reason):
        with self.lock:
            if self.reason is None:
                self.reason = reason
        self.stop.set()

    def _base(self, kind):
        if getattr(getattr(self, 'settings', None), 'daily_root', None) is not None:
            return dict(schema_id='gnss_logger.stage2.'+kind, schema_version=2,
                        contract_version='stage2.3/v2',run_id=self.run_id,attempt_id=self.attempt_id)
        return dict(schema_id='gnss_logger.stage2.'+kind, schema_version=1,
                    contract_version=CONTRACT_VERSION, run_id=self.run_id, attempt_id=self.attempt_id)

    def _error(self, component, error, storage=False, emit=True):
        self.error_count += 1
        entry = dict(component=component, error_type=type(error).__name__, message=str(error))
        if len(self.errors) < 100:
            self.errors.append(entry)
        self.request_stop('storage_error' if storage else component+'_error')
        if emit:
            self._emit('STORAGE_ERROR' if storage else 'SOURCE_ERROR', component, entry)
        if component=='source' and self.state in (State.IDENTIFYING,State.RECORDING):
            self._transition(State.DISCONNECTED)

    def _emit(self, kind, reason, details):
        if self.metadata is None or getattr(self,'event_broken',False):
            return
        value = dict(self._base('event'), sequence=self.sequence, connection_id=self.connection_id if self.connections else None,
                     utc=self.utc_clock(), monotonic_ns=max(0,int(self.clock()*1e9)), kind=kind, reason=reason, details=details)
        self.sequence += 1
        try:
            self.metadata.event(value)
        except Exception as error:
            self._error('event_log',error,storage=True,emit=False)
            # Disable a broken event sink, but keep the primary data path alive.
            self.event_broken = True

    def _transition(self, target):
        require_transition(self.state,target)
        before,self.state = self.state,target
        self._emit('STATE_CHANGED','lifecycle',dict(before=before.value,after=target.value))

    def _accounting(self):
        with self.lock:
            received = self.received
        written = self.journal.written if self.journal else 0
        synced = self.journal.synced if self.journal else 0
        opaque = self.d.parser.discarded_bytes if self.d else 0
        return asdict(ByteAccounting(received,written,synced,
            max(0,received-written-self.unarchived),self.unarchived,
            self.framed,opaque,written-self.framed-opaque,True))

    def _producer(self, deadline, external):
        sequence = offset = 0
        try:
            if not self.stop.is_set() and not external.is_set() and not self.interrupted:
                self.identity_started_clock=self.clock()
                self.reader.start()
            while not self.stop.is_set() and not external.is_set() and not self.interrupted:
                remaining = None if deadline is None else deadline-self.clock()
                if remaining is not None and remaining <= 0:
                    self.request_stop('duration')
                    break
                try:
                    source = self.reader.read(remaining)
                except StopIteration:
                    self.request_stop('source_exhausted')
                    break
                # No deadline/stop check here: account for every returned byte first.
                chunk = InputChunk(self.connection_id,sequence,offset,source.data,source.received_utc,source.monotonic_ns)
                with self.lock:
                    self.read_calls += 1
                    self.last_read = source.received_utc
                    if deadline is not None:
                        self.overrun = max(self.overrun,max(0,self.clock()-deadline))
                    self.received += len(chunk.data)
                    self.received_hash.update(chunk.data)
                    if chunk.data:
                        self.last_data = source.received_utc
                    else:
                        self.empty_reads += 1
                if chunk.data:
                    overflow = not self.queue.put(chunk)
                    malformed = (source.sequence != sequence or source.offset != offset or
                                 source.connection_id != self.connection_id or len(chunk.data)>READ_SIZE)
                    offset += len(chunk.data)
                    sequence += 1
                    if overflow or malformed:
                        with self.lock:
                            self.reader_errors.append(('queue' if overflow else 'reader_contract',
                                BufferError('Bounded queue overflow; emergency returned block retained') if overflow else
                                ValueError('Reader chunk metadata/size violates contract; original bytes retained')))
                        self.request_stop('queue_overflow' if overflow else 'reader_contract_error')
                        break
        except KeyboardInterrupt:
            self.request_stop('keyboard_interrupt')
        except Exception as error:
            with self.lock:
                self.reader_errors.append(('source',error))
            self.request_stop('source_error')
        finally:
            if self.interrupted:
                self.request_stop('keyboard_interrupt')
            if external.is_set():
                self.request_stop('stop_requested')
            try:
                self.reader.close()
            except Exception as error:
                with self.lock:
                    self.reader_errors.append(('source_close',error))
                self.request_stop('source_close_error')
            self.queue.finish()

    def _process(self, chunk):
        if self.storage_failed and (self.settings.daily_root is None or self.primary_write_failed):
            return  # Counted as unarchived after the producer is joined.
        before = self.journal.written
        try:
            self.journal.append(chunk)
        except Exception as error:
            self.storage_failed = True
            self.primary_write_failed = True
            self._error('primary_write',error,storage=True)
        accepted = self.journal.written-before
        if accepted:
            if self.projection:
                self.projection.primary_end = chunk.offset + accepted
                self._project(lambda: self.projection.receipt(chunk,accepted))
            before_counts=self.d.rawx_count,self.d.sfrbx_count
            frames = self.d.feed(chunk.data[:accepted])
            self.framed += sum(len(payload)+8 for _,_,payload in frames)
            if self.d.rawx_count!=before_counts[0]:
                self.last_rawx_utc,self.last_rawx_clock=chunk.received_utc,chunk.monotonic_ns/1e9
            if self.d.sfrbx_count!=before_counts[1]:
                self.last_sfrbx_utc,self.last_sfrbx_clock=chunk.received_utc,chunk.monotonic_ns/1e9
            # Keep long-running summaries bounded even for malformed time intervals/errors.
            for counter in (self.d.interval_histogram,self.d.decode_errors):
                if len(counter)>128:
                    key = next(k for k in counter if k != 'other')
                    counter['other'] += counter.pop(key)
            if not self.matched and self.d.identity is not None:
                c = self.connections[0]
                c.update(observed_model=self.d.identity.get('model') or None,firmware=self.d.identity.get('firmware') or None,
                         protocol=self.d.identity.get('protocol') or None)
                self.matched = self.settings.binding.matches(dict(model=c['observed_model'],**self.reader.usb))
                c['identity_status'] = 'MATCHED' if self.matched else 'REJECTED'
                c['match_strength'] = ('USB_SERIAL' if self.settings.binding.usb_serial is not None else 'USB_TOPOLOGY') if self.matched else 'NONE'
                self._emit('IDENTITY_CHECKED','MON-VER',dict(identity_status=c['identity_status'],
                    observed_model=c['observed_model'],match_strength=c['match_strength']))
                if self.matched and self.state == State.IDENTIFYING:
                    self._transition(State.RECORDING)
                elif not self.matched:
                    self._error('identity',ValueError('MON-VER model does not match explicit binding'))

    def _sync(self):
        if self.storage_failed or not self.journal:
            return
        try:
            prefix=self.journal.checkpoint()
        except Exception as error:
            self.storage_failed = True
            self._error('primary_sync',error,storage=True)
            return
        self.last_sync_utc = self.utc_clock()
        if self.projection:
            self._project(lambda: self.projection.sync_receipts(self.journal.synced))
            if self.matched:
                self._project(self.projection.advance)
            self._project(self.projection.checkpoint)
        checkpoint = dict(self._base('checkpoint'),journal_id=self.journal_id,utc=self.last_sync_utc,
            **prefix,time_binding=self._time())
        self._with_projection(checkpoint)
        self.checkpoints += 1
        self._save(f'checkpoints/{self.checkpoints:08d}.json',checkpoint)
        self._emit('CHECKPOINT','primary_synced',dict(journal_id=self.journal_id,synced_offset=self.journal.synced))

    def _save(self, name, value):
        try:
            self.metadata.save(name,value)
        except Exception as error:
            self._error('metadata_'+name,error,storage=True)

    def _time(self):
        if self.projection:
            return self.projection.time.binding()
        # Stage 2.2 never assigns a UTC day, even if Diagnostics sees valid RAWX UTC.
        return dict(status='UNDATED',receiver_utc=None,source='NONE',leap_seconds_valid=False,generation=0)

    def _manifest(self, terminal=False, code=None, integrity='NOT_VERIFIED'):
        a = self._accounting()
        result = dict(self._base('manifest'),state=self.state.value,started_utc=self.started,
            ended_utc=self.ended if terminal else None,elapsed_monotonic_ns=max(0,int((self.clock()-self.start)*1e9)),
            clock_discontinuities=int(terminal and self.ended < self.started),read_only=True,
            automatic_delete=False,retention_policy='keep_all',hardware_validation='NOT TESTED',
            binding=asdict(self.settings.binding),parameters=dict(seconds=self.settings.seconds,
                until_stopped=self.settings.seconds is None,baud=self.settings.baud,expected_hz=self.settings.expected_hz,station=self.settings.station),
            output_directory=str(self.settings.output.resolve()),
            versions={k:v for k,v in versions().items() if k in ('gnss_logger','python','platform','dependencies')},connections=self.connections,
            accounting=a,time_binding=self._time(),artifacts=self.artifacts,stop_reason=self.reason if terminal else None,
            exit_code=code,integrity=integrity,extensions={'gnss_logger.recording':self._details()})
        self._with_projection(result)
        validate_record(result)
        return result

    def _details(self):
        return dict(simulated=self.simulated,initial_port=self.settings.initial_port,python_executable=sys.executable,
            queue_capacity_bytes=self.queue.capacity,
            queue_peak_bytes=self.queue.peak,emergency_block_limit_bytes=READ_SIZE,
            sync_interval_s=self.sync_interval,identity_timeout_s=self.identity_timeout,
            received_sha256=self.received_hash.hexdigest(),read_calls=self.read_calls,empty_reads=self.empty_reads,
            last_successful_read_utc=self.last_read,last_data_read_utc=self.last_data,deadline_overrun_s=self.overrun,
            errors=list(self.errors),errors_total=self.error_count,stop_reason=self.reason,
            actual_fingerprint=self.actual_fingerprint,paths=dict(attempt=str(self.directory) if self.directory else None,
                manifest=str(self.directory/'manifest.json') if self.directory else None,
                diagnostic=str(self.directory/'report.json') if self.directory else None,
                events=str(self.directory/'events.jsonl') if self.directory else None),
            diagnostics_basis='journal_written_bytes; not unarchived ingress')

    def snapshot(self):
        complete = bool(self.artifacts and self.artifacts[0]['state']=='CLOSED_COMPLETE' and not self.errors and self.matched)
        status = ('STOPPED_COMPLETE' if complete else 'PARTIAL' if self.received else 'FAILED') if self.state==State.STOPPED else 'ACTIVE' if self.matched else 'STARTING'
        result = dict(self._base('diagnostic'),utc=self.utc_clock(),monotonic_ns=max(0,int(self.clock()*1e9)),
            state=self.state.value,registration=status,phase_quality=self.d.phase_tracker.report()['status'],
            hardware_validation='NOT TESTED',accounting=self._accounting(),last_data_utc=self.last_data,
            last_rawx_utc=self.last_rawx_utc,last_sfrbx_utc=self.last_sfrbx_utc,last_sync_utc=self.last_sync_utc,free_disk_bytes=None,
            queue_bytes=self.queue.bytes,reconnect_attempts=0,gap_events=self.d.gap_events,
            errors=[f"{e['component']}: {e['error_type']}: {e['message']}" for e in self.errors],
            extensions={'gnss_logger.recording':self._details(),'gnss_logger.diagnostics':self.d.report()})
        self._with_projection(result)
        validate_record(result)
        return result

    def _with_projection(self, record):
        if self.settings.daily_root is None:
            return
        if self.projection:
            record['projection'] = self.projection.snapshot()
        else:
            from .projection import empty_snapshot
            record['projection'] = empty_snapshot(self.settings.daily_root,
                self.directory, self.settings.expected_hz)

    def _project(self, action):
        if self.projection is None or self.projection.failed:
            return
        try:
            return action()
        except Exception as error:
            self.projection.failed = True
            self.projection.event('PROJECTION_ERROR',str(error))
            self._error('projection',error,storage=True)

    def run(self,settings,stop=None):
        """Controller protocol: returns the schema-v1 diagnostic Mapping."""
        code,report=self._run(settings,stop)
        return report

    def _run(self, settings, stop=None):
        if self.d is not None:
            raise RuntimeError('Controller instances are single-use')
        self.settings,self.start,self.started = settings,self.clock(),self.utc_clock()
        self.d = Diagnostics(settings.expected_hz)
        if settings.daily_root is not None:
            from ..device.ubx import UBXParser
            self.d.parser = UBXParser(max_payload=65535)
        external = stop or threading.Event()
        thread = None
        observed_errors = 0
        try:
            settings.output.mkdir(parents=True,exist_ok=True)
            run = settings.output/self.run_id
            run.mkdir(exist_ok=False)
            attempts = run/'attempts'
            attempts.mkdir()
            self.directory = attempts/self.attempt_id
            self.directory.mkdir()
            (self.directory/'primary').mkdir()
            (self.directory/'checkpoints').mkdir()
            self.metadata = self.metadata_factory(self.directory)
            self._save('manifest.start.json',self._manifest())
            if not self.stop.is_set() and not external.is_set() and not self.interrupted:
                self.reader = self.reader_factory(settings,self.connection_id)
                self.simulated = bool(getattr(self.reader,'simulated',False))
                self.connections.append(dict(connection_id=self.connection_id,port=settings.initial_port,
                    opened_utc=self.utc_clock(),closed_utc=None,identity_status='UNIDENTIFIED',match_strength='NONE',
                    observed_model=None,firmware=None,protocol=None,**self.reader.usb))
                self._emit('CONNECTION_OPENED','explicit_port',dict(port=settings.initial_port))
                self._transition(State.IDENTIFYING)
                path=self.directory/'primary'/(self.connection_id+'_'+self.journal_id+'.ubx.part')
                self.journal=self.journal_factory(path)
                if settings.daily_root is not None:
                    from .projection import Projection
                    self.projection=(self.projection_factory or Projection)(self.directory,settings.daily_root,
                        settings.station,dict(run_id=self.run_id,attempt_id=self.attempt_id,
                        connection_id=self.connection_id,journal_id=self.journal_id),path,
                        settings.expected_hz,self._emit)
                deadline=None if settings.seconds is None else self.start+settings.seconds
                thread=threading.Thread(target=self._producer,args=(deadline,external),name='gnss-single-reader',daemon=False)
                thread.start()
                last_sync=self.start
                while not self.queue.empty_finished():
                    try:
                        if self.interrupted:self.request_stop('keyboard_interrupt')
                        if external.is_set():self.request_stop('stop_requested')
                        chunk=self.queue.take()
                        if chunk is not None:self._process(chunk)
                        if self.projection and self.matched:
                            self._project(self.projection.advance)
                        if (not self.matched and not self.identity_timed_out and self.identity_started_clock is not None
                                and self.clock()-self.identity_started_clock>=self.identity_timeout):
                            self.identity_timed_out=True
                            self._error('identity_timeout',TimeoutError('No matching MON-VER response'))
                        if self.clock()-last_sync>=self.sync_interval:
                            self._sync();last_sync=self.clock()
                        with self.lock:
                            new_errors=self.reader_errors[observed_errors:]
                            observed_errors=len(self.reader_errors)
                        for component,error in new_errors:self._error(component,error)
                    except KeyboardInterrupt:
                        self.request_stop('keyboard_interrupt')
        except KeyboardInterrupt:
            self.request_stop('keyboard_interrupt')
        except Exception as error:
            self._error('setup',error,storage=self.reader is not None)
        finally:
            if self.interrupted:self.request_stop('keyboard_interrupt')
            if thread is not None:
                self.stop.set()
                thread.join()  # Real reader has <=0.25 s reads; no unsafe thread termination.
                # Unexpected processing failure still drains original returned bytes.
                while not self.queue.empty_finished():
                    chunk=self.queue.take()
                    if chunk is not None:
                        try:self._process(chunk)
                        except Exception as error:
                            self.storage_failed=True;self._error('drain',error,storage=True)
                for component,error in self.reader_errors[observed_errors:]:self._error(component,error)
            elif self.reader is not None:
                try:self.reader.close()
                except Exception as error:self._error('source_close',error)
            if self.reason is None:self.reason='stop_requested'
            if self.connections:self.connections[0]['closed_utc']=self.utc_clock()
            raw_age=None if self.last_rawx_clock is None else max(0,self.clock()-self.last_rawx_clock)
            nav_age=None if self.last_sfrbx_clock is None else max(0,self.clock()-self.last_sfrbx_clock)
            self.d.liveness=dict(rawx_age_s=raw_age,sfrbx_age_s=nav_age,
                rawx_stale=raw_age is not None and raw_age>max(5,3/settings.expected_hz),
                sfrbx_stale=nav_age is not None and nav_age>30,sfrbx_stale_threshold_s=30)
            if (self.d.liveness['rawx_stale'] or self.d.liveness['sfrbx_stale']) and not self.errors and self.reason!='keyboard_interrupt':
                self._error('source_stale',TimeoutError('Observation stream was stale at stop; empty reads do not prove continuous data'))
            if self.storage_failed and self.state in (State.STARTING,State.IDENTIFYING,State.RECORDING,State.DISCONNECTED):
                self._transition(State.STORAGE_ERROR)
            if self.state!=State.STOPPING:self._transition(State.STOPPING)
            if self.projection:
                self._sync()
                if not self.matched:
                    self.projection.failed = True
                try:
                    self.projection.finish(not self.errors and self.matched and self.reason!='keyboard_interrupt',self.reason)
                except Exception as error:
                    self.projection.failed = True
                    self._error('projection_finalize',error,storage=True)
            if self.journal is not None:
                complete=not self.errors and self.matched and self.reason!='keyboard_interrupt'
                closed=self.journal.close(self.reason if complete else 'partial:'+self.reason)
                artifact=closed['artifact']
                errors=[(f['component'],f['error']) for f in closed['failures']]
                self.actual_fingerprint=closed['fingerprint']
                self.artifacts=[dict(artifact,artifact_id=self.journal_id,connection_id=self.connection_id,
                                     kind='PRIMARY',utc_day=None,source_ranges=[])]
                for component,error in errors:self._error('primary_'+component,error,storage=True)
                if self.journal.synced==self.journal.written:self.last_sync_utc=self.utc_clock()
            self.unarchived=self.received-(self.journal.written if self.journal else 0)
            self._transition(State.STOPPED)
            self.ended=self.utc_clock()
        code=1 if self.errors else 130 if self.reason=='keyboard_interrupt' else 3 if not self.matched else (
            2 if not self.d.rawx_count or not self.d.sfrbx_count else 3 if self.d.decode_errors or self.d.parser.bad_checksums or self.d.gap_events else 0)
        if self.journal is None:
            integrity='INDETERMINATE'
        elif self.unarchived or (self.actual_fingerprint is not None and (
                self.actual_fingerprint['bytes']!=self.journal.written or
                self.actual_fingerprint['sha256']!=self.journal.digest.hexdigest())):
            integrity='MISMATCH'
        else:
            integrity='VERIFIED' if self.artifacts[0]['sha256'] is not None and self.journal.synced==self.journal.written else 'INDETERMINATE'
        self._emit('STOPPED',self.reason,dict(exit_code=code,accounting=self._accounting()))
        # Close/sync event log BEFORE publishing the terminal manifest/report.
        if self.metadata:
            for component,error in self.metadata.close():self._error(component,error,storage=True,emit=False)
        if self.errors:code=1
        report=self.snapshot()
        report['extensions']['gnss_logger.recording'].update(exit_code=code,integrity=integrity,commit_manifest_required=True)
        if self.metadata:
            try:self.metadata.save('report.json',report)
            except Exception as error:
                self._error('report',error,storage=True,emit=False);code=1
                report=self.snapshot()
                report['extensions']['gnss_logger.recording'].update(exit_code=code,integrity=integrity,commit_manifest_required=True)
        # Terminal manifest is the LAST publication and the finalization authority.
        if self.metadata:
            try:self.metadata.save('manifest.json',self._manifest(True,code,integrity))
            except Exception as error:
                self._error('manifest',error,storage=True,emit=False);code=1
                report=self.snapshot()
                report['extensions']['gnss_logger.recording'].update(exit_code=code,integrity=integrity,commit_manifest_required=True)
        return code,report


def run_with_signals(controller,settings):
    """SIGINT only requests stop, so it cannot interrupt write/accounting/finalization."""
    previous=signal.getsignal(signal.SIGINT)
    # Signal handlers must not acquire a lock or interrupt data accounting.
    signal.signal(signal.SIGINT,lambda signum,frame:setattr(controller,'interrupted',True))
    try:
        report=controller.run(settings)
        return report['extensions']['gnss_logger.recording']['exit_code'],report
    finally:signal.signal(signal.SIGINT,previous)
