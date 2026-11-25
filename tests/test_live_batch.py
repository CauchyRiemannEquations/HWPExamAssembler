"""Live batch boundaries across the native buffer, recoverable backup and disk."""
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, get_ident
import unittest
from unittest.mock import Mock, patch

from app.hwp.application import HwpAutomationError
from app.hwp.transaction import sha256
from app.services.exam_service import DocumentSnapshot, QuestionSummary
from app.services.live_document import LiveDocumentSession, LiveOrderBatch, _ScratchWorker


@dataclass
class Audit:
    after_sha256: str
    document: str = ""
    backup: str = ""

    def to_dict(self):
        return self.__dict__


class LiveBatchTest(unittest.TestCase):
    original = ("A", "B", "C")
    first = ("B", "A", "C")
    final = ("C", "B", "A")

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.source = self.root / "exam.hwpx"
        self.source.write_bytes(b"ABC;disk")
        self.questions = {q: QuestionSummary(q, q, False, {}) for q in self.original}
        self.snapshot = self.read_snapshot(self.source, self.original)
        service = Mock(work_dir=self.root / "work")
        self.session = LiveDocumentSession(service)
        target = self.target = self.session.target = Mock(path=self.source)
        target.doc.EditMode, target.doc.Modified, target.doc.FullName = 1, 0, str(self.source)
        target.guard_input.side_effect = nullcontext
        target.content_fingerprint.return_value = b"native"
        self.opens = []

        def reopen(old, new, **kwargs):
            self.assertEqual(str(old), target.doc.FullName)
            self.opens.append((old, new))
            target.doc.FullName = str(new)
            target.doc.Modified = 0

        self.reopen = reopen
        target.reopen.side_effect = reopen
        manager = self.enterContext(patch("app.services.live_document.QuestionFieldManager")).return_value
        manager.ordered_question_ids.side_effect = lambda: tuple(self.source.read_bytes().split(b";")[0].decode())
        self.enterContext(patch.object(self.session, "_check_structure", return_value=manager))
        scratch = self.enterContext(patch.object(self.session, "_scratch_worker")).return_value
        scratch.run.side_effect = lambda task, **_: task(None)
        self.batch = LiveOrderBatch(self.first, "B")
        self.attempts = []
        self.after_verify = lambda: None

        def stage(worker, path, order, numbered, **options):
            self.attempts.append(order)
            previous = path.read_bytes()
            self.assertEqual(previous[:3], b"ABC")
            path.write_bytes("".join(order).encode() + previous[3:])
            service.capture_verified_list.return_value = self.read_snapshot(path, order)
            options["on_verified"](manager, {})
            self.after_verify()
            return Audit(sha256(path))

        self.enterContext(patch("app.services.live_document.apply_reorder_transaction", side_effect=stage))

    def read_snapshot(self, path, order):
        return DocumentSnapshot(path, tuple(self.questions[q] for q in order), sha256(path))

    def backups(self):
        return sorted(p.read_bytes() for p in (self.root / "backup").glob("*.hwpx"))

    def apply(self):
        return self.session.apply(self.snapshot, self.first, "B", self.batch)

    def dirty_copy(self):
        self.target.doc.Modified = 1

        def export(payload, baseline, *args):
            baseline.write_bytes(b"ABC;teacher's unsaved edit")
            return True

        self.enterContext(patch.object(self.session, "_verify_live_copy", side_effect=export))

    def test_verified_intermediate_order_is_never_published(self):
        def supersede():
            self.assertEqual(self.source.read_bytes(), b"ABC;disk")
            self.assertFalse(self.backups())
            self.assertTrue(self.batch.update(self.final, "C"))
            self.after_verify = lambda: None

        self.after_verify = supersede
        result = self.apply()
        self.assertEqual(result.order, self.final)
        self.assertEqual(self.attempts, [self.first, self.final])
        self.assertEqual(self.source.read_bytes(), b"CBA;disk")
        self.assertEqual(len(self.opens), 2)
        self.assertEqual(self.backups(), [b"ABC;disk"])
        self.assertFalse(self.batch.update(self.original, "A"))

    def test_visible_preview_follows_concurrent_input_between_private_attempts(self):
        scratch = _ScratchWorker()
        scratch._hwp = Mock()
        self.addCleanup(scratch.close)
        self.enterContext(patch.object(self.session, "_scratch_worker", return_value=scratch))
        private_active, ui_moved, overlap = Event(), Event(), Event()
        previews = []
        owner_thread = get_ident()

        def preview(manager, qid):
            self.assertEqual(get_ident(), owner_thread)
            if private_active.is_set():
                overlap.set()
            previews.append(qid)

        def private_checkpoint():
            if self.attempts == [self.first]:
                private_active.set()
                try:
                    self.assertTrue(ui_moved.wait(2))
                    # Keep the native operation open across several UI ticks.
                    self.assertFalse(overlap.wait(0.18))
                finally:
                    private_active.clear()
            else:
                self.assertEqual(previews[-1], "C")

        def next_move():
            self.assertTrue(private_active.wait(2))
            self.assertTrue(self.batch.update(self.final, "C"))
            ui_moved.set()

        self.after_verify = private_checkpoint
        self.enterContext(patch.object(self.session, "_show_question", side_effect=preview))
        with ThreadPoolExecutor(max_workers=1) as ui:
            submitted = ui.submit(next_move)
            self.assertEqual(self.apply().order, self.final)
            submitted.result()
        self.assertEqual(self.attempts, [self.first, self.final])
        self.assertEqual(len(self.opens), 2)
        self.assertEqual(self.backups(), [b"ABC;disk"])

    def test_undo_after_verification_preserves_unsaved_native_buffer(self):
        self.dirty_copy()
        self.after_verify = lambda: self.batch.update(self.original, "A")
        with patch.object(self.session, "refresh", return_value=self.snapshot):
            self.assertEqual(self.apply().order, self.original)
        self.assertEqual(self.source.read_bytes(), b"ABC;disk")
        self.assertEqual(self.target.doc.Modified, 1)
        self.assertFalse(self.backups())
        self.assertFalse(self.opens)
        self.target.save_current.assert_not_called()

    def test_conflict_while_staging_keeps_the_external_edit(self):
        self.after_verify = lambda: self.source.write_bytes(b"ABC;external")
        with self.assertRaisesRegex(HwpAutomationError, "디스크"):
            self.apply()
        self.assertEqual(self.source.read_bytes(), b"ABC;external")
        self.assertFalse(self.opens)
        self.assertFalse(self.backups())

    def test_dirty_buffer_and_previous_disk_bytes_are_both_backed_up_at_publish(self):
        self.dirty_copy()
        self.assertEqual(self.apply().order, self.first)
        self.target.save_current.assert_not_called()
        self.assertEqual(self.source.read_bytes(), b"BAC;teacher's unsaved edit")
        self.assertEqual(self.backups(), [b"ABC;disk", b"ABC;teacher's unsaved edit"])

    def test_failed_publish_after_dirty_tab_handoff_restores_its_unsaved_text(self):
        self.dirty_copy()

        from os import replace

        def fail_first_publish(incoming, source):
            if len(self.opens) == 1 and Path(incoming).read_bytes().startswith(b"BAC"):
                raise PermissionError("busy")
            replace(incoming, source)

        with patch("app.services.live_document.os.replace", side_effect=fail_first_publish):
            with self.assertRaisesRegex(PermissionError, "busy"):
                self.apply()
        self.assertEqual(self.source.read_bytes(), b"ABC;teacher's unsaved edit")
        self.assertEqual(self.target.doc.FullName, str(self.source))
        self.assertEqual(self.backups(), [b"ABC;disk", b"ABC;teacher's unsaved edit"])

    def test_only_complete_permutations_join_before_publish(self):
        with self.assertRaisesRegex(ValueError, "삭제"):
            self.session.apply(self.snapshot, ("A", "B"), "A", LiveOrderBatch(("A", "B"), "A"))
        self.assertFalse(self.batch.update(("A", "B"), "A"))
        self.assertFalse(self.batch.update(("A", "A", "C"), "A"))
        self.batch.set_editing(True)
        self.assertFalse(self.batch.claim(self.first))
        self.assertTrue(self.batch.update(self.final, "C"))
        self.batch.set_editing(False)
        self.assertFalse(self.batch.claim(self.first))
        self.assertTrue(self.batch.claim(self.final))
        self.assertFalse(self.batch.claim(self.final))

    def test_dirty_recovery_reports_the_backup_on_a_concurrent_disk_change(self):
        self.dirty_copy()

        def external_write(old, new, **kwargs):
            self.reopen(old, new, **kwargs)
            self.source.write_bytes(b"ABC;external change")

        self.target.reopen.side_effect = external_write
        with self.assertRaisesRegex(HwpAutomationError, "최근 수정 백업") as failed:
            self.apply()
        self.assertIn(str(self.root / "backup"), str(failed.exception))
        self.assertEqual(self.source.read_bytes(), b"ABC;external change")
        self.assertIn(b"ABC;teacher's unsaved edit", self.backups())
