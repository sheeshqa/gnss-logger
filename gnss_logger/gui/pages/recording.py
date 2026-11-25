import json
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QIntValidator, QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)


class RecordingPage(QWidget):
    def __init__(self, mock=False, parent=None, *, operational=False, mock_root=None):
        super().__init__(parent)
        self.operational = operational
        if operational:
            self._operational_ui(mock, mock_root)
            return
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Запись'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        note = QLabel(self.tr('Stage 4.2: каркас интерфейса. Аппаратная запись ещё не подключена.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        group = QGroupBox(self.tr('Синтетические устройства — без обращения к COM') if mock
                          else self.tr('Приёмник — подключение будет доступно на Stage 4.3'))
        group_layout = QVBoxLayout(group)
        row = QHBoxLayout()
        self.devices = QComboBox()
        self.devices.setObjectName('devices')
        self.devices.setAccessibleName(self.tr('Выбранное синтетическое устройство'))
        row.addWidget(self.devices, 1)
        self.refresh = QPushButton(self.tr('Обновить MOCK') if mock else self.tr('Обновить'))
        row.addWidget(self.refresh)
        group_layout.addLayout(row)
        self.identity = QLabel(self.tr('Модель не подтверждена MON-VER. Реальные порты не сканируются.'))
        self.identity.setWordWrap(True)
        group_layout.addWidget(self.identity)
        layout.addWidget(group)
        demo = QGroupBox(self.tr('Проверка фоновых операций (не запись GNSS)'))
        demo_layout = QVBoxLayout(demo)
        self.scenario = QComboBox()
        self.scenario.addItem(self.tr('Обычное завершение'), 'normal')
        self.scenario.addItem(self.tr('Синтетическая ошибка'), 'error')
        demo_layout.addWidget(self.scenario)
        buttons = QHBoxLayout()
        self.start = QPushButton(self.tr('Начать демонстрацию'))
        self.start.setObjectName('startDemo')
        self.stop = QPushButton(self.tr('Остановить'))
        self.stop.setObjectName('stopDemo')
        buttons.addWidget(self.start)
        buttons.addWidget(self.stop)
        demo_layout.addLayout(buttons)
        self.phase = QLabel(self.tr('Состояние: ожидание'))
        self.steps = QLabel(self.tr('Шаги демонстрации: —'))
        self.quality = QLabel(self.tr('RAWX / SFRBX / байты / целостность: не измеряются'))
        self.quality.setWordWrap(True)
        demo_layout.addWidget(self.phase)
        demo_layout.addWidget(self.steps)
        demo_layout.addWidget(self.quality)
        layout.addWidget(demo)
        layout.addStretch()

    def apply_state(self, state):
        if self.operational:
            self._apply_operational_state(state)
            return
        idle = not state['busy'] and not state['closing']
        self.devices.setEnabled(idle and state['mock'])
        self.refresh.setEnabled(idle and state['mock'])
        self.scenario.setEnabled(idle and state['mock'])
        self.start.setEnabled(idle and state['mock'] and bool(state['selected_device']))
        self.stop.setEnabled(state['busy'] and not state['closing'] and
                             state['phase'] not in ('DISCOVERING', 'STOPPING', 'FINALIZING', 'FINISHED', 'FAILED'))
        self.phase.setText(self.tr('Состояние: {phase}').format(phase=state['phase']))

    def _operational_ui(self, mock, mock_root):
        root = (Path(mock_root) if mock_root is not None else
                Path(tempfile.gettempdir()) / 'GNSS-GUI-MOCK' / uuid4().hex) if mock else (
                    Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'GNSS')
        root = root.resolve()
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Запись — MOCK' if mock else 'Запись'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        receiver = QGroupBox(self.tr('Приёмник'))
        receiver_layout = QVBoxLayout(receiver)
        row = QHBoxLayout()
        self.devices = QComboBox()
        self.devices.setObjectName('devices')
        self.refresh = QPushButton(self.tr('Обновить'))
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems(['9600', '38400', '115200', '230400', '460800', '921600'])
        self.baud.setCurrentText('115200')
        self.baud.lineEdit().setValidator(QIntValidator(1, 2147483647, self.baud))
        for widget in (self.devices, self.refresh, QLabel(self.tr('Скорость порта')), self.baud):
            row.addWidget(widget)
        receiver_layout.addLayout(row)
        self.identity = QLabel(self.tr('Приёмник ещё не определён.'))
        self.identity.setWordWrap(True)
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        receiver_layout.addWidget(self.identity)
        actions = QHBoxLayout()
        self.identify = QPushButton(self.tr('Определить приёмник (5 с)'))
        self.diagnose = QPushButton(self.tr('Диагностика (10 с)'))
        actions.addWidget(self.identify)
        actions.addWidget(self.diagnose)
        receiver_layout.addLayout(actions)
        layout.addWidget(receiver)
        self.parameters = QGroupBox(self.tr('Параметры записи'))
        form = QFormLayout(self.parameters)
        self.output = QLineEdit(str(root / 'internal' / 'runs'))
        self.daily_root = QLineEdit(str(root / 'data'))
        self.daily_enabled = QCheckBox(self.tr('Сохранять суточную проекцию'))
        self.output_browse = QPushButton(self.tr('Выбрать папку записи…'))
        self.daily_browse = QPushButton(self.tr('Выбрать папку суточных данных…'))
        for label, edit, button in ((self.tr('Папка записи'), self.output, self.output_browse),
                                    (self.tr('Папка суточных данных'), self.daily_root, self.daily_browse)):
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addWidget(button)
            form.addRow(label, row)
            button.clicked.connect(lambda checked=False, field=edit: self._browse(field))
        form.addRow(self.daily_enabled)
        self.duration = QDoubleSpinBox()
        self.duration.setRange(0.001, 1000000)
        self.duration.setDecimals(3)
        self.duration.setValue(1)
        self.duration_unit = QComboBox()
        for label, factor in (('Минуты', 60), ('Часы', 3600), ('Сутки', 86400), ('До ручной остановки', None)):
            self.duration_unit.addItem(self.tr(label), factor)
        row = QHBoxLayout()
        row.addWidget(self.duration)
        row.addWidget(self.duration_unit)
        form.addRow(self.tr('Длительность'), row)
        self.station = QLineEdit('MOCK' if mock else 'GNSS')
        self.expected_hz = QDoubleSpinBox()
        self.expected_hz.setRange(0.001, 10000)
        self.expected_hz.setDecimals(3)
        self.expected_hz.setValue(1)
        form.addRow(self.tr('Станция'), self.station)
        form.addRow(self.tr('Ожидаемая частота, Гц'), self.expected_hz)
        layout.addWidget(self.parameters)
        self.auto_box = QGroupBox(self.tr('Автоматическая конвертация'))
        auto_layout = QVBoxLayout(self.auto_box)
        self.auto_configured = QCheckBox(self.tr('Подготовить Auto по текущему профилю страницы «Конвертация» (до Start)'))
        self.auto_enabled = QCheckBox(self.tr('Auto On — принимать новые завершённые сегменты'))
        self.cancel_auto = QPushButton(self.tr('Cancel current auto — отменить только текущее задание'))
        self.auto_status = QLabel(self.tr('Auto не настроен. Нужны профиль и суточная проекция.'))
        self.auto_status.setWordWrap(True)
        self.auto_status.setTextFormat(Qt.TextFormat.PlainText)
        self.auto_details = QLabel()
        self.auto_details.setWordWrap(True)
        self.auto_details.setTextFormat(Qt.TextFormat.PlainText)
        self.auto_open_report = QPushButton(self.tr('Открыть последний отчёт Auto'))
        self.auto_refresh_report = QPushButton(self.tr('Повторить чтение последнего отчёта Auto'))
        self.auto_open_report.setEnabled(False)
        self._auto_report = None
        self.auto_open_report.clicked.connect(self._open_auto_report)
        for widget in (self.auto_configured, self.auto_enabled, self.cancel_auto, self.auto_status):
            auto_layout.addWidget(widget)
        auto_layout.addWidget(self.auto_details)
        auto_layout.addWidget(self.auto_open_report)
        auto_layout.addWidget(self.auto_refresh_report)
        layout.addWidget(self.auto_box)
        row = QHBoxLayout()
        self.start_record = QPushButton(self.tr('Начать запись'))
        self.stop_record = QPushButton(self.tr('Остановить запись'))
        for name in ('identify', 'diagnose', 'start_record', 'stop_record'):
            getattr(self, name).setObjectName(name)
        row.addWidget(self.start_record)
        row.addWidget(self.stop_record)
        layout.addLayout(row)
        self.phase = QLabel()
        self.quality = QLabel(self.tr('Снимок состояния записи ещё не получен.'))
        self.quality.setTextFormat(Qt.TextFormat.PlainText)
        self.quality.setWordWrap(True)
        self.snapshot_age = QLabel(self.tr('Возраст снимка: нет данных'))
        self.elapsed = QLabel(self.tr('Прошло по таймеру интерфейса: —'))
        for widget in (self.phase, self.quality, self.snapshot_age, self.elapsed):
            layout.addWidget(widget)
        layout.addStretch()
        self._started_at = self._elapsed_end = self._snapshot_at = None
        self._snapshot_utc = None
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(500)
        self._clock_timer.timeout.connect(self._update_clocks)
        self._clock_timer.start()
        self.duration_unit.currentIndexChanged.connect(self._duration_changed)

    def _duration_changed(self):
        self.duration.setEnabled(self.duration_unit.currentData() is not None)

    def _browse(self, field):
        folder = QFileDialog.getExistingDirectory(self, self.tr('Выберите папку'), field.text())
        if folder:
            field.setText(folder)

    def record_parameters(self):
        def absolute(field):
            value = field.text().strip()
            if not value or not Path(value).expanduser().is_absolute():
                raise ValueError(self.tr('Укажите непустые абсолютные пути папок записи'))
            return Path(value).expanduser().resolve()
        station = self.station.text().strip()
        if not station:
            raise ValueError(self.tr('Укажите станцию'))
        factor = self.duration_unit.currentData()
        return dict(output=absolute(self.output),
                    daily_root=absolute(self.daily_root) if self.daily_enabled.isChecked() else None,
                    seconds=None if factor is None else float(self.duration.value() * factor),
                    station=station, expected_hz=float(self.expected_hz.value()))

    def recording_started(self):
        self._started_at = time.monotonic()
        self._elapsed_end = self._snapshot_at = None
        self._snapshot_utc = None
        self.quality.setText(self.tr('Ожидание снимка состояния записи.'))
        self._update_clocks()

    def recording_finished(self):
        self._elapsed_end = time.monotonic()
        self._update_clocks()

    def _update_clocks(self):
        if self._started_at is not None:
            end = self._elapsed_end if self._elapsed_end is not None else time.monotonic()
            self.elapsed.setText(self.tr('Прошло по таймеру интерфейса: {seconds:.1f} с').format(
                seconds=max(0, end - self._started_at)))
        if self._snapshot_at is not None:
            age = self.tr('нет данных')
            if self._snapshot_utc:
                try:
                    stamp = datetime.fromisoformat(self._snapshot_utc.replace('Z', '+00:00'))
                    if stamp.tzinfo is not None:
                        age = self.tr('{seconds:.1f} с').format(
                            seconds=max(0, (datetime.now(timezone.utc) - stamp).total_seconds()))
                except (ValueError, TypeError):
                    pass
            self.snapshot_age.setText(self.tr('UTC снимка: {utc} | возраст: {age} | с получения: {seconds:.1f} с').format(
                utc=self._snapshot_utc or self.tr('нет данных'), age=age,
                seconds=time.monotonic() - self._snapshot_at))
        else:
            self.snapshot_age.setText(self.tr('Возраст снимка: нет данных'))

    def _apply_operational_state(self, state):
        idle = not state['busy'] and not state['closing']
        for widget in (self.devices, self.refresh, self.baud, self.parameters):
            widget.setEnabled(idle)
        self.duration.setEnabled(idle and self.duration_unit.currentData() is not None)
        self.identify.setEnabled(idle and bool(state['selected_device']))
        self.diagnose.setEnabled(idle and bool(state['selected_device']))
        self.start_record.setEnabled(idle and bool(state.get('can_start')))
        self.stop_record.setEnabled(bool(state.get('recording_active')) and not state['closing']
                                    and state['phase'] not in ('STOPPING', 'FINALIZING', 'FINISHED', 'FAILED'))
        self.phase.setText(self.tr('Этап: {phase}').format(phase=state['phase']))
        self.auto_configured.setEnabled(idle)
        active = state.get('recording_active', False)
        capable = state.get('auto_capable', False) if active else self.auto_configured.isChecked()
        self.auto_enabled.setEnabled(not state['closing'] and capable)
        if active:
            self.auto_enabled.blockSignals(True)
            self.auto_enabled.setChecked(state.get('auto_enabled', False))
            self.auto_enabled.blockSignals(False)
        automatic = state.get('auto_conversion') or {}
        self.auto_refresh_report.setEnabled(not state['closing'] and bool(automatic.get('last_report')))
        self.cancel_auto.setEnabled(active and capable and bool(automatic.get('active')))
        self.auto_status.setText(self.tr('Auto: ') + json.dumps(automatic or {
            'configured': capable, 'enabled': state.get('auto_enabled') if active else self.auto_enabled.isChecked(),
            'note': 'Off не отменяет принятую очередь; Cancel не останавливает UBX'}, ensure_ascii=False, default=str))
        self.devices.blockSignals(True)
        self.devices.setCurrentIndex(self.devices.findData(state['selected_device']))
        self.devices.blockSignals(False)
        if 'baud' in state:
            self.baud.blockSignals(True)
            self.baud.setCurrentText(str(state['baud']))
            self.baud.blockSignals(False)

    def render_identity(self, payload):
        identity = payload.get('identity') or {}
        binding = payload.get('binding') or {}
        self.identity.setText(self.tr('MON-VER: модель / прошивка / протокол: ') +
                              json.dumps(identity, ensure_ascii=False, default=str) +
                              '\n' + self.tr('Привязка USB: ') + json.dumps(binding, ensure_ascii=False, default=str) +
                              '\n' + self.tr('Идентификация и метаданные USB из диагностики: ') +
                              json.dumps({key: value for key, value in (payload.get('report') or {}).items()
                                          if key in ('identity', 'usb_identity', 'binding', 'usb', 'adapter', 'errors', 'warnings', 'exit_code')},
                                         ensure_ascii=False, default=str))

    def render_auto_result(self, view):
        report = view.get('conversion') or {}
        self.auto_details.setText(self.tr('Последний результат Auto: ') + json.dumps(dict(
            status=report.get('status'), exit_code=view.get('exit_code'),
            published_files=view.get('published_files'),
            segments=[{key: row.get(key) for key in ('status', 'warnings', 'errors', 'skip_reason')}
                      for row in view.get('segments', [])],
            warnings=view.get('warnings'), errors=view.get('errors')), ensure_ascii=False, default=str))
        self._auto_report = Path(report['report_path']) if report.get('report_path') else None
        self.auto_open_report.setEnabled(bool(self._auto_report and self._auto_report.is_file()))

    def _open_auto_report(self):
        if self._auto_report is not None and self._auto_report.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._auto_report.resolve())))

    def render_snapshot(self, report):
        """Render published counters verbatim; never decode or infer observations."""
        ext = report.get('extensions') or {}
        diag = ext.get('gnss_logger.diagnostics') or {}
        detail = ext.get('gnss_logger.recording') or {}
        account = report.get('accounting') or {}
        missing = self.tr('нет данных')
        rawx = diag.get('RAWX') or {}
        nav = diag.get('SFRBX') or {}
        timing = diag.get('timing') or {}
        values = {
            self.tr('Получено байт'): account.get('received', missing),
            self.tr('Учёт байтов'): account or missing,
            self.tr('Эпохи RAWX'): rawx.get('epochs', missing),
            self.tr('Сообщения SFRBX'): nav.get('messages', missing),
            self.tr('Ошибки декодирования'): diag.get('decode_errors', missing),
            self.tr('Ошибки контрольной суммы'): diag.get('checksum_errors', missing),
            self.tr('Разрывы'): report.get('gap_events', timing.get('gap_events', missing)),
            self.tr('Регистрация'): report.get('registration', missing),
            self.tr('Причина остановки'): detail.get('stop_reason', missing),
            self.tr('Код завершения'): detail.get('exit_code', missing),
            self.tr('Ошибки записи'): detail.get('errors', missing),
            self.tr('Файлы'): report.get('artifacts', missing),
            self.tr('Целостность'): report.get('integrity', detail.get('integrity', missing)),
            self.tr('Качество фазы'): report.get('phase_quality', missing),
            self.tr('UTC последних данных'): report.get('last_data_utc', missing),
            self.tr('UTC последнего RAWX'): report.get('last_rawx_utc', missing),
            self.tr('UTC последнего SFRBX'): report.get('last_sfrbx_utc', missing),
            self.tr('Пути'): report.get('paths', detail.get('paths', report.get('output_directory', missing))),
        }
        self.quality.setText('\n'.join(f'{key}: {json.dumps(value, ensure_ascii=False, default=str)}'
                                       for key, value in values.items()))
        self._snapshot_at = time.monotonic()
        self._snapshot_utc = report.get('utc')
        self._update_clocks()
