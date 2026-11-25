"""Receiver workflow contracts; Qt event pumping never waits on a UI thread worker."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

from gnss_logger import application as api
from gnss_logger.gui.receivers import MockReceiverBackend

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from gnss_logger.gui.receiver_coordinator import ReceiverCoordinator
    from gnss_logger.gui.main_window import MainWindow
    from gnss_logger.gui.workers import RecordingMonitorWorker


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class ReceiverCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.owners = []
        self.releases = []
        self.workers = []
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail('Timed out waiting for receiver lifecycle')

    def owner(self, provider=None):
        owner = ReceiverCoordinator(provider or MockReceiverBackend(interval=0.01))
        self.owners.append(owner)
        return owner

    def tearDown(self):
        for release in self.releases:
            release.set()
        for owner in self.owners:
            owner.request_shutdown()
            self.wait_for(lambda: owner.shutdown_complete)
            owner.deleteLater()
        for worker, handle in self.workers:
            handle.stop()
            worker.request_stop()
            self.wait_for(worker.isFinished)
            worker.deleteLater()
        self.app.processEvents()

    def discover(self, owner):
        self.assertTrue(owner.refresh_devices())
        self.wait_for(lambda: not owner.busy)

    def identify(self, owner):
        self.discover(owner)
        self.assertTrue(owner.identify())
        self.wait_for(lambda: not owner.busy)
        self.assertTrue(owner.view()['can_start'])

    def parameters(self, seconds=5):
        return dict(output=Path(self.directory.name), station='MOCK', seconds=seconds)

    def operational_window(self):
        window = MainWindow(MockReceiverBackend(interval=0.01), operational=True)
        self.owners.append(window.coordinator)
        self.addCleanup(window.deleteLater)
        window.recording.output.setText(self.directory.name)
        window.show()
        return window

    def test_operational_window_invalid_baud_revokes_binding_and_blocks_start(self):
        window = self.operational_window()
        owner, page = window.coordinator, window.recording
        self.identify(owner)
        self.assertTrue(page.start_record.isEnabled())
        with patch.object(owner.provider, 'create_recording') as create:
            page.baud.setEditText('invalid')
            page.baud.lineEdit().editingFinished.emit()
            self.assertIsNone(owner.binding)
            self.assertFalse(page.start_record.isEnabled())
            # A direct activation must also validate, even if a stale click was queued.
            window._start_recording()
            create.assert_not_called()
        self.assertFalse(owner.busy)
        self.assertTrue(window.log.toPlainText())

    def test_operational_window_rejects_blank_recording_paths(self):
        window = self.operational_window()
        owner, page = window.coordinator, window.recording
        self.identify(owner)
        for field in ('output', 'daily_root'):
            with self.subTest(field=field), patch.object(owner.provider, 'create_recording') as create:
                page.output.setText(self.directory.name)
                page.daily_enabled.setChecked(field == 'daily_root')
                getattr(page, field).setText('   ')
                previous = window.log.toPlainText()
                page.start_record.click()
                create.assert_not_called()
                self.assertFalse(owner.busy)
                self.assertNotEqual(window.log.toPlainText(), previous)
                self.assertTrue(page.start_record.isEnabled())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_operational_window_buttons_follow_admission_busy_and_shutdown(self):
        window = self.operational_window()
        owner, page = window.coordinator, window.recording
        self.assertTrue(page.refresh.isEnabled())
        for button in (page.identify, page.diagnose, page.start_record, page.stop_record):
            self.assertFalse(button.isEnabled())
        page.refresh.click()
        self.wait_for(lambda: not owner.busy)
        self.assertTrue(page.identify.isEnabled())
        self.assertTrue(page.diagnose.isEnabled())
        self.assertFalse(page.start_record.isEnabled())
        entered, release = Event(), Event()
        self.releases.append(release)
        diagnose = owner.provider.diagnose
        def delayed(request):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test release timed out')
            return diagnose(request)
        with patch.object(owner.provider, 'diagnose', side_effect=delayed):
            page.identify.click()
            self.wait_for(entered.is_set)
            for widget in (page.refresh, page.devices, page.baud, page.identify,
                           page.diagnose, page.start_record, page.stop_record,
                           page.parameters, window.settings.polling):
                self.assertFalse(widget.isEnabled())
            release.set()
            self.wait_for(lambda: not owner.busy)
        self.assertTrue(page.start_record.isEnabled())
        # Exercise the window's signal/render wiring without starting another recorder.
        state = dict(owner.view(), busy=True, recording_active=True, phase='RECORDING', can_start=False)
        owner.state_changed.emit(state)
        self.assertTrue(page.stop_record.isEnabled())
        self.assertFalse(page.start_record.isEnabled())
        self.assertFalse(page.parameters.isEnabled())
        owner.state_changed.emit(dict(state, phase='STOPPING'))
        self.assertFalse(page.stop_record.isEnabled())
        owner.state_changed.emit(dict(state, closing=True))
        self.assertFalse(page.stop_record.isEnabled())
        owner._notify()
        self.assertTrue(page.start_record.isEnabled())
        owner.request_shutdown()
        self.wait_for(lambda: owner.shutdown_complete)
        self.assertFalse(page.start_record.isEnabled())
        self.assertFalse(page.identify.isEnabled())

    def test_operational_window_displays_structured_diagnostic_errors(self):
        window = self.operational_window()
        owner, page = window.coordinator, window.recording
        self.identify(owner)
        report = owner.provider.diagnose(api.DiagnosticRequest(owner.selected_device))
        report['errors'] = [dict(type='OSError', message='MOCK diagnostic failure visible')]
        report['exit_code'] = 1
        previous = window.log.toPlainText()
        with patch.object(owner.provider, 'diagnose', return_value=report):
            page.diagnose.click()
            self.wait_for(lambda: not owner.busy)
        self.assertIn('MOCK diagnostic failure visible', page.identity.text())
        self.assertNotEqual(window.log.toPlainText(), previous)
        self.assertTrue(page.identity.isVisible())
        self.assertTrue(window.log.isVisible())
        self.assertIsNone(owner.binding)
        self.assertFalse(page.start_record.isEnabled())
        self.assertTrue(page.diagnose.isEnabled())

    def test_identification_gates_start_and_view_is_detached(self):
        owner = self.owner()
        self.assertFalse(owner.start_recording(self.parameters()))
        self.discover(owner)
        self.assertFalse(owner.view()['can_start'])
        self.assertTrue(owner.identify())
        self.assertFalse(owner.view()['can_start'])
        self.wait_for(lambda: not owner.busy)
        view = owner.view()
        self.assertTrue(view['mock'])
        self.assertEqual(view['binding']['model'], 'ZED-X20P')
        view['identity'].clear()
        view['binding']['model'] = 'foreign'
        self.assertEqual(owner.view()['binding']['model'], 'ZED-X20P')
        self.assertTrue(owner.identity)

    def test_selection_baud_and_refresh_invalidate_identity(self):
        owner = self.owner()
        self.identify(owner)
        self.assertTrue(owner.select_device('MOCK-F9P'))
        self.assertIsNone(owner.binding)
        self.assertTrue(owner.identify())
        self.wait_for(lambda: not owner.busy)
        self.assertEqual(owner.binding.model, 'ZED-F9P')
        self.assertTrue(owner.set_baud(9600))
        self.assertFalse(owner.view()['can_start'])
        self.assertTrue(owner.identify())
        self.wait_for(lambda: not owner.busy)
        self.assertFalse(owner.set_baud(True))
        self.assertIsNone(owner.binding)
        self.assertTrue(owner.identify())
        self.wait_for(lambda: not owner.busy)
        self.assertTrue(owner.refresh_devices())
        self.assertIsNone(owner.binding)
        self.wait_for(lambda: not owner.busy)

    def test_usb_change_unknown_model_and_error_report_never_admit(self):
        for mutation in ('usb', 'model', 'errors'):
            with self.subTest(mutation=mutation):
                provider = MockReceiverBackend()
                report = provider.diagnose(api.DiagnosticRequest('MOCK-X20P'))
                if mutation == 'usb':
                    report['usb_identity']['usb_serial'] = 'REPLACED'
                elif mutation == 'model':
                    report['identity']['model'] = 'UNKNOWN'
                else:
                    report['errors'] = [dict(type='OSError', message='synthetic failure')]
                owner = self.owner(provider)
                errors = []
                owner.error_raised.connect(errors.append)
                self.discover(owner)
                with patch.object(provider, 'diagnose', return_value=report):
                    self.assertTrue(owner.identify())
                    self.wait_for(lambda: not owner.busy)
                self.assertFalse(owner.view()['can_start'])
                self.assertEqual(owner.phase, 'FAILED')
                self.assertTrue(errors)

    def test_recording_single_owner_busy_gates_and_terminal_result(self):
        owner = self.owner()
        self.identify(owner)
        results, events = [], []
        owner.record_result.connect(results.append)
        owner.events_ready.connect(events.extend)
        with patch.object(owner.provider, 'create_recording', wraps=owner.provider.create_recording) as create:
            self.assertTrue(owner.start_recording(self.parameters(None)))
            self.assertFalse(owner.start_recording(self.parameters()))
            self.assertFalse(owner.diagnose())
            self.assertFalse(owner.identify())
            self.assertFalse(owner.refresh_devices())
            self.assertFalse(owner.select_device('MOCK-F9P'))
            self.assertFalse(owner.set_baud(9600))
            create.assert_called_once()
            self.wait_for(lambda: owner._handle.snapshot()['recording']['extensions'][
                'gnss_logger.diagnostics']['RAWX']['epochs'] >= 2)
            self.assertTrue(owner.stop_recording())
            self.wait_for(lambda: not owner.busy)
        self.assertEqual(len(results), 1)
        self.assertTrue(any(event['kind'] == 'result' for event in events))
        self.assertTrue(owner.view()['can_start'])
        self.assertTrue(list(Path(self.directory.name).rglob('manifest.json')))

    def test_stop_waits_for_final_manifest_and_actual_result(self):
        owner = self.owner()
        self.identify(owner)
        results = []
        owner.record_result.connect(results.append)
        self.assertTrue(owner.start_recording(self.parameters(None)))
        self.wait_for(lambda: owner._handle.snapshot()['recording']['extensions'][
            'gnss_logger.diagnostics']['RAWX']['epochs'] >= 2)
        self.assertTrue(owner.stop_recording())
        self.assertTrue(owner.busy)
        self.assertEqual(owner.phase, 'STOPPING')
        self.wait_for(lambda: not owner.busy)
        self.assertEqual(results[0]['extensions']['gnss_logger.recording']['stop_reason'], 'stop_requested')
        self.assertTrue(list(Path(self.directory.name).rglob('manifest.json')))

    def test_shutdown_during_diagnostic_keeps_ui_alive_until_task_finishes(self):
        entered, release = Event(), Event()
        self.releases.append(release)
        provider = MockReceiverBackend()
        original = provider.diagnose
        def slow(request):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test release timed out')
            return original(request)
        owner = self.owner(provider)
        self.discover(owner)
        ready, ticks = [], []
        owner.ready_to_close.connect(lambda: ready.append(True))
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        self.addCleanup(timer.stop)
        with patch.object(provider, 'diagnose', side_effect=slow):
            self.assertTrue(owner.diagnose())
            self.wait_for(entered.is_set)
            owner.request_shutdown()
            self.assertTrue(owner.busy)
            self.assertFalse(owner.shutdown_complete)
            self.wait_for(lambda: len(ticks) >= 3)
            release.set()
            self.wait_for(lambda: owner.shutdown_complete)
        self.assertEqual(ready, [True])
        self.assertIsNone(owner.binding)

    def test_shutdown_during_recording_delivers_result_before_ready(self):
        owner = self.owner()
        self.identify(owner)
        order = []
        owner.record_result.connect(lambda result: order.append('result'))
        owner.ready_to_close.connect(lambda: order.append('ready'))
        owner.start_recording(self.parameters(None))
        owner.request_shutdown()
        self.assertFalse(owner.start_recording(self.parameters()))
        self.wait_for(lambda: owner.shutdown_complete)
        self.assertEqual(order, ['result', 'ready'])

    def test_source_disconnect_is_reported_with_critical_event(self):
        owner = self.owner(MockReceiverBackend(scenario='disconnect', interval=0.01))
        self.identify(owner)
        results, events = [], []
        owner.record_result.connect(results.append)
        owner.events_ready.connect(events.extend)
        owner.start_recording(self.parameters(5))
        self.wait_for(lambda: not owner.busy)
        self.assertEqual(results[0]['extensions']['gnss_logger.recording']['exit_code'], 1)
        self.assertTrue(any(event['kind'] == 'critical' for event in events))

    def test_monitor_failure_retains_owner_until_actual_completion(self):
        owner = self.owner()
        self.identify(owner)
        entered, release = Event(), Event()
        self.releases.append(release)
        original = owner.provider.create_recording
        def create(settings):
            handle = original(settings)
            run = handle.controller.run
            def delayed(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test release timed out')
                return run(*args, **kwargs)
            handle.controller.run = delayed
            return handle
        errors = []
        owner.error_raised.connect(errors.append)
        with patch.object(owner.provider, 'create_recording', side_effect=create), patch(
                'gnss_logger.gui.receiver_coordinator.api.OperationMonitor.refresh',
                side_effect=RuntimeError('synthetic adapter failure')):
            self.assertTrue(owner.start_recording(self.parameters()))
            self.wait_for(lambda: entered.is_set() and bool(errors))
            self.assertTrue(owner.busy)
            self.assertFalse(owner.start_recording(self.parameters()))
            owner.request_shutdown()
            self.assertFalse(owner.shutdown_complete)
            release.set()
            try:
                self.wait_for(lambda: owner.shutdown_complete, timeout=15)
            except AssertionError:
                self.fail(str(dict(phase=owner.phase, complete=owner._record_complete,
                                   terminal=owner._terminal_received,
                                   worker_finished=owner._poller.isFinished() if owner._poller else None,
                                   snapshot=owner._handle.snapshot() if owner._handle else None)))
        self.assertIn('synthetic adapter failure', errors[0])
        self.assertEqual(owner.phase, 'FAILED')


    def test_failure_payload_snapshots_are_detached(self):
        owner = self.owner()
        observed = []
        owner.events_ready.connect(observed.append)
        payload = [dict(kind='critical', lifecycle='FAILED', data=dict(
            errors=[dict(message='synthetic failure')], errors_total=1,
            error='synthetic failure'))]
        original = deepcopy(payload)
        owner._receive(payload)
        payload[0]['data']['errors'].clear()
        self.assertEqual(observed[0], original)
        self.assertEqual(owner.phase, 'FAILED')

    def test_stop_after_terminal_or_completion_never_regresses_phase(self):
        owner = self.owner()
        for terminal, complete in ((True, False), (False, True), (True, True)):
            with self.subTest(terminal=terminal, complete=complete):
                handle = Mock()
                owner._handle = handle
                owner._terminal_received = terminal
                owner._record_complete = complete
                owner.phase = 'FINISHED'
                try:
                    self.assertFalse(owner.stop_recording())
                    self.assertEqual(owner.phase, 'FINISHED')
                    handle.stop.assert_not_called()
                finally:
                    owner._handle = None

    def test_malformed_nested_identity_and_usb_reports_fail_safely(self):
        owner = self.owner()
        self.discover(owner)
        valid = owner.provider.diagnose(api.DiagnosticRequest(owner.selected_device))
        errors, changes = [], []
        owner.error_raised.connect(errors.append)
        owner.identity_changed.connect(changes.append)
        for key in ('identity', 'usb_identity'):
            for malformed in (['invalid'], 'invalid', 42):
                with self.subTest(key=key, malformed=malformed):
                    report = deepcopy(valid)
                    report[key] = malformed
                    with patch.object(owner.provider, 'diagnose', return_value=report):
                        self.assertTrue(owner.identify())
                        self.wait_for(lambda: not owner.busy)
                    self.assertEqual(owner.phase, 'FAILED')
                    self.assertFalse(owner.view()['can_start'])
                    self.assertIsNone(owner.identity)
                    self.assertIsNone(owner.binding)
                    self.assertIn('structure', errors[-1])
                    self.assertIsNone(changes[-1]['binding'])

    def test_monitor_construction_failure_releases_unstarted_handle(self):
        owner = self.owner()
        self.identify(owner)
        errors = []
        owner.error_raised.connect(errors.append)
        for target in ('api.OperationMonitor', 'RecordingMonitorWorker'):
            with self.subTest(target=target):
                handle = Mock()
                with patch.object(owner.provider, 'create_recording', return_value=handle), patch(
                        'gnss_logger.gui.receiver_coordinator.' + target,
                        side_effect=ValueError('synthetic monitor construction failure')):
                    self.assertFalse(owner.start_recording(self.parameters()))
                handle.start.assert_not_called()
                handle.result.assert_not_called()
                self.assertFalse(owner.busy)
                self.assertIsNone(owner._handle)
                self.assertIsNone(owner._poller)
                self.assertEqual(owner.phase, 'FAILED')
                self.assertTrue(owner.view()['can_start'])
                self.assertIn('synthetic monitor construction failure', errors[-1])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_record_monitor_bounds_unacknowledged_batches_and_keeps_terminal(self):
        backend = MockReceiverBackend(interval=0.01)
        binding = api.DeviceBinding('ZED-X20P', 0x1546, 0x01A9, usb_serial='MOCK-X20P')
        settings = api.RecordSettings(Path(self.directory.name), 'MOCK-X20P', binding,
                                      None, station='MOCK')
        handle = backend.create_recording(settings)
        monitor = api.OperationMonitor(handle, 'recording', capacity=2)
        worker = RecordingMonitorWorker(monitor)
        self.workers.append((worker, handle))
        batches, completed = [], []
        worker.batch.connect(batches.append, Qt.ConnectionType.QueuedConnection)
        worker.completed.connect(completed.append, Qt.ConnectionType.QueuedConnection)
        handle.start()
        worker.start()
        self.wait_for(lambda: len(batches) == 1)
        QTest.qWait(300)
        self.assertEqual(len(batches), 1)
        self.assertLessEqual(monitor.queued_count, 4)
        self.wait_for(lambda: handle.snapshot()['recording']['extensions'][
            'gnss_logger.diagnostics']['RAWX']['epochs'] >= 2)
        handle.stop()
        self.wait_for(lambda: handle.result() is not None)
        self.assertEqual(completed, [])
        worker.acknowledge()
        self.wait_for(lambda: bool(completed) and worker.isFinished())
        self.assertTrue(any(event['kind'] == 'result' for batch in batches for event in batch))
        self.assertIsNone(completed[0]['error'])
        self.assertEqual(completed[0]['value'], handle.result())
