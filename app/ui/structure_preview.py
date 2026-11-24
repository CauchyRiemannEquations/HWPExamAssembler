from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QPlainTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from app.services import StructureAnalysis


DIALOG_STYLE = """
    QDialog, QWidget#stage { background: white; color: #172f41; }
    QLabel, QCheckBox { background: transparent; color: #243d49; }
    QLabel#stageTitle { font-size: 22pt; font-weight: 700; }
    QLabel#sectionTitle { font-size: 13pt; font-weight: 700; }
    QLabel#stageStep { color: #105c5a; font-size: 10pt; font-weight: 700; }
    QLabel#muted, QCheckBox { color: #536775; font-size: 10pt; }
    QLabel#notice { background: #f0f6f5; color: #284e50; border-radius: 6px; padding: 10px 12px; }
    QLabel#attention { color: #894c18; font-weight: 700; }
    QTreeWidget, QPlainTextEdit, QLineEdit, QComboBox {
        background: white; color: #172f41; border: 1px solid #cedbdd;
        border-radius: 6px; selection-background-color: #d5ebe5; selection-color: #153b38;
    }
    QTreeWidget { padding: 0; alternate-background-color: #f7faf9; }
    QTreeView::item { padding: 6px 4px; }
    QHeaderView::section {
        background: #f1f5f5; color: #476165; border: 0; border-bottom: 1px solid #dce4e9;
        padding: 6px; font-weight: 600;
    }
    QLineEdit, QComboBox { min-height: 28px; padding: 4px 8px; }
    QPushButton {
        background: white; color: #244247; border: 1px solid #bdcdce;
        padding: 6px 13px; min-height: 26px; border-radius: 6px; font-weight: 600;
    }
    QPushButton:hover { background: #f1f7f5; border-color: #688d88; }
    QPushButton#stagePrimary { background: #105c5a; border-color: #105c5a; color: white; }
    QPushButton#stagePrimary:hover { background: #0b4a49; }
    QPushButton:disabled, QPushButton#stagePrimary:disabled {
        background: #eef2f3; border-color: #dce4e9; color: #71838b;
    }
    QCheckBox::indicator {
        width: 15px; height: 15px; border: 1px solid #8aa7a4;
        border-radius: 3px; background: white;
    }
    QCheckBox::indicator:checked { background: #105c5a; border: 3px double white; }
    QProgressBar { border: 0; border-radius: 2px; background: #e3edeb; max-height: 4px; }
    QProgressBar::chunk { background: #287d73; }
"""


MODE_LABELS = {
    "sequential": "순차형",
    "mixed_passage": "혼합 지문형",
    "objective_passage": "지문·자료형",
}


class StructurePreviewDialog(QDialog):
    def __init__(self, analysis: StructureAnalysis, source_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("출제 파일 확인")
        self.setStyleSheet(DIALOG_STYLE)
        self.resize(720, 520 if analysis.groups or analysis.fixed_blocks else 330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        step = QLabel("출제 파일 확인")
        step.setObjectName("stageStep")
        layout.addWidget(step)
        title = QLabel(source_name)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        title.setObjectName("stageTitle")
        layout.addWidget(title)
        mode = MODE_LABELS.get(analysis.suggested_mode, analysis.suggested_mode)
        summary = QLabel(f"{analysis.summary()}\n자동 분석 유형  {mode}")
        summary.setObjectName("notice")
        summary.setWordWrap(True)
        layout.addWidget(summary)

        if analysis.groups or analysis.fixed_blocks:
            items = QTreeWidget()
            items.setRootIsDecorated(False)
            items.setHeaderLabels(["항목", "감지한 범위", "수합 방법"])
            items.setWordWrap(True)
            items.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            items.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            items.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            items.header().setStretchLastSection(False)
            for item in analysis.groups:
                row = QTreeWidgetItem(items, [
                    "공통 묶음", f"{item.label}  {item.detail}\n문단 {item.paragraph}",
                    f"{item.member_count_hint}문항 함께 이동",
                ])
                row.setToolTip(1, item.detail)
            for item in analysis.fixed_blocks:
                QTreeWidgetItem(items, ["고정 안내문 후보", f"{item.label}\n문단 {item.paragraph}", "복사 제외"])
            layout.addWidget(items, 1)
        else:
            note = QLabel("공통 묶음과 고정 안내문 후보가 없습니다.")
            note.setObjectName("muted")
            layout.addWidget(note)

        if analysis.warnings:
            caption = QLabel("확인이 필요한 내용")
            caption.setObjectName("attention")
            layout.addWidget(caption)
            warnings = QPlainTextEdit()
            warnings.setReadOnly(True)
            warnings.setMaximumHeight(min(115, 30 + len(analysis.warnings) * 22))
            warnings.setPlainText("\n".join(f"• {warning}" for warning in analysis.warnings))
            layout.addWidget(warnings)
        if not analysis.groups and not analysis.fixed_blocks:
            layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        approve = buttons.button(QDialogButtonBox.StandardButton.Ok)
        approve.setText("이 파일 수합")
        approve.setObjectName("stagePrimary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("이 파일 건너뛰기")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
