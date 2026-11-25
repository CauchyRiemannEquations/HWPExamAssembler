from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from app.hwp.fields import FIXED_FIELD_PREFIX, TERMINAL_FIELD, question_field
from app.hwp.whole_paragraphs import _plan, open_verified_paragraph_order


class WholeParagraphOrderTests(unittest.TestCase):
    def setUp(self):
        self.fixed = FIXED_FIELD_PREFIX + "1"
        self.manager = Mock()
        self.manager.block_layout.return_value = ("A", self.fixed, "B")
        self.root = ET.fromstring("<HWPML><BODY><SECTION/></BODY></HWPML>")
        self.section = self.root.find("./BODY/SECTION")
        fields = {question_field("A"): 1, self.fixed: 3, question_field("B"): 4, TERMINAL_FIELD: 5}
        self.manager.anchor_position.side_effect = lambda name: (0, fields[name], 0)
        self.manager.block_boundary_position.side_effect = self.manager.anchor_position.side_effect
        for i in range(7):
            text = ET.SubElement(ET.SubElement(self.section, "P"), "TEXT")
            for field, num in fields.items():
                if num == i:
                    ET.SubElement(text, "FIELDBEGIN", Name=field, Type="Clickhere")
            ET.SubElement(text, "CHAR").text = str(i)
        self.order = ("B", "A")

    def test_fixed_slot_and_whole_multiline_questions(self):
        self.assertEqual(_plan(self.manager, self.root, self.order),
                         (("B", self.fixed, "A"), (0, 4, 3, 1, 2, 5, 6)))
        self.manager.block_boundary_position.side_effect = lambda name: (0, 2, 8)
        self.assertIsNone(_plan(self.manager, self.root, self.order))

    def test_native_reopen_checks_entire_document_and_restores_working_on_mismatch(self):
        original = ET.tostring(self.root, encoding="unicode")
        layout, indices = _plan(self.manager, self.root, self.order)
        paragraphs = list(self.section)
        self.section[:] = [paragraphs[i] for i in indices]
        good = ET.tostring(self.root, encoding="unicode")
        hwp = self.manager.hwp
        with (TemporaryDirectory() as tmp,
              patch("app.hwp.paragraph_reorder.reorder_hwpx_paragraphs", return_value=True),
              patch("app.hwp.whole_paragraphs.QuestionFieldManager") as reader):
            reader.return_value.block_layout.return_value = layout
            reader.return_value.ordered_question_ids.return_value = self.order
            work = Path(tmp) / "work.hwpx"
            candidate = Path(tmp) / "staged" / work.name
            for actual, ok in ((good, True), (good.replace(">2<", ">different<"), False)):
                hwp.reset_mock()
                hwp.automation.GetTextFile.side_effect = [original, actual]
                self.assertEqual(open_verified_paragraph_order(self.manager, work, work, candidate, self.order), ok)
                self.assertEqual(hwp.open.call_args.args[0], candidate if ok else work)
