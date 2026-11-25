"""Manual conversion coordinator contracts; no receivers or installed converter."""
from copy import deepcopy
from dataclasses import replace
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

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from gnss_logger.gui.conversion_coordinator import (
        ConversionCoordinator, ConversionMonitorWorker)
    from gnss_logger.gui.workers import TaskWorker
    from gnss_logger.gui.receiver_coordinator import ReceiverCoordinator
    from gnss_logger.gui.receivers import MockReceiverBackend


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class ConversionCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='stage44 conversion ')
        self.addCleanup(self.directory.cleanup)
        self.backend = MockConversionBackend(Path(self.directory.name))
        self.settings = self.backend.load_settings()
        self.owners = []
        self.releases = []

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail('Timed out waiting for manual conversion lifecycle')

    def tearDown(self):
        for release in self.releases:
            release.set()
        for owner in self.owners:
            owner.request_shutdown(cancel=True)
            self.wait_for(lambda: owner.shutdown_complete)
            owner.deleteLater()
        self.app.processEvents()

    def owner(self):
        owner = ConversionCoordinator(self.backend)
        self.owners.append(owner)
        return owner

    def request(self, scenario='success'):
        return api.ConversionRequest(input=self.backend.make_fixture(scenario))

    def blocked_job(self):
        """Use the real job protocol with a deterministic execute barrier."""
        entered, release, cancelled = Event(), Event(), Event()
        self.releases.append(release)

        def execute(request, sources, *, cancel, progress, **kwargs):
            progress(dict(status='RUNNING', segments=[], errors=[], warnings=[]))
            entered.set()
            if not release.wait(8):
                raise RuntimeError('test release timed out')
            if cancel.is_set():
                cancelled.set()
            return dict(status='INTERRUPTED' if cancel.is_set() else 'SUCCESS',
                        exit_code=130 if cancel.is_set() else 0,
                        segments=[], warnings=[], errors=[])

        patcher = patch('gnss_logger.conversion.job.execute', side_effect=execute)
        patcher.start()
        self.addCleanup(patcher.stop)
        return api.ConversionJob(), entered, release, cancelled

    def test_load_settings_delivers_and_releases_owner(self):
        owner = self.owner()
        loaded = []
        owner.settings_loaded.connect(loaded.append)
        self.assertTrue(owner.load())
        self.assertTrue(owner.busy)
        self.assertFalse(owner.load())
        self.wait_for(lambda: not owner.busy)
        self.assertEqual(loaded, [self.settings])
        self.assertEqual(owner.settings, self.settings)

    def test_load_failure_is_reported_and_can_retry(self):
        owner = self.owner()
        errors = []
        owner.error_raised.connect(errors.append)
        with patch.object(self.backend, 'load_settings', side_effect=ValueError('bad config')):
            self.assertTrue(owner.load())
            self.wait_for(lambda: not owner.busy)
        self.assertIn('bad config', errors[0])
        self.assertIsNone(owner.settings)
        self.assertTrue(owner.load())
        self.wait_for(lambda: not owner.busy)
        self.assertEqual(owner.settings, self.settings)

    def test_loader_thread_start_failure_releases_admission(self):
        owner = self.owner()
        errors = []
        owner.error_raised.connect(errors.append)
        with patch.object(TaskWorker, 'start', side_effect=RuntimeError('loader start failed')):
            self.assertFalse(owner.load())
        self.assertFalse(owner.busy)
        self.assertIn('loader start failed', errors[0])
        self.assertTrue(owner.load())
        self.wait_for(lambda: not owner.busy)

    def test_shutdown_waits_for_loader_without_blocking_ui_or_delivering_settings(self):
        owner = self.owner()
        entered, release = Event(), Event()
        self.releases.append(release)
        loaded, ready, ticks = [], [], []
        owner.settings_loaded.connect(loaded.append)
        owner.ready_to_close.connect(lambda: ready.append(True))
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        self.addCleanup(timer.stop)

        def load(path):
            entered.set()
            if not release.wait(8):
                raise RuntimeError('test release timed out')
            return self.settings

        with patch.object(self.backend, 'load_settings', side_effect=load):
            self.assertTrue(owner.load())
            self.wait_for(entered.is_set)
            owner.request_shutdown()
            self.assertFalse(owner.shutdown_complete)
            self.assertFalse(owner.load())
            self.wait_for(lambda: len(ticks) >= 3)
            release.set()
            self.wait_for(lambda: owner.shutdown_complete)
        self.assertEqual(loaded, [])
        self.assertEqual(ready, [True])

    def test_mock_results_preserve_wire_status_quality_and_outputs(self):
        for scenario, status, code, lifecycle in (
                ('success', 'SUCCESS_WITH_WARNINGS', 0, 'FINISHED'),
                ('obs_only', 'OBS_ONLY_INCOMPLETE_NAVIGATION', 3, 'FAILED'),
                ('skipped', 'SKIPPED', 0, 'FINISHED'),
                ('failed', 'FAILED', 1, 'FAILED')):
            with self.subTest(scenario=scenario):
                owner = self.owner()
                results, events = [], []
                owner.result_ready.connect(results.append)
                owner.events_ready.connect(events.extend)
                request = self.request(scenario)
                original = request.input.read_bytes()
                settings = replace(self.settings, allow_obs_without_nav=scenario == 'obs_only')
                self.assertTrue(owner.start(request, settings))
                self.wait_for(lambda: not owner.busy)
                self.assertEqual(len(results), 1)
                result = results[0]
                raw = result['conversion']
                expected_statuses = ('SUCCESS', status) if scenario == 'success' else (status,)
                self.assertIn(raw['status'], expected_statuses)
                self.assertEqual(raw['exit_code'], code)
                self.assertEqual(result['lifecycle'], lifecycle)
                self.assertEqual(owner.phase, lifecycle)
                self.assertEqual(raw['schema_id'], 'gnss_logger.conversion')
                self.assertEqual(raw['segments'][0]['status'], raw['status'])
                self.assertEqual(request.input.read_bytes(), original)
                self.assertEqual(sum(e['kind'] == 'result' for e in events), 1)
                if scenario == 'obs_only':
                    self.assertEqual(set(raw['segments'][0]['published_outputs']), {'obs'})
                saved = deepcopy(owner.result)
                raw['segments'].clear()
                self.assertEqual(owner.result, saved)

    def test_missing_executable_never_creates_job(self):
        owner = self.owner()
        errors = []
        owner.error_raised.connect(errors.append)
        settings = replace(self.settings, executable=Path(self.directory.name) / 'missing.exe')
        with patch.object(self.backend, 'create_job') as create:
            self.assertFalse(owner.start(self.request(), settings))
        create.assert_not_called()
        self.assertFalse(owner.busy)
        self.assertTrue(errors)

    def test_cancel_retains_ownership_until_actual_completion(self):
        owner = self.owner()
        job, entered, release, cancelled = self.blocked_job()
        results = []
        owner.result_ready.connect(results.append)
        with patch.object(self.backend, 'create_job', return_value=job) as create:
            self.assertTrue(owner.start(api.ConversionRequest(), self.settings))
            self.wait_for(entered.is_set)
            self.assertFalse(owner.start(api.ConversionRequest(), self.settings))
            create.assert_called_once()
            self.assertTrue(owner.cancel())
            self.assertTrue(owner.busy)
            self.assertTrue(owner.view()['job_active'])
            self.assertTrue(owner.view()['cancel_requested'])
            self.assertEqual(results, [])
            release.set()
            self.wait_for(lambda: not owner.busy)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(results[0]['lifecycle'], 'CANCELLED')
        self.assertEqual(results[0]['conversion']['status'], 'INTERRUPTED')
        self.assertFalse(owner.cancel())

    def test_shutdown_wait_or_cancel_delivers_result_before_ready(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                owner = self.owner()
                job, entered, release, cancelled = self.blocked_job()
                order = []
                owner.result_ready.connect(lambda result: order.append('result'))
                owner.ready_to_close.connect(lambda: order.append('ready'))
                with patch.object(self.backend, 'create_job', return_value=job):
                    self.assertTrue(owner.start(api.ConversionRequest(), self.settings))
                    self.wait_for(entered.is_set)
                    owner.request_shutdown(cancel=cancel)
                    self.assertFalse(owner.shutdown_complete)
                    self.assertFalse(owner.start(api.ConversionRequest(), self.settings))
                    release.set()
                    self.wait_for(lambda: owner.shutdown_complete)
                self.assertEqual(cancelled.is_set(), cancel)
                self.assertEqual(order, ['result', 'ready'])
                owner.request_shutdown()
                self.assertEqual(order, ['result', 'ready'])

    def test_observer_thread_start_failure_never_starts_job(self):
        owner = self.owner()
        job = api.ConversionJob()
        errors = []
        owner.error_raised.connect(errors.append)
        with patch.object(self.backend, 'create_job', return_value=job), patch.object(
                ConversionMonitorWorker, 'start', side_effect=RuntimeError('observer start failed')), \
                patch.object(job, 'start') as start:
            self.assertFalse(owner.start(api.ConversionRequest(), self.settings))
        start.assert_not_called()
        self.assertFalse(owner.busy)
        self.assertEqual(owner.phase, 'FAILED')
        self.assertIn('observer start failed', errors[0])

    def test_monitor_failure_cancels_but_waits_for_job_completion(self):
        owner = self.owner()
        job, entered, release, cancelled = self.blocked_job()
        errors, results = [], []
        owner.error_raised.connect(errors.append)
        owner.result_ready.connect(results.append)
        with patch.object(self.backend, 'create_job', return_value=job), patch(
                'gnss_logger.gui.conversion_coordinator.api.OperationMonitor.refresh',
                side_effect=RuntimeError('monitor failed')):
            self.assertTrue(owner.start(api.ConversionRequest(), self.settings))
            self.wait_for(lambda: entered.is_set() and bool(errors))
            self.assertTrue(owner.busy)
            self.assertEqual(results, [])
            owner.request_shutdown()
            self.assertFalse(owner.shutdown_complete)
            release.set()
            self.wait_for(lambda: owner.shutdown_complete)
        self.assertTrue(cancelled.is_set())
        self.assertIn('monitor failed', errors[0])
        self.assertEqual(results[0]['lifecycle'], 'CANCELLED')

    def test_job_thread_start_failure_releases_observer_asynchronously(self):
        owner = self.owner()
        job = api.ConversionJob()
        errors, results = [], []
        owner.error_raised.connect(errors.append)
        owner.result_ready.connect(results.append)
        with patch.object(self.backend, 'create_job', return_value=job), patch.object(
                job, 'start', side_effect=RuntimeError('job thread start failed')):
            self.assertFalse(owner.start(api.ConversionRequest(), self.settings))
            owner.request_shutdown()
            self.wait_for(lambda: owner.shutdown_complete)
        self.assertFalse(owner.busy)
        self.assertEqual(owner.phase, 'FAILED')
        self.assertTrue(any('job thread start failed' in error for error in errors))
        self.assertEqual(results, [])


class _AutoHandle:
    """Admission and current cancellation are independent of the queued work."""
    def __init__(self):
        self.calls = []
        self.queue = ['queued-first', 'queued-second']
        self.cancelled = Event()

    def set_auto_conversion(self, enabled):
        self.calls.append(('set_auto_conversion', enabled))

    def cancel_conversion(self):
        self.calls.append(('cancel_conversion',))
        self.cancelled.set()
        return True


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class ReceiverAutoConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.owner = ReceiverCoordinator(MockReceiverBackend())
        self.handle = _AutoHandle()

    def tearDown(self):
        # No recording was launched: remove the injected handle before shutdown.
        self.owner._handle = None
        self.owner.request_shutdown()
        self.assertTrue(self.owner.shutdown_complete)
        self.owner.deleteLater()
        self.app.processEvents()

    def activate(self):
        self.assertTrue(self.owner.configure_auto(object(), enabled=True))
        self.owner._handle = self.handle
        self.owner._auto_capable = True

    def test_auto_off_changes_admission_cancel_only_targets_current_queue_unchanged(self):
        self.activate()
        queue = self.handle.queue
        original = list(queue)
        self.assertTrue(self.owner.set_auto_conversion(False))
        self.assertEqual(self.handle.calls, [('set_auto_conversion', False)])
        self.assertFalse(self.handle.cancelled.is_set())
        self.assertFalse(self.owner.view()['auto_enabled'])
        self.assertTrue(self.owner.view()['auto_capable'])
        self.assertTrue(self.owner.cancel_conversion())
        self.assertEqual(self.handle.calls, [
            ('set_auto_conversion', False), ('cancel_conversion',)])
        self.assertTrue(self.handle.cancelled.is_set())
        self.assertFalse(self.owner.view()['auto_enabled'])
        self.assertIs(self.handle.queue, queue)
        self.assertEqual(queue, original)
        self.assertTrue(self.owner.set_auto_conversion(True))
        self.assertTrue(self.owner.view()['auto_enabled'])
        self.assertEqual(self.handle.calls[-1], ('set_auto_conversion', True))
        self.assertEqual(queue, original)

    def test_cancel_auto_keeps_admission_enabled(self):
        self.activate()
        self.assertTrue(self.owner.cancel_conversion())
        self.assertEqual(self.handle.calls, [('cancel_conversion',)])
        self.assertTrue(self.owner.view()['auto_enabled'])

    def test_auto_configuration_and_active_controls_require_capability(self):
        with self.assertRaises(ValueError):
            self.owner.configure_auto(enabled=True)
        self.assertFalse(self.owner.set_auto_conversion(True))
        self.assertFalse(self.owner.cancel_conversion())
        self.owner._handle = self.handle
        self.assertFalse(self.owner.configure_auto(object(), enabled=True))
        self.assertFalse(self.owner.set_auto_conversion(True))
        self.assertFalse(self.owner.cancel_conversion())
        self.assertEqual(self.handle.calls, [])

    def test_auto_progress_view_is_detached(self):
        self.activate()
        snapshot = dict(enabled=True, queued=2, current={'artifact_id': 'active'})
        self.owner._receive([dict(kind='progress', lifecycle='RECORDING',
                                  data={'auto_conversion': snapshot})])
        snapshot['current']['artifact_id'] = 'foreign'
        view = self.owner.view()
        self.assertEqual(view['auto_conversion']['current']['artifact_id'], 'active')
        view['auto_conversion']['current'].clear()
        self.assertEqual(self.owner.view()['auto_conversion']['current'],
                         {'artifact_id': 'active'})

    def test_application_recording_auto_uses_real_core_and_injected_mock_runner(self):
        with tempfile.TemporaryDirectory(prefix='stage44 auto ') as directory:
            root = Path(directory).resolve()
            backend = MockConversionBackend(root)
            provider = backend.prepare_recording(MockReceiverBackend(interval=0.01))
            conversion_settings = backend.load_settings()
            diagnostic = provider.diagnose(api.DiagnosticRequest('MOCK-X20P'))
            usb = diagnostic['usb_identity']
            binding = api.DeviceBinding(diagnostic['identity']['model'],
                                        usb['vid'], usb['pid'], usb_serial=usb['usb_serial'])
            settings = api.RecordSettings(
                initial_port='MOCK-X20P', binding=binding, output=root / 'runs',
                daily_root=root / 'data', station='MOCK', seconds=None)
            handle = provider.create_recording(settings, conversion_settings=conversion_settings)
            handle.start()
            try:
                deadline = time.monotonic() + 8
                while handle.snapshot()['recording']['extensions'][
                        'gnss_logger.diagnostics']['RAWX']['epochs'] < 3:
                    self.assertIsNone(handle.result())
                    self.assertLess(time.monotonic(), deadline, 'recording did not receive RAWX')
                    self.app.processEvents()
                    time.sleep(0.01)
                handle.stop()
                report = handle.result(wait=True, timeout=15)
                self.assertIsNotNone(report, 'recording and automatic conversion did not finish')
                summary = report['extensions']['gnss_logger.conversion']
                self.assertTrue(summary['statuses'], str(report))
                self.assertEqual(summary['deferred'], 0)
                self.assertTrue(set(summary['statuses']) <=
                                {'SUCCESS', 'SUCCESS_WITH_WARNINGS', 'SKIPPED'}, str(summary))
                self.assertGreater(sum(summary['statuses'].get(status, 0)
                                       for status in ('SUCCESS', 'SUCCESS_WITH_WARNINGS')), 0)
                self.assertTrue(Path(summary['summary_path']).is_file())
                self.assertTrue(Path(summary['last_report']).is_file())
                self.assertTrue(list((root / 'rinex').rglob('*.obs')))
                self.assertTrue(list((root / 'rinex').rglob('*.nav')))
                self.assertFalse(list(root.rglob('.convbin-*')))
            finally:
                handle.stop()
                handle.result(wait=True, timeout=15)
