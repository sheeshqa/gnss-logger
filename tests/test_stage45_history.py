"""Offline Stage 4.5a history acceptance and bounded-worker checks."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from threading import Event, get_ident
import time
import unittest
from unittest.mock import patch

from gnss_logger.application.history import HistoryService
import test_application_history as history_fixtures

HAS_QT = importlib.util.find_spec('PySide6') is not None
if HAS_QT:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtWidgets import QApplication, QPushButton
    from gnss_logger.gui.history_coordinator import HistoryCoordinator
    from gnss_logger.gui.pages.history import HistoryPage


@unittest.skipUnless(HAS_QT, 'Optional GUI extra is not installed')
class HistoryGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.fixtures = history_fixtures.HistoryTests()
        self.fixtures.setUp()
        self.addCleanup(self.fixtures.doCleanups)
        self.controllers = []
        self.pages = []

    def wait_for(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail('Timed out waiting for history')

    def tearDown(self):
        for controller in self.controllers:
            controller.request_shutdown()
            self.wait_for(lambda: controller.shutdown_complete)
            controller.deleteLater()
        for page in self.pages:
            page.deleteLater()
        self.app.processEvents()

    def controller(self, factory=HistoryService):
        controller = HistoryCoordinator(service_factory=factory)
        self.controllers.append(controller)
        return controller

    def page(self):
        page = HistoryPage(operational=True)
        self.pages.append(page)
        return page

    def load(self, controller, timeout=30):
        f = self.fixtures
        controller.configure_roots([f.runs], [f.converted], [f.stage1])
        self.wait_for(lambda: not controller.busy, timeout=timeout)

    def test_real_api_preserves_keys_standalone_receipts_issues_and_archive_bytes(self):
        f = self.fixtures
        directory, manifest = f.old_session('duplicate')
        f.old_session('other')  # same fingerprint is deliberately ambiguous
        f.operation(manifest['artifacts'][0], 'ambiguous', 'OBS_ONLY_INCOMPLETE_NAVIGATION',
                    {'obs': str(f.root / 'absent.obs')})
        f.operation({'path': str(f.root / 'standalone.ubx')}, 'standalone', 'INTERRUPTED')
        receipt = dict(schema_id='gnss_logger.conversion.result', schema_version=1,
                       identity=dict(source_sha256='a' * 64, source_bytes=8),
                       source=manifest['artifacts'][0], status='ALREADY_CONVERTED', published_outputs={})
        f.write(f.converted / 'receipt.json', receipt)
        f.write(f.converted / 'broken.json', '{')
        f.old_session('incomplete', manifest=False)
        before = {str(p): p.read_bytes() for p in f.root.rglob('*') if p.is_file()}
        controller = self.controller()
        snapshots, details = [], []
        controller.snapshot_ready.connect(snapshots.append)
        controller.detail_ready.connect(details.append)
        self.load(controller)
        payload = snapshots[-1]
        self.assertEqual(len(payload['sessions']), 3)
        self.assertEqual(len(payload['conversions']), 3)
        self.assertTrue(payload['diagnostics'])
        ambiguous = next(c for c in payload['conversions'] if c['operation_id'] == 'ambiguous')
        self.assertEqual(ambiguous['segments'][0]['match'], 'AMBIGUOUS')
        self.assertFalse(ambiguous['segments'][0]['published_outputs']['obs']['present'])
        self.assertEqual({c['kind'] for c in payload['conversions']}, {'operation', 'receipt'})
        controller.select(str(directory))
        self.wait_for(lambda: not controller.busy)
        self.assertEqual(details[-1]['session_key'], str(directory))
        self.assertFalse(details[-1]['independently_verified'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in f.root.rglob('*') if p.is_file()})

    def test_single_worker_latest_pending_and_shutdown_discard_stale_delivery(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        calls, threads = [], []
        ui_thread = get_ident()

        class Service:
            diagnostics = []
            def __init__(self, *roots):
                self.roots = roots
            def list_sessions(self):
                calls.append(self.roots)
                threads.append(get_ident())
                if len(calls) == 1:
                    entered.set()
                    release.wait(5)
                return [{'session_key': self.roots[0][0]}]
            def list_conversions(self):
                return []
            def get_session(self, key):
                return {'session_key': key}

        controller = self.controller(Service)
        snapshots, details, closed = [], [], []
        controller.snapshot_ready.connect(snapshots.append)
        controller.detail_ready.connect(details.append)
        controller.ready_to_close.connect(lambda: closed.append(True))
        controller.configure_roots(['old'], [], [])
        self.wait_for(entered.is_set)
        worker = controller._worker
        for index in range(100):
            controller.configure_roots([str(index)], [], [])
        self.assertIs(controller._worker, worker)
        self.assertEqual(len(calls), 1)
        release.set()
        self.wait_for(lambda: not controller.busy)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]['sessions'][0]['session_key'], '99')
        self.assertTrue(all(thread != ui_thread for thread in threads))
        controller.select('first')
        controller.select('latest')
        self.wait_for(lambda: not controller.busy)
        self.assertEqual([d for d in details if d], [{'session_key': 'latest'}])
        controller.refresh()
        controller.request_shutdown()
        self.wait_for(lambda: controller.shutdown_complete)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(closed, [True])
        self.assertFalse(controller.refresh())

    def test_page_filters_sorting_exact_selection_missing_metrics_and_conversion_handoff(self):
        f = self.fixtures
        f.old_session('one')
        f.old_session('two')
        sessions = f.history.list_sessions()
        sessions[0]['model'] = 'ZED-X20P'
        sessions[0]['manifest']['parameters'] = {'station': 'ABCD'}
        sessions[1]['model'] = 'ZED-F9P'
        sessions[1]['started_utc'] = '2025-01-01T00:00:00Z'
        page = self.page()
        page.render_snapshot(dict(sessions=sessions, conversions=[], diagnostics=[]))
        self.assertEqual(page.table.rowCount(), 2)
        self.assertTrue(page.table.isSortingEnabled())
        page.search.setText('abcd')
        self.assertEqual(page.table.rowCount(), 1)
        selected, converted = [], []
        page.selection_requested.connect(selected.append)
        page.conversion_requested.connect(converted.append)
        page.table.selectRow(0)
        self.assertEqual(selected[-1], sessions[0]['session_key'])
        page.render_detail(sessions[0])
        self.assertIn('RAWX: Нет данных', page.detail_text.toPlainText())
        page.convert_button.click()
        self.assertEqual(converted[-1]['session_key'], sessions[0]['session_key'])
        converted[-1]['terminal'] = False
        self.assertTrue(page._detail['terminal'])
        page.search.clear()
        page.date_from.setText('2026-01-01')
        self.assertEqual(page.table.rowCount(), 1)
        page.date_from.clear()
        page.model_filter.setCurrentIndex(page.model_filter.findData('ZED-F9P'))
        self.assertEqual(page.table.rowCount(), 1)
        page.render_detail(dict(sessions[1], terminal=False))
        self.assertFalse(page.convert_button.isEnabled())
        page.apply_state(dict(closing=True))
        self.assertFalse(page.refresh_button.isEnabled())

    def test_detail_metrics_resolve_actual_controller_diagnostics_and_legacy_reports(self):
        f = self.fixtures
        controller, report, _, _, _ = f.run_record(root=f.root)
        diagnostics = report['extensions']['gnss_logger.diagnostics']
        self.assertEqual(diagnostics['RAWX']['epochs'], 4)
        self.assertGreater(diagnostics['SFRBX']['messages'], 0)
        session = f.history.get_session(str(controller.directory))
        self.assertEqual(session['report'], report)
        page = self.page()
        metric_names = ('RAWX', 'SFRBX', 'checksum_errors', 'decode_errors', 'phase_quality')

        def check_metrics(payload, expected):
            page.render_detail(payload)
            # Check the dedicated fields, not the raw-report JSON further down.
            sections = page.detail_text.toPlainText().split('\n\n')
            for name in metric_names:
                value = expected[name]
                rendered = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else str(value)
                self.assertIn(f'{name}: {rendered}', sections)

        with self.subTest(layout='actual controller extension'):
            check_metrics(session, diagnostics)
        for layout in ('direct Stage 1', 'nested diagnostics'):
            with self.subTest(layout=layout):
                payload = deepcopy(session)
                if layout == 'direct Stage 1':
                    payload['report'] = deepcopy(diagnostics)
                else:
                    payload['report'] = {'diagnostics': {
                        name.lower(): diagnostics[name] for name in metric_names}}
                check_metrics(payload, diagnostics)
        with self.subTest(layout='metrics absent'):
            payload = deepcopy(session)
            payload['report'] = {}
            page.render_detail(payload)
            sections = page.detail_text.toPlainText().split('\n\n')
            for name in metric_names:
                self.assertIn(f'{name}: Нет данных', sections)

    def test_paths_use_desktop_services_check_existence_and_copy_literal_text(self):
        f = self.fixtures
        directory, _ = f.old_session()
        page = self.page()
        page.render_detail(f.history.get_session(str(directory)))
        page.paths.setCurrentIndex(page.paths.findData(str(directory)))
        with patch('gnss_logger.gui.pages.history.QDesktopServices.openUrl', return_value=True) as opened:
            self.assertTrue(page.open_selected_path())
            self.assertEqual(opened.call_args.args[0].toLocalFile(), str(directory).replace('\\', '/'))
            page.paths.addItem('missing', str(f.root / 'absent'))
            page.paths.setCurrentIndex(page.paths.count() - 1)
            self.assertFalse(page.open_selected_path())
            self.assertEqual(opened.call_count, 1)
        page.copy_selected_path()
        self.assertEqual(self.app.clipboard().text(), str(f.root / 'absent'))

    def test_legacy_default_and_standalone_conversion_detail(self):
        legacy = HistoryPage()
        self.pages.append(legacy)
        self.assertFalse(legacy.operational)
        self.assertFalse(legacy.findChild(QPushButton).isEnabled())
        f = self.fixtures
        f.operation({'path': str(f.root / 'standalone.ubx')}, 'standalone',
                    'OBS_ONLY_INCOMPLETE_NAVIGATION', {'obs': str(f.root / 'absent.obs')})
        page = self.page()
        page.render_snapshot(dict(sessions=[], conversions=f.history.list_conversions(), diagnostics=[]))
        page.conversion_table.selectRow(0)
        self.assertIn('UNLINKED', page.detail_text.toPlainText())
        self.assertIn('OBS_ONLY_INCOMPLETE_NAVIGATION', page.detail_text.toPlainText())
        self.assertGreaterEqual(page.paths.count(), 3)
        self.assertFalse(page.convert_button.isEnabled())

    def test_errors_are_delivered_and_shutdown_remains_possible(self):
        class BrokenService:
            def __init__(self, *roots):
                pass
            def list_sessions(self):
                raise OSError('unreadable archive')
        controller = self.controller(BrokenService)
        errors = []
        controller.error_raised.connect(errors.append)
        controller.refresh()
        self.wait_for(lambda: not controller.busy)
        self.assertEqual(errors, ['OSError: unreadable archive'])
        controller.request_shutdown()
        self.assertTrue(controller.shutdown_complete)

    def test_api_1000_sessions_benchmark_with_gui_heartbeat(self):
        f = self.fixtures
        for index in range(1000):
            f.old_session(f'synthetic-{index:04}')
        controller = self.controller()
        page = self.page()
        payloads = []
        controller.snapshot_ready.connect(page.render_snapshot)
        controller.snapshot_ready.connect(payloads.append)
        beats = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: beats.append(time.perf_counter()))
        timer.start()
        started = time.perf_counter()
        self.load(controller, timeout=180)
        elapsed = time.perf_counter() - started
        timer.stop()
        self.assertEqual(len(payloads[-1]['sessions']), 1000)
        self.assertEqual(page.table.rowCount(), 1000)
        self.assertGreater(len(beats), 1)
        gaps = [b - a for a, b in zip([started] + beats, beats + [time.perf_counter()])]
        print(f'\nHISTORY_BENCHMARK sessions=1000 elapsed_s={elapsed:.3f} '
              f'heartbeat_count={len(beats)} max_heartbeat_gap_s={max(gaps):.3f}', flush=True)


if __name__ == '__main__':
    unittest.main()
