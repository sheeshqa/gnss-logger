"""Synthetic record end-to-end and injected I/O faults. Never opens real COM."""
import contextlib
from dataclasses import replace
from datetime import datetime,timezone
import hashlib
import io
import json
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import patch

from gnss_logger.cli import main
from gnss_logger.device.ubx import MON_VER_POLL
from gnss_logger.mock import MockReceiver,synthetic_epoch,synthetic_nav
from gnss_logger.recording.contracts import DeviceBinding,InputChunk,RecordSettings
from gnss_logger.recording.controller import RecordController,run_with_signals
from gnss_logger.recording.reader import make_reader,SerialReader
from gnss_logger.recording.schema import validate_record
from gnss_logger.recording.storage import ByteQueue,Metadata,PrimaryJournal

STAMP='2026-10-27T00:00:00Z'
START=datetime(2026,10,27,tzinfo=timezone.utc)
USB=dict(vid=0x1546,pid=0x01A9,usb_serial=None,usb_location='1-9')


def identity(profile='f9p'):
    mock=MockReceiver(profile)
    mock.send(MON_VER_POLL)
    return mock.responses.popleft()


def stream(profile='f9p'):
    return identity(profile)+synthetic_epoch(START)+synthetic_nav()


class Clock:
    def __init__(self):self.value=0.0
    def __call__(self):return self.value


class ScriptReader:
    simulated=True
    usb=USB
    def __init__(self,conn,steps,clock):
        self.conn,self.steps,self.clock=conn,iter(steps),clock
        self.seq=self.offset=self.reads=0
        self.returned=b''
        self.closed=False
    def start(self):pass
    def read(self,remaining):
        self.reads+=1
        value=next(self.steps)
        if isinstance(value,BaseException):raise value
        instant,data=value
        self.clock.value=instant
        chunk=InputChunk(self.conn,self.seq,self.offset,data,STAMP,max(0,int(instant*1e9)))
        if data:self.seq+=1;self.offset+=len(data)
        self.returned+=data
        return chunk
    def close(self):self.closed=True


class FaultFile:
    def __init__(self,path,mode):self.handle=path.open('xb',buffering=0);self.mode=mode;self.calls=0
    def write(self,data):
        self.calls+=1
        if self.mode=='short':return self.handle.write(data[:3])
        if self.mode=='zero':return 0
        if self.mode=='write':raise OSError('disk failed')
        return self.handle.write(data)
    def flush(self):
        if self.mode=='flush':raise OSError('flush failed')
        self.handle.flush()
    def fileno(self):return self.handle.fileno()
    def close(self):
        self.handle.close()
        if self.mode=='close':raise OSError('close failed')
    @property
    def closed(self):return self.handle.closed


class RecordTests(unittest.TestCase):
    def exercise(self,steps=None,**options):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        clock=Clock()
        holders=[]
        def factory(settings,connection):
            reader=ScriptReader(connection,steps or [(0.1,stream())],clock)
            holders.append(reader)
            return reader
        controller=RecordController(reader_factory=options.pop('reader_factory',factory),clock=clock,**options)
        settings=RecordSettings(root,'COM_TEST',DeviceBinding('ZED-F9P',0x1546,0x01A9,usb_location='1-9'),1)
        report=controller.run(settings)
        code=report['extensions']['gnss_logger.recording']['exit_code']
        validate_record(report)
        manifest=json.loads((controller.directory/'manifest.json').read_text(encoding='utf-8'))
        validate_record(manifest)
        events=[json.loads(s) for s in (controller.directory/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        for e in events:validate_record(e)
        if controller.artifacts:
            raw=Path(controller.artifacts[0]['path']).read_bytes()
            if controller.artifacts[0]['sha256'] is not None:
                self.assertEqual(hashlib.sha256(raw).hexdigest(),controller.artifacts[0]['sha256'])
            else:self.assertEqual(controller.artifacts[0]['state'],'UNVERIFIED')
        self.assertEqual(report['accounting'],manifest['accounting'])
        self.assertEqual(code,manifest['exit_code'])
        self.assertEqual(report['accounting']['pending'],0)
        self.assertTrue(report['accounting']['received_count_exact'])
        return controller,holders[0] if holders else None,code,report,manifest,events

    def test_finite_source_preserves_every_original_byte_and_provenance(self):
        raw=stream()+b'noise\xb5\x62'
        c,r,code,report,m,events=self.exercise([(0.1,raw)])
        self.assertEqual(code,0)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),raw)
        self.assertEqual(report['accounting']['received'],len(raw))
        self.assertEqual(report['accounting']['journal_synced'],len(raw))
        self.assertEqual(report['accounting']['unarchived'],0)
        self.assertTrue(r.closed)
        self.assertEqual(report['registration'],'STOPPED_COMPLETE')
        self.assertTrue(report['extensions']['gnss_logger.recording']['simulated'])
        self.assertEqual(m['time_binding']['status'],'UNDATED')
        self.assertEqual([a['kind'] for a in m['artifacts']],['PRIMARY'])

    def test_read_crossing_deadline_is_stored_and_next_read_never_starts(self):
        c,r,code,report,m,e=self.exercise([(0.25,identity()),(1.01,synthetic_epoch(START)+synthetic_nav()),(2,b'NOT_READ')])
        self.assertEqual((code,r.reads),(0,2))
        self.assertEqual(m['stop_reason'],'duration')
        self.assertAlmostEqual(report['extensions']['gnss_logger.recording']['deadline_overrun_s'],.01)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),r.returned)

    def test_empty_read_crossing_deadline_is_counted(self):
        c,r,code,report,m,e=self.exercise([(0.1,stream()),(1.1,b''),(2,b'NOT_READ')])
        self.assertEqual(r.reads,2)
        self.assertEqual(report['extensions']['gnss_logger.recording']['empty_reads'],1)
        self.assertEqual(code,0)

    def test_partial_ubx_tail_is_retained_and_accounted(self):
        raw=stream()+synthetic_epoch(START)[:9]
        c,r,code,report,m,e=self.exercise([(0.1,raw)])
        self.assertEqual(report['accounting']['unclassified'],9)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),raw)

    def test_source_error_drains_queue_and_closes_partial_file(self):
        raw=stream()
        c,r,code,report,m,e=self.exercise([(0.1,raw),OSError('USB disconnected')])
        self.assertEqual(code,1)
        self.assertEqual(m['artifacts'][0]['state'],'CLOSED_PARTIAL')
        self.assertEqual(report['accounting']['unarchived'],0)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),raw)
        self.assertTrue(any(x['kind']=='SOURCE_ERROR' for x in e))

    def test_keyboard_interrupt_drains_and_finalizes(self):
        c,r,code,report,m,e=self.exercise([(0.1,stream()),KeyboardInterrupt()])
        self.assertEqual(code,130)
        self.assertEqual(m['stop_reason'],'keyboard_interrupt')
        self.assertEqual(report['registration'],'PARTIAL')
        self.assertEqual(report['accounting']['unarchived'],0)

    def test_overflow_emergency_block_is_not_dropped(self):
        raw=stream()*20
        c,r,code,report,m,e=self.exercise([(0.1,raw[i:i+16]) for i in range(0,len(raw),16)],queue_bytes=32)
        self.assertEqual(code,1)
        self.assertLessEqual(c.queue.peak,32)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),r.returned)
        self.assertEqual(report['accounting']['unarchived'],0)
        self.assertTrue(any('queue' in x['component'] for x in c.errors))

    def test_short_write_retains_accepted_prefix_and_reports_remainder(self):
        c,r,code,report,m,e=self.exercise(journal_factory=lambda p:PrimaryJournal(p,opener=lambda q:FaultFile(q,'short')))
        self.assertEqual(code,1)
        self.assertEqual(report['accounting']['journal_written'],3)
        self.assertEqual(report['accounting']['unarchived'],len(r.returned)-3)
        self.assertEqual(m['integrity'],'MISMATCH')
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),r.returned[:3])

    def test_write_flush_and_close_errors_never_succeed(self):
        for mode in ('write','flush','close','zero'):
            with self.subTest(mode=mode):
                c,r,code,report,m,e=self.exercise(journal_factory=lambda p:PrimaryJournal(p,opener=lambda q:FaultFile(q,mode)))
                self.assertEqual(code,1)
                self.assertNotEqual(report['registration'],'STOPPED_COMPLETE')
                self.assertNotEqual(m['artifacts'][0]['state'],'CLOSED_COMPLETE')

    def test_fsync_failure_reports_unsynced_bytes(self):
        def fail(fd):raise OSError('fsync failed')
        c,r,code,report,m,e=self.exercise(journal_factory=lambda p:PrimaryJournal(p,fsync=fail))
        self.assertEqual(code,1)
        self.assertEqual(report['accounting']['journal_synced'],0)
        self.assertGreater(report['accounting']['journal_written'],0)
        self.assertEqual(m['integrity'],'INDETERMINATE')

    def test_periodic_sync_failure_does_not_hide_source_error(self):
        def fail(fd):raise OSError('fsync failed')
        c,r,code,report,m,e=self.exercise([(0.1,stream()),(0.8,b'X'),OSError('USB failure')],
            sync_interval=.5,journal_factory=lambda p:PrimaryJournal(p,fsync=fail))
        self.assertEqual(code,1)
        self.assertTrue(any(x['component']=='source' for x in c.errors))
        self.assertTrue(any('sync' in x['component'] for x in c.errors))

    def test_checkpoints_validate_and_reference_synced_prefix(self):
        c,r,code,report,m,e=self.exercise([(0.1,stream()),(0.8,b'X'),(1.1,b'Y')],sync_interval=.25)
        values=[json.loads(p.read_text(encoding='utf-8')) for p in (c.directory/'checkpoints').glob('*.json')]
        self.assertTrue(values)
        raw=Path(c.artifacts[0]['path']).read_bytes()
        for v in values:
            validate_record(v)
            self.assertEqual(hashlib.sha256(raw[:v['synced_offset']]).hexdigest(),v['prefix_sha256'])

    def test_wrong_receiver_is_not_accepted(self):
        c,r,code,report,m,e=self.exercise([(0.1,stream('x20p'))])
        self.assertEqual(code,1)
        self.assertEqual(m['connections'][0]['identity_status'],'REJECTED')
        self.assertEqual(m['artifacts'][0]['state'],'CLOSED_PARTIAL')

    def test_identity_timeout_is_explicit_and_raw_data_remains(self):
        c,r,code,report,m,e=self.exercise([(0.1,b'X'),(0.7,b'Y')],identity_timeout=.5)
        self.assertEqual(code,1)
        self.assertFalse(c.matched)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),r.returned)

    def test_missing_device_is_reported_without_crash(self):
        def absent(settings,connection):raise OSError('COM missing')
        c,r,code,report,m,e=self.exercise(reader_factory=absent)
        self.assertEqual(code,1)
        self.assertEqual(report['registration'],'FAILED')
        self.assertFalse(m['artifacts'])

    def test_until_stopped_external_signal_preserves_returned_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop=threading.Event();clock=Clock();holders=[]
            class Reader(ScriptReader):
                def read(self,remaining):
                    result=super().read(remaining);stop.set();return result
            def factory(settings,conn):
                r=Reader(conn,[(.1,stream())],clock);holders.append(r);return r
            c=RecordController(reader_factory=factory,clock=clock)
            settings=RecordSettings(Path(tmp),'COM_TEST',DeviceBinding('ZED-F9P',0x1546,0x01A9,usb_location='1-9'),None)
            report=c.run(settings,stop);code=report['extensions']['gnss_logger.recording']['exit_code']
            self.assertEqual(code,0)
            self.assertEqual(c.reason,'stop_requested')
            self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),holders[0].returned)

    def test_controller_reuse_is_rejected(self):
        c,r,code,report,m,e=self.exercise()
        with self.assertRaises(RuntimeError):c.run(c.settings)

    def test_silent_disconnection_tail_is_not_reported_as_success(self):
        c,r,code,report,m,e=self.exercise([(0.1,stream()),(8.0,b'')])
        self.assertEqual(code,1)
        self.assertEqual(report['registration'],'PARTIAL')
        self.assertTrue(report['extensions']['gnss_logger.diagnostics']['live_arrival']['rawx_stale'])
        self.assertEqual(report['accounting']['unarchived'],0)

    def test_source_close_error_is_recorded(self):
        class Reader(ScriptReader):
            def close(self):super().close();raise OSError('COM close failed')
        clock=Clock()
        c,r,code,report,m,e=self.exercise(reader_factory=lambda s,c:Reader(c,[(0,stream())],clock))
        self.assertEqual(code,1)
        self.assertTrue(any(x['component']=='source_close' for x in c.errors))

    def test_checkpoint_publication_error_preserves_all_received_bytes(self):
        class Bad(Metadata):
            def save(self,name,value):
                if name.startswith('checkpoints/'):raise OSError('checkpoint publication failed')
                super().save(name,value)
        c,r,code,report,m,e=self.exercise([(0.1,stream()),(1.1,b'TAIL')],metadata_factory=Bad)
        self.assertEqual(code,1)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),r.returned)
        self.assertEqual(report['accounting']['unarchived'],0)

    def test_event_log_failure_does_not_discard_queued_data(self):
        class Bad(Metadata):
            def __init__(self,p):super().__init__(p);self.count=0
            def event(self,value):
                self.count+=1
                if self.count>2:raise OSError('event log failed')
                super().event(value)
        c,r,code,report,m,e=self.exercise(metadata_factory=Bad)
        self.assertEqual(code,1)
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),r.returned)
        self.assertTrue(any(x['component']=='event_log' for x in c.errors))

    def test_report_failure_is_reflected_by_authoritative_manifest(self):
        class Bad(Metadata):
            def save(self,name,value):
                if name=='report.json':raise OSError('report failed')
                super().save(name,value)
        c,r,code,report,m,e=self.exercise(metadata_factory=Bad)
        self.assertEqual(code,1)
        self.assertEqual(m['exit_code'],1)
        self.assertEqual(report['registration'],'PARTIAL')
        self.assertTrue(any(x['component']=='report' for x in c.errors))

    def test_readback_corruption_is_unverified_and_never_successful(self):
        class Bad(PrimaryJournal):
            def finalize(self,complete):
                def wrong(path):return dict(bytes=self.written,sha256='0'*64)
                return super().finalize(complete,fingerprint=wrong)
        c,r,code,report,m,e=self.exercise(journal_factory=Bad)
        self.assertEqual(code,1);self.assertEqual(m['integrity'],'MISMATCH')
        self.assertEqual(m['artifacts'][0]['state'],'UNVERIFIED')

    def test_x20p_uses_same_pipeline_with_explicit_model_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=Clock()
            c=RecordController(reader_factory=lambda s,c:ScriptReader(c,[(.1,stream('x20p'))],clock),clock=clock)
            settings=RecordSettings(Path(tmp),'SIMULATED_X20P',DeviceBinding('ZED-X20P',0x1546,0x01A9,usb_location='1-9'),1)
            report=c.run(settings);code=report['extensions']['gnss_logger.recording']['exit_code']
            self.assertEqual(code,0);self.assertEqual(c.connections[0]['observed_model'],'ZED-X20P')
            self.assertTrue(c.simulated)

    def test_capacity_and_sync_interval_reject_invalid_values(self):
        for options in (dict(queue_bytes=0),dict(queue_bytes=True),dict(sync_interval=0),dict(sync_interval=float('nan'))):
            with self.subTest(options=options),self.assertRaises(ValueError):RecordController(**options)

    def test_identity_window_starts_at_poll_not_disk_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=Clock()
            def factory(settings,conn):
                clock.value=2.0
                return ScriptReader(conn,[(2.1,b''),(2.2,stream())],clock)
            c=RecordController(reader_factory=factory,clock=clock,identity_timeout=1)
            settings=RecordSettings(Path(tmp),'SIMULATED',DeviceBinding('ZED-F9P',0x1546,0x01A9,usb_location='1-9'),10)
            report=c.run(settings)
            self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'],0)
            self.assertEqual(c.identity_started_clock,2.0)


class PublicationAndQueueTests(unittest.TestCase):
    def test_queue_preserves_order_and_byte_limit_including_overflow(self):
        q=ByteQueue(2)
        a=InputChunk('1'*32,0,0,b'ab',STAMP,0);b=InputChunk('1'*32,1,2,b'c',STAMP,1)
        self.assertTrue(q.put(a));self.assertFalse(q.put(b));q.finish()
        self.assertEqual((q.take().data,q.take().data),(b'ab',b'c'))
        self.assertTrue(q.empty_finished());self.assertLessEqual(q.peak,2)

    def test_existing_primary_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'raw.ubx.part';p.write_bytes(b'ORIGINAL')
            with self.assertRaises(FileExistsError):PrimaryJournal(p)
            self.assertEqual(p.read_bytes(),b'ORIGINAL')

    def test_publish_collision_preserves_both_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'raw.ubx.part';target=p.with_suffix('');target.write_bytes(b'ORIGINAL')
            j=PrimaryJournal(p);j.append(InputChunk('1'*32,0,0,b'NEW',STAMP,0))
            a,errors,actual=j.finalize(True)
            self.assertTrue(errors);self.assertEqual(target.read_bytes(),b'ORIGINAL')
            self.assertEqual(p.read_bytes(),b'NEW');self.assertEqual(a['state'],'CLOSED_PARTIAL')

    def test_metadata_publication_failure_leaves_only_non_authoritative_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=Metadata(Path(tmp))
            with patch('gnss_logger.recording.storage.validate_record'),patch('gnss_logger.recording.storage.write_new_json',side_effect=OSError('metadata sync failed')):
                with self.assertRaises(OSError):m.save('manifest.json',{})
            self.assertFalse((Path(tmp)/'manifest.json').exists());m.close()

    def test_signal_handler_only_requests_stop_and_previous_handler_restored(self):
        class C:
            interrupted=False
            def run(self,settings):
                signal.getsignal(signal.SIGINT)(signal.SIGINT,None)
                self_seen=self.interrupted
                return {'extensions':{'gnss_logger.recording':{'exit_code':130 if self_seen else 0}}}
        previous=signal.getsignal(signal.SIGINT)
        self.assertEqual(run_with_signals(C(),None)[0],130)
        self.assertIs(signal.getsignal(signal.SIGINT),previous)

    def test_physical_factory_checks_binding_before_opening(self):
        from types import SimpleNamespace
        settings=RecordSettings(Path('unused'),'COM3',DeviceBinding('ZED-F9P',0x1546,0x01A9,usb_location='1-9'),1)
        port=SimpleNamespace(device='COM3',vid=0x1546,pid=0x01A9,serial_number=None,location='9-9')
        with patch('serial.tools.list_ports.comports',return_value=[port]),patch('gnss_logger.recording.reader.open_port') as opened:
            with self.assertRaises(OSError):make_reader(settings,'1'*32)
            opened.assert_not_called()

    def test_serial_reader_transmits_only_read_poll_and_bounds_timeout(self):
        class Port:
            def __init__(self):self.writes=[];self._timeout=0;self.timeout_sets=0;self.closed=False
            @property
            def timeout(self):return self._timeout
            @timeout.setter
            def timeout(self,value):self._timeout=value;self.timeout_sets+=1
            def write(self,data):self.writes.append(data);return len(data)
            def read(self,size):return b'X'
            def close(self):self.closed=True
        p=Port();r=SerialReader(p,'1'*32,USB);r.start();r.read(.02);r.read(.02);r.close()
        self.assertEqual(p.writes,[MON_VER_POLL]);self.assertLessEqual(p.timeout,.02);self.assertTrue(p.closed)
        self.assertEqual(p.timeout_sets,1)

    def test_record_cli_runs_synthetic_stream_and_preserves_stage1_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=Clock();holders=[]
            def factory(settings,conn):
                r=ScriptReader(conn,[(.1,stream())],clock);holders.append(r);return r
            def create(**options):return RecordController(reader_factory=factory,clock=clock,**options)
            args=['record','--output',tmp,'--port','COM_TEST','--model','ZED-F9P','--vid','0x1546','--pid','0x01a9',
                  '--usb-location','1-9','--seconds','1','--queue-bytes','8192','--sync-interval','.5']
            with patch('gnss_logger.recording.controller.RecordController',side_effect=create),contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args),0)
            report=json.loads(output.getvalue());validate_record(report)
            manifest=json.loads(Path(report['extensions']['gnss_logger.recording']['paths']['manifest']).read_text(encoding='utf-8'))
            raw=manifest['artifacts'][0]['path']
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['replay',raw,'--session-dir',str(Path(tmp)/'replay')]),0)
            replay=json.loads(output.getvalue())
            self.assertEqual(replay['stream_sha256'],hashlib.sha256(holders[0].returned).hexdigest())
            self.assertEqual(replay['messages'],report['extensions']['gnss_logger.diagnostics']['messages'])

    def test_failed_terminal_manifest_is_not_published_as_success(self):
        class Bad(Metadata):
            def save(self,name,value):
                if name=='manifest.json':raise OSError('manifest failed')
                super().save(name,value)
        with tempfile.TemporaryDirectory() as tmp:
            clock=Clock()
            c=RecordController(reader_factory=lambda s,c:ScriptReader(c,[(.1,stream())],clock),metadata_factory=Bad,clock=clock)
            settings=RecordSettings(Path(tmp),'SIMULATED',DeviceBinding('ZED-F9P',0x1546,0x01A9,usb_location='1-9'),1)
            report=c.run(settings);code=report['extensions']['gnss_logger.recording']['exit_code']
            self.assertEqual(code,1);self.assertEqual(report['registration'],'PARTIAL')
            self.assertFalse((c.directory/'manifest.json').exists())
            provisional=json.loads((c.directory/'report.json').read_text(encoding='utf-8'))
            self.assertTrue(provisional['extensions']['gnss_logger.recording']['commit_manifest_required'])

    def test_journal_protocol_receipt_checkpoint_and_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            j=PrimaryJournal(Path(tmp)/'raw.ubx.part')
            receipt=j.append(InputChunk('1'*32,0,0,b'abc',STAMP,0))
            self.assertEqual((receipt.accepted_bytes,receipt.journal_end),(3,3))
            self.assertEqual(j.checkpoint()['synced_offset'],3)
            closed=j.close('source_exhausted')
            self.assertEqual(closed['artifact']['state'],'CLOSED_COMPLETE')
            self.assertFalse(closed['failures'])
