from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

from app.hwp import (
    HwpApplication,
    NumberingStyleProfile,
    QuestionFieldManager,
    apply_reorder_transaction,
    load_numbering_profile,
)
from app.hwp.fields import question_field
from app.hwp.transaction import sha256
from .auto_qid import create_auto_qid_copy
from .school_profile import load_school_profile


@dataclass(frozen=True)
class QuestionSummary:
    id: str
    preview: str
    numbered: bool
    controls: dict[str, int]
    kind: str = "question"
    child_ids: tuple[str, ...] = ()
    objective_count: int = 0
    written_count: int = 0
    manual_required: bool = False
    import_error: str = ""


@dataclass(frozen=True)
class DocumentSnapshot:
    path: Path
    questions: tuple[QuestionSummary, ...]
    content_sha256: str | None = None

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(question.id for question in self.questions)

    @property
    def numbered_ids(self) -> tuple[str, ...]:
        return tuple(
            question.id
            for question in self.questions
            if question.numbered or question.objective_count > 0
        )


class ExamDocumentService:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.numbering_profile_path = (
            self.project_root / "project" / "target_numbering_style.json"
        )
        self.work_dir = self.project_root / "work"
        self.school_profile = load_school_profile(
            self.project_root / "project" / "school_profile.json"
        )
        self._last_inspection: tuple[DocumentSnapshot, dict[str, dict[str, object]]] | None = None

    @staticmethod
    def _preview(text: str, *, limit: int = 90) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact:
            return "(텍스트 미리보기 없음)"
        return compact if len(compact) <= limit else compact[: limit - 1] + "…"

    @staticmethod
    def _copy_snapshot(snapshot: DocumentSnapshot) -> DocumentSnapshot:
        # Each caller owns the mutable control-count dictionaries in its summaries.
        return replace(snapshot, questions=tuple(
            replace(question, controls=question.controls.copy()) for question in snapshot.questions
        ))

    @staticmethod
    def _read_json_sidecar(document: Path, suffix: str) -> dict[str, object]:
        audit_path = document.with_suffix(document.suffix + suffix)
        if not audit_path.is_file():
            return {}
        try:
            data = json.loads(audit_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @classmethod
    def _import_metadata(cls, document: Path) -> dict[str, dict[str, object]]:
        data = cls._read_json_sidecar(document, ".import.json")
        questions = data.get("questions", [])
        return {
            str(item["id"]): item
            for item in questions
            if isinstance(item, dict) and item.get("id")
        }

    def _numbering_profile_for(self, document: Path) -> NumberingStyleProfile:
        """Use the style captured from this document, never another school."""
        for suffix in (".format.json", ".import.json", ".qid.json"):
            data = self._read_json_sidecar(document, suffix)
            value = data.get("numbering_style")
            if isinstance(value, dict):
                try:
                    return NumberingStyleProfile.from_dict(value)
                except (TypeError, ValueError, KeyError):
                    continue
        return load_numbering_profile(self.numbering_profile_path)

    @staticmethod
    def _write_format_profile(
        document: Path,
        profile: NumberingStyleProfile,
        *,
        source: Path,
    ) -> None:
        path = document.with_suffix(document.suffix + ".format.json")
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_document": str(source),
                    "numbering_style": profile.to_dict(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def read_open_questions(self, hwp, manager, order, import_metadata, on_progress=None, *, controls=None):
        """Read question summaries from an exact open document without opening or closing tabs."""
        questions = []
        if controls is None:
            controls = manager.question_control_summaries()
        children_by_group = {}
        for child in manager.child_question_fields():
            children_by_group.setdefault(child.group_id, []).append(child)
        para = hwp.automation.HParameterSet.HParaShape
        action, shape_set = hwp.automation.HAction, para.HSet
        for question_id in order:
            question_text = manager.question_text(question_id)
            preview = self._preview(question_text)
            # Collapse the preceding preview selection before reading paragraph numbering.
            hwp.set_position(manager.position(question_field(question_id)))
            action.GetDefault("ParagraphShape", shape_set)
            children = children_by_group.get(question_id, ())
            question_controls = controls.get(question_id, {})
            metadata = import_metadata.get(question_id, {})
            was_manual = bool(metadata.get("manual_required", False))
            still_blank = not question_text.strip() and not question_controls
            if was_manual and still_blank:
                source_preview = str(
                    metadata.get("preview")
                    or metadata.get("source_label")
                    or "원본 문항 범위"
                )
                preview = f"직접 붙여넣을 원본: {source_preview}"
            questions.append(
                QuestionSummary(
                    id=question_id,
                    preview=preview,
                    numbered=int(para.HeadingType) == 2,
                    controls=question_controls,
                    kind="group" if children else "question",
                    child_ids=tuple(item.question_id for item in children),
                    objective_count=sum(
                        item.kind == "objective" for item in children
                    ),
                    written_count=sum(
                        item.kind == "written" for item in children
                    ),
                    manual_required=was_manual and still_blank,
                    import_error=(
                        str(metadata.get("import_error", ""))
                        if was_manual and still_blank
                        else ""
                    ),
                )
            )
            if on_progress is not None:
                on_progress(f"{question_id} 문항 정보 읽기")
        return questions

    def inspect(
        self,
        document: str | Path,
        *,
        visible: bool = True,
        on_progress: Callable[[str], None] | None = None,
        auto_prepare: bool = True,
    ) -> DocumentSnapshot:
        source = Path(document).resolve()
        started_at = time.perf_counter()
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() not in {".hwp", ".hwpx"}:
            raise ValueError("HWP 또는 HWPX 파일만 열 수 있습니다.")

        import_metadata = self._import_metadata(source)
        cached = self._last_inspection
        if (cached is not None and cached[0].path == source and cached[1] == import_metadata
                and sha256(source) == cached[0].content_sha256):
            snapshot = self._copy_snapshot(cached[0])
            if on_progress is not None:
                on_progress(f"문항 {len(snapshot.questions)}개 읽기 완료")
            return snapshot

        runtime_dir = self.work_dir / f"gui-inspect-{uuid.uuid4().hex}"
        runtime_dir.mkdir(parents=True)
        runtime = runtime_dir / source.name
        shutil.copyfile(source, runtime)
        source_hash = sha256(runtime)

        if on_progress is not None:
            on_progress(f"{source.name} 열기")
        needs_auto_prepare = False
        questions: list[QuestionSummary] = []
        with HwpApplication(visible=visible) as hwp:
            hwp.open(runtime)
            manager = QuestionFieldManager(hwp)
            order = manager.ordered_question_ids()
            if not order:
                needs_auto_prepare = True
            else:
                questions = self.read_open_questions(hwp, manager, order, import_metadata, on_progress)
            hwp.reset_documents()

        if needs_auto_prepare:
            if not auto_prepare:
                raise ValueError("문항 번호를 자동 등록하지 못했습니다.")
            if on_progress is not None:
                on_progress("내부 문항 ID가 없어 자동 등록본을 만드는 중")
            prepared = create_auto_qid_copy(
                source,
                visible=visible,
                on_progress=on_progress,
                numbering_profile_path=self.numbering_profile_path,
            )
            if on_progress is not None:
                on_progress(f"자동 문항 등록 완료: {prepared.name}")
            return self.inspect(
                prepared,
                visible=visible,
                on_progress=on_progress,
                auto_prepare=False,
            )
        if sha256(source) != source_hash:
            raise RuntimeError("읽는 동안 파일이 바뀌었습니다. 저장을 마친 뒤 다시 열어 주세요.")
        snapshot = DocumentSnapshot(path=source, questions=tuple(questions), content_sha256=source_hash)
        self._last_inspection = (self._copy_snapshot(snapshot), import_metadata)
        if on_progress is not None:
            on_progress(
                f"문항 {len(questions)}개 읽기 완료 "
                f"({time.perf_counter() - started_at:.1f}초)"
            )
        return snapshot

    def apply_to_new_file(
        self,
        snapshot: DocumentSnapshot,
        destination: str | Path,
        requested_order: tuple[str, ...],
        *,
        visible: bool = True,
        on_progress: Callable[[str], None] | None = None,
        open_after: bool = True,
    ) -> Path:
        output = Path(destination).resolve()
        if output == snapshot.path:
            raise ValueError("선택한 원본 파일에는 저장할 수 없습니다.")
        if output.exists():
            raise FileExistsError(f"이미 존재하는 파일입니다: {output}")
        # This phase preserves the source document format. HWP↔HWPX format
        # conversion is deliberately not mixed into a reorder transaction.
        if output.suffix.lower() != snapshot.path.suffix.lower():
            output = output.with_suffix(snapshot.path.suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot.path, output)

        numbered = frozenset(snapshot.numbered_ids)
        numbered_in_new_order = tuple(
            question_id
            for question_id in requested_order
            if question_id in numbered
        )
        profile = self._numbering_profile_for(snapshot.path)
        try:
            with HwpApplication(visible=visible) as hwp:
                audit = apply_reorder_transaction(
                    hwp,
                    output,
                    requested_order,
                    numbered_in_new_order,
                    numbering_style=profile,
                    backup_dir=output.parent / "backup",
                    work_dir=self.work_dir,
                    on_progress=on_progress,
                    allow_deletions=True,
                )
        except Exception:
            # The copied output remains byte-equivalent to the selected input
            # because the transaction publishes only after verification.
            raise

        audit_path = output.with_suffix(output.suffix + ".audit.json")
        audit_path.write_text(
            json.dumps(audit.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_format_profile(output, profile, source=snapshot.path)
        if visible and open_after:
            os.startfile(output)
        return output

    def apply_in_place(
        self,
        snapshot: DocumentSnapshot,
        requested_order: tuple[str, ...],
        *,
        visible: bool = True,
        on_progress: Callable[[str], None] | None = None,
        open_after: bool = True,
    ) -> Path:
        """Safely update the current editable copy and reopen it in Hangul.

        ``apply_reorder_transaction`` creates a timestamped byte-for-byte
        backup and edits a separate working copy.  The current document is
        replaced only after save/reopen verification succeeds.
        """
        output = snapshot.path.resolve()
        if not output.is_file():
            raise FileNotFoundError(output)
        if output.suffix.lower() not in {".hwp", ".hwpx"}:
            raise ValueError("HWP 또는 HWPX 편집본만 저장할 수 있습니다.")
        if not os.access(output, os.W_OK):
            raise PermissionError(f"현재 편집본이 읽기 전용입니다: {output}")

        numbered = frozenset(snapshot.numbered_ids)
        numbered_in_new_order = tuple(
            question_id
            for question_id in requested_order
            if question_id in numbered
        )
        profile = self._numbering_profile_for(snapshot.path)
        with HwpApplication(visible=visible) as hwp:
            audit = apply_reorder_transaction(
                hwp,
                output,
                requested_order,
                numbered_in_new_order,
                numbering_style=profile,
                backup_dir=output.parent / "backup",
                work_dir=self.work_dir,
                on_progress=on_progress,
                allow_deletions=True,
                expected_sha256=snapshot.content_sha256,
            )

        audit_path = output.with_suffix(output.suffix + ".audit.json")
        audit_path.write_text(
            json.dumps(audit.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_format_profile(output, profile, source=snapshot.path)
        if open_after:
            os.startfile(output)
        return output

    def refresh_after_external_edit(
        self,
        document: str | Path,
        expected_ids: tuple[str, ...] | DocumentSnapshot,
        *,
        visible: bool = True,
        on_progress: Callable[[str], None] | None = None,
    ) -> DocumentSnapshot:
        """Read a teacher-edited HWP without silently regenerating its QIDs."""
        if on_progress is not None:
            on_progress("한글 직접 편집본의 내부 ID 확인")
        snapshot = self.inspect(
            document,
            visible=visible,
            on_progress=on_progress,
            auto_prepare=False,
        )
        expected_snapshot = (
            expected_ids if isinstance(expected_ids, DocumentSnapshot) else None
        )
        expected_order = (
            expected_snapshot.order
            if expected_snapshot is not None
            else expected_ids
        )
        if len(snapshot.order) != len(set(snapshot.order)):
            raise ValueError("한글 편집 중 내부 문항 ID가 중복되었습니다.")
        missing = tuple(sorted(set(expected_order) - set(snapshot.order)))
        unknown = tuple(sorted(set(snapshot.order) - set(expected_order)))
        if missing or unknown or len(snapshot.order) != len(expected_order):
            raise ValueError(
                "한글 편집 중 문항 경계 ID가 변경되었습니다. "
                f"누락={list(missing)}, 알 수 없음={list(unknown)}. "
                "문항 삭제는 앱의 ‘선택 문항 삭제’를 사용하세요."
            )
        if expected_snapshot is not None:
            expected_children = {
                question.id: question.child_ids
                for question in expected_snapshot.questions
            }
            current_children = {
                question.id: question.child_ids for question in snapshot.questions
            }
            changed_children = {
                question_id: {
                    "expected": expected_children.get(question_id, ()),
                    "actual": current_children.get(question_id, ()),
                }
                for question_id in expected_order
                if expected_children.get(question_id, ())
                != current_children.get(question_id, ())
            }
            if changed_children:
                raise ValueError(
                    "한글 편집 중 공통지문 묶음의 하위 문항 ID가 변경되었습니다. "
                    f"변경={changed_children!r}. 공통지문과 딸린 문항은 앱에서 "
                    "한 묶음으로 이동·삭제하세요."
                )
        if on_progress is not None:
            on_progress("한글 수정본을 현재 기준 문서로 반영")
        return snapshot
