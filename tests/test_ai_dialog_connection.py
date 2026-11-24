"""Provider buttons, cancellation, acknowledgement and review at the normal Qt entry point."""
import json
import os
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
from app.services.chatgpt_analysis import ChatGPTAnalysisError
from app.services.template_ai import AnalysisRequest
from app.services.template_model import Region
from app.ui import template_ai_dialog as ui


class AiDialogConnectionTest(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.app._exam_chatgpt_session = self.app._exam_google_client = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.settings = MagicMock()
        self.settings.value.return_value = False
        self.stack.enter_context(patch.object(ui, "QSettings", return_value=self.settings))
        self.session = SimpleNamespace(client_id="test-client", label="교사", expires_at=time.time()+500)
        self.accounts = self.stack.enter_context(patch.object(ui.chatgpt_auth, "saved_accounts", return_value=()))
        self.stack.enter_context(patch.object(ui.gemini_analysis, "find_client_config", return_value=None))
        self.request = AnalysisRequest("demo", "STRUCTURE ONLY", ((0,0,0), (0,1,0), (0,2,0)),
            (Region("insert","insert","삽입점",(0,1,0),(0,1,0),"문항 위치"),))
        self.dialog = ui.TemplateAiDialog(None, self.request)
        self.addCleanup(self.dialog.reject)
        self.reply = json.dumps({"request_id":"demo","changes":[{"region":0,"start":2,"end":2}]})

    def wait_for(self, condition):
        until = time.monotonic()+5
        while not condition() and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.01)
        self.assertTrue(condition())

    def test_chatgpt_login_click_to_review_and_reuse(self):
        with patch.object(ui.chatgpt_auth, "sign_in", return_value=self.session) as login, \
             patch.object(ui.chatgpt_analysis, "analyze_template", return_value=self.reply) as analyze, \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Ok) as welcome:
            self.accounts.return_value = (self.session,)
            login.assert_not_called()
            analyze.assert_not_called()
            self.dialog.continue_button.click()
            self.wait_for(lambda: self.dialog.pages.currentIndex() == 1)
            login.assert_called_once()
            welcome.assert_called_once()
            analyze.assert_called_once()
            self.assertEqual(analyze.call_args.args, (self.session,self.request.prompt))
            self.assertEqual(self.dialog.result(), 0)
            self.dialog.back.click()
            self.dialog.continue_button.click()
            self.wait_for(lambda: self.dialog.pages.currentIndex() == 1)
            self.assertEqual(analyze.call_count, 2)
            login.assert_called_once()
            self.dialog.continue_button.click()
            self.assertEqual(self.dialog.proposal[0].start, (0,2,0))

    def test_first_login_cancel_keeps_structure_local(self):
        with patch.object(ui.chatgpt_auth, "sign_in", return_value=self.session), \
             patch.object(ui.chatgpt_analysis, "analyze_template") as analyze, \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            self.accounts.return_value = (self.session,)
            self.dialog.continue_button.click()
            self.wait_for(lambda: self.dialog.continue_button.text() == "양식 분석")
            analyze.assert_not_called()

    def test_cancel_during_login_does_not_start_inference(self):
        stopped = threading.Event()
        def authorize(*, cancelled, client_id):
            deadline = time.monotonic()+4
            while not cancelled() and time.monotonic() < deadline:
                time.sleep(.01)
            stopped.set()
            return self.session
        with patch.object(ui.chatgpt_auth, "sign_in", side_effect=authorize), \
             patch.object(ui.chatgpt_analysis, "analyze_template") as analyze:
            self.dialog.continue_button.click()
            self.dialog.reject()
            self.wait_for(stopped.is_set)
            analyze.assert_not_called()
            self.assertIsNone(self.app._exam_chatgpt_session)

    def test_expired_session_returns_to_sign_in(self):
        self.accounts.return_value = (self.session,)
        self.dialog.load_accounts(self.session.client_id)
        self.app._exam_chatgpt_session = self.session
        with patch.object(ui.chatgpt_analysis, "analyze_template",
                side_effect=ChatGPTAnalysisError("다시 연결하세요.",status=401)):
            self.dialog.continue_button.click()
            self.wait_for(lambda: self.app._exam_chatgpt_session is None)
            self.assertEqual(self.dialog.continue_button.text(), "ChatGPT로 계속")
            self.assertIsNone(self.dialog.proposal)

    def test_gemini_requires_configuration_and_reviews_before_applying(self):
        self.dialog.provider.setCurrentIndex(1)
        self.assertFalse(self.dialog.continue_button.isEnabled())
        with patch.object(ui.QFileDialog, "getOpenFileName", return_value=("synthetic-client.json","")), \
             patch.object(ui.gemini_analysis, "analyze_with_gemini", return_value=self.reply) as analyze:
            self.dialog.choose_google_client()
            self.dialog.continue_button.click()
            self.wait_for(lambda: self.dialog.pages.currentIndex() == 1)
            analyze.assert_called_once()
            self.assertEqual(analyze.call_args.args,(self.request.prompt,))
            self.assertEqual(analyze.call_args.kwargs["client_path"],Path("synthetic-client.json"))
            self.assertEqual(self.dialog.result(),0)


if __name__ == "__main__":
    unittest.main()
