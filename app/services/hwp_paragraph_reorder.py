"""Reorder complete HWP 5 body paragraphs in a disposable compound-file copy."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import struct
from tempfile import TemporaryDirectory
import zlib

import pythoncom
from win32com.storagecon import STGM_READWRITE, STGM_SHARE_EXCLUSIVE

from .quick_hwp import _records, _stream
from .quick_questions import MAX_PART, NativeInspectionNeeded


_RW = STGM_READWRITE | STGM_SHARE_EXCLUSIVE
_ROOT_NAMES = {
    "FileHeader", "DocInfo", "BodyText", "BinData", "PrvImage", "PrvText", "Scripts",
    "DocOptions", "\x05HwpSummaryInformation",
}
# Only the record types covered by native comparison of simple exam paragraphs,
# tables, inline pictures, equations, section definitions and their descendants.
_BODY_TAGS = {66, 67, 68, 69, 71, 72, 73, 74, 75, 76, 77, 85, 87, 88}
_FIXED_CONTROLS = {b"secd", b"cold", b"head", b"foot", b"atno", b"pgnp"}
_MOVABLE_CONTROLS = {b"%clk", b"tbl ", b"eqed", b"gso "}


def _reorder_records(data, indices):
    starts = []
    fixed = set()
    sections = []
    offset = 0
    previous_level = -1
    for tag, level, payload in _records(data):
        if tag not in _BODY_TAGS or level > previous_level + 1:
            raise NativeInspectionNeeded()
        if level == 0:
            if tag != 66 or len(payload) < 22:
                raise NativeInspectionNeeded()
            starts.append(offset)
            # The last-paragraph flag belongs to its original native list.
            if struct.unpack_from("<I", payload)[0] & 0x80000000:
                fixed.add(len(starts) - 1)
        elif level == 1 and tag not in {67, 68, 69, 71}:
            raise NativeInspectionNeeded()
        if tag == 71:
            control = payload[:4][::-1]
            if control == b"secd":
                if level != 1:
                    raise NativeInspectionNeeded()
                sections.append(len(starts) - 1)
            if control in _FIXED_CONTROLS:
                fixed.add(len(starts) - 1)
            elif control not in _MOVABLE_CONTROLS:
                raise NativeInspectionNeeded()
            elif control in {b"tbl ", b"gso "} and (len(payload) < 8 or not payload[4] & 1):
                # Page-anchored shapes can belong to a different logical block.
                fixed.add(len(starts) - 1)
        # Slice the original bytes, including non-minimal extended record headers.
        header, = struct.unpack_from("<I", data, offset)
        offset += (8 if header >> 20 == 4095 else 4) + len(payload)
        previous_level = level
    count = len(starts)
    if (sections != [0] or not count or len(indices) != count or any(type(i) is not int for i in indices)
            or set(indices) != set(range(count))):
        raise NativeInspectionNeeded()
    fixed.update((0, count - 1))
    if any(indices[i] != i for i in fixed):
        raise NativeInspectionNeeded()
    starts.append(len(data))
    view = memoryview(data)
    return b"".join(view[starts[i]:starts[i + 1]] for i in indices)


def _section_bytes(encoded, compressed, indices):
    if not compressed:
        return _reorder_records(encoded, indices)
    decoder = zlib.decompressobj(-15)
    data = decoder.decompress(encoded, MAX_PART + 1)
    if (not decoder.eof or len(data) > MAX_PART or decoder.unused_data not in
            (b"", struct.pack("<II", zlib.crc32(data), len(data)))):
        raise NativeInspectionNeeded()
    result = _reorder_records(data, indices)
    encoder = zlib.compressobj(wbits=-15)
    encoded = encoder.compress(result) + encoder.flush()
    if decoder.unused_data:
        encoded += struct.pack("<II", zlib.crc32(result), len(result))
    return encoded


def _write_section(copy, indices):
    root = pythoncom.StgOpenStorage(str(copy), None, _RW)
    header = _stream(root, "FileHeader")
    if (len(header) != 256 or header[:32] != b"HWP Document File".ljust(32, b"\0")
            or header[35] != 5 or struct.unpack_from("<I", header, 36)[0] not in (0, 1)
            or struct.unpack_from("<I", header, 40)[0] != 0):
        raise NativeInspectionNeeded()
    names = {entry[0] for entry in root.EnumElements()}
    if names - _ROOT_NAMES:
        raise NativeInspectionNeeded()
    if "DocOptions" in names:
        options = root.OpenStorage("DocOptions", None, _RW)
        # Digital signatures, DRM and distribution options are never rewritten.
        if any(entry[:2] != ("_LinkDoc", 2) for entry in options.EnumElements()):
            raise NativeInspectionNeeded()
    body = root.OpenStorage("BodyText", None, _RW)
    if [entry[:2] for entry in body.EnumElements().Next(2)] != [("Section0", 2)]:
        raise NativeInspectionNeeded()
    compressed = bool(header[36] & 1)
    properties = next(_records(_stream(root, "DocInfo", compressed)), None)
    if (properties is None or properties[:2] != (16, 0) or len(properties[2]) < 2
            or struct.unpack_from("<H", properties[2])[0] != 1):
        raise NativeInspectionNeeded()
    new_section = _section_bytes(_stream(body, "Section0"), compressed, indices)
    stream = body.OpenStream("Section0", None, _RW)
    if stream.Write(new_section) != len(new_section):
        raise OSError("HWP 문단 사본에 기록할 수 없습니다.")
    stream.SetSize(len(new_section))
    stream.Commit(0)
    body.Commit(0)
    root.Commit(0)


def reorder_hwp_paragraphs(source: Path, output: Path, paragraph_indices: tuple[int, ...]) -> bool:
    """Publish a new candidate for native verification; indices list the original paragraph slots."""
    source, output = Path(source), Path(output)
    if source.suffix.lower() != ".hwp" or output.exists() or output.is_symlink():
        return False
    initialized = False
    try:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
        except pythoncom.com_error as error:
            if error.hresult != -2147417850:  # Already initialized on an MTA.
                raise
        with TemporaryDirectory(dir=output.parent) as directory:
            copy = Path(directory) / "paragraphs.hwp"
            shutil.copyfile(source, copy)
            try:
                _write_section(copy, paragraph_indices)
            except (OSError, NativeInspectionNeeded, struct.error, pythoncom.com_error, zlib.error):
                # Release traceback-held IStorage handles before Windows cleans
                # up the rejected private compound file.
                return False
            # Windows rename fails if another writer has claimed the output.
            os.rename(copy, output)
        return True
    except (OSError, pythoncom.com_error):
        return False
    finally:
        if initialized:
            pythoncom.CoUninitialize()
