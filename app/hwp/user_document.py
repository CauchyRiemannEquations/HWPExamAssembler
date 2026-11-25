"""Bind a user-owned Hangul tab by absolute document path on one COM apartment."""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import time
from xml.parsers import expat

import pythoncom
import win32com.client

from .application import HwpApplication, HwpAutomationError


def _content_digest(xml):
    raw = xml.encode("utf-8")
    if not any(tag in raw for tag in (b"<BINDATA ", b"<CARETPOS ", b"<AUTONUM ")):
        return hashlib.sha256(raw).digest()

    # Hancom sometimes reports a different Size for the exact same compressed
    # picture. Ignore only that derived number, still hashing the entire payload
    # and every other byte in the document, including other Size attributes.
    parser = expat.ParserCreate("utf-8")
    path = []
    start = None
    hashed_until = 0
    digest = hashlib.sha256()
    view = memoryview(raw)

    def hash_number_attrs(offset, names, replacement):
        nonlocal hashed_until
        tag_end = raw.index(b">", offset)
        # Consume quoted values whole, so text inside another attribute cannot
        # be mistaken for a native attribute name.
        for match in re.finditer(rb"""\s([^\s=/>]+)=(["'])(.*?)\2""", raw[offset:tag_end], re.DOTALL):
            if match[1] in names and match[3].isdigit():
                digest.update(view[hashed_until:offset + match.start(3)])
                digest.update(replacement)
                hashed_until = offset + match.end(3)

    def open_element(name, attrs):
        nonlocal start
        path.append(name)
        if path == ["HWPML", "HEAD", "DOCSETTING", "CARETPOS"]:
            # Scrolling or following a question changes the saved view.
            hash_number_attrs(parser.CurrentByteIndex, (b"List", b"Para", b"Pos"), b"0")
        elif (path[:2] == ["HWPML", "BODY"] and path[-2:] == ["TEXT", "AUTONUM"]
              and ("FOOTER" in path or "HEADER" in path) and attrs.get("NumberType") == "TotalPage"):
            # Pagination updates this computed field's display after a save.
            # Its type, format, position, and all page breaks remain significant.
            hash_number_attrs(parser.CurrentByteIndex, (b"Number",), b"0")
        if (path == ["HWPML", "TAIL", "BINDATASTORAGE", "BINDATA"]
                and attrs.get("Compress") == "true" and attrs.get("Encoding") == "Base64"):
            start = parser.CurrentByteIndex

    def close_element(name):
        nonlocal start, hashed_until
        if name == "BINDATA" and len(path) == 4 and start is not None:
            tag_end = raw.index(b">", start) + 1
            if b" Size=" in raw[start:tag_end]:
                data = b"".join(raw[tag_end:parser.CurrentByteIndex].split())
                try:
                    length = len(base64.b64decode(data, validate=True))
                except (ValueError, binascii.Error):
                    length = 0
                if length:
                    hash_number_attrs(start, (b"Size",), str(length).encode("ascii"))
            start = None
        path.pop()

    parser.StartElementHandler = open_element
    parser.EndElementHandler = close_element
    parser.Parse(raw, True)
    digest.update(view[hashed_until:])
    return digest.digest()


def _matches(document, path):
    name = document.FullName
    return bool(name) and Path(name).is_absolute() and Path(name).resolve() == path


def _find(path):
    rot = pythoncom.GetRunningObjectTable()
    context = pythoncom.CreateBindCtx(0)
    matches = []
    for moniker in rot.EnumRunning():
        try:
            if not moniker.GetDisplayName(context, None).lower().lstrip("!").startswith("hwpobject."):
                continue
            obj = win32com.client.Dispatch(rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch))
            docs = obj.XHwpDocuments
            for i in range(docs.Count):
                doc = docs.Item(i)
                if _matches(doc, path):
                    matches.append((obj, doc))
        except (pythoncom.com_error, OSError, AttributeError):
            continue
    if len(matches) > 1:
        raise HwpAutomationError("같은 편집본이 여러 한글 문서에 열려 있습니다. 하나의 편집 창만 남겨 주세요.")
    return matches[0] if matches else None


def is_document_open(path):
    pythoncom.CoInitialize()
    try:
        return _find(Path(path).resolve()) is not None
    finally:
        pythoncom.CoUninitialize()


class UserDocument(HwpApplication):
    """Borrow the COM object. Releasing a borrowed document must never call Quit or Clear."""

    def __init__(self, path):
        super().__init__(visible=False)
        self.path = Path(path).resolve()
        self.doc = None
        self.doc_id = None
        self.hwnd = None
        self._documents = None
        self._windows = None

    def connect(self, is_locked, timeout=16.0, *, only_if_open=False):
        import win32gui
        pythoncom.CoInitialize()
        self._com_initialized = True
        try:
            found = _find(self.path)
            if not found and only_if_open:
                self.close()
                return False
            if not found:
                # A shell-owned document survives releasing our last automation reference.
                if not is_locked(self.path):
                    os.startfile(self.path)
                deadline = time.monotonic() + timeout
                while found is None and time.monotonic() < deadline:
                    pythoncom.PumpWaitingMessages()
                    time.sleep(0.12)
                    found = _find(self.path)
            if not found:
                raise HwpAutomationError("열린 한글 문서의 경로를 확인할 수 없습니다. 해당 편집본을 한글에서 확인하세요.")
            self._hwp, self.doc = found
            # These collections belong to the application. The active item
            # is deliberately looked up again on every identity check.
            self._documents = self.automation.XHwpDocuments
            self._windows = self.automation.XHwpWindows
            self.doc_id = int(self.doc.DocumentID)
            if self.security_module:
                self.security_module_registered = bool(self.automation.RegisterModule(
                    "FilePathCheckDLL", self.security_module
                ))
            self.activate_document(self.doc)
            self.hwnd = int(self._windows.Active_XHwpWindow.WindowHandle)
            self.require_target()
            if not win32gui.IsWindowVisible(self.hwnd):
                raise HwpAutomationError("한글 창이 표시되지 않아 편집본에 연결할 수 없습니다.")
            return True
        except BaseException:
            self.close()
            raise

    def active_document(self):
        return self._documents.Active_XHwpDocument

    def state(self):
        if self.doc is None:
            return "closed"
        try:
            count = self._documents.Count
            if not count:
                return "closed"
            active = self.active_document()
            if int(active.DocumentID) == self.doc_id:
                if not _matches(active, self.path):
                    return "closed"
                hwnd = int(self._windows.Active_XHwpWindow.WindowHandle)
                return "active" if hwnd == self.hwnd else "inactive"
            for i in range(count):
                doc = self._documents.Item(i)
                if int(doc.DocumentID) == self.doc_id and _matches(doc, self.path):
                    return "inactive"
            return "closed"
        except pythoncom.com_error as error:
            # Only disconnection is evidence that the linked document is gone.
            # RPC_E_CALL_REJECTED during a Hangul dialog is temporary.
            if error.hresult in (-2147417848, -2147023174, -2147220995):
                return "closed"
            return "inactive"
        except AttributeError:
            # A quitting automation server can drop XHwpDocuments.Count
            # before pywin32 sees RPC_E_DISCONNECTED.
            import win32gui
            return "inactive" if self.hwnd and win32gui.IsWindow(self.hwnd) else "closed"

    def require_target(self, path=None):
        import win32gui
        path = self.path if path is None else path
        active = self.active_document()
        hwnd = int(self._windows.Active_XHwpWindow.WindowHandle)
        if (int(active.DocumentID) != self.doc_id or not _matches(active, path)
                or hwnd != self.hwnd or not win32gui.IsWindow(hwnd)):
            raise HwpAutomationError("연결된 한글 편집본이 현재 탭이 아닙니다. 해당 문서로 돌아와 다시 시도하세요.")

    @contextmanager
    def guard_input(self):
        import win32gui
        self.require_target()
        if not win32gui.IsWindowEnabled(self.hwnd):
            raise HwpAutomationError("한글 대화상자를 먼저 닫아 주세요.")
        win32gui.EnableWindow(self.hwnd, False)
        try:
            self.require_target()
            yield
        finally:
            if win32gui.IsWindow(self.hwnd):
                win32gui.EnableWindow(self.hwnd, True)

    def show_target(self):
        import win32gui
        if self.state() == "closed":
            raise HwpAutomationError("연결한 문서가 닫혔거나 다른 이름으로 저장되었습니다.")
        self.activate_document(self.doc)
        self.require_target()
        try:
            if win32gui.IsIconic(self.hwnd):
                win32gui.ShowWindow(self.hwnd, 9)  # SW_RESTORE
            win32gui.SetForegroundWindow(self.hwnd)
        except (OSError, win32gui.error):
            # Windows may reserve foreground activation for the last input owner.
            pass

    def save_current(self):
        """Write unsaved changes to their exact original native format and name."""
        self.require_target()
        fmt = {".hwp": "HWP", ".hwpx": "HWPX"}[self.path.suffix.lower()]
        previous_mode = self.automation.SetMessageBoxMode(0x00001000)
        try:
            saved = self.doc.SaveAs(str(self.path), fmt, "fullsave:true")
        finally:
            self.automation.SetMessageBoxMode(previous_mode)
        self.require_target()
        if not saved or self.doc.Modified:
            raise HwpAutomationError("한글의 현재 수정 내용을 저장하지 못했습니다.")
        with self.path.open("rb") as source:
            magic = bytes.fromhex("d0cf11e0a1b11ae1") if fmt == "HWP" else b"PK"
            if source.read(len(magic)) != magic:
                raise HwpAutomationError("한글 저장 형식을 확인할 수 없습니다.")

    def content_fingerprint(self):
        # Whole native XML includes text, styles, equations, pictures and
        # document settings. Binary HWP exports regenerate storage timestamps.
        xml = self.automation.GetTextFile("HWPML2X", "")
        if not isinstance(xml, str) or "<HWPML" not in xml or "</HWPML>" not in xml:
            raise HwpAutomationError("한글의 현재 문서 내용을 비교할 수 없습니다.")
        return _content_digest(xml)

    def reopen(self, old_path, new_path, *, unchanged_content=None):
        self.require_target(old_path)
        old_dirty = self.doc.Modified
        if old_dirty:
            # HWP 2022 can set Modified to 1 or 2 after a read/export or layout
            # pass. Never dismiss that flag without checking the actual document.
            if unchanged_content is None or self.content_fingerprint() != unchanged_content:
                raise HwpAutomationError("한글에 새 수정이 있어 문서 반영을 중단했습니다.")
            self.doc.Modified = 0
        try:
            if not self.doc.Open(str(new_path), "", ""):
                raise HwpAutomationError(f"같은 한글 창에 문서를 불러올 수 없습니다: {new_path}")
            self.require_target(new_path)
        except Exception:
            if old_dirty and _matches(self.doc, old_path):
                self.doc.Modified = old_dirty
            raise

    def close(self):
        self._documents = self._windows = None
        self.doc = self._hwp = None
        if self._com_initialized:
            pythoncom.CoUninitialize()
            self._com_initialized = False
