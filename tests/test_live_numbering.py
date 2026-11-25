from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from app.hwp.fields import question_field
from app.hwp.numbering import _field_numbering, load_numbering_profile
from app.hwp.reorder import mismatched_numbering, renumber_visible_questions


def exported_lists(starts, *, font_ids=None, shared_with_cell=False):
    root = ET.Element("HWPML")
    mapping = ET.SubElement(ET.SubElement(root, "HEAD"), "MAPPINGTABLE")
    shapes = ET.SubElement(mapping, "PARASHAPELIST")
    lists = ET.SubElement(mapping, "NUMBERINGLIST")
    section = ET.SubElement(ET.SubElement(root, "BODY"), "SECTION")
    for i, start in enumerate(starts):
        key = str(i + 1)
        ET.SubElement(shapes, "PARASHAPE", Id=key, Heading=key, HeadingType="Number", Level="0")
        numbering = ET.SubElement(lists, "NUMBERING", Id=key, Start=str(start))
        level = ET.SubElement(numbering, "PARAHEAD", Level="1", Start=str(start),
                              CharShape=font_ids[i] if font_ids else "4", NumFormat="Digit")
        level.text = "^1."
        paragraph = ET.SubElement(section, "P", ParaShape=key)
        ET.SubElement(ET.SubElement(paragraph, "TEXT"), "FIELDBEGIN",
                      Type="Clickhere", Name=question_field(key))
    if shared_with_cell:
        cell = ET.SubElement(ET.SubElement(section[0], "TABLE"), "CELL")
        ET.SubElement(cell, "P", ParaShape="1")
    return ET.tostring(root, encoding="unicode")


class LiveNumberingTest(unittest.TestCase):
    ids = ("1", "2", "3")
    targets = tuple((key, question_field(key)) for key in ("1", "2", "3"))
    style = load_numbering_profile(Path(__file__).resolve().parents[1] / "project" / "target_numbering_style.json")

    def test_continued_list_in_a_cell_cannot_supply_an_independent_start(self):
        xml = exported_lists([1, 2, 3], shared_with_cell=True)
        definitions = _field_numbering(xml, {name for _, name in self.targets})
        self.assertNotIn(question_field("1"), definitions)
        self.assertEqual(definitions[question_field("2")].start, 2)

    def test_updates_only_changed_ordinals_and_rechecks_the_result(self):
        manager = Mock()
        manager.ordered_question_ids.return_value = self.ids
        manager.child_question_fields.return_value = ()
        manager.hwp.automation.GetTextFile.side_effect = [
            exported_lists([2, 1, 3]), exported_lists([1, 2, 3])]
        with (patch("app.hwp.reorder.set_visible_field_number") as assign,
              patch("app.hwp.reorder.capture_numbering_style", return_value=self.style) as native):
            mapping = renumber_visible_questions(manager, self.ids, style=self.style, reuse_numbering=True)
        self.assertEqual(mapping, {"1": 1, "2": 2, "3": 3})
        self.assertEqual([c.args[1:] for c in assign.call_args_list],
                         [(question_field("1"), 1), (question_field("2"), 2)])
        self.assertEqual(native.call_count, 1)

    def test_reopen_checks_font_and_order_independently(self):
        manager = Mock()
        manager.hwp.automation.GetTextFile.return_value = exported_lists([1, 2, 8], font_ids=["4", "7", "4"])
        changed = replace(self.style, str_format_level0="^1)")
        with patch("app.hwp.reorder.capture_numbering_style", side_effect=[self.style, changed]) as native:
            failures = mismatched_numbering(manager, self.targets, self.style)
        self.assertEqual(failures, [(2, "2", question_field("2")), (3, "3", question_field("3"))])
        self.assertEqual(native.call_count, 2)

    def test_changed_or_unreadable_definition_falls_back_to_full_assignment(self):
        manager = Mock()
        manager.ordered_question_ids.return_value = self.ids
        manager.child_question_fields.return_value = ()
        manager.hwp.automation.GetTextFile.side_effect = [
            exported_lists([1, 2, 3]), exported_lists([1, 2, 3], shared_with_cell=True)]
        with (patch("app.hwp.reorder.set_visible_field_number") as assign,
              patch("app.hwp.reorder.capture_numbering_style", return_value=self.style)):
            renumber_visible_questions(manager, self.ids, style=self.style, reuse_numbering=True)
        assign.assert_called_once_with(manager, question_field("1"), 1, style=self.style)

    def test_unusual_export_still_verifies_the_native_start_numbers(self):
        manager = Mock()
        manager.hwp.automation.GetTextFile.return_value = ""
        with patch("app.hwp.reorder.capture_number_and_style",
                   side_effect=[(1, self.style), (1, self.style), (3, self.style)]):
            failures = mismatched_numbering(manager, self.targets, self.style)
        self.assertEqual(failures, [(2, "2", question_field("2"))])
