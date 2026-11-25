"""Compound-file paragraph permutation without starting Hancom."""
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
import zlib

import pythoncom
from win32com.storagecon import STGM_CREATE, STGM_READ, STGM_READWRITE, STGM_SHARE_DENY_WRITE, STGM_SHARE_EXCLUSIVE

from app.services.hwp_paragraph_reorder import reorder_hwp_paragraphs
from app.services.quick_hwp import _stream


def record(tag, depth, payload, *, extended=False):
    size = 4095 if extended else len(payload)
    prefix = struct.pack("<I", tag | depth << 10 | size << 20)
    return prefix + (struct.pack("<I", len(payload)) if extended else b"") + payload


def paragraph(label, *, control=None, last=False):
    text = (label + "\r").encode("utf-16-le")
    count = len(text) // 2 | ((1 << 31) if last else 0)
    data = record(66, 0, struct.pack("<I", count) + bytes(20)) + record(67, 1, text)
    if control:
        data += record(71, 1, control[::-1] + struct.pack("<I", 1))
    return data


def encode(raw, compressed, trailer):
    if not compressed:
        return raw
    compressor = zlib.compressobj(wbits=-15)
    result = compressor.compress(raw) + compressor.flush()
    return result + (struct.pack("<II", zlib.crc32(raw), len(raw)) if trailer else b"")


def make_fixture(path, parts, *, flags=1, trailer=True, sections=1, signed=False, bad_crc=False):
    mode = STGM_CREATE | STGM_READWRITE | STGM_SHARE_EXCLUSIVE
    root = pythoncom.StgCreateDocfile(str(path), mode, 0)
    header = bytearray(256)
    header[:17] = b"HWP Document File"
    header[35] = 5
    struct.pack_into("<I", header, 36, flags)
    root.CreateStream("FileHeader", mode, 0, 0).Write(bytes(header))
    info = record(16, 0, struct.pack("<H", sections) + bytes(24))
    root.CreateStream("DocInfo", mode, 0, 0).Write(encode(info, flags & 1, trailer))
    root.CreateStream("PrvText", mode, 0, 0).Write(b"static preview")
    binary = root.CreateStorage("BinData", mode, 0, 0)
    binary.CreateStream("BIN0001.bmp", mode, 0, 0).Write(b"image bytes")
    opts = root.CreateStorage("DocOptions", mode, 0, 0)
    opts.CreateStream("DigitalSignature" if signed else "_LinkDoc", mode, 0, 0).Write(b"options")
    body = root.CreateStorage("BodyText", mode, 0, 0)
    encoded = encode(b"".join(parts), flags & 1, trailer)
    if bad_crc:
        encoded += b"unexpected trailer"
    for i in range(sections):
        body.CreateStream(f"Section{i}", mode, 0, 0).Write(encoded)


def streams(path):
    def read(storage, prefix, result):
        for name, kind, *_ in storage.EnumElements():
            part = (*prefix, name)
            if kind == 1:
                read(storage.OpenStorage(name, None, STGM_READ | STGM_SHARE_EXCLUSIVE), part, result)
            else:
                result[part] = _stream(storage, name)

    root = pythoncom.StgOpenStorage(str(path), None, STGM_READ | STGM_SHARE_DENY_WRITE)
    result = {}
    read(root, (), result)
    return result


class HwpParagraphReorderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.source, self.output = self.root / "source.hwp", self.root / "reordered.hwp"
        self.parts = [paragraph("first", control=b"secd"), paragraph("table", control=b"tbl "),
                      paragraph("picture", control=b"gso "), paragraph("last", last=True)]
        self.parts[1] += record(66, 2, bytes(24)) + record(67, 3, b"x\x00", extended=True)
        self.parts[2] += record(76, 2, b"picture container") + record(85, 3, b"picture reference")
        self.order = (0, 2, 1, 3)

    def test_complete_subtrees_and_unchanged_streams_with_each_compression_form(self):
        section = ("BodyText", "Section0")
        for flags, trailer in ((0, False), (1, False), (1, True)):
            with self.subTest(flags=flags, trailer=trailer):
                make_fixture(self.source, self.parts, flags=flags, trailer=trailer)
                before = streams(self.source)
                original = self.source.read_bytes()
                self.assertTrue(reorder_hwp_paragraphs(self.source, self.output, self.order))
                after = streams(self.output)
                self.assertEqual(before.keys(), after.keys())
                self.assertEqual({key for key in before if before[key] != after[key]}, {section})
                expected = b"".join(self.parts[i] for i in self.order)
                if flags:
                    decoder = zlib.decompressobj(-15)
                    self.assertEqual(decoder.decompress(after[section]), expected)
                    self.assertTrue(decoder.eof)
                    self.assertEqual(decoder.unused_data, struct.pack("<II", zlib.crc32(expected), len(expected))
                                     if trailer else b"")
                else:
                    self.assertEqual(after[section], expected)
                self.assertEqual(self.source.read_bytes(), original)
                self.output.unlink()

    def test_refuses_unsupported_inputs_without_leaving_output_or_temp(self):
        for options in ({"flags": 3}, {"flags": 9}, {"signed": True}, {"sections": 2}, {"bad_crc": True}):
            with self.subTest(options=options):
                make_fixture(self.source, self.parts, **options)
                self.assertFalse(reorder_hwp_paragraphs(self.source, self.output, self.order))
                self.assertEqual(list(self.root.iterdir()), [self.source])

        for bad_part in (paragraph("columns", control=b"cold"), paragraph("section", control=b"secd"),
                         paragraph("extra tag") + record(130, 1, b"x"), record(66, 0, b"malformed")):
            with self.subTest(bad_part=bad_part[:32]):
                make_fixture(self.source, [self.parts[0], bad_part, *self.parts[2:]])
                self.assertFalse(reorder_hwp_paragraphs(self.source, self.output, self.order))
                self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_requires_complete_permutation_and_a_new_output(self):
        make_fixture(self.source, self.parts)
        for invalid in ((0, 1, 2), (0, 1, 1, 3), (1, 0, 2, 3), (0, 1, 3, 2), (0, True, 2, 3)):
            self.assertFalse(reorder_hwp_paragraphs(self.source, self.output, invalid))
        original = self.source.read_bytes()
        self.assertFalse(reorder_hwp_paragraphs(self.source, self.source, self.order))
        self.assertEqual(self.source.read_bytes(), original)
        self.output.write_bytes(b"existing")
        self.assertFalse(reorder_hwp_paragraphs(self.source, self.output, self.order))
        self.assertEqual(self.output.read_bytes(), b"existing")
