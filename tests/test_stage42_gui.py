"""Offline GUI shell gates. Optional Qt tests skip in the CLI-only environment."""
import importlib.util
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from gnss_logger.gui.mock import DemoOperation, MockProvider

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from gnss_logger.application import OperationMonitor
    from gnss_logger.gui.coordinator import Coordinator
    from gnss_logger.gui.main_window import MainWindow
    from gnss_logger.gui.workers import MonitorWorker


class EntrypointTests(unittest.TestCase):
    def test_lightweight_import_does_not_import_qt_or_scan_ports(self):
        code = ("import sys; import gnss_logger.gui; import gnss_logger.gui.app; "
                "assert 'PySide6' not in sys.modules; "
                "assert 'serial.tools.list_ports' not in sys.modules")
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_qt_actionable_message_without_system_exit(self):
        code = ("import sys; sys.modules['PySide6']=None; "
                "from gnss_logger.gui.app import main; assert main([])==2")
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('gnss-logger[gui]', result.stderr)

    def test_mock_has_no_com_names(self):
        rows = MockProvider().devices()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['device'].startswith('MOCK-') for row in rows))

    def test_demo_stop_is_detached_and_does_not_create_scientific_data(self):
        operation = DemoOperation(steps=100, interval=0.01).start()
        operation.stop()
        deadline = time.monotonic() + 3
        while operation.is_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(operation.is_running())
        result = operation.result()
        self.assertTrue(result['simulated'])
        self.assertEqual(result['stop_reason'], 'stop_requested')
        self.assertEqual(result['artifacts'], [])
        result['warnings'].clear()
        self.assertTrue(operation.result()['warnings'])
        with self.assertRaises(RuntimeError):
            operation.start()


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class QtShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.owners = []
        self.workers = []

    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail('Timed out waiting for GUI lifecycle')

    def coordinator(self, provider=None):
        owner = Coordinator(provider)
        self.owners.append(owner)
        return owner

    def window(self, provider=None):
        window = MainWindow(provider)
        self.owners.append(window.coordinator)
        window.show()
        self.addCleanup(window.deleteLater)
        return window

    def discover(self, owner):
        self.assertTrue(owner.refresh_devices())
        self.wait_for(lambda: not owner.busy)

    def tearDown(self):
        for owner in self.owners:
            owner.request_shutdown()
            self.wait_for(lambda: owner.shutdown_complete)
        for worker in self.workers:
            worker.request_stop()
            self.wait_for(worker.isFinished)
        self.app.processEvents()

    def test_four_pages_navigation_and_default_no_hardware(self):
        window = self.window()
        self.assertEqual(window.stack.count(), 4)
        for index in range(4):
            window.sidebar.setCurrentRow(index)
            self.assertEqual(window.stack.currentIndex(), index)
        self.assertFalse(window.recording.start.isEnabled())
        self.assertFalse(window.recording.refresh.isEnabled())
        with patch('serial.tools.list_ports.comports', side_effect=AssertionError('Real inventory forbidden')):
            self.discover(window.coordinator)
        self.assertEqual(window.coordinator.devices, [])

    def test_mock_discovery_selection_and_double_start(self):
        window = self.window(MockProvider())
        self.discover(window.coordinator)
        self.assertEqual(window.recording.devices.count(), 2)
        window.recording.devices.setCurrentIndex(1)
        self.assertEqual(window.coordinator.selected_device, 'MOCK-F9P')
        self.assertTrue(window.coordinator.start_demo())
        self.assertFalse(window.coordinator.start_demo())
        self.assertFalse(window.coordinator.select_device('MOCK-X20P'))
        self.assertFalse(window.recording.devices.isEnabled())
        self.assertFalse(window.recording.start.isEnabled())
        window.coordinator.stop_demo()
        self.wait_for(lambda: not window.coordinator.busy)
        self.assertTrue(window.recording.start.isEnabled())
        self.assertTrue(window.delivery_on_ui_thread)
        self.assertTrue(window.result_received)

    def test_heartbeat_during_slow_discovery_and_shutdown(self):
        class SlowProvider(MockProvider):
            def devices(self):
                time.sleep(0.25)
                return super().devices()
        owner = self.coordinator(SlowProvider())
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        self.discover(owner)
        timer.stop()
        self.assertGreater(len(ticks), 3)

    def test_close_during_demo_waits_for_terminal_and_worker(self):
        window = self.window(MockProvider())
        self.discover(window.coordinator)
        window.coordinator.start_demo()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            window.close()
        self.assertTrue(window.isVisible())
        self.assertTrue(window.coordinator.closing)
        self.assertFalse(window.coordinator.start_demo())
        self.wait_for(lambda: window.coordinator.shutdown_complete and not window.isVisible())
        self.assertTrue(window.result_received)

    def test_close_can_be_declined(self):
        window = self.window(MockProvider())
        self.discover(window.coordinator)
        window.coordinator.start_demo()
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            window.close()
        self.assertFalse(window.coordinator.closing)
        self.assertTrue(window.isVisible())

    def test_idle_window_closes_on_first_request_without_recursive_close(self):
        window = self.window()
        window.close()
        self.wait_for(lambda: not window.isVisible())
        self.assertTrue(window.coordinator.shutdown_complete)

    def test_shutdown_ignores_late_discovery_reply(self):
        class SlowProvider(MockProvider):
            def devices(self):
                time.sleep(0.1)
                return super().devices()
        owner = self.coordinator(SlowProvider())
        owner.refresh_devices()
        owner.request_shutdown()
        self.wait_for(lambda: owner.shutdown_complete)
        self.assertEqual(owner.devices, [])

    def test_synthetic_operation_error_reaches_terminal(self):
        owner = self.coordinator(MockProvider())
        batches = []
        owner.events_ready.connect(batches.extend)
        self.discover(owner)
        owner.start_demo('error')
        self.wait_for(lambda: not owner.busy)
        self.assertEqual(owner.phase, 'FAILED')
        self.assertTrue(any(event['kind'] == 'critical' for event in batches))
        self.assertTrue(any(event['kind'] == 'result' for event in batches))

    def test_task_exception_is_structured_and_device_admission_recovers(self):
        class BrokenProvider(MockProvider):
            def devices(self):
                raise OSError('MOCK inventory failure')
        owner = self.coordinator(BrokenProvider())
        errors = []
        owner.error_raised.connect(errors.append)
        self.discover(owner)
        self.assertEqual(owner.phase, 'FAILED')
        self.assertIn('MOCK inventory failure', errors[0])
        self.assertTrue(owner.refresh_devices())

    def test_bounded_batches_slow_consumer_and_terminal_delivery(self):
        operation = DemoOperation(steps=200, interval=0.01).start()
        self.addCleanup(operation.stop)
        monitor = OperationMonitor(operation, 'demo', capacity=2)
        worker = MonitorWorker(monitor)
        self.workers.append(worker)
        batches = []
        worker.batch.connect(batches.append, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self.wait_for(lambda: len(batches) == 1)
        QTest.qWait(600)
        self.assertEqual(len(batches), 1)  # no acknowledge: no second queued batch
        self.assertLessEqual(monitor.queued_count, 4)
        operation.stop()
        self.wait_for(lambda: not operation.is_running())
        worker.acknowledge()
        self.wait_for(lambda: len(batches) > 1)
        self.assertTrue(any(e['kind'] == 'result' for e in batches[-1]))
        self.wait_for(worker.isFinished)

    def test_polling_range_and_inactive_settings(self):
        owner = self.coordinator(MockProvider())
        with self.assertRaises(ValueError):
            MonitorWorker(None, 10)
        window = self.window(MockProvider())
        window.settings.polling.setValue(500)
        self.assertEqual(window.coordinator.interval_ms, 500)
        self.discover(window.coordinator)
        window.coordinator.start_demo()
        self.assertFalse(window.settings.polling.isEnabled())

    def test_task_thread_start_failure_does_not_leave_busy_owner(self):
        owner = self.coordinator(MockProvider())
        with patch('gnss_logger.gui.coordinator.TaskWorker.start', side_effect=RuntimeError('thread start failed')):
            self.assertFalse(owner.refresh_devices())
        self.assertFalse(owner.busy)
        self.assertEqual(owner.phase, 'FAILED')

    def test_base_exception_in_task_is_not_lost(self):
        class ExitingProvider(MockProvider):
            def devices(self):
                raise SystemExit('MOCK task exit')
        owner = self.coordinator(ExitingProvider())
        errors = []
        owner.error_raised.connect(errors.append)
        self.discover(owner)
        self.assertIn('SystemExit', errors[0])
        self.assertEqual(owner.phase, 'FAILED')

    def test_stop_request_not_reverted_by_already_queued_progress(self):
        owner = self.coordinator(MockProvider())
        self.discover(owner)
        owner.start_demo()
        owner.stop_demo()
        owner._receive([dict(kind='progress', lifecycle='RUNNING')])
        self.assertEqual(owner.phase, 'STOPPING')

    def test_operation_start_failure_is_reported_and_released(self):
        class FailingOperation:
            def start(self):
                raise RuntimeError('MOCK start failure')
            def stop(self):
                pass
            def is_running(self):
                return False
        class FailingProvider(MockProvider):
            def operation(self, scenario):
                return FailingOperation()
        owner = self.coordinator(FailingProvider())
        self.discover(owner)
        owner.start_demo()
        self.wait_for(lambda: not owner.busy)
        self.assertEqual(owner.phase, 'FAILED')


if __name__ == '__main__':
    unittest.main()
