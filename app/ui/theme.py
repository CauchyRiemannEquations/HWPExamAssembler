"""Shared Qt styling for the exam workspace."""

EDITOR_STYLE = """
QWidget {
    font-family: "Noto Sans KR", "Malgun Gothic", "Segoe UI";
    font-size: 11pt;
    color: #21383c;
}
QMainWindow, QDialog, QWidget#examHome, QWidget#examEditor { background: #ffffff; }
QLabel { background: transparent; }
QWidget#appBar { background: #f3f7f5; border-bottom: 1px solid #dbe5df; }
QWidget#appBar QLabel { font-size: 10pt; color: #526b66; }
QWidget#appBar QLabel#brandName { font-size: 13pt; font-weight: 700; color: #204441; }
QWidget#appBar QLabel#brandMark { background: #105c5a; color: white; border-radius: 7px; font-size: 14pt; font-weight: 700; }
QLabel#homeTitle { color: #162e32; font-size: 26pt; font-weight: 700; }
QLabel#homeIntro { color: #516465; font-size: 12pt; }
QFrame#importCard { background: #eaf4ee; border: 1px solid #c6ded2; border-radius: 12px; }
QFrame#editCard { background: #f7f9f8; border: 1px solid #dce5e1; border-radius: 12px; }
QLabel#cardTitle { color: #17393a; font-size: 17pt; font-weight: 700; }
QLabel#cardBody { color: #425b5a; font-size: 11pt; }
QLabel#dropHint { color: #627674; font-size: 10pt; padding: 6px 0; }
QLabel#eyebrow { color: #4a6c64; font-size: 10pt; font-weight: 600; }
QLabel#documentTitle { color: #172f36; font-size: 21pt; font-weight: 700; }
QLabel#listTitle { font-size: 12pt; font-weight: 700; }
QLabel#editorHint, QLabel#description, QLabel#editorStatus { color: #526769; font-size: 10pt; }
QWidget#columnTitles { background: #f1f5f3; border-radius: 4px; }
QWidget#columnTitles QLabel { color: #52665f; font-size: 10pt; }
QLabel#statusPanel { background: #eaf3ef; color: #24574e; padding: 8px; border-radius: 6px; }
QPushButton, QComboBox, QLineEdit {
    background: #ffffff; color: #203d42; border: 1px solid #c5d4ce;
    border-radius: 6px; padding: 5px 12px; min-height: 25px; font-size: 10.5pt;
}
QPushButton { font-weight: 600; }
QPushButton:hover, QComboBox:hover { background: #eef5f1; border-color: #79a399; }
QPushButton:pressed { background: #d7e9e1; }
QPushButton:focus, QComboBox:focus, QLineEdit:focus { border-color: #176d68; }
QPushButton#proofButton, QPushButton#primaryButton { background: #105c5a; color: #ffffff; border-color: #105c5a; }
QPushButton#proofButton:hover, QPushButton#primaryButton:hover { background: #0c4948; }
QPushButton:disabled, QComboBox:disabled, QLineEdit:disabled, QPushButton#proofButton:disabled, QPushButton#primaryButton:disabled {
    background: #f1f4f2; border-color: #dce4de; color: #6c7e7c;
}
QListWidget, QTreeWidget, QPlainTextEdit, QTextEdit {
    background: #ffffff; border: 1px solid #d6e0db; border-radius: 5px; padding: 4px;
    selection-background-color: #d8ece3; selection-color: #164d44;
}
QWidget#examEditor QListWidget, QWidget#examEditor QListWidget::item { padding: 0; }
QPlainTextEdit { font-size: 10pt; }
QMenu, QComboBox QAbstractItemView {
    background: white; color: #21383c; border: 1px solid #bdcfc6;
    selection-background-color: #dcece3; selection-color: #164d44;
}
QMenu::item { padding: 6px 18px; }
QMenu::item:selected { background: #e3f0e8; color: #164d44; }
QMenu::item:disabled { color: #899695; }
QToolTip { background: #f5f9f6; color: #203c38; border: 1px solid #90aca1; padding: 4px; }
"""
