"""Four-page Windows shell. All widget mutation happens in Qt's GUI thread."""
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from PySide6.QtCore import QThread, Qt, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QMainWindow,
                               QMessageBox, QPlainTextEdit, QScrollArea,
                               QStackedWidget, QVBoxLayout, QWidget)

from .coordinator import Coordinator
from .pages.conversion import ConversionPage
from .pages.history import HistoryPage
from .pages.recording import RecordingPage
from .pages.settings import SettingsPage
from .. import __version__
from .presentation import STYLE, application_icon, polish_page, status_text


class MainWindow(QMainWindow):
    def __init__(self, provider=None, *, operational=False, conversion_backend=None, mock_root=None, preferences_path=None):
        super().__init__()
        self.operational = operational
        self.setWindowTitle(self.tr('GNSS Logger — Stage 4.2') + (' — MOCK' if provider else ''))
        self.resize(1100, 750)
        self.setMinimumSize(760, 540)
        self.setWindowIcon(application_icon())
        if operational:
            from .receiver_coordinator import ReceiverCoordinator
            self.coordinator = ReceiverCoordinator(provider, self)
            self.setWindowTitle(self.tr('GNSS Logger — Этап 4.4') + (' — MOCK' if provider.is_mock else ''))
        else:
            self.coordinator = Coordinator(provider, self)
        self.setWindowTitle('GNSS Logger ' + __version__ + (' — MOCK' if
            (provider.is_mock if operational else provider is not None) else ''))
        self.last_events = []
        self.latest_record_report = None
        self.result_received = False
        self.delivery_on_ui_thread = True
        self._close_requested = False
        self._history_recording_roots = set()
        self._history_conversion_roots = set()
        central = QWidget()
        outer = QVBoxLayout(central)
        banner = QLabel(self.tr('MOCK — только демонстрация интерфейса. Нет аппаратной записи и RINEX.')
                        if provider else self.tr('Каркас GUI: работа с оборудованием ещё не подключена.'))
        banner.setObjectName('modeBanner')
        if operational:
            banner.setText(self.tr('MOCK — запись синтетического приёмника') if provider.is_mock
                           else self.tr('Запись приёмника — выберите порт и определите приёмник'))
        banner.setWordWrap(True)
        outer.addWidget(banner)
        content = QHBoxLayout()
        self.sidebar = QListWidget()
        self.sidebar.setObjectName('navigation')
        self.sidebar.setAccessibleName(self.tr('Разделы приложения'))
        self.sidebar.addItems([self.tr('Запись'), self.tr('Конвертация'),
                               self.tr('История'), self.tr('Настройки')])
        self.sidebar.setMaximumWidth(190)
        content.addWidget(self.sidebar)
        self.stack = QStackedWidget()
        backend_root = getattr(conversion_backend, 'mock_root', None)
        if mock_root is not None and backend_root is not None and Path(mock_root).resolve() != Path(backend_root).resolve():
            raise ValueError('Mock backend and window roots differ')
        self.mock_root = (Path(mock_root or backend_root or (Path(tempfile.gettempdir()) / 'GNSS-GUI-MOCK' / uuid4().hex)).resolve()
                          if operational and provider.is_mock else None)
        if mock_root is not None and self.mock_root is None:
            raise ValueError('mock_root requires a mock provider')
        if self.mock_root is not None and preferences_path is not None and not Path(preferences_path).resolve().is_relative_to(self.mock_root):
            raise ValueError('Mock preferences must be inside mock_root')
        self.recording = (RecordingPage(mock=provider.is_mock, operational=True, mock_root=self.mock_root)
                          if operational else RecordingPage(provider is not None))
        self.conversion = (ConversionPage(operational=True, mock=provider.is_mock, mock_root=self.mock_root)
                           if operational else ConversionPage())
        self.history = HistoryPage(operational=operational)
        self.settings = SettingsPage(operational=operational)
        for page in (self.recording, self.conversion, self.history, self.settings):
            polish_page(page)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        content.addWidget(self.stack, 1)
        outer.addLayout(content, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setMaximumHeight(130)
        self.log.setAccessibleName(self.tr('События интерфейса'))
        outer.addWidget(self.log)
        self.setCentralWidget(central)
        self.setStyleSheet(STYLE)
        self.persistent_status = QLabel()
        self.persistent_status.setObjectName('persistentStatus')
        self.persistent_status.setWordWrap(True)
        self.persistent_status.setTextFormat(Qt.TextFormat.PlainText)
        self.persistent_status.setAccessibleName(self.tr('Состояние операций'))
        self.statusBar().addPermanentWidget(self.persistent_status, 1)
        self.version_status = QLabel(__version__)
        self.statusBar().addPermanentWidget(self.version_status)
        navigation = self.menuBar().addMenu(self.tr('Разделы'))
        for index in range(4):
            action = QAction(self.sidebar.item(index).text(), self)
            action.setShortcut(QKeySequence('Ctrl+' + str(index + 1)))
            action.triggered.connect(lambda checked=False, row=index: self.sidebar.setCurrentRow(row))
            navigation.addAction(action)
        help_menu = self.menuBar().addMenu(self.tr('Справка'))
        self.about_action = QAction(self.tr('О программе'), self)
        self.about_action.setShortcut(QKeySequence('F1'))
        self.about_action.triggered.connect(self._about)
        help_menu.addAction(self.about_action)
        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.setCurrentRow(0)
        self.recording.refresh.clicked.connect(self.coordinator.refresh_devices)
        if operational:
            from .conversion_coordinator import ConversionCoordinator
            from .conversions import ConversionBackend, MockConversionBackend
            self.conversion_backend = conversion_backend or (
                MockConversionBackend(self.mock_root) if provider.is_mock else ConversionBackend())
            if provider.is_mock and hasattr(self.conversion_backend, 'worker_factory'):
                provider.conversion_worker_factory = self.conversion_backend.worker_factory
                provider.mock_root = self.conversion_backend.mock_root
            self.conversion_coordinator = ConversionCoordinator(self.conversion_backend, self)
            self.conversion_coordinator.settings_loaded.connect(self.conversion.set_settings)
            self.conversion_coordinator.error_raised.connect(self._error)
            self.conversion_coordinator.state_changed.connect(self.conversion.apply_state)
            self.conversion_coordinator.state_changed.connect(lambda _: self._update_status())
            self.conversion_coordinator.events_ready.connect(self._conversion_events)
            self.conversion_coordinator.result_ready.connect(self.conversion.render_view)
            self.conversion_coordinator.result_ready.connect(lambda _: self._refresh_history())
            self.conversion_coordinator.ready_to_close.connect(self._maybe_close, Qt.ConnectionType.QueuedConnection)
            self.conversion.load_requested.connect(self.conversion_coordinator.load)
            self.conversion.run_requested.connect(self._run_conversion)
            self.conversion.cancel_requested.connect(self.conversion_coordinator.cancel)
            self.conversion.fixture_requested.connect(self._mock_fixture)
            self.recording.auto_configured.toggled.connect(lambda: self._state(self.coordinator.view()))
            self.recording.auto_enabled.toggled.connect(self._auto_toggled)
            self.recording.cancel_auto.clicked.connect(self.coordinator.cancel_conversion)
            self.recording.auto_refresh_report.clicked.connect(self.coordinator.refresh_auto_report)
            self.recording.identify.clicked.connect(lambda: self.coordinator.identify(seconds=5))
            self.recording.diagnose.clicked.connect(lambda: self.coordinator.diagnose(seconds=10))
            self.recording.start_record.clicked.connect(self._start_recording)
            self.recording.stop_record.clicked.connect(self.coordinator.stop_recording)
            self.recording.baud.lineEdit().editingFinished.connect(self._baud_changed)
            self.recording.baud.activated.connect(self._baud_changed)
            self.coordinator.identity_changed.connect(self.recording.render_identity)
            self.coordinator.record_result.connect(self._record_result)
            self.coordinator.auto_result.connect(self.recording.render_auto_result)
            self.coordinator.auto_result.connect(lambda _: self._refresh_history())
            from .history_coordinator import HistoryCoordinator
            from .settings_bridge import SettingsBridge
            from ..conversion.config import default_root
            self.history_coordinator = HistoryCoordinator(self)
            self.history.refresh_requested.connect(self.history_coordinator.refresh)
            self.history.selection_requested.connect(self.history_coordinator.select)
            self.history.conversion_requested.connect(self._history_conversion)
            self.history_coordinator.snapshot_ready.connect(self.history.render_snapshot)
            self.history_coordinator.detail_ready.connect(self.history.render_detail)
            self.history_coordinator.state_changed.connect(self.history.apply_state)
            self.history_coordinator.error_raised.connect(self._error)
            self.history_coordinator.ready_to_close.connect(self._maybe_close, Qt.ConnectionType.QueuedConnection)
            path = Path(preferences_path) if preferences_path is not None else (self.mock_root or default_root()) / 'config' / 'gui-preferences.json'
            self.settings_bridge = SettingsBridge(self, path)
            self.settings_bridge.ready_to_close.connect(self._maybe_close, Qt.ConnectionType.QueuedConnection)
            self.history_coordinator.configure_roots(*[self.settings_bridge.defaults['history'][key] for key in ('recording_roots', 'conversion_roots', 'stage1_roots')])
        else:
            self.recording.start.clicked.connect(self._start_demo)
            self.recording.stop.clicked.connect(self.coordinator.stop_demo)
        self.recording.devices.currentIndexChanged.connect(self._selected)
        self.settings.polling.valueChanged.connect(self._polling_changed)
        self.coordinator.devices_changed.connect(self._devices)
        self.coordinator.state_changed.connect(self._state)
        self.coordinator.events_ready.connect(self._events)
        self.coordinator.error_raised.connect(self._error)
        self.coordinator.ready_to_close.connect(self._maybe_close if operational else self.close,
                                                Qt.ConnectionType.QueuedConnection)
        self._state(self.coordinator.view())
        if operational:
            self.conversion_coordinator.load()

    @Slot()
    def _about(self):
        QMessageBox.about(self, self.tr('О программе GNSS Logger'),
            self.tr('GNSS Logger {version}\nЗапись GNSS и конвертация UBX → RINEX 3.02.\n'
                    'Ctrl+1…4 — разделы; Tab / Shift+Tab — переход по элементам.\n'
                    'MOCK использует синтетические данные.').format(version=__version__))

    def _update_status(self, state=None):
        state = self.coordinator.view() if state is None else state
        manual = self.conversion_coordinator.view() if hasattr(self, 'conversion_coordinator') else None
        text = status_text(state, manual)
        self.persistent_status.setText(text)
        self.persistent_status.setToolTip(text)

    @Slot()
    def _maybe_close(self):
        if not self._close_requested and self.coordinator.shutdown_complete:
            self.close()
            return
        if self._close_requested and all(c.shutdown_complete for c in (self.coordinator, self.conversion_coordinator, self.history_coordinator, self.settings_bridge)):
            self.close()

    @Slot(object)
    def _history_conversion(self, detail):
        if not detail.get('terminal'):
            self._error(self.tr('Выберите завершённую сессию.'))
            return
        if detail.get('stage') == 2 and detail.get('run_id'):
            key = Path(detail['session_key'])
            matches = [row for row in self.history._sessions if row.get('run_id') == detail['run_id']]
            if key.parent.name != 'attempts' or len(matches) != 1:
                self._error(self.tr('Для run_id найдено несколько попыток; выберите завершённый UBX вручную.'))
                return
            self.conversion.runs_root.setText(str(key.parent.parent.parent))
            self.conversion.source_kind.setCurrentIndex(self.conversion.source_kind.findData('session'))
            self.conversion.session_id.setText(detail['run_id'])
            self.sidebar.setCurrentRow(1)
            return
        files = [row.get('path') for row in detail.get('ubx_files', []) if row.get('path')]
        if len(files) != 1:
            self._error(self.tr('Для выбранной сессии нужен один завершённый UBX; выберите источник вручную.'))
            return
        self.conversion.source_kind.setCurrentIndex(self.conversion.source_kind.findData('file'))
        self.conversion.source.setText(files[0])
        self.sidebar.setCurrentRow(1)

    def _refresh_history(self):
        roots = self.settings_bridge.values['history']
        self.history_coordinator.configure_roots(
            list(dict.fromkeys(roots['recording_roots'] + sorted(self._history_recording_roots))),
            list(dict.fromkeys(roots['conversion_roots'] + sorted(self._history_conversion_roots))),
            roots['stage1_roots'])

    @Slot(bool)
    def _auto_toggled(self, enabled):
        if self.coordinator.view()['recording_active']:
            if not self.coordinator.set_auto_conversion(enabled):
                self._error(self.tr('Auto не был настроен до Start; доступен со следующей записи'))

    @Slot(str)
    def _mock_fixture(self, scenario):
        if not self.conversion_backend.is_mock:
            return
        try:
            path = self.conversion_backend.make_fixture(scenario)
            self.conversion.source_kind.setCurrentIndex(self.conversion.source_kind.findData('file'))
            self.conversion.source.setText(str(path))
        except (OSError, ValueError) as error:
            self._error(str(error))

    @Slot(object)
    def _conversion_events(self, events):
        self.delivery_on_ui_thread &= QThread.currentThread() == self.thread()
        for event in events:
            self.log.appendPlainText(json.dumps(event, ensure_ascii=False, default=str)[:16000])
            data = event['data']
            if event['kind'] == 'result':
                self.conversion.render_view(data)
            elif isinstance(data.get('progress'), dict):
                from ..application import present_result
                self.conversion.render_view(present_result(data['progress'], 'conversion', lifecycle=event['lifecycle']))

    @Slot()
    def _run_conversion(self):
        try:
            settings = self.conversion.conversion_settings()
            request = self.conversion.conversion_request()
            if self.conversion_backend.is_mock:
                self.conversion_backend.scenario = self.conversion.scenario.currentData()
            active = self.coordinator._handle
            if active is not None:
                if request.run_id == active.controller.run_id:
                    raise ValueError(self.tr('Выбрана активная сессия; дождитесь её завершения'))
                if request.input is not None:
                    source = request.input.resolve()
                    roots = [active.settings.output, active.settings.daily_root]
                    if any(root is not None and (source.is_relative_to(root.resolve()) or root.resolve().is_relative_to(source))
                           for root in roots):
                        raise ValueError(self.tr('Источник пересекается с каталогом активной записи; выберите архив'))
                if self.coordinator.view()['auto_capable']:
                    self._error(self.tr('Внимание: ручная и Auto могут повторно конвертировать один источник; каталоги изолированы'))
            if self.conversion_coordinator.start(request, settings):
                self._history_conversion_roots.add(str(settings.output_root))
                self.sidebar.setCurrentRow(1)
        except (ValueError, OSError, TypeError) as error:
            self._error(str(error))

    def _baud_changed(self, *args):
        try:
            text = self.recording.baud.currentText().strip()
            if not text.isascii() or not text.isdecimal() or int(text) <= 0:
                raise ValueError(self.tr('Скорость порта должна быть положительным целым числом'))
            return self.coordinator.set_baud(int(text))
        except ValueError as error:
            self.coordinator.set_baud(0)  # Invalid input must revoke the verified binding.
            self._error(str(error))
            return False

    @Slot()
    def _start_recording(self):
        if not self._baud_changed():
            return
        if not self.coordinator.view()['can_start']:
            self._error(self.tr('Перед записью повторно определите приёмник с выбранной скоростью порта'))
            return
        try:
            parameters = self.recording.record_parameters()
            if self.recording.auto_configured.isChecked():
                if parameters['daily_root'] is None:
                    raise ValueError(self.tr('Для Auto включите суточную проекцию'))
                settings = self.conversion.conversion_settings()
                if self.conversion_backend.is_mock:
                    self.conversion_backend.scenario = self.conversion.scenario.currentData()
                if not settings.executable.is_file():
                    raise ValueError(self.tr('Для Auto нужен существующий convbin'))
                self.coordinator.configure_auto(settings, self.recording.auto_enabled.isChecked())
            else:
                self.coordinator.configure_auto()
            self.result_received = False
            if self.coordinator.start_recording(parameters):
                self._history_recording_roots.update(str(p) for p in (parameters['output'], parameters['daily_root']) if p is not None)
                if self.recording.auto_configured.isChecked():
                    self._history_conversion_roots.add(str(settings.output_root))
                self.recording.recording_started()
        except (ValueError, OSError) as error:
            self._error(str(error))

    @Slot(object)
    def _record_result(self, report):
        self.delivery_on_ui_thread &= QThread.currentThread() == self.thread()
        self.latest_record_report = report
        self.result_received = True
        self.recording.render_snapshot(report)
        self.recording.recording_finished()
        self._refresh_history()

    @Slot()
    def _start_demo(self):
        self.result_received = False
        self.coordinator.start_demo(self.recording.scenario.currentData())

    @Slot(int)
    def _selected(self, index):
        self.coordinator.select_device(self.recording.devices.itemData(index))

    @Slot(int)
    def _polling_changed(self, value):
        self.coordinator.interval_ms = value

    @Slot(object)
    def _devices(self, rows):
        combo = self.recording.devices
        combo.blockSignals(True)
        combo.clear()
        for row in rows:
            combo.addItem(str(row.get('description') or row['device']) + ' / ' + row['device'], row['device'])
        combo.setCurrentIndex(combo.findData(self.coordinator.selected_device))
        combo.blockSignals(False)

    @Slot(object)
    def _state(self, state):
        self.recording.apply_state(state)
        self._update_status(state)
        if self.operational:
            self.settings_bridge.apply_recording_if_idle()
            self.settings.polling.setEnabled(not state['busy'] and not state['closing'])
            return
        self.settings.polling.setEnabled(not state['busy'] and not state['closing'])

    @Slot(object)
    def _events(self, events):
        self.delivery_on_ui_thread &= QThread.currentThread() == self.thread()
        self.last_events = events  # bounded current batch, not an infinite event archive
        for event in events:
            self.log.appendPlainText(json.dumps(event, ensure_ascii=False, default=str)[:16000])
            if event['kind'] == 'result':
                self.result_received = True
            if self.operational:
                data = event.get('data') or {}
                if event.get('operation_type') == 'recording' and event['kind'] in ('state', 'progress'):
                    snapshot = data.get('recording')
                    if isinstance(snapshot, dict):
                        self.recording.render_snapshot(snapshot)
                continue
            steps = event['data'].get('demo_steps')
            if steps is not None:
                self.recording.steps.setText(self.tr('Шаги демонстрации: {steps}').format(steps=steps))
        self.log.ensureCursorVisible()

    @Slot(str)
    def _error(self, message):
        self.log.appendPlainText(self.tr('Ошибка: ') + message)

    def closeEvent(self, event):
        complete = self.coordinator.shutdown_complete
        if self.operational:
            complete = complete and all(c.shutdown_complete for c in (self.conversion_coordinator, self.history_coordinator, self.settings_bridge))
        if complete:
            event.accept()
            return
        event.ignore()
        if self._close_requested:
            return
        if self.operational:
            if not self.settings_bridge.confirm_close():
                return
            cancel_manual = False
            if self.coordinator.busy or self.conversion_coordinator.busy:
                answer = QMessageBox.question(
                    self, self.tr('Завершить операции и выйти?'),
                    self.tr('Да — остановить запись, отменить ручную конвертацию и дождаться Auto-очереди. '
                            'Нет — остановить запись, дождаться ручной конвертации и Auto-очереди. '
                            'Отмена — остаться. Опубликованные файлы сохраняются.'),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel)
                if answer == QMessageBox.StandardButton.Cancel:
                    return
                cancel_manual = answer == QMessageBox.StandardButton.Yes
            self._close_requested = True
            self.conversion_coordinator.request_shutdown(cancel=cancel_manual)
            self.coordinator.request_shutdown()
            self.history_coordinator.request_shutdown()
            self.settings_bridge.request_shutdown()
            return
        if self.coordinator.busy:
            if self.operational:
                answer = QMessageBox.question(
                    self, self.tr('Закрыть GNSS Logger'),
                    self.tr('Остановить запись и дождаться сохранения данных и завершения фоновых задач?'),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
                self._close_requested = True
                self.statusBar().showMessage(self.tr('Остановка записи; ожидание сохранения данных…'))
                self.coordinator.request_shutdown()
                return
            answer = QMessageBox.question(
                self, self.tr('Завершение приложения'),
                self.tr('Остановить демонстрацию и дождаться завершения фоновых задач?'),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._close_requested = True
        self.coordinator.request_shutdown()
