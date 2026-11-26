"""Preferences editor; persistence and operation changes belong to MainWindow."""
from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ... import __version__


class SettingsPage(QWidget):
    apply_requested = Signal()
    save_requested = Signal()
    restore_requested = Signal()
    load_requested = Signal()
    capture_current_requested = Signal()

    def __init__(self, parent=None, *, operational=False):
        super().__init__(parent)
        self.operational = operational
        self._base = {}
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Настройки'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        body = QWidget()
        content = QVBoxLayout(body)
        form = QFormLayout()
        form.addRow(self.tr('Версия приложения'), QLabel(__version__))
        form.addRow(self.tr('Язык'), QLabel(self.tr('Русский (RU)')))
        self.polling = QSpinBox()
        self.polling.setRange(250, 2000)
        self.polling.setSingleStep(250)
        self.polling.setValue(250)
        self.polling.setSuffix(self.tr(' мс'))
        self.polling.setAccessibleName(self.tr('Интервал мониторинга'))
        form.addRow(self.tr('Интервал мониторинга'), self.polling)
        content.addLayout(form)
        if operational:
            self._build_editor(content)
        else:
            note = QLabel(self.tr('Настройки этого каркаса действуют только до закрытия окна. '
                                 'TOML и конфигурация приёмника не изменяются. '
                                 'Сохранение preferences — Stage 4.5.'))
            note.setWordWrap(True)
            content.addWidget(note)
        content.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        layout.addWidget(scroll)

    def _build_editor(self, content):
        group = QGroupBox(self.tr('Параметры следующей записи'))
        form = QFormLayout(group)
        self.output = QLineEdit()
        self.daily_root = QLineEdit()
        for label, field in ((self.tr('Каталог UBX'), self.output),
                             (self.tr('Каталог суточных данных'), self.daily_root)):
            row = QHBoxLayout()
            row.addWidget(field)
            button = QPushButton(self.tr('Обзор…'))
            button.clicked.connect(lambda checked=False, edit=field: self._browse_directory(edit))
            row.addWidget(button)
            form.addRow(label, row)
        self.daily_root.setPlaceholderText(self.tr('Пусто — суточная проекция отключена'))
        self.station = QLineEdit('GNSS')
        self.baud = QSpinBox()
        self.baud.setRange(1, 2147483647)
        self.baud.setValue(115200)
        self.expected_hz = QDoubleSpinBox()
        self.expected_hz.setDecimals(3)
        self.expected_hz.setRange(0.001, 10000)
        self.expected_hz.setValue(1)
        self.seconds = QDoubleSpinBox()
        self.seconds.setDecimals(3)
        self.seconds.setRange(0.001, 2147483647)
        self.seconds.setValue(60)
        self.until_stop = QCheckBox(self.tr('До ручной остановки'))
        self.until_stop.toggled.connect(lambda checked: self.seconds.setEnabled(not checked))
        duration = QHBoxLayout()
        duration.addWidget(self.seconds)
        duration.addWidget(self.until_stop)
        self.retention_policy = QLineEdit('keep_all')
        self.retention_policy.setReadOnly(True)
        self.auto_configured = QCheckBox(self.tr('Подготовить автоконвертацию'))
        self.auto_enabled = QCheckBox(self.tr('Автоконвертация включена по умолчанию'))
        for label, field in ((self.tr('Станция'), self.station), (self.tr('Скорость порта, бод'), self.baud),
                             (self.tr('Ожидаемая частота, Гц'), self.expected_hz),
                             (self.tr('Продолжительность, с'), duration),
                             (self.tr('Политика хранения'), self.retention_policy)):
            form.addRow(label, field)
        form.addRow(self.auto_configured)
        form.addRow(self.auto_enabled)
        self.capture_current = QPushButton(self.tr('Взять текущие параметры записи'))
        self.capture_current.setToolTip(self.tr('Скопировать параметры страницы «Запись» в эту форму.'))
        self.capture_current.clicked.connect(lambda checked=False: self.capture_current_requested.emit())
        form.addRow(self.capture_current)
        content.addWidget(group)
        group = QGroupBox(self.tr('Каталоги истории — один путь на строку'))
        form = QFormLayout(group)
        for name, label in (('recording_roots', self.tr('Каталоги записей и архивов')),
                            ('conversion_roots', self.tr('Каталоги конвертаций и архивов')),
                            ('stage1_roots', self.tr('Дополнительные архивы Stage 1'))):
            edit = QPlainTextEdit()
            edit.setMaximumHeight(85)
            edit.setAccessibleName(label)
            setattr(self, name, edit)
            row = QHBoxLayout()
            row.addWidget(edit)
            button = QPushButton(self.tr('Добавить…'))
            button.clicked.connect(lambda checked=False, field=edit: self._browse_directory(field))
            row.addWidget(button)
            form.addRow(label, row)
        content.addWidget(group)
        group = QGroupBox(self.tr('Профиль конвертации по умолчанию'))
        form = QFormLayout(group)
        self.config_path = QLineEdit()
        self.profile_browse = QPushButton(self.tr('Выбрать TOML…'))
        self.profile_browse.clicked.connect(self._browse_profile)
        row = QHBoxLayout()
        row.addWidget(self.config_path)
        row.addWidget(self.profile_browse)
        form.addRow(self.tr('Путь к TOML'), row)
        note = QLabel(self.tr('Параметры конвертации, загрузка и сохранение TOML находятся '
                             'на странице «Конвертация». Выбор пути здесь не загружает профиль.'))
        note.setWordWrap(True)
        form.addRow(note)
        content.addWidget(group)
        self.preferences_path = QLabel()
        self.preferences_path.setTextFormat(Qt.TextFormat.PlainText)
        self.preferences_path.setWordWrap(True)
        self.preferences_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        content.addWidget(self.preferences_path)
        actions = QHBoxLayout()
        for name, label, signal in (
            ('apply', self.tr('Применить'), self.apply_requested),
            ('save', self.tr('Сохранить'), self.save_requested),
            ('load', self.tr('Загрузить сохранённые'), self.load_requested),
            ('restore', self.tr('Восстановить по умолчанию'), self.restore_requested),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, requested=signal: requested.emit())
            setattr(self, name, button)
            actions.addWidget(button)
        content.addLayout(actions)
        self.status = QLabel(self.tr('Изменения применяются к следующей операции.'))
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        content.addWidget(self.status)

    def _browse_directory(self, field):
        current = field.text() if isinstance(field, QLineEdit) else ''
        path = QFileDialog.getExistingDirectory(self, self.tr('Выберите каталог'), current)
        if path:
            if isinstance(field, QPlainTextEdit):
                field.appendPlainText(path)
            else:
                field.setText(path)

    def _browse_profile(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr('Выберите профиль TOML'), self.config_path.text(),
            self.tr('Профили TOML (*.toml);;Все файлы (*)'))
        if path:
            self.config_path.setText(path)

    def set_preferences_path(self, path):
        if self.operational:
            self.preferences_path.setText(self.tr('Файл настроек: ') + str(path))

    def set_status(self, text):
        if self.operational:
            self.status.setText(str(text))

    def set_preferences(self, preferences):
        """Populate from validated preferences without reading any files."""
        self._base = deepcopy(preferences)
        self.polling.setValue(preferences.get('interface', {}).get('polling_ms', 250))
        if not self.operational:
            return
        recording = preferences.get('recording', {})
        for name in ('output', 'daily_root', 'station'):
            getattr(self, name).setText(str(recording.get(name) or ''))
        for name, default in (('baud', 115200), ('expected_hz', 1)):
            getattr(self, name).setValue(recording.get(name, default))
        seconds = recording.get('seconds')
        self.seconds.setValue(60 if seconds is None else seconds)
        self.until_stop.setChecked(seconds is None)
        for name in ('auto_configured', 'auto_enabled'):
            getattr(self, name).setChecked(recording.get(name, False))
        for name in ('recording_roots', 'conversion_roots', 'stage1_roots'):
            getattr(self, name).setPlainText('\n'.join(preferences.get('history', {}).get(name, [])))
        self.config_path.setText(str(preferences.get('conversion', {}).get('config_path') or ''))

    def preferences(self, base=None):
        """Copy and overlay form values; validation belongs to the caller."""
        result = deepcopy(self._base if base is None else base)
        result.setdefault('interface', {})['polling_ms'] = self.polling.value()
        if not self.operational:
            return result
        recording = result.setdefault('recording', {})
        for name in ('output', 'station'):
            recording[name] = getattr(self, name).text().strip()
        recording['daily_root'] = self.daily_root.text().strip() or None
        for name in ('baud', 'expected_hz'):
            recording[name] = getattr(self, name).value()
        recording['seconds'] = None if self.until_stop.isChecked() else self.seconds.value()
        recording['retention_policy'] = 'keep_all'
        for name in ('auto_configured', 'auto_enabled'):
            recording[name] = getattr(self, name).isChecked()
        history = result.setdefault('history', {})
        for name in ('recording_roots', 'conversion_roots', 'stage1_roots'):
            history[name] = [line.strip() for line in getattr(self, name).toPlainText().splitlines()
                             if line.strip()]
        result.setdefault('conversion', {})['config_path'] = self.config_path.text().strip() or None
        return result
