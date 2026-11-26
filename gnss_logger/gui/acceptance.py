"""Opt-in native acceptance driver shared by source and frozen GUI.

No test-package imports, receiver hardware, or external converter processes.
The launcher supplies a persistent isolated mock root. Two independent launches
use GNSS_ACCEPTANCE_PHASE=write/read to verify persistence across processes.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QEvent, QEventLoop, QTimer, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .toml_writer import TomlStore


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _fixtures(root):
    """Archive metadata is synthetic; conversion input uses the mock backend."""
    archive = root / 'archive with spaces — синтетические данные'
    archive.mkdir(parents=True, exist_ok=False)
    first = None
    for index in range(12):
        directory = archive / f'synthetic-{index:04d}'
        directory.mkdir(exist_ok=False)
        capture = directory / 'synthetic.ubx'
        data = b'SYNTHETIC archive metadata; not measurements\n' + str(index).encode('ascii')
        capture.write_bytes(data)
        artifact = dict(path=str(capture), bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(), complete=True,
                        status='closed_complete')
        if first is None:
            first = artifact
        manifest = dict(schema_version=1, session_id=directory.name,
                        started_utc='2026-11-25T00:00:00Z', ended_utc='2026-11-25T00:01:00Z',
                        exit_code=0, registration='STOPPED_COMPLETE',
                        parameters=dict(station='SMOKE', baud=115200), artifacts=[artifact])
        (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    converted = root / 'archived conversions'
    converted.mkdir(exist_ok=False)
    obs = converted / 'synthetic.obs'
    obs.write_text('SYNTHETIC metadata fixture only; not observations\n', encoding='ascii')
    report = dict(schema_id='gnss_logger.conversion', schema_version=2,
                  operation_id='synthetic-obs-only', status='OBS_ONLY_INCOMPLETE_NAVIGATION',
                  segments=[dict(source=first, status='OBS_ONLY_INCOMPLETE_NAVIGATION',
                                 published_outputs=dict(obs=str(obs)), validation={}, errors=[],
                                 warnings=['Synthetic archive: NAV intentionally absent'])])
    (converted / 'conversion-synthetic.json').write_text(json.dumps(report), encoding='utf-8')
    return archive


class _Acceptance:
    def __init__(self, app, window, report, images):
        self.app, self.window = app, window
        self.report, self.images = Path(report), Path(images)
        self.root = window.mock_root
        self.phase = os.environ.get('GNSS_ACCEPTANCE_PHASE', 'write')
        self.started = time.monotonic()
        self.beats = []
        self.result = dict(schema_version=1, passed=False, phase=self.phase,
                           frozen=bool(getattr(sys, 'frozen', False)), pid=os.getpid(),
                           platform=app.platformName(), root=str(self.root),
                           scale=os.environ.get('GNSS_ACCEPTANCE_DPR', os.environ.get('QT_SCALE_FACTOR', '1')),
                           qt_scale_factor=os.environ.get('QT_SCALE_FACTOR', '1'), screenshots=[], checks=[])
        self.timer = QTimer(window)
        self.timer.setInterval(20)
        self.timer.timeout.connect(lambda: self.beats.append(time.monotonic()))

    def check(self, condition, message):
        _require(condition, message)
        self.result['checks'].append(message)

    def pause(self, milliseconds=100):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def wait(self, predicate, description, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            self.pause(20)
        raise TimeoutError(description)

    def coordinators(self):
        w = self.window
        return w.coordinator, w.conversion_coordinator, w.history_coordinator, w.settings_bridge

    def idle(self):
        self.wait(lambda: all(not c.busy for c in self.coordinators()), 'all workers idle')

    def key(self, widget, key):
        widget.setFocus()
        for event in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            self.app.sendEvent(widget, QKeyEvent(event, key, Qt.KeyboardModifier.NoModifier))
        self.pause(40)

    def capture(self, name):
        self.window.raise_()
        self.window.activateWindow()
        self.pause(180)
        path = self.images / (name + '.png')
        self.check(not path.exists(), 'screenshot target is fresh: ' + name)
        self.check(self.window.isVisible(), 'window visible for ' + name)
        self.check(self.window.grab().save(str(path)), 'saved actual widgets: ' + name)
        self.result['screenshots'].append(str(path))

    def write(self):
        w = self.window
        archive = _fixtures(self.root)
        # A long Unicode path is round-tripped through actual forms and TOML.
        # Stay below classic MAX_PATH so this checks UI handling on default Windows.
        long_root = self.root / ('длинный путь с пробелами ' + 'данные_' * 9)
        long_root.mkdir(parents=True, exist_ok=True)
        profile = long_root / 'профиль конвертации.toml'
        path_units = len(str(profile).encode('utf-16-le')) // 2
        self.check(path_units < 260, 'Unicode profile path below classic MAX_PATH')
        self.result['long_unicode_path_utf16_units'] = path_units
        original = self.root / 'original.toml'
        TomlStore(original).save(w.conversion.conversion_settings())
        w.conversion.toml_path.setText(str(original))
        w.conversion.load_button.click()
        self.idle()
        self.check(w.settings_bridge._toml_store is not None, 'loaded original TOML')
        w.conversion.receiver_model.setText('MOCK45CD roundtrip')
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(profile), 'TOML (*.toml)')):
            w.conversion.save_profile_as.click()
        self.idle()
        self.check(profile.is_file(), 'Save As writes Unicode long profile path')
        self.check(TomlStore(profile).load() == w.conversion.conversion_settings(), 'TOML roundtrip')
        self.check(TomlStore(original).load().receiver_model == 'MOCK', 'Save As preserves original')
        w.settings.station.setText('SMOKE45CD')
        w.settings.baud.setValue(230400)
        w.settings.expected_hz.setValue(2)
        w.settings.until_stop.setChecked(False)
        w.settings.seconds.setValue(45)
        w.settings.polling.setValue(500)
        w.settings.output.setText(str(long_root / 'записи UBX'))
        w.settings.stage1_roots.setPlainText(str(archive))
        w.settings.conversion_roots.setPlainText(str(self.root / 'archived conversions'))
        w.sidebar.setCurrentRow(2)
        w.settings.save.click()
        self.idle()
        self.wait(lambda: w.history.table.rowCount() == 12, 'synthetic history rendered')
        saved = json.loads(w.settings_bridge.path.read_text(encoding='utf-8'))
        self.check(saved['recording']['station'] == 'SMOKE45CD', 'preferences persisted')
        self.check(saved['conversion']['config_path'] == str(profile), 'default TOML persisted')
        (self.root / 'acceptance-expected.json').write_text(json.dumps(dict(
            profile=str(profile), output=str(long_root / 'записи UBX'))), encoding='utf-8')

    def read(self):
        w = self.window
        expected = json.loads((self.root / 'acceptance-expected.json').read_text(encoding='utf-8'))
        self.wait(lambda: w.settings_bridge._pending_profile is None and
                  w.conversion.receiver_model.text() == 'MOCK45CD roundtrip', 'restored profile')
        self.idle()
        self.check(w.settings.station.text() == 'SMOKE45CD', 'station restored in separate process')
        self.check(w.recording.station.text() == 'SMOKE45CD', 'recording defaults restored')
        self.check(w.recording.baud.currentText() == '230400', 'baud restored')
        self.check(w.recording.record_parameters()['seconds'] == 45, 'duration restored')
        self.check(w.recording.output.text() == expected['output'], 'long Unicode output restored')
        self.check(w.conversion.toml_path.text() == expected['profile'], 'profile path restored')
        self.check(w.settings.polling.value() == 500, 'polling restored')
        self.check(w.sidebar.currentRow() == 2, 'last page restored')
        self.wait(lambda: w.history.table.rowCount() == 12, 'history restored')
        self.check(w.history.conversion_table.rowCount() == 1, 'conversion history restored')
        self.check(w.history.conversion_table.item(0, 2).text() == 'OBS_ONLY_INCOMPLETE_NAVIGATION',
                   'OBS-only history remains incomplete')
        self.check(not w.coordinator.view()['can_start'], 'receiver identity requires fresh verification')
        w.history.search.setText('synthetic-0000')
        self.check(w.history.table.rowCount() == 1, 'history search filters rows')
        w.history.table.selectRow(0)
        self.wait(lambda: w.history._detail is not None, 'history detail delivered')
        self.idle()
        self.check('synthetic-0000' in w.history.detail_text.toPlainText(), 'real archive detail shown')
        self.check('OBS_ONLY_INCOMPLETE_NAVIGATION' in w.history.detail_text.toPlainText(),
                   'linked incomplete conversion shown in history detail')
        self.result['restart_verified'] = True

    def navigation_and_images(self):
        w = self.window
        w.resize(w.minimumWidth(), w.minimumHeight())
        self.pause()
        self.check(w.width() >= w.minimumWidth() and w.height() >= w.minimumHeight(), 'minimum window size respected')
        self.result['minimum_size'] = [w.minimumWidth(), w.minimumHeight()]
        w.sidebar.setCurrentRow(0)
        for index, name in enumerate(('recording', 'conversion', 'history', 'settings')):
            if index:
                self.key(w.sidebar, Qt.Key.Key_Down)
            self.check(w.sidebar.currentRow() == index and w.stack.currentIndex() == index,
                       'keyboard sidebar selects ' + name)
            self.capture(name + '-minimum')
            focus_before = self.app.focusWidget()
            self.key(w.sidebar, Qt.Key.Key_Tab)
            self.check(self.app.focusWidget() is not None and self.app.focusWidget() is not focus_before,
                       'Tab moves focus on ' + name)
        # Persist a deterministic last page after screenshots.
        w.sidebar.setCurrentRow(2)

    def cancel_and_close(self):
        w = self.window
        w.conversion.scenario.setCurrentIndex(w.conversion.scenario.findData('slow'))
        w._mock_fixture('slow')
        w.conversion.run.click()
        self.wait(lambda: w.conversion_coordinator.busy, 'mock conversion started')
        w.conversion.cancel.click()
        self.idle()
        self.check(w.conversion_coordinator.result is not None and
                   w.conversion_coordinator.result.get('status') == 'INTERRUPTED',
                   'cancel delivers INTERRUPTED terminal result')
        self.result['cancel_result'] = w.conversion_coordinator.result
        # Close during a second operation; Yes uses existing cancel/shutdown path.
        w.conversion.run.click()
        self.wait(lambda: w.conversion_coordinator.busy, 'second mock conversion started')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            w.close()
        self.check(w.isVisible() and not w._close_requested, 'Cancel in close dialog keeps GUI open')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            w.close()
            self.wait(lambda: all(c.shutdown_complete for c in self.coordinators()), 'four coordinator shutdown')
            self.wait(lambda: not w.isVisible(), 'native window closed')
        self.check(True, 'close waits for all four coordinators')

    def run(self):
        self.app.setQuitOnLastWindowClosed(False)
        self.images.mkdir(parents=True, exist_ok=False)
        self.timer.start()
        code = 1
        with ExitStack() as stack:
            serial = stack.enter_context(patch('serial.Serial', side_effect=AssertionError('COM forbidden in acceptance')))
            process = stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('External process forbidden in acceptance')))
            try:
                self.check(self.root is not None and self.window.conversion_backend.is_mock, 'mock backend required')
                self.check(self.app.platformName() == 'windows', 'native Windows Qt platform')
                self.check(abs(self.window.devicePixelRatioF() - float(self.result['scale'])) < 0.01,
                           'effective device pixel ratio matches requested scale')
                self.check(self.phase in ('write', 'read'), 'valid acceptance phase')
                self.wait(lambda: self.window.settings_bridge._loaded and
                          self.window.settings_bridge._pending_profile is None and
                          all(not c.busy for c in self.coordinators()) and
                          self.window.settings_bridge._profile_baseline is not None and
                          self.window.settings_bridge._profile_baseline ==
                          self.window.conversion.conversion_settings(),
                          'all workers idle with exact loaded profile baseline')
                if self.phase == 'write':
                    self.write()
                else:
                    self.read()
                self.navigation_and_images()
                if self.phase == 'read':
                    self.cancel_and_close()
                else:
                    self.window.close()
                    self.wait(lambda: all(c.shutdown_complete for c in self.coordinators()), 'write phase shutdown')
                self.check(serial.call_count == 0 and process.call_count == 0, 'zero COM and external process calls')
                self.result['passed'] = True
                code = 0
            except Exception:
                self.result['error'] = traceback.format_exc()
            finally:
                try:
                    if not all(c.shutdown_complete for c in self.coordinators()):
                        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
                            self.window.close()
                            self.wait(lambda: all(c.shutdown_complete for c in self.coordinators()), 'failure cleanup')
                except Exception:
                    self.result['cleanup_error'] = traceback.format_exc()
                    self.result['passed'], code = False, 1
                self.timer.stop()
                self.result.update(exit_code=code, elapsed_s=round(time.monotonic() - self.started, 3),
                    serial_open_calls=serial.call_count, subprocess_calls=process.call_count,
                    shutdown_complete_all_four=all(c.shutdown_complete for c in self.coordinators()),
                    heartbeat_count=len(self.beats), heartbeat_max_gap_s=round(max(
                        (b-a for a, b in zip(self.beats, self.beats[1:])), default=0), 3),
                    device_pixel_ratio=self.window.devicePixelRatioF())
                self.report.parent.mkdir(parents=True, exist_ok=True)
                with self.report.open('x', encoding='utf-8') as stream:
                    json.dump(self.result, stream, ensure_ascii=False, indent=2, default=str)
        self.app.exit(code)


def run_acceptance(app, window, report_path, images_path):
    """Schedule acceptance after show; retain driver until QApplication exits."""
    driver = _Acceptance(app, window, report_path, images_path)
    window._acceptance_driver = driver
    QTimer.singleShot(0, driver.run)
