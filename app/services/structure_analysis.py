from __future__ import annotations

from dataclasses import dataclass
import re

from .auto_qid import DetectedQuestion, ParagraphRecord
from .school_profile import SchoolProfile


GROUP_LABEL = re.compile(r"^\s*\[\s*(?P<body>[^\]\r\n]{1,60})\s*\]")
NUMBER_RANGE = re.compile(
    r"^(?P<start>\d{1,3})\s*[-~～]\s*(?P<end>\d{1,3})"
    r"(?:\s*,\s*(?P<written>서답형|서술형)\s*(?P<written_no>\d{1,3}))?$"
)
MIXED_SINGLE = re.compile(
    r"^(?P<number>\d{1,3})\s*,\s*(?P<written>서답형|서술형)"
    r"\s*(?P<written_no>\d{1,3})$"
)
WRITTEN_RANGE = re.compile(
    r"^(?P<written>서답형|서술형)\s*(?P<start>\d{1,3})"
    r"\s*[-~～]\s*(?P<end>\d{1,3})$"
)
SECTION_RANGE_NOTICE = re.compile(
    r"(?:선택형|객관식|서답형|서술형)\s*(?:문제|문항)|"
    r"OMR\s*카드|답란에\s*(?:답|정답)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StructureItem:
    kind: str
    paragraph: int
    label: str
    detail: str
    member_count_hint: int
    confidence: str
    objective_numbers: tuple[int, ...] = ()
    written_numbers: tuple[int, ...] = ()


@dataclass(frozen=True)
class ImportUnit:
    kind: str
    start_paragraph: int
    end_paragraph: int | None
    label: str
    questions: tuple[DetectedQuestion, ...]

    @property
    def objective_count(self) -> int:
        return sum(item.kind == "objective" for item in self.questions)

    @property
    def written_count(self) -> int:
        return sum(item.kind == "written" for item in self.questions)

    @property
    def numbered(self) -> bool:
        return self.kind == "question" and bool(self.questions[0].numbered)


@dataclass(frozen=True)
class StructureAnalysis:
    profile_id: str
    suggested_mode: str
    objective_count: int
    written_count: int
    groups: tuple[StructureItem, ...]
    fixed_blocks: tuple[StructureItem, ...]
    warnings: tuple[str, ...]

    @property
    def question_count(self) -> int:
        return self.objective_count + self.written_count

    def summary(self) -> str:
        return (
            f"선택형 {self.objective_count}개 · 서답/서술형 {self.written_count}개 · "
            f"공통 묶음 {len(self.groups)}개 · 고정 안내 {len(self.fixed_blocks)}개"
        )


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _group_item(record: ParagraphRecord) -> StructureItem | None:
    compact = _compact(record.text)
    label_match = GROUP_LABEL.match(compact)
    if label_match is None:
        return None
    body = _compact(label_match.group("body"))
    suffix = _compact(compact[label_match.end() :])
    # Number ranges are also used by schools as section notices, for example
    # ``[1~19] 선택형 문제입니다. 답을 OMR카드에 ...``.  Such a notice
    # describes the whole section; it is not a shared passage to be moved as
    # one enormous native block.
    if SECTION_RANGE_NOTICE.search(suffix):
        return None
    range_match = NUMBER_RANGE.fullmatch(body)
    if range_match is not None:
        start = int(range_match.group("start"))
        end = int(range_match.group("end"))
        if end < start:
            return None
        written = range_match.group("written")
        member_count = end - start + 1 + (1 if written else 0)
        detail = f"선택형 {start}~{end}"
        if written:
            detail += f" + {written} {range_match.group('written_no')}"
        return StructureItem(
            kind="group",
            paragraph=record.paragraph,
            label=f"[{body}]",
            detail=detail,
            member_count_hint=member_count,
            confidence="high",
            objective_numbers=tuple(range(start, end + 1)),
            written_numbers=(
                (int(range_match.group("written_no")),) if written else ()
            ),
        )
    mixed_match = MIXED_SINGLE.fullmatch(body)
    if mixed_match is not None:
        return StructureItem(
            kind="group",
            paragraph=record.paragraph,
            label=f"[{body}]",
            detail=(
                f"선택형 {mixed_match.group('number')} + "
                f"{mixed_match.group('written')} {mixed_match.group('written_no')}"
            ),
            member_count_hint=2,
            confidence="high",
            objective_numbers=(int(mixed_match.group("number")),),
            written_numbers=(int(mixed_match.group("written_no")),),
        )
    written_match = WRITTEN_RANGE.fullmatch(body)
    if written_match is not None:
        start = int(written_match.group("start"))
        end = int(written_match.group("end"))
        if end < start:
            return None
        return StructureItem(
            kind="group",
            paragraph=record.paragraph,
            label=f"[{body}]",
            detail=f"{written_match.group('written')} {start}~{end}",
            member_count_hint=end - start + 1,
            confidence="high",
            written_numbers=tuple(range(start, end + 1)),
        )
    return None


def analyze_structure(
    records: tuple[ParagraphRecord, ...],
    questions: tuple[DetectedQuestion, ...],
    profile: SchoolProfile,
) -> StructureAnalysis:
    groups = tuple(
        item
        for record in records
        if (item := _group_item(record)) is not None
    )
    fixed: list[StructureItem] = []
    for record in records:
        compact = _compact(record.text)
        if not compact:
            continue
        prefix = next(
            (
                item
                for item in profile.fixed_phrase_prefixes
                if compact.startswith(item)
            ),
            None,
        )
        if prefix is None:
            continue
        fixed.append(
            StructureItem(
                kind="fixed",
                paragraph=record.paragraph,
                label=prefix,
                detail="고정 안내문 후보 · 문항 재배열 대상 아님",
                member_count_hint=0,
                confidence="high",
            )
        )

    objective = tuple(item for item in questions if item.kind == "objective")
    written = tuple(item for item in questions if item.kind == "written")
    has_mixed_group = any(
        re.match(r"^\[\s*\d+.*(?:서답형|서술형)", item.label) is not None
        for item in groups
    )
    has_transition = any("여기서부터" in item.label for item in fixed)
    interleaved_written = bool(
        written
        and objective
        and min(item.paragraph for item in written)
        < max(item.paragraph for item in objective)
    )
    if has_mixed_group or interleaved_written:
        mode = "mixed_passage"
    elif written and objective and (
        has_transition
        or min(item.paragraph for item in written)
        > max(item.paragraph for item in objective)
    ):
        mode = "sequential"
    else:
        mode = "objective_passage"
    if mode not in profile.subject_modes:
        raise ValueError(f"분석 설정에 없는 시험지 모드입니다: {mode}")

    warnings: list[str] = []
    if groups:
        warnings.append(
            "공통 지문/그림 묶음은 하위 문항과 분리하지 않고 함께 이동해야 합니다."
        )
    if fixed:
        warnings.append(
            "고정 안내문은 문항 번호를 붙이거나 재배열하지 않습니다."
        )
    if not questions:
        warnings.append("채점 문항 시작을 찾지 못했습니다.")
    return StructureAnalysis(
        profile_id=profile.profile_id,
        suggested_mode=mode,
        objective_count=len(objective),
        written_count=len(written),
        groups=groups,
        fixed_blocks=tuple(fixed),
        warnings=tuple(warnings),
    )


def _questions_for_group(
    group: StructureItem,
    questions: tuple[DetectedQuestion, ...],
) -> tuple[DetectedQuestion, ...]:
    selected: list[DetectedQuestion] = []
    for kind, numbers in (
        ("objective", group.objective_numbers),
        ("written", group.written_numbers),
    ):
        for number in numbers:
            match = next(
                (
                    item
                    for item in questions
                    if item.paragraph > group.paragraph
                    and item.kind == kind
                    and item.source_number == number
                    and item not in selected
                ),
                None,
            )
            if match is None:
                raise ValueError(
                    f"공통 묶음 {group.label}의 {kind} {number}번을 찾지 못했습니다. "
                    "자동 수합을 중단하고 묶음 분석 결과를 확인하세요."
                )
            selected.append(match)
    return tuple(sorted(selected, key=lambda item: item.paragraph))


def plan_import_units(
    questions: tuple[DetectedQuestion, ...],
    analysis: StructureAnalysis,
) -> tuple[ImportUnit, ...]:
    """Create non-overlapping native ranges while excluding fixed blocks.

    A common passage becomes one GID unit. Its individual scoring questions
    remain available as invisible child QIDs, but they are never top-level
    reorder boundaries. Fixed school notices only split adjacent ranges and
    are deliberately not copied from teacher files.
    """
    grouped_questions: set[DetectedQuestion] = set()
    group_questions: dict[int, tuple[DetectedQuestion, ...]] = {}
    for group in analysis.groups:
        children = _questions_for_group(group, questions)
        overlap = grouped_questions.intersection(children)
        if overlap:
            raise ValueError(
                f"문항이 둘 이상의 공통 묶음에 포함됩니다: {analysis.groups!r}"
            )
        grouped_questions.update(children)
        group_questions[group.paragraph] = children

    starts: dict[int, tuple[str, object]] = {}
    for group in analysis.groups:
        starts[group.paragraph] = ("group", group)
    for fixed in analysis.fixed_blocks:
        if fixed.paragraph in starts:
            raise ValueError(f"같은 문단에 묶음과 고정 블록이 있습니다: {fixed.paragraph}")
        starts[fixed.paragraph] = ("fixed", fixed)
    for question in questions:
        if question in grouped_questions:
            continue
        if question.paragraph in starts:
            raise ValueError(f"같은 문단에 구조 표지와 문항이 있습니다: {question.paragraph}")
        starts[question.paragraph] = ("question", question)

    ordered_starts = tuple(sorted(starts))
    units: list[ImportUnit] = []
    for index, paragraph in enumerate(ordered_starts):
        kind, value = starts[paragraph]
        end = ordered_starts[index + 1] if index + 1 < len(ordered_starts) else None
        if kind == "fixed":
            continue
        if kind == "group":
            group = value
            assert isinstance(group, StructureItem)
            children = group_questions[group.paragraph]
            last_child = max(item.paragraph for item in children)
            if end is not None and end <= last_child:
                raise ValueError(
                    f"공통 묶음 {group.label} 안에 다른 구조 경계가 겹칩니다."
                )
            units.append(
                ImportUnit(
                    kind="group",
                    start_paragraph=paragraph,
                    end_paragraph=end,
                    label=group.label,
                    questions=children,
                )
            )
            continue
        question = value
        assert isinstance(question, DetectedQuestion)
        units.append(
            ImportUnit(
                kind="question",
                start_paragraph=paragraph,
                end_paragraph=end,
                label=question.preview,
                questions=(question,),
            )
        )
    if not units:
        raise ValueError("수합할 문항 또는 공통 묶음이 없습니다.")
    return tuple(units)


def validate_import_units(
    questions: tuple[DetectedQuestion, ...],
    analysis: StructureAnalysis,
    units: tuple[ImportUnit, ...],
) -> None:
    """Prove that the native copy plan is complete and non-overlapping."""
    if not questions or not units:
        raise ValueError("검증할 문항 수합 계획이 없습니다.")
    if len({item.id for item in questions}) != len(questions):
        raise ValueError("자동 인식 문항 ID가 중복되었습니다.")

    counts = {item: 0 for item in questions}
    previous_start = -1
    previous_end: int | None = -1
    group_starts: set[int] = set()
    for index, unit in enumerate(units):
        if unit.start_paragraph <= previous_start:
            raise ValueError("문항 블록 시작 위치가 문서 순서와 다릅니다.")
        if unit.end_paragraph is not None and unit.end_paragraph <= unit.start_paragraph:
            raise ValueError(f"문항 블록 범위가 비어 있습니다: {unit.label}")
        if previous_end is None:
            raise ValueError("문서 끝 블록 뒤에 다른 문항 블록이 있습니다.")
        if index and unit.start_paragraph < previous_end:
            raise ValueError(f"문항 블록 범위가 겹칩니다: {unit.label}")
        if unit.kind not in {"question", "group"}:
            raise ValueError(f"지원하지 않는 문항 블록 유형입니다: {unit.kind}")
        if unit.kind == "question" and len(unit.questions) != 1:
            raise ValueError("독립 문항 블록에는 문항이 정확히 하나여야 합니다.")
        if unit.kind == "group":
            if not unit.questions:
                raise ValueError(f"하위 문항이 없는 공통 묶음입니다: {unit.label}")
            group_starts.add(unit.start_paragraph)

        for question in unit.questions:
            if question not in counts:
                raise ValueError(f"수합 계획에 알 수 없는 문항이 있습니다: {question.id}")
            if question.paragraph < unit.start_paragraph:
                raise ValueError(f"문항이 묶음 시작보다 앞에 있습니다: {question.id}")
            if (
                unit.end_paragraph is not None
                and question.paragraph >= unit.end_paragraph
            ):
                raise ValueError(f"문항이 묶음 범위를 벗어났습니다: {question.id}")
            counts[question] += 1
        previous_start = unit.start_paragraph
        previous_end = unit.end_paragraph

    missing = tuple(item.id for item, count in counts.items() if count == 0)
    duplicated = tuple(item.id for item, count in counts.items() if count > 1)
    if missing or duplicated:
        raise ValueError(
            "문항 수합 계획의 포함 관계가 올바르지 않습니다: "
            f"누락={missing!r}, 중복={duplicated!r}"
        )
    expected_group_starts = {item.paragraph for item in analysis.groups}
    if group_starts != expected_group_starts:
        raise ValueError(
            "공통 묶음 계획이 구조 분석 결과와 다릅니다: "
            f"expected={sorted(expected_group_starts)!r}, "
            f"actual={sorted(group_starts)!r}"
        )
    for fixed in analysis.fixed_blocks:
        for unit in units:
            if unit.start_paragraph <= fixed.paragraph and (
                unit.end_paragraph is None or fixed.paragraph < unit.end_paragraph
            ):
                raise ValueError(
                    f"고정 안내문이 문항 이동 범위에 포함되었습니다: {fixed.label}"
                )
