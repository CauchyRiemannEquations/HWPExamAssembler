"""Verified fast paths for the public editor, including protected template headings."""
from itertools import combinations, permutations
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.hwp.application import HwpApplication
from app.hwp.fields import FIXED_FIELD_PREFIX, TERMINAL_FIELD, QuestionFieldManager, question_field
from app.hwp.reorder import apply_permutation
from app.services.exam_service import ExamDocumentService, QuestionSummary


class PublicEditorSpeedTests(unittest.TestCase):
    def test_minimal_moves_preserve_slot_counts_across_headings(self):
        ids = ("Q001", "Q002", "G001", "Q004", "Q005")
        fixed = FIXED_FIELD_PREFIX + "1"
        for initial in (ids, (*ids[:3], fixed, *ids[3:])):
            dividers = (0, 3, 5) if fixed in initial else (0, 5)
            for wanted in permutations(ids):
                layout = list(initial)
                mock = Mock()
                mock.block_layout.side_effect = lambda: tuple(layout)
                mock.ordered_question_ids.side_effect = lambda: tuple(i for i in layout if i != fixed)

                def move(qid, successor, **kwargs):
                    layout.remove(qid)
                    layout.insert(layout.index(successor) if successor else len(layout), qid)
                    return qid, successor

                mock.move_before.side_effect = move
                result = apply_permutation(mock, wanted)
                retained = 0
                for a, b in zip(dividers, dividers[1:]):
                    retained += max(length for length in range(b-a+1) if
                                    set(combinations(ids[a:b], length)).intersection(combinations(wanted[a:b], length)))
                self.assertEqual(len(result.moves), len(ids) - retained)
                self.assertEqual(result.order_after, wanted)
                if fixed in initial:
                    self.assertEqual(layout.index(fixed), 3)

    def test_range_ownership_excludes_objects_in_protected_gap(self):
        class Control:
            def __init__(self, cid, pos, projected=None):
                self.CtrlID = self.UserDesc = cid
                self.pos = pos, projected or pos
                self.Next = None

            def GetAnchorPos(self, mode):
                return SimpleNamespace(Item=dict(zip(("List", "Para", "Pos"), self.pos[mode])).__getitem__)

        controls = [Control("%clk", (0,1,0)), Control("eqed", (0,2,3)),
                    Control("%clk", (0,3,0)), Control("tbl", (0,4,4)), Control("%clk", (0,6,0)),
                    Control("gso", (0,7,2)), Control("tbl", (4,0,0), (0,7,7)), Control("%clk", (0,8,0))]
        for c, nxt in zip(controls, controls[1:]):
            c.Next = nxt
        hwp = SimpleNamespace(automation=SimpleNamespace(HeadCtrl=controls[0]))
        mgr = QuestionFieldManager(hwp)
        mgr._field_names_cache = (question_field("Q001"), FIXED_FIELD_PREFIX+"1", question_field("Q002"), TERMINAL_FIELD)
        total, owners = mgr.control_inventory()
        self.assertEqual(total["tbl:tbl"], 2)
        self.assertEqual(owners, {"Q001":{"eqed:eqed":1}, "Q002":{"gso:gso":1}})
        ranges = (((0,1,0),(0,3,0)), ((0,6,0),(0,8,0)))
        self.assertEqual(HwpApplication.transfer_control_summaries_for_ranges(hwp, ranges), tuple(owners.values()))
        self.assertEqual(HwpApplication.control_summaries_for_ranges(hwp, ranges)[1], {"gso:gso":1, "tbl:tbl":1})

    def test_snapshot_cache_requires_identical_document_and_import_metadata(self):
        service = ExamDocumentService(Path(__file__).resolve().parents[1])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.hwpx"
            service.work_dir = Path(folder) / "work"
            path.write_bytes(b"first")

            def read(hwp, manager, order, metadata, progress):
                return [QuestionSummary("Q001", metadata.get("Q001", {}).get("preview", ""), False, {"tbl":1})]

            with (patch("app.services.exam_service.HwpApplication") as native,
                  patch("app.services.exam_service.QuestionFieldManager") as manager,
                  patch.object(service, "read_open_questions", side_effect=read)):
                manager.return_value.ordered_question_ids.return_value = ("Q001",)
                service.inspect(path).questions[0].controls.clear()
                self.assertEqual(service.inspect(path).questions[0].controls, {"tbl":1})
                self.assertEqual(native.call_count, 1)
                path.write_bytes(b"other")
                service.inspect(path)
                path.with_suffix(".hwpx.import.json").write_text(json.dumps({"questions":[{"id":"Q001","preview":"new"}]}))
                self.assertEqual(service.inspect(path).questions[0].preview, "new")
                self.assertEqual(native.call_count, 3)


if __name__ == "__main__":
    unittest.main()
