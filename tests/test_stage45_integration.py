"""Integration of the operational shell, isolated storage and background shutdown."""
import os
import importlib.util
import json
from threading import Event
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from gnss_logger.gui.conversions import MockConversionBackend
    from gnss_logger.gui.main_window import MainWindow
    from gnss_logger.gui.receivers import MockReceiverBackend


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class Stage45IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.window = MainWindow(MockReceiverBackend(), operational=True,
                                 conversion_backend=MockConversionBackend(self.root))
        self.wait(lambda: not self.window.settings_bridge.busy and
                  not self.window.conversion_coordinator.busy and not self.window.history_coordinator.busy)

    def wait(self, predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail('Timed out waiting for GUI workers')

    def tearDown(self):
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            self.window.close()
            self.wait(lambda: self.window.settings_bridge.shutdown_complete and
                      self.window.history_coordinator.shutdown_complete and
                      self.window.coordinator.shutdown_complete and
                      self.window.conversion_coordinator.shutdown_complete)
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_mock_root_and_operational_pages(self):
        self.assertEqual(self.window.mock_root, self.root.resolve())
        self.assertTrue(Path(self.window.recording.output.text()).is_relative_to(self.root))
        self.assertTrue(self.window.history.operational)
        self.assertTrue(self.window.settings.operational)
        self.assertTrue(self.window.settings_bridge.path.is_relative_to(self.root))

    def test_history_selection_uses_core_request(self):
        source = self.window.conversion_backend.make_fixture()
        self.window._history_conversion(dict(terminal=True, ubx_files=[dict(path=str(source))]))
        request = self.window.conversion.conversion_request()
        self.assertEqual(request.input, source.resolve())
        self.assertEqual(self.window.sidebar.currentRow(), 1)

    def test_geometry_restore_handles_large_integers(self):
        values = self.window.settings_bridge.values.copy()
        values['interface'] = dict(values['interface'], geometry=dict(x=10**100, y=0,
            width=10**100, height=700, maximized=False))
        self.window.settings_bridge.apply_values(values, restore_window=True)

    def test_close_cancel_preserves_dirty_profile(self):
        self.window.conversion.receiver_model.setText('changed')
        self.assertTrue(self.window.settings_bridge.dirty_profile())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            self.window.close()
        self.assertFalse(self.window._close_requested)
        self.assertTrue(self.window.settings_bridge.dirty_profile())

    def test_save_and_reload_preferences(self):
        self.window.settings.station.setText('TEST')
        self.window.settings.baud.setValue(9600)
        self.assertTrue(self.window.settings_bridge.save())
        self.wait(lambda: not self.window.settings_bridge.busy)
        self.window.settings.station.setText('OTHER')
        self.assertTrue(self.window.settings_bridge.load())
        self.wait(lambda: not self.window.settings_bridge.busy)
        self.assertEqual(self.window.settings.station.text(), 'TEST')
        self.assertEqual(self.window.recording.station.text(), 'TEST')
        self.assertEqual(self.window.recording.baud.currentText(), '9600')
        self.assertEqual(self.window.coordinator.view()['baud'], 9600)

    def test_unrepresentable_baud_is_reported_without_widget_overflow(self):
        path = self.window.settings_bridge.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'recording': {'baud': 10**100}}), encoding='utf-8')
        self.assertTrue(self.window.settings_bridge.load())
        self.wait(lambda: not self.window.settings_bridge.busy)
        self.assertTrue(self.window.settings_bridge._bad_preferences)
        self.assertEqual(path.read_text(encoding='utf-8'), json.dumps({'schema_version': 1, 'recording': {'baud': 10**100}}))

    def test_saved_seconds_are_not_rounded_through_minutes(self):
        for seconds in (95, 95.123, 0.001, 60000000):
            with self.subTest(seconds=seconds):
                self.window.settings.seconds.setValue(seconds)
                self.assertTrue(self.window.settings_bridge.save())
                self.wait(lambda: not self.window.settings_bridge.busy)
                self.assertEqual(self.window.recording.record_parameters()['seconds'], seconds)

    def test_unrepresentable_precision_is_rejected_before_form_population(self):
        for field, value in (('expected_hz', 1.0001), ('seconds', 95.1234)):
            values = self.window.settings_bridge.store.defaults.copy()
            values['recording'] = dict(values['recording'], **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.window.settings_bridge._confine(values)

    def test_close_waits_for_settings_worker(self):
        entered, release = Event(), Event()
        def task():
            entered.set()
            release.wait(10)
        self.assertTrue(self.window.settings_bridge._run('test', task))
        self.wait(entered.is_set)
        self.window.close()
        self.assertFalse(self.window.settings_bridge.shutdown_complete)
        release.set()
        self.wait(lambda: self.window.settings_bridge.shutdown_complete)

    def test_corrupt_preferences_are_preserved(self):
        path = self.window.settings_bridge.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken', encoding='utf-8')
        self.assertTrue(self.window.settings_bridge.load())
        self.wait(lambda: not self.window.settings_bridge.busy)
        self.assertTrue(self.window.settings_bridge._bad_preferences)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            self.assertFalse(self.window.settings_bridge.save())
        self.assertEqual(path.read_text(encoding='utf-8'), '{broken')


if __name__ == '__main__':
    unittest.main()
