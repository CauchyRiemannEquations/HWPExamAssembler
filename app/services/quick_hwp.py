"""Read the HWP 5 DocInfo and BodyText streams using Windows structured storage."""
from __future__ import annotations

from pathlib import Path
import shutil
import struct
from tempfile import TemporaryDirectory
import zlib
import pythoncom
from win32com.storagecon import STGM_READ, STGM_SHARE_DENY_WRITE, STGM_SHARE_EXCLUSIVE

from .quick_questions import FIELD_PREFIXES, MAX_PART, PLAIN, NativeInspectionNeeded, Paragraph

READ = STGM_READ | STGM_SHARE_DENY_WRITE


def _stream(storage, name, compressed=False):
    stream = storage.OpenStream(name, None, STGM_READ | STGM_SHARE_EXCLUSIVE)
    length = stream.Stat(1)[2]
    if length > MAX_PART:
        raise NativeInspectionNeeded()
    data = stream.Read(length)
    if len(data) != length:
        raise NativeInspectionNeeded()
    if compressed:
        decoder = zlib.decompressobj(-15)
        data = decoder.decompress(data, MAX_PART + 1)
        trailer = decoder.unused_data
        if (len(data) > MAX_PART or not decoder.eof
                or (trailer and trailer != struct.pack("<II", zlib.crc32(data), len(data)))):
            raise NativeInspectionNeeded()
    return data


def _records(data):
    pos = 0
    count = 0
    while pos < len(data):
        count += 1
        if count > 250_000:
            raise NativeInspectionNeeded()
        header, = struct.unpack_from("<I", data, pos)
        pos += 4
        length = header >> 20
        if length == 4095:
            length, = struct.unpack_from("<I", data, pos)
            pos += 4
        if length > len(data) - pos:
            raise NativeInspectionNeeded()
        yield header & 1023, (header >> 10) & 1023, data[pos:pos + length]
        pos += length


def _numbering(data, key):
    # Seven level descriptions precede the list and per-level start values.
    pos = 0
    for level in range(7):
        size = struct.unpack_from("<H", data, pos + 12)[0] * 2
        if level == 0:
            flags, = struct.unpack_from("<I", data, pos)
            fmt = data[pos + 14:pos + 14 + size].decode("utf-16-le")
        pos += 14 + size
    start, first_start = struct.unpack_from("<HI", data, pos)
    if start and first_start == start and flags & 0xff00 == 0 and fmt.count("^1") == 1:
        return key, start, fmt
    return None


def _styles(data):
    numbers, shapes, number_refs = [None], [], []
    for tag, _, payload in _records(data):
        if tag == 23:  # HWPTAG_NUMBERING
            numbers.append(_numbering(payload, len(numbers)))
        elif tag == 25:  # HWPTAG_PARA_SHAPE
            attr, = struct.unpack_from("<I", payload)
            kind, level = (attr >> 23) & 3, (attr >> 25) & 7
            number_id, = struct.unpack_from("<H", payload, 30)
            number_refs.append(number_id if kind == 2 else 0)
            value = None
            if kind == 0:
                value = PLAIN
            elif kind == 2 and level == 0:
                value = numbers[number_id]
            shapes.append(value)
    return shapes, number_refs


def _field_name(data):
    # HWPTAG_CTRL_DATA stores an HWP parameter set. Support the standard
    # click-here field name item (PIT_BSTR); don't search arbitrary text bytes.
    if data[:10] != bytes.fromhex("1b020100000000400100"):
        raise NativeInspectionNeeded()
    chars, = struct.unpack_from("<H", data, 10)
    if len(data) != 12 + 2 * chars:
        raise NativeInspectionNeeded()
    return data[12:].decode("utf-16-le")


def _tokens(text, controls):
    # PARA_TEXT carries the anchors in reading order. CTRL_HEADER and its
    # CTRL_DATA carry the name, referenced only after validating the matching ID.
    index, pos = 0, 0
    click_end = False
    while pos < len(text):
        char, = struct.unpack_from("<H", text, pos)
        if click_end and char != 4:
            raise NativeInspectionNeeded()
        if char >= 32:
            first = pos
            pos += 2
            while pos < len(text) and struct.unpack_from("<H", text, pos)[0] >= 32:
                pos += 2
            yield "text", text[first:pos].decode("utf-16-le")
            continue
        if char in (0, 10, 13, 24, 30, 31):
            if char in (10, 30, 31):
                yield "text", " "
            elif char == 24:
                yield "native", ""
            pos += 2
            continue
        if pos + 16 > len(text) or struct.unpack_from("<H", text, pos + 14)[0] != char:
            raise NativeInspectionNeeded()
        if char == 9:
            yield "text", " "
        elif char == 4:
            if not click_end:
                yield "native", ""
            click_end = False
        else:
            chid, content = controls[index]
            index += 1
            if text[pos + 2:pos + 6][::-1] != chid:
                raise NativeInspectionNeeded()
            click_end = chid == b"%clk"
            yield from content
        pos += 16
    if click_end or index != len(controls):
        raise NativeInspectionNeeded()


def _groups(records, level):
    """Group each record with its descendants by the native record level."""
    head, children = None, []
    for entry in records:
        if entry[1] == level:
            if head is not None:
                yield head, children
            head, children = entry, []
        elif head is None or entry[1] < level:
            raise NativeInspectionNeeded()
        else:
            children.append(entry)
    if head is not None:
        yield head, children


def _table(children, shapes):
    # Body tables have a table record followed by the cell list headers and
    # plain paragraphs. Captions, merged layouts and nested lists use native
    # text export because it can include their labels and numbering.
    content = [("object", "tbl:표")]
    seen_table, cell_index, columns, rows = False, 0, 0, 0
    for (tag, _, data), descendants in _groups(children, 2):
        if tag == 77 and not seen_table:  # HWPTAG_TABLE
            rows, columns = struct.unpack_from("<HH", data, 4)
            seen_table = True
        elif tag == 72 and seen_table and columns:  # HWPTAG_LIST_HEADER
            col, row, width, height = struct.unpack_from("<HHHH", data, 8)
            if (row, col) != divmod(cell_index, columns) or (width, height) != (1, 1):
                return [("native", "")]
            cell_index += 1
        elif tag == 66 and cell_index:
            shape, = struct.unpack_from("<H", data, 8)
            if shapes[shape] != PLAIN:
                return [("native", "")]
            paragraph = _body_paragraph(data, descendants, 2, shapes)
            if any(kind not in ("text", "object") for kind, _ in paragraph.tokens):
                return [("native", "")]
            content.append(("text", " "))
            content.extend(paragraph.tokens)
            content.append(("text", " "))
        else:
            return [("native", "")]
    return content if seen_table and cell_index == rows * columns else [("native", "")]


def _control(payload, children, level, shapes):
    chid = payload[:4][::-1]
    if chid == b"%clk":
        fields = [_field_name(data) for tag, depth, data in children if tag == 87 and depth == level + 1]
        if len(fields) != 1:
            raise NativeInspectionNeeded()
        return chid, [("field", fields[0])]
    if chid == b"eqed":
        if len(children) == 1 and children[0][:2] == (88, level + 1):
            return chid, [("object", "eqed:수식")]
        return chid, [("native", "")]
    if chid == b"gso ":
        tags = [tag for tag, _, _ in children]
        if (tags == [76, 85] and children[0][2].startswith(b"cip$cip$")
                and (int.from_bytes(payload[4:8], "little") & 1)):
            return chid, [("object", "gso:그림")]
    if chid == b"tbl " and level == 1 and int.from_bytes(payload[4:8], "little") & 1:
        return chid, _table(children, shapes)
    return chid, [("native", "")]


def _body_paragraph(data, children, level, shapes):
    shape, = struct.unpack_from("<H", data, 8)
    row = Paragraph(shapes[shape])
    text, controls = None, []
    for (tag, depth, payload), descendants in _groups(children, level + 1):
        if tag == 67:
            if text is not None:
                raise NativeInspectionNeeded()
            text = payload
        elif tag == 71:
            controls.append(_control(payload, descendants, depth, shapes))
        elif tag == 70 and payload:
            row.tokens.append(("native", ""))
    row.tokens.extend(_tokens(text or b"", controls))
    return row


def _paragraphs(data, shapes, number_refs):
    result, nested_numbers = [], set()
    for (tag, _, payload), children in _groups(_records(data), 0):
        if tag != 66:
            raise NativeInspectionNeeded()
        for child_tag, depth, record in children:
            if child_tag == 66:
                shape, = struct.unpack_from("<H", record, 8)
                if number_refs[shape]:
                    nested_numbers.add(number_refs[shape])
            if child_tag == 71 and depth > 1 and record[:4] == b"klc%":
                # A nested click-here field may alias a body marker.
                raise NativeInspectionNeeded()
        result.append(_body_paragraph(payload, children, 0, shapes))
    if any(row.heading and row.heading[0] in nested_numbers for row in result):
        raise NativeInspectionNeeded()
    return result


def _read(path):
    storage = pythoncom.StgOpenStorage(str(path), None, READ)
    header = _stream(storage, "FileHeader")
    if (len(header) != 256 or header[:32].rstrip(b"\0") != b"HWP Document File"
            or header[35] != 5):
        raise NativeInspectionNeeded()
    flags, = struct.unpack_from("<I", header, 36)
    if flags & ~9:  # only ordinary unprotected, optionally compressed/scripted HWP
        raise NativeInspectionNeeded()
    compressed = bool(flags & 1)
    shapes, number_refs = _styles(_stream(storage, "DocInfo", compressed))
    body = storage.OpenStorage("BodyText", None, STGM_READ | STGM_SHARE_EXCLUSIVE)
    if [(entry[0], entry[1]) for entry in body.EnumElements().Next(2)] != [("Section0", 2)]:
        raise NativeInspectionNeeded()
    return _paragraphs(_stream(body, "Section0", compressed), shapes, number_refs)


def hwp_paragraphs(path):
    # The GUI worker is a separate thread. COM is used for compound file
    # streams only; reading the document does not create a Hangul process.
    initialized = False
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
    except pythoncom.com_error as error:
        if error.hresult != -2147417850:  # RPC_E_CHANGED_MODE (already initialized)
            raise
    try:
        try:
            return _read(path)
        except pythoncom.com_error as error:
            if error.hresult not in (-2147287008, -2147287007):  # STG_E_SHARE/LOCKVIOLATION
                raise
        # Hangul may allow ordinary readers while refusing a second structured-
        # storage lock. Parse a private copy and keep the user's tab untouched.
        with TemporaryDirectory(prefix="hwp-question-list-") as directory:
            copy = Path(directory) / "snapshot.hwp"
            shutil.copyfile(path, copy)
            return _read(copy)
    finally:
        if initialized:
            pythoncom.CoUninitialize()
