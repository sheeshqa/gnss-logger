"""Offline checks for shared presentation and persistent operation facts."""
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtGui import QAction
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from gnss_logger import __version__
    from gnss_logger.gui.main_window import MainWindow
    from gnss_logger.gui.receivers import MockReceiverBackend
    from gnss_logger.gui.presentation import status_text


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail('GUI shutdown/load timeout')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(MockReceiverBackend(), operational=True,
                                 mock_root=Path(self.temp.name))
        self.wait(lambda: not self.window.settings_bridge.busy and
                  not self.window.conversion_coordinator.busy)

    def tearDown(self):
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            self.window.close()
        self.wait(lambda: all(c.shutdown_complete for c in (
            self.window.coordinator, self.window.conversion_coordinator,
            self.window.history_coordinator, self.window.settings_bridge)))
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_four_pages_keyboard_actions_and_persistent_status(self):
        window = self.window
        self.assertEqual(window.stack.count(), 4)
        actions = {a.shortcut().toString(): a for a in window.findChildren(QAction)}
        for index in range(4):
            actions['Ctrl+' + str(index + 1)].trigger()
            self.assertEqual(window.sidebar.currentRow(), index)
            self.assertEqual(window.stack.currentIndex(), index)
            self.assertFalse(window.persistent_status.isHidden())
        self.assertIn('F1', actions)
        self.assertEqual(window.version_status.text(), __version__)
        self.assertFalse(window.windowIcon().pixmap(64, 64).isNull())

    def test_status_uses_coordinator_facts_on_every_page(self):
        state = self.window.coordinator.view()
        state.update(selected_device='MOCK', binding={'verified': True},
                     phase='RECORDING', auto_enabled=False,
                     auto_conversion={'queued': 3, 'active': True})
        text = status_text(state, {'phase': 'RUNNING'})
        for value in ('подтверждён', 'RECORDING', 'RUNNING', 'Auto: Off', 'Очередь: 3'):
            self.assertIn(value, text)
        self.window._state(state)
        self.assertIn('Очередь: 3', self.window.persistent_status.text())
        self.assertIn('Очередь: —', status_text({}))

    def test_long_paths_preserved_and_small_window_scrolls(self):
        path = 'C:/' + '/'.join(['длинный каталог'] * 40) + '/данные.ubx'
        self.window.conversion.source.setText(path)
        self.window.resize(760, 540)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.conversion.source.text(), path)
        self.assertEqual(self.window.conversion.source.toolTip(), path)
        self.window.conversion.source.setText(path + '.updated')
        self.assertEqual(self.window.conversion.source.toolTip(), path + '.updated')
        self.assertEqual(self.window.minimumWidth(), 760)
        for index in range(4):
            self.assertTrue(self.window.stack.widget(index).widgetResizable())

    def test_about_contains_runtime_version(self):
        with patch.object(QMessageBox, 'about') as about:
            self.window.about_action.trigger()
        self.assertIn(__version__, about.call_args.args[2])

    def test_latest_auto_skip_preserves_cumulative_failure_summary(self):
        state = self.window.coordinator.view()
        state.update(busy=True, recording_active=True, auto_capable=True,
                     auto_enabled=False, auto_conversion={
                         'statuses': {'FAILED': 1, 'SKIPPED': 1}, 'queued': 0})
        self.window._state(state)
        page = self.window.recording
        page.render_auto_result({'conversion': {'status': 'FAILED'}, 'exit_code': 1})
        self.assertIn('FAILED', page.auto_details.text())
        page.render_auto_result({'conversion': {'status': 'SKIPPED'}, 'exit_code': 0})
        self.assertIn('SKIPPED', page.auto_details.text())
        self.assertNotIn('FAILED', page.auto_details.text())
        self.assertIn('"FAILED": 1', page.auto_status.text())
        self.assertIn('Очередь: 0', self.window.persistent_status.text())

    def test_dynamic_table_items_keep_full_tooltips(self):
        path = 'C:/' + '/'.join(['длинный каталог'] * 40) + '/данные.ubx'
        for suffix in ('', '.updated'):
            value = path + suffix
            self.window.history.render_snapshot(dict(sessions=[dict(
                session_key=value, session_id=value, warnings=[value])],
                conversions=[dict(path=value, status='FINISHED')]))
            self.window.conversion.render_view(dict(operation_type='conversion',
                conversion={'status': 'FINISHED'}, segments=[dict(
                    source=value, status='FINISHED', published_outputs=[value],
                    warnings=[value], errors=[value])]))
            for table in (self.window.history.table, self.window.history.conversion_table,
                          self.window.conversion.table):
                self.assertEqual(table.rowCount(), 1)
                for column in range(table.columnCount()):
                    item = table.item(0, column)
                    self.assertEqual(item.toolTip(), item.text())
            self.assertEqual(self.window.conversion.table.item(0, 0).toolTip(), value)
            self.assertEqual(self.window.history.conversion_table.item(0, 1).toolTip(), value)


if __name__ == '__main__':
    unittest.main()
