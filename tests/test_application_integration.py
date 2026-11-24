"""Offline discovery -> diagnostic -> record -> convert -> history via public API."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import time
from unittest import TestCase
from unittest.mock import Mock, patch

from gnss_logger import application as api
from gnss_logger.conversion.selection import Source
from gnss_logger.device.ownership import DeviceRegistry
from gnss_logger.device.requests import ensure_read_only
from gnss_logger.mock import MockReceiver
from gnss_logger.recording.controller import RecordController
import test_stage31_conversion as fixtures
from test_stage2_record import Clock, ScriptReader, USB, stream
from test_application_receivers import inventory


class IntegrationTests(TestCase):
    setUp = fixtures.ConversionTests.setUp
    subprocess_fixture = fixtures.ConversionTests.subprocess_fixture
    popen_fixture = fixtures.ConversionTests.popen_fixture

    def test_public_services_workers_and_persisted_history(self):
        source = MockReceiver()
        connection = Mock()
        def write(data):
            ensure_read_only(data)
            source.send(data)
            return len(data)
        connection.write.side_effect = write
        discovered = api.discover_receivers(inventory())
        self.assertIsNone(discovered[0]['model'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api.diagnose,api.DiagnosticRequest(discovered[0]['device']),
                opener=lambda *a,**k:connection,registry=DeviceRegistry(),inventory=inventory(),
                source_factory=lambda conn:source,clock=source.monotonic)
            diagnostic_monitor = api.OperationMonitor(future,'diagnostic',capacity=1)
            diagnosis = future.result(timeout=5)
            diagnostic_monitor.refresh()
        self.assertEqual(diagnosis['identity']['model'],'ZED-F9P')
        self.assertEqual(diagnostic_monitor.terminal['data']['exit_code'],0)

        clock = Clock()
        controller = RecordController(reader_factory=lambda s,c:ScriptReader(c,[(.1,stream())],clock),clock=clock)
        record_settings = api.RecordSettings(self.root/'recordings','COM_TEST',
            api.DeviceBinding('ZED-F9P',USB['vid'],USB['pid'],usb_location=USB['usb_location']),1)
        with patch('signal.signal',side_effect=AssertionError('signals in API')), \
             patch('builtins.print',side_effect=AssertionError('console output in API')):
            handle = api.start_recording(record_settings,controller=controller)
            recording = handle.result(wait=True,timeout=5)
        self.assertEqual(recording['registration'],'STOPPED_COMPLETE')
        record_monitor = api.OperationMonitor(handle,'recording',capacity=1)
        record_monitor.refresh()
        self.assertEqual(record_monitor.terminal['operation_id'],handle.operation_id)
        artifact = controller.artifacts[0]
        ubx = Source(Path(artifact['path']),run_id=controller.run_id,attempt_id=controller.attempt_id,
                     artifact_id=artifact['artifact_id'],size=artifact['bytes'],sha256=artifact['sha256'])
        settings = api.load_conversion_settings(self.config)
        with patch('gnss_logger.conversion.engine.subprocess.Popen',self.popen_fixture):
            job = api.ConversionJob(settings=settings,sources=[ubx]).start()
            conversion_monitor = api.OperationMonitor(job,'conversion',capacity=1)
            conversion = job.result(wait=True,timeout=5)
            conversion_monitor.refresh()
        self.assertEqual(conversion['operation_id'],job.operation_id)
        self.assertEqual(conversion_monitor.terminal['operation_id'],job.operation_id)
        self.assertEqual(conversion['exit_code'],0)
        outcome = api.present_result(recording,conversions=[conversion],artifacts=controller.artifacts)
        self.assertEqual(outcome['registration'],'STOPPED_COMPLETE')
        self.assertEqual(set(map(Path,outcome['published_files'])),
                         set(map(Path,conversion['segments'][0]['published_outputs'].values())))
        history = api.HistoryService([record_settings.output],[settings.output_root])
        sessions = history.list_sessions()
        self.assertEqual(len(sessions),1)
        detail = history.get_session(sessions[0]['session_key'])
        self.assertTrue(detail['terminal'])
        self.assertEqual(detail['model'],'ZED-F9P')
        self.assertEqual(detail['recorded_integrity'],'VERIFIED')
        self.assertFalse(detail['independently_verified'])
        self.assertTrue(all(item['present'] for item in detail['ubx_files']))
        linked = history.list_conversions(detail['session_key'])
        self.assertTrue(any(c['operation_id'] == job.operation_id for c in linked))
        self.assertTrue(all(s['match'] == 'MATCHED' for c in linked for s in c['segments']))

    def test_public_obs_only_and_skipped_are_distinct_from_complete_success(self):
        self.nav = None
        settings = replace(api.load_conversion_settings(self.config),allow_obs_without_nav=True)
        with patch('gnss_logger.conversion.engine.subprocess.run',self.subprocess_fixture):
            partial = api.run_conversion(api.ConversionRequest(),[Source(self.source)],settings=settings)
        view = api.present_result(partial,'conversion')
        self.assertEqual(view['conversion']['status'],'OBS_ONLY_INCOMPLETE_NAVIGATION')
        self.assertEqual(view['exit_code'],3)
        self.assertEqual(len(view['published_files']),1)
        from test_stage2_record import identity
        control = self.root/'control.ubx'
        control.write_bytes(identity())
        skipped = api.run_conversion(api.ConversionRequest(),[Source(control)],settings=settings)
        view = api.present_result(skipped,'conversion')
        self.assertEqual(view['conversion']['status'],'SKIPPED')
        self.assertEqual(view['exit_code'],0)
        self.assertFalse(view['published_files'])
