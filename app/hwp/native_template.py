"""A local Hancom selection window for the template registration prototype."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import shutil
import threading
import time
from uuid import uuid4
from xml.etree import ElementTree as ET


class TemplateWindowError(RuntimeError):
    pass


Position = tuple[int, int, int]


@dataclass(frozen=True)
class Paragraph:
    paragraph: int
    text: str
    body_text: str
    table_text: str
    controls: tuple[str, ...]
    numbered: bool
    start: Position
    end: Position
    shape_text: str = ""
    header_text: str = ""
    footer_text: str = ""


def _characters(node):
    return "".join(node.itertext())


def _nested_text(item):
    lines = ("".join(_characters(c) for c in p.findall("./TEXT/CHAR")).strip()
             for p in item.iter("P"))
    return " ".join(line for line in lines if line)


def _paragraphs(xml, end, section_start):
    root = ET.fromstring(xml)
    numbering = {
        shape.get("Id") for shape in root.findall("./HEAD/MAPPINGTABLE/PARASHAPELIST/PARASHAPE")
        if shape.get("HeadingType") == "Number"
    }
    paragraphs = root.findall("./BODY/SECTION/P")
    if end[0] != 0 or len(paragraphs) != end[1] + 1:
        raise TemplateWindowError("한글의 본문 문단 경계를 확인할 수 없습니다.")
    records = []
    for index, paragraph in enumerate(paragraphs):
        texts, tables, shapes, headers, footers, controls = [], [], [], [], [], []
        anchored = {"TABLE": tables, "HEADER": headers, "FOOTER": footers}
        for run in paragraph.findall("TEXT"):
            for item in run:
                if item.tag == "CHAR":
                    texts.append(_characters(item))
                elif item.tag in ("TAB", "LINEBREAK"):
                    texts.append(" ")
                else:
                    controls.append(item.tag)
                    # Nested cell, shape and page-header lists have their own
                    # coordinates. Keep their text on the owning body paragraph.
                    if item.tag not in ("SECDEF", "COLDEF"):
                        label = _nested_text(item)
                        if label:
                            anchored.get(item.tag, shapes).append(label)
        body_text = "".join(texts).strip()
        table_text = " ".join(tables)
        shape_text, header_text, footer_text = (" ".join(parts) for parts in (shapes, headers, footers))
        stop = (0, index + 1, 0) if index < end[1] else end
        # Mandatory section/page controls precede the first selectable
        # character. Hancom clamps SetPos(0, 0, 0) to e.g. (0, 0, 80).
        start = section_start(index) if index == 0 or "SECDEF" in controls else (0, index, 0)
        if records and records[-1].end != start:
            records[-1] = replace(records[-1], end=start)
        records.append(Paragraph(
            index, " ".join(t for t in (body_text, table_text, shape_text, header_text, footer_text) if t),
            body_text, table_text, tuple(dict.fromkeys(controls)),
            paragraph.get("ParaShape") in numbering, start, stop,
            shape_text, header_text, footer_text,
        ))
    return tuple(records)


def _positions(result, size):
    values = tuple(result or ())
    if len(values) == size + 1:
        if not values[0]:
            return None
        values = values[1:]
    return tuple(map(int, values)) if len(values) == size else None


class NativeTemplate:
    """Open only an owned working copy, on the thread that created this session."""

    def __init__(self, session_dir):
        self.session_dir = Path(session_dir).resolve()
        self.working_path = None
        self.source_path = None
        self.hwnd = None
        self._hwp = self._doc = self._id = None
        self._com = None
        self._thread = threading.get_ident()
        self._records = ()
        self._digest = None

    def _same_thread(self):
        if threading.get_ident() != self._thread:
            raise TemplateWindowError("한글 연결은 처음 연 스레드에서 사용해야 합니다.")

    def open(self, source_path):
        self._same_thread()
        source = Path(source_path).resolve(strict=True)
        if source.suffix.lower() not in (".hwp", ".hwpx") or not source.is_file():
            raise TemplateWindowError("HWP 또는 HWPX 양식을 선택하세요.")
        directory = self.session_dir / uuid4().hex
        directory.mkdir(parents=True)
        copy = directory / source.name
        try:
            shutil.copyfile(source, copy)
        except Exception:
            copy.unlink(missing_ok=True)
            directory.rmdir()
            raise
        self.close()
        self.source_path, self.working_path = source, copy
        with copy.open("rb") as stream:
            self._digest = hashlib.file_digest(stream, "sha256").digest()
        self.reopen()
        return copy

    def _exact(self, doc):
        name = doc.FullName
        return (int(doc.DocumentID) == self._id and bool(name)
                and Path(name).is_absolute() and Path(name).resolve() == self.working_path)

    def _docs(self):
        docs = self._hwp.XHwpDocuments
        return tuple(docs.Item(index) for index in range(docs.Count))

    def state(self):
        self._same_thread()
        if self._hwp is None or self._doc is None:
            return "closed"
        try:
            if not any(self._exact(doc) for doc in self._docs()):
                return "closed"
            active = self._hwp.XHwpDocuments.Active_XHwpDocument
            hwnd = int(self._hwp.XHwpWindows.Active_XHwpWindow.WindowHandle)
            return "active" if self._exact(active) and hwnd == self.hwnd else "inactive"
        except self._com.com_error as exc:
            if exc.hresult in (-2147417848, -2147023174, -2147220995):
                return "closed"
            raise TemplateWindowError("한글의 대화상자를 확인한 뒤 다시 시도하세요.") from exc

    def _require(self, *, check_readonly=True):
        state = self.state()
        if state == "closed":
            raise TemplateWindowError("양식 작업 사본이 닫혔습니다. 사본을 다시 열어 주세요.")
        if state != "active":
            raise TemplateWindowError("한글에서 양식 작업 사본 탭을 선택한 뒤 다시 시도하세요.")
        if check_readonly and int(self._doc.EditMode) != 0:
            raise TemplateWindowError("작업 사본이 편집 모드입니다. 사본을 닫고 원본 양식을 다시 선택하세요.")

    def _verify_bytes(self):
        if not self.working_path or not self.working_path.is_file():
            raise TemplateWindowError("다시 열 작업 사본이 없습니다.")
        with self.working_path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").digest() != self._digest:
                raise TemplateWindowError("작업 사본 파일이 변경되었습니다. 원본 양식을 다시 선택하세요.")

    def reopen(self):
        self._same_thread()
        self._verify_bytes()
        if self.state() != "closed":
            self.show()
            return self.working_path
        self.close()
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        self._com = pythoncom
        try:
            hwp = self._hwp = win32com.client.DispatchEx("HWPFrame.HwpObject.2")
            docs = self._docs()
            if len(docs) != 1 or docs[0].FullName or docs[0].Modified:
                raise TemplateWindowError("독립된 새 한글 창을 준비하지 못했습니다.")
            doc = docs[0]
            self._id = int(doc.DocumentID)
            self._doc = doc
            window = hwp.XHwpWindows.Active_XHwpWindow
            self.hwnd = int(window.WindowHandle)
            window.Visible = True
            hwp.RegisterModule("FilePathCheckDLL", "FilePathCheckerModuleExample")
            if not doc.Open(str(self.working_path), "", ""):
                raise TemplateWindowError("한글에서 작업 사본을 열 수 없습니다.")
            self._require(check_readonly=False)
            # The registrar records coordinates against the bytes it was given.
            # Reading mode permits text selections while keeping these anchors stable.
            doc.EditMode = 0
            if int(doc.EditMode) != 0:
                raise TemplateWindowError("양식 사본을 읽기 전용으로 전환할 수 없습니다.")
        except Exception:
            self.close()
            raise
        return self.working_path

    def show(self):
        self._same_thread()
        if self.state() == "closed":
            raise TemplateWindowError("양식 작업 사본을 다시 열어 주세요.")
        if self.state() != "active":
            self._doc.SetActive_XHwpDocument()
            self._com.PumpWaitingMessages()
            time.sleep(0.04)
            self._require()
        import win32gui
        window = self._hwp.XHwpWindows.Active_XHwpWindow
        window.Visible = True
        if win32gui.IsIconic(self.hwnd):
            win32gui.ShowWindow(self.hwnd, 9)
        try:
            win32gui.SetForegroundWindow(self.hwnd)
        except (OSError, win32gui.error):
            pass

    def _caret(self):
        position = _positions(self._hwp.GetPos(), 3)
        if position is None:
            raise TemplateWindowError("한글의 현재 커서 위치를 읽을 수 없습니다.")
        return position

    def scan(self):
        self._require()
        if not self._records:
            previous = self._caret()
            selected = _positions(self._hwp.GetSelectedPos(), 6)
            self._hwp.Run("Cancel")
            try:
                self._hwp.Run("MoveDocEnd")
                end = self._caret()
                xml = self._hwp.GetTextFile("HWPML2X", "")
                self._records = _paragraphs(xml, end, self._section_start)
            finally:
                self._require()
                if selected and selected[:3] != selected[3:]:
                    self._select(selected[:3], selected[3:])
                else:
                    self._hwp.SetPos(*previous)
        return self._records

    def _section_start(self, index):
        if self._hwp.SetPos(0, index, 0):
            actual = self._caret()
            if actual[:2] == (0, index):
                return actual
        raise TemplateWindowError("한글에서 구역의 첫 본문 위치를 확인할 수 없습니다.")

    def _move(self, value):
        if not self._hwp.SetPos(*value) or self._caret() != value:
            raise TemplateWindowError("선택한 위치가 현재 한글 문서의 경계를 벗어났습니다.")

    def place(self, position):
        self._require()
        position = tuple(position)
        if len(position) != 3 or position[0] != 0 or min(position) < 0:
            raise TemplateWindowError("본문의 위치를 선택하세요.")
        self._hwp.Run("Cancel")
        self._move(position)
        return position, position

    def _select(self, start, end):
        self._hwp.Run("Cancel")
        self._move(start)
        self._hwp.Run("Select")
        if not self._hwp.SetPos(*end):
            raise TemplateWindowError("한글에서 선택 영역 끝으로 이동할 수 없습니다.")
        selected = _positions(self._hwp.GetSelectedPos(), 6)
        if not selected or sorted((selected[:3], selected[3:])) != sorted((start, end)):
            raise TemplateWindowError("한글의 선택 범위와 지정한 위치가 일치하지 않습니다.")
        return start, end

    def select(self, start, end):
        self._require()
        start, end = tuple(start), tuple(end)
        if (len(start) != 3 or len(end) != 3 or start[0] != 0 or end[0] != 0
                or min(*start, *end) < 0 or start > end):
            raise TemplateWindowError("본문의 시작부터 끝까지 순서대로 선택하세요.")
        if start == end:
            return self.place(start)
        return self._select(start, end)

    def select_paragraphs(self, first, stop):
        records = self.scan()
        if not 0 <= first < stop <= len(records):
            raise TemplateWindowError("현재 문서의 문단 범위를 확인해 주세요.")
        return self.select(records[first].start, records[stop - 1].end)

    def capture(self, *, point=False):
        self._require()
        self._verify_bytes()
        selected = _positions(self._hwp.GetSelectedPos(), 6)
        if selected and selected[:3] != selected[3:]:
            if point:
                raise TemplateWindowError("한글에서 삽입할 곳을 한 번 클릭해 커서만 놓으세요.")
            start, end = sorted((selected[:3], selected[3:]))
        elif point:
            start = end = self._caret()
        else:
            raise TemplateWindowError("한글에서 등록할 범위를 먼저 드래그하세요.")
        if start[0] != 0 or end[0] != 0:
            raise TemplateWindowError("표나 글상자 안입니다. 개체 바로 앞이나 뒤의 본문에 커서를 놓아 주세요.")
        return start, end

    def close(self):
        self._same_thread()
        try:
            if self._hwp is not None and self._doc is not None:
                docs = self._docs()
                owned = next((doc for doc in docs if self._exact(doc)), None)
                if owned is not None:
                    if self.state() != "active":
                        owned.SetActive_XHwpDocument()
                        self._com.PumpWaitingMessages()
                    self._require()
                    if len(docs) == 1:
                        self._hwp.Quit()
                    else:
                        owned.Close(False)
                elif (len(docs) == 1 and int(docs[0].DocumentID) == self._id
                      and not docs[0].FullName and not docs[0].Modified):
                    self._hwp.Quit()
        except Exception:
            # An already closed user-facing window may have disconnected COM.
            # Never send an unverified Quit, Close or Clear to another document.
            pass
        finally:
            self._doc = self._hwp = self._id = self.hwnd = None
            self._records = ()
            if self._com is not None:
                self._com.CoUninitialize()
                self._com = None
