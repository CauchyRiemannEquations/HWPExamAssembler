from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import shutil
import stat

from app.hwp import (
    HwpApplication,
    QuestionFieldManager,
    capture_template_numbering_style,
    load_numbering_profile,
    renumber_visible_questions,
)
from app.hwp.fields import TERMINAL_FIELD, question_field
from app.hwp.transaction import sha256


NUMBERED_START = re.compile(r"^\s*(?P<number>\d{1,3})\s*[.)]\s*\S")
NUMBER_ONLY_START = re.compile(r"^\s*(?P<number>\d{1,3})\s*[.)]\s*$")
LITERAL_NUMBER_PREFIX = re.compile(r"^[ \t]*\d{1,3}[ \t]*[.)][ \t]*")
SHORT_ANSWER_START = re.compile(
    r"^\s*(?:\[\s*)?(?P<label>서답형|서술형)\s*"
    r"(?P<number>\d+)(?:\s*\])?"
)
SHORT_ANSWER_RANGE_HEADER = re.compile(
    r"^\s*\[?\s*(?:서답형|서술형)\s*\d{1,3}\s*[-~～]\s*"
    r"(?:(?:서답형|서술형)\s*)?\d{1,3}\s*\]?"
)


@dataclass(frozen=True)
class ParagraphRecord:
    paragraph: int
    text: str
    heading_type: int


@dataclass(frozen=True)
class DetectedQuestion:
    id: str
    paragraph: int
    preview: str
    numbered: bool
    reason: str
    literal_prefix_chars: int = 0
    kind: str = "objective"
    source_number: int | None = None


def _unique_output(source: Path) -> Path:
    candidate = source.with_name(source.stem + "_문항등록본" + source.suffix)
    counter = 2
    while candidate.exists():
        candidate = source.with_name(
            f"{source.stem}_문항등록본_{counter}{source.suffix}"
        )
        counter += 1
    return candidate


def paragraph_text(hwp: HwpApplication, paragraph: int) -> str | None:
    if not hwp.automation.SetPos(0, paragraph, 0):
        return None
    # InitScan is more reliable than creating a visible selection for empty
    # paragraphs and paragraphs whose first item is a floating HWP control.
    initialized = hwp.automation.InitScan(
        0x00, 0x0011, paragraph, 0, paragraph + 1, 0
    )
    if not initialized:
        return ""
    chunks: list[str] = []
    try:
        for _ in range(1000):
            result = hwp.automation.GetText()
            if not result or not hasattr(result, "__len__"):
                break
            values = tuple(result)
            if len(values) != 2:
                break
            status, text = int(values[0]), str(values[1] or "")
            if text:
                chunks.append(text)
            if status in {0, 1, 101, 102}:
                break
    finally:
        hwp.automation.ReleaseScan()
    return "".join(chunks)


def scan_paragraphs(
    hwp: HwpApplication, *, max_paragraphs: int = 5000
) -> tuple[ParagraphRecord, ...]:
    # HWP 2022's SetPos(0, para, 0) can return True even after ``para`` has
    # passed the real end of the body.  It then keeps the caret clamped to the
    # last paragraph, so the former sentinel loop scanned thousands of phantom
    # paragraphs and made a normal exam take tens of seconds to inspect.
    hwp.move_to_end()
    document_end = hwp.current_position()
    if document_end[0] != 0:
        raise RuntimeError(
            f"본문 끝 위치를 안전하게 읽지 못했습니다: {document_end!r}"
        )
    paragraph_count = min(document_end[1] + 1, max_paragraphs)
    records: list[ParagraphRecord] = []
    for paragraph in range(paragraph_count):
        if not hwp.automation.SetPos(0, paragraph, 0):
            break
        parameter = hwp.automation.HParameterSet.HParaShape
        hwp.automation.HAction.GetDefault("ParagraphShape", parameter.HSet)
        text = paragraph_text(hwp, paragraph)
        if text is None:
            break
        records.append(
            ParagraphRecord(
                paragraph=paragraph,
                text=text,
                heading_type=int(parameter.HeadingType),
            )
        )
    return tuple(records)


def detect_questions_from_records(
    records: tuple[ParagraphRecord, ...],
) -> tuple[DetectedQuestion, ...]:
    candidates: list[tuple[int, str, bool, str, int]] = []
    kinds: list[str] = []
    source_numbers: list[int | None] = []
    objective_sequence = 0
    for record in records:
        paragraph = record.paragraph
        text = record.text
        heading_type = record.heading_type
        compact = re.sub(r"\s+", " ", text).strip()
        auto_number = heading_type == 2
        short_answer_match = SHORT_ANSWER_START.match(compact)
        # ``[서답형1~서답형4] 답란에 ...`` is a section notice, not the
        # first scoring question.  The old prefix-only expression stopped at
        # ``서답형1`` and created one spurious QID for the notice.
        if SHORT_ANSWER_RANGE_HEADER.match(compact):
            short_answer_match = None
        literal_number_match = NUMBERED_START.match(compact)
        number_only_match = NUMBER_ONLY_START.match(compact)
        short_answer = bool(short_answer_match)
        literal_number = bool(literal_number_match)
        literal_match = LITERAL_NUMBER_PREFIX.match(text)
        number_only = bool(number_only_match)
        if not (short_answer or literal_number or number_only or auto_number):
            continue
        if len(compact) < 4 and not (number_only or auto_number):
            continue
        if short_answer:
            numbered = False
            reason = "서답형 표지"
        elif auto_number:
            numbered = True
            reason = "한글 문단 번호"
        else:
            numbered = True
            reason = "문단 첫 문제 번호"
        if short_answer_match is not None:
            kind = "written"
            source_number = int(short_answer_match.group("number"))
        elif literal_number or number_only:
            kind = "objective"
            objective_sequence += 1
            source_number = (
                int(literal_number_match.group("number"))
                if literal_number_match is not None
                else int(number_only_match.group("number"))
            )
        else:
            kind = "objective"
            objective_sequence += 1
            source_number = objective_sequence
        candidates.append(
            (
                paragraph,
                (compact or "(개체로 시작하는 한글 문단 번호 문항)")[:120],
                numbered,
                reason,
                literal_match.end() if literal_match else 0,
            )
        )
        kinds.append(kind)
        source_numbers.append(source_number)

    return tuple(
        DetectedQuestion(
            id=f"Q{index:03d}",
            paragraph=paragraph,
            preview=preview,
            numbered=numbered,
            reason=reason,
            literal_prefix_chars=literal_prefix_chars,
            kind=kind,
            source_number=source_number,
        )
        for index, ((
            paragraph,
            preview,
            numbered,
            reason,
            literal_prefix_chars,
        ), kind, source_number) in enumerate(
            zip(candidates, kinds, source_numbers, strict=True),
            start=1,
        )
    )


def detect_questions(
    hwp: HwpApplication, *, max_paragraphs: int = 5000
) -> tuple[DetectedQuestion, ...]:
    return detect_questions_from_records(
        scan_paragraphs(hwp, max_paragraphs=max_paragraphs)
    )


def strip_literal_number_prefixes(
    hwp: HwpApplication, detected: tuple[DetectedQuestion, ...]
) -> None:
    """Remove only verified, directly typed question-number prefixes."""
    numbered = tuple(q for q in detected if q.literal_prefix_chars)
    if not numbered:
        return
    paragraphs = tuple(((0, q.paragraph, 0), (0, q.paragraph + 1, 0)) for q in numbered)
    before = hwp.control_summaries_for_ranges(paragraphs)
    for question in reversed(numbered):
        text = paragraph_text(hwp, question.paragraph)
        match = LITERAL_NUMBER_PREFIX.match(text or "")
        if match is None or match.end() != question.literal_prefix_chars:
            raise RuntimeError(
                f"{question.id}의 직접 입력 번호를 확인하지 못했습니다."
            )
        # Deleting a selection ending at a floating picture also deletes
        # that picture in Hancom. Forward-delete the verified text from the
        # actual start caret, which also skips a first-page section control.
        hwp.set_position((0, question.paragraph, 0))
        for _ in match.group():
            hwp.automation.Run("Delete")
        if paragraph_text(hwp, question.paragraph) != text[match.end():]:
            raise RuntimeError(f"{question.id}의 번호 정리 중 문항 내용이 달라졌습니다.")
    after = hwp.control_summaries_for_ranges(paragraphs)
    for q, original, current in zip(numbered, before, after, strict=True):
        if original != current:
            raise RuntimeError(f"{q.id}의 번호 정리 중 수식·그림·표 구성이 달라졌습니다.")


def create_auto_qid_copy(
    source: str | Path,
    *,
    visible: bool = True,
    on_progress: Callable[[str], None] | None = None,
    numbering_profile_path: str | Path | None = None,
) -> Path:
    original = Path(source).resolve()
    if not original.is_file():
        raise FileNotFoundError(original)
    output = _unique_output(original)
    shutil.copy2(original, output)
    output.chmod(output.stat().st_mode | stat.S_IWRITE)
    source_hash = sha256(original)

    def progress(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    progress(f"{original.name}에서 문항 시작 자동 인식")
    numbering_profile = None
    numbering_style_source = "document-unchanged"
    with HwpApplication(visible=visible) as hwp:
        hwp.open(output)
        detected = detect_questions(hwp)
        if not detected:
            raise ValueError(
                "문제 번호를 자동 인식하지 못했습니다. "
                "문항이 '1.', '1)' 또는 '[서답형 1]' 형태로 시작하는지 확인하세요."
            )
        manager = QuestionFieldManager(hwp)
        if manager.ordered_question_ids():
            raise ValueError("이미 내부 문항 ID가 있는 파일입니다.")
        if numbering_profile_path is not None:
            fallback = load_numbering_profile(numbering_profile_path)
            first_numbered = next(
                (item for item in detected if item.numbered), None
            )
            if first_numbered is not None:
                hwp.set_position((0, first_numbered.paragraph, 0))
                literal = NUMBERED_START.match(first_numbered.preview)
                literal_format = None
                if literal is not None:
                    matched_text = literal.group(0)
                    punctuation = ")" if ")" in matched_text else "."
                    literal_format = f"^1{punctuation}"
                numbering_profile, numbering_style_source = (
                    capture_template_numbering_style(
                        hwp, fallback, literal_format=literal_format
                    )
                )
            else:
                numbering_profile = fallback
                numbering_style_source = "generic-fallback"
        controls_before = hwp.control_summary()
        # HWP's automatic paragraph number is not part of InitScan text, so
        # only directly typed and re-verified prefixes are removed.
        strip_literal_number_prefixes(hwp, detected)
        hwp.move_to_end()
        manager.create_at_current_position(TERMINAL_FIELD)
        for question in reversed(detected):
            hwp.set_position((0, question.paragraph, 0))
            manager.create_at_current_position(question_field(question.id))
            progress(f"{question.id} 자동 등록: {question.preview[:45]}")
            hwp.visual_step(0.03 if visible else 0.0)
        if numbering_profile is not None:
            renumber_visible_questions(
                manager,
                tuple(item.id for item in detected if item.numbered),
                style=numbering_profile,
                on_progress=progress,
                step_delay=0.03 if visible else 0.0,
            )
        hwp.save_document(hwp.active_document())
        hwp.reset_documents()

        progress("자동 등록본 저장 후 다시 열어 검증")
        hwp.open(output)
        manager = QuestionFieldManager(hwp)
        order = manager.ordered_question_ids()
        controls_after = hwp.control_summary()
        expected = tuple(item.id for item in detected)
        if order != expected:
            raise RuntimeError(f"자동 QID 검증 실패: {order!r} != {expected!r}")
        for key in ("eqed:수식", "gso:그림", "tbl:표"):
            if int(controls_before.get(key, 0)) != int(controls_after.get(key, 0)):
                raise RuntimeError(f"자동 QID 등록 중 네이티브 개체 수 변경: {key}")
        hwp.reset_documents()

    audit = {
        "method": "automatic paragraph-number detection + invisible QID fields",
        "source": str(original),
        "output": str(output),
        "source_sha256": source_hash,
        "source_unchanged": sha256(original) == source_hash,
        "detected": [asdict(item) for item in detected],
        "order": list(order),
        "controls_before": controls_before,
        "controls_after": controls_after,
        "numbering_style_source": numbering_style_source,
        "numbering_style": (
            numbering_profile.to_dict() if numbering_profile is not None else None
        ),
    }
    output.with_suffix(output.suffix + ".qid.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output
