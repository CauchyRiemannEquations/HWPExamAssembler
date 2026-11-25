from __future__ import annotations

import ctypes
from pathlib import Path
import time
from typing import Any

import pythoncom
import win32com.client

from .diagnostics import safe_summary


class HwpAutomationError(RuntimeError):
    """Raised when Hangul Automation cannot complete an operation."""


class HwpApplication:
    """One isolated Hangul Automation instance.

    The wrapper intentionally exposes only the operations needed by the first
    proof of concept: open, inspect, select all, copy, paste, save, and quit.
    """

    def __init__(
        self,
        *,
        visible: bool = True,
        security_module: str | None = "FilePathCheckerModuleExample",
    ) -> None:
        self.visible = visible
        self.security_module = security_module
        self.security_module_registered: bool | None = None
        self._hwp: Any | None = None
        self._com_initialized = False
        self.diagnostic = None

    def __enter__(self) -> "HwpApplication":
        pythoncom.CoInitialize()
        self._com_initialized = True
        try:
            self._hwp = win32com.client.DispatchEx("HWPFrame.HwpObject.2")
            if self.security_module:
                # HWP 2022 (12.0.0.4605) can return False even though the
                # registered checker is subsequently called. Preserve the
                # value for auditing and verify access through Open(). The
                # official sample checker can still request one user approval.
                self.security_module_registered = bool(
                    self._hwp.RegisterModule(
                        "FilePathCheckDLL", self.security_module
                    )
                )
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @property
    def automation(self) -> Any:
        if self._hwp is None:
            raise HwpAutomationError("Hangul Automation is not running")
        return self._hwp

    def open(self, path: str | Path) -> None:
        document_path = Path(path).resolve()
        if not document_path.is_file():
            raise FileNotFoundError(document_path)

        # The approval prompt belongs to Hangul, so its window must be visible
        # before Open() when no FilePathChecker module has been registered yet.
        if self.visible:
            self.automation.XHwpWindows.Item(0).Visible = True

        # Calling HwpObject.Open() can occasionally stall on the initial blank
        # frame after an Automation process was force-terminated. Opening
        # through the already-created XHwpDocument is the same path used by
        # open_additional_document() and has proved recoverable on HWP 2022.
        document = self.automation.XHwpDocuments.Active_XHwpDocument
        old_message_mode = self.automation.SetMessageBoxMode(0x00001000)
        try:
            opened = document.Open(
                str(document_path), "", "lock:false;skipcomment:true"
            )
            if not opened:
                # A freshly started HWP 2022 Automation frame can
                # intermittently reject XHwpDocument.Open from a GUI worker
                # thread. The equivalent IHwpObject.Open path succeeds in the
                # same process and is the documented recovery route.
                opened = self.automation.Open(
                    str(document_path), "", "lock:false;skipcomment:true"
                )
        finally:
            self.automation.SetMessageBoxMode(old_message_mode)
        if not opened:
            raise HwpAutomationError(f"Failed to open document: {document_path}")

    def open_via_object(self, path: str | Path) -> None:
        """Open through IHwpObject instead of the active XHwpDocument.

        HWP 2022 can occasionally leave one of its two Automation open paths
        unresponsive after an abnormally terminated process. This alternate
        official entry point is kept explicit so callers can recover without
        changing the normal, previously verified path.
        """
        document_path = Path(path).resolve()
        if not document_path.is_file():
            raise FileNotFoundError(document_path)
        if self.visible:
            self.automation.XHwpWindows.Item(0).Visible = True
        old_message_mode = self.automation.SetMessageBoxMode(0x00001000)
        try:
            opened = self.automation.Open(
                str(document_path), "", "lock:false;skipcomment:true"
            )
        finally:
            self.automation.SetMessageBoxMode(old_message_mode)
        if not opened:
            raise HwpAutomationError(f"Failed to open document: {document_path}")

    def active_document(self) -> Any:
        return self.automation.XHwpDocuments.Active_XHwpDocument

    def open_additional_document(self, path: str | Path) -> Any:
        document_path = Path(path).resolve()
        if not document_path.is_file():
            raise FileNotFoundError(document_path)
        # Keep source and target in one HWP window as tabs. With separate
        # windows HWP 2022 can switch IXHwpDocument while IHwpObject actions
        # (Copy/Paste/Field/Save) still address the previous window.
        document = self.automation.XHwpDocuments.Add(1)
        try:
            old_message_mode = self.automation.SetMessageBoxMode(0x00001000)
            try:
                opened = document.Open(
                    str(document_path), "", "lock:false;skipcomment:true"
                )
            finally:
                self.automation.SetMessageBoxMode(old_message_mode)
            if not opened:
                raise HwpAutomationError(
                    f"Failed to open additional document: {document_path}"
                )
        except BaseException:
            # Add(1) has already created a tab. If Open fails before the
            # caller receives its handle, clean that exact tab here so later
            # source/target activation cannot drift onto an orphan blank tab.
            try:
                self.close_document(document, discard_changes=True)
            except Exception:
                pass
            raise
        if self.visible:
            for index in range(self.automation.XHwpWindows.Count):
                self.automation.XHwpWindows.Item(index).Visible = True
        return document

    def activate_document(
        self,
        document: Any,
        *,
        timeout_seconds: float = 1.5,
    ) -> Any:
        """Activate an exact HWP tab and verify that the switch has settled.

        ``SetActive_XHwpDocument`` can return before IHwpObject actions such
        as MoveDocEnd, Paste, and HeadCtrl have switched to the requested tab.
        Never issue a mutating action based only on that call returning.
        """
        target_id = int(document.DocumentID)
        deadline = time.perf_counter() + timeout_seconds
        last_id: int | None = None
        while True:
            document.SetActive_XHwpDocument()
            pythoncom.PumpWaitingMessages()
            active = self.active_document()
            last_id = int(active.DocumentID)
            if last_id == target_id:
                # A second activation after the observable ID switch flushes
                # HWP 2022's tab bridge used by the global IHwpObject actions.
                document.SetActive_XHwpDocument()
                time.sleep(0.01)
                pythoncom.PumpWaitingMessages()
                active = self.active_document()
                if int(active.DocumentID) == target_id:
                    return active
            if time.perf_counter() >= deadline:
                raise HwpAutomationError(
                    "HWP document activation did not settle: "
                    f"target={target_id}, active={last_id}"
                )
            time.sleep(0.015)

    def require_active_document(self, document: Any, *, operation: str) -> None:
        target_id = int(document.DocumentID)
        active_id = int(self.active_document().DocumentID)
        if active_id != target_id:
            raise HwpAutomationError(
                f"{operation} 문서 전환 오류: target={target_id}, active={active_id}"
            )

    def show_windows(self) -> None:
        for index in range(self.automation.XHwpWindows.Count):
            self.automation.XHwpWindows.Item(index).Visible = True

    def visual_step(self, seconds: float = 0.45) -> None:
        """Make the active HWP document observable during Automation."""
        if not self.visible:
            return
        self.show_windows()
        if seconds > 0:
            time.sleep(seconds)

    def plain_text(self) -> str:
        value = self.automation.GetTextFile("UNICODE", "")
        return "" if value is None else str(value)

    def control_summary(self, *, limit: int = 10_000) -> dict[str, int]:
        from .control_layout import count_controls

        return count_controls(self, transfer=False, limit=limit)

    def transfer_control_summary(self, *, limit: int = 10_000) -> dict[str, int]:
        """Count native controls using HWP clipboard-stable table semantics.

        HWP exposes a table contained in another table as an additional
        ``tbl`` control in a child list.  Copy/Paste of the selected outer
        table can normalize that internal representation while preserving the
        exact selected text and top-level native table.  Counting those child
        ``tbl`` records caused false missing-table alarms. Equations and
        drawings remain individually significant even inside tables, so they
        are still counted at every list level.
        """
        from .control_layout import count_controls

        return count_controls(self, transfer=True, limit=limit)

    def control_inventory(self, *, limit: int = 10_000) -> list[dict[str, object]]:
        inventory: list[dict[str, object]] = []
        control = self.automation.HeadCtrl
        visited = 0
        while control is not None:
            anchor = control.GetAnchorPos(0)
            inventory.append(
                {
                    "id": str(getattr(control, "CtrlID", "<unknown>")),
                    "description": str(getattr(control, "UserDesc", "")).strip(),
                    "anchor": {
                        "list": int(anchor.Item("List")),
                        "para": int(anchor.Item("Para")),
                        "pos": int(anchor.Item("Pos")),
                    },
                }
            )
            visited += 1
            if visited >= limit:
                raise HwpAutomationError("Control traversal limit exceeded")
            control = getattr(control, "Next", None)
        return inventory

    def last_top_level_control_anchor(
        self,
        control_id: str,
        *,
        before: tuple[int, int, int] | None = None,
        limit: int = 10_000,
    ) -> tuple[int, int, int]:
        """Return the last body-list anchor for a native control type."""
        matches: list[tuple[int, int, int]] = []
        control = self.automation.HeadCtrl
        visited = 0
        while control is not None:
            if str(getattr(control, "CtrlID", "")) == control_id:
                value = control.GetAnchorPos(1)
                anchor = (
                    int(value.Item("List")),
                    int(value.Item("Para")),
                    int(value.Item("Pos")),
                )
                if anchor[0] == 0 and (before is None or anchor < before):
                    matches.append(anchor)
            visited += 1
            if visited >= limit:
                raise HwpAutomationError("Control traversal limit exceeded")
            control = getattr(control, "Next", None)
        if not matches:
            raise HwpAutomationError(
                f"Top-level control not found: control_id={control_id!r}"
            )
        return max(matches)

    def control_summary_in_range(
        self,
        start: tuple[int, int, int],
        end: tuple[int, int, int],
        *,
        control_ids: frozenset[str] = frozenset({"eqed", "gso", "tbl"}),
        limit: int = 10_000,
    ) -> dict[str, int]:
        """Count native controls whose top-level anchors belong to a block.

        ``GetAnchorPos(1)`` resolves an equation inside a table or text box to
        the top-level body position of its owner. This lets a source block be
        compared with native Paste without recreating any object.
        """
        return self.control_summaries_for_ranges(
            ((start, end),), control_ids=control_ids, limit=limit
        )[0]

    def control_summaries_for_ranges(
        self,
        ranges: tuple[
            tuple[tuple[int, int, int], tuple[int, int, int]], ...
        ],
        *,
        control_ids: frozenset[str] = frozenset({"eqed", "gso", "tbl"}),
        limit: int = 10_000,
    ) -> tuple[dict[str, int], ...]:
        """Count controls for ordered blocks in one HeadCtrl traversal."""
        from .control_layout import count_control_ranges

        return count_control_ranges(self, ranges, control_ids=control_ids, transfer=False, limit=limit)

    def transfer_control_summaries_for_ranges(
        self,
        ranges: tuple[
            tuple[tuple[int, int, int], tuple[int, int, int]], ...
        ],
        *,
        limit: int = 10_000,
    ) -> tuple[dict[str, int], ...]:
        """Range form of :meth:`transfer_control_summary`."""
        from .control_layout import count_control_ranges

        return count_control_ranges(self, ranges, transfer=True, limit=limit)

    def transfer_anchor_exclusions(self, copy_ranges, integrity_ranges, *, limit: int = 10_000):
        """Read-only anchor evidence; call before establishing Copy selection.

        Same nested-table exclusion and GetAnchorPos(1) ownership rules as
        transfer_control_summaries_for_ranges. HWP selection is not assumed
        to be half-open: this is evidence about anchors, not proof of Copy.
        """
        if len(copy_ranges) != len(integrity_ranges):
            raise ValueError("Copy/integrity range counts differ")
        result = [[] for _ in copy_ranges]
        from .control_layout import ControlChain

        chain = ControlChain(self.automation.HeadCtrl)
        for _control, ctrl_id, anchor in chain.anchored_controls(limit, transfer=True):
            for index, ((start, end), (check_start, check_end)) in enumerate(
                zip(copy_ranges, integrity_ranges, strict=True)
            ):
                if start <= anchor < end and not check_start <= anchor < check_end:
                    result[index].append({"ctrl_id": ctrl_id, "anchor": anchor})
        return tuple(result)

    def select_all(self) -> None:
        if not self.automation.Run("MoveDocBegin"):
            raise HwpAutomationError("MoveDocBegin failed")
        if not self.automation.Run("SelectAll"):
            raise HwpAutomationError("SelectAll failed")

    def find_text(self, text: str) -> tuple[int, int, int, int, int, int]:
        parameter = self.automation.HParameterSet.HFindReplace
        self.automation.HAction.GetDefault("RepeatFind", parameter.HSet)
        parameter.ReplaceString = ""
        parameter.FindString = text
        parameter.Direction = self.automation.FindDir("Forward")
        parameter.WholeWordOnly = 0
        parameter.UseWildCards = 0
        parameter.SeveralWords = 0
        parameter.AllWordForms = 0
        parameter.MatchCase = 0
        parameter.ReplaceMode = 0
        parameter.FindRegExp = 0
        parameter.FindJaso = 0
        parameter.HanjaFromHangul = 0
        parameter.IgnoreMessage = 1
        parameter.FindType = 1
        found = self.automation.HAction.Execute("RepeatFind", parameter.HSet)
        if not found:
            raise HwpAutomationError(f"Text marker not found: {text!r}")
        selected = self.automation.GetSelectedPos()
        normalized = self._normalize_selected_position(selected)
        if normalized is None:
            raise HwpAutomationError(
                f"Cannot read selected marker position: {text!r}; "
                f"returned={selected!r}; type={type(selected)!r}"
            )
        return normalized

    def top_level_anchor_for_text(
        self, text: str
    ) -> tuple[int, int, int] | None:
        """Find text anywhere (including a text box) and return its body anchor.

        Confirmation notices are usually text inside a floating shape.  Their
        scan list is therefore not list 0, but ``MoveTopLevelList`` resolves
        the selection to the owning shape's paragraph in the main body.  The
        method is read-only and returns ``None`` when the marker is absent.
        """
        self.automation.Run("Cancel")
        self.automation.Run("MoveDocBegin")
        try:
            self.find_text(text)
        except HwpAutomationError:
            self.automation.Run("Cancel")
            return None
        self.automation.Run("MoveTopLevelList")
        anchor = self.current_position()
        self.automation.Run("Cancel")
        if anchor[0] != 0:
            raise HwpAutomationError(
                f"Text marker did not resolve to the main body: {text!r}, {anchor!r}"
            )
        return anchor

    def position_shape_at_page_bottom(
        self,
        anchor: tuple[int, int, int],
        *,
        align_right: bool = True,
    ) -> dict[str, int]:
        """Pin one verified top-level drawing object to the page's bottom.

        The caller must pass the anchor returned for the just-pasted school
        confirmation box.  The native object and all of its text/formatting
        remain intact; only its HWP ShapeObject layout properties are changed.
        """
        self.set_position(anchor)
        found = str(self.automation.FindCtrl() or "")
        selected = self.automation.CurSelectedCtrl
        if found != "gso" or selected is None or str(selected.CtrlID) != "gso":
            raise HwpAutomationError(
                f"Cannot select final confirmation shape at {anchor!r}: {found!r}"
            )
        parameter = self.automation.HParameterSet.HShapeObject
        if not self.automation.HAction.GetDefault(
            "ShapeObjDialog", parameter.HSet
        ):
            raise HwpAutomationError("Cannot read final confirmation shape layout")
        # 1=page/body area and 2=bottom according to the bundled Hancom
        # ParameterSet table. Top-and-bottom wrapping plus no overlap keeps the
        # last question from being silently covered by the floating box.
        parameter.VertRelTo = 1
        parameter.VertAlign = 2
        parameter.VertOffset = 0
        parameter.TextWrap = 1
        parameter.AllowOverlap = 0
        if align_right:
            parameter.HorzRelTo = 1
            parameter.HorzAlign = 2
            parameter.HorzOffset = 0
        if not self.automation.HAction.Execute(
            "ShapeObjDialog", parameter.HSet
        ):
            raise HwpAutomationError("Cannot position final confirmation shape")

        # Re-read the live object rather than trusting Execute's return value.
        values = self.shape_layout(anchor)
        expected = {
            "VertRelTo": 1,
            "VertAlign": 2,
            "VertOffset": 0,
            "TextWrap": 1,
            "AllowOverlap": 0,
        }
        if align_right:
            expected.update(
                {
                    "HorzRelTo": 1,
                    "HorzAlign": 2,
                    "HorzOffset": 0,
                }
            )
        if any(values[key] != value for key, value in expected.items()):
            raise HwpAutomationError(
                f"Final confirmation shape layout verification failed: {values!r}"
            )
        return values

    def shape_layout(
        self, anchor: tuple[int, int, int]
    ) -> dict[str, int]:
        """Read one exact drawing object's persisted layout without changing it."""
        self.set_position(anchor)
        if str(self.automation.FindCtrl() or "") != "gso":
            raise HwpAutomationError("Final confirmation shape disappeared")
        verify = self.automation.HParameterSet.HShapeObject
        if not self.automation.HAction.GetDefault("ShapeObjDialog", verify.HSet):
            raise HwpAutomationError("Cannot verify final confirmation shape layout")
        values = {
            "VertRelTo": int(verify.VertRelTo),
            "VertAlign": int(verify.VertAlign),
            "VertOffset": int(verify.VertOffset),
            "HorzRelTo": int(verify.HorzRelTo),
            "HorzAlign": int(verify.HorzAlign),
            "HorzOffset": int(verify.HorzOffset),
            "TextWrap": int(verify.TextWrap),
            "AllowOverlap": int(verify.AllowOverlap),
        }
        self.automation.Run("Cancel")
        return values

    def select_between_markers(
        self, start_marker: str, end_marker: str
    ) -> tuple[int, int, int, int, int, int]:
        self.automation.Run("MoveDocBegin")
        start_selection = self.find_text(start_marker)
        start = start_selection[:3]

        self.automation.Run("Cancel")
        self.automation.Run("MoveDocBegin")
        end_selection = self.find_text(end_marker)
        end = end_selection[:3]

        if start[0] != end[0] or start >= end:
            raise HwpAutomationError(
                f"Unsafe marker range: start={start!r}, end={end!r}"
            )

        self.automation.Run("Cancel")
        if not self.automation.SetPos(*start):
            raise HwpAutomationError(f"Cannot move to range start: {start!r}")
        # Hangul 2022 enters selection mode here but may return False.
        # The selected coordinates below are the authoritative check.
        self.automation.Run("Select")
        if not self.automation.SetPos(*end):
            raise HwpAutomationError(f"Cannot extend selection to range end: {end!r}")

        selected = self.automation.GetSelectedPos()
        normalized = self._normalize_selected_position(selected)
        if normalized is None:
            raise HwpAutomationError("Range selection was not created")
        return normalized

    def select_from_marker_to_end(
        self, start_marker: str
    ) -> tuple[int, int, int, int, int, int]:
        self.automation.Run("MoveDocBegin")
        start_selection = self.find_text(start_marker)
        start = start_selection[:3]

        self.automation.Run("Cancel")
        self.automation.Run("MoveDocEnd")
        end_value = self._normalize_position(self.automation.GetPos())
        if end_value is None or start[0] != end_value[0] or start >= end_value:
            raise HwpAutomationError(
                f"Unsafe marker-to-end range: start={start!r}, end={end_value!r}"
            )

        if not self.automation.SetPos(*start):
            raise HwpAutomationError(f"Cannot move to range start: {start!r}")
        self.automation.Run("Select")
        if not self.automation.SetPos(*end_value):
            raise HwpAutomationError(
                f"Cannot extend selection to document end: {end_value!r}"
            )
        normalized = self._normalize_selected_position(
            self.automation.GetSelectedPos()
        )
        if normalized is None:
            raise HwpAutomationError("Marker-to-end selection was not created")
        return normalized

    def select_between_paragraph_markers(
        self, start_marker: str, end_marker: str
    ) -> tuple[int, int, int, int, int, int]:
        """Select whole body paragraphs containing two searchable markers.

        HWP paragraph numbers are not always searchable text, and an equation
        object can precede the first searchable word. Expanding both marker
        positions to paragraph offset zero keeps those native objects and the
        paragraph-number formatting inside the copied block.
        """
        self.automation.Run("MoveDocBegin")
        start_selection = self.find_text(start_marker)
        start = (start_selection[0], start_selection[1], 0)

        self.automation.Run("Cancel")
        self.automation.Run("MoveDocBegin")
        end_selection = self.find_text(end_marker)
        end = (end_selection[0], end_selection[1], 0)

        if start[0] != end[0] or start >= end:
            raise HwpAutomationError(
                f"Unsafe paragraph marker range: start={start!r}, end={end!r}"
            )
        return self.select_position_range(start, end)

    def select_paragraph_from_marker_to_end(
        self, start_marker: str
    ) -> tuple[int, int, int, int, int, int]:
        self.automation.Run("MoveDocBegin")
        start_selection = self.find_text(start_marker)
        start = (start_selection[0], start_selection[1], 0)

        self.automation.Run("Cancel")
        self.automation.Run("MoveDocEnd")
        end = self._normalize_position(self.automation.GetPos())
        if end is None or start[0] != end[0] or start >= end:
            raise HwpAutomationError(
                f"Unsafe paragraph marker-to-end range: start={start!r}, end={end!r}"
            )
        return self.select_position_range(start, end)

    def select_position_range(
        self, start: tuple[int, int, int], end: tuple[int, int, int]
    ) -> tuple[int, int, int, int, int, int]:
        self.automation.Run("Cancel")
        if not self.automation.SetPos(*start):
            raise HwpAutomationError(f"Cannot move to range start: {start!r}")
        self.automation.Run("Select")
        if not self.automation.SetPos(*end):
            raise HwpAutomationError(f"Cannot extend selection to range end: {end!r}")
        normalized = self._normalize_selected_position(
            self.automation.GetSelectedPos()
        )
        if normalized is None:
            raise HwpAutomationError("Position range selection was not created")
        return normalized

    def current_position(self) -> tuple[int, int, int]:
        normalized = self._normalize_position(self.automation.GetPos())
        if normalized is None:
            raise HwpAutomationError("Cannot read current caret position")
        return normalized

    def set_position(self, position: tuple[int, int, int]) -> None:
        self.automation.Run("Cancel")
        if not self.automation.SetPos(*position):
            raise HwpAutomationError(f"Cannot move to position: {position!r}")

    def previous_paragraph_end(
        self, position: tuple[int, int, int]
    ) -> tuple[int, int, int]:
        self.set_position(position)
        self.automation.Run("MovePrevParaEnd")
        result = self.current_position()
        if result[0] != position[0] or result >= position:
            raise HwpAutomationError(
                f"Cannot resolve previous paragraph end: {position!r} -> {result!r}"
            )
        return result

    @staticmethod
    def _normalize_selected_position(
        selected: object,
    ) -> tuple[int, int, int, int, int, int] | None:
        if not selected or not hasattr(selected, "__len__"):
            return None
        values = tuple(selected)
        # pywin32 exposes COM's BOOL return value before the six out params.
        if len(values) == 7:
            if not bool(values[0]):
                return None
            values = values[1:]
        if len(values) != 6:
            return None
        return tuple(int(value) for value in values)

    @staticmethod
    def _normalize_position(position: object) -> tuple[int, int, int] | None:
        if position is None or not hasattr(position, "__len__"):
            return None
        values = tuple(position)
        if len(values) == 4:
            if not bool(values[0]):
                return None
            values = values[1:]
        if len(values) != 3:
            return None
        return tuple(int(value) for value in values)

    def selected_plain_text(self) -> str:
        value = self.automation.GetTextFile("UNICODE", "saveblock")
        return "" if value is None else str(value)

    def selected_position(self) -> tuple[int, int, int, int, int, int] | None:
        return self._normalize_selected_position(self.automation.GetSelectedPos())

    def copy_selection(self) -> None:
        self._send_selection_to_clipboard("Copy")

    def cut_selection(self) -> None:
        self._send_selection_to_clipboard("Cut")

    def _send_selection_to_clipboard(self, action: str) -> None:
        # Run() may return False for clipboard actions in HWP 2022 even when
        # the action executes. Wait for Windows' clipboard generation to
        # advance so a complex equation/drawing block is fully published
        # before switching to another HWP tab.
        started = time.perf_counter()
        before = int(ctypes.windll.user32.GetClipboardSequenceNumber())
        self.automation.Run(action)
        deadline = time.perf_counter() + 1.5
        changed = False
        current = before
        while time.perf_counter() < deadline:
            pythoncom.PumpWaitingMessages()
            current = int(ctypes.windll.user32.GetClipboardSequenceNumber())
            if current != before:
                changed = True
                break
            time.sleep(0.015)
        # HWP can use a private delayed-rendering clipboard whose public
        # sequence number does not advance. Keep a small fallback settle time
        # instead of treating that implementation detail as a copy failure.
        time.sleep(0.045 if changed else 0.12)
        pythoncom.PumpWaitingMessages()

        if self.diagnostic is not None and action == "Copy":
            self.diagnostic.emit("clipboard_copy", sequence_before=before, sequence_after=current,
                                 sequence_changed=changed, elapsed_seconds=time.perf_counter() - started)

    def move_to_end(self) -> None:
        # MoveDocEnd extends an existing block selection instead of merely
        # moving the caret. Group-label verification intentionally selects the
        # whole native group; carrying that selection into InsertFile can
        # replace several questions and delete the terminal QID field.
        self.automation.Run("Cancel")
        self.automation.Run("MoveDocEnd")

    def paste(self) -> None:
        self.automation.Run("Paste")

    def save_selection_as_hwp(self, path: str | Path) -> Path:
        """Save the current native HWP selection as a standalone block file."""
        output = Path(path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise FileExistsError(output)
        parameter = self.automation.HParameterSet.HFileOpenSave
        self.automation.HAction.GetDefault("FileSaveBlock_S", parameter.HSet)
        parameter.filename = str(output)
        parameter.Format = "HWP"
        parameter.Attributes = 0
        parameter.Argument = "lock:false;fullsave:true"
        try:
            saved = bool(
                self.automation.HAction.Execute(
                    "FileSaveBlock_S", parameter.HSet
                )
            )
        finally:
            self.automation.Run("Cancel")
        if not saved or not output.is_file() or output.stat().st_size == 0:
            raise HwpAutomationError(
                f"Selected HWP block was not saved: {output}"
            )
        return output

    def insert_hwp_file(self, path: str | Path) -> None:
        """Insert an HWP block without using the cross-document clipboard."""
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        parameter = self.automation.HParameterSet.HInsertFile
        self.automation.HAction.GetDefault("InsertFile", parameter.HSet)
        parameter.filename = str(source)
        # Keep the block's character/paragraph styling, but do not import its
        # page section, headers, or footers into the school exam template.
        parameter.KeepSection = 0
        parameter.KeepCharshape = 1
        parameter.KeepParashape = 1
        parameter.KeepStyle = 1
        inserted = bool(
            self.automation.HAction.Execute("InsertFile", parameter.HSet)
        )
        if not inserted:
            raise HwpAutomationError(f"InsertFile failed: {source}")

    def paste_and_wait_for_controls(
        self,
        document: Any,
        before: dict[str, int],
        expected_delta: dict[str, int],
        *,
        timeout_seconds: float = 2.0,
    ) -> dict[str, int]:
        """Paste and wait until native controls settle in the target tab.

        Large equation and drawing blocks are materialized asynchronously by
        HWP 2022. An immediate HeadCtrl traversal can observe a legitimate
        paste halfway through and falsely roll it back. This method waits for
        the expected native controls and a stable observation before the
        caller performs its exact object and text comparison.
        """
        self.require_active_document(document, operation="붙여넣기 직전")
        diagnostic = self.diagnostic
        caret_before = self.current_position() if diagnostic is not None else None
        sequence = int(ctypes.windll.user32.GetClipboardSequenceNumber()) if diagnostic is not None else None
        self.automation.Run("Paste")
        started = time.perf_counter()
        if diagnostic is not None:
            diagnostic.emit("paste_invoked", caret_before=caret_before,
                            caret_immediate=self.current_position(), clipboard_sequence=sequence)
        deadline = started + timeout_seconds
        last: dict[str, int] | None = None
        stable_samples = 0
        latest = dict(before)
        movable_ids = {"eqed", "gso", "tbl"}

        while True:
            pythoncom.PumpWaitingMessages()
            latest = self.transfer_control_summary()
            delta = {
                key: int(latest.get(key, 0)) - int(before.get(key, 0))
                for key in set(before) | set(latest)
                if key.split(":", 1)[0] in movable_ids
                and int(latest.get(key, 0)) != int(before.get(key, 0))
            }
            has_negative = any(value < 0 for value in delta.values())
            expected_ready = not has_negative and all(
                int(delta.get(key, 0)) >= int(value)
                for key, value in expected_delta.items()
            )
            if latest == last:
                stable_samples += 1
            else:
                stable_samples = 0
                last = latest

            elapsed = time.perf_counter() - started
            settled = expected_ready and elapsed >= 0.07 and stable_samples >= 1
            timed_out = time.perf_counter() >= deadline
            if diagnostic is not None:
                diagnostic.emit("paste_sample", caret=self.current_position(),
                                elapsed_seconds=elapsed, stable_samples=stable_samples,
                                controls=safe_summary(latest), control_delta=safe_summary(delta),
                                expected_ready=expected_ready, controls_settled=settled,
                                text_settled=None, text_only=not bool(expected_delta),
                                exit_reason="controls_ready" if settled else "timeout" if timed_out else None)
            if settled or timed_out:
                return latest
            if has_negative:
                # Active_XHwpDocument can update before the global IHwpObject
                # bridge. Reassert the target before the next observation.
                self.activate_document(document, timeout_seconds=0.4)
            time.sleep(0.025)

    def delete_selection(self) -> None:
        self.automation.Run("Delete")

    def delete_range_preserving_native_controls(
        self,
        start: tuple[int, int, int],
        end: tuple[int, int, int],
        controls_before: dict[str, int],
    ) -> bool:
        """Delete a paragraph range, undoing it if HWP removes native objects.

        HWP 2022 can treat a table anchored immediately after a paragraph
        boundary as part of that boundary. Deleting a visually empty leading
        paragraph may then remove the following table and its equations. The
        delete is kept only when the native-control inventory is unchanged.
        """
        movable_ids = {"eqed", "gso", "tbl"}

        def movable(summary: dict[str, int]) -> dict[str, int]:
            return {
                key: int(value)
                for key, value in summary.items()
                if key.split(":", 1)[0] in movable_ids
            }

        expected = movable(controls_before)
        self.select_position_range(start, end)
        self.delete_selection()
        after_delete = self.transfer_control_summary()
        if movable(after_delete) == expected:
            return True

        self.automation.Run("Undo")
        restored = self.transfer_control_summary()
        if movable(restored) != expected:
            raise HwpAutomationError(
                "빈 문단 삭제가 네이티브 개체를 제거했고 Undo로 완전히 "
                f"복구하지 못했습니다: before={expected!r}, "
                f"after={movable(restored)!r}"
            )
        return False

    def break_paragraph(self) -> None:
        self.automation.Run("BreakPara")

    def insert_text(self, text: str) -> None:
        parameter = self.automation.HParameterSet.HInsertText
        self.automation.HAction.GetDefault("InsertText", parameter.HSet)
        parameter.Text = text
        executed = self.automation.HAction.Execute("InsertText", parameter.HSet)
        if not executed:
            raise HwpAutomationError(f"InsertText failed: {text!r}")

    def save(self) -> None:
        # HwpObject.Save has mandatory COM parameters in HWP 2022. Because
        # the target is already a protected copy at its final path, FileSave
        # is the correct no-dialog operation here.
        self.automation.HAction.Run("FileSave")

    def save_document(self, document=None, *, force: bool = True) -> None:
        """Save an exact IXHwpDocument, independent of the active tab/window."""
        target = document if document is not None else self.active_document()
        saved = bool(target.Save(not force))
        if not saved and getattr(target, "FullName", ""):
            # HWP 2022 sometimes returns False from Save() for a document
            # opened with lock:false. SaveAs on the document object still
            # targets the correct window and gives us an explicit path.
            old_message_mode = self.automation.SetMessageBoxMode(0x00001000)
            try:
                saved = bool(
                    target.SaveAs(
                        str(target.FullName),
                        str(getattr(target, "Format", "") or "HWP"),
                        "lock:false;fullsave:true",
                    )
                )
            finally:
                self.automation.SetMessageBoxMode(old_message_mode)
        if not saved:
            raise HwpAutomationError(
                f"IXHwpDocument.Save failed: {getattr(target, 'FullName', '')}"
            )

    def save_as(self, path: str | Path, format_name: str) -> bool:
        output_path = Path(path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return bool(self.automation.SaveAs(str(output_path), format_name, ""))

    def save_exam_copy(self, destination: Path) -> None:
        """Write a new working document using the requested native file format."""
        destination = destination.resolve()
        formats = {".hwp": "HWP", ".hwpx": "HWPX"}
        format_name = formats[destination.suffix.lower()]
        if destination.exists():
            raise FileExistsError(destination)
        document = self.active_document()
        previous = self.automation.SetMessageBoxMode(0x00001000)
        try:
            saved = document.SaveAs(str(destination), format_name, "lock:false;fullsave:true")
        finally:
            self.automation.SetMessageBoxMode(previous)
        if not saved or not destination.is_file() or destination.stat().st_size == 0:
            raise HwpAutomationError(f"{format_name} 형식으로 저장하지 못했습니다: {destination.name}")
        signature = bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1") if format_name == "HWP" else b"PK\x03\x04"
        with destination.open("rb") as stream:
            if stream.read(len(signature)) != signature:
                raise HwpAutomationError(f"저장된 문서의 형식이 {format_name}와 일치하지 않습니다.")

    def reset_documents(self) -> None:
        """Close all documents without ending the Automation process."""
        # The common reorder case holds one saved scratch tab. HWP 2022 can
        # block for seconds inside Clear(1) while tearing down native objects.
        # Close the exact last tab first, and check for a fresh blank frame.
        last_error: Exception | None = None
        try:
            self.automation.Run("Cancel")
        except Exception:
            pass
        for attempt in range(4):
            try:
                docs = self.automation.XHwpDocuments
                if int(docs.Count) == 1:
                    doc = docs.Active_XHwpDocument
                    if doc.FullName or doc.Modified:
                        self.close_document(doc, discard_changes=True)
                    if int(docs.Count) == 0 or (
                        int(docs.Count) == 1 and not docs.Active_XHwpDocument.FullName
                        and not docs.Active_XHwpDocument.Modified
                    ):
                        last_error = None
                        break
            except Exception:
                # Recheck through Clear if the document bridge is settling.
                pass
            try:
                self.automation.Clear(1)
                last_error = None
                break
            except Exception as error:
                last_error = error
                try:
                    if int(self.automation.XHwpDocuments.Count) == 0:
                        last_error = None
                        break
                except Exception:
                    pass
                pythoncom.PumpWaitingMessages()
                time.sleep(0.10 * (attempt + 1))
        if last_error is not None:
            raise HwpAutomationError(
                "한글 문서 탭을 저장 후 닫지 못했습니다."
            ) from last_error
        if self.automation.XHwpDocuments.Count == 0:
            self.automation.XHwpDocuments.Add(0)

    def close_document(self, document: Any, *, discard_changes: bool = True) -> None:
        """Close one exact temporary HWP tab without touching other tabs."""
        if document is None:
            return
        # IXHwpDocument.Close can follow the global active-document bridge on
        # HWP 2022. Always make the receiver active and prove its DocumentID
        # before closing, otherwise a delayed tab switch could close target.
        self.activate_document(document)
        self.require_active_document(document, operation="임시 문서 닫기 전")
        closed = bool(document.Close(False if discard_changes else True))
        if not closed:
            raise HwpAutomationError(
                f"Cannot close HWP document: {getattr(document, 'FullName', '')}"
            )
        pythoncom.PumpWaitingMessages()
        time.sleep(0.03)

    def close(self) -> None:
        had_instance = self._hwp is not None
        try:
            if self._hwp is not None:
                try:
                    # Discard any incidental in-memory changes before process
                    # shutdown. Searching/selecting must never rewrite inputs.
                    self._hwp.Clear(1)
                except Exception:
                    pass
                self._hwp.Quit()
        finally:
            self._hwp = None
            if self._com_initialized:
                pythoncom.CoUninitialize()
                self._com_initialized = False
            if had_instance:
                # HWP 2022's out-of-process COM server can report Quit before
                # its automation frame has completely released. A short grace
                # period prevents the next instance from stalling on Open().
                time.sleep(1.0)
