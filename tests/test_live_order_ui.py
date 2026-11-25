"""Exercise the editor → saved file → external editor → same question handoff."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import threading
import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog

from app.services import DocumentSnapshot, ExamDocumentService, QuestionSummary, file_fingerprint
from app.services.difficulty import load_difficulties
from app.ui import MainWindow
from app.ui.question_list import QuestionListWidget
from app.ui.question_row import LEVEL_ROLE, NUMBER_ROLE


class SyntheticService(ExamDocumentService):
    def __init__(self):
        super().__init__(Path(__file__).resolve().parents[1])
        self.refreshes = 0
        self.saves = 0
        self.reject_refresh = False

    def apply_in_place(self, snapshot, order, *, visible, open_after, on_progress):
        assert visible is False and open_after is False
        path = snapshot.path
        self.saves += 1
        path.write_text(" ".join(order), encoding="utf-8")
        audit = {"after_sha256": file_fingerprint(path).sha256}
        path.with_suffix(".hwp.audit.json").write_text(json.dumps(audit), encoding="utf-8")
        return path

    def refresh_after_external_edit(self, path, previous, *, visible, on_progress):
        assert visible is False
        self.refreshes += 1
        if self.reject_refresh:
            raise ValueError("문항 경계가 변경되었습니다.")
        return DocumentSnapshot(path, previous.questions, file_fingerprint(path).sha256)


def wait_until(application, predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for the GUI worker")
        application.processEvents()
        time.sleep(0.005)


class NativeWindow:
    """A user-owned open document; changes can exist only in its live buffer."""

    def __init__(self, service):
        self.service = service
        self.active = True
        self.closed = False
        self.opens = 0
        self.applies = 0
        self.releases = 0
        self.visits = []
        self.passive_visits = []
        self.focus_after_apply = []
        self.hold = threading.Event()
        self.hold.set()
        self.applying = threading.Event()
        self.ready_to_claim = threading.Event()
        self.claimed = threading.Event()
        self.publish = threading.Event()
        self.publish.set()
        self.native_order = ()
        self.text = {}
        self.worker_thread = None

    def check_thread(self):
        current = threading.get_ident()
        assert current != threading.main_thread().ident
        if self.worker_thread:
            assert self.worker_thread == current
        self.worker_thread = current

    def open(self, expected, *, only_if_open=False):
        self.check_thread()
        if only_if_open and self.closed:
            return None
        self.opens += 1
        self.closed = False
        if not self.native_order:
            self.native_order = expected.order
        return self.refresh(expected)

    def probe(self):
        self.check_thread()
        return "closed" if self.closed else "active" if self.active else "inactive"

    def navigate(self, qid, activate=True):
        self.check_thread()
        (self.visits if activate else self.passive_visits).append(qid)

    def refresh(self, expected):
        self.check_thread()
        by_id = {q.id: q for q in expected.questions}
        questions = tuple(replace(by_id[qid], preview=self.text.get(qid, by_id[qid].preview)) for qid in self.native_order)
        return DocumentSnapshot(expected.path, questions, content_sha256=None)

    def apply(self, expected, requested, focus_qid, batch=None):
        self.check_thread()
        self.applying.set()
        assert self.hold.wait(3)
        if not self.active:
            raise ValueError("연결된 문서 탭으로 돌아와 주세요.")
        if self.native_order != expected.order:
            raise ValueError("한글에서도 순서가 바뀌었습니다.")
        self.ready_to_claim.set()
        if batch is not None:
            while True:
                batch.wait_until_idle()
                requested, focus_qid = batch.latest()
                if batch.claim(requested):
                    break
        self.claimed.set()
        assert self.publish.wait(3)
        if requested == expected.order:
            return expected
        self.applies += 1
        self.focus_after_apply.append(focus_qid)
        self.native_order = requested
        expected.path.write_text(" ".join(requested) + str(self.text), encoding="utf-8")
        return replace(self.refresh(expected), content_sha256=file_fingerprint(expected.path).sha256)

    def release(self):
        self.check_thread()
        self.releases += 1


def exercise_live_editor():
    application = QApplication.instance() or QApplication([])
    service = SyntheticService()
    native = NativeWindow(service)
    window = MainWindow(service, companion_factory=lambda _: native)

    def settled():
        return window._thread is None and window._live_operation is None and not window._busy

    with TemporaryDirectory() as directory:
        path = Path(directory) / "exam.hwp"
        path.write_bytes(b"initial")
        questions = tuple(QuestionSummary(f"Q{i}", f"Item {i}", True, {}) for i in range(30))
        original = DocumentSnapshot(path, questions, file_fingerprint(path).sha256)
        window._document_loaded(original)
        window._set_busy(False)
        window.question_list.setCurrentRow(20)
        # Already open outside the app, even when the file-lock hint is absent.
        assert window.question_list.move_current(2)
        assert window.question_list.move_current(-1)
        window.question_list.setCurrentRow(3)
        wait_until(application, lambda: settled() and native.applies and not window._dirty_order())
        assert native.opens == native.applies == 1 and service.saves == 0
        assert window.snapshot.order[21] == "Q20" and native.focus_after_apply == ["Q20"]
        assert window.question_list.currentRow() == 3
        window.question_list.setCurrentRow(21)
        assert window.question_list.isEnabled()

        native.text["Q20"] = "원래 열린 한글에서 아직 저장하지 않은 수식 설명"
        window._save_difficulty(("Q20",), "상")
        window.question_list.move_current(-1)
        native.active = False
        wait_until(application, lambda: settled() and window._live_issue)
        assert window.question_list.isEnabled() and window.apply_button.isEnabled()
        assert window._dirty_order() and native.applies == 1
        native.active = True
        native.last_warning = "선택한 문항으로 이동하지 못했습니다."
        window.apply_button.click()
        wait_until(application, settled)
        assert native.opens == 1 and native.applies == 2 and window._editing_in_hwp
        wait_until(application, lambda: native.last_warning in window.status_label.text())
        assert native.last_warning in window.log.toPlainText()
        native.last_warning = ""
        assert native.text["Q20"] in path.read_text(encoding="utf8")
        assert window.snapshot.questions[20].preview == native.text["Q20"]
        assert window.question_list.currentRow() == 20
        assert window.question_list.difficulties["Q20"] == "상"
        window.edit_in_hwp_button.click()
        wait_until(application, settled)
        assert native.visits[-1] == "Q20"
        window.undo_button.click()
        wait_until(application, lambda: settled() and native.applies == 3 and not window._dirty_order())
        assert window.snapshot.order[21] == "Q20"
        window.undo_button.click()
        assert "Q20" not in window.question_list.difficulties

        # Undo before the publication claim needs no native write at all.
        native.applying.clear()
        native.hold.clear()
        assert window.question_list.move_current(-1)
        wait_until(application, native.applying.is_set)
        window.undo_button.click()
        native.hold.set()
        wait_until(application, lambda: settled() and not window._dirty_order())
        assert native.applies == 3 and native.native_order == window.snapshot.order

        # Undo to the old baseline while that move is still publishing. Both
        # native writes must finish in order, without discarding the later undo.
        native.claimed.clear()
        native.publish.clear()
        assert window.question_list.move_current(-1)
        wait_until(application, native.claimed.is_set)
        assert window.question_list.isEnabled()
        window.undo_button.click()
        native.publish.set()
        wait_until(application, lambda: settled() and native.applies == 5 and not window._dirty_order())
        assert native.native_order == window.question_list.ordered_ids()
        assert window.question_list.move_current(-1)
        wait_until(application, lambda: settled() and native.applies == 6 and not window._dirty_order())

        # A move must not implicitly commit an earlier staged deletion.
        window.question_list.setCurrentRow(8)
        window.question_list.remove_selected()
        assert window.question_list.move_current(1)
        wait_until(application, lambda: settled() and not window._auto_pending)
        assert window.apply_button.isEnabled() and native.applies == 6
        assert len(native.native_order) == 30
        window.undo_button.click()
        window.undo_button.click()
        wait_until(application, lambda: settled() and not window._auto_pending)
        assert window.question_list.ordered_ids() == native.native_order
        window.question_list.setCurrentRow(20)

        # Even a confirmed clean close must leave the pending movement intact.
        window.question_list.move_current(1)
        native.closed = True
        window._poll_hwp_session()
        wait_until(application, lambda: settled() and not window._editing_in_hwp)
        assert service.refreshes == 0 and window._dirty_order()
        assert window.question_list.ordered_ids()[21] == "Q20"

        window.open_current_in_hwp()
        wait_until(application, lambda: settled() and window._editing_in_hwp)
        assert native.applies == 7 and not window._dirty_order()
        window.question_list.move_current(-1)
        path.write_bytes(b"external changes saved on close")
        native.closed = True
        window._poll_hwp_session()
        wait_until(application, lambda: settled() and not window._editing_in_hwp)
        assert service.refreshes == 1 and window._dirty_order()
        assert window.snapshot.content_sha256 == file_fingerprint(path).sha256
        assert window.question_list.ordered_ids()[20] == "Q20"

        # A new movement while the file is closed stays a draft; an automatic
        # attachment must neither launch the shell nor write through the disk path.
        assert window.question_list.move_current(1)
        assert window.question_list.move_current(-1)
        wait_until(application, lambda: settled() and not window._auto_pending)
        assert native.closed and not window._editing_in_hwp and service.saves == 0

        # A closed document changed by another process must not receive a
        # stale pending order through the disk-based save path.
        path.write_bytes(b"outside update")
        with (patch("app.ui.hwp_companion.find_open_document", return_value=False),
              patch("app.ui.main_window.file_is_in_use", return_value=False)):
            window.apply_button.click()
            wait_until(application, settled)
        assert service.saves == 0 and "외부에서 변경" in window.status_label.text()
        assert window._dirty_order()
        window._restore_original_order()
        window.open_current_in_hwp()
        wait_until(application, lambda: settled() and window._editing_in_hwp)
        native.closed = True
        service.reject_refresh = True
        window._poll_hwp_session()
        wait_until(application, lambda: settled() and window._sync_failed)
        assert not window.question_list.isEnabled() and window.open_button.isEnabled()
        service.reject_refresh = False
        window.reload_document()
        wait_until(application, settled)
        assert not window._sync_failed and window.question_list.isEnabled()
        window.close()
        wait_until(application, lambda: window._live_thread is None)


class AutomaticOrderTest(unittest.TestCase):
    def test_number_picker_keeps_draft_after_failure_and_retry_leaves_new_deletion_staged(self):
        app = QApplication.instance() or QApplication([])
        service = SyntheticService()
        native = NativeWindow(service)
        with TemporaryDirectory() as folder, patch.object(native, "apply", wraps=native.apply) as apply:
            path = Path(folder) / "exam.hwp"
            path.write_bytes(b"initial")
            questions = tuple(QuestionSummary(key, key, True, {}) for key in "ABCDE")
            window = MainWindow(service, companion_factory=lambda _: native)
            widget = window.question_list
            window.show()
            window._document_loaded(DocumentSnapshot(path, questions, file_fingerprint(path).sha256))
            window.open_current_in_hwp()
            wait_until(app, lambda: window._editing_in_hwp and not window._busy)
            try:
                native.hold.clear()
                widget.setCurrentRow(1)
                widget.move_selected_to(0)
                wait_until(app, native.applying.is_set)
                widget.setCurrentRow(3)
                deadline = time.monotonic() + 2
                timer = QTimer()
                def finish_picker():
                    dialog = app.activeModalWidget()
                    if isinstance(dialog, QInputDialog):
                        native.active = False
                        native.hold.set()
                        if window._live_issue:
                            dialog.setIntValue(1)
                            dialog.accept()
                        elif time.monotonic() >= deadline:
                            dialog.reject()
                timer.timeout.connect(finish_picker)
                timer.start(10)
                try:
                    widget.request_position()
                finally:
                    timer.stop()
                QTest.qWait(120)
                self.assertEqual(widget.ordered_ids(), tuple("DBACE"))
                self.assertTrue(window._live_issue and window.apply_button.isEnabled())
                self.assertEqual(apply.call_count, 1)
                self.assertEqual(native.native_order, tuple("ABCDE"))

                native.active = True
                native.applying.clear()
                native.hold.clear()
                window.apply_button.click()
                wait_until(app, native.applying.is_set)
                widget.setCurrentRow(2)
                self.assertEqual(widget.remove_selected(), ("A",))
                native.hold.set()
                wait_until(app, lambda: not window._busy and not window._auto_pending)
                self.assertEqual(native.native_order, tuple("DBACE"))
                self.assertEqual(widget.ordered_ids(), tuple("DBCE"))
                self.assertEqual(window.apply_button.text(), "순서·삭제 반영")
                window.undo_button.click()
                wait_until(app, lambda: not window._auto_pending)
                self.assertEqual(widget.ordered_ids(), native.native_order)
                self.assertEqual(apply.call_count, 2)
            finally:
                native.hold.set()
                wait_until(app, lambda: not window._busy)
                with patch.object(window, "_allow_discard", return_value=True):
                    window.close()
                    wait_until(app, lambda: window._live_thread is None)

    def test_move_other_rows_while_probe_and_save_are_outstanding(self):
        app = QApplication.instance() or QApplication([])
        service = SyntheticService()
        native = NativeWindow(service)
        probe_started, release_probe = threading.Event(), threading.Event()

        def probe():
            probe_started.set()
            assert release_probe.wait(3)
            return "active"

        class DisableObserver(QObject):
            def __init__(self):
                super().__init__()
                self.disabled = False

            def eventFilter(self, watched, event):
                if event.type() == QEvent.Type.EnabledChange and not watched.isEnabled():
                    self.disabled = True
                return False

        with TemporaryDirectory() as folder:
            path = Path(folder) / "exam.hwp"
            path.write_bytes(b"initial")
            questions = tuple(QuestionSummary(key, key, True, {}) for key in "ABCDEF")
            window = MainWindow(service, companion_factory=lambda _: native)
            widget = window.question_list
            window.show()
            window._document_loaded(DocumentSnapshot(path, questions, file_fingerprint(path).sha256))
            window.open_current_in_hwp()
            wait_until(app, lambda: window._editing_in_hwp and window._live_operation is None)
            observer = DisableObserver()
            widget.installEventFilter(observer)
            try:
                with patch.object(native, "probe", side_effect=probe):
                    window._poll_hwp_session()
                    wait_until(app, probe_started.is_set)
                    widget.setCurrentRow(1)
                    self.assertTrue(widget.move_selected_to(0))
                    wait_until(app, lambda: not window._auto_timer.isActive())
                    self.assertTrue(widget.isEnabled())
                    widget.setCurrentRow(3)
                    self.assertTrue(widget.move_selected_to(1))
                    latest = widget.ordered_ids()
                    self.assertEqual(latest, tuple("BDACEF"))
                    native.hold.clear()
                    release_probe.set()
                    wait_until(app, native.applying.is_set)
                self.assertEqual(native.applies, 0)
                widget.setCurrentRow(4)
                self.assertTrue(widget.move_selected_to(2))
                widget.difficulty_requested.emit(("E",), "중")
                self.assertTrue(widget.isEnabled())
                # While F is being dragged the worker cannot publish E's
                # earlier order. Dropping F publishes the whole burst once.
                viewport = widget.viewport()
                start = widget.visualItemRect(widget.item(5)).center()
                end = widget.visualItemRect(widget.item(1)).center()
                end.setY(widget.visualItemRect(widget.item(1)).top() + 2)
                QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
                QTest.mouseMove(viewport, end)
                self.assertIsNotNone(widget._drag_point)
                native.hold.set()
                wait_until(app, native.ready_to_claim.is_set)
                self.assertFalse(native.claimed.wait(0.15))
                self.assertEqual(native.native_order, tuple("ABCDEF"))
                self.assertEqual(widget.selected_ids(), ("F",))
                self.assertIsNotNone(widget._drag_point)
                QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
                self.assertEqual(widget.ordered_ids(), tuple("BFDEAC"))
                wait_until(app, lambda: not window._busy and window.snapshot.order == tuple("BFDEAC"))
                self.assertFalse(observer.disabled)
                self.assertEqual(native.native_order, widget.ordered_ids())
                self.assertEqual(widget.selected_ids(), ("F",))
                self.assertEqual(native.focus_after_apply, ["F"])
                self.assertEqual(load_difficulties(path), {"E": "중"})

                # Hold a real number picker across the next publication
                # opportunity. The accepted position joins the existing batch.
                native.hold.clear()
                native.applying.clear()
                native.ready_to_claim.clear()
                native.claimed.clear()
                widget.setCurrentRow(0)
                widget.move_current(1)
                wait_until(app, native.applying.is_set)
                widget.setCurrentRow(5)
                held = []
                def accept_position():
                    dialog = app.activeModalWidget()
                    native.hold.set()
                    reached = native.ready_to_claim.wait(1)
                    held.append(reached and not native.claimed.wait(0.12))
                    dialog.setIntValue(2)
                    dialog.accept()
                QTimer.singleShot(0, accept_position)
                widget.request_position()
                self.assertEqual(held, [True])
                wait_until(app, lambda: not window._busy and not window._dirty_order())
                self.assertEqual(native.native_order, tuple("FCBDEA"))
                self.assertEqual(native.focus_after_apply, ["F", "C"])

                # After F's order is sealed, move B and undo it. The final
                # order needs one save; the native caret should still follow B.
                native.claimed.clear()
                native.publish.clear()
                widget.setCurrentRow(0)
                widget.move_current(1)
                wait_until(app, native.claimed.is_set)
                widget.setCurrentRow(2)
                widget.move_current(1)
                window.undo_button.click()
                navigation_started, finish_navigation = threading.Event(), threading.Event()
                native_navigate = native.navigate
                def hold_navigation(qid, activate=True):
                    navigation_started.set()
                    assert finish_navigation.wait(3)
                    native_navigate(qid, activate)
                with patch.object(native, "navigate", side_effect=hold_navigation):
                    native.publish.set()
                    wait_until(app, navigation_started.is_set)
                    self.assertEqual(native.native_order, tuple("CFBDEA"))
                    self.assertEqual(native.focus_after_apply, ["F", "C", "F"])
                    self.assertEqual(widget.selected_ids(), ("B",))
                    self.assertTrue(widget.isEnabled())
                    # Drag E across the late caret acknowledgement, then undo
                    # before autosave. Only the caret should need another move.
                    start = widget.visualItemRect(widget.item(4)).center()
                    end = widget.visualItemRect(widget.item(1)).center()
                    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
                    QTest.mouseMove(viewport, end)
                    finish_navigation.set()
                    wait_until(app, lambda: native.passive_visits == ["B"] and not window._busy)
                    self.assertTrue(widget.move_in_progress)
                    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
                window.undo_button.click()
                wait_until(app, lambda: native.passive_visits == ["B", "E"] and not window._busy)
                self.assertEqual(native.applies, 3)
            finally:
                native.hold.set()
                release_probe.set()
                wait_until(app, lambda: not window._busy)
                with patch.object(window, "_allow_discard", return_value=True):
                    window.close()
                    wait_until(app, lambda: window._live_thread is None)

    def test_direct_open_starts_in_editor_and_first_selection_is_editable(self):
        app = QApplication.instance() or QApplication([])
        with TemporaryDirectory() as folder:
            path = Path(folder) / "sample.hwpx"
            path.write_bytes(b"assembled")
            questions = tuple(QuestionSummary(qid, qid, True, {}) for qid in ("Q01", "Q02"))
            snapshot = DocumentSnapshot(path, questions, file_fingerprint(path).sha256)
            service = SyntheticService()
            with (patch.object(service, "inspect", return_value=snapshot),
                  patch("app.ui.main_window.recent_templates") as home_catalog):
                window = MainWindow(service, initial_document=path)
                window.show()
                self.assertIs(window.pages.currentWidget(), window.editor_page)
                wait_until(app, lambda: window._thread is None)
                home_catalog.assert_not_called()
                self.assertTrue(window.question_list.isEnabled())
                self.assertEqual(window.question_list.selected_ids(), ("Q01",))
                self.assertTrue(window.question_list.move_current(1))
                window.question_list.difficulty_requested.emit(("Q01",), "상")
                self.assertEqual(load_difficulties(path), {"Q01": "상"})
                self.assertEqual(window.question_list.ordered_ids(), ("Q02", "Q01"))
                window._restore_original_order()
                window.close()

    def test_move_and_undo_while_hancom_is_saving(self):
        exercise_live_editor()

    def test_selected_block_keeps_current_question_and_difficulty(self):
        app = QApplication.instance() or QApplication([])
        widget = QuestionListWidget()
        widget.set_questions(tuple(QuestionSummary(qid, qid, True, {}) for qid in "ABCDEF"))
        widget.set_difficulties({"C": "상"})
        widget.setCurrentRow(2)
        widget.item(1).setSelected(True)
        current = widget.currentItem()
        self.assertTrue(widget.move_selected_to(4))
        self.assertEqual(widget.ordered_ids(), tuple("ADEFBC"))
        self.assertEqual(widget.selected_ids(), ("B", "C"))
        self.assertIs(widget.currentItem(), current)
        self.assertEqual(current.data(NUMBER_ROLE), "6.")
        self.assertEqual(current.data(LEVEL_ROLE), "상")
        self.assertTrue(widget.move_current(-1))
        self.assertEqual(widget.ordered_ids(), tuple("ADEBCF"))
        widget.item(0).setSelected(True)
        self.assertTrue(widget.move_selected_to(1))
        self.assertEqual(widget.ordered_ids(), tuple("DABCEF"))
        self.assertEqual(widget.selected_ids(), ("A", "B", "C"))
        self.assertIs(widget.currentItem(), current)
        widget.set_difficulties({"C": "중"})
        self.assertEqual(current.data(LEVEL_ROLE), "중")
        self.assertIn("난이도: 중", current.text())
        widget.close()
