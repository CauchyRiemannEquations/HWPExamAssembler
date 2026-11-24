import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from app.hwp.native_template import Paragraph
from app.services.template_model import Region
from app.services.template_ai import make_request, read_suggestions


class TemplateAiTest(unittest.TestCase):
    def setUp(self):
        texts = ("기말고사 성명 극비이름", "답안지 PRIVATE_PATH", "1. 극비문항의 값을 구하시오?",
                 "수고하셨습니다 SECRET_ANSWER")
        self.ps = tuple(Paragraph(i, s, s, "", ("TABLE", "PRIVATE_CONTROL"), False,
                                  (0, i, 0), (0, i + 1, 0)) for i, s in enumerate(texts))
        self.regions = (
            Region("header", "keep", "PRIVATE_LABEL", (0, 0, 0), (0, 1, 0), ""),
            Region("examples", "replace", "문항", (0, 1, 0), (0, 4, 0), ""),
            Region("insert", "insert", "삽입", (0, 1, 0), (0, 1, 0), ""),
            Region("footer", "keep", "끝", None, None, ""),
        )
        self.req = make_request(self.ps, self.regions)

    def reply(self, changes, request=None):
        return json.dumps({"request_id": (request or self.req).request_id, "changes": changes})

    def test_content_is_local(self):
        for sentinel in ("극비이름", "극비문항", "SECRET", "PRIVATE"):
            self.assertNotIn(sentinel, self.req.prompt)
        self.assertIn("학생 기입란", self.req.prompt)
        self.assertIn("표", self.req.prompt)

    def test_valid_result_and_noop(self):
        corrected = [{"region": i, "start": a, "end": b} for i,a,b in
                     [(0,0,2), (1,2,3), (2,2,2), (3,3,4)]]
        updated = read_suggestions(self.req, self.reply(corrected))
        self.assertEqual(updated[2].start, (0,2,0))
        self.assertEqual(updated[3].end, self.ps[-1].end)
        self.assertEqual(self.regions, read_suggestions(self.req, self.reply([])))

    def test_bad_response_is_rejected(self):
        for change in ([{"region": 1, "start": 0, "end": 4}],
                       [{"region": 2, "start": 1, "end": 2}],
                       [{"region": True, "start": 0, "end": 1}],
                       [{"region": 22, "start": 3, "end": 4}],
                       [{"region": 1, "start": 1, "end": 5}]):
            with self.subTest(change=change), self.assertRaises(ValueError):
                read_suggestions(self.req, self.reply(change))
        with self.assertRaises(ValueError):
            read_suggestions(make_request(self.ps, self.regions), self.reply([]))
        locked = make_request(self.ps, [replace(self.regions[0], corrected=True), *self.regions[1:]])
        with self.assertRaises(ValueError):
            read_suggestions(locked, self.reply([{"region": 0, "start": 0, "end": 1}], locked))

    def test_browser_handoff_requires_a_click_and_review(self):
        from PySide6.QtWidgets import QApplication
        from app.ui.template_ai_dialog import TemplateAiDialog
        app = QApplication.instance() or QApplication([])
        with patch("app.ui.template_ai_dialog.QDesktopServices.openUrl", return_value=True) as browser:
            dialog = TemplateAiDialog(None, self.req)
            browser.assert_not_called()
            dialog.provider.setCurrentIndex(1)
            dialog.manual.setChecked(True)
            dialog.copy_button.click()
            self.assertEqual(browser.call_args.args[0].host(), "gemini.google.com")
            self.assertEqual(app.clipboard().text(), self.req.prompt)
            app.clipboard().setText(self.reply([{"region": 1, "start": 1, "end": 3},
                                                  {"region": 3, "start": 3, "end": 4}]))
            dialog.continue_button.click()
            self.assertEqual(dialog.pages.currentIndex(), 1)
            self.assertIn("제안", dialog.review.toPlainText())
            dialog.continue_button.click()
            self.assertEqual(dialog.result(), dialog.DialogCode.Accepted)
