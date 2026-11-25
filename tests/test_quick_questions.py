"""The fast path must reconstruct native question fields or defer to Hangul."""
from pathlib import Path
from tempfile import TemporaryDirectory
import struct
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pythoncom

from app.hwp.fields import CHILD_FIELD_PREFIX, FIELD_PREFIX, FIXED_FIELD_PREFIX, TERMINAL_FIELD
from app.services.quick_hwp import _control, _paragraphs, hwp_paragraphs
from app.services.quick_questions import HH, HP, HS, PLAIN, NativeInspectionNeeded, read_questions


def hwpx(path, names, *, change=None):
    header = ET.Element(HH + "head")
    shapes = ET.SubElement(ET.SubElement(header, HH + "refList"), HH + "paraProperties")
    shape = ET.SubElement(shapes, HH + "paraPr", id="0")
    ET.SubElement(shape, HH + "heading", type="NONE")
    section = ET.Element(HS + "sec")
    for index, (name, text) in enumerate(names):
        para = ET.SubElement(section, HP + "p", paraPrIDRef="0")
        run = ET.SubElement(para, HP + "run")
        if name:
            begin = ET.SubElement(run, HP + "ctrl")
            ET.SubElement(begin, HP + "fieldBegin", type="CLICK_HERE", name=name, id=str(index))
            end = ET.SubElement(run, HP + "ctrl")
            ET.SubElement(end, HP + "fieldEnd", beginIDRef=str(index))
        ET.SubElement(run, HP + "t").text = text
    if change:
        change(section, header)
    with ZipFile(path, "w") as saved:
        saved.writestr("mimetype", "application/hwp+zip")
        saved.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                       '<rootfiles><rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>'
                       '</rootfiles></container>')
        saved.writestr("Contents/content.hpf", '<package xmlns="http://www.idpf.org/2007/opf/"><manifest>'
                       '<item id="header" href="Contents/header.xml"/><item id="S0" href="Contents/section0.xml"/>'
                       '</manifest><spine><itemref idref="header"/><itemref idref="S0"/></spine></package>')
        saved.writestr("Contents/header.xml", ET.tostring(header, "utf-8"))
        saved.writestr("Contents/section0.xml", ET.tostring(section, "utf-8"))


class QuickQuestionsTest(unittest.TestCase):
    rows = [
        (None, "보호 표지"),
        (FIELD_PREFIX + "G001", "공통 지문"),
        (CHILD_FIELD_PREFIX + "G001__Q001__O", "선택형 문제"),
        (CHILD_FIELD_PREFIX + "G001__Q002__W", "서답형 문제"),
        (FIXED_FIELD_PREFIX + "1", "고정 안내"),
        (FIELD_PREFIX + "Q003", ""),
        (TERMINAL_FIELD, "보호 꼬리말"),
    ]

    def test_reads_group_and_blank_manual_question_without_fixed_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "questions.hwpx"
            hwpx(path, self.rows)
            group, manual = read_questions(path, {"Q003": {"manual_required": True, "preview": "원고"}})
        self.assertEqual(group.id, "G001")
        self.assertEqual(group.preview, "공통 지문 선택형 문제 서답형 문제")
        self.assertEqual(group.child_ids, ("Q001", "Q002"))
        self.assertEqual((group.objective_count, group.written_count), (1, 1))
        self.assertTrue(manual.manual_required)
        self.assertEqual(manual.preview, "직접 붙여넣을 원본: 원고")

    def test_untrusted_field_order_and_aliases_defer(self):
        def add_nested(section, _):
            nested = ET.SubElement(section[0][0], HP + "tbl")
            ET.SubElement(nested, HP + "fieldBegin", type="CLICK_HERE", name=FIELD_PREFIX + "Q001")

        def mismatch(section, _):
            section[1].find(".//" + HP + "fieldEnd").set("beginIDRef", "wrong")

        def object_before_fixed(section, _):
            section[4][0].insert(0, ET.Element(HP + "equation"))

        cases = [
            (self.rows[:-1], None), (self.rows + [(FIELD_PREFIX + "Q004", "extra")], None),
            (self.rows[:1] + [self.rows[2]] + self.rows[1:], None),
            (self.rows[:2] + [self.rows[1]] + self.rows[2:], None),
            (self.rows, add_nested), (self.rows, mismatch), (self.rows, object_before_fixed),
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unusual.hwpx"
            for rows, change in cases:
                with self.subTest(rows=len(rows), change=change):
                    hwpx(path, rows, change=change)
                    self.assertIsNone(read_questions(path, {}))

    def test_inline_table_picture_and_equation_belong_to_the_question(self):
        def add_table(section, header, caption=False):
            run = section[1][0]
            table = ET.SubElement(run, HP + "tbl", rowCnt="1", colCnt="1")
            ET.SubElement(table, HP + "pos", treatAsChar="1")
            cell = ET.SubElement(ET.SubElement(table, HP + "tr"), HP + "tc")
            ET.SubElement(cell, HP + "cellAddr", rowAddr="0", colAddr="0")
            ET.SubElement(cell, HP + "cellSpan", rowSpan="1", colSpan="1")
            cell_run = ET.SubElement(ET.SubElement(ET.SubElement(cell, HP + "subList"),
                                                   HP + "p", paraPrIDRef="0"), HP + "run")
            ET.SubElement(cell_run, HP + "t").text = "관찰 결과"
            ET.SubElement(ET.SubElement(cell_run, HP + "pic"), HP + "pos", treatAsChar="1")
            equation = ET.SubElement(cell_run, HP + "equation")
            if caption:
                ET.SubElement(equation, HP + "caption")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "objects.hwpx"
            hwpx(path, self.rows, change=add_table)
            group, _ = read_questions(path, {})
            self.assertIn("관찰 결과", group.preview)
            self.assertEqual(group.controls, {"tbl:표": 1, "gso:그림": 1, "eqed:수식": 1})
            hwpx(path, self.rows, change=lambda section, header: add_table(section, header, caption=True))
            self.assertIsNone(read_questions(path, {}))

    def test_nested_numbering_can_change_a_body_question_number(self):
        def reuse_header_number(section, header):
            reference = header.find(HH + "refList")
            numbering = ET.SubElement(ET.SubElement(reference, HH + "numberings"),
                                      HH + "numbering", id="1", start="1")
            ET.SubElement(numbering, HH + "paraHead", level="1", numFormat="DIGIT", start="1").text = "^1."
            shape = ET.SubElement(reference.find(HH + "paraProperties"), HH + "paraPr", id="1")
            ET.SubElement(shape, HH + "heading", type="NUMBER", level="0", idRef="1")
            table = ET.SubElement(section[0][0], HP + "tbl")
            ET.SubElement(table, HP + "p", paraPrIDRef="1")
            section[1].set("paraPrIDRef", "1")

        with TemporaryDirectory() as directory:
            path = Path(directory) / "numbering.hwpx"
            hwpx(path, self.rows, change=reuse_header_number)
            self.assertIsNone(read_questions(path, {}))

    def test_share_conflict_reads_private_copy_and_releases_it(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "open.hwp"
            path.write_bytes(b"opaque immutable source")
            calls = []

            def locked(document):
                calls.append(document)
                if document == path:
                    raise pythoncom.com_error(-2147287008, "sharing", None, None)
                self.assertEqual(document.read_bytes(), path.read_bytes())
                return ["read"]

            with patch("app.services.quick_hwp._read", side_effect=locked):
                self.assertEqual(hwp_paragraphs(path), ["read"])
            self.assertFalse(calls[1].exists())
            with patch("app.services.quick_hwp._read", side_effect=NativeInspectionNeeded()) as invalid:
                self.assertIsNone(read_questions(path, {}))
                invalid.assert_called_once()

    def test_binary_anchor_must_match_its_control_record(self):
        def record(tag, level, data):
            return struct.pack("<I", tag | level << 10 | len(data) << 20) + data

        begin = b"\x0b\x00deqe" + bytes(8) + b"\x0b\x00"
        data = (record(66, 0, bytes(24)) + record(67, 1, begin)
                + record(71, 1, b" lbt" + bytes(32)))
        with self.assertRaises(NativeInspectionNeeded):
            _paragraphs(data, [PLAIN], [0])

    def test_binary_equation_with_caption_uses_native_reader(self):
        self.assertEqual(_control(b"deqe", [(88, 2, b"eq")], 1, [PLAIN])[1],
                         [("object", "eqed:수식")])
        self.assertEqual(_control(b"deqe", [(88, 2, b"eq"), (72, 2, b"caption")], 1, [PLAIN])[1],
                         [("native", "")])


if __name__ == "__main__":
    unittest.main()
