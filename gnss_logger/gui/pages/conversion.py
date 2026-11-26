from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import Signal, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout,
                              QHBoxLayout, QLabel, QLineEdit, QListWidget,
                              QListWidgetItem, QPlainTextEdit, QPushButton,
                              QScrollArea, QTableWidget, QTableWidgetItem,
                              QVBoxLayout, QWidget)

from ... import application as api
from ...conversion.config import GROUPS, MASKS, default_root


class ConversionPage(QWidget):
    form_changed = Signal()
    load_requested = Signal(str)
    run_requested = Signal()
    cancel_requested = Signal()
    fixture_requested = Signal(str)

    def __init__(self, parent=None, *, operational=False, mock=False, mock_root=None):
        super().__init__(parent)
        self.operational = operational
        if operational:
            self._build_form(mock, mock_root)
            return
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Конвертация'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        note = QLabel(self.tr('UBX → RINEX 3.02. Подключение существующего ConversionJob — Stage 4.4. '
                              'Здесь convbin не запускается и файлы не создаются.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        for label in (self.tr('Выбрать завершённый UBX'), self.tr('Run conversion'),
                      self.tr('Cancel manual'), self.tr('Auto conversion — настройки до Start')):
            button = QPushButton(label)
            button.setEnabled(False)
            layout.addWidget(button)
        table = QTableWidget(0, 3)
        table.setHorizontalHeaderLabels([self.tr('Сегмент'), self.tr('Статус'), self.tr('Файлы')])
        layout.addWidget(table)
        caption = QLabel(self.tr('Результатов пока нет. OBS без NAV, SKIPPED и предупреждения '
                                 'будут отображаться отдельно, без изменения правил ядра.'))
        caption.setWordWrap(True)
        layout.addWidget(caption)

    def _build_form(self, mock, mock_root):
        self.mock = mock
        self.mock_root = Path(mock_root).resolve() if mock_root is not None else None
        self._setting_form = False
        self._state = dict(busy=False, closing=False, phase='IDLE')
        self._directory = self._report = None
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Конвертация — MOCK, синтетические результаты') if mock else self.tr('Конвертация'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        self.archive_warning = QLabel(self.tr(
            'Для файла или каталога выбирайте только завершённый архив: процесс записи '
            'должен быть остановлен, а записывающий файл закрыт. Расширение .ubx само по себе '
            'не доказывает завершение записи. В режиме сессии ядро проверяет конечное '
            'состояние по манифесту; укажите run_id явно. Активные файлы .part '
            'не предназначены для конвертации.'))
        self.archive_warning.setWordWrap(True)
        form.addRow(self.archive_warning)

        self.source_kind = QComboBox()
        for mode in ('file', 'directory', 'session'):
            self.source_kind.addItem({'file': self.tr('Файл'), 'directory': self.tr('Каталог'),
                                           'session': self.tr('Сессия')}[mode], mode)
        form.addRow(self.tr('Тип источника'), self.source_kind)
        self.source = QLineEdit()
        self.source_button = QPushButton(self.tr('Выбрать синтетический пример') if mock else self.tr('Выбрать источник'))
        row = QHBoxLayout()
        row.addWidget(self.source)
        row.addWidget(self.source_button)
        form.addRow(self.tr('Завершённый источник UBX'), row)
        self.session_id = QLineEdit()
        form.addRow(self.tr('Идентификатор сессии run_id'), self.session_id)
        self.toml_path = QLineEdit()
        self.toml_browse = QPushButton(self.tr('Выбрать TOML'))
        self.load_button = QPushButton(self.tr('Загрузить'))
        self.save_profile = QPushButton(self.tr('Сохранить TOML'))
        self.save_profile_as = QPushButton(self.tr('Сохранить как…'))
        self.reset_profile = QPushButton(self.tr('Профиль по умолчанию'))
        row = QHBoxLayout()
        for widget in (self.toml_path, self.toml_browse, self.load_button):
            row.addWidget(widget)
        form.addRow(self.tr('Конфигурация'), row)
        profile_actions = QHBoxLayout()
        for button in (self.save_profile, self.save_profile_as, self.reset_profile):
            profile_actions.addWidget(button)
        form.addRow(profile_actions)
        self.dirty_label = QLabel()
        form.addRow(self.dirty_label)
        form.addRow(self.tr('Версия RINEX'), QLabel('3.02'))
        self.systems = {name: QCheckBox(name) for name in 'GREC'}
        self.groups = {name: QCheckBox(name) for name in GROUPS}
        self.observations = {name: QCheckBox(name) for name in 'CLDS'}
        for label, controls in ((self.tr('Системы'), self.systems), (self.tr('Группы сигналов'), self.groups),
                                (self.tr('Наблюдения'), self.observations)):
            row = QHBoxLayout()
            for control in controls.values():
                row.addWidget(control)
            form.addRow(label, row)
        for name in 'CL':
            self.observations[name].setChecked(True)
            self.observations[name].setEnabled(False)
        self.masks = QListWidget()
        self.masks.setMaximumHeight(130)
        for mask in MASKS:
            item = QListWidgetItem(mask, self.masks)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
        form.addRow(self.tr('Полные маски сигналов'), self.masks)
        self.obs = QCheckBox('OBS')
        self.nav = QCheckBox('NAV')
        self.allow_obs_without_nav = QCheckBox(self.tr('Разрешить OBS без NAV (неполный результат)'))
        for control in (self.obs, self.nav, self.allow_obs_without_nav):
            form.addRow(control)
        for name, label in (('timeout_s', self.tr('Таймаут (с)')), ('interval_s', self.tr('Интервал (с)')),
                            ('executable', self.tr('Исполняемый файл convbin')), ('output_root', self.tr('Выходной каталог')),
                            ('runs_root', self.tr('Корень сессий')), ('daily_root', self.tr('Корень суточных файлов')),
                            ('receiver_model', self.tr('Модель приёмника'))):
            control = QLineEdit()
            setattr(self, name, control)
            form.addRow(label, control)
        self.scenario = QComboBox()
        for scenario in ('success', 'warnings', 'obs_only', 'failed', 'slow', 'skipped'):
            self.scenario.addItem({
                'success': self.tr('Успех'), 'warnings': self.tr('С предупреждениями'),
                'obs_only': self.tr('Только OBS, без NAV'), 'failed': self.tr('Ошибка'),
                'slow': self.tr('Длительная операция'), 'skipped': self.tr('Пропущено'),
            }[scenario], scenario)
        self.scenario.setVisible(mock)
        form.addRow(self.tr('Синтетический сценарий') if mock else '', self.scenario)
        self.run = QPushButton(self.tr('Запустить конвертацию'))
        self.cancel = QPushButton(self.tr('Отменить ручную конвертацию'))
        self.open_directory = QPushButton(self.tr('Открыть каталог'))
        self.open_report = QPushButton(self.tr('Открыть отчёт'))
        row = QHBoxLayout()
        for widget in (self.run, self.cancel, self.open_directory, self.open_report):
            row.addWidget(widget)
        layout.addLayout(row)
        self.phase = QLabel('IDLE')
        self.progress = QLabel(self.tr('Завершено сегментов: 0'))
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        for widget in (self.phase, self.progress, self.validation):
            layout.addWidget(widget)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels([self.tr('Сегмент'), self.tr('Статус'), self.tr('Опубликованные файлы'), self.tr('Предупреждения'), self.tr('Ошибки')])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details)

        # Defaults are constructed without reading a user's TOML or touching disk.
        root = self.mock_root or (Path(tempfile.gettempdir()) / 'GNSS-GUI-MOCK' if mock else default_root())
        self.set_settings(api.ConversionSettings(
            executable=Path(sys.executable).resolve() if mock else (root / 'tools/convbin.exe').resolve(),
            runs_root=(root / 'internal/runs').resolve(), daily_root=(root / 'data').resolve(),
            output_root=(root / 'data/rinex').resolve(), version='3.02', systems=tuple('GREC'),
            observations=tuple('CLDS'), groups=GROUPS, masks=tuple(MASKS), obs=True, nav=True,
            receiver_model='', timeout_s=1800, interval_s=1))
        for control in body.findChildren(QLineEdit):
            control.textChanged.connect(self._changed)
        for control in body.findChildren(QCheckBox):
            control.toggled.connect(self._changed)
        self.source_kind.currentIndexChanged.connect(self._changed)
        self.scenario.currentIndexChanged.connect(self._changed)
        self.masks.itemChanged.connect(self._changed)
        self.source_button.clicked.connect(self._browse_source)
        self.toml_browse.clicked.connect(self._browse_toml)
        self.load_button.clicked.connect(self._request_load)
        self.run.clicked.connect(self.run_requested.emit)
        self.cancel.clicked.connect(self.cancel_requested.emit)
        self.open_directory.clicked.connect(lambda: self._open_existing(self._directory, directory=True))
        self.open_report.clicked.connect(lambda: self._open_existing(self._report))
        self._changed()

    def _absolute(self, text):
        path = Path(os.path.expandvars(str(text).strip())).expanduser()
        if not str(text).strip() or not path.is_absolute():
            raise ValueError(self.tr('Укажите непустой абсолютный путь.'))
        return path.resolve()

    def _request_load(self):
        if self._state.get('closing', False):
            return
        try:
            path = self._absolute(self.toml_path.text())
        except (ValueError, OSError, RuntimeError) as error:
            self.validation.setText(str(error))
            return
        self.load_requested.emit(str(path))

    def set_settings(self, settings):
        """Populate from the worker's load_conversion_settings result, without I/O."""
        self._settings = replace(settings)
        self._setting_form = True
        try:
            for name in ('systems', 'groups', 'observations'):
                for value, control in getattr(self, name).items():
                    control.setChecked(value in getattr(settings, name))
            for index in range(self.masks.count()):
                item = self.masks.item(index)
                item.setCheckState(Qt.CheckState.Checked if item.text() in settings.masks else Qt.CheckState.Unchecked)
            for name in ('obs', 'nav', 'allow_obs_without_nav'):
                getattr(self, name).setChecked(getattr(settings, name))
            for name in ('timeout_s', 'interval_s', 'executable', 'output_root', 'runs_root', 'daily_root', 'receiver_model'):
                getattr(self, name).setText(str(getattr(settings, name)))
        finally:
            self._setting_form = False
        if hasattr(self, 'validation'):
            self._changed()

    def conversion_settings(self):
        """Return an immutable snapshot validated by the actual Settings contract."""
        values = {name: self._absolute(getattr(self, name).text())
                  for name in ('executable', 'runs_root', 'daily_root', 'output_root')}
        for name in ('systems', 'groups', 'observations'):
            values[name] = tuple(value for value, control in getattr(self, name).items() if control.isChecked())
        values['masks'] = tuple(self.masks.item(i).text() for i in range(self.masks.count())
                                if self.masks.item(i).checkState() == Qt.CheckState.Checked)
        for name in ('obs', 'nav', 'allow_obs_without_nav'):
            values[name] = getattr(self, name).isChecked()
        for name in ('timeout_s', 'interval_s'):
            values[name] = float(getattr(self, name).text())
        values['receiver_model'] = self.receiver_model.text()
        return replace(self._settings, **values)

    def conversion_request(self):
        settings = self.conversion_settings()
        session = self.source_kind.currentData() == 'session'
        if session and not self.session_id.text().strip():
            raise ValueError(self.tr('Укажите идентификатор сессии run_id'))
        return api.ConversionRequest(
            config=self._absolute(self.toml_path.text()) if self.toml_path.text().strip() else None,
            output=settings.output_root,
            input=None if session else self._absolute(self.source.text()),
            run_id=self.session_id.text().strip() if session else None,
            allow_obs_without_nav=settings.allow_obs_without_nav)

    def _changed(self, *_):
        if self._setting_form:
            return
        self.apply_state(self._state)
        self.form_changed.emit()

    def apply_state(self, state):
        if not self.operational:
            return
        self._state = dict(state)
        busy, closing = state.get('busy', False), state.get('closing', False)
        self.phase.setText(self.tr('Состояние выполнения: {phase}').format(phase=state.get('phase', 'IDLE')))
        reason = ''
        try:
            settings = self.conversion_settings()
            if not settings.executable.is_file():
                reason = self.tr('Файл convbin не найден. Укажите путь к исполняемому файлу.')
            else:
                self.conversion_request()
        except (ValueError, OSError, RuntimeError) as error:
            reason = str(error)
        try:
            self._absolute(self.toml_path.text())
            can_load = True
        except (ValueError, OSError, RuntimeError):
            can_load = False
        self.load_button.setEnabled(can_load and not closing)
        self.validation.setText(reason)
        job_active = state.get('job_active', busy)
        self.run.setEnabled(not busy and not job_active and not closing and not reason)
        cancel_requested = state.get('cancel_requested', state.get('phase') in
                                     ('CANCELLING', 'STOPPING'))
        self.cancel.setEnabled(bool(job_active and not cancel_requested))
        session = self.source_kind.currentData() == 'session'
        self.source_kind.setEnabled(not closing)
        self.scenario.setEnabled(not closing)
        self.session_id.setEnabled(session and not closing)
        self.source.setEnabled(not session and not closing)
        self.source_button.setEnabled(not session and not closing)
        self.toml_browse.setEnabled(not closing)
        for button in (self.save_profile, self.save_profile_as, self.reset_profile):
            button.setEnabled(not closing)
        self.open_directory.setEnabled(bool(self._directory and self._directory.is_dir()))
        self.open_report.setEnabled(bool(self._report and self._report.is_file()))

    def _browse_source(self):
        if self._state.get('closing', False):
            return
        if self.mock:
            self.fixture_requested.emit(self.scenario.currentData())
            return
        if self.source_kind.currentData() == 'directory':
            path = QFileDialog.getExistingDirectory(self, self.tr('Каталог завершённых файлов UBX'))
        else:
            path, _ = QFileDialog.getOpenFileName(self, self.tr('Завершённый файл UBX'), '', 'UBX (*.ubx)')
        if path:
            self.source.setText(path)

    def _browse_toml(self):
        if self._state.get('closing', False):
            return
        path, _ = QFileDialog.getOpenFileName(self, self.tr('Конфигурация конвертации'), '', 'TOML (*.toml)')
        if path:
            self.toml_path.setText(path)

    @staticmethod
    def _open_existing(path, directory=False):
        if path is not None and (path.is_dir() if directory else path.is_file()):
            return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
        return False

    def render_view(self, view):
        """Render present_result output or a raw conversion report, preserving statuses."""
        if not self.operational:
            return
        if 'operation_type' not in view:
            view = api.present_result(view, operation_type='conversion')
        report = view.get('conversion') or {}
        segments = view.get('segments', report.get('segments', []))
        completed = sum(segment.get('status', 'NOT_RUN') != 'NOT_RUN' for segment in segments)
        self.progress.setText(self.tr('Завершено сегментов: {completed} / {total}').format(
            completed=completed, total=len(segments)))
        lines = [self.tr('Операция: {status}').format(status=report.get('status', view.get('lifecycle', ''))),
                 self.tr('Код выхода: {code}').format(code=view.get('exit_code', report.get('exit_code')))]
        self.table.setRowCount(len(segments))
        incomplete = report.get('status') == 'OBS_ONLY_INCOMPLETE_NAVIGATION'
        for row, segment in enumerate(segments):
            outputs = segment.get('published_outputs', [])
            files = list(outputs.values()) if isinstance(outputs, dict) else list(outputs)
            source = segment.get('source', '')
            if isinstance(source, dict):
                source = source.get('path', source)
            status = segment.get('status', 'NOT_RUN')
            incomplete |= status == 'OBS_ONLY_INCOMPLETE_NAVIGATION'
            warnings = list(segment.get('warnings', []))
            if status == 'SKIPPED' and segment.get('skip_reason'):
                warnings.append(self.tr('Причина пропуска: {reason}').format(
                    reason=segment['skip_reason']))
            values = (source, status, '\n'.join(map(str, files)),
                      '\n'.join(map(str, warnings)),
                      '\n'.join(map(str, segment.get('errors', []))))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.table.setItem(row, column, item)
        if report.get('status') == 'SKIPPED':
            lines.append(self.tr('Конвертация пропущена. Готовность RINEX не подтверждена.'))
            if report.get('skip_reason'):
                lines.append(self.tr('Причина пропуска: {reason}').format(reason=report['skip_reason']))
        if incomplete:
            lines.append(self.tr('OBS опубликован; NAV отсутствует. Неполный результат. Код выхода 3.'))
        for label, key in ((self.tr('Предупреждения'), 'warnings'), (self.tr('Ошибки'), 'errors'), (self.tr('Опубликованные файлы'), 'published_files')):
            lines.append(label + ':')
            lines.extend(str(value) for value in view.get(key, []))
        if self.mock:
            lines.insert(0, self.tr('MOCK — синтетические результаты'))
        self.details.setPlainText('\n'.join(lines))
        self._directory = Path(report['output_directory']).resolve() if report.get('output_directory') else None
        self._report = Path(report['report_path']).resolve() if report.get('report_path') else None
        self.apply_state(self._state)
