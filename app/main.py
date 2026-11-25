from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
import traceback

from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox


APP_DATA_DIRECTORY = "HWPExamAssembler"
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
if __package__ in {None, ""} and not getattr(sys, "frozen", False):
    sys.path.insert(0, str(BUNDLE_ROOT))


def runtime_project_root() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_DATA_DIRECTORY
    for directory in ("logs", "profiles", "project", "work"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for filename in ("school_profile.json", "target_numbering_style.json"):
        installed = root / "project" / filename
        if not installed.exists():
            shutil.copy2(BUNDLE_ROOT / "project" / filename, installed)
    return root


PROJECT_ROOT = runtime_project_root()

from app.hwp.security import install_official_security_module
from app.services import ExamDocumentService
from app.ui import MainWindow


def prepare_hwp_security_module() -> Path | None:
    bundle_root = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])
    )
    bundled_module = (
        bundle_root
        / "security"
        / "FilePathCheckerModuleExample.dll"
    )
    if not bundled_module.is_file() and not getattr(sys, "frozen", False):
        bundled_module = (
            bundle_root
            / "security"
            / "official-package"
            / "FilePathCheckerModuleExample.dll"
        )
    local_app_root = (
        Path(os.environ.get("LOCALAPPDATA", Path.home()))
        / APP_DATA_DIRECTORY
    )
    if bundled_module.is_file():
        return install_official_security_module(bundled_module, local_app_root)
    return None


def register_ui_font() -> str:
    """Load the bundled Korean font so Qt never falls back to missing glyphs."""
    bundle_root = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])
    )
    font_path = bundle_root / "assets" / "fonts" / "NotoSansKR-Variable.ttf"
    if font_path.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                return families[0]
    return "Malgun Gothic"


def apply_light_theme(application: QApplication) -> None:
    """Keep every Qt control readable even when Windows uses dark mode."""
    application.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f3f6fb"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#17212f"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#edf3fa"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#fffbea"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#17212f"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#17212f"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#152b43"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#cfe5fb"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#082f5a"))
    palette.setColor(QPalette.ColorRole.Link, QColor("#075ea8"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor("#667789"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#667789"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#667789"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor("#e9eef4"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor("#e9eef4"))
    application.setPalette(palette)


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("시험지 편집기 · 공용판")
    bundle_root = BUNDLE_ROOT
    application.setWindowIcon(QIcon(str(bundle_root / "assets" / "exam-assembler.ico")))
    application.setFont(QFont(register_ui_font(), 12))
    apply_light_theme(application)
    try:
        prepare_hwp_security_module()
    except Exception:
        # Keep the editor usable when a managed PC blocks per-user registry
        # writes. HWP will then show its own approval dialog; the detailed
        # cause remains available without exposing a dark startup traceback.
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "security-module-error.log").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
    try:
        initial_document = None
        if len(sys.argv) == 2:
            candidate = Path(sys.argv[1])
            if candidate.suffix.lower() in {".hwp", ".hwpx"} and candidate.is_file():
                initial_document = candidate
        window = MainWindow(ExamDocumentService(PROJECT_ROOT), initial_document=initial_document)
        window.show()
        return application.exec()
    except Exception:
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "startup-error.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        QMessageBox.critical(None, "공용판 실행 오류", f"프로그램을 시작하지 못했습니다.\n오류 로그: {log_path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
