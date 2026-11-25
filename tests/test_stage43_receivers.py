"""Stage 4.3 adapters exercise application services without serial hardware."""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gnss_logger import application as api
from gnss_logger.gui.receivers import MockReader, MockReceiverBackend, ReceiverBackend


class ReceiverBackendTests(unittest.TestCase):
    def test_production_delegates_exact_arguments(self):
        backend = ReceiverBackend()
        self.assertFalse(backend.is_mock)
        for method, target, args in (
            ('devices', 'discover_receivers', ()),
            ('diagnose', 'diagnose', (object(),)),
            ('create_recording', 'RecordingHandle', (object(),)),
        ):
            with self.subTest(method=method), patch.object(api, target) as service:
                self.assertIs(getattr(backend, method)(*args), service.return_value)
                service.assert_called_once_with(*args)

    def test_mock_inventory_is_normalized_detached_and_never_scans(self):
        backend = MockReceiverBackend()
        self.assertTrue(backend.is_mock)
        with patch('serial.tools.list_ports.comports', side_effect=AssertionError('inventory')):
            rows = backend.devices()
        self.assertEqual([r['device'] for r in rows], ['MOCK-X20P', 'MOCK-F9P'])
        for row in rows:
            self.assertEqual((row['vid'], row['pid']), (0x1546, 0x01A9))
            self.assertEqual(row['serial_number'], row['device'])
            self.assertIsNone(row['model'])
        rows[0]['serial_number'] = 'foreign'
        self.assertEqual(backend.devices()[0]['serial_number'], 'MOCK-X20P')

    def test_mock_identity_and_inspection_use_real_service(self):
        backend = MockReceiverBackend()
        for port, model in [('MOCK-X20P', 'ZED-X20P'), ('MOCK-F9P', 'ZED-F9P')]:
            with self.subTest(port=port), patch('serial.Serial', side_effect=AssertionError('open')):
                with patch.object(api, 'diagnose', wraps=api.diagnose) as service:
                    report = backend.diagnose(api.DiagnosticRequest(port, inspect=True))
                service.assert_called_once()
                self.assertIs(service.call_args.kwargs['registry'], backend.registry)
                self.assertEqual(report['identity']['model'], model)
                self.assertEqual(report['identity']['firmware'], port)
                self.assertEqual(report['usb_identity']['usb_serial'], port)
                self.assertFalse(report['errors'])
                self.assertTrue(report['diagnostics']['messages'])
                binding = api.DeviceBinding(model, 0x1546, 0x01A9, usb_serial=port)
                self.assertTrue(binding.matches(dict(model=model, **report['usb_identity'])))

    def test_mock_shared_registry_rejects_physical_alias_before_identity(self):
        backend = MockReceiverBackend()
        usb = dict(vid=0x1546, pid=0x01A9, usb_serial='MOCK-X20P')
        with backend.registry.reserve('synthetic-alias', usb):
            report = backend.diagnose(api.DiagnosticRequest('MOCK-X20P'))
        self.assertEqual(report['errors'][0]['type'], 'DeviceBusyError')
        self.assertFalse(backend.diagnose(api.DiagnosticRequest('MOCK-X20P'))['errors'])

    def test_real_recording_handle_commits_bytes_hash_and_manifest(self):
        backend = MockReceiverBackend(interval=0.01)
        binding = api.DeviceBinding('ZED-X20P', 0x1546, 0x01A9, usb_serial='MOCK-X20P')
        chunks = []
        read = MockReader.read
        def capture(reader, remaining):
            chunk = read(reader, remaining)
            chunks.append(chunk)
            return chunk
        with tempfile.TemporaryDirectory() as root:
            settings = api.RecordSettings(Path(root), 'MOCK-X20P', binding, None, station='MOCK')
            with patch('serial.Serial', side_effect=AssertionError('open')), patch.object(
                    MockReader, 'read', capture):
                handle = backend.create_recording(settings)
                self.assertIsInstance(handle, api.RecordingHandle)
                handle.start()
                self.addCleanup(handle.stop)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if handle.snapshot()['recording']['extensions']['gnss_logger.diagnostics']['RAWX']['epochs'] >= 2:
                            break
                        time.sleep(0.01)
                    else:
                        self.fail('Synthetic RAWX did not arrive before the deadline')
                finally:
                    handle.stop()
                    report = handle.result(wait=True, timeout=8)
            self.assertIsNotNone(report)
            manifests = list(Path(root).rglob('manifest.json'))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding='utf-8'))
            self.assertEqual(manifest['binding'], asdict(binding))
            self.assertEqual(manifest['parameters']['station'], 'MOCK')
            self.assertEqual(manifest['integrity'], 'VERIFIED')
            self.assertFalse(manifest['extensions']['gnss_logger.recording']['errors'])
            self.assertTrue(manifest['artifacts'])
            for artifact in manifest['artifacts']:
                data = Path(artifact['path']).read_bytes()
                self.assertGreater(len(data), 0)
                self.assertEqual(hashlib.sha256(data).hexdigest(), artifact['sha256'])
            self.assertEqual(len(manifest['artifacts']), 1)
            self.assertEqual(data, b''.join(chunk.data for chunk in chunks))
            offset = 0
            for sequence, chunk in enumerate(chunk for chunk in chunks if chunk.data):
                self.assertEqual((chunk.sequence, chunk.offset), (sequence, offset))
                offset += len(chunk.data)
            accounting = manifest['accounting']
            self.assertGreater(accounting['received'], 0)
            self.assertEqual(accounting['received'], accounting['journal_written'])
            self.assertEqual(accounting['journal_written'], accounting['journal_synced'])
            self.assertEqual(accounting['pending'], 0)
            self.assertEqual(accounting['unarchived'], 0)
            self.assertEqual(report['gap_events'], 0)
            report.clear()
            self.assertTrue(handle.result())

    def test_mock_station_is_explicit(self):
        binding = api.DeviceBinding('ZED-F9P', 1, 2, usb_serial='synthetic')
        settings = api.RecordSettings(Path('unused'), 'MOCK-F9P', binding, 0.3)
        with self.assertRaises(ValueError):
            MockReceiverBackend().create_recording(settings)
