"""Deterministic daily record integration; no serial devices or wall-clock midnight."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_stage2_record import Clock, ScriptReader, USB, identity
from test_stage23_time import START, raw
from gnss_logger.capture.daily import DailyWriter
from gnss_logger.device.ubx import packet
from gnss_logger.mock import synthetic_epoch, synthetic_nav
from gnss_logger.recording.contracts import DeviceBinding, RecordSettings
from gnss_logger.recording.controller import RecordController
from gnss_logger.recording.projection import Projection
from gnss_logger.recording.schema import validate_record
from gnss_logger.recording.verification import Verifier
from gnss_logger.session import file_fingerprint


class ProjectionTests(unittest.TestCase):
    def run_record(self, steps=None, root=None, **options):
        if root is None:
            temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        clock=Clock();holders=[]
        def factory(settings,connection):
            reader=ScriptReader(connection,steps if steps is not None else self.midnight(),clock)
            holders.append(reader);return reader
        settings=RecordSettings(root/'internal/runs','COM_TEST',
            DeviceBinding(options.pop('model','ZED-F9P'),USB['vid'],USB['pid'],usb_location=USB['usb_location']),
            1000,daily_root=root/'data')
        c=RecordController(reader_factory=factory,clock=clock,sync_interval=.01,identity_timeout=1000,**options)
        report=c.run(settings)
        manifest=json.loads((c.directory/'manifest.json').read_text(encoding='utf-8'))
        validate_record(report);validate_record(manifest)
        for path in (c.directory/'checkpoints').glob('*.json'):
            validate_record(json.loads(path.read_text(encoding='utf-8')))
        events=[json.loads(line) for line in (c.directory/'events.jsonl').read_text(encoding='utf-8').splitlines()]
        for event in events:validate_record(event)
        original=holders[0].returned
        self.assertEqual(Path(c.artifacts[0]['path']).read_bytes(),original)
        self.assertEqual(report['accounting']['received'],len(original))
        self.assertEqual(report['accounting']['unarchived'],0)
        self.assertEqual(file_fingerprint(Path(c.artifacts[0]['path']))['sha256'],hashlib.sha256(original).hexdigest())
        return c,report,manifest,events,original

    def midnight(self):
        return [(0.0,identity())]+[(i+1,synthetic_epoch(START+timedelta(seconds=i))+synthetic_nav()) for i in range(4)]

    def verify_projection(self,c,original):
        records={}
        for path in c.projection.artifact_directory.glob('*.json'):
            if '.open.' in path.name or '.failed.' in path.name:continue
            record=json.loads(path.read_text(encoding='utf-8'));records[record['artifact_id']]=record
            self.assertEqual(file_fingerprint(Path(record['path'])),dict(bytes=record['bytes'],sha256=record['sha256']))
        ranges=[json.loads(line) for line in c.projection.ranges.path.read_text(encoding='utf-8').splitlines()]
        expected_start=0;destinations={}
        for span in ranges:
            self.assertEqual(span['start'],expected_start);expected_start=span['end']
            if span['kind']=='FRAME':
                artifact=records[span['artifact_id']]
                start=span['destination_offset'];data=Path(artifact['path']).read_bytes()
                self.assertEqual(data[start:start+span['end']-span['start']],original[span['start']:span['end']])
                self.assertEqual(destinations.get(span['artifact_id'],0),start)
                destinations[span['artifact_id']]=start+span['end']-span['start']
        self.assertEqual(expected_start,len(original))
        self.assertEqual(sum(destinations.values()),c.projection.projected)
        for snapshot in (c.projection.receipts.synced,c.projection.ranges.synced):
            data=Path(snapshot['path']).read_bytes()[:snapshot['bytes']]
            self.assertEqual(hashlib.sha256(data).hexdigest(),snapshot['sha256'])
        return list(records.values())

    def test_midnight_no_loss_duplicates_and_schema_v2(self):
        c,r,m,e,original=self.run_record()
        self.assertEqual(m['exit_code'],0)
        self.assertEqual(r['schema_version'],2)
        artifacts=self.verify_projection(c,original)
        daily=sorted(a['utc_day'] for a in artifacts if a['kind']=='DAILY')
        self.assertEqual(daily,['2026-10-28','2026-10-29'])
        self.assertTrue(any(x['kind']=='ROTATION' for x in e))
        self.assertTrue(r['projection']['finished'])

    def test_undated_then_valid_and_unconfirmed_last_anchor(self):
        steps=[(0,identity()+synthetic_nav()),(1,packet(*raw(flags=0))),
               (2,synthetic_epoch(START)),(3,synthetic_epoch(START+timedelta(seconds=1))),
               (4,synthetic_epoch(START+timedelta(seconds=2)))]
        c,r,m,e,original=self.run_record(steps)
        artifacts=self.verify_projection(c,original)
        # Initial unknown, invalid-time generation, and unconfirmed new day stay separate.
        self.assertEqual(sum(a['kind']=='UNDATED' for a in artifacts),3)
        self.assertEqual(sum(a['kind']=='DAILY' for a in artifacts),1)

    def test_noise_invalid_checksum_and_tail(self):
        steps=self.midnight();steps[-1]=(4,steps[-1][1]+b'noise\xb5\x62')
        c,r,m,e,original=self.run_record(steps)
        self.verify_projection(c,original)
        self.assertEqual(r['projection']['opaque_bytes'],5)
        self.assertEqual(r['projection']['tail_bytes'],2)

    def test_restarts_after_ctrlc_and_usb_preserve_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            snapshots={}
            for error in (KeyboardInterrupt(),OSError('synthetic USB removal'),None):
                steps=self.midnight()[:3]
                if error is not None:steps.append(error)
                c,r,m,e,original=self.run_record(steps,root=root)
                self.assertEqual(m['exit_code'],130 if isinstance(error,KeyboardInterrupt) else 1 if error else 0)
                self.verify_projection(c,original)
                for path,checksum in snapshots.items():self.assertEqual(file_fingerprint(path)['sha256'],checksum)
                snapshots={p:file_fingerprint(p)['sha256'] for p in (root/'data').rglob('*.ubx*') if p.is_file()}
            self.assertTrue(any('_002.ubx' in p.name for p in snapshots))

    def test_projection_write_error_still_preserves_all_primary(self):
        class Broken(DailyWriter):
            def append(self,*args):raise OSError('synthetic daily disk failure')
        def projection(*args):return Projection(*args,writer_factory=Broken)
        c,r,m,e,original=self.run_record(projection_factory=projection)
        self.assertEqual(m['exit_code'],1)
        self.assertTrue(r['projection']['failed'])
        self.assertEqual(m['integrity'],'VERIFIED')
        self.assertTrue(any(x['kind']=='PROJECTION_ERROR' for x in e))

    def test_readback_error_never_claims_success(self):
        def fingerprint(path):raise OSError('synthetic readback failure')
        def projection(*args):return Projection(*args,verifier_factory=lambda:Verifier(fingerprint=fingerprint))
        c,r,m,e,original=self.run_record(projection_factory=projection)
        self.assertEqual(m['exit_code'],1)
        self.assertTrue(r['projection']['failed'])
        self.assertTrue(list(c.projection.artifact_directory.glob('*.failed.json')))

    def test_short_write_fsync_and_index_failures(self):
        for failure in ('short','sync','index'):
            class Broken(DailyWriter):
                def append(self,data,frame,end):
                    if failure=='short':
                        self.handle.write(data[:3]);raise OSError('Short write')
                    return super().append(data,frame,end)
                def sync(self):
                    if failure=='sync':raise OSError('fsync failed')
                    return super().sync()
            def projection(*args):
                p=Projection(*args,writer_factory=Broken)
                if failure=='index':p.receipts.append=lambda value: (_ for _ in ()).throw(OSError('index failed'))
                return p
            with self.subTest(failure=failure):
                c,r,m,e,original=self.run_record(projection_factory=projection)
                self.assertEqual(m['exit_code'],1)
                self.assertTrue(r['projection']['failed'])

    def test_interrupt_before_and_after_midnight(self):
        for epochs in (2,3,4):
            c,r,m,e,original=self.run_record(self.midnight()[:epochs+1]+[KeyboardInterrupt()])
            self.assertEqual(m['exit_code'],130)
            self.verify_projection(c,original)

    def test_mismatching_receiver_does_not_create_projection(self):
        c,r,m,e,original=self.run_record([(0,identity('x20p'))]+self.midnight()[1:])
        self.assertNotEqual(m['exit_code'],0)
        self.assertFalse(list(c.projection.artifact_directory.glob('*.open.json')))

    def test_nested_roots_are_rejected(self):
        settings=RecordSettings(Path('/tmp/runs'),'COM_TEST',DeviceBinding('ZED-F9P',1,2,usb_location='x'),1)
        with self.assertRaises(ValueError):replace(settings,daily_root=settings.output/'daily')

    def test_offline_verifier_checks_real_files_and_detects_modification(self):
        from tools.check_stage2_3_record import verify
        c,r,m,e,original=self.run_record()
        result=verify(c.directory)
        self.assertEqual(result['rawx'],4)
        artifact=next(p for p in (c.settings.daily_root).rglob('*.ubx'))
        with artifact.open('ab') as handle:handle.write(b'changed test copy')
        with self.assertRaises(ValueError):verify(c.directory)

    def test_publish_collision_keeps_both_files(self):
        from gnss_logger.recording.storage import publish_exclusive
        def collision(source,target):
            if not target.exists():target.write_bytes(b'FOREIGN')
            return publish_exclusive(source,target)
        with patch('gnss_logger.recording.projection.publish_exclusive',collision):
            c,r,m,e,original=self.run_record()
        self.assertEqual(m['exit_code'],1)
        self.assertTrue(r['projection']['failed'])
        self.assertTrue(any(p.read_bytes()==b'FOREIGN' for p in c.settings.daily_root.rglob('*.ubx')))
        self.assertTrue(list(c.settings.daily_root.rglob('*.part')))

    def test_artifact_metadata_failure_keeps_primary(self):
        with patch('gnss_logger.capture.daily.write_new_json',side_effect=OSError('metadata failed')):
            c,r,m,e,original=self.run_record()
        self.assertEqual(m['exit_code'],1)
        self.assertTrue(r['projection']['failed'])

    def test_bounded_slow_verification_reports_overflow_without_primary_loss(self):
        gate=threading.Event()
        class Slow(Verifier):
            def __init__(self):
                def fingerprint(path):
                    gate.wait(5)
                    return file_fingerprint(path)
                super().__init__(capacity=1,fingerprint=fingerprint)
            def submit(self,task):
                if self.pending>=self.capacity:gate.set()
                return super().submit(task)
        def projection(*args):return Projection(*args,verifier_factory=Slow)
        try:c,r,m,e,original=self.run_record(projection_factory=projection)
        finally:gate.set()
        self.assertEqual(m['exit_code'],1)
        self.assertTrue(r['projection']['failed'])
        self.assertFalse(c.projection.verifier.thread.is_alive())

    def test_offline_replay_is_explicitly_simulated(self):
        from tools.check_stage2_3_record import replay
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.ubx'
            source.write_bytes(b''.join(data for _,data in self.midnight()))
            before=file_fingerprint(source)
            result=replay(source,root/'new')
            self.assertEqual(result['rawx'],4)
            self.assertTrue(result['simulated'])
            self.assertEqual(file_fingerprint(source),before)
            with self.assertRaises(FileExistsError):replay(source,root/'new')

    def test_x20p_same_projection_without_assuming_configuration_keys(self):
        steps=[(0,identity('x20p'))]+self.midnight()[1:]
        c,r,m,e,original=self.run_record(steps,model='ZED-X20P')
        self.assertEqual(m['exit_code'],0)
        self.verify_projection(c,original)

    def test_primary_sync_failure_does_not_claim_completed_projection(self):
        from gnss_logger.recording.storage import PrimaryJournal
        def broken(path):
            return PrimaryJournal(path,fsync=lambda fd:(_ for _ in ()).throw(OSError('primary fsync failed')))
        c,r,m,e,original=self.run_record(journal_factory=broken)
        self.assertEqual(m['exit_code'],1)
        self.assertFalse(r['projection']['finished'])
        self.assertEqual(r['accounting']['journal_synced'],0)

    def test_daily_short_write_marks_partial_packet_unverified(self):
        class Short(DailyWriter):
            def __init__(self,*args):
                super().__init__(*args)
                handle=self.handle
                class Proxy:
                    def write(self,data):return handle.write(data[:3])
                    def __getattr__(self,name):return getattr(handle,name)
                self.handle=Proxy()
        def projection(*args):return Projection(*args,writer_factory=Short)
        c,r,m,e,original=self.run_record(projection_factory=projection)
        self.assertEqual(m['exit_code'],1)
        failures=list(c.projection.artifact_directory.glob('*.failed.json'))
        self.assertTrue(failures)
        self.assertEqual(json.loads(failures[0].read_text())['state'],'UNVERIFIED')
