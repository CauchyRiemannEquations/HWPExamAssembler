"""Format evidence review, using the existing school application's Qt theme."""
from PySide6.QtCore import Qt
import json
import hashlib
import sys
from pathlib import Path
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QFileDialog, QMessageBox, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

from app.services.format_analysis import Evidence, FormatReport, parse_instructions
from app.services.ai_template_profile import load_profile, profile_evidence


def display_value(prop, value):
    words = {'Justify': '양쪽 정렬', 'Left': '왼쪽', 'Right': '오른쪽', 'Center': '가운데',
             'Top': '위', 'Bottom': '아래', 'Paper': '종이', 'Page': '쪽', 'Column': '단',
             'Para': '문단', 'TopAndBottom': '위아래', 'Square': '어울림',
             'InFrontOfText': '글 앞으로', 'BehindText': '글 뒤로', 'true': '예', 'false': '아니요'}
    if prop in {'position', 'outside', 'inside'}:
        try:
            data = json.loads(value.removesuffix(' HWPUNIT'))
            mm = lambda v: f'{float(v) * 25.4 / 7200:.2f} mm'
            if prop == 'position':
                parts = []
                for axis, label in [('Horz', '가로'), ('Vert', '세로')]:
                    rel, align = data.get(axis + 'RelTo', '?'), data.get(axis + 'Align', '?')
                    parts.append(f'{label}: {words.get(rel, rel)} 기준 · {words.get(align, align)} · 이동 {mm(data.get(axis + "Offset", 0))}')
                if 'AllowOverlap' in data:
                    parts.append('겹침 허용: ' + words.get(data['AllowOverlap'], data['AllowOverlap']))
                return '\n'.join(parts)
            return ' / '.join(f'{words.get(k, k)} {mm(v)}' for k, v in data.items())
        except (ValueError, TypeError):
            return value
    return words.get(value, value)


class FormatReviewDialog(QDialog):
    def __init__(self, report: FormatReport, extra_instructions: str = "", parent=None, *, template_path=None):
        super().__init__(parent)
        self.report = report
        self.template_path = Path(template_path) if template_path else None
        self.base_lines = list(report.instruction_lines)
        # The base report contains document instructions only; supplemental rules
        # are reparsed on each edit and never accumulated as stale declarations.
        self.setWindowTitle('학교 양식 인식 결과')
        self.resize(1120, 860)
        layout = QVBoxLayout(self)
        title = QLabel('학교 양식 확인')
        title.setObjectName('pageTitle')
        layout.addWidget(title)
        description = QLabel('예시에서 읽은 서식과 글로 적힌 지시를 비교합니다. 여러 값이나 충돌은 임의로 하나를 선택하지 않습니다.\n'
                             '이 단계는 서식을 읽고 검토하는 기능입니다. 문서 서식 자동 변경과 화면의 겹침·잘림 검사는 수행하지 않습니다.')
        description.setWordWrap(True)
        layout.addWidget(description)
        ai_notice = QLabel('AI 분석에는 실제 문항·정답이 없는 빈 양식과 서식 지침만 제공하면 됩니다. 빈 양식의 서식 분석은 시험 문제 제공과 다릅니다.\n'
                           '이 프로그램은 AI에 파일을 자동 전송하지 않습니다. 예시 문항이 든 파일은 빈 양식과 구분해 주세요.')
        ai_notice.setWordWrap(True)
        layout.addWidget(ai_notice)
        toolbar = QHBoxLayout()
        for label, action in [('AI 분석 프롬프트 복사', self.copy_ai_prompt),
                              ('AI용 서식 근거 저장…', self.export_native_evidence),
                              ('AI JSON 불러오기…', self.choose_ai_profile),
                              ('JSON 전체 근거', self.show_external_notes),
                              ('JSON 제외', self.clear_ai_profile)]:
            button = QPushButton(label)
            button.clicked.connect(action)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.external_toggle = QCheckBox('JSON의 비교 가능한 후보를 이번 작업의 기준으로 사용 (추정·조건부 규칙 제외)')
        self.external_toggle.setChecked(report.external_enabled)
        self.external_toggle.setEnabled(bool(report.external_rules))
        self.external_toggle.toggled.connect(self.reparse)
        layout.addWidget(self.external_toggle)
        if report.external_profile:
            rules, notes, allowed = profile_evidence(report.external_profile, self.template_path)
            report.external_rules, report.external_notes = rules, notes
            self.external_toggle.setEnabled(allowed and bool(rules))
            if not allowed:
                self.external_toggle.blockSignals(True)
                self.external_toggle.setChecked(False)
                self.external_toggle.blockSignals(False)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['확인 항목', '예시에서 읽은 값', '비교 기준', 'AI JSON 후보', '판정'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.show_evidence)
        layout.addWidget(self.table, 1)
        self.evidence = QPlainTextEdit()
        self.evidence.setReadOnly(True)
        self.evidence.setMaximumHeight(105)
        self.evidence.setPlaceholderText('항목을 선택하면 실제 서식과 지시문의 근거 위치가 표시됩니다.')
        layout.addWidget(self.evidence)
        layout.addWidget(QLabel('추가 서식 지시 또는 수정한 지시를 입력하세요. 예: 본문 바탕 10pt, 줄간격 160%'))
        self.instructions = QPlainTextEdit(extra_instructions)
        self.instructions.setMaximumHeight(75)
        self.instructions.textChanged.connect(self.reparse)
        layout.addWidget(self.instructions)
        self.override = QCheckBox('추가 지시를 이 작업의 기준으로 지정 (양식 지시보다 우선, 근거는 모두 보관)')
        self.override.setChecked(bool(report.overrides))
        self.override.toggled.connect(self.reparse)
        layout.addWidget(self.override)
        self.issues = QLabel()
        self.issues.setTextFormat(Qt.TextFormat.PlainText)
        self.issues.setWordWrap(True)
        layout.addWidget(self.issues)
        self.acknowledge = QCheckBox('확인 불가·충돌·미해석 항목을 확인했습니다. 필요한 수정은 한글에서 진행합니다.')
        self.acknowledge.toggled.connect(self.update_accept)
        layout.addWidget(self.acknowledge)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton('돌아가기')
        self.cancel_button.clicked.connect(self.reject)
        self.accept_button = QPushButton('확인 결과 저장')
        self.accept_button.setObjectName('primaryButton')
        self.accept_button.clicked.connect(self.accept_review)
        buttons.addStretch()
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.accept_button)
        layout.addLayout(buttons)
        self.reparse()

    def reparse(self):
        lines = self.base_lines + self.instructions.toPlainText().splitlines()
        fonts = tuple(e.value for e in self.report.observed if e.property == 'font')
        self.report.declared, self.report.unresolved = parse_instructions(lines, fonts)
        extra, _ = parse_instructions(self.instructions.toPlainText().splitlines(), fonts)
        self.report.overrides = [Evidence(e.role, e.property, e.value, '사용자 지정', e.location)
                                 for e in extra] if self.override.isChecked() else []
        self.report.reviewed = False
        self.report.external_enabled = self.external_toggle.isChecked() and self.external_toggle.isEnabled()
        self.acknowledge.setChecked(False)
        self.rows = self.report.rows()
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            values = [row['label'], ' / '.join(display_value(row['property'], v) for v in row['observed']) or '확인 불가',
                      ('사용자 지정: ' if row['user_override'] else '') + (' / '.join(row['declared']) or '명시 지시 없음'),
                      ' / '.join(row['external']) or '없음', row['status']]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if '충돌' in row['status'] or '불일치' in row['status']:
                    item.setBackground(QColor('#fff1dc'))
                self.table.setItem(index, col, item)
        self.table.resizeRowsToContents()
        for index in range(len(self.rows)):
            self.table.setRowHeight(index, min(85, self.table.rowHeight(index)))
        messages = self.report.warnings + self.report.unresolved + self.report.external_notes
        profile_name = self.report.external_profile['profile_name'] if self.report.external_profile else '없음'
        self.issues.setText(f'AI JSON: {profile_name} · 미해석 지시 {len(self.report.unresolved)}개 · 전체 JSON 근거도 확인하세요.')
        self.issues.setToolTip('\n'.join(messages))
        self.evidence.setPlainText('\n'.join(messages))
        self.update_accept()

    def copy_ai_prompt(self):
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
        try:
            text = (root / 'assets' / 'ai-template' / 'analysis-prompt.txt').read_text(encoding='utf-8')
            QApplication.clipboard().setText(text)
            self.evidence.setPlainText('상세 프롬프트를 복사했습니다. 개인적으로 사용하는 AI에 양식·작성 지침·서식 근거 파일과 함께 제공하세요.')
        except OSError as error:
            QMessageBox.warning(self, '프롬프트 읽기 실패', str(error))

    def export_native_evidence(self):
        path, _ = QFileDialog.getSaveFileName(self, 'AI 분석용 서식 근거 저장', '학교양식_서식근거.json', 'JSON (*.json)')
        if not path:
            return
        try:
            template = {'name': self.template_path.name if self.template_path else '미지정',
                        'sha256': hashlib.sha256(self.template_path.read_bytes()).hexdigest() if self.template_path else None}
            current = self.report.to_dict()
            exported = {'type': 'hwp-exam-native-evidence', 'version': 1, 'template': template,
                        'observed': current['observed'], 'instruction_lines': current['instruction_lines'],
                        'warnings': current['warnings'], 'limits': current['limits']}
            Path(path).write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding='utf-8')
            self.evidence.setPlainText('서식 속성과 지시문 근거를 로컬 파일로 저장했습니다. 원문 문제 전체와 그림 데이터는 이 파일에 넣지 않았습니다.')
        except OSError as error:
            QMessageBox.warning(self, '서식 근거 저장 실패', str(error))

    def choose_ai_profile(self):
        path, _ = QFileDialog.getOpenFileName(self, 'AI 분석 JSON 선택', '', 'JSON (*.json)')
        if not path:
            return
        try:
            self.import_ai_profile(path)
        except (ValueError, OSError, TypeError) as error:
            QMessageBox.warning(self, 'AI JSON 확인 필요', str(error))

    def import_ai_profile(self, path):
        data = load_profile(path)
        rules, notes, allowed = profile_evidence(data, self.template_path)
        # Validate everything before replacing the existing profile.
        self.report.external_profile = data
        self.report.external_rules = rules
        self.report.external_notes = notes
        self.external_toggle.blockSignals(True)
        self.external_toggle.setChecked(False)
        self.external_toggle.setEnabled(allowed and bool(rules))
        self.external_toggle.blockSignals(False)
        self.reparse()
        self.show_external_notes()

    def show_external_notes(self):
        self.evidence.setPlainText('\n\n'.join(self.report.external_notes) or '불러온 JSON이 없습니다.')

    def clear_ai_profile(self):
        self.report.external_profile = None
        self.report.external_rules = []
        self.report.external_notes = []
        self.external_toggle.blockSignals(True)
        self.external_toggle.setChecked(False)
        self.external_toggle.setEnabled(False)
        self.external_toggle.blockSignals(False)
        self.reparse()

    def show_evidence(self):
        row = self.table.currentRow()
        if 0 <= row < len(getattr(self, 'rows', [])):
            self.evidence.setPlainText('\n'.join(f"{e['source']} · {e['location']} → {e['value']}"
                                                 for e in self.rows[row]['evidence']) or '인식할 근거가 없습니다.')

    def update_accept(self):
        self.accept_button.setEnabled(self.acknowledge.isChecked())

    def accept_review(self):
        self.report.reviewed = True
        self.accept()
