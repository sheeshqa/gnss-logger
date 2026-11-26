"""GUI-only preferences/TOML orchestration; all storage runs off the UI thread."""
from copy import deepcopy
from math import isclose
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .preferences import PreferencesStore, default_preferences, validate_preferences
from .toml_writer import TomlStore
from .workers import TaskWorker


class SettingsBridge(QObject):
    ready_to_close = Signal()

    def __init__(self, window, path):
        super().__init__(window)
        self.window = window
        self.path = Path(path)
        self.defaults = default_preferences(window.mock_root, window.coordinator.provider.is_mock)
        self.store = PreferencesStore(self.path, self.defaults)
        self.values = deepcopy(self.defaults)
        self._saved_values = deepcopy(self.values)
        self._loaded = False
        self._persisted = False
        self._shutdown_ui_saved = False
        self._worker = None
        self._delivered = False
        self._action = None
        self._toml_store = None
        self._profile_baseline = None
        self._resetting_profile = False
        self._pending_profile = None
        self._pending_recording = None
        self.closing = self.shutdown_complete = False
        self._close_after_save = False
        self._bad_preferences = False
        page = window.settings
        page.set_preferences_path(self.path)
        page.set_preferences(self.values)
        page.apply_requested.connect(self.apply)
        page.save_requested.connect(self.save)
        page.restore_requested.connect(self.restore)
        page.load_requested.connect(self.load)
        page.capture_current_requested.connect(self.capture_recording)
        # Restore milliseconds directly rather than through rounded minutes.
        window.recording.duration_unit.addItem(self.tr('Секунды'), 1)
        window.recording.duration.setMaximum(2147483647)
        conversion = window.conversion
        conversion.load_requested.disconnect(window.conversion_coordinator.load)
        conversion.load_requested.connect(self.load_profile)
        conversion.save_profile.clicked.connect(lambda: self.save_profile(False))
        conversion.save_profile_as.clicked.connect(lambda: self.save_profile(True))
        conversion.reset_profile.clicked.connect(self.reset_profile)
        conversion.form_changed.connect(self._dirty_changed)
        window.conversion_coordinator.settings_loaded.connect(self.profile_loaded)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._check)
        self._timer.start()
        self.load()

    @property
    def busy(self):
        return self._worker is not None

    def _run(self, kind, action):
        coordinator = self.window.conversion_coordinator
        if self.busy or self.closing or (kind == 'profile_load' and coordinator.busy):
            return False
        worker = TaskWorker(action, 0, self)
        self._worker, self._action, self._delivered = worker, kind, False
        if kind == 'profile_load':
            coordinator._loader = worker
            coordinator._load_delivered = False
            coordinator._notify()
        worker.outcome.connect(self._outcome, Qt.ConnectionType.QueuedConnection)
        try:
            worker.start()
        except Exception as error:
            self._worker = None
            if kind == 'profile_load':
                coordinator._loader = None
                coordinator._notify()
            worker.deleteLater()
            self._error(str(error))
            return False
        self.window.settings.set_status(self.tr('Выполняется операция с настройками…'))
        return True

    def _error(self, message):
        self.window.settings.set_status(message)
        self.window._error(message)
        self._close_after_save = False

    def load(self):
        def read():
            existed = self.path.exists()
            values = self.store.load()
            return dict(values=values, warnings=list(self.store.warnings), existed=existed)
        return self._run('load', read)

    def profile_loaded(self, settings):
        if self._resetting_profile:
            self._resetting_profile = False
        else:
            self._profile_baseline = settings
        self._dirty_changed()

    def _dirty_changed(self):
        self.window.conversion.dirty_label.setText(self.tr('Профиль изменён, но не сохранён')
            if self.dirty_profile() else self.tr('Профиль без несохранённых изменений'))

    def capture_recording(self):
        try:
            page = self.window.recording
            parameters = page.record_parameters()
            values = self.window.settings.preferences(self.values)
            values['recording'].update({key: str(value) if isinstance(value, Path) else value
                                       for key, value in parameters.items()})
            values['recording'].update(baud=int(page.baud.currentText()),
                auto_configured=page.auto_configured.isChecked(), auto_enabled=page.auto_enabled.isChecked())
            values = validate_preferences(values, self.defaults)
            self._confine(values)
            self.window.settings.set_preferences(values)
            self.window.settings.set_status(self.tr('Текущая форма записи скопирована; сохраните настройки явно.'))
        except (ValueError, OSError) as error:
            self._error(str(error))

    def dirty_profile(self):
        try:
            return self._profile_baseline is not None and self.window.conversion.conversion_settings() != self._profile_baseline
        except (ValueError, OSError):
            return True

    def _geometry(self):
        rectangle = self.window.normalGeometry() if self.window.isMaximized() else self.window.geometry()
        return dict(x=rectangle.x(), y=rectangle.y(), width=rectangle.width(), height=rectangle.height(),
                    maximized=self.window.isMaximized())

    def _candidate(self):
        values = self.window.settings.preferences(self.values)
        values['interface'].update(geometry=self._geometry(), page=self.window.sidebar.currentRow())
        values = validate_preferences(values, self.defaults)
        self._confine(values)
        return values

    def _confine(self, values):
        recording = values['recording']
        if recording['baud'] > 2147483647 or not 0.001 <= recording['expected_hz'] <= 10000 or (recording['seconds'] is not None and
                not 0.001 <= recording['seconds'] <= 2147483647):
            raise ValueError(self.tr('Значения скорости, частоты или продолжительности вне диапазона GUI'))
        for name in ('seconds', 'expected_hz'):
            value = recording[name]
            if value is not None and not isclose(value, round(value, 3), rel_tol=0, abs_tol=1e-9):
                raise ValueError(self.tr('Точность параметров GUI — не более трёх знаков после запятой'))
        if self.window.mock_root is None:
            return
        root = self.window.mock_root.resolve()
        paths = [values['recording']['output'], values['recording']['daily_root'], values['conversion']['config_path']]
        for rows in values['history'].values():
            paths.extend(rows)
        if any(path is not None and not Path(path).resolve().is_relative_to(root) for path in paths):
            raise ValueError(self.tr('В MOCK все пути настроек должны оставаться внутри mock-каталога'))

    def apply(self):
        try:
            values = self._candidate()
            profile = values['conversion']['config_path']
            if profile and profile != self.values['conversion']['config_path'] and self.dirty_profile():
                if QMessageBox.question(self.window, self.tr('Несохранённый профиль'),
                        self.tr('Отбросить изменения и загрузить выбранный TOML?')) != QMessageBox.StandardButton.Yes:
                    return False
                self._profile_baseline = self.window.conversion.conversion_settings()
            self.apply_values(values)
            if profile and profile != self.window.conversion.toml_path.text().strip():
                self._pending_profile = profile
            self.window.settings.set_status(self.tr('Применено для следующих операций; на диск ещё не сохранено.'))
            return True
        except (ValueError, OSError) as error:
            self._error(str(error))
            return False

    def apply_values(self, values, restore_window=False):
        self._confine(values)
        self.values = deepcopy(values)
        self.window.settings.set_preferences(values)
        self._pending_recording = deepcopy(values['recording'])
        self.apply_recording_if_idle()
        roots = values['history']
        self.window.history_coordinator.configure_roots(roots['recording_roots'], roots['conversion_roots'], roots['stage1_roots'])
        self.window.settings.polling.setValue(values['interface']['polling_ms'])
        if restore_window:
            geometry = values['interface']['geometry']
            if geometry:
                from PySide6.QtCore import QRect
                screens = QApplication.screens()
                available = QApplication.primaryScreen().availableGeometry()
                for screen in screens:
                    area = screen.availableGeometry()
                    if area.left() <= geometry['x'] <= area.right() and area.top() <= geometry['y'] <= area.bottom():
                        available = area
                        break
                width = min(max(900, geometry['width']), max(900, available.width()))
                height = min(max(600, geometry['height']), max(600, available.height()))
                x = max(available.left(), min(geometry['x'], available.right() - min(width, available.width()) + 1))
                y = max(available.top(), min(geometry['y'], available.bottom() - min(height, available.height()) + 1))
                rect = QRect(x, y, width, height)
                self.window.setGeometry(rect)
                if geometry['maximized']:
                    self.window.showMaximized()
            self.window.sidebar.setCurrentRow(values['interface']['page'])

    def apply_recording_if_idle(self):
        if self._pending_recording is None or self.window.coordinator.busy or self.window.coordinator.closing:
            return
        values, self._pending_recording = self._pending_recording, None
        page = self.window.recording
        page.output.setText(values['output'])
        if values['daily_root'] is not None:
            page.daily_root.setText(values['daily_root'])
        page.daily_enabled.setChecked(values['daily_root'] is not None)
        page.station.setText(values['station'])
        page.expected_hz.setValue(values['expected_hz'])
        page.duration_unit.setCurrentIndex(page.duration_unit.findData(None if values['seconds'] is None else 1))
        if values['seconds'] is not None:
            page.duration.setValue(values['seconds'])
        page.auto_configured.setChecked(values['auto_configured'])
        page.auto_enabled.setChecked(values['auto_enabled'])
        page.baud.setCurrentText(str(values['baud']))
        self.window.coordinator.set_baud(values['baud'])

    def save(self):
        try:
            values = self._candidate()
            if self.dirty_profile():
                self._error(self.tr('Сначала сохраните изменённый профиль TOML на странице «Конвертация».'))
                return False
            recover = False
            if self._bad_preferences:
                recover = QMessageBox.question(self.window, self.tr('Восстановить настройки?'),
                    self.tr('Повреждённый оригинал будет сохранён отдельно. Записать проверенные настройки?')) == QMessageBox.StandardButton.Yes
                if not recover:
                    return False
            return self._run('save', lambda: self.store.save(values, recover=recover))
        except (ValueError, OSError) as error:
            self._error(str(error))
            return False

    def restore(self):
        self.window.settings.set_preferences(self.defaults)
        self.window.settings.set_status(self.tr('Значения по умолчанию в форме. Примените или сохраните явно.'))

    def load_profile(self, path):
        if self.dirty_profile() and QMessageBox.question(self.window, self.tr('Несохранённый профиль'),
                self.tr('Отбросить изменения формы и загрузить другой TOML?')) != QMessageBox.StandardButton.Yes:
            return False
        target = Path(path).expanduser()
        if not target.is_absolute():
            self._error(self.tr('Укажите абсолютный путь TOML'))
            return False
        if self.window.mock_root is not None and not target.resolve().is_relative_to(self.window.mock_root.resolve()):
            self._error(self.tr('В MOCK профиль должен находиться внутри mock-каталога'))
            return False
        store = TomlStore(target)
        def read():
            settings = store.load()
            if self.window.conversion_backend.is_mock:
                self.window.conversion_backend._check_settings(settings)
            return dict(settings=settings, path=str(target), store=store)
        # TaskWorker deep-copies this plain store; no locks or open handles.
        return self._run('profile_load', read)

    def save_profile(self, save_as=False):
        if self.busy or self.closing:
            return False
        try:
            settings = self.window.conversion.conversion_settings()
            text = self.window.conversion.toml_path.text().strip()
            if save_as or not text:
                text, _ = QFileDialog.getSaveFileName(self.window, self.tr('Сохранить профиль TOML'),
                    str(self.window.mock_root or self.path.parent), 'TOML (*.toml)')
                if not text:
                    return False
            path = Path(text).expanduser()
            if not path.is_absolute():
                raise ValueError(self.tr('Укажите абсолютный путь TOML'))
            if self.window.mock_root is not None:
                if not path.resolve().is_relative_to(self.window.mock_root.resolve()):
                    raise ValueError(self.tr('В MOCK TOML должен оставаться внутри mock-каталога'))
                self.window.conversion_backend._check_settings(settings)
            overwrite = path.exists()
            if overwrite and QMessageBox.question(self.window, self.tr('Перезаписать TOML?'),
                    self.tr('Комментарии и форматирование будут потеряны. Пути сохраняются абсолютными. Продолжить?')) != QMessageBox.StandardButton.Yes:
                return False
            store = self._toml_store if self._toml_store and self._toml_store.path == path.resolve() else TomlStore(path)
            def write():
                if overwrite and store is not self._toml_store:
                    store.load()
                store.save(settings, overwrite=overwrite)
                return dict(settings=settings, path=str(path.resolve()), store=store)
            return self._run('profile_save', write)
        except (ValueError, OSError) as error:
            self._error(str(error))
            return False

    def reset_profile(self):
        if self.closing:
            return
        if self.dirty_profile() and QMessageBox.question(self.window, self.tr('Несохранённый профиль'),
                self.tr('Отбросить изменения и восстановить профиль по умолчанию?')) != QMessageBox.StandardButton.Yes:
            return
        self._resetting_profile = True
        if not self.window.conversion_coordinator.load():
            self._resetting_profile = False

    def confirm_close(self):
        if self.closing or self.window.coordinator.closing or not self.dirty_profile():
            return True
        answer = QMessageBox.question(self.window, self.tr('Несохранённый профиль TOML'),
            self.tr('Да — сохранить; Нет — отбросить изменения формы; Отмена — остаться.'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Yes:
            self._close_after_save = True
            if not self.save_profile(False):
                self._close_after_save = False
            return False
        return True

    def request_shutdown(self):
        self.closing = True
        self._check()

    def _outcome(self, outcome):
        self._delivered = True
        if outcome['error']:
            self._error(outcome['error'])
            return
        value = outcome['value']
        if self._action == 'load':
            self._loaded = True
            self._persisted = value['existed']
            try:
                self._confine(value['values'])
            except (ValueError, OSError) as error:
                value['warnings'].append(str(error))
                value['values'] = deepcopy(self.defaults)
            self.values = value['values']
            self._saved_values = deepcopy(self.values)
            self._bad_preferences = bool(value['warnings'])
            self.window.settings.set_preferences(self.values)
            if value['existed'] and not value['warnings'] and not self.closing:
                self.apply_values(self.values, restore_window=True)
                profile = self.values['conversion']['config_path']
                if profile:
                    self._pending_profile = profile
            self.window.settings.set_status('\n'.join(value['warnings']) or self.tr('Настройки загружены.'))
        elif self._action == 'save':
            self._persisted = True
            self.values = value
            self._saved_values = deepcopy(value)
            self._bad_preferences = False
            if not self.closing:
                self.apply_values(value)
            self.window.settings.set_status(self.tr('Настройки сохранены.'))
        elif self._action == 'ui_save':
            self._saved_values = deepcopy(value)
        elif self._action in ('profile_load', 'profile_save'):
            self._toml_store = value['store']
            self._profile_baseline = value['settings']
            self.window.conversion.set_settings(value['settings'])
            self.window.conversion.toml_path.setText(value['path'])
            self.window.settings.config_path.setText(value['path'])
            self.window.settings.set_status(self.tr('Профиль TOML загружен или сохранён.'))
            self._dirty_changed()

    def _check(self):
        if self._worker is not None and self._delivered and self._worker.isFinished():
            if self._action == 'profile_load':
                self.window.conversion_coordinator._loader = None
                self.window.conversion_coordinator._notify()
            self._worker.deleteLater()
            self._worker = None
            if self._close_after_save:
                self._close_after_save = False
                QTimer.singleShot(0, self.window.close)
        if (not self.closing and not self.busy and self._pending_profile
                and not self.window.conversion_coordinator.busy):
            path, self._pending_profile = self._pending_profile, None
            self.load_profile(path)
        if self.closing and not self.busy and not self.shutdown_complete:
            if self._loaded and self._persisted and not self._bad_preferences and not self._shutdown_ui_saved:
                self._shutdown_ui_saved = True
                values = deepcopy(self._saved_values)
                values['interface'].update(geometry=self._geometry(), page=self.window.sidebar.currentRow())
                # Close saves only UI placement, not unsaved recording/profile defaults.
                self.closing = False
                launched = self._run('ui_save', lambda: self.store.save(values))
                self.closing = True
                if launched:
                    return
            self.shutdown_complete = True
            self._timer.stop()
            self.ready_to_close.emit()
