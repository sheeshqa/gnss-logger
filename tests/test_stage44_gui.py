"""Operational conversion form and window acceptance, entirely offline."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gnss_logger.gui.conversions import MockConversionBackend
from gnss_logger.gui.receivers import MockReceiverBackend

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication, QMessageBox
    from gnss_logger.gui.pages.conversion import ConversionPage
    from gnss_logger.gui.main_window import MainWindow


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class ConversionGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='stage44 GUI ')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.backend = MockConversionBackend(self.root)
        self.windows = []
        self.pages = []

    def wait_for(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for conversion GUI')

    def tearDown(self):
        for window in self.windows:
            window.conversion_coordinator.request_shutdown(cancel=True)
            window.coordinator.request_shutdown()
            self.wait_for(lambda: window.conversion_coordinator.shutdown_complete
                          and window.coordinator.shutdown_complete)
            window.close()
            window.deleteLater()
        for page in self.pages:
            page.deleteLater()
        self.app.processEvents()

    def page(self):
        page = ConversionPage(operational=True, mock=True, mock_root=self.root)
        self.pages.append(page)
        page.set_settings(self.backend.load_settings())
        page.source.setText(str(self.backend.make_fixture()))
        return page

    def window(self):
        provider = MockReceiverBackend(interval=.01)
        window = MainWindow(provider, operational=True, conversion_backend=self.backend)
        self.windows.append(window)
        self.wait_for(lambda: not window.conversion_coordinator.busy)
        window.show()
        return window

    def test_form_roundtrip_returns_immutable_settings_and_explicit_source_modes(self):
        page = self.page()
        settings = page.conversion_settings()
        self.assertEqual(settings, self.backend.load_settings())
        for mode in ('file', 'directory', 'session'):
            with self.subTest(mode=mode):
                page.source_kind.setCurrentIndex(page.source_kind.findData(mode))
                page.source.setText(str(self.root if mode == 'directory' else self.root / 'archive.ubx'))
                page.session_id.setText('a' * 32)
                request = page.conversion_request()
                self.assertEqual(request.run_id, 'a' * 32 if mode == 'session' else None)
                self.assertEqual(request.input, None if mode == 'session' else Path(page.source.text()))
                self.assertEqual(request.output, settings.output_root)
        page.timeout_s.setText('25')
        self.assertEqual(settings.timeout_s, 10)
        self.assertEqual(page.conversion_settings().timeout_s, 25)

    def test_form_invalid_settings_disable_run_and_raise_contract_error(self):
        page = self.page()
        for field, text in (('timeout_s', 'nan'), ('interval_s', '0'),
                            ('output_root', 'relative'), ('executable', '')):
            with self.subTest(field=field):
                page.set_settings(self.backend.load_settings())
                getattr(page, field).setText(text)
                with self.assertRaises(ValueError):
                    page.conversion_settings()
                self.assertFalse(page.run.isEnabled())
                self.assertTrue(page.validation.text())
        page.set_settings(self.backend.load_settings())
        page.obs.setChecked(False)
        page.nav.setChecked(False)
        with self.assertRaises(ValueError):
            page.conversion_settings()

    def test_session_requires_explicit_id_and_shutdown_blocks_new_work(self):
        page = self.page()
        page.source_kind.setCurrentIndex(page.source_kind.findData('session'))
        page.session_id.clear()
        with self.assertRaises(ValueError):
            page.conversion_request()
        page.session_id.setText('b' * 32)
        self.assertTrue(page.run.isEnabled())
        page.apply_state(dict(phase='RUNNING', busy=True, job_active=True, closing=False))
        self.assertFalse(page.run.isEnabled())
        self.assertTrue(page.cancel.isEnabled())
        page.apply_state(dict(phase='RUNNING', busy=True, job_active=True, closing=True))
        self.assertFalse(page.run.isEnabled())
        self.assertFalse(page.source_kind.isEnabled())
        self.assertTrue(page.cancel.isEnabled())
        page.apply_state(dict(phase='RUNNING', busy=True, job_active=True, closing=True,
                              cancel_requested=True))
        self.assertFalse(page.cancel.isEnabled())

    def test_render_twelve_of_twenty_preserves_status_files_warnings_errors(self):
        page = self.page()
        statuses = ['SUCCESS_WITH_WARNINGS'] * 9 + [
            'OBS_ONLY_INCOMPLETE_NAVIGATION', 'SKIPPED', 'FAILED'] + ['NOT_RUN'] * 8
        report = dict(status='RUNNING', exit_code=3, errors=[], warnings=['batch warning'],
                      segments=[dict(source={'path': str(self.root / f'{i}.ubx')},
                                     status=status, published_outputs={'obs': f'{i}.obs'}
                                     if status == 'OBS_ONLY_INCOMPLETE_NAVIGATION' else {},
                                     warnings=['segment warning'], errors=['converter error']
                                     if status == 'FAILED' else [], skip_reason='control_only')
                                for i, status in enumerate(statuses)])
        original = deepcopy(report)
        page.render_view(report)
        self.assertIn('12 / 20', page.progress.text())
        self.assertEqual(page.table.rowCount(), 20)
        for row, status in enumerate(statuses):
            self.assertEqual(page.table.item(row, 1).text(), status)
        self.assertEqual(page.table.item(9, 2).text(), '9.obs')
        self.assertIn('control_only', page.table.item(10, 3).text())
        self.assertIn('converter error', page.table.item(11, 4).text())
        self.assertIn('batch warning', page.details.toPlainText())
        self.assertEqual(report, original)

    def test_window_loads_toml_into_form_and_reports_invalid_toml(self):
        window = self.window()
        config = self.root / 'profile.toml'
        config.write_text('[engine]\nexecutable="' +
                          self.backend.load_settings().executable.as_posix() +
                          '"\ntimeout_s=27\n[input]\nruns_root="runs"\ndaily_root="data"\n'
                          '[output]\nroot="rinex"\n', encoding='utf-8')
        page = window.conversion
        page.toml_path.setText(str(config))
        page.load_button.click()
        self.wait_for(lambda: not window.conversion_coordinator.busy)
        self.assertEqual(page.conversion_settings().timeout_s, 27)
        self.assertEqual(page.conversion_settings().output_root, self.root / 'rinex')
        previous = window.log.toPlainText()
        config.write_text('invalid = [', encoding='utf-8')
        page.load_button.click()
        self.wait_for(lambda: not window.conversion_coordinator.busy)
        self.assertNotEqual(window.log.toPlainText(), previous)
        self.assertEqual(page.conversion_settings().timeout_s, 27)

    def test_window_manual_conversion_uses_injected_backend_and_ui_delivery(self):
        window = self.window()
        self.assertEqual(window.coordinator.provider.conversion_worker_factory,
                         self.backend.worker_factory)
        self.assertIsNot(window.coordinator, window.conversion_coordinator)
        window._mock_fixture('success')
        self.assertTrue(window.conversion.run.isEnabled())
        window.conversion.run.click()
        self.assertTrue(window.conversion_coordinator.busy)
        self.assertFalse(window.conversion.run.isEnabled())
        self.wait_for(lambda: not window.conversion_coordinator.busy)
        self.assertTrue(window.delivery_on_ui_thread)
        self.assertEqual(window.conversion.table.rowCount(), 1)
        self.assertIn(window.conversion.table.item(0, 1).text(),
                      ('SUCCESS', 'SUCCESS_WITH_WARNINGS'))
        self.assertTrue(window.conversion.open_report.isEnabled())
        self.assertTrue(window.conversion.open_directory.isEnabled())

    def test_window_close_cancel_stays_open_then_yes_waits_for_manual_result(self):
        window = self.window()
        window._mock_fixture('slow')
        window.conversion.scenario.setCurrentIndex(window.conversion.scenario.findData('slow'))
        window.conversion.run.click()
        self.assertTrue(window.conversion_coordinator.busy)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            window.close()
        self.assertTrue(window.isVisible())
        self.assertFalse(window.conversion_coordinator.closing)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            window.close()
        self.wait_for(lambda: window.conversion_coordinator.shutdown_complete
                      and window.coordinator.shutdown_complete)
        self.wait_for(lambda: not window.isVisible())
        self.assertEqual(window.conversion_coordinator.result['status'], 'INTERRUPTED')
