from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from collections.abc import Callable
from pathlib import Path
import shutil

from app.hwp.application import HwpAutomationError
from app.hwp.fields import MoveAudit, QuestionFieldManager, question_field
from app.hwp.numbering import NumberingStyleProfile, apply_numbering_style


@dataclass(frozen=True)
class ReorderAudit:
    order_before: tuple[str, ...]
    requested_order: tuple[str, ...]
    order_after: tuple[str, ...]
    moves: tuple[MoveAudit, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "order_before": list(self.order_before),
            "requested_order": list(self.requested_order),
            "order_after": list(self.order_after),
            "moves": [asdict(move) for move in self.moves],
        }


def validate_permutation(
    current_order: tuple[str, ...], requested_order: tuple[str, ...]
) -> None:
    if not current_order:
        raise ValueError("The document contains no question ID fields")
    if len(requested_order) != len(set(requested_order)):
        raise ValueError("Requested order contains duplicate question IDs")
    current = set(current_order)
    requested = set(requested_order)
    missing = sorted(current - requested)
    unknown = sorted(requested - current)
    if missing or unknown or len(current_order) != len(requested_order):
        raise ValueError(
            "Requested order must be an exact permutation; "
            f"missing={missing!r}, unknown={unknown!r}"
        )


def create_timestamped_backup(
    document: str | Path,
    backup_dir: str | Path,
    *,
    now: datetime | None = None,
) -> Path:
    source = Path(document).resolve()
    destination_dir = Path(backup_dir).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S_%f")
    destination = destination_dir / f"{source.stem}_{stamp}{source.suffix}"
    shutil.copy2(source, destination)
    if destination.stat().st_size != source.stat().st_size:
        raise OSError(f"Backup size mismatch: {destination}")
    return destination


def apply_permutation(
    manager: QuestionFieldManager,
    requested_order: tuple[str, ...],
    *,
    on_progress: Callable[[str], None] | None = None,
    step_delay: float = 0.0,
) -> ReorderAudit:
    order_before = manager.ordered_question_ids()
    validate_permutation(order_before, requested_order)
    moves: list[MoveAudit] = []

    # Build the requested prefix from left to right. Each operation moves one
    # complete native block (including its invisible QID field) before the
    # block currently occupying this position.
    for index, question_id in enumerate(requested_order):
        current = manager.ordered_question_ids()
        if current[index] == question_id:
            continue
        moves.append(
            manager.move_before(
                question_id,
                current[index],
                on_progress=on_progress,
                step_delay=step_delay,
            )
        )

    order_after = manager.ordered_question_ids()
    if order_after != requested_order:
        raise HwpAutomationError(
            f"Permutation verification failed: {order_after!r}; "
            f"expected={requested_order!r}"
        )
    return ReorderAudit(
        order_before=order_before,
        requested_order=requested_order,
        order_after=order_after,
        moves=tuple(moves),
    )


def set_visible_field_number(
    manager: QuestionFieldManager,
    field_name: str,
    number: int,
    *,
    style: NumberingStyleProfile | None = None,
) -> None:
    if number < 1:
        raise ValueError("Visible question numbers must be positive")
    hwp = manager.hwp
    position = manager.position(field_name)
    hwp.set_position(position)
    if style is not None:
        apply_numbering_style(hwp, style, number=number)
        return
    parameter = hwp.automation.HParameterSet.HParaShape
    hwp.automation.HAction.GetDefault("ParagraphShape", parameter.HSet)
    parameter.HeadingType = 2
    parameter.Level = 0
    parameter.Numbering.NewList = 1
    parameter.Numbering.StartNumber = number
    parameter.Numbering.StartNumber0 = number
    executed = hwp.automation.HAction.Execute(
        "ParagraphShape", parameter.HSet
    )
    if not executed:
        raise HwpAutomationError(
            f"Cannot set visible number {number} for {field_name}"
        )


def set_visible_question_number(
    manager: QuestionFieldManager,
    question_id: str,
    number: int,
    *,
    style: NumberingStyleProfile | None = None,
) -> None:
    set_visible_field_number(
        manager, question_field(question_id), number, style=style
    )


def numbered_field_targets(
    manager: QuestionFieldManager,
    question_ids: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    current = manager.ordered_question_ids()
    unknown = sorted(set(question_ids) - set(current))
    if unknown:
        raise ValueError(f"Cannot renumber unknown question IDs: {unknown!r}")
    targets: list[tuple[str, str]] = []
    for question_id in question_ids:
        children = manager.child_question_fields(question_id, kind="objective")
        if children:
            targets.extend(
                (child.question_id, child.field_name) for child in children
            )
        else:
            targets.append((question_id, question_field(question_id)))
    return tuple(targets)


def renumber_visible_questions(
    manager: QuestionFieldManager,
    question_ids: tuple[str, ...],
    *,
    style: NumberingStyleProfile | None = None,
    on_progress: Callable[[str], None] | None = None,
    step_delay: float = 0.0,
) -> dict[str, int]:
    targets = numbered_field_targets(manager, question_ids)
    mapping = {
        display_id: number
        for number, (display_id, _field_name) in enumerate(targets, start=1)
    }
    for number, (display_id, field_name) in enumerate(targets, start=1):
        if on_progress is not None:
            on_progress(f"{display_id} 화면 번호를 {number}번으로 갱신")
        set_visible_field_number(manager, field_name, number, style=style)
        manager.hwp.visual_step(step_delay)
    # A common passage label such as [16~17] is part of the native block, not
    # a paragraph number.  Keep it synchronized with the child QIDs after the
    # permutation has established their new visible numbers.
    for group_id in question_ids:
        children = manager.child_question_fields(group_id, kind="objective")
        if not children:
            continue
        numbers = tuple(mapping[item.question_id] for item in children)
        updated = manager.update_group_objective_label(group_id, numbers)
        if on_progress is not None:
            if updated is None:
                on_progress(
                    f"{group_id} 묶음 번호 표지를 자동 판별하지 못해 원문 유지"
                )
            elif updated[0] != updated[1]:
                on_progress(
                    f"{group_id} 묶음 표지 {updated[0]} → {updated[1]} 갱신"
                )
        manager.hwp.visual_step(step_delay)
    return mapping
