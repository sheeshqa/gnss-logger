"""Hardware-free service contracts; no CLI or process signal machinery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from gnss_logger.application.receivers import DiagnosticRequest, diagnose, discover_receivers, result_code
from gnss_logger.device.access import open_device
from gnss_logger.device.ownership import DeviceBusyError, DeviceRegistry
from gnss_logger.device.requests import ensure_read_only
from gnss_logger.diagnostics import Diagnostics
from gnss_logger.mock import MockReceiver


def inventory(port='receiver-a', location='usb-a'):
    return [SimpleNamespace(device=port, description='advertised F9P', hwid='synthetic',
                            vid=0x1546, pid=1, serial_number=None, location=location)]


class ReceiverTests(unittest.TestCase):
    def run_mock(self, request=None, **kwargs):
        source = MockReceiver()
        connection = Mock()
        def write(data):
            ensure_read_only(data)
            source.send(data)
            return len(data)
        connection.write.side_effect = write
        opened = Mock(return_value=connection)
        report = diagnose(request or DiagnosticRequest('receiver-a', inspect=True),
                          opener=opened, inventory=inventory(), registry=DeviceRegistry(),
                          source_factory=lambda conn: source, clock=source.monotonic, **kwargs)
        connection.close.assert_called_once()
        return report, connection

    def test_discovery_is_neutral_and_detached(self):
        ports = inventory()
        report = discover_receivers(lambda: ports)
        self.assertIsNone(report[0]['model'])
        self.assertEqual(report[0]['model_detection_status'], 'NOT_QUERIED')
        report[0]['device'] = 'changed'
        self.assertEqual(ports[0].device, 'receiver-a')

    def test_request_validation_and_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            DiagnosticRequest('receiver-a').seconds = 2
        for key, value in [('port', ''), ('baud', True), ('seconds', 0),
                           ('seconds', float('inf')), ('expected_hz', float('nan')),
                           ('request_timeout', -1), ('inspect', 1)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                DiagnosticRequest(**dict(port='receiver-a', **{key: value}) if key != 'port'
                                  else {key: value})

    def test_direct_worker_read_only_inspection(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            report, connection = pool.submit(self.run_mock).result(timeout=3)
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(report['stop_reason'], 'source_exhausted')
        self.assertEqual(report['identity']['model'], 'ZED-F9P')
        self.assertGreaterEqual(connection.write.call_count, 2)
        for call in connection.write.call_args_list:
            ensure_read_only(call.args[0])

    def test_passive_does_not_write(self):
        report, connection = self.run_mock(DiagnosticRequest('receiver-a', poll_mon_ver=False))
        connection.write.assert_not_called()
        self.assertIsNone(report['requests'])

    def test_busy_prevents_open_including_physical_alias(self):
        registry = DeviceRegistry()
        usb = dict(vid=0x1546, pid=1, usb_location='usb-a')
        opened = Mock()
        with registry.reserve('another-name', usb):
            result = diagnose(DiagnosticRequest('receiver-a'), registry=registry,
                              inventory=inventory(), opener=opened)
        self.assertEqual(result['exit_code'], 1)
        self.assertEqual(result['errors'][0]['type'], 'DeviceBusyError')
        opened.assert_not_called()

    def test_source_error_and_close_error_are_structured_and_release(self):
        for fail_close in (False, True):
            registry = DeviceRegistry()
            connection = Mock()
            connection.timeout = 0.25
            connection.read.side_effect = OSError('disconnected')
            if fail_close:
                connection.close.side_effect = OSError('close failed')
            report = diagnose(DiagnosticRequest('receiver-a', poll_mon_ver=False),
                              opener=Mock(return_value=connection), registry=registry,
                              inventory=inventory())
            self.assertEqual(report['exit_code'], 1)
            self.assertEqual(report['stop_reason'], 'source_error')
            self.assertTrue(any(e['message'] == 'disconnected' for e in report['errors']))
            if fail_close:
                self.assertTrue(any(e['component'] == 'close' for e in report['errors']))
            with registry.reserve('receiver-a'):
                pass

    def test_reservation_covers_read_restore_and_close_and_other_device(self):
        registry = DeviceRegistry()
        class Source:
            finite = True
            def __iter__(self):
                check_busy()
                return iter(())
            def restore_read_timeout(self):
                check_busy()
        def check_busy():
            with self.assertRaises(DeviceBusyError):
                registry.reserve('receiver-a')
            with registry.reserve('receiver-b', dict(vid=0x1546, pid=1, usb_location='usb-b')):
                pass
        connection = Mock()
        connection.close.side_effect = check_busy
        report = diagnose(DiagnosticRequest('receiver-a', poll_mon_ver=False),
                          registry=registry, inventory=inventory(), opener=Mock(return_value=connection),
                          source_factory=lambda conn: Source())
        self.assertEqual(report['exit_code'], 2)
        with registry.reserve('receiver-a'):
            pass

    def test_default_opener_contract_borrows_outer_lease(self):
        connection = Mock()
        def opened(*args, **kwargs):
            return open_device(*args, **kwargs, opener=lambda *a: connection)
        source = MockReceiver()
        report = diagnose(DiagnosticRequest('receiver-a', poll_mon_ver=False), opener=opened,
                          registry=DeviceRegistry(), inventory=inventory(),
                          source_factory=lambda conn: source, clock=source.monotonic)
        self.assertEqual(report['exit_code'], 0)
        connection.close.assert_called_once()

    def test_progress_dictionary_detached(self):
        source = MockReceiver(epochs=15)
        snapshots = []
        def progress(snapshot):
            snapshots.append(snapshot)
            snapshot['RAWX']['epochs'] = -999
            snapshot['messages'].clear()
        connection = Mock()
        report = diagnose(DiagnosticRequest('receiver-a', poll_mon_ver=False),
                          registry=DeviceRegistry(), inventory=inventory(), opener=lambda *a, **k: connection,
                          source_factory=lambda conn: source, clock=source.monotonic, progress=progress)
        self.assertTrue(snapshots)
        self.assertEqual(report['diagnostics']['RAWX']['epochs'], 15)
        self.assertTrue(report['diagnostics']['messages'])

    def test_result_code_missing_code_valid_and_request_precedence(self):
        d = Diagnostics()
        d.rawx_count = d.sfrbx_count = d.phase_valid = 1
        self.assertEqual(result_code(d), 2)
        d.code_valid = 1
        self.assertEqual(result_code(d), 0)
        d.parser.bad_checksums = 1
        self.assertEqual(result_code(d), 3)
        d.rawx_count = 0
        self.assertEqual(result_code(d, SimpleNamespace(pending=True, results=[])), 3)


if __name__ == '__main__':
    unittest.main()
