"""Neutral recording services: no CLI, signals, hardware or stdout parsing."""
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from gnss_logger.application.recording import RecordingHandle, run_recording, start_recording
from gnss_logger.recording.contracts import RecordSettings, DeviceBinding
from gnss_logger.recording.controller import RecordController
from test_stage2_record import ScriptReader, Clock, USB, stream


class ApplicationRecordingTests(TestCase):
    def test_shutdown_before_start_and_failed_thread_start_do_not_hang(self):
        with tempfile.TemporaryDirectory() as root:
            settings,controller,_ = self.make(root)
            handle = RecordingHandle(settings,controller=controller)
            self.assertIsNone(handle.shutdown(wait=True))
            with patch('gnss_logger.application.recording.Thread.start',side_effect=RuntimeError('thread start failed')):
                with self.assertRaises(RuntimeError): handle.start()
            with self.assertRaises(RuntimeError): handle.result(wait=True,timeout=.01)
            self.assertEqual(handle.snapshot()['lifecycle'],'FAILED')

    def make(self, root, *, stop_after_read=False, error=None):
        clock = Clock()
        holder = {}
        class Reader(ScriptReader):
            def read(self, remaining):
                chunk = super().read(remaining)
                if stop_after_read:
                    holder['handle'].stop()
                    holder['handle'].stop()
                return chunk
        controller = RecordController(reader_factory=lambda s,c:Reader(c,
            [(.1,stream())]+([error] if error else []),clock),clock=clock)
        settings = RecordSettings(Path(root),'COM_TEST',DeviceBinding('ZED-F9P',
            USB['vid'],USB['pid'],usb_location=USB['usb_location']),None)
        return settings,controller,holder

    def test_direct_service_and_async_worker_without_console_or_signals(self):
        for background in (False,True):
            with self.subTest(background=background), tempfile.TemporaryDirectory() as root:
                settings,controller,_ = self.make(root)
                with patch('signal.signal',side_effect=AssertionError('signal in API')), \
                     patch('builtins.print',side_effect=AssertionError('print in API')):
                    if background:
                        handle = start_recording(settings,controller=controller)
                        report = handle.result(wait=True,timeout=5)
                    else:
                        report = run_recording(settings,controller=controller)
                self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'],0)
                self.assertEqual(Path(controller.artifacts[0]['path']).read_bytes(),stream())

    def test_repeat_stop_is_canonical_and_finalization_is_complete(self):
        with tempfile.TemporaryDirectory() as root:
            settings,controller,holder = self.make(root,stop_after_read=True)
            handle = holder['handle'] = RecordingHandle(settings,controller=controller)
            self.assertIsNone(handle.result())
            handle.start()
            report = handle.result(wait=True,timeout=5)
            self.assertEqual(report['extensions']['gnss_logger.recording']['stop_reason'],'stop_requested')
            self.assertEqual(report['registration'],'STOPPED_COMPLETE')
            handle.stop()
            with self.assertRaises(RuntimeError): handle.start()
            view = handle.snapshot()
            view['recording']['errors'].append('foreign')
            self.assertNotIn('foreign',handle.snapshot()['recording']['errors'])

    def test_error_is_not_masked_by_stop_and_auto_requires_projection(self):
        with tempfile.TemporaryDirectory() as root:
            settings,controller,_ = self.make(root,error=OSError('USB disconnected'))
            handle = start_recording(settings,controller=controller)
            report = handle.result(wait=True,timeout=5)
            handle.stop()
            self.assertEqual(report['registration'],'PARTIAL')
            self.assertEqual(report['extensions']['gnss_logger.recording']['exit_code'],1)
            with self.assertRaises(ValueError): RecordingHandle(settings,convert_config=Path('unused'))
