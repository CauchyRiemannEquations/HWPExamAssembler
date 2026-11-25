from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtCore import QItemSelectionModel, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
    QStackedWidget,
)

from app.services import (
    DocumentSnapshot,
    ExamDocumentService,
    file_fingerprint,
    file_is_in_use,
)
from app.services.difficulty import DIFFICULTIES, load_difficulties, save_difficulties
from app.services.template_catalog import recent_templates, template_data_root
from app.ui.question_list import QuestionListWidget
from app.ui.hwp_companion import CompanionWorker
from app.ui.workers import DocumentWorker
from app.ui.home import HomePage
from app.ui.theme import EDITOR_STYLE


MOVE_SETTLE_MS = 75


@dataclass(frozen=True)
class OrderEdit:
    order: tuple[str, ...]
    anchor: str | None


@dataclass(frozen=True)
class DifficultyEdit:
    previous: dict[str, str | None]


class MainWindow(QMainWindow):
    companion_requested = Signal(str, object)
    inspect_requested = Signal(str, bool)
    refresh_requested = Signal(str, bool, object)
    apply_requested = Signal(object, str, object, bool)
    save_current_requested = Signal(object, object, bool)

    def __init__(self, service: ExamDocumentService, *, companion_factory=None, initial_document: Path | None = None) -> None:
        super().__init__()
        self.service = service
        self.snapshot: DocumentSnapshot | None = None
        self._original_order: tuple[str, ...] = ()
        self._thread: QThread | None = None
        self._worker: DocumentWorker | None = None
        self._editing_in_hwp = False
        self._companion_factory = companion_factory

        self._import_format_result = None
        self.setWindowTitle("HWP 시험문제 수합·편집 · 공용판")
        self.resize(1040, 760)
        self.setMinimumSize(820, 570)
        self._busy = False
        self._edit_history: list[OrderEdit | DifficultyEdit] = []
        self._last_order: tuple[str, ...] = ()
        self._init_hwp_session()
        self._build_ui()
        self._apply_style()
        self._set_busy(False)
        if initial_document is None:
            self.show_home()
        else:
            self.load_document(str(initial_document))

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("examEditor")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 18, 24, 16)
        layout.setSpacing(10)

        self.document_meta = QLabel("시험지 편집본")
        self.document_meta.setObjectName("eyebrow")
        layout.addWidget(self.document_meta)
        self.file_label = QLabel("시험지 편집본을 여세요")
        self.file_label.setObjectName("documentTitle")
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.file_label.setWordWrap(True)
        layout.addWidget(self.file_label)

        tools = QHBoxLayout()
        heading = QLabel("문항 순서")
        heading.setObjectName("listTitle")
        tools.addWidget(heading)
        hint = QLabel("끌어서 옮기거나 번호를 눌러 이동")
        hint.setObjectName("editorHint")
        tools.addWidget(hint)
        tools.addStretch()
        self.selection_label = QLabel()
        self.selection_label.setObjectName("editorHint")
        tools.addWidget(self.selection_label)
        self.difficulty_combo = QComboBox()
        self.difficulty_combo.setAccessibleName("선택한 문항의 난이도")
        self.difficulty_combo.setToolTip("선택한 문항의 난이도를 함께 변경합니다.")
        self.difficulty_combo.setPlaceholderText("난이도 일괄 지정")
        self.difficulty_combo.addItems(DIFFICULTIES)
        self.difficulty_combo.activated.connect(self._set_selected_difficulty)
        tools.addWidget(self.difficulty_combo)
        more = QPushButton("문항 도구")
        menu = QMenu(more)
        more.setMenu(menu)
        self.up_button = menu.addAction("선택 문항 위로", lambda: self.question_list.move_current(-1))
        self.down_button = menu.addAction("선택 문항 아래로", lambda: self.question_list.move_current(1))
        for action, key in ((self.up_button, "Alt+Up"), (self.down_button, "Alt+Down")):
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            root.addAction(action)
        self.swap_button = menu.addAction("두 문항 교환", self._swap_selected)
        self.delete_button = menu.addAction("선택 문항 삭제…", self._delete_selected)
        menu.addSeparator()
        self.easy_first_button = menu.addAction("같은 유형 안에서 쉬운 순", lambda: self.question_list.sort_by_difficulty(False))
        self.hard_first_button = menu.addAction("같은 유형 안에서 어려운 순", lambda: self.question_list.sort_by_difficulty(True))
        self.reset_button = menu.addAction("반영 전 순서로 복원", self._restore_order)
        menu.addSeparator()
        self.import_button = menu.addAction("새 시험지 수합…", self.open_import_dialog)
        self.refresh_button = menu.addAction("문서 다시 읽기", self.reload_document)
        self.log_action = menu.addAction("작업 기록")
        self.log_action.setCheckable(True)
        tools.addWidget(more)
        layout.addLayout(tools)

        header = QWidget()
        header.setObjectName("columnTitles")
        columns = QHBoxLayout(header)
        columns.setContentsMargins(12, 5, 12, 5)
        position = QLabel("순서")
        position.setFixedWidth(82)
        columns.addWidget(position)
        columns.addWidget(QLabel("문항"), 1)
        level = QLabel("난이도")
        level.setFixedWidth(144)
        level.setAlignment(Qt.AlignmentFlag.AlignCenter)
        columns.addWidget(level)
        layout.addWidget(header)
        self.question_list = QuestionListWidget()
        self.question_list.setAccessibleName("문항 순서와 난이도")
        self.question_list.difficulty_requested.connect(self._save_difficulty)
        self.question_list.order_changed.connect(self._order_edited)
        self.question_list.move_interaction_changed.connect(self._move_interaction_changed)
        self.question_list.itemSelectionChanged.connect(self._sync_difficulty_combo)
        layout.addWidget(self.question_list, 1)

        actions = QHBoxLayout()
        self.status_label = QLabel("기존 편집본을 열거나 새 시험지를 수합하세요.")
        self.status_label.setObjectName("editorStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        actions.addWidget(self.status_label, 1)
        self.recover_button = QPushButton("문항 다시 확인")
        self.recover_button.clicked.connect(self.reload_document)
        actions.addWidget(self.recover_button)
        self.undo_button = QPushButton("실행 취소")
        self.undo_button.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_button.clicked.connect(self._undo_edit)
        actions.addWidget(self.undo_button)
        self.apply_button = QPushButton("순서만 반영")
        self.apply_button.clicked.connect(self.apply_current_document)
        actions.addWidget(self.apply_button)
        self.edit_in_hwp_button = QPushButton("한글에서 확인")
        self.edit_in_hwp_button.setObjectName("proofButton")
        self.edit_in_hwp_button.clicked.connect(self.open_current_in_hwp)
        actions.addWidget(self.edit_in_hwp_button)
        layout.addLayout(actions)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(115)
        self.log.setPlaceholderText("작업 기록")
        self.log.hide()
        self.log_action.toggled.connect(self.log.setVisible)
        layout.addWidget(self.log)

        self.editor_page = root
        self.pages = QStackedWidget()
        self.home_page = HomePage()
        self.home_import_button = self.home_page.import_button
        self.home_edit_button = self.home_page.edit_button
        self.home_import_button.clicked.connect(self.open_import_dialog)
        self.home_edit_button.clicked.connect(self.resume_or_open_editor)
        self.home_page.document_dropped.connect(self._open_dropped_document)
        self.pages.addWidget(self.home_page)
        self.pages.addWidget(self.editor_page)

        shell = QWidget()
        frame = QVBoxLayout(shell)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("appBar")
        top = QHBoxLayout(bar)
        top.setContentsMargins(24, 10, 24, 10)
        top.setSpacing(10)
        mark = QLabel("ㅎ")
        mark.setObjectName("brandMark")
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(mark)
        brand = QLabel("시험지 편집기")
        brand.setObjectName("brandName")
        top.addWidget(brand)
        top.addStretch()
        self.home_button = QPushButton("작업 홈")
        self.home_button.clicked.connect(self.show_home)
        top.addWidget(self.home_button)
        self.open_button = QPushButton("다른 편집본 열기…")
        self.open_button.clicked.connect(self.choose_document)
        top.addWidget(self.open_button)
        self.help_button = QPushButton("사용 안내")
        self.help_button.clicked.connect(self.show_help)
        top.addWidget(self.help_button)
        frame.addWidget(bar)
        frame.addWidget(self.pages, 1)
        self.setCentralWidget(shell)

    def _open_dropped_document(self, path: str) -> None:
        if not self._busy and not self._editing_in_hwp and self._allow_discard():
            self.load_document(path)

    def show_home(self) -> None:
        self.pages.setCurrentWidget(self.home_page)
        self._refresh_home_template()
        self.home_button.hide()
        self.open_button.setVisible(self.snapshot is not None)
        if self.snapshot is not None:
            self.home_edit_button.setText("편집 이어가기")
            self.home_page.edit_description.setText(self.snapshot.path.name)
            self.home_page.edit_description.setToolTip(str(self.snapshot.path))

    def _refresh_home_template(self):
        templates = recent_templates(template_data_root())
        label = self.home_page.import_description
        if templates:
            source = templates[0].source
            name = label.fontMetrics().elidedText(source.name, Qt.TextElideMode.ElideMiddle, 280)
            label.setText(f"최근 양식 · {name}")
            label.setToolTip(str(source))
            self.home_import_button.setText("수합 시작")
        else:
            label.setText("내 학교 양식을 등록하고 여러 출제 파일을 한 시험지에 모읍니다.")
            label.setToolTip("")
            self.home_import_button.setText("내 학교 양식 선택…")

    def show_editor(self) -> None:
        self.pages.setCurrentWidget(self.editor_page)
        self.home_button.show()
        self.open_button.show()

    def resume_or_open_editor(self) -> None:
        if self.snapshot is None:
            self.choose_document()
        else:
            self.show_editor()

    def show_help(self) -> None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("사용 안내")
        dialog.setText("시험지 수합과 문항 편집")
        dialog.setInformativeText(
            "처음에는 ‘내 학교 양식 선택’에서 양식 파일을 여세요. 한글에서 예시 문항과 새 문항을 넣을 자리를 확인하면 "
            "같은 양식을 다음 수합에도 사용할 수 있습니다. 출제 파일을 추가하고 ‘저장하고 편집기로’를 누르세요.\n\n"
            "편집기에서는 번호를 누르거나 문항을 끌어 순서를 바꿉니다. 난이도는 선택하면 저장됩니다. "
            "수합본이 한글에 열려 있으면 바뀐 순서가 자동으로 저장되고 옮긴 문항이 표시됩니다. "
            "문항 삭제는 ‘순서·삭제 반영’을 눌러 확정하세요."
        )
        dialog.addButton("닫기", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()

    def _sync_difficulty_combo(self) -> None:
        values = {self.question_list.difficulties.get(item.data(Qt.ItemDataRole.UserRole).id, "미지정")
                  for item in self.question_list.selectedItems()}
        self.difficulty_combo.setCurrentIndex(DIFFICULTIES.index(values.pop()) if len(values) == 1 else -1)
        self._update_editor_controls()

    def _set_selected_difficulty(self, index: int) -> None:
        ids = tuple(item.data(Qt.ItemDataRole.UserRole).id for item in self.question_list.selectedItems())
        if not ids:
            QMessageBox.information(self, "문항 선택", "난이도를 지정할 문항을 먼저 선택하세요.")
            return
        self._save_difficulty(ids, DIFFICULTIES[index])

    def _save_difficulty(self, ids, level: str) -> None:
        if self.snapshot is None or not self.question_list.isEnabled():
            return
        try:
            # Re-read before writing so other unchanged QIDs are preserved.
            values = load_difficulties(self.snapshot.path)
            previous = {qid: values.get(qid) for qid in ids if values.get(qid, "미지정") != level}
            if not previous:
                return
            values.update({qid: level for qid in previous})
            save_difficulties(self.snapshot.path, values)
        except (OSError, ValueError) as error:
            self._sync_difficulty_combo()
            QMessageBox.warning(self, "난이도 저장 실패", str(error))
            return
        self._push_edit(DifficultyEdit(previous))
        self.question_list.set_difficulties(values)
        self._sync_difficulty_combo()
        self.log.appendPlainText(f"[난이도 저장] {', '.join(ids)} → {level}")
        pending = " · 순서 반영 전" if self.question_list.ordered_ids() != self._original_order else ""
        self.status_label.setText(f"난이도 {level} 저장됨{pending}")

    def _apply_style(self) -> None:
        self.setStyleSheet(EDITOR_STYLE)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_editor_controls()
        if not busy and self._auto_pending and not self._auto_timer.isActive() and not self._closing:
            self._auto_timer.start(0)

    def _update_editor_controls(self) -> None:
        available = not self._busy and not self._editing_in_hwp
        has_document = self.snapshot is not None
        saving_move = (self._applying_order is not None
                       and len(self._applying_order) == len(self._original_order))
        can_edit = (has_document and not self._sync_failed and not self._closing
                    and (not self._busy or saving_move or self._live_operation in {"attach", "navigate"}))
        order = self.question_list.ordered_ids()
        selected = self.question_list.selectedItems()
        count = len(selected)
        dirty = has_document and order != self._original_order
        self.home_button.setEnabled(available)
        self.home_import_button.setEnabled(available)
        self.home_edit_button.setEnabled(available)
        self.open_button.setEnabled(available)
        self.import_button.setEnabled(available)
        self.edit_in_hwp_button.setEnabled(not self._busy and has_document and (not self._editing_in_hwp or count == 1))
        self.refresh_button.setEnabled(not self._busy and has_document)
        self.question_list.setEnabled(can_edit)
        if has_document:
            total = len(self.snapshot.questions)
            connection = "한글 연결됨" if self._editing_in_hwp else f"{self.snapshot.path.suffix.lstrip('.').upper()} 편집본"
            self.document_meta.setText(f"{connection}   /   편집 항목 {total}개")
        self.selection_label.setText(f"{count}개 선택" if count > 1 else "")
        self.selection_label.setVisible(count > 1)
        self.difficulty_combo.setVisible(count > 1)
        self.difficulty_combo.setEnabled(can_edit and count > 1)
        self.easy_first_button.setEnabled(can_edit and len(order) > 1)
        self.hard_first_button.setEnabled(can_edit and len(order) > 1)
        self.up_button.setEnabled(can_edit and count > 0 and not self.question_list.item(0).isSelected())
        self.down_button.setEnabled(can_edit and count > 0 and not self.question_list.item(len(order) - 1).isSelected())
        self.swap_button.setEnabled(can_edit and count == 2)
        self.delete_button.setEnabled(can_edit and 0 < count < len(order))
        self.reset_button.setEnabled(can_edit and dirty)
        self.undo_button.setEnabled(can_edit and bool(self._edit_history))
        manual = not self._editing_in_hwp or self._live_issue or self._native_order_conflict or self._deletions_pending()
        self.apply_button.setVisible(dirty and manual)
        self.apply_button.setEnabled(can_edit and dirty and manual and not self._busy)
        if self._deletions_pending():
            self.apply_button.setText("순서·삭제 반영")
        else:
            self.apply_button.setText("순서 반영 다시 시도" if self._editing_in_hwp else "순서만 반영")
        if self._editing_in_hwp:
            self.edit_in_hwp_button.setText("선택 문항 보기")
        else:
            self.edit_in_hwp_button.setText("반영하고 한글에서 확인" if dirty and can_edit else "한글에서 확인")
        self.recover_button.setVisible(has_document and (self._sync_failed or self._live_issue))
        self.recover_button.setEnabled(not self._busy)

    def _reset_edit_history(self) -> None:
        self._edit_history.clear()
        self._last_order = self.question_list.ordered_ids()

    def _push_edit(self, edit) -> None:
        self._edit_history.append(edit)
        del self._edit_history[:-50]

    def _order_edited(self) -> None:
        current = self.question_list.ordered_ids()
        if self.snapshot is not None and current != self._last_order:
            item = self.question_list.currentItem()
            anchor = item.data(Qt.ItemDataRole.UserRole).id if item else None
            self._push_edit(OrderEdit(self._last_order, anchor))
            self._last_order = current
            self._schedule_order_sync(anchor)
        self._update_order_status()

    def _restore_order(self) -> None:
        self._restore_original_order()
        self._order_edited()

    def _undo_edit(self) -> None:
        if not self.undo_button.isEnabled() or not self._edit_history:
            return
        edit = self._edit_history[-1]
        if isinstance(edit, OrderEdit):
            questions = {q.id: q for q in self.snapshot.questions}
            self.question_list.set_questions(tuple(questions[qid] for qid in edit.order))
            self._last_order = edit.order
            if edit.anchor in edit.order:
                self.question_list.setCurrentRow(edit.order.index(edit.anchor))
            self._schedule_order_sync(edit.anchor)
        else:
            try:
                values = load_difficulties(self.snapshot.path)
                for qid, previous in edit.previous.items():
                    if previous is None:
                        values.pop(qid, None)
                    else:
                        values[qid] = previous
                save_difficulties(self.snapshot.path, values)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "난이도 복원 실패", str(error))
                return
            self.question_list.set_difficulties(values)
            self._sync_difficulty_combo()
        self._edit_history.pop()
        self._update_order_status()
        self.question_list.setFocus()

    def _start_worker(self) -> DocumentWorker:
        if self._thread is not None:
            raise RuntimeError("이미 작업이 진행 중입니다.")
        thread = QThread(self)
        worker = DocumentWorker(self.service)
        worker.moveToThread(thread)
        worker.failed.connect(self._operation_failed)
        worker.progress.connect(self._show_progress)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._worker_finished)
        self._thread = thread
        self._worker = worker
        thread.start()
        return worker

    def _init_hwp_session(self) -> None:
        self._sync_failed = False
        self._live_issue = False
        self._refreshing_external = False
        self._draft_on_close = False
        self._native_order_conflict = False
        self._apply_on_connect = False
        self._live_thread = None
        self._live_worker = None
        self._live_operation = None
        self._after_probe = None
        self._closing = False
        self._applying_order = None
        self._live_batch = None
        self._move_anchor = None
        self._auto_pending = False
        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.timeout.connect(self._sync_moved_order)
        self._hwp_timer = QTimer(self)
        self._hwp_timer.setInterval(1000)
        self._hwp_timer.timeout.connect(self._poll_hwp_session)

    def _schedule_order_sync(self, anchor) -> None:
        self._move_anchor = anchor
        if self._live_batch is not None and not self._deletions_pending():
            order = self.question_list.ordered_ids()
            if self._live_batch.update(order, anchor):
                # The private copy can still change. The worker will return
                # the actual published order, including an undo to the baseline.
                self._applying_order = order
                self._auto_pending = False
                self._auto_timer.stop()
                return
        if self._live_issue or self._native_order_conflict or self._sync_failed:
            # Keep subsequent gestures in the draft until a failed save is
            # explicitly retried or the native document has been re-read.
            self._auto_pending = False
            self._auto_timer.stop()
            return
        self._auto_pending = True
        self._auto_timer.start(MOVE_SETTLE_MS)

    def _move_interaction_changed(self, active: bool) -> None:
        if self._live_batch is not None:
            self._live_batch.set_editing(active)
        if not active and self._auto_pending and not self._auto_timer.isActive() and not self._busy:
            self._auto_timer.start(0)

    def _sync_moved_order(self) -> None:
        if (not self._auto_pending or self._closing or self._busy or self._live_operation == "probe"
                or self.question_list.move_in_progress):
            return
        if QApplication.activeModalWidget() is not None:
            self._auto_timer.start(MOVE_SETTLE_MS)
            return
        self._auto_pending = False
        if self._deletions_pending() or self._sync_failed or self._live_issue or self._native_order_conflict:
            return
        if not self._dirty_order():
            # A move and undo can finish within one debounce interval.
            if self._editing_in_hwp and self._move_anchor in self._original_order:
                self._request_companion("navigate", self._move_anchor, False)
            return
        if self._editing_in_hwp:
            self._apply_live_order()
        else:
            self._request_companion("attach", self.snapshot)

    def _ensure_companion_worker(self) -> None:
        if self._live_thread is not None:
            return
        thread = QThread(self)
        worker = CompanionWorker(self.service, self._companion_factory)
        worker.moveToThread(thread)
        self.companion_requested.connect(worker.perform)
        worker.completed.connect(self._live_completed)
        worker.failed.connect(self._live_failed)
        worker.warning.connect(self._live_warning)
        worker.stopped.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._companion_stopped)
        self._live_thread, self._live_worker = thread, worker
        thread.start()

    @Slot()
    def _companion_stopped(self) -> None:
        # Windows thread-local destruction can outlive QThread.finished.
        # Join before the last window closes and Python starts tearing down.
        self._live_thread.wait()
        self._live_thread.deleteLater()
        self._live_thread = None
        self._live_worker = None
        if self._closing:
            self.close()

    def _request_companion(self, operation, *args) -> None:
        self._ensure_companion_worker()
        if self._live_operation == "probe" and operation != "probe":
            self._after_probe = (operation, args)
            self._set_busy(True)
            return
        if self._live_operation is not None:
            return
        self._live_operation = operation
        if operation != "probe":
            self._set_busy(True)
        self.companion_requested.emit(operation, args)

    def _current_qid(self):
        item = self.question_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole).id if item else None

    def _poll_hwp_session(self) -> None:
        if self._editing_in_hwp and not self._busy and not self._closing and self._live_operation is None:
            self._request_companion("probe")

    def _live_completed(self, operation, result) -> None:
        follow_qid = None
        if operation == "apply":
            if self._live_batch is not None and self._move_anchor != self._live_batch.latest()[1]:
                # Later moves can return to the published order with a
                # different last question after the batch has been sealed.
                follow_qid = self._move_anchor
            self._live_batch = None
        if self._closing:
            self._live_operation = None
            return
        # Keep the operation active while updating rows. Briefly disabling the
        # list here would interrupt a drag or keyboard input for the next move.
        if operation in {"open", "attach", "apply", "refresh"} and result is not None:
            self._document_loaded(result, live=True, preserve_draft=operation != "apply", committed=operation == "apply")
        self._live_operation = None
        if operation == "find":
            self._set_busy(False)
            if result:
                self._connect_document(apply_on_connect=True)
            else:
                self._save_order()
            return
        if operation == "attach" and result is None:
            self._set_busy(False)
            self._update_order_status()
            return
        if operation == "probe":
            deferred, self._after_probe = self._after_probe, None
            if result == "closed":
                self._auto_pending = False
                self._auto_timer.stop()
                self._applying_order = None
                self._live_batch = None
                self._editing_in_hwp = False
                self._draft_on_close = self._dirty_order()
                self._hwp_timer.stop()
                try:
                    unchanged = self.snapshot.content_sha256 and file_fingerprint(self.snapshot.path).sha256 == self.snapshot.content_sha256
                except OSError:
                    unchanged = False
                if unchanged and not self._sync_failed:
                    self._draft_on_close = False
                    self._set_busy(False)
                    self._update_order_status()
                    self.question_list.setFocus()
                    return
                self._start_external_refresh()
            elif deferred:
                self._request_companion(deferred[0], *deferred[1])
            elif self._auto_pending and not self._auto_timer.isActive():
                self._sync_moved_order()
            return
        if operation in {"open", "attach", "apply", "refresh"}:
            self._applying_order = None
            self._set_busy(False)
            self._hwp_timer.start()
            if operation == "attach":
                self._auto_pending = True
                self._sync_moved_order()
                return
            if operation == "open":
                apply_now, self._apply_on_connect = self._apply_on_connect, False
                if apply_now and self._dirty_order():
                    self._apply_live_order()
                    return
                if self._current_qid():
                    self._request_companion("navigate", self._current_qid())
                    return
            if operation == "apply" and not self._dirty_order():
                self.status_label.setText("열려 있는 한글 문서에 순서를 반영했습니다.")
                if follow_qid in self._original_order:
                    self._request_companion("navigate", follow_qid, False)
            elif operation == "apply" and not self._deletions_pending():
                self.status_label.setText("다음 이동도 저장합니다. 계속 편집하세요.")
            elif operation != "apply" and not self._native_order_conflict:
                self.status_label.setText("한글 문서가 연결되었습니다. 이 목록에서 순서와 난이도를 조정하세요.")
        else:
            self._set_busy(False)

    def _live_warning(self, details: str) -> None:
        if self._dirty_order():
            self._update_order_status()
            self.status_label.setText(f"{self.status_label.text()} · {details}")
        else:
            self.status_label.setText(f"순서를 반영했습니다. {details}")
        self.log.appendPlainText(f"[순서 반영 후 안내] {details}")

    def _live_failed(self, operation: str, details: str) -> None:
        self._live_batch = None
        self._live_operation = None
        self._apply_on_connect = False
        deferred, self._after_probe = self._after_probe, None
        if self._closing:
            return
        if (operation in {"apply", "refresh", "attach"} or deferred is not None
                or (operation == "probe" and self._auto_pending)):
            # Inactive tabs and open Hangul dialogs reject operations too.
            # Keep the draft editable; every retry verifies the live QIDs.
            self._live_issue = True
            self._auto_pending = False
            self._auto_timer.stop()
        self._applying_order = None
        if operation == "probe":
            # A busy or modal Hangul instance may temporarily reject COM
            # calls. Its session stays attached until closure is confirmed.
            self._hwp_timer.setInterval(2000)
        self._set_busy(False)
        message = f"한글 문서 확인 필요 · {details}"
        self.status_label.setText(message)
        if operation != "probe":
            self.log.appendPlainText(message)

    def _apply_live_order(self) -> None:
        if not self._dirty_order() or self._sync_failed or not self._confirm_current_order():
            return
        self._applying_order = self.question_list.ordered_ids()
        self.status_label.setText("문항 삭제를 한글에 반영하는 중…" if self._deletions_pending()
                                  else "한글에 저장 중 · 다음 문항을 옮길 수 있습니다.")
        anchor = self._move_anchor if self._move_anchor in self._applying_order else self._current_qid()
        if self._deletions_pending():
            self._request_companion("apply", self.snapshot, self._applying_order, anchor)
        else:
            from app.services.live_document import LiveOrderBatch

            self._live_batch = LiveOrderBatch(self._applying_order, anchor)
            self._live_batch.set_editing(self.question_list.move_in_progress)
            self._request_companion("apply", self.snapshot, self._applying_order, anchor, self._live_batch)

    def _confirm_current_order(self) -> bool:
        if not self._native_order_conflict:
            return True
        return QMessageBox.question(
            self, "한글에서도 순서 변경", "한글에서도 문항 순서가 바뀌었습니다. 현재 앱 목록의 순서와 삭제를 반영할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def _dirty_order(self) -> bool:
        return self.snapshot is not None and self.question_list.ordered_ids() != self._original_order

    def _deletions_pending(self) -> bool:
        return self.snapshot is not None and self.question_list.count() < len(self._original_order)

    def _allow_discard(self) -> bool:
        if not self._dirty_order():
            return True
        return QMessageBox.question(
            self, "아직 반영하지 않은 변경", "문항 순서와 삭제 변경을 버리고 계속할까요?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def choose_document(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "기존 시험지 또는 문제 파일 선택", "", "한글 문서 (*.hwp *.hwpx)"
        )
        if path and self._allow_discard():
            self.load_document(path)

    def open_import_dialog(self) -> None:
        if not self._allow_discard():
            return
        from app.ui.import_dialog import ImportDialog

        dialog = ImportDialog(self.service.project_root, self)
        if not dialog.template_combo.count():
            path, _ = QFileDialog.getOpenFileName(self, "학교 시험지 양식 선택", "", "한글 문서 (*.hwp *.hwpx)")
            if not path:
                dialog.deleteLater()
                return
            QTimer.singleShot(0, dialog, lambda: dialog.open_registrar(Path(path)))
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.completed_output:
            if dialog.format_summary:
                self._import_format_result = (Path(dialog.completed_output), dialog.format_summary)
            self.load_document(str(dialog.completed_output))
        elif self.pages.currentWidget() is self.home_page:
            self._refresh_home_template()
        dialog.deleteLater()

    def load_document(self, path: str) -> None:
        self._auto_timer.stop()
        self._auto_pending = False
        self._move_anchor = None
        self.show_editor()
        if self.snapshot is None:
            self.file_label.setText(Path(path).name)
        self.status_label.setText(f"{Path(path).name} · 문항을 불러오는 중…")
        self.log.appendPlainText(f"[OPEN] {path}")
        self._set_busy(True)
        worker = self._start_worker()
        self.inspect_requested.connect(worker.inspect)
        worker.inspected.connect(self._document_loaded)
        self._thread.finished.connect(self.question_list.setFocus)
        self.inspect_requested.emit(path, self._visible_mode())

    def reload_document(self) -> None:
        if self.snapshot is None or self._thread is not None:
            return
        if self._editing_in_hwp:
            self._request_companion("refresh", self.snapshot)
        elif self._draft_on_close or self._allow_discard():
            self._start_external_refresh()

    def _start_external_refresh(self) -> None:
        self._hwp_timer.stop()
        self._refreshing_external = True
        self.status_label.setText("저장된 문항을 다시 확인하는 중…")
        self._set_busy(True)
        worker = self._start_worker()
        self.refresh_requested.connect(worker.refresh)
        worker.refreshed.connect(self._document_loaded)
        self.refresh_requested.emit(str(self.snapshot.path), False, self.snapshot)

    def open_current_in_hwp(self) -> None:
        if self.snapshot is None or self._busy:
            return
        if self._editing_in_hwp:
            if self._current_qid():
                self._request_companion("navigate", self._current_qid())
            return
        self._auto_timer.stop()
        self._auto_pending = False
        self._connect_document(apply_on_connect=self._dirty_order() and not self._sync_failed)

    def _connect_document(self, *, apply_on_connect: bool) -> None:
        self._apply_on_connect = apply_on_connect
        self.status_label.setText("수합본을 한글에 연결하는 중…")
        self._request_companion("open", self.snapshot)

    def _synchronization_failed(self, details: str) -> None:
        self._hwp_timer.stop()
        self._sync_failed = True
        self._editing_in_hwp = False
        self._refreshing_external = False
        self.status_label.setText(f"문항 확인 필요 · {details.splitlines()[0]}")
        self.log.appendPlainText(f"[다시 읽기] {details}")
        self._set_busy(self._thread is not None)

    def _document_loaded(self, snapshot: DocumentSnapshot, *, live: bool = False,
                         preserve_draft: bool = False, committed: bool = False) -> None:
        self.show_editor()
        same_document = self.snapshot is not None and self.snapshot.path == snapshot.path
        order = self.question_list.ordered_ids()
        row = self.question_list.currentRow()
        current = order[row] if same_document and 0 <= row < len(order) else None
        selected = {item.data(Qt.ItemDataRole.UserRole).id for item in self.question_list.selectedItems()}
        scroll = self.question_list.verticalScrollBar().value()
        external = self._refreshing_external
        previous_order = self._original_order
        history = list(self._edit_history)
        keep_draft = same_document and (preserve_draft or self._draft_on_close) and self._dirty_order()
        self._native_order_conflict = bool(keep_draft and (self._native_order_conflict or self._original_order != snapshot.order))
        if committed:
            # The teacher may have moved or undone another question while Hancom
            # was saving. Only the submitted order is acknowledged by this result.
            keep_draft = order != snapshot.order
            self._auto_pending = keep_draft
        self._draft_on_close = False
        self.snapshot = snapshot
        self.file_label.setText(snapshot.path.name)
        self.file_label.setToolTip(str(snapshot.path))
        self._editing_in_hwp = live
        self._sync_failed = False
        self._live_issue = False
        self._refreshing_external = False
        self._hwp_timer.stop()
        try:
            difficulties = load_difficulties(snapshot.path)
        except (OSError, ValueError) as error:
            difficulties = {}
            self.log.appendPlainText(f"[난이도 읽기 실패] {error}")
            QMessageBox.warning(self, "난이도 읽기 실패", f"문항은 불러왔지만 난이도를 읽지 못했습니다.\n{error}")
        self.question_list.set_difficulties(difficulties)
        self._original_order = snapshot.order
        by_id = {q.id: q for q in snapshot.questions}
        shown = order if keep_draft and all(qid in by_id for qid in order) else snapshot.order
        update_in_place = same_document and (committed or preserve_draft) and shown == order
        if update_in_place:
            # Keep a drag or row click that started while Hancom was saving.
            self.question_list.refresh_labels(by_id)
        else:
            self.question_list.set_questions(tuple(by_id[qid] for qid in shown))
        if not update_in_place and current in shown:
            current_row = shown.index(current)
            self.question_list.setCurrentRow(current_row, QItemSelectionModel.SelectionFlag.NoUpdate)
            for index, qid in enumerate(shown):
                self.question_list.item(index).setSelected(qid in selected)
            self.question_list.verticalScrollBar().setValue(scroll)
            self.question_list.scrollToItem(self.question_list.item(current_row))
        self._reset_edit_history()
        if same_document:
            valid_ids = set(snapshot.order)
            self._edit_history = [
                edit for edit in history
                if (isinstance(edit, DifficultyEdit) and valid_ids.issuperset(edit.previous))
                or (isinstance(edit, OrderEdit) and (committed or previous_order == snapshot.order)
                    and shown == order and valid_ids.issuperset(edit.order))
            ]
        self.log.appendPlainText(f"[READ] 문항 {len(snapshot.questions)}개")
        self._update_order_status()
        if self._import_format_result and self._import_format_result[0] == snapshot.path:
            summary = self._import_format_result[1]
            self.status_label.setText(f"수합 완료 · {summary}")
            self.log.appendPlainText(f"{summary} · {snapshot.path.name}.format-check.json")
            self._import_format_result = None
        if self._native_order_conflict:
            self.status_label.setText("한글에서도 순서가 변경되었습니다. 반영할 앱 목록을 확인하세요.")
        elif external:
            note = " · 순서 반영 전" if self._dirty_order() else ""
            self.status_label.setText(f"한글 저장 내용을 불러왔습니다{note}.")
            self.question_list.setFocus()

    def _swap_selected(self) -> None:
        if not self.question_list.swap_selected():
            QMessageBox.information(self, "문항 선택", "교환할 문항 두 개를 선택하세요.")

    def _delete_selected(self) -> None:
        selected = tuple(
            item.data(Qt.ItemDataRole.UserRole).id
            for item in self.question_list.selectedItems()
        )
        if not selected:
            QMessageBox.information(self, "문항 선택", "삭제할 문항을 선택하세요.")
            return
        answer = QMessageBox.question(
            self,
            "문항 삭제",
            f"선택한 문항 {', '.join(selected)}을 결과 시험지에서 삭제할까요?\n\n"
            "‘순서·삭제 반영’을 누르면 시험지에서도 삭제됩니다. 반영 전 파일은 자동 백업됩니다.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.question_list.remove_selected()
        except ValueError as error:
            QMessageBox.warning(self, "문항 삭제", str(error))
            return
        self.log.appendPlainText(f"[DELETE] 저장 예정: {' '.join(removed)}")

    def _restore_original_order(self) -> None:
        if self.snapshot is None:
            return
        by_id = {question.id: question for question in self.snapshot.questions}
        self.question_list.set_questions(
            tuple(by_id[question_id] for question_id in self._original_order)
        )
        self._update_order_status()

    def _update_order_status(self) -> None:
        if self.snapshot is None:
            return
        order = self.question_list.ordered_ids()
        changed = order != self._original_order
        state = "반영할 변경 있음" if changed else "변경 없음"
        if changed and self._live_issue and self._editing_in_hwp and not self._deletions_pending():
            state = "한글 저장 대기 · 순서 반영 다시 시도"
        if self._editing_in_hwp and not (self._live_issue or self._native_order_conflict or self._deletions_pending()):
            if self._applying_order is not None and order != self._applying_order:
                state = "다음 이동 저장 대기 · 계속 편집 가능"
            elif changed:
                state = "한글에 저장 중 · 계속 편집 가능"
        deleted_count = len(self._original_order) - len(order)
        deleted_text = f" · 삭제 예정 {deleted_count}개" if deleted_count else ""
        by_id = {
            question.id: question for question in self.snapshot.questions
        }
        manual_count = sum(
            by_id[question_id].manual_required
            for question_id in order
            if question_id in by_id
        )
        manual_text = (
            f" · 직접 입력 필요 {manual_count}개" if manual_count else ""
        )
        self.status_label.setText(f"문항 {len(order)}개 · {state}{deleted_text}{manual_text}")
        self._update_editor_controls()

    def choose_output_and_apply(self) -> None:
        """Legacy Save As path retained for integration tests and recovery."""
        if self.snapshot is None:
            return
        suggested = self.snapshot.path.with_name(
            self.snapshot.path.stem + "_편집적용본" + self.snapshot.path.suffix
        )
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "새 HWP로 저장",
            str(suggested),
            "한글 문서 (*.hwp *.hwpx)",
        )
        if destination:
            self.apply_to(destination)

    def apply_current_document(self) -> None:
        if self.snapshot is None or self._busy or self._sync_failed:
            return
        self._auto_timer.stop()
        self._auto_pending = False
        if self._editing_in_hwp:
            self._apply_live_order()
        else:
            self._request_companion("find", self.snapshot.path)

    def _save_order(self) -> None:
        if self.snapshot is None or self._thread is not None or self._editing_in_hwp or self._sync_failed:
            return
        try:
            if file_is_in_use(self.snapshot.path):
                self._connect_document(apply_on_connect=True)
                return
            if self.snapshot.content_sha256 and file_fingerprint(self.snapshot.path).sha256 != self.snapshot.content_sha256:
                self.status_label.setText("파일이 외부에서 변경되었습니다. ‘문항 도구’에서 문서를 다시 읽어 주세요.")
                return
        except OSError as error:
            self.status_label.setText(f"파일 상태 확인 실패 · {error}")
            return
        order = self.question_list.ordered_ids()
        if order == self._original_order or not self._confirm_current_order():
            return
        self.status_label.setText("문항 순서를 반영하는 중…")
        self.log.appendPlainText(f"[REORDER] {' '.join(order)}")
        self._set_busy(True)
        worker = self._start_worker()
        self.save_current_requested.connect(worker.apply_in_place)
        worker.applied.connect(self._apply_completed)
        self.save_current_requested.emit(self.snapshot, order, False)

    def apply_to(self, destination: str) -> None:
        if self.snapshot is None:
            raise RuntimeError("먼저 HWP를 불러오세요.")
        if self._editing_in_hwp:
            QMessageBox.information(
                self,
                "한글 편집 중",
                "한글에서 저장·닫은 뒤 ‘현재 파일 다시 읽기’를 눌러 수정본을 반영하세요.",
            )
            return
        output = Path(destination)
        if output.exists():
            QMessageBox.warning(
                self,
                "덮어쓰기 차단",
                "기존 파일에는 저장하지 않습니다. 다른 파일명을 선택하세요.",
            )
            return
        order = self.question_list.ordered_ids()
        self.status_label.setText("백업 후 순서 변경·삭제를 적용하고 검증하는 중…")
        deleted = tuple(
            question_id for question_id in self._original_order if question_id not in order
        )
        self.log.appendPlainText(f"[EDIT] order={' '.join(order)}")
        if deleted:
            self.log.appendPlainText(f"[DELETE] apply={' '.join(deleted)}")
        self._set_busy(True)
        worker = self._start_worker()
        self.apply_requested.connect(worker.apply)
        worker.applied.connect(self._apply_completed)
        self.apply_requested.emit(
            self.snapshot, str(output), order, self._visible_mode()
        )

    def _visible_mode(self) -> bool:
        return False

    def _show_progress(self, message: str) -> None:
        self.status_label.setText(message)
        self.log.appendPlainText(f"[HWP] {message}")

    def _apply_completed(self, output: str) -> None:
        if self.snapshot is None:
            return
        path = Path(output)
        by_id = {question.id: question for question in self.snapshot.questions}
        questions = tuple(by_id[qid] for qid in self.question_list.ordered_ids())
        if path != self.snapshot.path:
            try:
                save_difficulties(path, self.question_list.difficulties)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "난이도 복사 실패", f"시험지는 저장했습니다.\n{error}")
        try:
            audit_path = path.with_suffix(path.suffix + ".audit.json")
            digest = json.loads(audit_path.read_text(encoding="utf-8"))["after_sha256"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._document_loaded(DocumentSnapshot(path, questions))
            self._synchronization_failed(f"시험지는 저장했습니다. 저장 기록을 다시 확인해 주세요. {error}")
            return
        self._document_loaded(DocumentSnapshot(path, questions, content_sha256=digest))
        self.log.appendPlainText(f"[SAVE] {path}")
        self.status_label.setText("문항 순서를 반영했습니다.")

    def _operation_failed(self, details: str) -> None:
        if self._refreshing_external:
            self._synchronization_failed(details)
            return
        summary = details.splitlines()[0] if details else "알 수 없는 오류"
        self.status_label.setText(f"작업 실패 · {summary}")
        self.log.appendPlainText(f"[ERROR] {details}")
        QMessageBox.critical(self, "작업 실패", summary)

    @Slot()
    def _worker_finished(self) -> None:
        self._thread.wait()
        self._thread.deleteLater()
        self._thread = None
        self._worker = None
        self._set_busy(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None or self._live_operation in {'apply', 'open', 'refresh'}:
            QMessageBox.information(
                self,
                "작업 진행 중",
                "한글 문서 작업이 끝난 뒤 창을 닫아 주세요.",
            )
            event.ignore()
            return
        if not self._closing and not self._allow_discard():
            event.ignore()
            return
        self._hwp_timer.stop()
        self._auto_timer.stop()
        self._auto_pending = False
        if self._live_thread is not None:
            if not self._closing:
                self._closing = True
                self._set_busy(True)
                self.companion_requested.emit("stop", ())
            event.ignore()
            return
        super().closeEvent(event)
