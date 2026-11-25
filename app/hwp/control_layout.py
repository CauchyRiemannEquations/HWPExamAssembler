"""Read a native control chain once for block boundaries and ownership checks."""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field

import pythoncom

from .application import HwpAutomationError


Position = tuple[int, int, int]
_MOVABLE = {"eqed", "gso", "tbl"}


class ControlChain:
    """Resolve the handful of read-only IDispatch members once per chain."""

    def __init__(self, head):
        raw_type = getattr(pythoncom, "TypeIIDs", {}).get(getattr(pythoncom, "IID_IDispatch", None), ())
        raw = getattr(head, "_oleobj_", None)
        self.ids = {}
        self.item_id = None
        self.head = head
        if isinstance(raw, raw_type):
            # All IHwpCtrl members are obtained by name from the installed
            # Hangul COM server. No version-specific DISPIDs or generated COM
            # module are needed. Keep the Automation path for other adapters.
            try:
                self.ids = {name: raw.GetIDsOfNames(name) for name in
                            ("CtrlID", "Next", "UserDesc", "GetAnchorPos")}
                self.head = raw
            except pythoncom.com_error:
                pass

    def prop(self, control, name):
        if self.ids:
            return control.Invoke(self.ids[name], 0, pythoncom.DISPATCH_PROPERTYGET, True)
        return getattr(control, name, None)

    def controls(self, limit):
        control = self.head
        count = 0
        while control is not None:
            count += 1
            if limit is not None and count >= limit:
                raise HwpAutomationError("Control traversal limit exceeded")
            yield control, str(self.prop(control, "CtrlID") or "<unknown>")
            control = self.prop(control, "Next")

    def key(self, control, cid):
        description = str(self.prop(control, "UserDesc") or "").strip()
        return f"{cid}:{description}" if description else cid

    def anchor(self, control, mode):
        if self.ids:
            pos = control.Invoke(self.ids["GetAnchorPos"], 0, pythoncom.DISPATCH_METHOD, True, mode)
            if self.item_id is None:
                self.item_id = pos.GetIDsOfNames("Item")
            flags = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET
            return tuple(int(pos.Invoke(self.item_id, 0, flags, True, key)) for key in ("List", "Para", "Pos"))
        pos = control.GetAnchorPos(mode)
        return tuple(int(pos.Item(key)) for key in ("List", "Para", "Pos"))

    def anchored_controls(self, limit, *, transfer, control_ids=_MOVABLE):
        for control, cid in self.controls(limit):
            if cid in control_ids and not (transfer and cid == "tbl" and self.anchor(control, 0)[0] != 0):
                yield control, cid, self.anchor(control, 1)


def count_control_ranges(hwp, ranges, *, transfer, limit, control_ids=_MOVABLE):
    previous_end = None
    for start, end in ranges:
        if start[0] != end[0] or start >= end:
            raise HwpAutomationError(f"Unsafe control range: start={start!r}, end={end!r}")
        if previous_end is not None and start < previous_end:
            raise HwpAutomationError("Control ranges overlap or are unordered")
        previous_end = end
    starts = tuple(start for start, _ in ranges)
    counts = [Counter() for _ in ranges]
    chain = ControlChain(hwp.automation.HeadCtrl)
    for control, cid, anchor in chain.anchored_controls(limit, transfer=transfer, control_ids=control_ids):
        index = bisect_right(starts, anchor) - 1
        if index >= 0 and anchor < ranges[index][1]:
            counts[index][chain.key(control, cid)] += 1
    return tuple(dict(sorted(c.items())) for c in counts)


def count_controls(hwp, *, transfer: bool, limit: int):
    chain = ControlChain(hwp.automation.HeadCtrl)
    counts = Counter()
    for control, cid in chain.controls(limit):
        if transfer and (cid not in _MOVABLE or (cid == "tbl" and chain.anchor(control, 0)[0] != 0)):
            continue
        counts[chain.key(control, cid)] += 1
    return dict(sorted(counts.items()))


@dataclass
class ControlLayout:
    field_anchors: list[Position] = field(default_factory=list)
    first_movable: dict[tuple[int, int], int] = field(default_factory=dict)
    totals: Counter = field(default_factory=Counter)
    owned_anchors: list[tuple[Position, str]] = field(default_factory=list)

    @classmethod
    def read(cls, hwp, *, inventory: bool, limit: int | None = None):
        result = cls()
        chain = ControlChain(hwp.automation.HeadCtrl)
        for control, cid in chain.controls(limit):
            if inventory:
                key = chain.key(control, cid)
                result.totals[key] += 1
            if cid == "%clk" or cid in _MOVABLE:
                direct = chain.anchor(control, 0)
                if cid == "%clk":
                    if direct[0] == 0:
                        result.field_anchors.append(direct)
                else:
                    if direct[0] == 0:
                        paragraph = direct[:2]
                        result.first_movable[paragraph] = min(result.first_movable.get(paragraph, direct[2]), direct[2])
                    # Nested tables normalize during native Paste. Equations
                    # and drawings stay significant inside every list.
                    if inventory and (cid != "tbl" or direct[0] == 0):
                        result.owned_anchors.append((chain.anchor(control, 1), key))
        return result
