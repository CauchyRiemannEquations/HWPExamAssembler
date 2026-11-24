"""Collect notices from the interpreter and wheels used to build this executable."""
from __future__ import annotations

from importlib.metadata import distribution
from pathlib import Path
import sqlite3
import ssl
import sys


from cryptography.hazmat.backends.openssl.backend import backend as crypto_backend


PROJECT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "PyInstaller": "https://pyinstaller.org/en/stable/license.html",
    "PySide6": "https://doc.qt.io/qtforpython-6/licenses.html",
    "PySide6_Essentials": "https://doc.qt.io/qt-6/licensing.html",
    "PySide6_Addons": "https://doc.qt.io/qt-6/licensing.html",
    "shiboken6": "https://code.qt.io/cgit/pyside/pyside-setup.git/",
    "pywin32": "https://github.com/mhammond/pywin32",
    "PyJWT": "https://github.com/jpadilla/pyjwt/blob/master/LICENSE",
    "cryptography": "https://github.com/pyca/cryptography/blob/main/LICENSE",
    "cffi": "https://github.com/python-cffi/cffi/blob/main/LICENSE",
    "pycparser": "https://github.com/eliben/pycparser/blob/main/LICENSE",
}


def license_files(name, entries):
    if name == "pywin32":
        # pywin32 distributes these outside its .dist-info directory.
        return sorted(p for p in entries if p.name.lower() == "license.txt" and
                      p.parts[0] in ("adodbapi", "pythonwin", "win32", "win32com"))
    licenses = []
    for p in entries:
        if not p.parts or not p.parts[0].endswith(".dist-info"):
            continue
        if "licenses" in p.parts or p.name.lower().startswith(("license", "copying", "notice")):
            licenses.append(p)
    return sorted(licenses)


def collect(destination):
    qt_version = distribution("PySide6").version
    qt_series = ".".join(qt_version.split(".")[:2])
    python_version = sys.version.split()[0]
    sections = ["HWPExamAssembler · 공용판 오픈소스 고지\n\n"
                "앱 소스는 MIT 라이선스로 제공됩니다. 종속 라이브러리와 글꼴에는 각 저작권자의 조건이 적용됩니다.\n"
                "아래 버전은 이 실행 파일을 만든 환경에서 읽었습니다. 원문이 동일한 배포판은 해당 절을 함께 참조합니다.\n"
                "앱 소스와 빌드 방법: https://github.com/CauchyRiemannEquations/HWPExamAssembler",
                f"Python {python_version}\nhttps://docs.python.org/3/license.html\n"
                f"대응 소스: https://www.python.org/ftp/python/{python_version}/\n\n"
                + (Path(sys.base_prefix) / "LICENSE.txt").read_text(encoding="utf-8-sig"),
                f"Qt {qt_version} · Qt for Python / PySide6 / Shiboken6\n"
                f"Qt 대응 소스: https://download.qt.io/archive/qt/{qt_series}/{qt_version}/single/\n"
                f"Qt for Python 대응 소스: https://code.qt.io/cgit/pyside/pyside-setup.git/?h=v{qt_version}\n"
                f"모듈별 라이선스 및 제3자 코드 고지: https://doc.qt.io/archives/qt-{qt_series}/licenses-used-in-qt.html\n"
                "Qt for Python 라이선스: https://doc.qt.io/qtforpython-6/licenses.html\n\n"
                + (PROJECT / "assets/licenses/qt-open-source.txt").read_text(encoding="utf-8"),
                f"{ssl.OPENSSL_VERSION} (Python ssl)\n{crypto_backend.openssl_version_text()} (cryptography)\n"
                "https://openssl-library.org/source/license/\n"
                "Apache License 2.0 원문은 Python과 cryptography 절에 수록되어 있습니다.\n\n"
                f"SQLite {sqlite3.sqlite_version}\nhttps://sqlite.org/copyright.html\n\n"
                "Microsoft Visual C++ Runtime\nhttps://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files\n\n"
                "Qt Windows 소프트웨어 OpenGL · Mesa\nhttps://docs.mesa3d.org/license.html",
                "Noto Sans KR · SIL Open Font License 1.1\nhttps://github.com/notofonts/noto-cjk\n\n"
                + (PROJECT / "assets/fonts/OFL.txt").read_text(encoding="utf-8-sig")]
    seen = {}
    for name, url in PACKAGES.items():
        dist = distribution(name)
        parts = [f"{name} {dist.version}\n{url}"]
        expression = dist.metadata.get("License-Expression") or dist.metadata.get("License")
        if expression:
            parts.append(expression)
        files = license_files(name, dist.files or [])
        if not files:
            raise RuntimeError(f"No license text in installed distribution {name}")
        for f in files:
            text = Path(dist.locate_file(f)).read_text(encoding="utf-8-sig").strip()
            if text in seen:
                parts.append(f"{f.name} · {seen[text]} 절과 동일")
            else:
                seen[text] = name
                parts.append(f"{f.name}\n\n{text}")
        sections.append("\n\n".join(parts))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(("\n\n" + "=" * 72 + "\n\n").join(sections) + "\n", encoding="utf-8")


if __name__ == "__main__":
    collect(Path(sys.argv[1]))
