"""Exam editor start screen and its two document workflows."""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


class DocumentIllustration(QWidget):
    def __init__(self, *, stack: bool, parent=None):
        super().__init__(parent)
        self.stack = stack
        self.setFixedSize(84, 65)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        stroke = QColor("#407674")
        painter.setPen(QPen(stroke, 1.6))
        if self.stack:
            for x, y, fill in [(25, 4, "#c4dcd4"), (17, 10, "#e1eee8"), (9, 16, "#ffffff")]:
                painter.setBrush(QColor(fill))
                painter.drawRoundedRect(QRectF(x, y, 40, 44), 3, 3)
            painter.drawLine(16, 26, 37, 26)
            for y in (34, 40, 46):
                painter.drawLine(16, y, 36 if y == 46 else 42, y)
        else:
            for i, y in enumerate((8, 25, 42)):
                painter.setBrush(QColor("#dceee7" if i == 1 else "#ffffff"))
                painter.drawRoundedRect(QRectF(5, y, 59, 14), 3, 3)
                painter.setBrush(stroke)
                painter.drawEllipse(QPointF(13, y + 7), 1, 1)
                painter.drawLine(21, y + 7, 42, y + 7)
        painter.end()


class HomePage(QWidget):
    document_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("examHome")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(38, 26, 38, 30)
        outer.setSpacing(10)
        outer.addStretch(1)
        title = QLabel("시험지 시작하기")
        title.setObjectName("homeTitle")
        outer.addWidget(title)
        subtitle = QLabel("새 시험지를 수합하거나 기존 편집본의 문항을 정리하세요.")
        subtitle.setObjectName("homeIntro")
        outer.addWidget(subtitle)
        outer.addSpacing(13)
        cards = QHBoxLayout()
        cards.setSpacing(16)
        for stack, title, detail, action in (
            (True, "새 시험지 수합", "내 학교 양식을 등록하고 여러 출제 파일을 한 시험지에 모읍니다.", "내 학교 양식 선택…"),
            (False, "기존 시험지 편집", "문항 순서와 난이도를 바꾸고 한글에서 확인합니다.", "편집본 열기…"),
        ):
            card = QFrame()
            card.setObjectName("importCard" if stack else "editCard")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(23, 21, 23, 22)
            layout.setSpacing(8)
            layout.addWidget(DocumentIllustration(stack=stack))
            heading = QLabel(title)
            heading.setObjectName("cardTitle")
            layout.addWidget(heading)
            body = QLabel(detail)
            body.setTextFormat(Qt.TextFormat.PlainText)
            body.setWordWrap(True)
            body.setObjectName("cardBody")
            layout.addWidget(body)
            layout.addStretch(1)
            layout.addSpacing(10)
            button = QPushButton(action)
            button.setObjectName("primaryButton" if stack else "secondaryButton")
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            if stack:
                self.import_button = button
                self.import_description = body
            else:
                self.edit_button = button
                self.edit_description = body
            cards.addWidget(card, 1)
        outer.addLayout(cards)
        self.drop_hint = QLabel("HWP 또는 HWPX 편집본을 이 화면에 놓아도 열 수 있습니다.")
        self.drop_hint.setObjectName("dropHint")
        outer.addWidget(self.drop_hint)
        outer.addStretch(2)

    @staticmethod
    def _document(event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() in {".hwp", ".hwpx"} and path.is_file():
                return str(path)
        return None

    def dragEnterEvent(self, event):
        if self._document(event):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = self._document(event)
        if path:
            event.acceptProposedAction()
            self.document_dropped.emit(path)
