"""Verified fast paths for the public editor, including protected template headings."""
from itertools import combinations, permutations
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pythoncom

from app.hwp.application import HwpApplication, HwpAutomationError
from app.hwp.fields import FIXED_FIELD_PREFIX, TERMINAL_FIELD, QuestionFieldManager, QuestionTransferMismatch, question_field
from app.hwp.reorder import apply_permutation
from app.hwp.transaction import apply_reorder_transaction
from app.hwp.user_document import UserDocument
from app.services.exam_service import ExamDocumentService, QuestionSummary


class PublicEditorSpeedTests(unittest.TestCase):
    def test_next_move_cannot_cancel_between_cut_and_verified_paste(self):
        native = Mock()
        manager = QuestionFieldManager(native)
        fields = [question_field("A"), question_field("B")]
        original = fields.copy()
        native.automation.GetFieldList.side_effect = lambda _number, flags: (
            question_field("B") if flags & 4 else "\x02".join(fields)
        )
        selected = (0, 5, 0, 0, 8, 0)
        native.select_position_range.return_value = selected
        def read_saveblock():
            native.select_position_range.reset_mock()
            return "body"
        def cut_reselected_block():
            native.select_position_range.assert_called_once_with(selected[:3], selected[3:])
            fields.remove(question_field("B"))
        native.cut_selection.side_effect = cut_reselected_block
        native.paste.side_effect = lambda: fields.insert(0, question_field("B"))
        native.selected_plain_text.side_effect = read_saveblock
        observed = []
        with (patch.object(manager, "block_boundary_position", return_value=(0, 3, 1)),
              patch.object(manager, "select_question", return_value=selected)):
            manager.move_before("B", "A", on_progress=lambda _msg: observed.append(fields.copy()))
        self.assertEqual(observed, [original, original, list(reversed(original))])

    def test_cut_waits_until_partial_native_field_updates_settle(self):
        manager = QuestionFieldManager(Mock())
        target = ("A", "C")
        with (patch.object(manager, "ordered_question_ids", side_effect=[("A",), target]),
              patch("app.hwp.fields.time.sleep"),
              patch("app.hwp.fields.pythoncom.PumpWaitingMessages") as pump):
            self.assertEqual(manager._wait_for_transfer_order(target), target)
            pump.assert_called_once()

    def test_second_incorrect_native_transfer_stops_without_writing_the_original(self):
        native = Mock()
        manager = Mock()
        manager.ordered_question_ids.return_value = ("A", "B")
        manager.control_inventory.return_value = ({}, {})
        with (tempfile.TemporaryDirectory() as directory,
              patch("app.hwp.transaction.QuestionFieldManager", return_value=manager),
              patch("app.hwp.transaction.apply_permutation",
                    side_effect=QuestionTransferMismatch("unexpected native block")) as transfer):
            root = Path(directory)
            doc = root / "exam.hwp"
            doc.write_bytes(b"original")
            with self.assertRaises(QuestionTransferMismatch):
                apply_reorder_transaction(native, doc, ("B", "A"), (), numbering_style=None,
                                          backup_dir=root / "backup", work_dir=root)
            self.assertEqual(transfer.call_count, 2)
            self.assertEqual(native.open.call_args_list[0], native.open.call_args_list[1])
            self.assertEqual(doc.read_bytes(), b"original")
            native.save_exam_copy.assert_not_called()

    def test_cut_uses_new_field_location_only_if_the_paragraph_still_starts_with_it(self):
        native = Mock()
        manager = QuestionFieldManager(native)
        marker = question_field("Q003")
        native.current_position.side_effect = [(0, 12, 8), (0, 14, 16), (3, 2, 8)]
        with patch.object(manager, "block_boundary_position", return_value=(0, 10, 25)) as scan:
            self.assertEqual(manager._destination_after_cut(marker, True), (0, 12, 0))
            scan.assert_not_called()
            for starts_paragraph in (True, True, False):
                self.assertEqual(manager._destination_after_cut(marker, starts_paragraph), (0, 10, 25))
            self.assertEqual(scan.call_count, 3)

    @staticmethod
    def _shell_link(old):
        link = UserDocument(old)
        link.doc = SimpleNamespace(FullName=str(old), DocumentID=15, Modified=0)
        link.doc_id, link.hwnd = 15, 30
        link._documents = SimpleNamespace(Active_XHwpDocument=link.doc)
        link._windows = SimpleNamespace(Active_XHwpWindow=SimpleNamespace(WindowHandle=30))
        link._hwp = Mock()
        link._hwp.Path = str(old)
        return link

    def test_shell_reopen_uses_the_bound_active_tab_once_even_if_com_loses_the_result(self):
        old, new = Path("original.hwpx").resolve(), Path("verified.hwpx").resolve()
        link = self._shell_link(old)

        def open_then_fault(path, fmt, options):
            link.doc.FullName = path
            link._hwp.Path = path
            raise pythoncom.com_error(-2147417851, "server fault", None, None)

        link._hwp.Open.side_effect = open_then_fault
        with patch("win32gui.IsWindow", return_value=True):
            link.reopen(old, new)
            link._hwp.Open.assert_called_once_with(str(new), "HWPX", "skipcomment:true")
            link._documents.Active_XHwpDocument = SimpleNamespace(FullName=str(new), DocumentID=16)
            with self.assertRaises(HwpAutomationError):
                link.reopen(new, old)
            link._documents.Active_XHwpDocument = link.doc
            link._hwp.Path = str(old)
            with self.assertRaises(HwpAutomationError):
                link.reopen(new, old)
            self.assertEqual(link._hwp.Open.call_count, 1)
            # Older object interfaces can omit Path; tab identity still applies.
            del link._hwp.Path
            link._require_file_action_target(new)

    def test_retry_only_when_guarded_shell_tab_still_has_the_whole_checkpoint(self):
        old, new = Path("old.hwpx").resolve(), Path("new.hwpx").resolve()
        for scenario, calls in (("unchanged", 2), ("edited", 1), ("switched", 1),
                                ("external-dialog", 1), ("finished-while-pumping", 1),
                                ("second-fault", 2), ("unrelated-error", 1)):
            with self.subTest(scenario=scenario):
                link = self._shell_link(old)
                link._input_guarded = scenario != "external-dialog"
                link.content_fingerprint = Mock(return_value=b"edited" if scenario == "edited" else b"original")

                def finish():
                    link.doc.FullName = link._hwp.Path = str(new)

                def attempt(*args):
                    if link._hwp.Open.call_count == 1 or scenario == "second-fault":
                        if scenario == "switched":
                            link._documents.Active_XHwpDocument = SimpleNamespace(FullName=str(old), DocumentID=61)
                        code = -2147417846 if scenario == "unrelated-error" else -2147417851
                        raise pythoncom.com_error(code, "native fault", None, None)
                    finish()
                    return True

                link._hwp.Open.side_effect = attempt
                with (patch("win32gui.IsWindow", return_value=True), patch("win32gui.IsWindowEnabled", return_value=False),
                      patch("pythoncom.PumpWaitingMessages", side_effect=finish if scenario == "finished-while-pumping" else None),
                      patch("app.hwp.user_document.time.sleep")):
                    if scenario in ("unchanged", "finished-while-pumping"):
                        link.reopen(old, new, unchanged_content=b"original")
                    else:
                        with self.assertRaises((pythoncom.com_error, HwpAutomationError)):
                            link.reopen(old, new, unchanged_content=b"original")
                self.assertEqual(link._hwp.Open.call_count, calls)

    def test_lost_com_collection_detaches_only_after_the_native_window_closes(self):
        link = UserDocument(Path("exam.hwp"))
        link.doc, link._documents, link.hwnd = object(), object(), 10
        with patch("win32gui.IsWindow", return_value=True) as is_window:
            self.assertEqual(link.state(), "inactive")
            is_window.return_value = False
            self.assertEqual(link.state(), "closed")

    def test_incomplete_opening_list_cannot_delete_unseen_native_question(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "exam.hwp"
            path.write_bytes(b"original")
            native = Mock()
            fields = [question_field(qid) for qid in ("Q001", "Q002", "Q003")]
            native.automation.GetFieldList.return_value = "\x02".join([*fields, TERMINAL_FIELD])
            with self.assertRaisesRegex(HwpAutomationError, "문항 목록"):
                apply_reorder_transaction(native, path, ("Q003", "Q001"), (), numbering_style=None,
                                          backup_dir=path.parent / "backup", work_dir=path.parent,
                                          allow_deletions=True, expected_order=("Q001", "Q003"))
            native.delete_selection.assert_not_called()
            self.assertEqual(path.read_bytes(), b"original")

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
