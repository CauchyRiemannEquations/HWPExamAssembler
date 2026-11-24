"""Qt side panel for reviewing and registering a local Hancom exam template."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFontDatabase, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QDialog, QMessageBox, QPushButton, QSizePolicy,
    QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

ROLES = {
    "keep": ("남길 영역", "#e7f1eb", "#33644e"),
    "replace": ("예시 문항", "#fbedd9", "#79501a"),
    "insert": ("새 문항 시작", "#e8eef9", "#395c8a"),
}
LEFT = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
CENTER = Qt.AlignmentFlag.AlignCenter
REGION = Qt.ItemDataRole.UserRole

STYLE = """
QDialog, QWidget#shell, QWidget#landing, QWidget#editor { background: #fcfdfb; }
QWidget { font-family: "Malgun Gothic", "Segoe UI"; font-size: 10.5pt; color: #223a36; }
QLabel { background: transparent; }
QFrame#bar { background: #f0f4ef; border-bottom: 1px solid #d6e1d8; }
QLabel#brand { font-size: 13pt; font-weight: bold; color: #203f37; }
QLabel#heading { font-size: 20pt; font-weight: bold; color: #193b32; }
QLabel#subtitle { color: #52665d; font-size: 10pt; }
QLabel#section { font-size: 13pt; font-weight: bold; color: #223e34; }
QLabel#actionTitle { font-weight: bold; }
QLabel#footerText { color: #4a6355; font-size: 9.5pt; }
QFrame#fileDrop { background: #eff5f0; border: 1px dashed #a6bdae; border-radius: 12px; }
QFrame#fileDrop[dragging="true"] { background: #deeee3; border: 2px solid #267459; }
QPushButton#openFile { background: #196352; color: white; border: 1px solid #196352;
                       border-radius: 7px; padding: 6px 18px; min-height: 28px; font-weight: bold; }
QPushButton#openFile:hover { background: #114f42; }
QListWidget { background: #fcfdfb; border: none; outline: none; }
QFrame#controls { background: #f0f5f1; border: 1px solid #d8e3da; border-radius: 8px; }
QFrame#controls[correcting="true"] { background: #eaf4ee; border: 1px solid #90b9a5; }
QFrame#footer { background: #f0f4ef; border-top: 1px solid #d6e1d8; }
QPushButton { background: #fff; color: #274d3f; font-weight: 600; border: 1px solid #bdcfc2;
              border-radius: 6px; min-height: 27px; padding: 3px 12px; }
QPushButton:hover { background: #e9f2eb; border-color: #89aa97; }
QPushButton:focus { border: 2px solid #267464; }
QPushButton#primary { background: #196352; color: white; border: 1px solid #196352; }
QPushButton#primary:hover { background: #114f42; }
QPushButton:disabled, QPushButton#primary:disabled { color: #65776b; background: #e7ece7;
                                                     border: 1px solid #d8e2d9; }
QPushButton#quiet { background: transparent; border-color: transparent; }
QPushButton#quiet:hover { background: #e3ebe4; }
"""


def label(text, name, wrap=False):
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    return widget


def ensure_korean_font():
    # Windows' offscreen Qt plugin can see no installed fonts even though
    # the language pack is present. Use the PC's own font in that case.
    if os.name == "nt" and "Malgun Gothic" not in QFontDatabase.families():
        font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for name in ("malgun.ttf", "malgunbd.ttf"):
            path = font_dir / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))


class PaperMark(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(28, 28)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#246852"))
        p.drawRoundedRect(self.rect(), 6, 6)
        outline = QPainterPath()
        outline.moveTo(8, 5)
        outline.lineTo(16, 5)
        outline.lineTo(20, 9)
        outline.lineTo(20, 22)
        outline.lineTo(8, 22)
        outline.closeSubpath()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor("#edf7ef"), 1.3))
        p.drawPath(outline)
        p.drawLine(11, 13, 17, 13)
        p.drawLine(11, 16, 16, 16)
        p.end()


class DocumentMark(QWidget):
    """A paper form marking the supported document drop target."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(58, 56)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#dae8de"))
        p.setPen(QPen(QColor("#aec5b6"), 1.4))
        p.drawRoundedRect(11, 7, 29, 38, 3, 3)
        front = QPainterPath()
        front.moveTo(18, 12)
        front.lineTo(38, 12)
        front.lineTo(47, 21)
        front.lineTo(47, 49)
        front.lineTo(18, 49)
        front.closeSubpath()
        p.setBrush(QColor("#ffffff"))
        p.setPen(QPen(QColor("#36775b"), 1.6))
        p.drawPath(front)
        p.drawLine(38, 12, 38, 21)
        p.drawLine(38, 21, 47, 21)
        for y, end in ((28, 40), (33, 40), (38, 35)):
            p.drawLine(24, y, end, y)
        p.end()


def has_location(region):
    return region.start is not None and (region.role == "insert" or region.end is not None)


class RegionDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(200, max(62, QFontMetrics(option.font).height() * 2 + 22))

    def paint(self, painter, option, index):
        region = index.data(REGION)
        role, fill, ink = ROLES[region.role]
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        row = option.rect.adjusted(1, 2, -2, -3)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#f0f7f1" if selected else "#ffffff"))
        painter.setPen(QPen(QColor("#44806a" if selected else "#d7e1d9"), 1.6 if selected else 1))
        painter.drawRoundedRect(row, 7, 7)
        small = painter.font()
        small.setPointSizeF(9)
        small.setBold(True)
        painter.setFont(small)
        fm = QFontMetrics(small)
        badge = QRect(row.left()+12, row.center().y()-12, fm.horizontalAdvance("새 문항 시작")+15, 25)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(QColor(ink))
        painter.drawText(badge, CENTER, role)
        status = "직접 지정" if region.corrected else ("후보" if has_location(region) else "미지정")
        sw = fm.horizontalAdvance(status) + 2
        painter.setPen(QColor("#397258" if region.corrected else "#6d776f"))
        painter.drawText(QRect(row.right()-sw-12, row.top(), sw, row.height()), CENTER, status)
        x = badge.right() + 13
        width = max(0, row.right() - sw - 22 - x)
        normal = option.font
        normal.setBold(True)
        painter.setFont(normal)
        height = QFontMetrics(normal).height()
        painter.setPen(QColor("#223d32"))
        painter.drawText(QRect(x, row.center().y()-height, width, height), LEFT,
                         QFontMetrics(normal).elidedText(region.label, Qt.TextElideMode.ElideRight, width))
        small.setBold(False)
        painter.setFont(small)
        painter.setPen(QColor("#52645a"))
        detail = " ".join(region.detail.split()) or ("커서 위치" if region.role == "insert" else "문서 범위")
        painter.drawText(QRect(x, row.center().y()+1, width, height), LEFT,
                         QFontMetrics(small).elidedText(detail, Qt.TextElideMode.ElideRight, width))
        painter.restore()


class StudioWindow(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        ensure_korean_font()
        self.controller = controller
        self.registration = None
        self.state = None
        self._busy = False
        self._capture_key = None
        self._pending_capture = None
        self._closing = False
        self._closed = False
        self._rendering = False
        self._changed = False
        self.setWindowTitle("학교 양식 등록")
        self.resize(594, 755)
        self.setMinimumSize(430, 580)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLE)
        shell = QWidget()
        shell.setObjectName("shell")
        frame = QVBoxLayout(shell)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("bar")
        top = QHBoxLayout(bar)
        top.setContentsMargins(18, 10, 14, 10)
        top.addWidget(PaperMark())
        top.addWidget(label("학교 양식 등록", "brand"))
        top.addStretch()
        self.change_button = QPushButton("다른 양식 열기…")
        self.change_button.setObjectName("quiet")
        self.change_button.clicked.connect(self._choose_file)
        top.addWidget(self.change_button)
        frame.addWidget(bar)
        self.pages = QStackedWidget()
        self.landing = self._landing()
        self.editor = self._editor()
        self.pages.addWidget(self.landing)
        self.pages.addWidget(self.editor)
        frame.addWidget(self.pages, 1)
        self.footer = QFrame()
        self.footer.setObjectName("footer")
        bottom = QHBoxLayout(self.footer)
        bottom.setContentsMargins(18, 10, 18, 10)
        self.notice = label("", "footerText", True)
        self.notice.setAccessibleName("작업 상태")
        bottom.addWidget(self.notice, 1)
        self.save_button = QPushButton("이 양식 등록")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save)
        bottom.addWidget(self.save_button)
        frame.addWidget(self.footer)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(shell)
        self._buttons()
        controller.state_changed.connect(self._set_state)
        controller.busy_changed.connect(self._set_busy)
        controller.status.connect(self._set_status)
        controller.failed.connect(self._failed)
        controller.saved.connect(self._saved)
        controller.closed.connect(self._on_closed)

    def _landing(self):
        page = QWidget()
        page.setObjectName("landing")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(7)
        layout.addStretch(2)
        layout.addWidget(label("학교 시험지 양식을 여세요", "heading", True))
        layout.addWidget(label("한글에서 남길 영역과 새 문항을 넣을 자리를 확인합니다.", "subtitle", True))
        layout.addSpacing(14)
        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("fileDrop")
        body = QVBoxLayout(self.drop_zone)
        body.setContentsMargins(12, 24, 12, 24)
        body.setSpacing(10)
        body.addWidget(DocumentMark(), 0, Qt.AlignmentFlag.AlignHCenter)
        self.own_button = QPushButton("내 학교 양식 열기…")
        self.own_button.setObjectName("openFile")
        self.own_button.clicked.connect(self._choose_file)
        body.addWidget(self.own_button, 0, Qt.AlignmentFlag.AlignHCenter)
        drag_hint = label("HWP · HWPX 파일을 여기에 끌어 놓아도 됩니다.", "subtitle", True)
        drag_hint.setAlignment(CENTER)
        body.addWidget(drag_hint)
        layout.addWidget(self.drop_zone)
        layout.addStretch(3)
        return page

    def _editor(self):
        page = QWidget()
        page.setObjectName("editor")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 15, 20, 12)
        layout.setSpacing(5)
        self.document_label = label("", "heading", True)
        self.document_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.document_label)
        self.source_label = label("", "subtitle")
        self.source_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.source_label)
        layout.addSpacing(9)
        heading = QHBoxLayout()
        heading.addWidget(label("양식 영역", "section"))
        heading.addStretch()
        self.ai_button = QPushButton("AI로 영역 제안…")
        self.ai_button.setToolTip("전송할 구조 정보를 확인한 뒤 선택한 분석 방법으로 영역을 제안받습니다.")
        self.ai_button.clicked.connect(self._review_ai)
        heading.addWidget(self.ai_button)
        layout.addLayout(heading)
        layout.addWidget(label("항목을 누르면 한글에서 위치를 보여줍니다. 예시 문항 밖은 그대로 남깁니다.", "subtitle", True))
        self.regions = QListWidget()
        self.regions.setAccessibleName("양식 영역 목록")
        self.regions.setItemDelegate(RegionDelegate(self.regions))
        self.regions.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.regions.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.regions.currentItemChanged.connect(self._selected)
        self.regions.itemClicked.connect(lambda _: self._show())
        layout.addWidget(self.regions, 1)
        self.controls = QFrame()
        self.controls.setObjectName("controls")
        action = QVBoxLayout(self.controls)
        action.setContentsMargins(12, 10, 12, 10)
        action.setSpacing(4)
        self.action_title = label("", "actionTitle", True)
        self.hint = label("", "subtitle", True)
        action.addWidget(self.action_title)
        action.addWidget(self.hint)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.show_button = QPushButton("한글에서 위치 보기")
        self.show_button.clicked.connect(self._show)
        self.correct_button = QPushButton("직접 지정")
        self.correct_button.clicked.connect(self._start_capture)
        self.clear_button = QPushButton("지정 해제")
        self.clear_button.setObjectName("quiet")
        self.clear_button.clicked.connect(self._clear)
        self.cancel_button = QPushButton("취소")
        self.cancel_button.clicked.connect(self._cancel_capture)
        self.capture_button = QPushButton("선택 적용")
        self.capture_button.setObjectName("primary")
        self.capture_button.clicked.connect(self._capture)
        for b in (self.show_button, self.correct_button, self.clear_button, self.cancel_button, self.capture_button):
            buttons.addWidget(b)
        buttons.addStretch()
        action.addLayout(buttons)
        layout.addWidget(self.controls)
        return page

    def _selected_region(self):
        item = self.regions.currentItem()
        return item.data(REGION) if item else None

    def _selected(self, current, previous):
        if self._rendering:
            return
        self._buttons()

    def _set_state(self, state):
        previous = self.state
        selected = self._selected_region()
        same = bool(previous and state and previous.source == state.source)
        self._rendering = True
        self.state = state
        self.regions.clear()
        if state:
            self.document_label.setText(state.title)
            self.document_label.setToolTip(str(state.source))
            self.source_label.setText(Path(state.source).name)
            self.source_label.setToolTip(str(state.source))
            for region in state.regions:
                item = QListWidgetItem()
                item.setData(REGION, region)
                role = ROLES[region.role][0]
                item.setData(Qt.ItemDataRole.AccessibleTextRole, f"{role}, {region.label}, {region.detail}")
                item.setToolTip(f"{role} · {region.label}\n{region.detail}")
                self.regions.addItem(item)
            initial = next((i for i, r in enumerate(state.regions) if r.role == "insert"), 0)
            index = next((i for i, r in enumerate(state.regions)
                          if same and selected and r.key == selected.key), initial)
            if self.regions.count():
                self.regions.setCurrentRow(index)
            if self._pending_capture is not None:
                self._capture_key = self._pending_capture = None
                self._changed = True
            if state.registered:
                self._changed = False
        if not same:
            self._capture_key = self._pending_capture = None
            self._changed = False
        self.pages.setCurrentWidget(self.editor if state else self.landing)
        self._rendering = False
        self._buttons()

    def _buttons(self):
        if not hasattr(self, "save_button"):
            return
        loaded = self.state is not None
        region = self._selected_region()
        editing = self._capture_key is not None
        enabled = not self._busy and not self._closing
        self.own_button.setEnabled(enabled)
        self.change_button.setVisible(loaded)
        self.change_button.setEnabled(enabled and not editing)
        self.ai_button.setEnabled(loaded and enabled and not editing)
        self.footer.setVisible(loaded or self._busy or bool(self.notice.text()))
        self.save_button.setVisible(loaded)
        self.save_button.setText("이 양식 사용" if loaded and self.state.registered else "등록하고 사용")
        ready = loaded and any(r.role == "insert" and has_location(r) for r in self.state.regions)
        self.save_button.setEnabled(ready and enabled and not editing)
        self.save_button.setToolTip("" if ready else "새 문항 시작 위치를 먼저 지정하세요.")
        self.regions.setEnabled(enabled and not editing)
        self.show_button.setVisible(not editing)
        self.correct_button.setVisible(not editing)
        self.clear_button.setVisible(not editing and region is not None and region.role != "insert" and has_location(region))
        self.clear_button.setEnabled(enabled)
        self.cancel_button.setVisible(editing)
        self.capture_button.setVisible(editing)
        self.cancel_button.setEnabled(enabled)
        self.capture_button.setEnabled(enabled and editing)
        self.show_button.setEnabled(enabled and region is not None and has_location(region))
        self.correct_button.setEnabled(enabled and region is not None)
        self.controls.setProperty("correcting", editing)
        self.controls.style().unpolish(self.controls)
        self.controls.style().polish(self.controls)
        if region is None:
            self.action_title.setText("영역을 선택하세요")
            self.hint.clear()
            return
        self.action_title.setText(region.label + (" 직접 지정" if editing else ""))
        if editing:
            if region.role == "insert":
                self.hint.setText("한글 작업 사본에서 새 문항이 시작될 자리를 클릭한 뒤, 이 창에서 ‘커서 위치 적용’을 누르세요.")
            else:
                self.hint.setText("한글 작업 사본에서 범위를 드래그한 뒤, 이 창으로 돌아와 ‘선택 범위 적용’을 누르세요.")
            self.capture_button.setText("커서 위치 적용" if region.role == "insert" else "선택 범위 적용")
        else:
            self.hint.setText({
                "keep": "시험지에서 계속 사용할 제목, 안내문 등의 범위입니다.",
                "replace": "새 문항으로 바꿀 예시 문제의 처음부터 끝까지입니다.",
                "insert": "새 문제의 첫 문항이 들어갈 커서 위치입니다.",
            }[region.role])

    def _set_status(self, message):
        self.notice.setText(message)
        self.notice.setToolTip(message)
        self.notice.setStyleSheet("")
        self.footer.setVisible(bool(message) or self._busy or self.state is not None)

    def _set_busy(self, busy):
        self._busy = busy
        self._buttons()

    def _failed(self, message):
        self._pending_capture = None
        self.notice.setText(message)
        self.notice.setToolTip(message)
        self.notice.setStyleSheet("color: #93421e;")
        self._buttons()

    def _show(self):
        region = self._selected_region()
        if region and has_location(region) and not self._busy:
            self.controller.show_region(region.key)

    def _start_capture(self):
        region = self._selected_region()
        if region:
            self._capture_key = region.key
            self._set_status("한글에서 위치를 지정한 뒤 이 창으로 돌아오세요.")
            self._buttons()

    def _cancel_capture(self):
        self._capture_key = self._pending_capture = None
        self._set_status("기존 영역을 유지했습니다.")
        self._buttons()

    def _capture(self):
        if self._capture_key and not self._busy:
            self._pending_capture = self._capture_key
            self.controller.capture_region(self._capture_key)

    def _clear(self):
        region = self._selected_region()
        if region and region.role != "insert" and not self._busy:
            self._pending_capture = region.key
            self.controller.clear_region(region.key)

    def _discard_ok(self):
        if not self._changed:
            return True
        return QMessageBox.question(self, "아직 등록하지 않은 변경",
            "변경한 영역을 등록하지 않고 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "학교 시험지 양식 열기", "",
                                               "한글 문서 (*.hwp *.hwpx)")
        if path and self._discard_ok():
            self.controller.open(Path(path))

    def _save(self):
        if self.state is not None and self._capture_key is None and not self._busy:
            self.controller.save_profile()

    def _review_ai(self):
        if self.state is None or self._busy or self._capture_key:
            return
        from app.ui.template_ai_dialog import review_with_ai
        proposed = review_with_ai(self, self.state.paragraphs, self.state.regions)
        if proposed is not None and proposed != self.state.regions:
            self._pending_capture = 'proposal'
            self.controller.apply_proposal(proposed)

    def _saved(self, registration):
        self.registration = registration
        self._closing = True
        self._changed = False
        self._buttons()
        self.controller.close_session()

    @staticmethod
    def _dropped_path(event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() in {".hwp", ".hwpx"} and path.is_file():
                return path
        return None

    def _drag_highlight(self, enabled):
        self.drop_zone.setProperty("dragging", enabled)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)

    def dragEnterEvent(self, event):
        if not self._busy and not self._closing and self._capture_key is None and self._dropped_path(event):
            self._drag_highlight(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._drag_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._drag_highlight(False)
        path = self._dropped_path(event)
        if path and not self._busy and not self._closing and self._capture_key is None and self._discard_ok():
            event.acceptProposedAction()
            self.controller.open(path)

    def closeEvent(self, event):
        if self._closed:
            event.accept()
            return
        event.ignore()
        if not self._closing and self._discard_ok():
            self._closing = True
            self._buttons()
            self.controller.close_session()

    def reject(self):
        if not self._closing:
            self.close()

    def _on_closed(self):
        self._closed = True
        self.done(QDialog.DialogCode.Accepted if self.registration else QDialog.DialogCode.Rejected)
