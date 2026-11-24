"""Native block transaction and retry policy, without owning a COM session.

The caller owns document handles and mutable totals. Calls back through its
transaction methods preserve rollback boundaries and existing test seams.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING

from app.hwp.fields import question_field
from app.hwp.diagnostics import comparison, fingerprint, safe_error, safe_summary, structural

if TYPE_CHECKING:
    from .live_import import LiveImportSession


class NativeBlockIntegrityError(RuntimeError):
    """Raised after an incomplete native Paste has been rolled back."""


@dataclass(frozen=True)
class NativeTransferResult:
    selected_position: tuple[int, int, int, int, int, int]
    selected_text: str
    control_delta: dict[str, int]
    manual_required: bool
    import_error: str


def _first_pasted_content_paragraph(
    hwp,
    paragraph_reader,
    insertion_start: tuple[int, int, int],
    pasted_end: tuple[int, int, int],
) -> int | None:
    """Find the first pasted paragraph containing text or native HWP objects.

    Some questions begin with an object-only paragraph (for example, a table
    containing equations). Treating that paragraph as blank deletes real
    question content while removing the artificial paragraph created before
    Paste. Native controls therefore participate in the content check.
    """
    range_summary = getattr(hwp, "control_summary_in_range", None)
    for paragraph in range(insertion_start[1], pasted_end[1] + 1):
        text = paragraph_reader(hwp, paragraph)
        if text is not None and text.strip():
            return paragraph
        if not callable(range_summary):
            continue
        paragraph_start = (
            insertion_start
            if paragraph == insertion_start[1]
            else (insertion_start[0], paragraph, 0)
        )
        paragraph_end = (
            pasted_end
            if paragraph == pasted_end[1]
            else (insertion_start[0], paragraph + 1, 0)
        )
        if paragraph_start < paragraph_end and range_summary(
            paragraph_start, paragraph_end
        ):
            return paragraph
    return None


def append_copied_block(
    session: LiveImportSession,
    question_id: str,
    expected_controls: dict[str, int] | None = None,
    expected_text: str | None = None,
    child_fields: tuple[tuple[str, int], ...] = (),
) -> dict[str, int]:
    """Append one question transactionally at the target document tail."""
    if session.hwp is None or session.target_document is None:
        raise RuntimeError("수합 대상 문서가 열려 있지 않습니다.")
    session._diagnostic_comparison = {}
    session._stage("target_activation")
    session._require_target_bridge(
        operation=f"{question_id} 트랜잭션 시작 전"
    )
    session.hwp.move_to_end()
    transaction_start = session.hwp.current_position()
    controls_before = session._target_control_totals
    if controls_before is None:
        controls_before = session._transfer_control_summary()
        session._target_control_totals = controls_before

    try:
        return session._append_copied_block_core(
            question_id,
            expected_controls,
            expected_text,
            child_fields,
        )
    except Exception as original_error:
        category = session._diagnostic_comparison.get("classification", "UNASSESSED")
        if category == "MATCH":
            category = "D"
        session._diag("attempt_error", stage=session._diagnostic_stage,
                   classification=category, error=safe_error(original_error), success=False)
        session._diag("rollback", status="started", rollback=True)
        # Any failure after the first BreakPara/Paste/QID mutation must
        # leave the target exactly as it was before this question. This
        # also covers failures outside the explicit integrity comparison,
        # such as a delayed Paste, empty selected text, or field creation.
        try:
            session._require_target_bridge(
                operation=f"{question_id} 실패분 되돌리기 전"
            )
            session.hwp.move_to_end()
            transaction_end = session.hwp.current_position()
            if transaction_end < transaction_start:
                raise RuntimeError(
                    "문서 끝이 문항 수합 시작점보다 앞으로 이동했습니다: "
                    f"start={transaction_start!r}, end={transaction_end!r}"
                )
            if transaction_end > transaction_start:
                session.hwp.select_position_range(
                    transaction_start, transaction_end
                )
                session.hwp.delete_selection()
            session.hwp.move_to_end()
            restored_end = session.hwp.current_position()
            if restored_end != transaction_start:
                raise RuntimeError(
                    "문항 텍스트 영역이 원래 끝 위치로 돌아오지 않았습니다: "
                    f"before={transaction_start!r}, after={restored_end!r}"
                )
            field_exists = getattr(
                getattr(session.hwp, "automation", None),
                "FieldExist",
                None,
            )
            if callable(field_exists):
                residue_fields = [
                    field_name
                    for field_name in (
                        question_field(question_id),
                        *(name for name, _paragraph in child_fields),
                    )
                    if bool(field_exists(field_name))
                ]
                if residue_fields:
                    raise RuntimeError(
                        "실패한 문항의 내부 ID가 남았습니다: "
                        + ", ".join(residue_fields)
                    )
            rolled_back = session._transfer_control_summary()
            movable_ids = {"eqed", "gso", "tbl"}
            movable_before = {
                key: int(value)
                for key, value in controls_before.items()
                if key.split(":", 1)[0] in movable_ids
            }
            movable_after = {
                key: int(value)
                for key, value in rolled_back.items()
                if key.split(":", 1)[0] in movable_ids
            }
            if movable_after != movable_before:
                raise RuntimeError(
                    "네이티브 개체 수가 원래 상태로 돌아오지 않았습니다: "
                    f"before={movable_before!r}, after={movable_after!r}"
                )
            session._target_control_totals = rolled_back
            session._diag("rollback", status="verified", rollback=True)
        except Exception as rollback_error:
            session._diag("rollback", status="failed", rollback=True, error=safe_error(rollback_error))
            raise RuntimeError(
                f"{question_id} 수합 실패 후 대상 문서를 완전히 되돌리지 "
                f"못했습니다: {rollback_error}"
            ) from original_error
        raise


def append_copied_block_core(
    session: LiveImportSession,
    question_id: str,
    expected_controls: dict[str, int] | None = None,
    expected_text: str | None = None,
    child_fields: tuple[tuple[str, int], ...] = (),
    *,
    field_manager_factory,
    paragraph_reader,
) -> dict[str, int]:
    """Paste a copied block and anchor QID to its real first paragraph.

    A whole-paragraph HWP paste can insert the copied first paragraph
    after the empty paragraph that contains the caret.  Creating the QID
    before Paste therefore produced a numbered empty paragraph while the
    source number remained on the actual question.  Paste first, remove
    only the artificial leading empty paragraph, then create the field on
    the first non-empty pasted paragraph.
    """
    if session.hwp is None or session.target_document is None:
        raise RuntimeError("수합 대상 문서가 열려 있지 않습니다.")
    session.hwp.activate_document(session.target_document)
    session.hwp.require_active_document(
        session.target_document, operation=f"{question_id} 붙여넣기 전"
    )
    session.hwp.move_to_end()
    session.hwp.require_active_document(
        session.target_document, operation=f"{question_id} 붙여넣기 위치 이동 후"
    )
    rollback_start = session.hwp.current_position()
    session.hwp.break_paragraph()
    insertion_start = session.hwp.current_position()
    controls_before = session._target_control_totals
    if controls_before is None:
        controls_before = session._transfer_control_summary()
    session._stage("target_paste")
    paste_and_wait = getattr(session.hwp, "paste_and_wait_for_controls", None)
    if callable(paste_and_wait):
        controls_after = paste_and_wait(
            session.target_document,
            controls_before,
            expected_controls or {},
        )
    else:
        session.hwp.paste()
        controls_after = session._transfer_control_summary()
    session.hwp.require_active_document(
        session.target_document, operation=f"{question_id} 붙여넣기 후"
    )
    pasted_end = session.hwp.current_position()
    movable_ids = {"eqed", "gso", "tbl"}
    control_delta = {
        key: int(controls_after.get(key, 0))
        - int(controls_before.get(key, 0))
        for key in sorted(set(controls_before) | set(controls_after))
        if key.split(":", 1)[0] in movable_ids
        if int(controls_after.get(key, 0))
        != int(controls_before.get(key, 0))
    }
    negative_delta = {
        key: count for key, count in control_delta.items() if count < 0
    }
    session._stage("integrity_verification")
    normalize = lambda value: re.sub(r"\s+", " ", value).strip()
    pasted_text = ""
    text_matches = True
    if expected_text is not None:
        session.hwp.select_position_range(insertion_start, pasted_end)
        pasted_text = session.hwp.selected_plain_text()
        session.hwp.automation.Run("Cancel")
        text_matches = normalize(expected_text) == normalize(pasted_text)

    session._diagnostic_comparison = comparison(
        expected_controls, control_delta, text_matches if expected_text is not None else None)
    session._diag("paste_verification", **session._diagnostic_comparison,
               source_selected_text=None if expected_text is None else fingerprint(expected_text),
               target_pasted_text=None if expected_text is None else fingerprint(pasted_text),
               insertion_start=insertion_start, pasted_end=pasted_end,
               controls_before=safe_summary(controls_before), controls_after=safe_summary(controls_after))
    if (
        (
            expected_controls is not None
            and control_delta != expected_controls
        )
        or bool(negative_delta)
        or not text_matches
    ):
        # Never leave a partially pasted question in the target. The
        # caller reselects and recopies the source block before retrying.
        session._require_target_bridge(
            operation=f"{question_id} 불완전 붙여넣기 되돌리기 전"
        )
        session._diag("rollback", status="started", rollback=True)
        session.hwp.select_position_range(rollback_start, pasted_end)
        session.hwp.delete_selection()
        rolled_back = session._transfer_control_summary()
        movable_ids = {"eqed", "gso", "tbl"}
        movable_before = {
            key: int(value)
            for key, value in controls_before.items()
            if key.split(":", 1)[0] in movable_ids
        }
        movable_after_rollback = {
            key: int(value)
            for key, value in rolled_back.items()
            if key.split(":", 1)[0] in movable_ids
        }
        if movable_after_rollback != movable_before:
            raise RuntimeError(
                f"{question_id} 불완전 붙여넣기 되돌리기 검증 실패: "
                f"before={movable_before!r}, after={movable_after_rollback!r}"
            )
        session._target_control_totals = rolled_back
        missing = {
            key: {
                "source": int(expected_controls.get(key, 0)),
                "pasted": int(control_delta.get(key, 0)),
            }
            for key in sorted(set(expected_controls) | set(control_delta))
            if int(control_delta.get(key, 0))
            != int(expected_controls.get(key, 0))
        } if expected_controls is not None else {}
        if not text_matches:
            missing["text"] = {
                "source": len(normalize(expected_text or "")),
                "pasted": len(normalize(pasted_text)),
            }
        if negative_delta:
            missing["document"] = {
                "source": "target",
                "pasted": negative_delta,
            }
        raise NativeBlockIntegrityError(
            f"{question_id} 네이티브 개체 복사가 불완전합니다: {missing}"
        )
    session._target_control_totals = controls_after

    session._stage("cleanup")
    first_content_paragraph = _first_pasted_content_paragraph(
        session.hwp,
        paragraph_reader,
        insertion_start,
        pasted_end,
    )
    if first_content_paragraph is None:
        raise RuntimeError(
            f"{question_id} 붙여넣기 후 실제 문제 문단을 찾지 못했습니다."
        )
    preserve_leading_paragraph = False
    if first_content_paragraph > insertion_start[1]:
        cleanup_end = (insertion_start[0], first_content_paragraph, 0)
        safe_delete = getattr(
            session.hwp, "delete_range_preserving_native_controls", None
        )
        if callable(safe_delete):
            deleted = safe_delete(
                insertion_start,
                cleanup_end,
                controls_after,
            )
            preserve_leading_paragraph = not deleted
        else:
            session.hwp.select_position_range(insertion_start, cleanup_end)
            session.hwp.delete_selection()

    session._stage("qid_creation")
    manager = field_manager_factory(session.hwp)
    qid_position = (
        (insertion_start[0], first_content_paragraph, 0)
        if preserve_leading_paragraph
        else insertion_start
    )
    session.hwp.set_position(qid_position)
    manager.create_at_current_position(question_field(question_id))
    for field_name, relative_paragraph in child_fields:
        target_paragraph = qid_position[1] + relative_paragraph
        if not session.hwp.automation.SetPos(0, target_paragraph, 0):
            raise RuntimeError(
                f"{question_id} 하위 QID 위치를 찾지 못했습니다: "
                f"paragraph={target_paragraph}"
            )
        manager.create_at_current_position(field_name)
    # The QID and visible question number must live on the same paragraph
    # as the copied question text.
    qid_paragraph = manager.anchor_position(question_field(question_id))[1]
    text = paragraph_reader(session.hwp, qid_paragraph)
    if text is None or not text.strip():
        raise RuntimeError(
            f"{question_id} 내부 ID가 빈 문단에 생성되었습니다."
        )
    for _ in range(session.document_type.separator_blank_paragraphs):
        session.hwp.move_to_end()
        session.hwp.break_paragraph()
    final_controls = session._transfer_control_summary()
    movable_ids = {"eqed", "gso", "tbl"}
    movable_after_paste = {
        key: int(value)
        for key, value in controls_after.items()
        if key.split(":", 1)[0] in movable_ids
    }
    movable_after_cleanup = {
        key: int(value)
        for key, value in final_controls.items()
        if key.split(":", 1)[0] in movable_ids
    }
    session._stage("cleanup_verification")
    session._diag("cleanup_verification", controls_after=safe_summary(movable_after_cleanup),
               structural_after=structural(movable_after_cleanup),
               controls_match=movable_after_cleanup == movable_after_paste)
    if movable_after_cleanup != movable_after_paste:
        raise NativeBlockIntegrityError(
            f"{question_id} 번호·내부 ID 정리 중 네이티브 개체가 "
            "변경되었습니다: "
            f"paste={movable_after_paste!r}, "
            f"cleanup={movable_after_cleanup!r}"
        )
    session._target_control_totals = final_controls
    session._diag("attempt_result", success=True, rollback=False)
    return control_delta


def append_manual_placeholder(
    session: LiveImportSession,
    question_id: str,
    *,
    child_fields: tuple[tuple[str, int], ...] = (),
    field_manager_factory,
) -> None:
    """Create an empty, numbered-safe slot after a recoverable copy failure."""
    if session.hwp is None or session.target_document is None:
        raise RuntimeError("수합 대상 문서가 열려 있지 않습니다.")
    session.hwp.activate_document(session.target_document)
    session.hwp.require_active_document(
        session.target_document, operation=f"{question_id} 빈 자리 생성 전"
    )
    session.hwp.move_to_end()
    session.hwp.break_paragraph()
    manager = field_manager_factory(session.hwp)
    manager.create_at_current_position(question_field(question_id))
    for field_name, _relative_paragraph in child_fields:
        # Keep every child field on its own paragraph.  Placing the first child
        # at the exact same empty caret position as the parent GID allowed HWP
        # to collapse/drop that field, which later stopped renumbering.
        session.hwp.break_paragraph()
        manager.create_at_current_position(field_name)
    for _ in range(session.document_type.separator_blank_paragraphs):
        session.hwp.move_to_end()
        session.hwp.break_paragraph()
    session._target_control_totals = session._transfer_control_summary()
    session._progress(
        f"{question_id} 내용은 비우고 번호·내부 ID 자리만 만들었습니다."
    )


def transfer_import_unit(
    session: LiveImportSession,
    next_id: str,
    source: str | Path,
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    expected_controls: dict[str, int],
    child_field_specs: tuple[tuple[str, int], ...],
) -> NativeTransferResult:
    selected_position = (0, 0, 0, 0, 0, 0)
    selected_text = ""
    delta: dict[str, int] | None = None
    last_error: Exception | None = None
    object_text = ", ".join(
        f"{key} {count}개" for key, count in expected_controls.items()
    ) or "별도 개체 없음"
    session._progress(f"{next_id} 원본 개체 확인: {object_text}")
    for attempt in range(1, 4):
        if session.diagnostic is not None:
            session.diagnostic.context["attempt"] = attempt
        session._diagnostic_comparison = {}
        session._stage("source_selection")
        try:
            session._require_non_target_bridge(
                session.source_document,
                operation=f"{next_id} 원본 블록 선택 전",
            )
            selected_position = session.hwp.select_position_range(start, end)
            selected_text = session.hwp.selected_plain_text()
            session._diag("source_selection", selected_position=selected_position,
                       source_selected_text=fingerprint(selected_text))
            if not selected_text.strip():
                raise ValueError(
                    f"{next_id} 범위에서 문항 내용을 읽지 못했습니다."
                )
            session._progress(
                f"{Path(source).name} → {next_id} 네이티브 문항 블록 복사"
                + (f" (재시도 {attempt}/3)" if attempt > 1 else "")
            )
            # GetTextFile(saveblock) can move the native object focus
            # on complex HWP blocks. Re-select the exact verified
            # range and issue Copy immediately afterward.
            session._stage("copy")
            selected_position = session.hwp.select_position_range(start, end)
            session.hwp.copy_selection()
            session._diag("copy_selection", selected_position=selected_position)
            session._diag("copy_returned")
            session.hwp.visual_step(0.02 if session.hwp.visible else 0.0)
        except Exception as error:
            # The target was not touched, so this source-side failure
            # can be retried and eventually represented by a blank
            # slot without risking earlier questions.
            last_error = error
            session._diag("attempt_result", success=False, rollback=False,
                       stage=session._diagnostic_stage, error=safe_error(error))
            session._progress(
                f"{next_id} 원본 범위 읽기 실패: {error} "
                f"(재시도 {attempt}/3)"
            )
            time.sleep(0.12 * attempt)
            continue
        try:
            delta = session._append_copied_block(
                next_id,
                expected_controls,
                expected_text=selected_text,
                child_fields=child_field_specs,
            )
            last_error = None
            break
        except NativeBlockIntegrityError as error:
            last_error = error
            session._diag("attempt_result", success=False, rollback=True,
                       stage=session._diagnostic_stage, error=safe_error(error))
            session._progress(f"{error}: 원본에서 다시 복사합니다.")
            time.sleep(0.12 * attempt)
    manual_required = last_error is not None or delta is None
    import_error = ""
    if manual_required:
        import_error = str(last_error or "알 수 없는 네이티브 복사 오류")
        session._stage("placeholder")
        session._append_manual_placeholder(
            next_id, child_fields=child_field_specs
        )
        delta = {}
        session._progress(
            f"[직접 입력 필요] {next_id}만 비워 두고 다음 문항 수합을 계속합니다."
        )
    session._diag("question_result", success=not manual_required, placeholder=manual_required)
    return NativeTransferResult(selected_position, selected_text, delta, manual_required, import_error)
