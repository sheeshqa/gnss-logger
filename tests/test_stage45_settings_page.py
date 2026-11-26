"""Settings form contract, without persistence, hardware or conversion workers."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QFileDialog, QPlainTextEdit
    from gnss_logger.gui.pages.settings import SettingsPage


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def page(self, operational=True):
        page = SettingsPage(operational=operational)
        self.addCleanup(page.deleteLater)
        return page

    def values(self):
        root = Path('C:/архив GNSS')
        return {
            'schema_version': 1,
            'recording': {'output': str(root / 'runs'), 'daily_root': None,
                          'station': 'GNSS', 'baud': 115200, 'expected_hz': 1.0,
                          'seconds': 60.0, 'retention_policy': 'keep_all',
                          'auto_configured': False, 'auto_enabled': False},
            'history': {'recording_roots': [str(root / 'runs')],
                        'conversion_roots': [str(root / 'rinex')], 'stage1_roots': []},
            'interface': {'polling_ms': 500, 'page': 2,
                          'geometry': {'x': 10, 'y': 20, 'width': 900, 'height': 700,
                                       'maximized': False}},
            'conversion': {'config_path': None},
        }

    def test_default_constructor_preserves_legacy_polling_contract(self):
        page = SettingsPage()
        self.addCleanup(page.deleteLater)
        self.assertEqual((page.polling.minimum(), page.polling.maximum()), (250, 2000))
        self.assertEqual(page.polling.value(), 250)
        self.assertEqual(page.polling.singleStep(), 250)
        self.assertFalse(hasattr(page, 'output'))

    def test_roundtrip_preserves_unedited_fields_and_detaches_all_copies(self):
        page = self.page()
        values = self.values()
        original = deepcopy(values)
        page.set_preferences(values)
        self.assertEqual(page.preferences(), values)
        values['interface']['geometry']['x'] = 999
        values['history']['recording_roots'].append('external change')
        self.assertEqual(page.preferences(), original)
        result = page.preferences()
        result['interface']['geometry']['width'] = 1
        result['history']['stage1_roots'].append('another change')
        self.assertEqual(page.preferences(), original)
        self.assertTrue(page.retention_policy.isReadOnly())
        self.assertEqual(page.retention_policy.text(), 'keep_all')

    def test_edited_values_overlay_explicit_base_without_mutation(self):
        page = self.page()
        page.set_preferences(self.values())
        base = self.values()
        base['interface']['page'] = 3
        snapshot = deepcopy(base)
        page.output.setText('C:/Новые записи')
        page.daily_root.setText('C:/Суточные данные')
        page.station.setText('TEST')
        page.baud.setValue(230400)
        page.expected_hz.setValue(2.5)
        page.seconds.setValue(12.5)
        page.auto_configured.setChecked(True)
        page.auto_enabled.setChecked(True)
        page.polling.setValue(750)
        page.config_path.setText('C:/Профили GNSS/local.toml')
        result = page.preferences(base)
        self.assertEqual(base, snapshot)
        self.assertEqual(result['interface']['page'], 3)
        self.assertEqual(result['interface']['polling_ms'], 750)
        self.assertEqual(result['recording'], dict(
            output='C:/Новые записи', daily_root='C:/Суточные данные', station='TEST',
            baud=230400, expected_hz=2.5, seconds=12.5, retention_policy='keep_all',
            auto_configured=True, auto_enabled=True))
        self.assertEqual(result['conversion']['config_path'], 'C:/Профили GNSS/local.toml')

    def test_until_stop_and_optional_paths(self):
        page = self.page()
        values = self.values()
        values['recording']['seconds'] = None
        page.set_preferences(values)
        self.assertTrue(page.until_stop.isChecked())
        self.assertFalse(page.seconds.isEnabled())
        self.assertIsNone(page.preferences()['recording']['seconds'])
        page.until_stop.setChecked(False)
        self.assertTrue(page.seconds.isEnabled())
        page.seconds.setValue(25)
        self.assertEqual(page.preferences()['recording']['seconds'], 25)
        self.assertIsNone(page.preferences()['recording']['daily_root'])
        self.assertIsNone(page.preferences()['conversion']['config_path'])

    def test_history_multiline_paths_preserve_unavailable_archives(self):
        page = self.page()
        page.set_preferences(self.values())
        self.assertEqual(len(page.findChildren(QPlainTextEdit)), 3)
        page.recording_roots.setPlainText(' C:/Архив 1\n\nZ:/offline archive\n')
        page.stage1_roots.setPlainText('C:/старый архив')
        self.assertEqual(page.preferences()['history']['recording_roots'],
                         ['C:/Архив 1', 'Z:/offline archive'])
        self.assertEqual(page.preferences()['history']['stage1_roots'], ['C:/старый архив'])

    def test_buttons_emit_only_requested_no_argument_signal(self):
        page = self.page()
        page.set_preferences(self.values())
        observed = []
        before = page.preferences()
        for name in ('apply', 'save', 'load', 'restore'):
            getattr(page, name + '_requested').connect(lambda n=name: observed.append(n))
        for name in ('apply', 'save', 'load', 'restore'):
            getattr(page, name).click()
        self.assertEqual(observed, ['apply', 'save', 'load', 'restore'])
        self.assertEqual(page.preferences(), before)
        self.assertTrue(page.output.isEnabled())
        page.set_status('<error> Недоступный каталог')
        page.set_preferences_path('C:/Настройки/preferences.json')
        self.assertEqual(page.status.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn('<error>', page.status.text())
        self.assertIn('C:/Настройки/preferences.json', page.preferences_path.text())

    def test_native_browsers_only_set_paths_and_cancellation_keeps_values(self):
        page = self.page()
        page.set_preferences(self.values())
        with patch.object(QFileDialog, 'getOpenFileName', return_value=('C:/Профили/a.toml', '')):
            page.profile_browse.click()
        self.assertEqual(page.config_path.text(), 'C:/Профили/a.toml')
        with patch.object(QFileDialog, 'getOpenFileName', return_value=('', '')):
            page.profile_browse.click()
        self.assertEqual(page.config_path.text(), 'C:/Профили/a.toml')
        with patch.object(QFileDialog, 'getExistingDirectory', return_value='Z:/Архив с пробелами'):
            page._browse_directory(page.output)
            page._browse_directory(page.stage1_roots)
        self.assertEqual(page.output.text(), 'Z:/Архив с пробелами')
        self.assertEqual(page.stage1_roots.toPlainText(), 'Z:/Архив с пробелами')
        with patch.object(QFileDialog, 'getExistingDirectory', return_value=''):
            page._browse_directory(page.output)
        self.assertEqual(page.output.text(), 'Z:/Архив с пробелами')

    def test_capture_current_requests_main_bridge_without_changing_defaults(self):
        page = self.page()
        page.set_preferences(self.values())
        before = page.preferences()
        requests = []
        page.capture_current_requested.connect(lambda: requests.append(True))
        page.capture_current.click()
        self.assertEqual(requests, [True])
        self.assertEqual(page.preferences(), before)


if __name__ == '__main__':
    unittest.main()
