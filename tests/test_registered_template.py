"""Offline validation of the registration contract and same-package assembly."""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.hwp.fields import FIXED_FIELD_PREFIX, TERMINAL_FIELD, QuestionFieldManager, question_field
from app.hwp.transaction import sha256
from app.services.registered_template import RegisteredTemplate


def reject(call, error):
    try:
        call()
    except error:
        return
    raise AssertionError("unsafe registered template accepted")


def main():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "school.hwpx"
        source.write_bytes(b"fixture source")
        path = root / "registration.json"
        regions = [
            {"key": "insert", "role": "insert", "start": [0, 2, 0], "end": [0, 2, 0]},
            {"key": "examples", "role": "replace", "start": [0, 2, 0], "end": [0, 6, 0]},
        ]
        doc = {"version": 1, "source_sha256": sha256(source), "regions": regions}
        path.write_text(json.dumps(doc))
        plan = RegisteredTemplate(path, source, root)
        assert plan.insert == (0, 2, 0)
        path.write_text(json.dumps({**doc, "source_sha256": "invalid"}))
        reject(lambda: RegisteredTemplate(path, source, root), ValueError)
        path.write_text(json.dumps({**doc, "regions": [*regions,
            {"key": "keep", "role": "keep", "start": [0, 3, 0], "end": [0, 5, 0]}]}))
        reject(lambda: RegisteredTemplate(path, source, root), ValueError)
        incoming = root / "collected.hwpx"
        with ZipFile(incoming, "w") as archive:
            archive.writestr("Contents/section0.xml", (
                '<sec xmlns="urn:sec" xmlns:p="urn:paragraph">'
                '<p:p><p:secPr/><p:t>header</p:t></p:p><p:p><p:t>sample</p:t></p:p>'
                '<p:p><p:t>tail</p:t></p:p><p:p><p:t>new</p:t><p:pic ref="image7"/></p:p></sec>'
            ))
            archive.writestr("BinData/image7.png", b"image bytes")
        result = root / "assembled.hwpx"
        plan.body_count = 3
        plan._write_body(incoming, result, 4, [(0, 0), (1, 3), (2, 2)])
        with ZipFile(result) as archive:
            tree = ET.fromstring(archive.read("Contents/section0.xml"))
            assert [p.find("{*}t").text for p in tree] == ["header", "new", "tail"]
            assert tree[1].find("{*}pic").get("ref") == "image7"
            assert archive.read("BinData/image7.png") == b"image bytes"
        output = root / "output.hwpx"
        output.write_bytes(b"someone else's output")
        reject(lambda: plan.publish(result, output), FileExistsError)
        assert output.read_bytes() == b"someone else's output"
        assert source.read_bytes() == b"fixture source"
        target = root / "new.hwpx"
        plan.publish(result, target)
        assert target.read_bytes() == result.read_bytes()
        race = root / "race.hwpx"
        publish_name = "rename" if os.name == "nt" else "link"
        atomic = getattr(os, publish_name)
        def competing_writer(temporary, destination):
            Path(destination).write_bytes(b"created after validation")
            return atomic(temporary, destination)
        with patch("app.services.registered_template.os." + publish_name, side_effect=competing_writer):
            reject(lambda: plan.publish(result, race), FileExistsError)
        assert race.read_bytes() == b"created after validation"
        assert not list(root.glob(".registered-*"))
        plan._temp.cleanup()

    fields = QuestionFieldManager(None)
    fixed = FIXED_FIELD_PREFIX + "1"
    fields._field_names_cache = (question_field("Q001"), fixed, question_field("Q002"), TERMINAL_FIELD)
    assert fields.next_boundary("Q001") == fixed and fields.next_boundary("Q002") == TERMINAL_FIELD
    print("Registered template validation, protected layout and resource retention passed")


if __name__ == "__main__":
    main()
