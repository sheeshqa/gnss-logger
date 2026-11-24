"""Programmatic settings also reach auto conversion, without a TOML editor."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import test_stage31_conversion as fixtures
from gnss_logger.application.recording import RecordingHandle
from gnss_logger.conversion.config import load
from gnss_logger.conversion.worker import SegmentWorker
from gnss_logger.recording.contracts import RecordSettings, DeviceBinding
from gnss_logger.recording.controller import RecordController
from test_stage2_record import Clock, ScriptReader, USB, stream


class ConfigurationTests(TestCase):
    setUp = fixtures.ConversionTests.setUp
    subprocess_fixture = fixtures.ConversionTests.subprocess_fixture
    popen_fixture = fixtures.ConversionTests.popen_fixture

    def test_auto_worker_uses_supplied_settings_without_reloading_toml(self):
        settings = replace(load(self.config), output_root=self.root/'custom-results',timeout_s=123)
        artifact = dict(path=str(self.source),run_id='run',attempt_id='attempt',artifact_id='artifact',
            bytes=len(self.original),sha256=hashlib.sha256(self.original).hexdigest(),utc_day=None)
        with patch('gnss_logger.conversion.engine.subprocess.Popen',self.popen_fixture):
            worker = SegmentWorker(settings=settings)
            try:
                self.assertTrue(worker.submit(artifact))
            finally:
                summary = worker.finish()
        self.assertEqual(sum(summary['statuses'].values()),1)
        report = json.loads(Path(summary['last_report']).read_text(encoding='utf-8'))
        self.assertEqual(report['exit_code'],0)
        self.assertEqual(report['settings']['timeout_s'],123)
        self.assertEqual(Path(report['output_directory']).parent,settings.output_root)
        self.assertEqual(load(self.config).timeout_s,1800)
        view = worker.snapshot()
        self.assertFalse(view['active'])
        self.assertTrue(view['closed'])
        view['statuses'].clear()
        self.assertTrue(worker.snapshot()['statuses'])
        self.assertFalse(worker.cancel_current())

    def test_handle_auto_admission_and_cancel_are_independent(self):
        clock = Clock()
        controller = RecordController(reader_factory=lambda s,c:ScriptReader(c,[(.1,stream())],clock),clock=clock)
        settings = RecordSettings(self.root/'runs','COM_TEST',DeviceBinding('ZED-F9P',
            USB['vid'],USB['pid'],usb_location=USB['usb_location']),1,daily_root=self.root/'daily')
        conversion = load(self.config)
        worker = Mock()
        worker.finish.return_value = dict(statuses={},deferred=0,last_report=None,last_error=None)
        with patch('gnss_logger.application.recording.SegmentWorker',return_value=worker) as factory:
            handle = RecordingHandle(settings,controller=controller,conversion_settings=conversion)
            handle.set_auto_conversion(False)
            handle.start()
            report = handle.result(wait=True,timeout=5)
        self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'],0)
        self.assertIs(factory.call_args.kwargs['settings'],conversion)
        worker.set_enabled.assert_called_with(False)
        worker.cancel_current.assert_not_called()
        handle.cancel_conversion()
        worker.cancel_current.assert_called_once()
