"""Boundary planning: COM observations plus pure range assembly.

LiveImportSession owns the adapter/documents. This module neither opens nor
closes them and preserves observation order from the diagnostic baseline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .structure_analysis import ImportUnit

if TYPE_CHECKING:
    from .live_import import LiveImportSession

Position = tuple[int, int, int]
PositionRange = tuple[Position, Position]


@dataclass(frozen=True)
class ImportBoundaryPlan:
    semantic_ranges: tuple[PositionRange, ...]
    copy_ranges: tuple[PositionRange, ...]
    integrity_ranges: tuple[PositionRange, ...]


def semantic_ranges_for(units: tuple[ImportUnit, ...], source_content_end: Position) -> tuple[PositionRange, ...]:
    semantic_ranges = tuple(
        (
            (0, unit.start_paragraph, 0),
            (
                (0, unit.end_paragraph, 0)
                if unit.end_paragraph is not None
                else source_content_end
            ),
        )
        for unit in units
    )
    return semantic_ranges


def shared_boundary_points(units, semantic_ranges) -> set[Position]:
    boundary_points = {
        semantic_start
        for semantic_start, _semantic_end in semantic_ranges
        if semantic_start[1] > 0
    }
    boundary_points.update(
        semantic_end
        for unit, (_semantic_start, semantic_end) in zip(
            units, semantic_ranges, strict=True
        )
        if unit.end_paragraph is not None and semantic_end[1] > 0
    )
    return boundary_points


def assemble_copy_ranges(units, semantic_ranges, resolved_boundaries) -> tuple[PositionRange, ...]:
    copy_ranges = []
    for unit, (semantic_start, semantic_end) in zip(
        units, semantic_ranges, strict=True
    ):
        copy_start = resolved_boundaries.get(semantic_start, semantic_start)
        copy_end = (
            resolved_boundaries[semantic_end]
            if unit.end_paragraph is not None
            else semantic_end
        )
        if copy_start >= copy_end:
            raise RuntimeError(
                "문항의 네이티브 개체 경계를 안전하게 만들지 못했습니다: "
                f"start={copy_start!r}, end={copy_end!r}"
            )
        copy_ranges.append((copy_start, copy_end))
    return tuple(copy_ranges)


def plan_import_boundaries(session: LiveImportSession, units, detected) -> ImportBoundaryPlan:
    # Count every question's native controls in one source traversal.
    # This must happen before a block is selected: walking HWP controls can
    # disturb the active selection, which caused a regression where Copy
    # lost equations or copied nothing.
    session._require_non_target_bridge(
        session.source_document, operation="출제 파일 개체 집계 전"
    )
    session.hwp.move_to_end()
    document_end = session.hwp.current_position()
    source_content_end = document_end
    last_question_start = (0, detected[-1].paragraph, 0)
    marker_hits: list[tuple[str, tuple[int, int, int]]] = []
    for marker in session.school_profile.final_check.source_markers:
        anchor = session.hwp.top_level_anchor_for_text(marker)
        if (
            anchor is not None
            and last_question_start < anchor < source_content_end
        ):
            marker_hits.append((marker, anchor))
    if marker_hits:
        marker, marker_anchor = min(marker_hits, key=lambda item: item[1])
        source_content_end = session.hwp.previous_paragraph_end(marker_anchor)
        if source_content_end <= last_question_start:
            raise RuntimeError(
                "출제 파일의 마지막 문항과 마무리 상자 경계를 "
                "안전하게 분리하지 못했습니다."
            )
        session._progress(
            f"출제 파일의 마무리 상자({marker})는 복사하지 않고 "
            "대상 양식의 마지막 고정 블록과 중복되지 않도록 제외"
        )
    semantic_ranges = semantic_ranges_for(units, source_content_end)
    # A floating drawing anchored at offset zero belongs to the following
    # question, but HWP's selection boundary is not half-open for that
    # object type: selecting exactly to/from the anchor can copy it with
    # the previous block and omit it from its own block. Use the shared
    # previous-paragraph-end boundary on both sides. It contains no text
    # from the previous question and preserves the following paragraph's
    # complete native object set.
    # Resolve each shared semantic boundary exactly once. Repeating the
    # COM movement for the previous block's end and the next block's
    # start would make an intermittent cursor result capable of creating
    # either a gap or an overlap between adjacent questions.
    boundary_points = shared_boundary_points(units, semantic_ranges)
    resolved_boundaries = {
        boundary: session.hwp.previous_paragraph_end(boundary)
        for boundary in sorted(boundary_points)
    }
    copy_ranges = assemble_copy_ranges(units, semantic_ranges, resolved_boundaries)
    for index in range(1, len(copy_ranges)):
        if (
            semantic_ranges[index - 1][1] == semantic_ranges[index][0]
            and copy_ranges[index - 1][1] != copy_ranges[index][0]
        ):
            session._diag("range_warning", warning="SHARED_BOUNDARY_MISMATCH",
                       unit_index=index, previous=copy_ranges[index - 1], current=copy_ranges[index])
            raise RuntimeError(
                "인접 문항의 네이티브 복사 경계가 일치하지 않습니다: "
                f"previous={copy_ranges[index - 1]!r}, "
                f"current={copy_ranges[index]!r}"
            )
    # A school's first-page header can share top-level paragraph zero
    # with the first common-passage label.  Counting from position zero
    # then attributes header/info tables to the question although HWP's
    # body selection correctly leaves those structural controls behind.
    # For a group, use the exact visible label start only for integrity
    # accounting; the native Copy range still begins at the whole
    # paragraph so its paragraph formatting is preserved.
    integrity_ranges: list[
        tuple[tuple[int, int, int], tuple[int, int, int]]
    ] = []
    for index, (unit, (_semantic_start, semantic_end)) in enumerate(zip(
        units, semantic_ranges, strict=True
    )):
        integrity_start = (0, unit.start_paragraph, 0)
        # The first visible question can share body paragraph zero with a
        # floating school/header table anchored before the selectable question
        # text.  HWP normalizes Select(0, 0, 0, ...) to the first selectable
        # character and correctly leaves that layout table out of Copy.  Count
        # controls from the normalized start as well, otherwise the verifier
        # expects a header table that can never appear in the pasted question.
        #
        # Probe only this exceptional first-question boundary.  Apart from
        # avoiding one COM round-trip per question, later shared boundaries
        # deliberately use previous_paragraph_end to retain floating objects.
        if index == 0 and unit.kind != "group" and integrity_start == (0, 0, 0):
            select_range = getattr(session.hwp, "select_position_range", None)
            if callable(select_range):
                selected = select_range(*copy_ranges[index])
                session.hwp.automation.Run("Cancel")
                normalized_start = tuple(selected[:3])
                if (
                    normalized_start[0] == 0
                    and normalized_start[1] == unit.start_paragraph
                    and integrity_start <= normalized_start < semantic_end
                ):
                    integrity_start = normalized_start
                    if normalized_start != (0, unit.start_paragraph, 0):
                        session._diag(
                            "range_adjustment",
                            reason="FIRST_PARAGRAPH_LAYOUT_PREFIX_EXCLUDED",
                            original=(0, unit.start_paragraph, 0),
                            normalized=normalized_start,
                        )
        if unit.kind == "group":
            session.hwp.automation.Run("MoveDocBegin")
            selected_label = session.hwp.find_text(unit.label)
            candidate = selected_label[:3]
            if candidate[0] != 0 or candidate[1] != unit.start_paragraph:
                raise RuntimeError(
                    f"공통 묶음 {unit.label} 시작 위치 검증 실패: "
                    f"expected paragraph={unit.start_paragraph}, "
                    f"actual={candidate!r}"
                )
            integrity_start = candidate
        integrity_ranges.append((integrity_start, semantic_end))
    return ImportBoundaryPlan(semantic_ranges, tuple(copy_ranges), tuple(integrity_ranges))
