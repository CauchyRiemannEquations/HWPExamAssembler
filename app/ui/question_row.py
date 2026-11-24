"""Painting and hit testing for the movable question rows."""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QStyle

QUESTION_ROLE = Qt.ItemDataRole.UserRole
NUMBER_ROLE = QUESTION_ROLE + 1
LEVEL_ROLE = QUESTION_ROLE + 2
LEVELS = ("하", "중", "상")
CENTER = Qt.AlignmentFlag.AlignCenter
LEFT = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft


class QuestionRowDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(260, max(60, option.fontMetrics.height() * 2 + 16))

    @staticmethod
    def position_rect(row):
        return QRect(row.left() + 28, row.center().y() - 15, 54, 31)

    @staticmethod
    def level_rect(row, index):
        return QRect(row.right() - 128 + 39 * index, row.center().y() - 15, 38, 31)

    def control_at(self, row, point):
        if self.position_rect(row).contains(point):
            return "position"
        for index, value in enumerate(LEVELS):
            if self.level_rect(row, index).contains(point):
                return value
        return None

    def paint(self, painter, option, index):
        row = option.rect
        question = index.data(QUESTION_ROLE)
        number = index.data(NUMBER_ROLE) or ""
        level = index.data(LEVEL_ROLE)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setClipRect(row)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        background = "#e7f3ef" if selected else ("#f6f9f9" if hovered else "#ffffff")
        painter.fillRect(row, QColor(background))
        if not enabled:
            painter.setOpacity(0.64)
        if selected:
            painter.fillRect(QRect(row.left(), row.top(), 3, row.height()), QColor("#19756e"))
        painter.setPen(QColor("#dce4e9"))
        painter.drawLine(row.bottomLeft(), row.bottomRight())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#819493"))
        for x in (12, 17):
            for y in (-5, 0, 5):
                painter.drawEllipse(row.left() + x, row.center().y() + y, 2, 2)

        normal = option.font
        compact = painter.font()
        compact.setFamily(normal.family())
        compact.setPointSizeF(9.5)
        compact.setBold(False)
        painter.setFont(compact)
        pos = self.position_rect(row)
        painter.setPen(QColor("#bccfce"))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(pos.adjusted(0, 0, -1, -1), 5, 5)
        painter.setPen(QColor("#264642"))
        painter.drawText(pos.adjusted(0, 0, -10, 0), CENTER, str(index.row() + 1))
        painter.drawLine(pos.right() - 12, pos.center().y() - 1, pos.right() - 9, pos.center().y() + 2)
        painter.drawLine(pos.right() - 9, pos.center().y() + 2, pos.right() - 6, pos.center().y() - 1)

        if question.kind == "group":
            category = f"공통 지문 묶음 · 선택형 {question.objective_count} · 서답형 {question.written_count}"
        elif question.numbered:
            category = "선택형"
        else:
            category = "서답형"
        if number and number != "—":
            category += "  " + number.rstrip(".") + "번"
        if level == "미지정":
            category += "  ·  난이도 미지정"
        if question.manual_required:
            category += "  ·  직접 입력 필요"
        x = pos.right() + 14
        width = max(0, self.level_rect(row, 0).left() - x - 12)
        line_height = QFontMetrics(compact).height() + 1
        top = row.center().y() - line_height - 1
        painter.setPen(QColor("#934119" if question.manual_required else "#4c6569"))
        painter.drawText(QRect(x, top, width, line_height), LEFT,
                         QFontMetrics(compact).elidedText(category, Qt.TextElideMode.ElideRight, width))
        normal.setBold(True)
        painter.setFont(normal)
        painter.setPen(QColor("#19363d"))
        prompt = " ".join(question.preview.split()) or "문항 미리보기 없음"
        painter.drawText(QRect(x, top + line_height, width, QFontMetrics(normal).height() + 2), LEFT,
                         QFontMetrics(normal).elidedText(prompt, Qt.TextElideMode.ElideRight, width))

        painter.setFont(compact)
        for i, value in enumerate(LEVELS):
            area = self.level_rect(row, i)
            active = level == value
            painter.setPen(QColor("#105c5a" if active else "#ccdad9"))
            painter.setBrush(QColor("#105c5a" if active else "#ffffff"))
            painter.drawRoundedRect(area.adjusted(0, 0, -1, -1), 5, 5)
            painter.setPen(QColor("#ffffff" if active else "#425c60"))
            painter.drawText(area, CENTER, value)
        painter.restore()
