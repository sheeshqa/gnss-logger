"""Real conversion engine/queue behind the GUI; synthetic data and no COM."""
import importlib.util
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from gnss_logger import application as api
from gnss_logger.gui.conversions import MockConversionBackend
from gnss_logger.gui.receivers import MockReceiverBackend
from gnss_logger.session import file_fingerprint

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtTest import QTest
    from gnss_logger.gui.main_window import MainWindow
    from gnss_logger.gui.conversion_coordinator import ConversionCoordinator


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stage44 integration ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = MockConversionBackend(self.root)
        self.owners = []

    def wait(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail('GUI integration deadline exceeded')

    def tearDown(self):
        for owner in self.owners:
            if isinstance(owner, MainWindow):
                owner.conversion_coordinator.request_shutdown(cancel=True)
                owner.coordinator.request_shutdown()
                self.wait(lambda: owner.coordinator.shutdown_complete and
                          owner.conversion_coordinator.shutdown_complete)
            else:
                owner.request_shutdown(cancel=True)
                self.wait(lambda: owner.shutdown_complete)
            owner.deleteLater()
        self.app.processEvents()

    def test_cancel_second_segment_keeps_first_publication_and_cleans_work(self):
        first = self.backend.make_fixture()
        second = self.backend.make_fixture()
        entered = Event()
        original = self.backend.invoke
        conversions = []

        def invoke(args, cwd, timeout, cancel=None, **kwargs):
            if args[-1] == 'input.ubx':
                conversions.append(str(cwd))
                if len(conversions) == 2:
                    entered.set()
                    return original(args, cwd, timeout, cancel, scenario='slow')
            return original(args, cwd, timeout, cancel, scenario='success')

        owner = ConversionCoordinator(self.backend)
        self.owners.append(owner)
        with patch.object(self.backend, 'invoke', side_effect=invoke), patch(
                'serial.Serial', side_effect=AssertionError('physical COM open')):
            self.assertTrue(owner.start(api.ConversionRequest(input=first.parent), self.backend.load_settings()))
            self.wait(entered.is_set)
            prior = owner._job.snapshot()['progress']['segments'][0]
            saved = {p: Path(p).read_bytes() for p in prior['published_outputs'].values()}
            self.assertTrue(saved)
            owner.cancel()
            self.wait(lambda: not owner.busy)
        self.assertEqual(owner.result['status'], 'INTERRUPTED')
        self.assertEqual(owner.result['exit_code'], 130)
        self.assertEqual(saved, {p: Path(p).read_bytes() for p in saved})
        self.assertEqual(owner.result['segments'][1]['published_outputs'], {})
        self.assertFalse(list(self.root.rglob('.convbin-*')))
        self.assertTrue(second.exists())

    def test_auto_report_read_failure_can_be_retried_explicitly(self):
        backend = self.backend
        job = backend.create_job(api.ConversionRequest(input=backend.make_fixture()), backend.load_settings()).start()
        report = job.result(wait=True, timeout=10)
        window = MainWindow(MockReceiverBackend(), operational=True, conversion_backend=backend)
        self.owners.append(window)
        self.wait(lambda: not window.conversion_coordinator.busy)
        owner = window.coordinator
        owner._auto_snapshot = dict(last_report=report['report_path'])
        errors, results = [], []
        owner.error_raised.connect(errors.append)
        owner.auto_result.connect(results.append)
        with patch('gnss_logger.gui.receiver_coordinator.api.HistoryService', side_effect=OSError('transient metadata failure')):
            self.assertTrue(owner.refresh_auto_report())
            self.wait(lambda: not owner.busy)
        self.assertTrue(errors)
        self.assertEqual(results, [])
        self.assertTrue(owner.refresh_auto_report())
        self.wait(lambda: not owner.busy)
        self.assertEqual(results[0]['conversion']['status'], report['status'])
        self.assertTrue(results[0]['published_files'])

    def test_mock_recording_cannot_write_outside_synthetic_workspace(self):
        window = MainWindow(MockReceiverBackend(), operational=True, conversion_backend=self.backend)
        self.owners.append(window)
        self.wait(lambda: not window.conversion_coordinator.busy)
        provider = window.coordinator.provider
        binding = api.DeviceBinding('ZED-X20P', 0x1546, 0x01A9, usb_serial='MOCK-X20P')
        settings = api.RecordSettings(self.root.parent/'outside-synthetic-workspace', 'MOCK-X20P', binding,
                                      None, station='MOCK')
        with self.assertRaisesRegex(ValueError, 'synthetic workspace'):
            provider.create_recording(settings)

    def test_all_wire_statuses_and_full_path_tooltips_are_preserved(self):
        window = MainWindow(MockReceiverBackend(), operational=True, conversion_backend=self.backend)
        self.owners.append(window)
        self.wait(lambda: not window.conversion_coordinator.busy)
        statuses = ('SUCCESS', 'SUCCESS_WITH_WARNINGS', 'ALREADY_CONVERTED', 'SKIPPED',
                    'FAILED', 'CONFLICT', 'INCOMPLETE_NAVIGATION', 'OBS_ONLY_INCOMPLETE_NAVIGATION',
                    'INTERRUPTED', 'NOT_RUN', 'DRY_RUN')
        report = dict(status='FAILED', exit_code=1, warnings=[], errors=[], segments=[
            dict(status=status, source={'path': str(self.root/status/'source.ubx')},
                 published_outputs={}, warnings=[], errors=[], skip_reason='control_only')
            for status in statuses])
        window.conversion.render_view(api.present_result(report, 'conversion'))
        for row, status in enumerate(statuses):
            item = window.conversion.table.item(row, 1)
            self.assertEqual(item.text(), status)
            self.assertEqual(item.toolTip(), status)
        self.assertIn('CONFLICT', window.conversion.table.item(5, 1).text())
        self.assertIn('3', window.conversion.details.toPlainText())

    def test_real_auto_queue_off_and_cancel_keep_already_accepted_work(self):
        entered = Event()
        original = self.backend.invoke
        count = []

        def invoke(args, cwd, timeout, cancel=None, **kwargs):
            if args[-1] == 'input.ubx':
                count.append(1)
                if len(count) == 1:
                    entered.set()
                    return original(args, cwd, timeout, cancel, scenario='slow')
            return original(args, cwd, timeout, cancel, scenario='success')

        def artifact(path):
            return dict(path=str(path), run_id='a'*32, attempt_id='b'*32, artifact_id=path.stem,
                        utc_day='2026-10-28', **file_fingerprint(path))

        with patch.object(self.backend, 'invoke', side_effect=invoke):
            worker = self.backend.worker_factory(settings=self.backend.load_settings())
            try:
                self.assertTrue(worker.submit(artifact(self.backend.make_fixture())))
                self.wait(entered.is_set)
                self.assertTrue(worker.submit(artifact(self.backend.make_fixture())))
                self.assertEqual(worker.snapshot()['queued'], 1)
                worker.set_enabled(False)
                self.assertFalse(worker.submit(artifact(self.backend.make_fixture())))
                self.assertEqual(worker.snapshot()['queued'], 1)
                self.assertTrue(worker.cancel_current())
                self.wait(lambda: sum(worker.snapshot()['statuses'].values()) == 2)
                state = worker.snapshot()
                self.assertFalse(state['enabled'])
                self.assertEqual(state['statuses']['INTERRUPTED'], 1)
                self.assertEqual(sum(state['statuses'].get(s, 0) for s in ('SUCCESS', 'SUCCESS_WITH_WARNINGS')), 1)
            finally:
                worker.cancel_current()
                worker.finish()
        self.assertFalse(list(self.root.rglob('.convbin-*')))

    def test_manual_recording_and_shutdown_are_independent(self):
        window = MainWindow(MockReceiverBackend(interval=.01), operational=True,
                            conversion_backend=self.backend)
        self.owners.append(window)
        self.wait(lambda: not window.conversion_coordinator.busy)
        recording = window.coordinator
        recording.refresh_devices()
        self.wait(lambda: not recording.busy)
        recording.identify(seconds=1)
        self.wait(lambda: not recording.busy)
        window.recording.output.setText(str(self.root/'runs'))
        window.recording.daily_root.setText(str(self.root/'daily-recording'))
        window.recording.daily_enabled.setChecked(True)
        window.recording.duration_unit.setCurrentIndex(window.recording.duration_unit.findData(None))
        window._start_recording()
        self.wait(lambda: recording._handle.snapshot()['recording']['extensions'][
            'gnss_logger.diagnostics']['RAWX']['epochs'] >= 2)
        window._mock_fixture('slow')
        window.conversion.scenario.setCurrentIndex(window.conversion.scenario.findData('slow'))
        with patch('serial.Serial', side_effect=AssertionError('physical COM open')):
            window._run_conversion()
            self.assertTrue(window.conversion_coordinator.busy)
            self.assertTrue(recording.busy)
            before = recording._handle.snapshot()['recording']['accounting']['received']
            window.conversion_coordinator.cancel()
            self.wait(lambda: not window.conversion_coordinator.busy)
            self.wait(lambda: recording._handle.snapshot()['recording']['accounting']['received'] > before)
            self.assertFalse(recording._handle.snapshot()['stop_requested'])
            # Close asks cancellation separately; finalization stays asynchronous.
            window._mock_fixture('slow')
            window._run_conversion()
            with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                window.close()
            self.assertTrue(recording.closing)
            self.assertTrue(window.conversion_coordinator.closing)
            self.wait(lambda: recording.shutdown_complete and window.conversion_coordinator.shutdown_complete)
        self.assertEqual(window.latest_record_report['registration'], 'STOPPED_COMPLETE')
        self.assertTrue(window.delivery_on_ui_thread)

    def test_auto_failure_does_not_stop_recording_and_final_queue_is_reported(self):
        self.backend.scenario = 'failed'
        window = MainWindow(MockReceiverBackend(interval=.01), operational=True,
                            conversion_backend=self.backend)
        self.owners.append(window)
        self.wait(lambda: not window.conversion_coordinator.busy)
        recording = window.coordinator
        recording.refresh_devices()
        self.wait(lambda: not recording.busy)
        recording.identify(seconds=1)
        self.wait(lambda: not recording.busy)
        page = window.recording
        page.output.setText(str(self.root/'runs'))
        page.daily_root.setText(str(self.root/'daily-recording'))
        page.daily_enabled.setChecked(True)
        page.auto_configured.setChecked(True)
        page.auto_enabled.setChecked(True)
        page.duration_unit.setCurrentIndex(page.duration_unit.findData(None))
        window.conversion.scenario.setCurrentIndex(window.conversion.scenario.findData('failed'))
        window._start_recording()
        self.wait(lambda: recording._handle.snapshot()['recording']['extensions'][
            'gnss_logger.diagnostics']['RAWX']['epochs'] >= 2)
        handle = recording._handle
        # Feed the existing committed-segment callback a completed synthetic
        # artifact while the recorder is active. No GUI file-polling/rotation.
        source = self.backend.make_fixture('failed')
        artifact = dict(path=str(source), run_id=handle.controller.run_id,
                        attempt_id='b'*32, artifact_id='c'*32, utc_day='2026-10-28',
                        **file_fingerprint(source))
        self.assertTrue(handle.controller.on_segment(artifact))
        self.wait(lambda: handle.snapshot()['auto_conversion']['statuses'].get('FAILED') == 1)
        self.wait(lambda: 'FAILED' in page.auto_details.text())
        before = handle.snapshot()['recording']['accounting']['received']
        page.auto_enabled.setChecked(False)
        self.assertFalse(handle.snapshot()['stop_requested'])
        self.wait(lambda: handle.snapshot()['recording']['accounting']['received'] > before)
        self.assertFalse(handle.snapshot()['auto_conversion']['enabled'])
        self.assertFalse(page.auto_details.text().find('FAILED') < 0)
        recording.stop_recording()
        self.wait(lambda: not recording.busy)
        report = window.latest_record_report
        self.assertEqual(report['registration'], 'STOPPED_COMPLETE')
        self.assertEqual(report['extensions']['gnss_logger.conversion']['statuses']['FAILED'], 1)
