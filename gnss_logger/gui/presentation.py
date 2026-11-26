"""Shared, GUI-only presentation. Qt sizes are device-independent pixels."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QLineEdit, QTableWidget


STYLE = '''
QLabel#pageTitle { font-size: 18pt; font-weight: 600; margin-bottom: 8px; }
QLabel#modeBanner { padding: 8px; background: #e7eef5; color: #18232f; }
QGroupBox { font-weight: 600; margin-top: 12px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; }
QPushButton { padding: 6px 10px; }
QPushButton:focus, QLineEdit:focus, QComboBox:focus, QListWidget:focus {
    border: 2px solid #2563a6;
}
QListWidget#navigation::item { padding: 10px 8px; }
QListWidget#navigation::item:selected { background: #245985; color: white; }
QLabel#persistentStatus { padding: 3px; }
'''


def application_icon():
    return QIcon(str(Path(__file__).with_name('resources') / 'gnss-logger.svg'))


def polish_page(page):
    """Keep native controls and palettes; make forms and long paths adaptable."""
    page.layout().setContentsMargins(16, 12, 16, 12)
    page.layout().setSpacing(10)
    for form in page.findChildren(QFormLayout):
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    for label in page.findChildren(QLabel):
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setMinimumWidth(0)
    for edit in page.findChildren(QLineEdit):
        edit.setMinimumWidth(0)
        edit.setToolTip(edit.text())
        edit.textChanged.connect(edit.setToolTip)
        if not edit.accessibleName():
            edit.setAccessibleName(edit.placeholderText() or page.tr('Поле параметра'))
    for combo in page.findChildren(QComboBox):
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(10)
    for table in page.findChildren(QTableWidget):
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        table.setWordWrap(False)


def status_text(receiver, manual=None):
    """Display only coordinator facts; absence of a queue snapshot is unknown."""
    automatic = receiver.get('auto_conversion') or {}
    device = receiver.get('selected_device')
    verified = bool(receiver.get('binding'))
    return ('Приёмник: {device} ({verified}) | Запись: {record} | '
            'Ручная: {manual} | Auto: {auto} | Очередь: {queue}').format(
        device=device or '—', verified='подтверждён' if verified else 'не подтверждён',
        record=receiver.get('phase', '—'), manual=(manual or {}).get('phase', '—'),
        auto='On' if receiver.get('auto_enabled') else 'Off',
        queue=automatic.get('queued', '—'))
