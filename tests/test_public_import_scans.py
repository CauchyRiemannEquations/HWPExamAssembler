import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app.services.format_analysis import analyze_hwpml, capture_format_report
from app.services.live_import import LiveImportSession
from app.services.registered_template import RegisteredTemplate


class PublicImportScans(unittest.TestCase):
    def test_validates_all_child_relationships_from_one_read(self):
        session = object.__new__(LiveImportSession)
        session.diagnostic = None
        session.registered = [
            SimpleNamespace(id="G001", child_ids=("Q001", "Q002"), objective_count=1, written_count=1),
            SimpleNamespace(id="Q003", child_ids=(), objective_count=1, written_count=0),
        ]
        manager = Mock()
        manager.ordered_question_ids.return_value = ("G001", "Q003")
        first = SimpleNamespace(group_id="G001", question_id="Q001", kind="objective")
        second = SimpleNamespace(group_id="G001", question_id="Q002", kind="written")
        manager.child_question_fields.return_value = (first, second)
        session._validate_target_identity_graph(manager, phase="test")
        manager.child_question_fields.assert_called_once_with()

        orphan = SimpleNamespace(group_id="G004", question_id="Q004", kind="objective")
        wrong_kind = SimpleNamespace(group_id="G001", question_id="Q002", kind="objective")
        for children in ((second, first), (first, wrong_kind), (first, second, orphan)):
            with self.subTest(children=children):
                manager.child_question_fields.return_value = children
                with self.assertRaises(RuntimeError):
                    session._validate_target_identity_graph(manager, phase="test")

    def test_shared_export_still_checks_the_actual_protected_paragraph(self):
        xml = "<HWPML><BODY><SECTION><P><TEXT><CHAR>school heading</CHAR></TEXT></P></SECTION></BODY></HWPML>"
        hwp = Mock()
        hwp.automation.GetTextFile.side_effect = AssertionError("duplicate native export")
        self.assertEqual(capture_format_report(hwp, xml=xml).to_dict(), analyze_hwpml(xml).to_dict())
        plan = object.__new__(RegisteredTemplate)
        plan.protected = {0: plan._signature(plan._native_paragraphs(xml)[0])}
        plan.verify_preserved(hwp, xml=xml)
        with self.assertRaises(RuntimeError):
            plan.verify_preserved(hwp, xml=xml.replace("school heading", "modified heading"))
        hwp.automation.GetTextFile.assert_not_called()


if __name__ == "__main__":
    unittest.main()
