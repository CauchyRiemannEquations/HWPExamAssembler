"""Permute verified whole body paragraphs within a single private HWPX package."""
from __future__ import annotations

from copy import copy
import os
from pathlib import Path
import re
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from xml.etree import ElementTree as ET
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

from .fields import CHILD_FIELD_PREFIX, FIELD_PREFIX, FIXED_FIELD_PREFIX


SECTION = "Contents/section0.xml"
HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
HS = "{http://www.hancom.co.kr/hwpml/2011/section}"
OPF = "{http://www.idpf.org/2007/opf/}"
MAX_XML = 32 * 1024 * 1024


class _Unsupported(ValueError):
    pass


def _paragraph_offsets(data):
    """Return byte offsets; serializing the XML tree would rewrite other markup."""
    parser = expat.ParserCreate(namespace_separator="}")
    depth = 0
    offsets = []

    def start(tag, attrs):
        nonlocal depth
        if depth == 0 and "{" + tag != HS + "sec":
            raise _Unsupported
        if depth == 1:
            if "{" + tag != HP + "p":
                raise _Unsupported
            offsets.append(parser.CurrentByteIndex)
        depth += 1

    def end(tag):
        nonlocal depth
        depth -= 1
        if depth == 0:
            offsets.append(parser.CurrentByteIndex)

    def content(text):
        if depth == 1 and text.strip():
            raise _Unsupported

    def other_markup(*args):
        if depth == 1:
            raise _Unsupported

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = content
    parser.CommentHandler = other_markup
    parser.ProcessingInstructionHandler = other_markup
    parser.StartCdataSectionHandler = other_markup
    parser.Parse(data, True)
    return offsets


def _verify_paragraph(paragraph, moved, field_ids, field_names):
    # Only empty click-here markers in the containing body paragraph have
    # known range semantics. Nested tables can still contain text and objects.
    active = None
    controls = set(paragraph.findall(f"{HP}run/{HP}ctrl/*"))
    first = next((node for run in paragraph.findall(HP + "run") for node in run
                  if node.tag != HP + "t" or len(node) or node.text), None)
    for node in paragraph.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if node.tag == HP + "fieldBegin":
            name = node.get("name", "")
            ident = node.get("id")
            if (node not in controls or node.get("type") != "CLICK_HERE" or active is not None
                    or not ident or ident in field_ids or not name or name in field_names):
                raise _Unsupported
            if (name.startswith((FIELD_PREFIX, FIXED_FIELD_PREFIX, "__HWPT__"))
                    and (first is None or len(first) != 1 or first[0] is not node)):
                raise _Unsupported
            if moved and not name.startswith((FIELD_PREFIX, CHILD_FIELD_PREFIX)):
                raise _Unsupported
            field_ids.add(ident)
            field_names.add(name)
            active = ident
        elif node.tag == HP + "fieldEnd":
            if node not in controls or active is None or node.get("beginIDRef") != active:
                raise _Unsupported
            active = None
        elif (tag in {"secPr", "colPr"} and moved or tag in {"bookmark", "rangeTag", "switch"}
              or tag.lower().startswith("trackchange") or tag.endswith(("Begin", "End", "Start"))):
            raise _Unsupported
    if active is not None:
        raise _Unsupported


def _reordered_section(data, indices):
    if b"\x00" in data or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise _Unsupported
    offsets = _paragraph_offsets(data)
    count = len(offsets) - 1
    if (not count or len(indices) != count or any(type(i) is not int for i in indices)
            or set(indices) != set(range(count))):
        raise _Unsupported
    rows = ET.fromstring(data)
    field_ids, field_names = set(), set()
    for location, original in enumerate(indices):
        _verify_paragraph(rows[original], original != location, field_ids, field_names)
    # Keep whitespace between paragraphs at its original location. Each
    # paragraph, including its namespace declarations, remains byte-identical.
    parts = [data[a:b] for a, b in zip(offsets, offsets[1:])]
    bodies = [part.rstrip(b" \r\n\t") for part in parts]
    result = [data[:offsets[0]]]
    for location, original in enumerate(indices):
        result.extend((bodies[original], parts[location][len(bodies[location]):]))
    result.append(data[offsets[-1]:])
    return b"".join(result)


def _single_section_spine(metadata):
    if b"\x00" in metadata or b"<!DOCTYPE" in metadata or b"<!ENTITY" in metadata:
        return False
    root = ET.fromstring(metadata)
    items = root.findall(f"{OPF}manifest/{OPF}item")
    hrefs = {item.get("id"): item.get("href") for item in items}
    spine = root.findall(f"{OPF}spine/{OPF}itemref")
    return (root.tag == OPF + "package" and len(items) == len(hrefs)
            and [hrefs.get(item.get("idref")) for item in spine] == ["Contents/header.xml", SECTION])


def reorder_hwpx_paragraphs(source: Path, output: Path, paragraph_indices: tuple[int, ...]) -> bool:
    """Write a private reordered copy or return False for an unsupported package.

    The caller supplies all paragraph indices from verified native block ranges.
    Publication and native numbering/content verification belong to the caller.
    """
    if source.resolve() == output.resolve():
        return False
    temporary = None
    try:
        with ZipFile(source) as package:
            entries = package.infolist()
            names = [entry.filename.casefold() for entry in entries]
            if (len(names) != len(set(names)) or any(entry.flag_bits & 1 for entry in entries)
                    or [name for name in names if re.fullmatch(r"contents/section\d+\.xml", name)] != [SECTION.lower()]
                    or any("signature" in name or "encrypt" in name or name.endswith((".p7s", ".sig")) for name in names)
                    or any(name.startswith("meta-inf/") and name not in {
                        "meta-inf/", "meta-inf/container.rdf", "meta-inf/container.xml", "meta-inf/manifest.xml",
                    } for name in names)
                    or package.getinfo("mimetype").file_size > 100
                    or package.read("mimetype") != b"application/hwp+zip"):
                return False
            for member in ("Contents/content.hpf", "META-INF/manifest.xml"):
                if package.getinfo(member).file_size > MAX_XML:
                    return False
                metadata = package.read(member)
                lower = metadata.lower()
                if any(marker in lower for marker in (b"xmldsig", b"signature", b"encryption", b"xmlenc")):
                    return False
                if member == "Contents/content.hpf" and not _single_section_spine(metadata):
                    return False
            if package.getinfo(SECTION).file_size > MAX_XML:
                return False
            section = _reordered_section(package.read(SECTION), paragraph_indices)
            with NamedTemporaryFile(dir=output.parent, delete=False) as staged:
                temporary = Path(staged.name)
                with ZipFile(staged, "w") as result:
                    result.comment = package.comment
                    for entry in entries:
                        if entry.filename == SECTION:
                            result.writestr(copy(entry), section)
                        else:
                            with package.open(entry) as src, result.open(copy(entry), "w") as dst:
                                copyfileobj(src, dst)
        os.replace(temporary, output)
        return True
    except (_Unsupported, BadZipFile, KeyError, expat.ExpatError, ET.ParseError, NotImplementedError):
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
