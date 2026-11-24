from __future__ import annotations

from dataclasses import dataclass
import ctypes
from pathlib import Path

from app.hwp.transaction import sha256


ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    modified_ns: int
    sha256: str


def file_fingerprint(path: str | Path) -> FileFingerprint:
    document = Path(path).resolve()
    stat = document.stat()
    return FileFingerprint(
        size=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        sha256=sha256(document),
    )


def file_is_in_use(path: str | Path) -> bool:
    """Return True when another Windows process still holds the HWP file."""
    document = Path(path).resolve()
    if not document.is_file():
        raise FileNotFoundError(document)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(document),
        GENERIC_READ,
        0,
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        if error in {ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION}:
            return True
        raise OSError(error, ctypes.FormatError(error), str(document))
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(handle)
    return False
