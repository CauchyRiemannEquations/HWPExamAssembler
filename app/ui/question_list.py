from __future__ import annotations

from PySide6.QtCore import QEvent, QItemSelectionModel, QModelIndex, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractItemView, QApplication, QInputDialog, QListWidget, QListWidgetItem, QMenu

from app.services import QuestionSummary
from app.services.difficulty import DIFFICULTIES


from .question_row import LEVEL_ROLE, NUMBER_ROLE, QUESTION_ROLE, QuestionRowDelegate


class QuestionListWidget(QListWidget):
    order_changed = Signal()
    move_interaction_changed = Signal(bool)
    difficulty_requested = Signal(object, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setItemDelegate(QuestionRowDelegate(self))
        self.setUniformItemSizes(True)
        self.setDropIndicatorShown(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setAutoScrollMargin(36)
        self.setAccessibleName("문항 순서 및 난이도")
        self.setStyleSheet("QListWidget { padding: 0px; border: 1px solid #dce4e9; background: white; }"
                           "QListWidget::item { padding: 0px; }")
        self._pressed_control = None
        self.move_in_progress = False
        self._picking_position = False
        self._drag_origin = None
        self._drag_point = None
        self._preserved_selection = False
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(55)
        self._scroll_timer.timeout.connect(self._scroll_drag)
        self._drop_boundary: int | None = None
        self.difficulties: dict[str, str] = {}
        self.itemDoubleClicked.connect(self._difficulty_menu)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def selected_ids(self) -> tuple[str, ...]:
        return tuple(self.item(row).data(QUESTION_ROLE).id for row in self._selected_rows())

    def _selected_rows(self):
        return sorted(self.row(item) for item in self.selectedItems())

    def _select_ids(self, selected, current):
        self.clearSelection()
        for row in range(self.count()):
            item = self.item(row)
            qid = item.data(QUESTION_ROLE).id
            item.setSelected(qid in selected)
            if qid == current:
                self.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _choose_item(self, item):
        if not item.isSelected():
            self.setCurrentItem(item, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        else:
            self.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _control_at(self, position):
        item = self.itemAt(position)
        if item is not None:
            control = self.itemDelegate().control_at(self.visualItemRect(item), position)
            if control:
                return item, control
        return None

    def mousePressEvent(self, event):
        self._pressed_control = None
        self._end_drag()
        if event.button() == Qt.MouseButton.LeftButton and not event.modifiers():
            hit = self._control_at(event.position().toPoint())
            if hit:
                self._pressed_control = hit
                self._set_move_interaction(hit[1] == "position")
                self._choose_item(hit[0])
                event.accept()
                return
            point = event.position().toPoint()
            item = self.itemAt(point)
            if item:
                self._drag_origin = point
                self._set_move_interaction(True)
                if item.isSelected() and len(self.selectedItems()) > 1:
                    self._choose_item(item)
                    self._preserved_selection = True
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._drag_point is not None or (point - self._drag_origin).manhattanLength() >= QApplication.startDragDistance():
                self._drag_point = point
                self._drop_boundary = self._boundary_at(point)
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                if not self._scroll_timer.isActive():
                    self._scroll_timer.start()
                self.viewport().update()
            event.accept()
            return
        item = self.itemAt(point)
        if item:
            row = self.visualItemRect(item)
            if self.itemDelegate().control_at(row, point):
                self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
            elif point.x() - row.left() < 27:
                self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.viewport().unsetCursor()
        else:
            self.viewport().unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        hit = self._pressed_control
        self._pressed_control = None
        if hit:
            try:
                if hit == self._control_at(event.position().toPoint()):
                    if hit[1] == "position":
                        self.request_position()
                    else:
                        self.difficulty_requested.emit(self.selected_ids(), hit[1])
            finally:
                self._set_move_interaction(False)
            event.accept()
            return
        if self._drag_point is not None:
            point = event.position().toPoint()
            boundary = self._boundary_at(point)
            try:
                if self.viewport().rect().contains(point):
                    final_row = boundary - sum(row < boundary for row in self._selected_rows())
                    self.move_selected_to(final_row)
            finally:
                self._end_drag()
                self._set_move_interaction(False)
            event.accept()
            return
        preserved = self._preserved_selection
        self._end_drag()
        self._set_move_interaction(False)
        if preserved and self.currentItem():
            self.setCurrentItem(self.currentItem(), QItemSelectionModel.SelectionFlag.ClearAndSelect)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self._control_at(event.position().toPoint()):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if self._drag_origin is not None and event.key() == Qt.Key.Key_Escape:
            self._end_drag()
            self._set_move_interaction(False)
            event.accept()
            return
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_M:
            self.request_position()
            event.accept()
            return
        super().keyPressEvent(event)

    def request_position(self):
        rows = self._selected_rows()
        if not rows or len(rows) == self.count():
            return
        selected = self.selected_ids()
        label = "최종 배치 위치" if len(rows) == 1 else f"선택한 {len(rows)}개 항목의 첫 번째 위치"
        last_position = self.count() - len(rows) + 1
        self._picking_position = True
        self._set_move_interaction(True)
        try:
            position, accepted = QInputDialog.getInt(
                self, "문항 이동", f"{label} (1~{last_position})", rows[0] + 1, 1, last_position
            )
            if accepted and self.isEnabled() and self.selected_ids() == selected:
                self.move_selected_to(position - 1)
        finally:
            self._picking_position = False
            self._set_move_interaction(False)

    def _set_move_interaction(self, active):
        if self.move_in_progress != active:
            self.move_in_progress = active
            self.move_interaction_changed.emit(active)

    def event(self, event):
        if (event.type() in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide)
                and self.move_in_progress and not self._picking_position):
            self._pressed_control = None
            self._end_drag()
            self._set_move_interaction(False)
        return super().event(event)

    def set_difficulties(self, values: dict[str, str]) -> None:
        if values == self.difficulties:
            return
        self.difficulties = dict(values)
        for row in range(self.count()):
            item = self.item(row)
            question = item.data(QUESTION_ROLE)
            if item.data(LEVEL_ROLE) != self.difficulties.get(question.id, "미지정"):
                self._update_row_text(item, question, item.data(NUMBER_ROLE))

    def _update_row_text(self, item, question, number):
        level = self.difficulties.get(question.id, "미지정")
        kind = "공통묶음" if question.kind == "group" else "문항"
        manual = " · 직접 입력 필요" if question.manual_required else ""
        item.setData(LEVEL_ROLE, level)
        item.setText(f"{number:<7} [난이도: {level}]   {question.id} [{kind}{manual}]   {question.preview}")

    def _context_menu(self, position) -> None:
        item = self.itemAt(position)
        if item is not None:
            self._difficulty_menu(item)

    def _difficulty_menu(self, item) -> None:
        menu = QMenu(self)
        self._choose_item(item)
        ids = self.selected_ids()
        levels = {self.difficulties.get(qid, "미지정") for qid in ids}
        for value in DIFFICULTIES:
            action = menu.addAction(f"난이도: {value}")
            action.setCheckable(True)
            action.setChecked(levels == {value})
            action.triggered.connect(
                lambda checked=False, level=value: self.difficulty_requested.emit(ids, level)
            )
        menu.addSeparator()
        menu.addAction("배치 위치로 이동…", self.request_position)
        menu.exec(self.viewport().mapToGlobal(self.visualItemRect(item).bottomLeft()))

    def sort_by_difficulty(self, descending: bool = False) -> None:
        # Keep question types in separate runs and never split common passages.
        items = [self.item(row) for row in range(self.count())]
        def key(item):
            value = self.difficulties.get(item.data(QUESTION_ROLE).id, "미지정")
            rank = DIFFICULTIES.index(value)
            return (rank == 0, -rank if descending else rank)
        def category(item):
            question = item.data(QUESTION_ROLE)
            if question.objective_count and question.written_count:
                return None  # Mixed groups stay in place.
            return "objective" if question.numbered or question.objective_count else "written"
        start = 0
        while start < len(items):
            current_category = category(items[start])
            if current_category is None:
                start += 1
                continue
            end = start + 1
            while end < len(items) and category(items[end]) == current_category:
                end += 1
            items[start:end] = sorted(items[start:end], key=key)
            start = end
        self._reorder(items)

    def set_questions(self, questions: tuple[QuestionSummary, ...]) -> None:
        selected = set(self.selected_ids())
        current = self.currentItem()
        current_id = current.data(QUESTION_ROLE).id if current else None
        scroll = self.verticalScrollBar().value()
        self.clear()
        for question in questions:
            item = QListWidgetItem()
            item.setData(QUESTION_ROLE, question)
            flags = item.flags() | Qt.ItemFlag.ItemIsDragEnabled
            item.setFlags(flags)
            self.addItem(item)
        self.refresh_labels()
        if selected.intersection(self.ordered_ids()):
            self._select_ids(selected, current_id)
            self.verticalScrollBar().setValue(scroll)
        elif self.count():
            self.setCurrentRow(0)

    def ordered_ids(self) -> tuple[str, ...]:
        return tuple(
            self.item(row).data(QUESTION_ROLE).id for row in range(self.count())
        )

    def refresh_labels(self, summaries: dict[str, QuestionSummary] | None = None) -> None:
        visible_number = 0
        for row in range(self.count()):
            item = self.item(row)
            previous: QuestionSummary = item.data(QUESTION_ROLE)
            question = summaries[previous.id] if summaries is not None else previous
            old_number = item.data(NUMBER_ROLE)
            summary_changed = summaries is not None and question != previous
            details_changed = old_number is None or summary_changed
            if summary_changed:
                item.setData(QUESTION_ROLE, question)
            if question.kind == "group" and question.objective_count:
                start = visible_number + 1
                visible_number += question.objective_count
                number = (
                    f"{start}."
                    if start == visible_number
                    else f"{start}~{visible_number}."
                )
            elif question.numbered:
                visible_number += 1
                number = f"{visible_number}."
            else:
                number = "—"
            if not details_changed and number == old_number:
                continue
            item.setData(NUMBER_ROLE, number)
            self._update_row_text(item, question, number)
            if not details_changed:
                continue
            controls = ", ".join(
                f"{name.split(':', 1)[-1]} {count}"
                for name, count in question.controls.items()
            ) or "개체 없음"
            count_text = (
                f" · 선택형 {question.objective_count} · 서답형 {question.written_count}"
                if question.kind == "group"
                else ""
            )
            child_text = (
                f"\n하위 QID: {' '.join(question.child_ids)}" if question.child_ids else ""
            )
            error_text = (
                f"\n자동 수합 오류: {question.import_error}"
                if question.manual_required and question.import_error
                else ""
            )
            item.setToolTip(
                f"{question.preview}\n{question.id}{count_text}{child_text}\n{controls}{error_text}"
                "\n번호를 누르거나 Ctrl+M으로 이동 · 하·중·상 클릭으로 난이도 지정"
            )
            item.setData(Qt.ItemDataRole.ForegroundRole, QColor("#a33a16") if question.manual_required else None)

    def _reorder(self, items) -> bool:
        if tuple(item.data(QUESTION_ROLE).id for item in items) == self.ordered_ids():
            return False
        selected = set(self.selected_ids())
        current = self.currentItem()
        current_id = current.data(QUESTION_ROLE).id if current else None
        for _ in range(self.count()):
            self.takeItem(0)
        for item in items:
            self.addItem(item)
        self._select_ids(selected, current_id)
        self.refresh_labels()
        if self.currentItem():
            self.scrollToItem(self.currentItem())
        self.order_changed.emit()
        return True

    def move_selected_to(self, final_row: int) -> bool:
        """Move the selection as one block, keeping its internal order."""
        rows = self._selected_rows()
        if not rows or not 0 <= final_row <= self.count() - len(rows):
            return False
        if rows[-1] - rows[0] + 1 == len(rows):
            if rows[0] == final_row:
                return False
            parent = QModelIndex()
            destination = final_row if final_row < rows[0] else final_row + len(rows)
            if self.model().moveRows(parent, rows[0], len(rows), parent, destination):
                self.refresh_labels()
                if self.currentItem():
                    self.scrollToItem(self.currentItem())
                self.order_changed.emit()
                return True
        selected = [self.item(row) for row in rows]
        remaining = [self.item(row) for row in range(self.count()) if not self.item(row).isSelected()]
        return self._reorder(remaining[:final_row] + selected + remaining[final_row:])

    def move_current(self, offset: int) -> bool:
        rows = self._selected_rows()
        if not rows:
            return False
        if rows[0] + offset < 0 or rows[-1] + offset >= self.count():
            return False
        if rows[-1] - rows[0] + 1 == len(rows):
            return self.move_selected_to(rows[0] + offset)
        items = [self.item(row) for row in range(self.count())]
        for row in (reversed(rows) if offset > 0 else rows):
            item = items.pop(row)
            items.insert(row + offset, item)
        return self._reorder(items)

    def swap_selected(self) -> bool:
        rows = self._selected_rows()
        if len(rows) != 2:
            return False
        low, high = rows
        items = [self.item(row) for row in range(self.count())]
        items[low], items[high] = items[high], items[low]
        return self._reorder(items)

    def remove_selected(self) -> tuple[str, ...]:
        rows = sorted(
            (self.row(item) for item in self.selectedItems()), reverse=True
        )
        if not rows:
            return ()
        if len(rows) == self.count():
            raise ValueError("시험지의 모든 문항을 삭제할 수는 없습니다.")
        removed: list[str] = []
        for row in rows:
            item = self.takeItem(row)
            question: QuestionSummary = item.data(QUESTION_ROLE)
            removed.append(question.id)
        self.refresh_labels()
        if self.count():
            self.setCurrentRow(min(rows[-1], self.count() - 1))
        self.order_changed.emit()
        return tuple(reversed(removed))

    def _boundary_at(self, point):
        item = self.itemAt(point)
        if item is None:
            return 0 if point.y() < 0 else self.count()
        return self.row(item) + int(point.y() > self.visualItemRect(item).center().y())

    def _end_drag(self):
        self._drag_origin = None
        self._drag_point = None
        self._drop_boundary = None
        self._preserved_selection = False
        self._scroll_timer.stop()
        self.viewport().unsetCursor()
        self.viewport().update()

    def _scroll_drag(self):
        point = self._drag_point
        if point is None:
            self._scroll_timer.stop()
            return
        if not 0 <= point.x() < self.viewport().width():
            return
        bar = self.verticalScrollBar()
        margin = self.autoScrollMargin()
        if point.y() < margin:
            bar.setValue(bar.value() - max(8, margin - point.y()))
        elif point.y() >= self.viewport().height() - margin:
            bar.setValue(bar.value() + max(8, point.y() - self.viewport().height() + margin))
        else:
            return
        self._drop_boundary = self._boundary_at(point)
        self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        boundary = self._drop_boundary
        if boundary is None or not self.count():
            return
        if boundary == self.count():
            y = self.visualItemRect(self.item(boundary - 1)).bottom() - 1
        else:
            y = self.visualItemRect(self.item(boundary)).top() + 1
        painter = QPainter(self.viewport())
        painter.setPen(QPen(QColor("#105c5a"), 3))
        painter.drawLine(QPoint(4, y), QPoint(self.viewport().width() - 4, y))
        painter.end()
