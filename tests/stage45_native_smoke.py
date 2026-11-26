"""Manual native Windows acceptance smoke; deliberately outside test discovery.

Run from the repository with the existing .venv-gui interpreter, only after
integration is ready and the main acceptance runner has released the test slot:
    <gui-python> tests/stage45_native_smoke.py

No hardware or converter is executed. Metadata below is explicitly synthetic,
using the Stage 1 schema exercised by test_application_history. Screenshots are
captures of the actual operational widgets after persistence and window restart.
The isolated root is retained for inspection and printed in the JSON result.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))
os.environ['QT_QPA_PLATFORM'] = 'windows'

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from gnss_logger.application.history import HistoryService
from gnss_logger.gui.conversions import MockConversionBackend
from gnss_logger.gui.main_window import MainWindow
from gnss_logger.gui.receivers import MockReceiverBackend
from gnss_logger.gui.toml_writer import TomlStore


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def fixtures(root, count):
    """Write archived synthetic evidence, never fabricated UI payloads."""
    archive, converted = root / 'archive', root / 'conversions'
    archive.mkdir()
    converted.mkdir()
    first = None
    for index in range(count):
        directory = archive / f'synthetic-{index:04d}'
        directory.mkdir()
        capture = directory / 'synthetic.ubx'
        data = b'SYNTHETIC metadata fixture; not receiver measurements\n' + str(index).encode()
        capture.write_bytes(data)
        artifact = dict(path=str(capture), bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                        complete=True, status='closed_complete')
        manifest = dict(schema_version=1, session_id=directory.name,
                        started_utc='2026-11-25T00:00:00Z', ended_utc='2026-11-25T00:01:00Z',
                        exit_code=0, registration='STOPPED_COMPLETE',
                        parameters=dict(station='SMOKE', baud=115200), artifacts=[artifact])
        (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        if first is None:
            first = artifact
    obs = converted / 'synthetic.obs'
    obs.write_text('SYNTHETIC fixture only, no scientific observations\n', encoding='ascii')
    report = dict(schema_id='gnss_logger.conversion', schema_version=2,
                  operation_id='synthetic-obs-only', status='OBS_ONLY_INCOMPLETE_NAVIGATION',
                  segments=[dict(source=first, status='OBS_ONLY_INCOMPLETE_NAVIGATION',
                                 published_outputs=dict(obs=str(obs)), validation={}, errors=[],
                                 warnings=['Synthetic archive: NAV is intentionally absent'])])
    (converted / 'conversion-synthetic.json').write_text(json.dumps(report), encoding='utf-8')
    return archive, converted


class Smoke:
    def __init__(self, root, images, count):
        self.root, self.images, self.count = root, images, count
        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        require(self.app.platformName() == 'windows', 'Native windows Qt platform required')
        QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
        self.app.setFont(QFont('Segoe UI', 9))
        self.beats = []
        self.timer = QTimer()
        self.timer.setInterval(20)
        self.timer.timeout.connect(lambda: self.beats.append(time.monotonic()))
        self.timer.start()
        self.windows = []
        self.result = dict(platform=self.app.platformName(), root=str(root),
                           preferences=str(root / 'preferences.json'), sessions=count)

    def wait(self, predicate, description, timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        raise TimeoutError(description)

    @staticmethod
    def coordinators(window):
        return (window.coordinator, window.conversion_coordinator,
                window.history_coordinator, window.settings_bridge)

    def idle(self, window):
        self.wait(lambda: all(not c.busy for c in self.coordinators(window)), 'workers idle')

    def open(self):
        window = MainWindow(MockReceiverBackend(), operational=True,
                            conversion_backend=MockConversionBackend(self.root), mock_root=self.root,
                            preferences_path=self.root / 'preferences.json')
        self.windows.append(window)
        window.resize(1450, 1000)
        window.show()
        self.wait(lambda: window.settings_bridge._loaded and
                  window.settings_bridge._profile_baseline is not None, 'initial settings load')
        self.idle(window)
        return window

    def close(self, window):
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            window.close()
            self.wait(lambda: all(c.shutdown_complete for c in self.coordinators(window)),
                      'all four coordinators shutdown', timeout=45)
            self.wait(lambda: not window.isVisible(), 'window closed')

    def capture(self, window, name):
        self.wait(lambda: window.isVisible() and window.isActiveWindow(), 'visible active native window', 10)
        QTest.qWait(150)  # Allow the native paint/expose cycle, with Qt event delivery.
        path = self.images / name
        require(window.grab().save(str(path)), f'Could not save {path}')
        return str(path)

    def run(self):
        archive, converted = fixtures(self.root, self.count)
        self.result['history_roots'] = dict(stage1=[str(archive)], conversion=[str(converted)])
        window = self.open()
        profile = self.root / 'profiles' / 'saved-as.toml'
        profile.parent.mkdir()
        original = self.root / 'original.toml'
        TomlStore(original).save(window.conversion.conversion_settings())
        window.conversion.toml_path.setText(str(original))
        window.conversion.load_button.click()
        self.idle(window)
        require(window.settings_bridge._toml_store is not None and
                window.settings_bridge._toml_store.path == original, 'Source TOML was not loaded')
        window.conversion.receiver_model.setText('MOCK45 roundtrip')
        expected = window.conversion.conversion_settings()
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(profile), 'TOML (*.toml)')):
            window.conversion.save_profile_as.click()
        self.idle(window)
        require(profile.is_file(), 'Save As did not write TOML')
        require(TomlStore(profile).load() == expected, 'TOML roundtrip differs')
        require(TomlStore(original).load().receiver_model == 'MOCK', 'Save As changed original')
        require(not window.settings_bridge.dirty_profile(), 'Saved profile remains dirty')
        window.settings.station.setText('SMOKE45')
        window.settings.baud.setValue(230400)
        window.settings.expected_hz.setValue(2.0)
        window.settings.seconds.setValue(45)
        window.settings.polling.setValue(500)
        window.settings.stage1_roots.setPlainText(str(archive))
        window.settings.conversion_roots.setPlainText(str(converted))
        window.sidebar.setCurrentRow(2)
        started, beat_start = time.monotonic(), len(self.beats)
        window.settings.save.click()
        self.idle(window)
        self.wait(lambda: window.history.table.rowCount() == self.count, 'sessions rendered')
        self.result['history_load_elapsed_s'] = round(time.monotonic() - started, 3)
        self.result['history_load_heartbeats'] = len(self.beats) - beat_start
        require(self.result['history_load_heartbeats'] > 0, 'No heartbeat during history load')
        saved = json.loads((self.root / 'preferences.json').read_text(encoding='utf-8'))
        require(saved['recording']['station'] == 'SMOKE45', 'Preferences were not saved')
        require(saved['conversion']['config_path'] == str(profile), 'Profile default not persisted')
        self.close(window)
        window = self.open()
        self.wait(lambda: window.settings_bridge._pending_profile is None and
                  window.conversion.conversion_settings() == expected, 'profile restored after restart')
        self.idle(window)
        require(window.mock_root == self.root, 'Restart changed isolated root')
        require(window.settings.station.text() == 'SMOKE45', 'Station not restored')
        require(window.recording.station.text() == 'SMOKE45', 'Recording form not restored')
        require(window.recording.baud.currentText() == '230400', 'Recording baud not restored')
        require(window.recording.expected_hz.value() == 2.0, 'Expected frequency not restored')
        require(window.recording.record_parameters()['seconds'] == 45, 'Duration not restored')
        require(window.settings.polling.value() == 500, 'Polling not restored')
        require(window.sidebar.currentRow() == 2, 'Last page not restored')
        require(window.history.table.rowCount() == self.count, 'History not restored')
        require(window.history.conversion_table.rowCount() == 1, 'Conversion history not restored')
        require(window.history.conversion_table.item(0, 2).text() == 'OBS_ONLY_INCOMPLETE_NAVIGATION',
                'OBS-only status changed')
        # Compare the displayed stable key with the actual service result.
        window.history.search.setText('synthetic-0000')
        require(window.history.table.rowCount() == 1, 'History search failed')
        key = window.history.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        service = HistoryService([], [converted], [archive])
        require(service.get_session(key)['session_key'] == key, 'UI key differs from HistoryService')
        window.history.table.selectRow(0)
        self.wait(lambda: window.history._detail is not None and
                  window.history._detail['session_key'] == key, 'actual session detail')
        self.idle(window)
        require('OBS_ONLY_INCOMPLETE_NAVIGATION' in window.history.detail_text.toPlainText(),
                'Linked conversion missing from session details')
        self.images.mkdir(parents=True, exist_ok=True)
        window.raise_()
        window.activateWindow()
        history_image = self.capture(window, 'stage4_5_history.png')
        window.sidebar.setCurrentRow(3)
        settings_image = self.capture(window, 'stage4_5_settings.png')
        self.result.update(screenshots=[history_image, settings_image], profile=str(profile),
                           conversion_settings=asdict(expected), restart_verified=True)
        self.close(window)

    def cleanup(self):
        for window in self.windows:
            if not all(c.shutdown_complete for c in self.coordinators(window)):
                self.close(window)
        self.timer.stop()
        self.result['shutdown_complete_all_four'] = all(
            all(c.shutdown_complete for c in self.coordinators(w)) for w in self.windows)
        self.result['heartbeat_count'] = len(self.beats)
        self.result['heartbeat_max_gap_s'] = round(max(
            (b - a for a, b in zip(self.beats, self.beats[1:])), default=0), 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', type=Path, default=REPOSITORY / 'docs' / 'images')
    parser.add_argument('--sessions', type=int, default=12)
    args = parser.parse_args()
    require(sys.platform == 'win32', 'This acceptance smoke requires Windows')
    require(args.sessions >= 1, 'At least one session required')
    root = Path(tempfile.mkdtemp(prefix='gnss-stage45-native-')).resolve()
    started = time.monotonic()
    smoke = Smoke(root, args.images.resolve(), args.sessions)
    code = 1
    try:
        with patch('serial.Serial', side_effect=AssertionError('Real COM access forbidden')) as serial_open, \
                patch('subprocess.Popen', side_effect=AssertionError('External processes forbidden')) as process:
            smoke.run()
            smoke.result.update(serial_open_calls=serial_open.call_count,
                                subprocess_calls=process.call_count)
        code = 0
    except Exception:
        smoke.result['error'] = traceback.format_exc()
    finally:
        try:
            smoke.cleanup()
        except Exception:
            smoke.result['cleanup_error'] = traceback.format_exc()
            code = 1
        smoke.result.update(exit_code=code, elapsed_s=round(time.monotonic() - started, 3))
        print(json.dumps(smoke.result, ensure_ascii=True, default=str, indent=2), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
