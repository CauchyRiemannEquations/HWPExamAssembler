"""User-supplied template preparation and optional native final-block handling.

The public application never bundles or guesses a school's proprietary exam
paper. It edits a protected copy of the HWP/HWPX selected by the user. Any
example content is removed only from an explicit Hangul selection, and an
optional closing notice/table/shape is likewise captured from an explicit
selection as a native HWP fragment.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re
import statistics
import uuid
from typing import TYPE_CHECKING

from app.hwp import (
    QuestionFieldManager,
    capture_template_numbering_style,
)
from .auto_qid import detect_questions_from_records, scan_paragraphs
from .native_transfer import NativeBlockIntegrityError
from .format_analysis import capture_format_report

if TYPE_CHECKING:
    from .live_import import LiveImportSession


_MOVABLE_CONTROL_IDS = frozenset({"eqed", "gso", "tbl"})
_LITERAL_NUMBER = re.compile(r"^\s*\d{1,3}\s*(?P<punct>[.)])")


@dataclass(frozen=True)
class TemplateAnalysis:
    paragraph_count: int
    detected_question_count: int
    objective_count: int
    written_count: int
    first_question_paragraph: int | None
    last_question_paragraph: int | None
    suggested_separator_blank_paragraphs: int
    numbering_style_source: str
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.detected_question_count:
            detected = (
                f"예시 문항 후보 {self.detected_question_count}개 "
                f"(선택형 {self.objective_count}, 서답/서술형 {self.written_count})"
            )
        else:
            detected = "예시 문항 후보 없음"
        style_labels = {
            "template-native-numbering": "한글 문단 번호 서식",
            "template-literal-number-character-shape": "문자 번호의 글자 서식",
            "generic-fallback": "중립 기본 번호 서식",
        }
        return (
            f"본문 {self.paragraph_count}개 문단 · {detected} · "
            f"문항 간 빈 문단 {self.suggested_separator_blank_paragraphs}개 · "
            f"번호 기준: {style_labels.get(self.numbering_style_source, self.numbering_style_source)}"
        )


def _movable(summary: dict[str, int]) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in summary.items()
        if key.split(":", 1)[0] in _MOVABLE_CONTROL_IDS and int(value)
    }


def _removed_controls(
    before: dict[str, int], after: dict[str, int]
) -> dict[str, int]:
    return {
        key: int(before.get(key, 0)) - int(after.get(key, 0))
        for key in sorted(set(before) | set(after))
        if key.split(":", 1)[0] in _MOVABLE_CONTROL_IDS
        and int(before.get(key, 0)) != int(after.get(key, 0))
    }


def _added_controls(
    before: dict[str, int], after: dict[str, int]
) -> dict[str, int]:
    return {
        key: int(after.get(key, 0)) - int(before.get(key, 0))
        for key in sorted(set(before) | set(after))
        if key.split(":", 1)[0] in _MOVABLE_CONTROL_IDS
        and int(before.get(key, 0)) != int(after.get(key, 0))
    }


def analyze_user_template(session: LiveImportSession) -> TemplateAnalysis:
    """Inspect the working copy without deleting or rewriting any content."""
    if session.hwp is None or session.target_document is None:
        raise RuntimeError("양식 작업 사본이 열려 있지 않습니다.")
    session.hwp.activate_document(session.target_document)
    session.hwp.require_active_document(
        session.target_document, operation="양식 분석 전"
    )
    if getattr(session, "format_report", None) is None:
        try:
            session.format_report = capture_format_report(session.hwp)
        except Exception as error:
            session._progress(f"양식 서식 정보를 읽을 수 없습니다: {error}")
    records = scan_paragraphs(session.hwp)
    detected = detect_questions_from_records(records)
    by_paragraph = {item.paragraph: item for item in records}
    gap_samples: list[int] = []
    for question in detected[1:]:
        paragraph = question.paragraph - 1
        blank_count = 0
        while paragraph >= 0:
            record = by_paragraph.get(paragraph)
            if record is None or record.text.strip():
                break
            blank_count += 1
            paragraph -= 1
        gap_samples.append(min(blank_count, 3))
    suggested_gap = (
        int(statistics.median_low(gap_samples))
        if gap_samples
        else int(session.document_type.separator_blank_paragraphs)
    )
    if gap_samples and not session.template_spacing_learned:
        session.document_type = replace(
            session.document_type,
            separator_blank_paragraphs=suggested_gap,
            rule=(
                session.document_type.rule
                + f" · 양식 예시에서 문항 간 빈 문단 {suggested_gap}개 감지"
            ),
        )
        session.template_spacing_learned = True
    numbered = next((item for item in detected if item.numbered), None)
    style_source = "generic-fallback"
    if numbered is not None:
        record = next(
            item for item in records if item.paragraph == numbered.paragraph
        )
        match = _LITERAL_NUMBER.match(record.text)
        literal_format = f"^1{match.group('punct')}" if match else None
        session.hwp.set_position((0, numbered.paragraph, 0))
        session.numbering_style, style_source = capture_template_numbering_style(
            session.hwp,
            session.numbering_style,
            literal_format=literal_format,
        )

    warnings: list[str] = []
    if detected:
        warnings.append(
            "감지 결과는 삭제 명령이 아닙니다. 실제 한글 화면에서 범위를 확인한 뒤 선택 영역만 삭제하세요."
        )
    else:
        warnings.append(
            "문항 번호 예시를 찾지 못했습니다. 번호는 중립 기본값을 사용하며 한글에서 나중에 조정할 수 있습니다."
        )
    analysis = TemplateAnalysis(
        paragraph_count=len(records),
        detected_question_count=len(detected),
        objective_count=sum(item.kind == "objective" for item in detected),
        written_count=sum(item.kind == "written" for item in detected),
        first_question_paragraph=(detected[0].paragraph if detected else None),
        last_question_paragraph=(detected[-1].paragraph if detected else None),
        suggested_separator_blank_paragraphs=(
            int(session.document_type.separator_blank_paragraphs)
        ),
        numbering_style_source=style_source,
        warnings=tuple(warnings),
    )
    session.template_analysis = analysis
    session.numbering_style_source = style_source
    if detected:
        session.hwp.set_position((0, detected[0].paragraph, 0))
    else:
        session.hwp.move_to_end()
    session._progress(f"양식 분석: {analysis.summary()}")
    return analysis


def select_detected_example_area(
    session: LiveImportSession,
) -> tuple[int, int, int, int, int, int]:
    """Select the detected example body for visual review, never delete it."""
    analysis = analyze_user_template(session)
    if analysis.first_question_paragraph is None:
        raise ValueError("양식에서 예시 문항 시작 후보를 찾지 못했습니다.")
    session.hwp.move_to_end()
    end = session.hwp.current_position()
    start = (0, analysis.first_question_paragraph, 0)
    if start >= end:
        raise RuntimeError("예시 문항 선택 범위를 안전하게 만들지 못했습니다.")
    selected = session.hwp.select_position_range(start, end)
    session._progress(
        "첫 예시 문항 후보부터 본문 끝까지 선택했습니다. 삭제 전에 한글 화면에서 끝 범위를 확인하세요."
    )
    return selected


def _require_preparation_selection(
    session: LiveImportSession,
) -> tuple[tuple[int, int, int], tuple[int, int, int], str, dict[str, int]]:
    if session.template_ready:
        raise RuntimeError("수합을 시작한 뒤에는 양식 본문을 삭제할 수 없습니다.")
    if session.hwp is None or session.target_document is None:
        raise RuntimeError("양식 작업 사본이 열려 있지 않습니다.")
    session.hwp.require_active_document(
        session.target_document, operation="양식 선택 영역 확인"
    )
    selected = session.hwp.selected_position()
    if selected is None:
        raise ValueError("실제 한글 창에서 먼저 연속된 영역을 블록 선택하세요.")
    start = tuple(selected[:3])
    end = tuple(selected[3:])
    if start[0] != 0 or end[0] != 0 or start >= end:
        raise ValueError("본문의 연속된 한 영역만 선택할 수 있습니다.")
    text = session.hwp.selected_plain_text()
    controls = session.hwp.control_summary_in_range(start, end)
    return start, end, text, controls


def _delete_selected_range_verified(
    session: LiveImportSession,
    *,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    expected_controls: dict[str, int],
) -> None:
    before = session._transfer_control_summary()
    session.hwp.select_position_range(start, end)
    session.hwp.delete_selection()
    session.hwp.move_to_end()
    after = session._transfer_control_summary()
    removed = _removed_controls(before, after)
    expected = _movable(expected_controls)
    if any(value < 0 for value in removed.values()) or removed != expected:
        session.hwp.automation.Run("Undo")
        restored = session._transfer_control_summary()
        if _movable(restored) != _movable(before):
            raise NativeBlockIntegrityError(
                "선택 영역 삭제를 Undo로 완전히 복구하지 못했습니다: "
                f"before={_movable(before)!r}, restored={_movable(restored)!r}"
            )
        raise NativeBlockIntegrityError(
            "선택한 범위와 실제 삭제된 한글 개체가 다릅니다. "
            f"selected={expected!r}, removed={removed!r}"
        )
    session._target_control_totals = after


def capture_user_selected_final_block(session: LiveImportSession) -> Path:
    """Save one explicit native selection, then remove it from the work copy."""
    if session.final_block_path is not None:
        raise RuntimeError("마지막 고정 블록은 한 번만 보관할 수 있습니다.")
    start, end, text, controls = _require_preparation_selection(session)
    runtime_dir = session.work_dir / f"template-final-{uuid.uuid4().hex}"
    runtime_dir.mkdir(parents=True, exist_ok=False)
    block_path = runtime_dir / "selected-final-block.hwp"
    session.hwp.save_selection_as_hwp(block_path)
    try:
        _delete_selected_range_verified(
            session,
            start=start,
            end=end,
            expected_controls=controls,
        )
    except Exception:
        block_path.unlink(missing_ok=True)
        raise
    session.final_block_path = block_path
    session.final_block_expected_controls = _movable(controls)
    session.final_block_text = text
    session.hwp.save_document(session.target_document)
    session._progress(
        "선택한 마지막 고정 블록을 네이티브 HWP로 보관하고 작업 사본에서는 제거했습니다."
    )
    analyze_user_template(session)
    return block_path


def delete_selected_template_area(session: LiveImportSession) -> str:
    """Delete only the range the user selected in the visible Hangul window."""
    start, end, text, controls = _require_preparation_selection(session)
    _delete_selected_range_verified(
        session,
        start=start,
        end=end,
        expected_controls=controls,
    )
    session.hwp.save_document(session.target_document)
    session._progress("선택한 양식 본문 영역만 삭제했습니다.")
    analyze_user_template(session)
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:120]


def finalize_template_setup(session: LiveImportSession) -> None:
    """Freeze the prepared template body and create the private target marker."""
    if session.template_ready:
        return
    if session.hwp is None or session.target_document is None:
        raise RuntimeError("양식 작업 사본이 열려 있지 않습니다.")
    report = getattr(session, "format_report", None)
    if report is not None and session.output_path is not None and getattr(session, "registration", None) is None:
        try:
            report.save(session.output_path.with_suffix(session.output_path.suffix + ".format-review.json"))
        except OSError as error:
            session._progress(f"서식 검토 기록을 저장하지 못했습니다: {error}")
    session.hwp.activate_document(session.target_document)
    session.hwp.require_active_document(
        session.target_document, operation="양식 준비 완료 전"
    )
    session.hwp.automation.Run("Cancel")
    session.hwp.move_to_end()
    session._target_sentinel_field = "__HWPT__" + uuid.uuid4().hex[:20].upper()
    QuestionFieldManager(session.hwp).create_at_current_position(
        session._target_sentinel_field
    )
    session._require_target_bridge(operation="대상 문서 식별 필드 생성 확인")
    session.hwp.move_to_end()
    session.insertion_position = session.hwp.current_position()
    session._target_control_totals = session._transfer_control_summary()
    session.template_ready = True
    session.hwp.save_document(session.target_document)
    session._progress("양식 준비 완료 · 이제 출제 파일을 수합할 수 있습니다.")


def append_captured_final_block(session: LiveImportSession) -> None:
    """Append the user-selected native block once after the terminal QID field."""
    if session.final_block_path is None or session.final_block_added:
        return
    if session.hwp is None or session.target_document is None:
        raise RuntimeError("마지막 고정 블록을 추가할 대상 문서가 없습니다.")
    session._require_target_bridge(
        operation="마지막 고정 블록 붙여넣기 전", require_terminal=True
    )
    session.hwp.move_to_end()
    before = session._transfer_control_summary()
    session.hwp.break_paragraph()
    session.hwp.insert_hwp_file(session.final_block_path)
    session.hwp.move_to_end()
    after = session._transfer_control_summary()
    added = _added_controls(before, after)
    expected = _movable(session.final_block_expected_controls)
    if any(value < 0 for value in added.values()) or added != expected:
        raise NativeBlockIntegrityError(
            "마지막 고정 블록의 네이티브 개체 보존 검증 실패: "
            f"source={expected!r}, pasted={added!r}"
        )
    session._target_control_totals = after
    session.final_block_added = True
    # Legacy alias keeps older automation tests compatible; behavior is generic.
    session._final_check_added = True
    session._progress("보관한 마지막 고정 블록을 최종 문항 뒤에 한 번 추가했습니다.")


# Compatibility seams for older callers. The public workflow never supplies
# or discovers a built-in school template.
def builtin_template_path(session: LiveImportSession) -> Path:
    raise RuntimeError("공개 버전에는 내장 시험지 양식이 없습니다. 양식 파일을 선택하세요.")


def prepare_school_template(session: LiveImportSession) -> None:
    raise RuntimeError("내장 학교 양식 준비 기능은 공개 버전에서 사용하지 않습니다.")


def append_final_check_box(session: LiveImportSession) -> None:
    append_captured_final_block(session)


def prepare_blank_template(session: LiveImportSession) -> None:
    finalize_template_setup(session)
