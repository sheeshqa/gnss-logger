from PySide6.QtWidgets import QLabel, QPushButton, QTableWidget, QVBoxLayout, QWidget


class HistoryPage(QWidget):
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
