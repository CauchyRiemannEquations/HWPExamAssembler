from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from collections.abc import Callable

from app.hwp.application import HwpApplication, HwpAutomationError
from app.hwp.fields import QuestionFieldManager, question_field
from app.hwp.numbering import (
    NumberingStyleProfile,
    capture_numbering_style,
    numbering_styles_visually_equivalent,
)
from app.hwp.reorder import (
    ReorderAudit,
    apply_permutation,
    create_timestamped_backup,
    numbered_field_targets,
    renumber_visible_questions,
)


@dataclass(frozen=True)
class ReorderTransactionAudit:
    document: str
    backup: str
    before_sha256: str
    after_sha256: str
    reorder: ReorderAudit
    number_mapping: dict[str, int]
    controls_before: dict[str, int]
    controls_after: dict[str, int]
    question_controls_before: dict[str, dict[str, int]]
    question_controls_after: dict[str, dict[str, int]]
    deleted_question_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "document": self.document,
            "backup": self.backup,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "reorder": self.reorder.to_dict(),
            "number_mapping": self.number_mapping,
            "controls_before": self.controls_before,
            "controls_after": self.controls_after,
            "question_controls_before": self.question_controls_before,
            "question_controls_after": self.question_controls_after,
            "deleted_question_ids": list(self.deleted_question_ids),
        }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def apply_reorder_transaction(
    hwp: HwpApplication,
    document: str | Path,
    requested_order: tuple[str, ...],
    numbered_question_ids: tuple[str, ...],
    *,
    numbering_style: NumberingStyleProfile,
    backup_dir: str | Path,
    work_dir: str | Path,
    on_progress: Callable[[str], None] | None = None,
    step_delay: float = 0.0,
    allow_deletions: bool = False,
    expected_sha256: str | None = None,
) -> ReorderTransactionAudit:
    """Apply reorder and numbering atomically to an existing editable copy.

    The user's document is left untouched until a separate working copy has
    been saved, reopened, and verified. A byte-for-byte backup is created
    before Automation starts.
    """
    destination = Path(document).resolve()
    if not destination.is_file():
        raise FileNotFoundError(destination)
    if not os.access(destination, os.W_OK):
        raise PermissionError(f"Document is not writable: {destination}")
    before_hash = sha256(destination)
    if expected_sha256 is not None and before_hash != expected_sha256:
        raise ValueError("편집을 시작한 뒤 파일이 변경되었습니다. 최신 파일을 다시 읽어 주세요.")
    backup = create_timestamped_backup(destination, backup_dir)
    if sha256(backup) != before_hash:
        raise OSError(f"Backup hash mismatch: {backup}")

    runtime_dir = Path(work_dir).resolve() / f"reorder-{uuid.uuid4().hex}"
    runtime_dir.mkdir(parents=True)
    working = runtime_dir / destination.name
    shutil.copyfile(destination, working)
    os.chmod(working, 0o666)

    try:
        if on_progress is not None:
            on_progress(f"작업 사본 열기: {destination.name}")
        hwp.open(working)
        hwp.visual_step(step_delay)
        manager = QuestionFieldManager(hwp)
        initial_order = manager.ordered_question_ids()
        if not requested_order:
            raise ValueError("시험지에는 문항이 한 개 이상 있어야 합니다.")
        if len(requested_order) != len(set(requested_order)):
            raise ValueError("요청 순서에 중복 문항이 있습니다.")
        unknown = sorted(set(requested_order) - set(initial_order))
        if unknown:
            raise ValueError(f"문서에 없는 문항입니다: {unknown!r}")
        deleted_question_ids = tuple(
            question_id
            for question_id in initial_order
            if question_id not in requested_order
        )
        if deleted_question_ids and not allow_deletions:
            raise ValueError("문항 삭제가 허용되지 않은 작업입니다.")
        controls_before = hwp.control_summary()
        question_controls_before = manager.question_control_summaries()
        deleted_owned_fields: dict[str, tuple[str, ...]] = {}
        for question_id in reversed(deleted_question_ids):
            order_before_delete = manager.ordered_question_ids()
            manager.select_question(question_id)
            selected_fields = manager.field_names(selection_only=True)
            expected_fields = manager.owned_field_names(question_id)
            deleted_owned_fields[question_id] = expected_fields
            if selected_fields != expected_fields:
                raise HwpAutomationError(
                    f"Unsafe fields while deleting {question_id}: "
                    f"actual={selected_fields!r}, expected={expected_fields!r}"
                )
            if on_progress is not None:
                on_progress(f"{question_id} 문항 블록 삭제")
            hwp.visual_step(step_delay)
            hwp.delete_selection()
            manager.invalidate_cache()
            expected_after_delete = tuple(
                item for item in order_before_delete if item != question_id
            )
            if manager.ordered_question_ids() != expected_after_delete:
                raise HwpAutomationError(
                    f"문항 삭제 후 순서 검증 실패: {question_id}"
                )
        reorder = apply_permutation(
            manager,
            requested_order,
            on_progress=on_progress,
            step_delay=step_delay,
        )
        number_mapping = renumber_visible_questions(
            manager,
            numbered_question_ids,
            style=numbering_style,
            on_progress=on_progress,
            step_delay=step_delay,
        )
        numbered_targets = numbered_field_targets(manager, numbered_question_ids)
        if on_progress is not None:
            on_progress("변경 내용 저장")
        # FileSave follows the user's default format and can open a modal
        # conversion notice. Save explicitly to a fresh path, then verify
        # and publish exactly that file.
        saved_copy = runtime_dir / "saved" / working.name
        saved_copy.parent.mkdir()
        hwp.save_exam_copy(saved_copy)
        hwp.reset_documents()

        if on_progress is not None:
            on_progress("저장된 HWP 다시 열어 검증")
        hwp.open(saved_copy)
        manager = QuestionFieldManager(hwp)
        reopened_order = manager.ordered_question_ids()
        controls_after = hwp.control_summary()
        question_controls_after = manager.question_control_summaries()
        if reopened_order != requested_order:
            raise HwpAutomationError(
                f"Reopened order mismatch: {reopened_order!r}; "
                f"expected={requested_order!r}"
            )
        for display_id, field_name in numbered_targets:
            hwp.set_position(manager.position(field_name))
            actual_style = capture_numbering_style(hwp)
            if not numbering_styles_visually_equivalent(
                numbering_style, actual_style
            ):
                raise HwpAutomationError(
                    f"Numbering style mismatch after reopen: {display_id}"
                )
        expected_question_controls = {
            question_id: question_controls_before[question_id]
            for question_id in requested_order
        }
        if question_controls_after != expected_question_controls:
            raise HwpAutomationError(
                "Native control ownership changed between question blocks"
            )
        if not deleted_question_ids and controls_after != controls_before:
            raise HwpAutomationError(
                "Native control inventory changed during reorder transaction"
            )
        if deleted_question_ids:
            deleted_controls: dict[str, int] = {}
            for question_id in deleted_question_ids:
                for key, count in question_controls_before[question_id].items():
                    deleted_controls[key] = deleted_controls.get(key, 0) + int(count)
            for key in set(controls_before) | set(controls_after) | set(deleted_controls):
                control_id = key.split(":", 1)[0]
                if control_id not in {"eqed", "gso", "tbl"}:
                    continue
                expected_count = int(controls_before.get(key, 0)) - int(
                    deleted_controls.get(key, 0)
                )
                if int(controls_after.get(key, 0)) != expected_count:
                    raise HwpAutomationError(
                        f"문항 삭제 후 네이티브 개체 수 검증 실패: {key}"
                    )
            for question_id in deleted_question_ids:
                for field_name in deleted_owned_fields[question_id]:
                    if hwp.automation.FieldExist(field_name):
                        raise HwpAutomationError(
                            f"삭제한 문항의 내부 ID가 남아 있습니다: {field_name}"
                        )
        hwp.reset_documents()
    except Exception:
        try:
            hwp.reset_documents()
        except Exception:
            pass
        raise

    # Stage on the destination volume so a failed publication leaves the
    # previous editable document intact, including when a user reopened it.
    fd, temporary_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
    os.close(fd)
    staged = Path(temporary_name)
    try:
        shutil.copyfile(saved_copy, staged)
        after_hash = sha256(staged)
        if sha256(destination) != before_hash:
            raise ValueError("작업 중 원래 파일이 변경되어 저장하지 않았습니다. 최신 파일을 다시 읽어 주세요.")
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    if on_progress is not None:
        # Keep progress text safe for Windows consoles still using CP949.
        on_progress("검증 완료 - 결과 파일 게시")
    return ReorderTransactionAudit(
        document=str(destination),
        backup=str(backup),
        before_sha256=before_hash,
        after_sha256=after_hash,
        reorder=reorder,
        number_mapping=number_mapping,
        controls_before=controls_before,
        controls_after=controls_after,
        question_controls_before=question_controls_before,
        question_controls_after=question_controls_after,
        deleted_question_ids=deleted_question_ids,
    )
