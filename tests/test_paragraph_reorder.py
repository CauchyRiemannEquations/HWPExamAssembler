from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from app.hwp.paragraph_reorder import reorder_hwpx_paragraphs


SECTION = "Contents/section0.xml"
PREFIX = (b"<?xml version='1.0' encoding='UTF-8'?>\r\n"
          b"<s:sec xmlns:s='http://www.hancom.co.kr/hwpml/2011/section' "
          b"xmlns:p='http://www.hancom.co.kr/hwpml/2011/paragraph'>")
SUFFIX = b"</s:sec>"
SETTINGS = b"<p:p><p:run><p:secPr/><p:ctrl><p:colPr/></p:ctrl></p:run></p:p>"
SPINE = (b"<package xmlns='http://www.idpf.org/2007/opf/'><manifest>"
         b"<item id='h' href='Contents/header.xml'/><item id='s' href='Contents/section0.xml'/>"
         b"</manifest><spine><itemref idref='h'/><itemref idref='s'/></spine></package>")


def question(qid):
    return (f"<p:p><p:run><p:ctrl><p:fieldBegin id='{qid}' type='CLICK_HERE' name='__HWPQ__{qid}'/>"
            f"</p:ctrl><p:ctrl><p:fieldEnd beginIDRef='{qid}'/></p:ctrl>"
            f"<p:t>문항 &amp; {qid}</p:t></p:run></p:p>").encode()


def write_package(path, section, extra=None):
    entries = {"mimetype": b"application/hwp+zip", "Contents/content.hpf": SPINE,
               "META-INF/manifest.xml": b"<manifest/>", "Contents/header.xml": b"<head/>",
               SECTION: section, "BinData/image.bmp": bytes(range(256))}
    entries.update(extra or {})
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.comment = b"keep this comment"
        for name, data in entries.items():
            archive.writestr(name, data)


class ParagraphReorderTests(unittest.TestCase):
    def test_moves_raw_paragraphs_preserving_lexical_xml_and_other_members(self):
        rows = [SETTINGS, question("A"), b"<p:p />", question("B")]
        def section(items):
            return PREFIX + b"\n  " + items[0] + b"\r\n" + items[1] + items[2] + b"\t" + items[3] + SUFFIX
        order = (0, 3, 1, 2)
        with TemporaryDirectory() as directory:
            src, dst = (Path(directory) / name for name in ("source.hwpx", "result.hwpx"))
            write_package(src, section(rows))
            original = src.read_bytes()
            self.assertTrue(reorder_hwpx_paragraphs(src, dst, order))
            self.assertEqual(original, src.read_bytes())
            with ZipFile(src) as before, ZipFile(dst) as after:
                self.assertEqual(before.namelist(), after.namelist())
                self.assertEqual(before.comment, after.comment)
                for name in before.namelist():
                    expected = section([rows[i] for i in order]) if name == SECTION else before.read(name)
                    self.assertEqual(after.read(name), expected)
                    self.assertEqual(before.getinfo(name).date_time, after.getinfo(name).date_time)

    def test_declines_ambiguous_or_signed_packages_without_touching_destination(self):
        a, b = question("A"), question("B")
        whole = PREFIX + SETTINGS + a + b + SUFFIX
        order = (0, 2, 1)
        cases = (
            ("non-permutation", whole, (0, 1, 1), {}),
            ("section settings move", whole, (1, 0, 2), {}),
            ("non-body root child", whole.replace(SETTINGS, b"<p:run/>" + SETTINGS), order, {}),
            ("interstitial markup", whole.replace(a, a + b"<![CDATA[ ]]>"), order, {}),
            ("multiple sections", whole, order, {"Contents/section1.xml": whole}),
            ("detached field", whole.replace(b"beginIDRef='A'", b"beginIDRef='B'"), order, {}),
            ("duplicate field", whole.replace(b + SUFFIX, a + SUFFIX), order, {}),
            ("field after body text", whole.replace(a, a.replace(b"<p:run>", b"<p:run><p:t>x</p:t>", 1)), order, {}),
            ("tracked range", whole.replace(a, a.replace(b"<p:t>", b"<p:markpenBegin/><p:t>")), order, {}),
            ("signed", whole, order, {"META-INF/documentsignatures.xml": b"<Signature/>"}),
            ("unknown signature part", whole, order, {"META-INF/ds.xml": b"<Signature/>"}),
            ("encrypted manifest", whole, order, {"META-INF/manifest.xml": b"<manifest:encryption-data/>"}),
            ("other spine", whole, order, {"Contents/content.hpf": SPINE.replace(b"idref='s'", b"idref='h'")}),
        )
        with TemporaryDirectory() as directory:
            src, dst = (Path(directory) / name for name in ("source.hwpx", "result.hwpx"))
            dst.write_bytes(b"previous result")
            for label, section, indices, extra in cases:
                with self.subTest(label=label):
                    write_package(src, section, extra)
                    self.assertFalse(reorder_hwpx_paragraphs(src, dst, indices))
                    self.assertEqual(dst.read_bytes(), b"previous result")
            self.assertFalse(reorder_hwpx_paragraphs(src, src, order))
