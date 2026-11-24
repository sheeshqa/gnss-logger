"""Application admission tests. All inventory and serial I/O are synthetic."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from gnss_logger.cli import main
from gnss_logger.device.access import open_device
from gnss_logger.device.ownership import DeviceBusyError, DeviceRegistry, devices
from gnss_logger.recording.contracts import DeviceBinding, InputChunk, RecordSettings
from gnss_logger.recording.controller import RecordController
from gnss_logger.recording.storage import PrimaryJournal
from test_stage2_record import USB, STAMP, stream


class OwnershipTests(unittest.TestCase):
    def test_two_different_receivers_record_concurrently(self):
        from test_stage2_record import Clock, ScriptReader
        registry = DeviceRegistry()
        both_opening = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as tmp:
            def record(index):
                clock = Clock()
                usb = dict(USB,usb_location='1-'+str(index))
                def factory(settings, connection):
                    both_opening.wait(timeout=3)
                    reader = ScriptReader(connection,[(.1,stream())],clock)
                    reader.usb = usb
                    return reader
                controller = RecordController(reader_factory=factory,clock=clock,device_registry=registry)
                settings = RecordSettings(Path(tmp)/str(index),'COM'+str(index),
                    DeviceBinding('ZED-F9P',USB['vid'],USB['pid'],usb_location=usb['usb_location']),1)
                return controller.run(settings)
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = [pool.submit(record,index) for index in (7,8)]
                results = [task.result(timeout=10) for task in tasks]
            self.assertEqual([r['extensions']['gnss_logger.recording']['exit_code'] for r in results],[0,0])

    def test_port_aliases_usb_rename_and_independent_devices(self):
        registry = DeviceRegistry()
        usb = dict(USB, usb_serial='RECEIVER_A')
        with registry.reserve('COM7', usb):
            for port, identity in [('com7',None), ('\\\\.\\COM7',None), ('COM9',usb),
                                   ('COM11',dict(USB))]:
                with self.subTest(port=port), self.assertRaises(DeviceBusyError):
                    registry.reserve(port, identity)
            with registry.reserve('COM8',dict(usb,usb_serial='RECEIVER_B',usb_location='2-1')):
                pass
        with registry.reserve('COM9', usb): pass

    def test_concurrent_reservation_has_exactly_one_winner(self):
        registry = DeviceRegistry()
        barrier = threading.Barrier(8)
        release = threading.Event()
        attempts = []
        lock = threading.Lock()
        def acquire():
            barrier.wait(timeout=3)
            try:
                with registry.reserve('COM7', USB):
                    with lock: attempts.append(True)
                    release.wait(3)
            except DeviceBusyError:
                with lock: attempts.append(False)
        with ThreadPoolExecutor(max_workers=8) as pool:
            tasks = [pool.submit(acquire) for _ in range(8)]
            try:
                deadline = time.monotonic()+3
                while len(attempts) != 8:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.005)
            finally:
                release.set()
            for task in tasks: task.result(timeout=3)
        self.assertEqual(attempts.count(True), 1)

    def test_open_guard_and_error_release_without_real_inventory(self):
        registry = DeviceRegistry()
        connection = Mock()
        opener = Mock(return_value=connection)
        resolver = Mock(return_value=USB)
        with open_device('COM7',115200,registry=registry,resolver=resolver,opener=opener):
            with self.assertRaises(DeviceBusyError):
                open_device('COM7',115200,registry=registry,resolver=resolver,opener=opener)
            # A new COM alias of the same physical USB receiver is also rejected.
            with self.assertRaises(DeviceBusyError):
                open_device('COM9',115200,registry=registry,resolver=resolver,opener=opener)
            self.assertEqual(opener.call_count, 1)
        connection.close.assert_called_once()
        for error_source in ('opener','resolver'):
            kwargs = dict(registry=registry,resolver=Mock(return_value=USB),opener=Mock(return_value=Mock()))
            kwargs[error_source].side_effect = OSError('u-center or inventory failure')
            with self.assertRaises(OSError): open_device('COM7',115200,**kwargs)
            with registry.reserve('COM7', USB): pass
        connection.close.side_effect = OSError('close failed')
        with self.assertRaises(OSError):
            with open_device('COM7',115200,registry=registry,resolver=resolver,opener=opener): pass
        with registry.reserve('COM7', USB): pass

    def test_nested_operation_borrowing_and_upgrade_collision(self):
        registry = DeviceRegistry()
        with registry.reserve('COM7') as lease, lease.activate():
            connection = open_device('COM7',115200,identity=USB,registry=registry,opener=lambda *a:Mock())
            with self.assertRaises(DeviceBusyError):
                open_device('COM7',115200,identity=USB,registry=registry,opener=lambda *a:Mock())
            connection.close()
            with self.assertRaises(DeviceBusyError): registry.reserve('COM9',USB)
        with registry.reserve('COM9',USB): pass
        with registry.reserve('COM9',USB):
            with registry.reserve('COM7') as lease, lease.activate():
                opener = Mock()
                with self.assertRaises(DeviceBusyError):
                    open_device('COM7',115200,identity=USB,registry=registry,opener=opener)
                opener.assert_not_called()
        with registry.reserve('COM7',USB): pass

    def test_record_and_diagnose_guard_until_finalization(self):
        read_started, close_started, finish = (threading.Event() for _ in range(3))
        class Reader:
            simulated = True
            usb = USB
            def __init__(self, settings, conn): self.conn=conn;self.seq=0;self.offset=0
            def start(self): pass
            def read(self, remaining):
                data = stream() if self.seq == 0 else b''
                if not data: time.sleep(.005)
                chunk = InputChunk(self.conn,self.seq,self.offset,data,STAMP,time.monotonic_ns())
                if data: self.seq+=1;self.offset+=len(data)
                read_started.set()
                return chunk
            def close(self): pass
        class Journal(PrimaryJournal):
            def close(self, reason):
                close_started.set()
                if not finish.wait(5): raise TimeoutError('finalization test barrier')
                return super().close(reason)
        with tempfile.TemporaryDirectory() as tmp:
            settings = RecordSettings(Path(tmp)/'primary','COM_TEST',
                DeviceBinding('ZED-F9P',USB['vid'],USB['pid'],usb_location=USB['usb_location']),None)
            controller = RecordController(reader_factory=Reader,journal_factory=Journal)
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(controller.run,settings)
                try:
                    self.assertTrue(read_started.wait(3))
                    factory = Mock()
                    second = RecordController(reader_factory=factory)
                    rejected = second.run(replace(settings,output=Path(tmp)/'second'))
                    self.assertEqual(rejected['extensions']['gnss_logger.recording']['exit_code'],1)
                    factory.assert_not_called()
                    with patch('gnss_logger.cli.open_port') as opened, redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
                        code = main(['diagnose','--port','COM_TEST','--session-dir',str(Path(tmp)/'reports')])
                    self.assertEqual(code,1)
                    self.assertIn('busy',json.loads(out.getvalue())['error'])
                    opened.assert_not_called()
                    raw_open = Mock()
                    def renamed_open(port, baud):
                        return open_device(port,baud,identity=USB,opener=raw_open)
                    with patch('gnss_logger.cli.open_port',side_effect=renamed_open), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        self.assertEqual(main(['diagnose','--port','COM_RENAMED',
                            '--session-dir',str(Path(tmp)/'renamed_diagnostic')]),1)
                    raw_open.assert_not_called()
                    # Same USB device, renumbered COM, rejected before reader factory.
                    renamed = RecordController(reader_factory=factory)
                    renamed.run(replace(settings,initial_port='COM_RENAMED',output=Path(tmp)/'renamed'))
                    factory.assert_not_called()
                    # A different receiver can reserve its own port while A runs.
                    with devices.reserve('COM_OTHER',dict(USB,usb_location='2-7')): pass
                    controller.request_stop('stop_requested')
                    self.assertTrue(close_started.wait(3))
                    with self.assertRaises(DeviceBusyError): devices.reserve('COM_TEST',USB)
                finally:
                    controller.request_stop('stop_requested')
                    finish.set()
                report = running.result(timeout=10)
            self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'],0)
            with devices.reserve('COM_TEST',USB): pass

    def test_controller_releases_after_setup_and_unexpected_finalization_errors(self):
        from test_stage2_record import Clock, ScriptReader
        registry = DeviceRegistry()
        with tempfile.TemporaryDirectory() as tmp:
            settings = RecordSettings(Path(tmp),'COM_TEST',
                DeviceBinding('ZED-F9P',USB['vid'],USB['pid'],usb_location=USB['usb_location']),1)
            def missing(*args): raise OSError('external program owns COM')
            controller = RecordController(reader_factory=missing,device_registry=registry)
            self.assertEqual(controller.run(settings)['extensions']['gnss_logger.recording']['exit_code'],1)
            with registry.reserve('COM_TEST',USB): pass
            class BrokenJournal(PrimaryJournal):
                def close(self,reason):
                    super().close(reason)
                    raise RuntimeError('unexpected injected failure')
            clock = Clock()
            controller = RecordController(reader_factory=lambda s,c:ScriptReader(c,[(.1,stream())],clock),
                journal_factory=BrokenJournal,clock=clock,device_registry=registry)
            with self.assertRaises(RuntimeError): controller.run(settings)
            view = controller.snapshot()['extensions']['gnss_logger.recording']
            self.assertEqual(view['monitor_phase'],'FAILED')
            self.assertIn('unexpected injected failure',view['operation_error'])
            with registry.reserve('COM_TEST',USB): pass
