"""Import-specific diagnostic context and the existing final audit payload.

Shared text-free fingerprints/JSONL primitives remain in app.hwp.diagnostics
so the low-level adapter never imports a service policy module.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import TYPE_CHECKING

from app.hwp.diagnostics import safe_summary, structural
from app.hwp.transaction import sha256

if TYPE_CHECKING:
    from .live_import import LiveImportSession
    from .import_boundaries import ImportBoundaryPlan


def collect_range_diagnostics(session: LiveImportSession, boundaries: ImportBoundaryPlan):
    copy_ranges = boundaries.copy_ranges
    integrity_ranges = boundaries.integrity_ranges
    copy_summaries = None
    anchor_exclusions = None
    if session.diagnostic is not None:
        # Before selection: traversal can disturb HWP's selection.
        copy_summaries = session._transfer_control_summaries_for_ranges(tuple(copy_ranges))
        anchor_reader = getattr(session.hwp, "transfer_anchor_exclusions", None)
        if callable(anchor_reader):
            anchor_exclusions = anchor_reader(tuple(copy_ranges), tuple(integrity_ranges))
    return copy_summaries, anchor_exclusions


def record_unit_ranges(
    session: LiveImportSession, *, unit_index, unit, next_id, source, child_ids,
    boundaries: ImportBoundaryPlan, expected_controls, copy_summaries, anchor_exclusions,
) -> None:
    semantic_ranges = boundaries.semantic_ranges
    copy_ranges = boundaries.copy_ranges
    integrity_ranges = boundaries.integrity_ranges
    start, end = copy_ranges[unit_index]
    if session.diagnostic is not None:
        session.diagnostic.context = {
            "question_id": next_id if unit.kind != "group" else None,
            "group_id": next_id if unit.kind == "group" else None,
            "unit_id": next_id, "child_ids": child_ids,
            "source": str(Path(source).resolve()),
            "source_document_id": getattr(session.source_document, "DocumentID", None),
            "target_document_id": getattr(session.target_document, "DocumentID", None),
            "semantic_range": semantic_ranges[unit_index], "copy_range": (start, end),
            "integrity_range": integrity_ranges[unit_index],
        }
        copy_summary = copy_summaries[unit_index]
        range_warnings = []
        if (start, end) != integrity_ranges[unit_index]:
            range_warnings.append("COPY_INTEGRITY_RANGE_DIFFER")
        excluded = None if anchor_exclusions is None else anchor_exclusions[unit_index]
        if excluded or any(count > expected_controls.get(key, 0) for key, count in copy_summary.items()):
            range_warnings.append("INTEGRITY_MAY_EXCLUDE_COPY_ANCHORS")
        session._diag("ranges", warnings=range_warnings,
                   copy_anchors_outside_integrity=excluded,
                   copy_controls=safe_summary(copy_summary),
                   integrity_controls=safe_summary(expected_controls),
                   copy_structural=structural(copy_summary),
                   integrity_structural=structural(expected_controls),
                   shared_boundary_matches=(None if unit_index == 0 or
                       semantic_ranges[unit_index - 1][1] != semantic_ranges[unit_index][0]
                       else copy_ranges[unit_index - 1][1] == start))


def write_import_audit(
    session: LiveImportSession, *, used_builtin_template, final_check_added,
    disk_final_check_layout, insertion_position, structure_analyses, disk_order, controls,
    numbering_style, numbering_style_source, final_block_expected_controls,
) -> None:
    audit = {
        "method": "automatic block detection -> verified native Copy/Paste",
        "template": str(session.template_path),
        "output": str(session.output_path),
        "backup": str(session.backup_path),
        "analysis_profile": session.school_profile.display_name,
        "document_type": asdict(session.document_type),
        "template_policy": (
            "user-supplied template; only explicitly selected areas may be removed"
        ),
        "numbering_style_source": numbering_style_source,
        "numbering_style": numbering_style.to_dict(),
        "final_block_added": final_check_added,
        "final_block_expected_controls": final_block_expected_controls,
        "insertion_position": insertion_position,
        "questions": [asdict(item) for item in session.registered],
        "structure_analyses": [
            asdict(item) for item in structure_analyses
        ],
        "order": list(disk_order),
        "question_controls": controls,
        "output_sha256": sha256(session.output_path),
    }
    audit_path = session.output_path.with_suffix(session.output_path.suffix + ".import.json")
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
