from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget, QVBoxLayout, QWidget


class _LegacyHistoryPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('История'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        text = QLabel(self.tr('Read-only HistoryService будет подключён на Stage 4.5. '
                              'На этом этапе каталоги наблюдений не сканируются.'))
        text.setWordWrap(True)
        layout.addWidget(text)
        refresh = QPushButton(self.tr('Обновить историю'))
        refresh.setEnabled(False)
        layout.addWidget(refresh)
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels([self.tr('Дата'), self.tr('Приёмник'), self.tr('UBX'),
                                        self.tr('Целостность'), self.tr('Конвертация')])
        layout.addWidget(table)
        note = QLabel(self.tr('Сохранённая оценка целостности не является новой проверкой файлов.'))
        note.setWordWrap(True)
        layout.addWidget(note)


from copy import deepcopy
import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QHBoxLayout,
                               QLineEdit, QPlainTextEdit, QTableWidgetItem)
from ...application.results import present_result


NO_DATA = 'Нет данных'


def display(value):
    if value is None or value == '':
        return NO_DATA
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def station(session):
    manifest = session.get('manifest') or {}
    report = session.get('report') or {}
    return (manifest.get('parameters') or report.get('parameters') or {}).get('station')


class HistoryPage(_LegacyHistoryPage):
    refresh_requested = Signal()
    selection_requested = Signal(str)
    conversion_requested = Signal(object)

    def __init__(self, parent=None, operational=False):
        self.operational = operational
        self._sessions = []
        self._conversions = []
        self._detail = None
        self._closing = False
        if not operational:
            super().__init__(parent)
            return
        QWidget.__init__(self, parent)
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('История'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        self.refresh_button = QPushButton(self.tr('Обновить историю'))
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        layout.addWidget(self.refresh_button)
        filters = QHBoxLayout()
        self.date_from = QLineEdit()
        self.date_to = QLineEdit()
        self.search = QLineEdit()
        self.model_filter = QComboBox()
        self.status_filter = QComboBox()
        for widget, hint in ((self.date_from, 'С даты UTC (YYYY-MM-DD)'),
                             (self.date_to, 'По дату UTC (YYYY-MM-DD)'),
                             (self.search, 'Станция / сессия')):
            widget.setPlaceholderText(self.tr(hint))
            widget.textChanged.connect(self._filter)
            filters.addWidget(widget)
        for widget in (self.model_filter, self.status_filter):
            widget.addItem(self.tr('Все'), None)
            widget.currentIndexChanged.connect(self._filter)
            filters.addWidget(widget)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 11)
        self.table.setHorizontalHeaderLabels(['Начало UTC', 'Окончание UTC', 'Модель', 'Станция',
            'Длительность, с', 'UBX, байт', 'Запись', 'Сохранённая целостность',
            'Конвертации', 'Предупреждения', 'Сессия'])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table)
        self.conversion_table = QTableWidget(0, 5)
        self.conversion_table.setHorizontalHeaderLabels(['Тип', 'Операция / отчёт', 'Статус', 'Связь', 'Подробности'])
        self.conversion_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel(self.tr('Операции конвертации и квитанции (отдельные записи)')))
        self.conversion_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.conversion_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.conversion_table.itemSelectionChanged.connect(self._conversion_selection)
        layout.addWidget(self.conversion_table)
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        layout.addWidget(self.detail_text)
        self.paths = QComboBox()
        layout.addWidget(self.paths)
        actions = QHBoxLayout()
        self.open_button = QPushButton(self.tr('Открыть выбранный путь'))
        self.copy_button = QPushButton(self.tr('Скопировать путь'))
        self.convert_button = QPushButton(self.tr('К ручной конвертации'))
        self.open_button.clicked.connect(self.open_selected_path)
        self.copy_button.clicked.connect(self.copy_selected_path)
        self.convert_button.clicked.connect(self._convert)
        for button in (self.open_button, self.copy_button, self.convert_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.issues = QPlainTextEdit()
        self.issues.setReadOnly(True)
        layout.addWidget(self.issues)
        layout.addWidget(QLabel(self.tr('Сохранённая целостность — исторический результат; новая проверка файлов не выполнялась.')))
        self.render_detail(None)

    def render_snapshot(self, payload):
        if not self.operational:
            return
        self._sessions = deepcopy(payload.get('sessions', []))
        self.render_detail(None)
        for combo, values in ((self.model_filter, {s.get('model') for s in self._sessions}),
                              (self.status_filter, {s.get('registration') or s.get('state') for s in self._sessions})):
            previous = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(self.tr('Все'), None)
            for value in sorted(v for v in values if v):
                combo.addItem(value, value)
            combo.setCurrentIndex(max(0, combo.findData(previous)))
            combo.blockSignals(False)
        self.issues.setPlainText(display(payload.get('diagnostics', [])))
        conversions = deepcopy(payload.get('conversions', []))
        self._conversions = conversions
        self.conversion_table.blockSignals(True)
        self.conversion_table.setRowCount(len(conversions))
        for row, conversion in enumerate(conversions):
            values = [conversion.get('kind'), conversion.get('operation_id') or conversion.get('path'),
                      conversion.get('status'), ', '.join(s['match'] for s in conversion.get('segments', [])),
                      conversion]
            for column, value in enumerate(values):
                self.conversion_table.setItem(row, column, QTableWidgetItem(display(value)))
        self.conversion_table.blockSignals(False)
        self._filter()

    def _filter(self, *_):
        key = self._detail.get('session_key') if self._detail else None
        query = self.search.text().casefold()
        rows = []
        for session in self._sessions:
            date = (session.get('started_utc') or '')[:10]
            if self.date_from.text() and (not date or date < self.date_from.text()):
                continue
            if self.date_to.text() and (not date or date > self.date_to.text()):
                continue
            if self.model_filter.currentData() and session.get('model') != self.model_filter.currentData():
                continue
            if self.status_filter.currentData() and (session.get('registration') or session.get('state')) != self.status_filter.currentData():
                continue
            if query and query not in ' '.join(str(session.get(k) or '') for k in
                    ('session_key', 'session_id', 'run_id', 'attempt_id')).casefold() + ' ' + str(station(session) or '').casefold():
                continue
            rows.append(session)
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row, session in enumerate(rows):
            manifest = session.get('manifest') or {}
            # Only persisted accounting is shown; artifact totals may double-count projections.
            size = (manifest.get('accounting') or {}).get('journal_written')
            values = [session.get('started_utc'), session.get('ended_utc'), session.get('model'),
                      station(session), session.get('duration_s'), size,
                      session.get('registration') or session.get('state'), session.get('recorded_integrity'),
                      ', '.join(c['status'] for c in session.get('conversions', [])) or None,
                      session.get('warnings'), session.get('session_id') or session.get('run_id')]
            for column, value in enumerate(values):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, value if type(value) in (int, float) else display(value))
                item.setData(Qt.ItemDataRole.UserRole, session['session_key'])
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.blockSignals(False)
        if key and not any(s['session_key'] == key for s in rows):
            self.render_detail(None)

    def _selection(self):
        items = self.table.selectedItems()
        if items:
            self.selection_requested.emit(items[0].data(Qt.ItemDataRole.UserRole))
        else:
            self.render_detail(None)

    def render_detail(self, payload):
        if not self.operational:
            return
        self._detail = deepcopy(payload)
        self.paths.clear()
        self.convert_button.setEnabled(bool(payload and payload.get('terminal') and not self._closing))
        if payload is None:
            self.detail_text.setPlainText(NO_DATA)
            return
        report = payload.get('report') or {}
        manifest = payload.get('manifest') or {}
        presented = present_result(report, conversions=payload.get('conversions', []))
        fields = {name: payload.get(name) for name in ('session_key', 'session_id', 'run_id',
                  'attempt_id', 'identity', 'model', 'started_utc', 'ended_utc', 'stop_reason',
                  'duration_s', 'registration', 'state', 'recorded_integrity')}
        diagnostics = presented['diagnostics']
        # The recorder persists these under extensions['gnss_logger.diagnostics'];
        # present_result also resolves Stage 1 and nested diagnostics reports.
        for name in ('RAWX', 'SFRBX', 'checksum_errors', 'decode_errors', 'phase_quality'):
            value = diagnostics.get(name, diagnostics.get(name.lower()))
            if value is None:
                value = report.get(name, report.get(name.lower()))
            fields[name] = value
        fields['segments'] = report.get('segments')
        fields['COM port'] = report.get('port') or [c.get('port') for c in manifest.get('connections', [])] or None
        fields['baud rate'] = (manifest.get('parameters') or report.get('parameters') or {}).get('baud')
        fields['bytes received'] = (report.get('accounting') or {}).get('received', report.get('bytes_received'))
        fields.update(report=report or None, manifest=payload.get('manifest'),
                      diagnostics=payload.get('diagnostics'), artifacts=payload.get('artifacts'),
                      conversions=payload.get('conversions'), presentation=presented)
        self.detail_text.setPlainText('\n\n'.join(f'{name}: {display(value)}' for name, value in fields.items()))
        paths = dict(payload.get('paths') or {})
        paths['session'] = payload['session_key']
        for index, artifact in enumerate(payload.get('ubx_files', [])):
            paths[f'UBX {index}'] = artifact.get('path')
            if artifact.get('path'):
                paths[f'UBX directory {index}'] = str(Path(artifact['path']).parent)
        for conversion in payload.get('conversions', []):
            paths[conversion['path']] = conversion['path']
            for segment in conversion.get('segments', []):
                for kind, output in segment.get('published_outputs', {}).items():
                    paths[f'{kind}: {output["path"]}'] = output['path']
                    paths[f'RINEX directory: {output["path"]}'] = str(Path(output['path']).parent)
        for label, path in paths.items():
            if path:
                self.paths.addItem(f'{label}: {path}', path)

    def apply_state(self, view):
        if self.operational:
            self._closing = view.get('closing', False)
            self.refresh_button.setEnabled(not self._closing)
            self.convert_button.setEnabled(bool(self._detail and self._detail.get('terminal') and not self._closing))

    def open_selected_path(self):
        raw = self.paths.currentData()
        if raw and Path(raw).exists():
            return QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(raw).resolve())))
        return False

    def copy_selected_path(self):
        raw = self.paths.currentData()
        if raw:
            QApplication.clipboard().setText(raw)

    def _convert(self):
        if self._detail and self._detail.get('terminal') and not self._closing:
            self.conversion_requested.emit(deepcopy(self._detail))

    def _conversion_selection(self):
        items = self.conversion_table.selectedItems()
        if not items:
            return
        conversion = self._conversions[items[0].row()]
        self.render_detail(None)
        self.detail_text.setPlainText(display(conversion))
        self.paths.addItem('JSON: ' + conversion['path'], conversion['path'])
        for segment in conversion.get('segments', []):
            source = segment.get('source_file') or {}
            if source.get('path'):
                self.paths.addItem('UBX: ' + source['path'], source['path'])
            for kind, output in segment.get('published_outputs', {}).items():
                self.paths.addItem(kind + ': ' + output['path'], output['path'])
                directory = str(Path(output['path']).parent)
                self.paths.addItem('RINEX: ' + directory, directory)
