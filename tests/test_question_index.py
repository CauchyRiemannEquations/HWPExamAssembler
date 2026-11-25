"""The editable list may be reused only for the exact saved native document."""
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from app.hwp.transaction import sha256
from app.services import question_index
from app.services.exam_service import DocumentSnapshot, ExamDocumentService, QuestionSummary


class QuestionIndexTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "exam.hwp"
        self.path.write_bytes(b"verified native bytes")
        self.group = QuestionSummary(
            "G1", "공통 문항", True, {"eqed:수식": 2},
            kind="group", child_ids=("A", "B"), objective_count=1, written_count=1,
        )
        self.snapshot = DocumentSnapshot(self.path, (self.group,), sha256(self.path))
        self.metadata = {"G1": {"id": "G1", "source_label": "본문"}}
        self.path.with_suffix(".hwp.import.json").write_text(
            json.dumps({"questions": list(self.metadata.values())}), encoding="utf-8",
        )

    def service(self):
        service = ExamDocumentService(Path(__file__).resolve().parents[1])
        service.work_dir = self.root / "work"
        return service

    def test_restarted_service_uses_verified_list_and_reads_metadata_changes(self):
        self.service().remember_inspection(self.snapshot, self.metadata)
        service = self.service()
        wrong = replace(self.group, preview="새 스냅샷")
        with (patch("app.services.quick_questions.read_questions", return_value=(wrong,)) as local,
              patch("app.services.exam_service.HwpApplication", side_effect=AssertionError("unnecessary native open"))):
            result = service.inspect(self.path)
            self.assertEqual(result, self.snapshot)
            local.assert_not_called()
            result.questions[0].controls.clear()
            self.assertEqual(service.inspect(self.path), self.snapshot)
            self.path.with_suffix(".hwp.import.json").unlink()
            self.assertEqual(service.inspect(self.path).questions, (wrong,))
            self.assertEqual(local.call_count, 1)
            self.path.write_bytes(b"new native document")
            self.assertEqual(service.inspect(self.path).content_sha256, sha256(self.path))
            self.assertEqual(local.call_count, 2)

    def test_corrupt_or_unsaved_index_never_supplies_a_question(self):
        directory = self.service().work_dir
        question_index.store_questions(directory, self.snapshot, self.metadata)
        entry, = directory.iterdir()
        saved = json.loads(entry.read_text(encoding="utf-8"))
        saved["questions"][0]["id"] = "wrong"
        entry.write_text(json.dumps(saved), encoding="utf-8")
        self.assertIsNone(question_index.load_questions(directory, self.snapshot.content_sha256, self.metadata))
        entry.write_bytes(b"{")
        self.assertIsNone(question_index.load_questions(directory, self.snapshot.content_sha256, self.metadata))
        question_index.store_questions(directory, replace(self.snapshot, content_sha256=None), self.metadata)
        self.assertEqual(entry.read_bytes(), b"{")

    def test_explicit_refresh_revalidates_and_replaces_an_index(self):
        service = self.service()
        service.remember_inspection(self.snapshot, self.metadata)
        current = replace(self.group, preview="한글에서 확인한 미리보기")
        with (patch("app.services.exam_service.HwpApplication") as native,
              patch("app.services.exam_service.QuestionFieldManager") as manager,
              patch.object(service, "read_open_questions", return_value=[current])):
            manager.return_value.ordered_question_ids.return_value = ("G1",)
            result = service.refresh_after_external_edit(self.path, self.snapshot)
        self.assertEqual(result.questions, (current,))
        self.assertEqual(native.call_count, 1)
        self.assertEqual(self.service().inspect(self.path), result)

    def test_failed_atomic_write_preserves_previous_complete_entry(self):
        directory = self.service().work_dir
        question_index.store_questions(directory, self.snapshot, self.metadata)
        with patch("app.services.question_index.os.replace", side_effect=PermissionError):
            changed = replace(self.snapshot, questions=(replace(self.group, preview="새 문항"),))
            question_index.store_questions(directory, changed, self.metadata)
        self.assertEqual(question_index.load_questions(directory, self.snapshot.content_sha256, self.metadata),
                         self.snapshot.questions)
        self.assertEqual(len(list(directory.iterdir())), 1)

    def test_save_reuses_native_tab_if_local_group_or_object_inventory_differs(self):
        manager = Mock()
        manager.hwp.active_document.return_value.FullName = str(self.path)
        manager.child_question_fields.return_value = [Mock(group_id="G1", question_id=q) for q in ("A", "B")]
        with (patch("app.services.quick_questions.read_questions", return_value=(self.group,)) as local,
              patch.object(ExamDocumentService, "read_open_questions", return_value=[self.group]) as native):
            capture = ExamDocumentService.capture_verified_list
            controls = {"G1": self.group.controls}
            self.assertEqual(capture(manager, ("G1",), controls, self.metadata), self.snapshot)
            native.assert_not_called()
            for bad in (replace(self.group, child_ids=("B", "A")), replace(self.group, controls={})):
                local.return_value = (bad,)
                self.assertEqual(capture(manager, ("G1",), controls, self.metadata), self.snapshot)
            self.assertEqual(native.call_count, 2)
            self.assertIsNone(capture(manager, ("G1",), controls, self.metadata, verified_path=self.root / "other.hwp"))
