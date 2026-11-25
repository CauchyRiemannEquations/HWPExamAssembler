from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable
import re
import time
import pythoncom

from app.hwp.application import HwpApplication, HwpAutomationError
from app.hwp.control_layout import ControlLayout
from project import ImportManifest


FIELD_PREFIX = "__HWPQ__"
FIXED_FIELD_PREFIX = "__HWP_FIXED__"
CHILD_FIELD_PREFIX = "__HWPC__"
TERMINAL_FIELD = f"{FIELD_PREFIX}DOCUMENT_END"
CLICK_HERE_OPTION = 2
SELECTION_OPTION = 4
GROUP_OBJECTIVE_LABEL = re.compile(
    r"^(?P<leading>\s*)\[\s*"
    r"(?P<objective>\d{1,3}(?:\s*[-~～]\s*\d{1,3})?)"
    r"(?P<suffix>\s*,\s*(?:서답형|서술형)\s*\d{1,3})?\s*\]"
)


class QuestionTransferMismatch(HwpAutomationError):
    """Native Cut/Paste produced a different question block in the working copy."""


def group_objective_label_replacement(
    text: str, numbers: tuple[int, ...]
) -> tuple[str, str] | None:
    if not numbers:
        return None
    match = GROUP_OBJECTIVE_LABEL.match(text)
    if match is None:
        return None
    old_label = match.group(0)[len(match.group("leading")) :]
    objective = (
        str(numbers[0])
        if len(numbers) == 1
        else f"{numbers[0]}~{numbers[-1]}"
    )
    suffix = match.group("suffix") or ""
    return old_label, f"[{objective}{suffix}]"


def question_field(question_id: str) -> str:
    return f"{FIELD_PREFIX}{question_id}"


def parse_question_field(name: str) -> str | None:
    if not name.startswith(FIELD_PREFIX) or name == TERMINAL_FIELD:
        return None
    value = name[len(FIELD_PREFIX) :]
    return value or None


@dataclass(frozen=True)
class ChildQuestionField:
    group_id: str
    question_id: str
    kind: str
    field_name: str


def child_question_field(group_id: str, question_id: str, kind: str) -> str:
    kind_code = {"objective": "O", "written": "W"}.get(kind)
    if kind_code is None:
        raise ValueError(f"Unknown child question kind: {kind}")
    return f"{CHILD_FIELD_PREFIX}{group_id}__{question_id}__{kind_code}"


def parse_child_question_field(name: str) -> ChildQuestionField | None:
    if not name.startswith(CHILD_FIELD_PREFIX):
        return None
    values = name[len(CHILD_FIELD_PREFIX) :].split("__")
    if len(values) != 3:
        return None
    group_id, question_id, kind_code = values
    kind = {"O": "objective", "W": "written"}.get(kind_code)
    if not group_id or not question_id or kind is None:
        return None
    return ChildQuestionField(
        group_id=group_id,
        question_id=question_id,
        kind=kind,
        field_name=name,
    )


@dataclass(frozen=True)
class MoveAudit:
    question_id: str
    destination_id: str
    order_before: tuple[str, ...]
    order_after: tuple[str, ...]
    selected_fields: tuple[str, ...]
    selected_text_length: int


class QuestionFieldManager:
    def __init__(self, hwp: HwpApplication) -> None:
        self.hwp = hwp
        self._field_names_cache: tuple[str, ...] | None = None
        self._anchor_cache: dict[str, tuple[int, int, int]] | None = None
        self._boundary_cache: dict[str, tuple[int, int, int]] | None = None
        self._control_layout: ControlLayout | None = None
        self._next_boundaries: dict[str, str] | None = None

    def invalidate_cache(self) -> None:
        """Forget document-order data after a structural edit."""
        self._field_names_cache = None
        self._anchor_cache = None
        self._boundary_cache = None
        self._control_layout = None
        self._next_boundaries = None

    def field_names(self, *, selection_only: bool = False) -> tuple[str, ...]:
        if not selection_only and self._field_names_cache is not None:
            return self._field_names_cache
        option = CLICK_HERE_OPTION
        if selection_only:
            option |= SELECTION_OPTION
        value = self.hwp.automation.GetFieldList(0, option)
        result = () if not value else tuple(str(value).split("\x02"))
        if not selection_only:
            self._field_names_cache = result
        return result

    def ordered_question_ids(self) -> tuple[str, ...]:
        result: list[str] = []
        for name in self.field_names():
            question_id = parse_question_field(name)
            if question_id is not None:
                result.append(question_id)
        return tuple(result)

    def block_layout(self) -> tuple[str, ...]:
        """Question IDs and immovable section ends in native field order."""
        layout = []
        for name in self.field_names():
            if name.startswith(FIXED_FIELD_PREFIX):
                layout.append(name)
            else:
                question_id = parse_question_field(name)
                if question_id is not None:
                    layout.append(question_id)
        return tuple(layout)

    def child_question_fields(
        self,
        group_id: str | None = None,
        *,
        kind: str | None = None,
    ) -> tuple[ChildQuestionField, ...]:
        result: list[ChildQuestionField] = []
        for name in self.field_names():
            child = parse_child_question_field(name)
            if child is None:
                continue
            if group_id is not None and child.group_id != group_id:
                continue
            if kind is not None and child.kind != kind:
                continue
            result.append(child)
        return tuple(result)

    def owned_field_names(self, question_id: str) -> tuple[str, ...]:
        main = question_field(question_id)
        result: list[str] = []
        for name in self.field_names():
            child = parse_child_question_field(name)
            if name == main or (
                child is not None and child.group_id == question_id
            ):
                result.append(name)
        return tuple(result)

    def create_at_current_position(self, name: str) -> None:
        if self.hwp.automation.FieldExist(name):
            raise HwpAutomationError(f"Field already exists: {name}")
        created = self.hwp.automation.CreateField("", "", name)
        if not created or not self.hwp.automation.FieldExist(name):
            raise HwpAutomationError(f"Cannot create field: {name}")
        self.invalidate_cache()

    def create_at_paragraph_marker(self, marker: str, name: str) -> None:
        self.hwp.automation.Run("MoveDocBegin")
        selected = self.hwp.find_text(marker)
        self.hwp.set_position((selected[0], selected[1], 0))
        self.create_at_current_position(name)

    def insert_manifest_boundaries(self, manifest: ImportManifest) -> None:
        self.hwp.move_to_end()
        self.create_at_current_position(TERMINAL_FIELD)
        for question in reversed(manifest.questions):
            self.create_at_paragraph_marker(
                question.start_marker, question_field(question.id)
            )

    def _move_to_field(self, name: str) -> None:
        if not self.hwp.automation.MoveToField(name, False, True, False):
            raise HwpAutomationError(f"Cannot move to field: {name}")

    def focus_field(self, name: str) -> None:
        """Position the caret at a named field without extending a selection."""
        self.hwp.automation.Run("Cancel")
        self._move_to_field(name)

    def position(self, name: str) -> tuple[int, int, int]:
        self._move_to_field(name)
        return self.hwp.current_position()

    def anchor_position(self, name: str) -> tuple[int, int, int]:
        try:
            return self._top_level_anchors()[name]
        except KeyError as error:
            raise HwpAutomationError(f"Unknown body field: {name}") from error

    def _top_level_anchors(self) -> dict[str, tuple[int, int, int]]:
        """Return the field control's anchor, not an inferred text marker.

        An empty click-here field occupies eight logical character positions in
        HWP.  ``MoveToField`` therefore reports the caret after that control.
        A floating object anchored immediately before the field would otherwise
        be assigned to the preceding question.  The top-level click-here
        controls and ``GetFieldList`` are both in document order, so pairing
        them gives the true zero-width boundary used for native block moves.
        """
        if self._anchor_cache is None:
            names = self.field_names()
            if self._control_layout is None:
                self._control_layout = ControlLayout.read(self.hwp, inventory=False)
            anchors = sorted(self._control_layout.field_anchors)
            if len(names) > len(anchors):
                # GetFieldList includes named fields inside tables and running
                # headers. They have another list ID and cannot bound a body block.
                caret = self.hwp.current_position()
                try:
                    # Ask the field at each real body anchor for its name. This
                    # avoids looking up and scrolling to every header/table
                    # field too. Keep the original lookup for unusual fields
                    # where the caret cannot enter the click-here control.
                    body_names = []
                    try:
                        for lst, para, offset in anchors:
                            if not self.hwp.automation.SetPos(lst, para, offset + 8):
                                break
                            body_names.append(self.hwp.automation.GetCurFieldName(0))
                    except Exception:
                        body_names.clear()
                    in_body = set(body_names)
                    ordered_names = tuple(name for name in names if name in in_body)
                    if (len(in_body) == len(anchors) and "" not in in_body
                            and tuple(body_names) == ordered_names):
                        names = ordered_names
                    else:
                        names = tuple(name for name in names if self.position(name)[0] == 0)
                finally:
                    self.hwp.set_position(caret)
            if len(anchors) != len(names):
                raise HwpAutomationError(
                    "Cannot map named click-here fields to top-level controls: "
                    f"names={len(names)}, controls={len(anchors)}"
                )
            self._anchor_cache = dict(zip(names, anchors, strict=True))
        return self._anchor_cache

    def block_boundary_position(self, name: str) -> tuple[int, int, int]:
        """Expand a question boundary over native objects before its ID field.

        HWP may place a newly inserted empty field after a floating object that
        is anchored at the very start of the same paragraph.  Such an object is
        part of the new question, not the preceding one.  We include leading
        picture/equation/table controls for every question. Only a first
        question that actually starts in paragraph zero is kept unexpanded,
        because that paragraph can share template header controls.
        """
        if self._boundary_cache is None:
            anchors = self._top_level_anchors()
            order = self.ordered_question_ids()
            first_question = order[0] if order else None
            boundaries: dict[str, tuple[int, int, int]] = {}
            for field_name, field_anchor in anchors.items():
                question_id = parse_question_field(field_name)
                first_movable = self._control_layout.first_movable.get(field_anchor[:2])
                if (
                    question_id is None
                    or (
                        question_id == first_question
                        and field_anchor[1] == 0
                    )
                    or first_movable is None or first_movable >= field_anchor[2]
                ):
                    boundaries[field_name] = field_anchor
                    continue
                # Starting at the previous paragraph end includes a floating
                # object anchored immediately before the QID field.
                self.hwp.set_position((field_anchor[0], field_anchor[1], 0))
                self.hwp.automation.Run("MovePrevParaEnd")
                previous_end = self.hwp.current_position()
                if previous_end >= field_anchor:
                    raise HwpAutomationError(
                        f"Unsafe previous paragraph boundary for field: {field_name}"
                    )
                boundaries[field_name] = previous_end
            self._boundary_cache = boundaries
        try:
            return self._boundary_cache[name]
        except KeyError as error:
            raise HwpAutomationError(f"Unknown field: {name}") from error

    def next_boundary(self, question_id: str) -> str:
        if self._next_boundaries is None:
            self._next_boundaries = {}
            following = TERMINAL_FIELD
            for name in reversed(self.field_names()):
                qid = parse_question_field(name)
                if qid is not None:
                    self._next_boundaries[qid] = following
                    following = name
                elif name == TERMINAL_FIELD or name.startswith(FIXED_FIELD_PREFIX):
                    following = name
        try:
            return self._next_boundaries[question_id]
        except KeyError as error:
            raise HwpAutomationError(f"Unknown question ID: {question_id}") from error

    def select_between_fields(
        self, start_name: str, end_name: str
    ) -> tuple[int, int, int, int, int, int]:
        start = self.block_boundary_position(start_name)
        end = self.block_boundary_position(end_name)
        if start[0] != end[0] or start >= end:
            raise HwpAutomationError(
                f"Unsafe field range: {start_name}={start!r}, {end_name}={end!r}"
            )
        return self.hwp.select_position_range(start, end)

    def select_question(
        self, question_id: str
    ) -> tuple[int, int, int, int, int, int]:
        return self.select_between_fields(
            question_field(question_id), self.next_boundary(question_id)
        )

    def question_text(self, question_id: str) -> str:
        self.select_question(question_id)
        return self.hwp.selected_plain_text()

    def update_group_objective_label(
        self, group_id: str, numbers: tuple[int, ...]
    ) -> tuple[str, str] | None:
        """Update only a group's leading visible objective-number range.

        The native passage, tables and child questions are not rebuilt.  The
        replacement is accepted only when the exact leading bracket label is
        found inside the GID block and before its first child field.
        """
        if not numbers:
            return None
        text = self.question_text(group_id)
        replacement = group_objective_label_replacement(text, numbers)
        if replacement is None:
            return None
        old_label, new_label = replacement
        if old_label == new_label:
            return old_label, new_label

        block_start = self.block_boundary_position(question_field(group_id))
        block_end = self.block_boundary_position(self.next_boundary(group_id))
        first_child = next(iter(self.child_question_fields(group_id)), None)
        child_anchor = (
            self.anchor_position(first_child.field_name)
            if first_child is not None
            else block_end
        )
        self.hwp.set_position(self.anchor_position(question_field(group_id)))
        found = self.hwp.find_text(old_label)
        found_start = found[:3]
        found_end = found[3:]
        if not (
            block_start <= found_start < found_end <= block_end
            and found_end <= child_anchor
        ):
            self.hwp.automation.Run("Cancel")
            raise HwpAutomationError(
                f"Unsafe group label selection for {group_id}: {found!r}"
            )
        self.hwp.insert_text(new_label)
        self.invalidate_cache()
        try:
            verify = self.question_text(group_id)
        finally:
            # question_text selects the complete group block. Never leak that
            # selection into final numbering/save operations: MoveDocEnd would
            # otherwise extend it and a following InsertFile would replace the
            # selected questions, including __HWPQ__DOCUMENT_END.
            self.hwp.automation.Run("Cancel")
        verify_match = GROUP_OBJECTIVE_LABEL.match(verify)
        if verify_match is None:
            raise HwpAutomationError(
                f"Group label disappeared after update: {group_id}"
            )
        actual = verify_match.group(0)[len(verify_match.group("leading")) :]
        if actual != new_label:
            raise HwpAutomationError(
                f"Group label verification failed: {actual!r} != {new_label!r}"
            )
        return old_label, new_label

    def control_inventory(self, *, limit: int | None = 10_000) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        """Collect global totals and per-block object ownership in the same native pass."""
        layout = ControlLayout.read(self.hwp, inventory=True, limit=limit)
        self._control_layout = layout
        order = self.ordered_question_ids()
        starts = [self.block_boundary_position(question_field(qid)) for qid in order]
        ends = [self.block_boundary_position(self.next_boundary(qid)) for qid in order]
        ranges = list(zip(starts, ends))
        ordered = all(first <= last for first, last in ranges) and all(
            end <= start for end, start in zip(ends, starts[1:])
        )
        results = {qid: Counter() for qid in order}
        for anchor, key in layout.owned_anchors:
            if ordered:
                index = bisect_right(starts, anchor) - 1
                if index >= 0 and anchor < ends[index]:
                    results[order[index]][key] += 1
            else:
                # Leading shapes in shared paragraphs can expand a boundary
                # before its predecessor. Retain the document-order match.
                for qid, (first, last) in zip(order, ranges):
                    if first <= anchor < last:
                        results[qid][key] += 1
                        break
        return dict(sorted(layout.totals.items())), {
            qid: dict(sorted(counts.items())) for qid, counts in results.items()
        }

    def question_control_summaries(self) -> dict[str, dict[str, int]]:
        return self.control_inventory(limit=None)[1]

    def move_before(
        self,
        question_id: str,
        destination_id: str | None,
        *,
        on_progress: Callable[[str], None] | None = None,
        step_delay: float = 0.0,
    ) -> MoveAudit:
        if question_id == destination_id:
            raise ValueError("A question cannot be moved before itself")
        order_before = self.ordered_question_ids()
        fixed_target = destination_id is not None and destination_id.startswith(FIXED_FIELD_PREFIX)
        if question_id not in order_before:
            raise HwpAutomationError("Source question ID is missing")
        if fixed_target:
            layout = self.block_layout()
            if destination_id not in layout:
                raise HwpAutomationError("Fixed template boundary is missing")
            before_fixed = layout[:layout.index(destination_id)]
            insertion_index = sum(qid in order_before and qid != question_id for qid in before_fixed)
            destination_field = destination_id
        else:
            remaining = [qid for qid in order_before if qid != question_id]
            if destination_id is not None and destination_id not in remaining:
                raise HwpAutomationError("Destination question ID is missing")
            insertion_index = remaining.index(destination_id) if destination_id is not None else len(remaining)
            destination_field = question_field(destination_id) if destination_id is not None else TERMINAL_FIELD

        destination_before_cut = self.block_boundary_position(destination_field)
        starts_paragraph = (destination_before_cut[0] == 0 and destination_before_cut[2] == 0
                            and destination_before_cut == self.anchor_position(destination_field))
        self.select_question(question_id)
        if on_progress is not None:
            on_progress(f"{question_id} 문항 블록 선택")
        self.hwp.visual_step(step_delay)
        selected_fields = self.field_names(selection_only=True)
        expected_fields = self.owned_field_names(question_id)
        if selected_fields != expected_fields:
            raise HwpAutomationError(
                "Question selection has unsafe fields: "
                f"actual={selected_fields!r}, expected={expected_fields!r}"
            )
        selected_text = self.hwp.selected_plain_text()
        if on_progress is not None:
            on_progress(f"{question_id} 문항 잘라내기")
        # A new list gesture can cancel at a progress callback. Finish the
        # entire clipboard transfer before allowing it to discard this tab.
        cut_order = tuple(qid for qid in order_before if qid != question_id)
        self.hwp.cut_selection()
        if self._order_after_transfer(order_before) != cut_order:
            raise QuestionTransferMismatch(f"{question_id} 문항만 잘라냈는지 확인할 수 없습니다.")
        destination_position = self._destination_after_cut(destination_field, starts_paragraph)
        self.hwp.set_position(destination_position)
        self.hwp.paste()
        self.hwp.visual_step(step_delay)
        order_after = self._order_after_transfer(cut_order)
        expected_order = list(cut_order)
        expected_order.insert(insertion_index, question_id)
        if order_after != tuple(expected_order):
            raise QuestionTransferMismatch(f"{question_id} 문항을 붙여넣은 뒤 순서가 요청과 다릅니다.")
        if on_progress is not None:
            on_progress(f"{question_id} → {destination_id + ' 앞' if destination_id else '마지막'}에 붙여넣기")
        return MoveAudit(
            question_id=question_id,
            destination_id=destination_id if destination_id is not None else TERMINAL_FIELD,
            order_before=order_before,
            order_after=order_after,
            selected_fields=selected_fields,
            selected_text_length=len(selected_text),
        )

    def _order_after_transfer(self, previous: tuple[str, ...]) -> tuple[str, ...]:
        # Hancom can update GetFieldList after the clipboard action returns.
        self.invalidate_cache()
        order = self.ordered_question_ids()
        deadline = time.monotonic() + 1.5
        while order == previous and time.monotonic() < deadline:
            time.sleep(0.035)
            pythoncom.PumpWaitingMessages()
            self.invalidate_cache()
            order = self.ordered_question_ids()
        return order

    def _destination_after_cut(self, field: str, starts_paragraph: bool) -> tuple[int, int, int]:
        if starts_paragraph:
            # Locate the field again: cutting earlier paragraphs changes their
            # indices. Its eight-position control must still be first in the body
            # paragraph. A merged paragraph or a leading object uses the full scan.
            try:
                lst, para, pos = self.position(field)
                if lst == 0 and pos == 8:
                    return (0, para, 0)
            except HwpAutomationError:
                pass
        return self.block_boundary_position(field)
