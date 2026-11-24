"""Concurrent monitoring with synthetic readers only; no physical ports."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gnss_logger.mock import synthetic_epoch, synthetic_nav
from gnss_logger.recording.contracts import DeviceBinding, InputChunk, RecordSettings
from gnss_logger.recording.controller import RecordController
from gnss_logger.recording.schema import validate_record
from gnss_logger.recording.storage import PrimaryJournal
from test_stage2_record import identity, START, USB


class SnapshotTests(unittest.TestCase):
    def test_monitor_publication_failure_does_not_bypass_drain(self):
        from test_stage2_record import ScriptReader, Clock, stream
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            controller = RecordController(reader_factory=lambda s,c:ScriptReader(c,[(.1,stream())],clock),clock=clock)
            settings = RecordSettings(Path(tmp), 'COM_TEST',
                DeviceBinding('ZED-F9P', USB['vid'], USB['pid'], usb_location=USB['usb_location']), 1)
            original = controller._build_snapshot
            def fail_monitor():
                if controller.state.value != 'STOPPED':
                    raise ValueError('injected monitoring failure')
                return original()
            with patch.object(controller, '_build_snapshot', side_effect=fail_monitor):
                report = controller.run(settings)
            self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'], 0)
            self.assertEqual(Path(controller.artifacts[0]['path']).read_bytes(), stream())
            self.assertNotIn('monitor_error', controller.snapshot()['extensions']['gnss_logger.recording'])

    def test_slow_monitor_does_not_hold_publication_lock(self):
        import json
        from test_stage2_record import ScriptReader, Clock, stream
        decoding, release = threading.Event(), threading.Event()
        original_loads = json.loads
        def slow_decode(value):
            decoding.set()
            if not release.wait(5): raise TimeoutError('test decoder barrier')
            return original_loads(value)
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            controller = RecordController(reader_factory=lambda s,c:ScriptReader(c,[(.1,stream())],clock),clock=clock)
            settings = RecordSettings(Path(tmp), 'COM_TEST',
                DeviceBinding('ZED-F9P', USB['vid'], USB['pid'], usb_location=USB['usb_location']), 1)
            with ThreadPoolExecutor(max_workers=2) as pool:
                with patch('gnss_logger.recording.controller.json.loads', side_effect=slow_decode):
                    monitor = pool.submit(controller.snapshot)
                    try:
                        self.assertTrue(decoding.wait(2))
                        report = pool.submit(controller.run, settings).result(timeout=3)
                        self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'], 0)
                    finally:
                        release.set()
                    monitor.result(timeout=2)

    def test_idle_is_valid_and_detached(self):
        controller = RecordController()
        view = controller.snapshot()
        validate_record(view)
        self.assertEqual(view['extensions']['gnss_logger.recording']['monitor_phase'], 'IDLE')
        view['accounting']['received'] = 999
        view['extensions'].clear()
        self.assertEqual(controller.snapshot()['accounting']['received'], 0)

    def test_concurrent_snapshots_stop_and_finalization_preserve_bytes(self):
        for daily in (False, True):
            with self.subTest(daily=daily), tempfile.TemporaryDirectory() as tmp:
                entered_close, release_close = threading.Event(), threading.Event()
                holders = []
                class Reader:
                    usb = USB
                    simulated = True
                    def __init__(self, settings, connection):
                        self.connection = connection
                        self.offset = self.sequence = 0
                        self.returned = bytearray()
                        holders.append(self)
                    def start(self): pass
                    def read(self, remaining):
                        time.sleep(.005)
                        data = ((identity() if not self.sequence else b'') +
                                synthetic_epoch(START+timedelta(seconds=self.sequence))+synthetic_nav())
                        result = InputChunk(self.connection, self.sequence, self.offset, data,
                                            START.isoformat(), time.monotonic_ns())
                        self.offset += len(data)
                        self.sequence += 1
                        self.returned.extend(data)
                        return result
                    def close(self): pass
                class Journal(PrimaryJournal):
                    def close(self, reason):
                        entered_close.set()
                        if not release_close.wait(5):
                            raise TimeoutError('test finalization barrier')
                        return super().close(reason)
                controller = RecordController(reader_factory=Reader, journal_factory=Journal)
                settings = RecordSettings(Path(tmp)/'primary', 'COM_TEST',
                    DeviceBinding('ZED-F9P', USB['vid'], USB['pid'], usb_location=USB['usb_location']),
                    None, daily_root=Path(tmp)/'daily' if daily else None)
                done = threading.Event()
                def monitor():
                    observations = 0
                    while not done.is_set():
                        value = controller.snapshot()
                        validate_record(value)
                        a = value['accounting']
                        self.assertEqual(a['received'], a['journal_written']+a['pending']+a['unarchived'])
                        value['extensions'].clear()
                        observations += 1
                        time.sleep(.001)
                    return observations
                with ThreadPoolExecutor(max_workers=5) as pool:
                    readers = [pool.submit(monitor) for _ in range(4)]
                    running = pool.submit(controller.run, settings)
                    try:
                        deadline = time.monotonic()+5
                        while controller.snapshot()['accounting']['journal_written'] == 0:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.01)
                        with self.assertRaises(RuntimeError):
                            controller.run(settings)
                        controller.request_stop('stop_requested')
                        self.assertTrue(entered_close.wait(5))
                        self.assertEqual(controller.snapshot()['extensions']['gnss_logger.recording']['monitor_phase'], 'FINALIZING')
                    finally:
                        release_close.set()
                        controller.request_stop('stop_requested')
                        done.set()
                    report = running.result(timeout=10)
                    self.assertTrue(all(f.result(timeout=2) > 0 for f in readers))
                self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'], 0)
                saved = Path(controller.artifacts[0]['path']).read_bytes()
                self.assertEqual(saved, holders[0].returned)
                self.assertEqual(hashlib.sha256(saved).hexdigest(), controller.snapshot()['extensions']['gnss_logger.recording']['received_sha256'])
                self.assertEqual(controller.snapshot()['extensions']['gnss_logger.recording']['monitor_phase'], 'FINISHED')
                self.assertEqual(controller.snapshot()['state'], 'STOPPED')

    def test_setup_failure_has_terminal_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            def missing(settings, connection): raise OSError('external program owns port')
            controller = RecordController(reader_factory=missing)
            settings = RecordSettings(Path(tmp), 'COM_TEST',
                DeviceBinding('ZED-F9P', USB['vid'], USB['pid'], usb_location=USB['usb_location']), 1)
            report = controller.run(settings)
            self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'], 1)
            self.assertEqual(controller.snapshot()['registration'], 'FAILED')
            self.assertEqual(controller.snapshot()['extensions']['gnss_logger.recording']['monitor_phase'], 'FINISHED')
