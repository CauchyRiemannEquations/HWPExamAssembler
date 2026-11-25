"""Reorder aligned question blocks in a private copy and verify it in Hangul."""
from pathlib import Path
from xml.etree import ElementTree as ET

import pythoncom

from .application import HwpAutomationError
from .fields import FIXED_FIELD_PREFIX, TERMINAL_FIELD, QuestionFieldManager, question_field
from .user_document import _content_digest


def _plan(manager, root, order):
    body = root.find("BODY")
    if body is None or len(body) != 1 or body[0].tag != "SECTION":
        return None
    section = body[0]
    if len(section) == 0 or any(p.tag != "P" for p in section):
        return None
    layout = manager.block_layout()
    questions = [item for item in layout if not item.startswith(FIXED_FIELD_PREFIX)]
    if len(order) != len(questions) or set(order) != set(questions):
        return None
    starts = []
    for item in (*layout, TERMINAL_FIELD):
        name = item if item.startswith(FIXED_FIELD_PREFIX) or item == TERMINAL_FIELD else question_field(item)
        pos = manager.block_boundary_position(name)
        index = pos[1]
        if (pos != (0, index, 0) or manager.anchor_position(name) != pos
                or not 0 <= index < len(section) or (starts and index <= starts[-1])):
            return None
        if not any(f.get("Type") == "Clickhere" and f.get("Name") == name
                   for f in section[index].iterfind("TEXT/FIELDBEGIN")):
            return None
        starts.append(index)
    blocks = dict(zip(layout, (range(a, b) for a, b in zip(starts, starts[1:])), strict=True))
    requested = iter(order)
    target = tuple(item if item.startswith(FIXED_FIELD_PREFIX) else next(requested) for item in layout)
    indices = list(range(starts[0]))
    for item in target:
        indices.extend(blocks[item])
    indices.extend(range(starts[-1], len(section)))
    return target, tuple(indices)


def open_verified_paragraph_order(manager, source: Path, working: Path, candidate: Path, order, on_progress=None):
    """Leave the private tab on an equivalent reordered copy, or restore working."""
    hwp = manager.hwp
    try:
        root = ET.fromstring(hwp.automation.GetTextFile("HWPML2X", ""))
        plan = _plan(manager, root, order)
        if plan is None:
            return False
        target, indices = plan
        candidate.parent.mkdir(exist_ok=True)
        if working.suffix.lower() == ".hwpx":
            from .paragraph_reorder import reorder_hwpx_paragraphs as write_copy
        else:
            from app.services.hwp_paragraph_reorder import reorder_hwp_paragraphs as write_copy
        if not write_copy(source, candidate, indices):
            return False
        section = root.find("./BODY/SECTION")
        original = list(section)
        section[:] = [original[index] for index in indices]
        expected = _content_digest(ET.tostring(root, encoding="unicode"))
    except (ET.ParseError, ValueError, OSError, HwpAutomationError, pythoncom.com_error):
        return False

    if on_progress is not None:
        on_progress("문항 순서와 일치하는 작업 사본 열기")
    hwp.reset_documents()
    try:
        hwp.open(candidate)
        reordered = QuestionFieldManager(hwp)
        if reordered.block_layout() == target and reordered.ordered_question_ids() == order:
            actual = ET.fromstring(hwp.automation.GetTextFile("HWPML2X", ""))
            if _content_digest(ET.tostring(actual, encoding="unicode")) == expected:
                return True
    except (ET.ParseError, ValueError, OSError, HwpAutomationError, pythoncom.com_error):
        pass
    hwp.reset_documents()
    hwp.open(working)
    return False
