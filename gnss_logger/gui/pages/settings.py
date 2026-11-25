from PySide6.QtWidgets import QFormLayout, QLabel, QSpinBox, QVBoxLayout, QWidget

from ... import __version__


class SettingsPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel(self.tr('Настройки'))
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        form = QFormLayout()
        form.addRow(self.tr('Версия приложения'), QLabel(__version__))
        form.addRow(self.tr('Язык'), QLabel(self.tr('Русский (RU)')))
        self.polling = QSpinBox()
        self.polling.setRange(250, 2000)
        self.polling.setSingleStep(250)
        self.polling.setValue(250)
        self.polling.setSuffix(self.tr(' мс'))
        self.polling.setAccessibleName(self.tr('Интервал мониторинга'))
        form.addRow(self.tr('Опрос следующей демонстрации'), self.polling)
        layout.addLayout(form)
        note = QLabel(self.tr('Настройки этого каркаса действуют только до закрытия окна. '
                              'TOML и конфигурация приёмника не изменяются. '
                              'Сохранение preferences — Stage 4.5.'))
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
