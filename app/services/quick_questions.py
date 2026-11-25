"""Read the assembler's paragraph-aligned question fields without starting Hangul.

Use native inspection for ambiguous boundaries, numbering or rich content. All
edits and saves still use Hangul's original document and verification path.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile
import re
import struct
import zlib

import pythoncom

from app.hwp.fields import (CHILD_FIELD_PREFIX, FIELD_PREFIX, FIXED_FIELD_PREFIX,
                            TERMINAL_FIELD, parse_child_question_field, parse_question_field)
from .exam_service import ExamDocumentService, QuestionSummary

MAX_PART = 32 * 1024 * 1024
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HH = "{http://www.hancom.co.kr/hwpml/2011/head}"
HS = "{http://www.hancom.co.kr/hwpml/2011/section}"
OPF = "{http://www.idpf.org/2007/opf/}"
OCF = "{urn:oasis:names:tc:opendocument:xmlns:container}"
FIELD_PREFIXES = (FIELD_PREFIX, CHILD_FIELD_PREFIX, FIXED_FIELD_PREFIX)


class NativeInspectionNeeded(ValueError):
    pass


@dataclass
class Paragraph:
    # None denotes an unsupported heading. Numbered paragraphs carry their
    # numbering definition, start and first-level decimal format.
    heading: tuple[int, int, str] | None
    tokens: list[tuple[str, str]] = field(default_factory=list)


PLAIN = (0, 0, "")


def _xml(archive, name):
    if archive.getinfo(name).file_size > MAX_PART:
        raise NativeInspectionNeeded()
    data = archive.read(name)
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data or b"\x00" in data:
        raise NativeInspectionNeeded()
    return ET.fromstring(data)


def _text_tokens(part):
    yield "text", part.text or ""
    for mark in part:
        if mark.tag not in {HP + "lineBreak", HP + "tab", HP + "fwSpace", HP + "nbSpace"}:
            yield "native", ""
        yield "text", " " + (mark.tail or "")


def _inline_tokens(part):
    if part.tag == HP + "t":
        yield from _text_tokens(part)
    elif part.tag == HP + "equation" and part.find(HP + "caption") is None:
        yield "object", "eqed:수식"
    elif part.tag == HP + "pic" and part.find(HP + "caption") is None:
        position = part.find(HP + "pos")
        if position is not None and position.get("treatAsChar") == "1":
            yield "object", "gso:그림"
        else:
            yield "native", ""
    else:
        yield "native", ""


def _cell_tokens(cell, shapes):
    paragraphs = cell.findall(f"{HP}subList/{HP}p")
    if not paragraphs:
        yield "native", ""
        return
    for paragraph in paragraphs:
        if shapes.get(paragraph.get("paraPrIDRef")) != PLAIN:
            yield "native", ""
            return
        yield "text", " "
        for run in paragraph:
            if run.tag == HP + "linesegarray":
                continue
            if run.tag != HP + "run":
                yield "native", ""
                return
            for part in run:
                yield from _inline_tokens(part)


def _table_tokens(table, shapes):
    # Native plain text separates the paragraphs of successive cells. Cell
    # equations and pictures still belong to the outer question's inventory.
    yield "object", "tbl:표"
    position = table.find(HP + "pos")
    if position is None or position.get("treatAsChar") != "1" or table.find(HP + "caption") is not None:
        yield "native", ""
        return
    rows = table.findall(HP + "tr")
    if not rows or str(len(rows)) != table.get("rowCnt"):
        yield "native", ""
        return
    for y, row in enumerate(rows):
        cells = row.findall(HP + "tc")
        if not cells or str(len(cells)) != table.get("colCnt"):
            yield "native", ""
            return
        for x, cell in enumerate(cells):
            addr, span = cell.find(HP + "cellAddr"), cell.find(HP + "cellSpan")
            if (addr is None or (addr.get("colAddr"), addr.get("rowAddr")) != (str(x), str(y))
                    or span is None or (span.get("colSpan"), span.get("rowSpan")) != ("1", "1")):
                yield "native", ""
                return
            yield from _cell_tokens(cell, shapes)
    yield "text", " "


def _hwpx_paragraphs(path):
    with ZipFile(path) as archive:
        names = archive.namelist()
        sections = [n for n in names if re.fullmatch(r"Contents/section\d+\.xml", n)]
        if (sections != ["Contents/section0.xml"] or len(set(names)) != len(names)
                or archive.getinfo("mimetype").file_size > 64
                or archive.read("mimetype") != b"application/hwp+zip"):
            raise NativeInspectionNeeded()
        container = _xml(archive, "META-INF/container.xml")
        packages = [x.get("full-path") for x in container.findall(f"./{OCF}rootfiles/{OCF}rootfile")
                    if x.get("media-type") == "application/hwpml-package+xml"]
        if packages != ["Contents/content.hpf"]:
            raise NativeInspectionNeeded()
        package = _xml(archive, packages[0])
        items = package.findall(f"./{OPF}manifest/{OPF}item")
        parts = {x.attrib["id"]: x.attrib["href"] for x in items}
        spine = package.findall(f"./{OPF}spine/{OPF}itemref")
        if (len(parts) != len(items) or [parts[x.attrib["idref"]] for x in spine]
                != ["Contents/header.xml", "Contents/section0.xml"]):
            raise NativeInspectionNeeded()
        header = _xml(archive, "Contents/header.xml")
        section = _xml(archive, sections[0])
    if header.tag != HH + "head" or section.tag != HS + "sec":
        raise NativeInspectionNeeded()

    numbers = {}
    for n in header.findall(f"./{HH}refList/{HH}numberings/{HH}numbering"):
        key = int(n.attrib["id"])
        if key in numbers:
            raise NativeInspectionNeeded()
        first = n.find(HH + "paraHead")
        definition = None
        if first is not None and first.get("level") == "1" and first.get("numFormat") == "DIGIT":
            start = int(n.attrib["start"])
            if start > 0 and int(first.get("start", "0")) == start and (first.text or "").count("^1") == 1:
                definition = (key, start, first.text)
        numbers[key] = definition

    shapes, number_refs = {}, {}
    for p in header.findall(f"./{HH}refList/{HH}paraProperties/{HH}paraPr"):
        key = p.attrib["id"]
        if key in shapes:
            raise NativeInspectionNeeded()
        heading = p.find(HH + "heading")
        number_refs[key] = {int(h.attrib["idRef"]) for h in p.iter(HH + "heading")
                            if h.get("type") == "NUMBER"}
        value = None
        if heading is not None:
            if heading.get("type") == "NONE":
                value = PLAIN
            elif heading.get("type") == "NUMBER" and heading.get("level") == "0":
                value = numbers.get(int(heading.attrib["idRef"]))
        shapes[key] = value

    body_paragraphs = set(section)
    nested_numbers = set()
    for p in section.iter(HP + "p"):
        if p not in body_paragraphs:
            nested_numbers.update(number_refs[p.attrib["paraPrIDRef"]])
    known_fields, field_ids = set(), set()
    for p in section:
        if p.tag != HP + "p":
            raise NativeInspectionNeeded()
        row = Paragraph(shapes[p.attrib["paraPrIDRef"]])
        if row.heading and row.heading[0] in nested_numbers:
            raise NativeInspectionNeeded()
        awaiting_end = None
        for run in p:
            if run.tag == HP + "linesegarray":
                continue
            if run.tag != HP + "run":
                row.tokens.append(("native", ""))
                continue
            for part in run:
                if awaiting_end is not None and part.tag != HP + "ctrl":
                    raise NativeInspectionNeeded()
                if part.tag == HP + "tbl":
                    row.tokens.extend(_table_tokens(part, shapes))
                elif part.tag == HP + "ctrl":
                    for ctrl in part:
                        if awaiting_end is not None and ctrl.tag != HP + "fieldEnd":
                            raise NativeInspectionNeeded()
                        if ctrl.tag == HP + "fieldBegin" and ctrl.get("type") == "CLICK_HERE":
                            ref = ctrl.attrib["id"]
                            if awaiting_end is not None or ref in field_ids:
                                raise NativeInspectionNeeded()
                            field_ids.add(ref)
                            awaiting_end = ref
                            known_fields.add(ctrl)
                            row.tokens.append(("field", ctrl.get("name", "")))
                        elif ctrl.tag == HP + "fieldEnd":
                            if awaiting_end is not None and ctrl.get("beginIDRef") != awaiting_end:
                                raise NativeInspectionNeeded()
                            awaiting_end = None
                        else:
                            row.tokens.append(("native", ""))
                else:
                    row.tokens.extend(_inline_tokens(part))
        if awaiting_end is not None:
            raise NativeInspectionNeeded()
        yield row

    # A marker inside a table cell/header is not a main-body boundary.
    if any(f not in known_fields and f.get("name", "").startswith(FIELD_PREFIXES)
           for f in section.iter(HP + "fieldBegin")):
        raise NativeInspectionNeeded()


def _summaries(rows, metadata):
    summaries = []
    ids, field_names, numbering_used = set(), set(), set()
    current = None
    terminal = False
    pieces, children, controls = [], [], Counter()
    numbered = False

    def finish():
        if current is None:
            return
        text = "".join(pieces)
        meta = metadata.get(current, {})
        manual = bool(meta.get("manual_required")) and not text.strip() and not controls
        preview = ExamDocumentService._preview(text)
        if manual:
            preview = "직접 붙여넣을 원본: " + str(meta.get("preview") or meta.get("source_label") or "원본 문항 범위")
        summaries.append(QuestionSummary(
            current, preview, numbered, dict(controls), "group" if children else "question",
            tuple(c.question_id for c in children),
            sum(c.kind == "objective" for c in children), sum(c.kind == "written" for c in children),
            manual, str(meta.get("import_error", "")) if manual else "",
        ))

    for row in rows:
        leading = True
        preceding_object = False
        numbered_rendered = False
        prefix = ""
        if row.heading and row.heading != PLAIN:
            ref, start, pattern = row.heading
            # Shared lists may continue other paragraphs in a table or header.
            # Only accept an explicit single-use numbering definition.
            if ref in numbering_used:
                raise NativeInspectionNeeded()
            numbering_used.add(ref)
            if not re.fullmatch(r"[^\^]*\^1[^\^]*", pattern):
                raise NativeInspectionNeeded()
            prefix = pattern.replace("^1", str(start)) + " "
        for kind, value in row.tokens:
            if kind == "field" and value.startswith(FIELD_PREFIXES):
                if value in field_names:
                    raise NativeInspectionNeeded()
                field_names.add(value)
                if value.startswith(CHILD_FIELD_PREFIX):
                    child = parse_child_question_field(value)
                    if not child or child.group_id != current or child.question_id in ids:
                        raise NativeInspectionNeeded()
                    children.append(child)
                    ids.add(child.question_id)
                    continue
                if preceding_object:
                    raise NativeInspectionNeeded()
                finish()
                current = None
                pieces, children, controls = [], [], Counter()
                if terminal:
                    raise NativeInspectionNeeded()
                if value == TERMINAL_FIELD:
                    terminal = True
                elif value.startswith(FIELD_PREFIX):
                    current = parse_question_field(value)
                    if not current or not leading or current in ids:
                        raise NativeInspectionNeeded()
                    ids.add(current)
                    numbered = bool(prefix)
                    if row.heading is None:
                        raise NativeInspectionNeeded()
                continue
            if kind == "field":
                continue
            if kind in ("object", "native"):
                preceding_object = True
            if current is not None:
                if kind == "native" or row.heading is None:
                    raise NativeInspectionNeeded()
                if prefix and not numbered_rendered:
                    pieces.append(prefix)
                    numbered_rendered = True
                if kind == "text":
                    pieces.append(value)
                elif kind == "object":
                    controls[value] += 1
            leading = leading and kind == "text" and not value
        if current is not None:
            pieces.append(" ")
    if not terminal or not summaries:
        raise NativeInspectionNeeded()
    return tuple(summaries)


def read_questions(path: Path, import_metadata: dict) -> tuple[QuestionSummary, ...] | None:
    """Return a complete editable list, or defer to the native reader."""
    try:
        if path.suffix.lower() == ".hwpx":
            return _summaries(_hwpx_paragraphs(path), import_metadata)
        if path.suffix.lower() == ".hwp":
            from .quick_hwp import hwp_paragraphs
            return _summaries(hwp_paragraphs(path), import_metadata)
    except (OSError, ValueError, KeyError, IndexError, struct.error, EOFError, ET.ParseError,
            BadZipFile, RuntimeError, zlib.error, pythoncom.com_error):
        return None
    return None
