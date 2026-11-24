"""Optional provider sign-in and teacher approval for school template regions."""
from pathlib import Path
import time

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from app.services import chatgpt_analysis, chatgpt_auth, gemini_analysis
from app.services.template_ai import make_request, read_suggestions
from app.services.template_model import area_detail
from app.ui.template_ai_task import AnalysisTask


class TemplateAiDialog(QDialog):
    def __init__(self, parent, request):
        super().__init__(parent)
        self.request = request
        self.proposal = None
        self.app = QApplication.instance()
        self.settings = QSettings("HWPExamAssembler", "PublicTemplateAI")
        self.google_client = getattr(self.app, "_exam_google_client", None) or gemini_analysis.find_client_config()
        self.task = AnalysisTask(self)
        self.task.completed.connect(self.finished_step)
        self.task.failed.connect(self.failed_step)
        self.task.progress.connect(self.set_status)
        self.setWindowTitle("학교 양식 · AI 영역 제안")
        self.resize(590, 460)
        layout = QVBoxLayout(self)
        heading = QLabel("양식에서 남길 곳과 문항을 넣을 곳 확인")
        heading.setWordWrap(True)
        heading.setStyleSheet("font-size:19px;font-weight:600;")
        layout.addWidget(heading)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages)
        draft = QWidget()
        first = QVBoxLayout(draft)
        first.setContentsMargins(0, 0, 0, 0)
        info = QLabel("분석을 시작하면 선택한 서비스에 문단 번호와 표·수식·문항 번호 같은 구조 단서를 보냅니다. "
                      "시험 문구, 정답, 학교명과 파일 이름은 요청에서 제외합니다.")
        info.setWordWrap(True)
        first.addWidget(info)
        select = QHBoxLayout()
        self.provider = QComboBox()
        self.provider.addItem("ChatGPT", "https://chatgpt.com/")
        self.provider.addItem("Gemini", "https://gemini.google.com/app")
        select.addWidget(self.provider)
        self.account = QComboBox()
        self.account.setAccessibleName("ChatGPT 계정 선택")
        select.addWidget(self.account)
        select.addStretch()
        self.preview_button = QPushButton("보낼 내용 보기")
        self.preview_button.setCheckable(True)
        select.addWidget(self.preview_button)
        first.addLayout(select)
        self.preview = QPlainTextEdit(request.prompt)
        self.preview.setReadOnly(True)
        self.preview.setVisible(False)
        self.preview_button.toggled.connect(self.preview.setVisible)
        first.addWidget(self.preview)
        self.usage = QLabel('<a href="https://chatgpt.com/settings/usage">ChatGPT 사용량 관리</a>')
        self.usage.setOpenExternalLinks(True)
        account_actions = QHBoxLayout()
        account_actions.addWidget(self.usage)
        account_actions.addStretch()
        self.disconnect_button = QPushButton("계정 연결 해제")
        self.disconnect_button.clicked.connect(self.disconnect_account)
        account_actions.addWidget(self.disconnect_button)
        first.addLayout(account_actions)
        self.details = QLabel()
        self.details.setWordWrap(True)
        first.addWidget(self.details)
        self.config_button = QPushButton("기관용 Google 설정 파일 선택…")
        self.config_button.clicked.connect(self.choose_google_client)
        first.addWidget(self.config_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.manual = QCheckBox("웹에서 복사·붙여넣기로 분석")
        first.addWidget(self.manual)
        self.copy_button = QPushButton("요청 복사하고 브라우저 열기")
        self.copy_button.clicked.connect(self.copy_and_open)
        first.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.answer = QPlainTextEdit()
        self.answer.setPlaceholderText("브라우저에서 답변을 복사하거나 여기에 붙여넣으세요.")
        first.addWidget(self.answer)
        first.addStretch()
        self.hint = QLabel()
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        self.hint.setWordWrap(True)
        first.addWidget(self.hint)
        self.pages.addWidget(draft)
        confirm = QWidget()
        second = QVBoxLayout(confirm)
        second.setContentsMargins(0, 0, 0, 0)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        second.addWidget(self.review)
        self.pages.addWidget(confirm)
        buttons = QHBoxLayout()
        self.back = QPushButton("돌아가기")
        self.back.clicked.connect(self.edit_answer)
        self.back.hide()
        buttons.addWidget(self.back)
        buttons.addStretch()
        self.cancel_button = QPushButton("닫기")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.continue_button = QPushButton()
        self.continue_button.setDefault(True)
        self.continue_button.clicked.connect(self.advance)
        buttons.addWidget(self.continue_button)
        layout.addLayout(buttons)
        self.load_accounts()
        self.manual.toggled.connect(self.refresh_controls)
        self.account.currentIndexChanged.connect(self.refresh_controls)
        self.provider.currentIndexChanged.connect(self.provider_changed)
        self.answer.textChanged.connect(self.refresh_controls)
        self.refresh_controls()

    def load_accounts(self, preferred=None):
        self.account.blockSignals(True)
        self.account.clear()
        try:
            accounts = chatgpt_auth.saved_accounts()
        except chatgpt_auth.AuthError:
            accounts = ()
        for account in accounts:
            self.account.addItem(account.label, account.client_id)
        self.account.addItem("다른 계정으로 로그인" if accounts else "계정 선택", None)
        index = self.account.findData(preferred) if preferred else 0
        self.account.setCurrentIndex(max(index, 0))
        self.account.blockSignals(False)

    def cached_session(self):
        session = getattr(self.app, "_exam_chatgpt_session", None)
        if session and session.expires_at > time.time() + 30 and session.client_id == self.account.currentData():
            return session
        return None

    def set_status(self, text):
        self.hint.setText(text)

    def provider_changed(self):
        self.set_status("")
        self.answer.clear()
        self.refresh_controls()

    def refresh_controls(self):
        if self.pages.currentIndex():
            return
        busy = self.task.running
        gpt = self.provider.currentIndex() == 0
        manual = self.manual.isChecked()
        self.provider.setEnabled(not busy)
        self.account.setEnabled(not busy)
        self.account.setVisible(gpt and not manual and self.account.count() > 1)
        self.manual.setEnabled(not busy)
        self.copy_button.setVisible(manual)
        self.answer.setVisible(manual)
        self.usage.setVisible(gpt and not manual)
        self.disconnect_button.setVisible(gpt and not manual and self.cached_session() is not None)
        self.disconnect_button.setEnabled(not busy)
        self.config_button.setVisible(not manual and not gpt)
        self.config_button.setEnabled(not busy)
        self.cancel_button.setText("취소" if busy else "닫기")
        enabled = True
        if manual:
            description = "개인 AI 웹사이트에서 요청을 보내고 답변을 복사해 오세요."
            action = "제안 확인" if self.answer.toPlainText().strip() else "답변 붙여넣어 확인"
        elif gpt:
            description = "양식 분석 1회에 로그인한 계정의 ChatGPT 요금제 사용량이 적용됩니다. 지원 여부는 계정 연결 시 확인합니다."
            action = "양식 분석" if self.cached_session() else "ChatGPT로 계속"
        elif self.google_client:
            description = "Google 계정으로 승인하면 소속 기관의 Gemini API 프로젝트 사용량으로 양식 구조를 한 번 분석합니다."
            action = "Google로 계속"
        else:
            description = "Gemini 직접 연결에는 기관의 Google 설정이 필요합니다. 개인 Gemini는 아래의 웹 방식으로 이용할 수 있습니다."
            action = "Google로 계속"
            enabled = False
        self.details.setText(description)
        self.continue_button.setText("진행 중…" if busy else action)
        self.continue_button.setEnabled(enabled and not busy)

    def choose_google_client(self):
        filename, _ = QFileDialog.getOpenFileName(self, "기관의 Google 데스크톱 OAuth 설정", "", "Google OAuth 설정 (*.json)")
        if filename:
            self.google_client = self.app._exam_google_client = Path(filename)
            self.set_status("이 실행에서 선택한 Google 설정을 사용합니다.")
            self.refresh_controls()

    def copy_and_open(self):
        QApplication.clipboard().setText(self.request.prompt)
        opened = QDesktopServices.openUrl(QUrl(self.provider.currentData()))
        self.set_status("새 대화에 요청을 붙여넣고 답변을 복사해 오세요." if opened else
                        f"요청을 복사했습니다. 브라우저에서 {self.provider.currentData()}를 여세요.")

    def start_step(self, kind, operation, message):
        self.set_status(message)
        self.task.start(kind, operation)
        self.refresh_controls()

    def advance(self):
        if self.pages.currentIndex():
            self.accept()
        elif self.task.running:
            return
        elif self.manual.isChecked():
            text = self.answer.toPlainText().strip() or QApplication.clipboard().text().strip()
            if not text or text == self.request.prompt:
                self.set_status("브라우저에서 AI의 답변을 먼저 복사하세요.")
                return
            self.review_answer(text)
        elif self.provider.currentIndex() == 0:
            session = self.cached_session()
            if session:
                self.analyze_chatgpt(session)
            else:
                client_id = self.account.currentData()
                self.start_step("signin", lambda stop, status: chatgpt_auth.sign_in(
                    cancelled=stop.is_set, client_id=client_id), "브라우저에서 ChatGPT 계정으로 로그인하고 앱 연결을 승인하세요.")
        elif self.google_client:
            prompt, client = self.request.prompt, self.google_client
            self.start_step("gemini", lambda stop, status: gemini_analysis.analyze_with_gemini(
                prompt, cancel=stop, progress=status, client_path=client), "Google 계정 연결을 준비하고 있습니다.")

    def analyze_chatgpt(self, session):
        prompt = self.request.prompt
        self.start_step("chatgpt", lambda stop, status: chatgpt_analysis.analyze_template(
            session, prompt, cancelled=stop.is_set), "ChatGPT 요금제를 사용해 양식 구조를 분석하고 있습니다.")

    def disconnect_account(self):
        session = self.cached_session()
        if session and not self.task.running:
            self.app._exam_chatgpt_session = None
            self.start_step("disconnect", lambda stop, status: chatgpt_auth.sign_out(session),
                            "ChatGPT 계정 연결을 해제하고 있습니다.")

    def finished_step(self, kind, value):
        self.refresh_controls()
        if kind == "disconnect":
            self.set_status("이 PC에서 ChatGPT 계정 연결을 해제했습니다.")
            return
        if kind != "signin":
            self.review_answer(value)
            return
        self.app._exam_chatgpt_session = value
        self.load_accounts(value.client_id)
        if not self.settings.value("welcome/" + value.client_id, False, type=bool):
            result = QMessageBox.question(self, "ChatGPT 계정 연결 완료",
                "이 계정의 ChatGPT 요금제를 사용합니다. 지금 화면에 표시한 양식 구조로 분석을 시작할까요?",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            self.settings.setValue("welcome/" + value.client_id, True)
            if result != QMessageBox.StandardButton.Ok:
                self.set_status("계정이 연결됐습니다. 준비되면 양식 분석을 누르세요.")
                self.refresh_controls()
                return
        self.analyze_chatgpt(value)

    def failed_step(self, kind, message):
        if kind == "chatgpt" and self.task.last_status == 401:
            self.app._exam_chatgpt_session = None
        self.set_status(message)
        self.refresh_controls()

    def edit_answer(self):
        self.pages.setCurrentIndex(0)
        self.proposal = None
        self.back.hide()
        self.set_status("")
        self.refresh_controls()

    def review_answer(self, text):
        try:
            proposed = read_suggestions(self.request, text)
        except ValueError as error:
            self.set_status(str(error))
            self.refresh_controls()
            return
        differences = [
            f"{after.label}\n  기존  {area_detail(before.start, before.end)}\n  제안  {area_detail(after.start, after.end)}"
            for before, after in zip(self.request.regions, proposed, strict=True) if before != after
        ]
        self.proposal = proposed if differences else None
        self.review.setPlainText("\n\n".join(differences) or "바꿀 위치가 없다는 제안입니다. 한글에서 현재 위치를 확인하세요.")
        self.pages.setCurrentIndex(1)
        self.back.show()
        self.continue_button.setEnabled(True)
        self.continue_button.setText("제안 적용" if differences else "확인")

    def done(self, result):
        self.task.cancel()
        super().done(result)


def review_with_ai(parent, paragraphs, regions):
    try:
        request = make_request(paragraphs, regions)
    except ValueError as error:
        QMessageBox.information(parent, "양식 구조 검토", str(error))
        return None
    dialog = TemplateAiDialog(parent, request)
    return dialog.proposal if dialog.exec() == QDialog.DialogCode.Accepted else None
