from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import winreg


MODULE_NAME = "FilePathCheckerModuleExample"
OFFICIAL_MODULE_SHA256 = (
    "9AC5B97C47AC8AED1E8BCA27A3EEF39411361D8F68C262509F0C40A8F9D21BB6"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def install_official_security_module(
    bundled_module: str | Path,
    local_app_root: str | Path,
) -> Path:
    """Install and register Hancom's official Automation path checker.

    This does not click or bypass an approval dialog. It uses Hancom's
    documented FilePathCheckDLL extension point. Hancom's sample checker may
    ask the user once and remembers `allow all` for that HWP process only.
    """
    source = Path(bundled_module).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if _sha256(source) != OFFICIAL_MODULE_SHA256:
        raise RuntimeError("한컴 공식 보안 모듈의 파일 지문이 다릅니다.")

    destination_dir = Path(local_app_root).resolve() / "security"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{MODULE_NAME}.dll"
    if not destination.is_file() or _sha256(destination) != OFFICIAL_MODULE_SHA256:
        shutil.copy2(source, destination)

    registry_bases = (
        r"Software\HNC\HwpAutomation\Modules",
        r"Software\HNC\HwpCtrl\Modules",
    )
    view_flags = {0}
    if hasattr(winreg, "KEY_WOW64_32KEY"):
        view_flags.add(winreg.KEY_WOW64_32KEY)
    if hasattr(winreg, "KEY_WOW64_64KEY"):
        view_flags.add(winreg.KEY_WOW64_64KEY)
    for base in registry_bases:
        for view in view_flags:
            access = winreg.KEY_SET_VALUE | view
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, base, 0, access
            ) as key:
                winreg.SetValueEx(
                    key, MODULE_NAME, 0, winreg.REG_SZ, str(destination)
                )
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, base + r"\Uses", 0, access
            ) as key:
                winreg.SetValueEx(
                    key, MODULE_NAME, 0, winreg.REG_DWORD, 1
                )
    return destination
