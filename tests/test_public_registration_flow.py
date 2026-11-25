"""Exercise the Qt registration handoff with an owned, deterministic Hancom stand-in."""
from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from time import monotonic
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtTest import QTest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.hwp.native_template import Paragraph
from app.services import ExamDocumentService
from app.services.template_catalog import recent_templates
from app.services.template_model import file_digest
from app.ui.import_dialog import ImportDialog
from app.ui.main_window import MainWindow


class WorkingCopy:
    hwnd = 0

    def __init__(self, folder):
        self.folder = Path(folder)
        self.closed = False

    def open(self, source):
        path = self.folder / source.name
        path.write_bytes(source.read_bytes())
        self.source_sha256 = file_digest(path)
        return path

    def scan(self):
        lines = ("시험 안내", "1. 값을 구하시오.", "확인 사항")
        return tuple(Paragraph(i, line, line, "", (), False, (0, i, 0),
                               (0, i + 1, 0) if i < 2 else (0, i, len(line)))
                     for i, line in enumerate(lines))

    def state(self):
        return "closed" if self.closed else "active"

    def place(self, pos):
        self.cursor = pos

    def capture(self, *, point):
        assert point
        return (0, 1, 2), (0, 1, 2)

    def close(self):
        self.closed = True


class RegistrationFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.file = self.folder / "내 학교.hwp"
        self.file.write_bytes(b"template")
        self.stack.enter_context(patch("app.ui.template_registration.NativeTemplate", WorkingCopy))
        self.project = Path(__file__).resolve().parents[1]
        self.dialog = ImportDialog(self.project, data_root=self.folder)

    def wait(self, condition):
        deadline = monotonic() + 4
        while not condition() and monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(condition())

    def tearDown(self):
        if self.dialog.registrar is not None:
            self.dialog.registrar.controller.close_session()
            self.wait(lambda: self.dialog.registrar is None)
        self.dialog.deleteLater()

    def test_manual_capture_reaches_import_and_survives_restart(self):
        self.assertFalse(self.dialog.start_button.isEnabled())
        self.dialog.open_registrar(self.file)
        panel = self.dialog.registrar
        self.wait(lambda: panel.state is not None and not panel._busy)
        panel.correct_button.click()
        panel.capture_button.click()
        self.wait(lambda: not panel._busy)
        panel.save_button.click()
        self.wait(lambda: self.dialog.registrar is None)
        entry = self.dialog.template_combo.currentData()
        self.assertEqual(entry.source, self.file)
        saved = json.loads(entry.profile.read_text(encoding="utf-8"))
        anchor = next(r for r in saved["regions"] if r["role"] == "insert")
        self.assertEqual(anchor["start"], [0, 1, 2])

        restored = ImportDialog(self.project, data_root=self.folder)
        self.addCleanup(restored.deleteLater)
        self.assertEqual(restored.template_combo.currentData(), entry)
        self.assertTrue(restored.start_button.isEnabled())
        target = self.folder / "수합.hwp"
        restored.output_edit.setText(str(target))
        with patch.object(restored.session, "start") as start:
            restored.start_button.click()
        self.assertEqual(start.call_args.args, (self.file, target))
        self.assertEqual(start.call_args.kwargs["registration"], entry.profile)
        self.assertEqual(restored.phase, restored.PHASE_IMPORT)

    def test_invalid_ai_position_preserves_local_candidates_and_close_discards(self):
        self.dialog.open_registrar(self.file)
        panel = self.dialog.registrar
        self.wait(lambda: panel.state is not None and not panel._busy)
        original = panel.state.regions
        invalid = tuple(replace(r, start=(0, 99, 0), end=(0, 99, 0)) if r.role == "insert" else r
                        for r in original)
        panel.controller.apply_proposal(invalid)
        self.wait(lambda: not panel._busy)
        self.assertIn("위치", panel.notice.text())
        self.assertEqual(panel.state.regions, original)
        panel.reject()
        self.wait(lambda: self.dialog.registrar is None)
        self.assertEqual(recent_templates(self.folder), [])
        self.assertFalse(self.dialog.start_button.isEnabled())

    def test_first_home_action_opens_registration_and_next_visit_reuses_it(self):
        self.stack.enter_context(patch("app.ui.main_window.template_data_root", return_value=self.folder))
        self.stack.enter_context(patch("app.ui.import_dialog.template_data_root", return_value=self.folder))
        picker = self.stack.enter_context(patch("app.ui.main_window.QFileDialog.getOpenFileName",
                                                return_value=(str(self.file), "")))
        window = MainWindow(ExamDocumentService(self.project))
        self.addCleanup(window.deleteLater)
        window.show()
        self.assertIn("공용판", window.windowTitle())
        self.assertEqual(window.home_import_button.text(), "내 학교 양식 선택…")

        seen = []
        deadline = monotonic() + 4
        timer = QTimer()
        timer.setInterval(15)
        def drive():
            wizard = next((w for w in window.findChildren(ImportDialog) if w.isVisible()), None)
            if wizard is None:
                return
            if monotonic() > deadline:
                wizard.reject()
                return
            panel = wizard.registrar
            if panel is not None and panel.state and not panel._busy and not seen:
                seen.append("registration")
                panel.save_button.click()
            elif panel is None and wizard.start_button.isEnabled():
                seen.append("configured")
                wizard.reject()
        timer.timeout.connect(drive)
        timer.start()
        try:
            window.home_import_button.click()
            self.assertEqual(seen, ["registration", "configured"])
            self.assertEqual(window.home_import_button.text(), "수합 시작")
            self.assertIn(self.file.name, window.home_page.import_description.text())
            window.home_import_button.click()
            self.assertEqual(seen, ["registration", "configured", "configured"])
            self.assertEqual(picker.call_count, 1)
        finally:
            timer.stop()
            window.close()
