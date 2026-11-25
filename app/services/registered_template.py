"""Apply reviewed template coordinates to Hancom's own combined document.

Hancom first imports every question through the normal verified native transfer
path. Its resulting HWPX package has a single set of style, object and media
identities. Rearrange its existing body paragraphs inside that one package;
never rebuild a question or mix resource references from different documents.
"""
from __future__ import annotations

from collections import defaultdict
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from xml.dom import minidom
from xml.etree import ElementTree
from zipfile import ZipFile
from uuid import uuid4

from app.hwp.fields import CHILD_FIELD_PREFIX, FIELD_PREFIX, FIXED_FIELD_PREFIX, QuestionFieldManager, TERMINAL_FIELD, question_field
from app.hwp.transaction import sha256


@dataclass(frozen=True)
class Area:
    key: str
    role: str
    start: tuple
    end: tuple


class RegisteredTemplate:
    def __init__(self, profile, source, work_dir):
        data = json.loads(Path(profile).read_text(encoding="utf-8"))
        self.digest = sha256(source)
        if data.get("version") != 1 or data.get("source_sha256") != self.digest:
            raise ValueError("등록한 양식과 현재 파일의 내용이 다릅니다. 양식을 다시 등록하세요.")
        self.areas = []
        keys = set()
        for item in data["regions"]:
            key, role = item["key"], item["role"]
            if key in keys or role not in ("insert", "keep", "replace"):
                raise ValueError("양식의 등록 구분을 확인할 수 없습니다.")
            keys.add(key)
            if item["start"] is None and item["end"] is None and role != "insert":
                continue
            points = []
            for coord in (item["start"], item["end"]):
                if not isinstance(coord, list) or len(coord) != 3 or any(type(v) is not int or v < 0 for v in coord) or coord[0] != 0:
                    raise ValueError("양식에 올바르지 않은 본문 좌표가 있습니다.")
                points.append(tuple(coord))
            a, b = points
            if (role == "insert" and a != b) or (role != "insert" and a >= b):
                raise ValueError("양식에 올바르지 않은 본문 범위가 있습니다.")
            self.areas.append(Area(key, role, a, b))
        anchors = [a.start for a in self.areas if a.role == "insert"]
        spans = sorted((a.start, a.end) for a in self.areas if a.role != "insert")
        if len(anchors) != 1 or any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise ValueError("등록한 양식의 삽입 위치나 겹친 범위를 확인하세요.")
        self.insert = anchors[0]
        work_dir.mkdir(parents=True, exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(dir=work_dir, prefix="registered-", ignore_cleanup_errors=True)
        self.directory = Path(self._temp.name)
        self.slots = {}
        self.fixed = {}

    def prepare(self, session, template_xml=None):
        hwp = session.hwp
        fields = QuestionFieldManager(hwp)
        # A prior year's assembled exam is a valid template. Retire this
        # application's own field names on the private copy, leaving every
        # control, character position and the school's own named fields intact.
        for old in fields.field_names():
            if old.startswith((FIELD_PREFIX, CHILD_FIELD_PREFIX, FIXED_FIELD_PREFIX, "__HWPT__")):
                fresh = "__HWPOld__" + uuid4().hex
                hwp.automation.RenameField(old, fresh)
                template_xml = None
                if hwp.automation.FieldExist(old) or not hwp.automation.FieldExist(fresh):
                    raise ValueError("작업 사본의 이전 수합 식별자를 분리할 수 없습니다.")
        hwp.move_to_end()
        end = hwp.current_position()
        coords = sorted({p for a in self.areas for p in (a.start, a.end)})
        for p in coords:
            hwp.set_position(p)
            if p > end or hwp.current_position() != p:
                raise ValueError("등록한 본문 좌표와 실제 한글 양식이 일치하지 않습니다.")
        if template_xml is None:
            template_xml = hwp.automation.GetTextFile("HWPML2X", "")
        original = self._native_paragraphs(template_xml)
        if end[0] != 0 or len(original) != end[1] + 1:
            raise ValueError("양식의 한글 본문 문단 수를 확인할 수 없습니다.")
        replacements = sorted((a.start, a.end) for a in self.areas if a.role == "replace")
        boundaries = sorted({self.insert, *(p for a in replacements for p in a)})
        split = [p for p in boundaries if p[2] and p != end]
        for p in reversed(split):
            hwp.set_position(p)
            hwp.break_paragraph()
            if hwp.current_position()[:2] != (0, p[1] + 1):
                raise ValueError("한글에서 등록한 위치의 문단을 나눌 수 없습니다.")

        def index(p):
            if p == end:
                return len(original) + len(split)
            return p[1] + sum(s <= p for s in split)

        self.removed = [(index(a), index(b)) for a, b in replacements]
        self.primary = index(self.insert)
        self.slots = {"default": self.primary}
        # Additional approved replacement blocks may have a retained type heading.
        # A mixed stimulus group remains one unit at the primary insertion point.
        for a, _ in replacements:
            if a <= self.insert:
                continue
            label = next((r for r in self.areas if r.role == "keep" and r.end == a
                          and r.key.startswith("section-")), None)
            if label is None:
                continue
            rows = original[label.start[1]:label.end[1] + int(label.end[2] > 0)]
            text = "".join("".join(c.itertext()) for row in rows for c in row.iter("CHAR"))
            if re.search("서답형|서술형|단답형", text):
                self.slots.setdefault("written", index(a))
            elif re.search("선택형|객관식|지선다형", text):
                self.slots.setdefault("objective", index(a))
        hwp.move_to_end()
        self.body_count = hwp.current_position()[1] + 1
        if self.body_count != len(original) + len(split):
            raise ValueError("한글의 실제 문단 수가 등록한 범위와 일치하지 않습니다.")
        for number, slot in enumerate(sorted(set(self.slots.values())), 1):
            hwp.break_paragraph()
            self.fixed[slot] = hwp.current_position()[1]
            fields.create_at_current_position(f"{FIXED_FIELD_PREFIX}{number}")
            hwp.move_to_end()
        hwp.break_paragraph()
        self.sentinel = hwp.current_position()[1]

    def assemble(self, session, manager):
        hwp = session.hwp
        hwp.move_to_end()
        hwp.break_paragraph()
        manager.create_at_current_position(TERMINAL_FIELD)
        terminal = manager.anchor_position(TERMINAL_FIELD)[1]
        starts = [manager.anchor_position(question_field(q.id))[1] for q in session.registered]
        if starts != sorted(set(starts)) or not self.sentinel < starts[0] <= starts[-1] < terminal:
            raise RuntimeError("수합 문항의 한글 경계를 확인할 수 없습니다.")
        buckets = defaultdict(list)
        blocks = defaultdict(list)
        for q, first, stop in zip(session.registered, starts, [*starts[1:], terminal], strict=True):
            kind = "written" if q.written_count and not q.objective_count else "objective" if q.objective_count and not q.written_count else "default"
            slot = self.slots.get(kind, self.primary)
            blocks[slot].extend(range(first, stop))
            buckets[slot].append(q)
        hwp.move_to_end()
        total = hwp.current_position()[1] + 1
        native_paragraphs = self._native_paragraphs(hwp.automation.GetTextFile("HWPML2X", ""))
        if len(native_paragraphs) != total:
            raise RuntimeError("수합본의 본문 문단과 한글 위치를 대조할 수 없습니다.")
        raw = self.directory / "with-questions.hwpx"
        composed = self.directory / "composed.hwpx"
        hwp.save_exam_copy(raw)
        hwp.reset_documents()
        last_slot = max(blocks)
        layout = []
        for index in range(self.body_count + 1):
            members = blocks.get(index)
            if members:
                if index == min(blocks):
                    layout.append((index, self.sentinel))
                layout.extend((index, n) for n in members)
                layout.append((index, terminal if index == last_slot else self.fixed[index]))
            if index < self.body_count and not any(first <= index < stop for first, stop in self.removed):
                layout.append((index, index))
        self.protected = {i: self._signature(native_paragraphs[n])
                          for i, (_, n) in enumerate(layout) if n < self.body_count}
        self._write_body(raw, composed, total, layout)
        hwp.open(composed)
        session.target_document = hwp.active_document()
        session.registered[:] = [q for slot in sorted(buckets) for q in buckets[slot]]
        session._require_target_bridge(operation="등록 양식 반영", require_terminal=True)
        result = QuestionFieldManager(hwp)
        session._validate_target_identity_graph(result, phase="등록 양식 반영")
        session._target_control_totals = session._transfer_control_summary()
        session.insertion_position = self.insert
        return result

    @staticmethod
    def _native_paragraphs(xml):
        return ElementTree.fromstring(xml).findall("./BODY/SECTION/P")

    @staticmethod
    def _signature(paragraph):
        content = "".join("".join(node.itertext()) for node in paragraph.iter()
                          if node.tag in ("CHAR", "SCRIPT"))
        objects = Counter(node.tag for node in paragraph.iter() if node.tag in (
            "HEADER", "FOOTER", "TABLE", "PICTURE", "EQUATION", "RECTANGLE", "GROUP",
            "ELLIPSE", "POLYGON", "CURVE", "LINE", "OLE", "FOOTNOTE", "ENDNOTE",
        ))
        return " ".join(content.split()), objects

    def verify_preserved(self, hwp, *, xml=None):
        if xml is None:
            xml = hwp.automation.GetTextFile("HWPML2X", "")
        paragraphs = self._native_paragraphs(xml)
        for index, expected in self.protected.items():
            if index >= len(paragraphs) or self._signature(paragraphs[index]) != expected:
                raise RuntimeError(f"저장된 수합본의 고정 영역이 원래 양식과 다릅니다 ({index + 1}번째 문단).")

    def _write_body(self, source, destination, total, layout):
        with ZipFile(source) as incoming, ZipFile(destination, "w") as output:
            sections = sorted((s for s in incoming.namelist() if re.fullmatch(r"Contents/section\d+\.xml", s)),
                              key=lambda s: int(re.search(r"(\d+)\.xml", s)[1]))
            trees, rows, owners = {}, [], []
            for path in sections:
                tree = trees[path] = minidom.parseString(incoming.read(path))
                for node in list(tree.documentElement.childNodes):
                    if node.nodeType == node.ELEMENT_NODE and node.localName == "p":
                        rows.append(tree.documentElement.removeChild(node))
                        owners.append(path)
            if len(rows) != total:
                raise ValueError("한글 저장본과 화면의 본문 문단 경계가 일치하지 않습니다.")
            for location, n in layout:
                tree = trees[owners[min(location, self.body_count - 1)]]
                tree.documentElement.appendChild(rows[n])
            for tree in trees.values():
                if not tree.documentElement.getElementsByTagNameNS("*", "secPr"):
                    raise ValueError("등록 범위에서 한글 구역 설정을 보존할 수 없습니다.")
            for entry in incoming.infolist():
                tree = trees.get(entry.filename)
                value = tree.toxml(encoding="utf-8") if tree is not None else incoming.read(entry)
                output.writestr(entry, value)
            for tree in trees.values():
                tree.unlink()

    def publish(self, verified, output):
        if output.exists():
            raise FileExistsError(f"수합 중 같은 이름의 파일이 생겼습니다: {output}")
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".registered-", delete=False) as dst:
            temp = Path(dst.name)
        try:
            shutil.copyfile(verified, temp)
            if sha256(temp) != sha256(verified):
                raise RuntimeError("검증한 수합본을 복사하는 중 파일이 변경되었습니다.")
            # Same-volume, atomic creation. On Windows rename fails if the
            # destination exists; POSIX link provides that same guarantee.
            if os.name == "nt":
                os.rename(temp, output)
            else:
                os.link(temp, output)
        finally:
            temp.unlink(missing_ok=True)
