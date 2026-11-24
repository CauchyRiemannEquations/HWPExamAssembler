from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from app.hwp import (
    HwpApplication,
    QuestionFieldManager,
    child_question_field,
    create_timestamped_backup,
    load_numbering_profile,
    renumber_visible_questions,
)
from app.hwp.fields import TERMINAL_FIELD, question_field
from app.hwp.diagnostics import DiagnosticLog, comparison, fingerprint, safe_error
from app.hwp.transaction import sha256
from . import import_diagnostics, native_transfer, template_operations
from .registered_template import RegisteredTemplate
from .format_analysis import capture_format_report, compare_output
from .native_transfer import NativeBlockIntegrityError
from .import_boundaries import plan_import_boundaries
from .auto_qid import (
    detect_questions_from_records,
    paragraph_text,
    scan_paragraphs,
    strip_literal_number_prefixes,
)
from .school_profile import load_school_profile
from .school_profile import SubjectLayoutProfile
from .structure_analysis import (
    StructureAnalysis,
    analyze_structure,
    plan_import_units,
    validate_import_units,
)


@dataclass(frozen=True)
class RegisteredQuestion:
    id: str
    author: str
    source: str
    preview: str
    numbered: bool
    selected_position: tuple[int, int, int, int, int, int]
    control_delta: dict[str, int]
    kind: str = "question"
    source_label: str = ""
    child_ids: tuple[str, ...] = ()
    objective_count: int = 0
    written_count: int = 0
    manual_required: bool = False
    import_error: str = ""


class LiveImportSession:
    """Own documents/state and orchestrate native question imports.

    Boundary, transfer and template helpers borrow this session; they never
    retain handles or create a second owner. Private delegation methods stay
    available for existing callers and transaction test seams.
    """

    def __init__(self, project_root: str | Path, *, diagnostic_dir: str | Path | None = None) -> None:
        self.diagnostic = DiagnosticLog(diagnostic_dir) if diagnostic_dir is not None else None
        self._diagnostic_stage = "idle"
        self._diagnostic_comparison: dict = {}
        self.project_root = Path(project_root).resolve()
        self.work_dir = self.project_root / "work"
        self.hwp: HwpApplication | None = None
        self.target_document = None
        self.source_document = None
        self.source_path: Path | None = None
        self.template_path: Path | None = None
        self.output_path: Path | None = None
        self.backup_path: Path | None = None
        self.insertion_position: tuple[int, int, int] | None = None
        self._target_control_totals: dict[str, int] | None = None
        self._target_sentinel_field: str | None = None
        self.registered: list[RegisteredQuestion] = []
        self.structure_analyses: list[StructureAnalysis] = []
        self.on_progress: Callable[[str], None] | None = None
        self.school_profile = load_school_profile(
            self.project_root / "project" / "school_profile.json"
        )
        self.document_type: SubjectLayoutProfile = (
            self.school_profile.document_type("none")
        )
        self.numbering_style = load_numbering_profile(
            self.project_root / "project" / "target_numbering_style.json"
        )
        self.numbering_style_source = "generic-fallback"
        self.template_analysis: template_operations.TemplateAnalysis | None = None
        self.template_spacing_learned = False
        self.template_ready = False
        self.registration = None
        self.format_report = None
        self.format_check = None
        self.final_block_path: Path | None = None
        self.final_block_expected_controls: dict[str, int] = {}
        self.final_block_text = ""
        self.final_block_added = False
        self._use_builtin_school_template = False
        self._final_check_added = False
        self._final_check_layout: dict[str, int] | None = None

    def _diag(self, event: str, **data) -> None:
        if self.diagnostic is not None:
            self.diagnostic.emit(event, **data)

    def _stage(self, stage: str) -> None:
        self._diagnostic_stage = stage
        self._diag("stage", stage=stage)

    def _allocate_question_id(self, reserved: tuple[str, ...] = ()) -> str:
        used = {
            value
            for item in self.registered
            for value in (item.id, *item.child_ids)
        }
        used.update(reserved)
        index = 1
        while f"Q{index:03d}" in used:
            index += 1
        return f"Q{index:03d}"

    def _allocate_group_id(self) -> str:
        used = {item.id for item in self.registered}
        index = 1
        while f"G{index:03d}" in used:
            index += 1
        return f"G{index:03d}"

    def _progress(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    def _require_target_bridge(
        self, *, operation: str, require_terminal: bool = False
    ) -> None:
        """Prove IHwpObject actions address target, not just the active tab."""
        if self.hwp is None or self.target_document is None:
            raise RuntimeError("수합 대상 문서가 열려 있지 않습니다.")
        self.hwp.activate_document(self.target_document)
        self.hwp.require_active_document(
            self.target_document, operation=operation
        )
        automation = getattr(self.hwp, "automation", None)
        field_exists = getattr(automation, "FieldExist", None)
        if not callable(field_exists):
            return
        required = []
        if self._target_sentinel_field:
            required.append(self._target_sentinel_field)
        if require_terminal:
            required.append(TERMINAL_FIELD)
        missing = [name for name in required if not bool(field_exists(name))]
        if missing:
            # XHwpDocuments and the global IHwpObject bridge can settle at
            # different times, especially immediately after a complex Copy
            # from another tab. Wait for the target's private field graph,
            # not merely Active_XHwpDocument, before allowing a mutation.
            deadline = time.perf_counter() + 1.5
            while missing and time.perf_counter() < deadline:
                time.sleep(0.025)
                self.hwp.activate_document(
                    self.target_document, timeout_seconds=0.4
                )
                self.hwp.require_active_document(
                    self.target_document, operation=operation + " 재확인"
                )
                missing = [
                    name for name in required if not bool(field_exists(name))
                ]
        if missing:
            raise RuntimeError(
                f"{operation}: 대상 문서 식별 필드를 찾지 못했습니다: "
                + ", ".join(missing)
            )

    def _require_non_target_bridge(
        self, document: object, *, operation: str
    ) -> None:
        """Prove global HWP actions left target before touching a temp tab."""
        if self.hwp is None:
            raise RuntimeError("한글 Automation이 실행 중이 아닙니다.")
        self.hwp.activate_document(document)
        self.hwp.require_active_document(document, operation=operation)
        automation = getattr(self.hwp, "automation", None)
        field_exists = getattr(automation, "FieldExist", None)
        if not self._target_sentinel_field or not callable(field_exists):
            return
        deadline = time.perf_counter() + 1.5
        while (
            bool(field_exists(self._target_sentinel_field))
            and time.perf_counter() < deadline
        ):
            time.sleep(0.025)
            self.hwp.activate_document(document)
            self.hwp.require_active_document(
                document, operation=operation + " 재확인"
            )
        if bool(field_exists(self._target_sentinel_field)):
            raise RuntimeError(
                f"{operation}: 한글 명령 연결이 대상 문서에 남아 있어 "
                "임시 원본을 안전하게 조작할 수 없습니다."
            )

    def _transfer_control_summary(self) -> dict[str, int]:
        if self.hwp is None:
            raise RuntimeError("한글 Automation이 실행 중이 아닙니다.")
        method = getattr(self.hwp, "transfer_control_summary", None)
        if callable(method):
            return method()
        return self.hwp.control_summary()

    def _transfer_control_summaries_for_ranges(
        self,
        ranges: tuple[
            tuple[tuple[int, int, int], tuple[int, int, int]], ...
        ],
    ) -> tuple[dict[str, int], ...]:
        if self.hwp is None:
            raise RuntimeError("한글 Automation이 실행 중이 아닙니다.")
        method = getattr(self.hwp, "transfer_control_summaries_for_ranges", None)
        if callable(method):
            return method(ranges)
        return self.hwp.control_summaries_for_ranges(ranges)

    def _append_copied_block(
        self,
        question_id: str,
        expected_controls: dict[str, int] | None = None,
        expected_text: str | None = None,
        child_fields: tuple[tuple[str, int], ...] = (),
    ) -> dict[str, int]:
        return native_transfer.append_copied_block(
            self, question_id, expected_controls, expected_text, child_fields,
        )

    def _append_copied_block_core(
        self,
        question_id: str,
        expected_controls: dict[str, int] | None = None,
        expected_text: str | None = None,
        child_fields: tuple[tuple[str, int], ...] = (),
    ) -> dict[str, int]:
        return native_transfer.append_copied_block_core(
            self, question_id, expected_controls, expected_text, child_fields,
            field_manager_factory=QuestionFieldManager,
            paragraph_reader=paragraph_text,
        )

    def _append_manual_placeholder(
        self,
        question_id: str,
        *,
        child_fields: tuple[tuple[str, int], ...] = (),
    ) -> None:
        return native_transfer.append_manual_placeholder(
            self, question_id, child_fields=child_fields,
            field_manager_factory=QuestionFieldManager,
        )

    def _builtin_template_path(self) -> Path:
        return template_operations.builtin_template_path(self)

    def _prepare_school_template(self) -> None:
        return template_operations.prepare_school_template(self)

    def _append_final_check_box(self) -> None:
        return template_operations.append_final_check_box(self)

    def _prepare_blank_template(self) -> None:
        return template_operations.prepare_blank_template(self)

    def capture_selected_final_block(self) -> Path:
        return template_operations.capture_user_selected_final_block(self)

    def delete_selected_template_area(self) -> str:
        return template_operations.delete_selected_template_area(self)

    def select_detected_example_area(
        self,
    ) -> tuple[int, int, int, int, int, int]:
        return template_operations.select_detected_example_area(self)

    def finalize_template_setup(self) -> None:
        return template_operations.finalize_template_setup(self)

    def start(
        self,
        template: str | Path | None,
        output: str | Path,
        *,
        document_type_key: str = "none",
        visible: bool = True,
        on_progress: Callable[[str], None] | None = None,
        defer_template_setup: bool = False,
        registration: str | Path | None = None,
    ) -> template_operations.TemplateAnalysis:
        if self.hwp is not None:
            raise RuntimeError("이미 수합 세션이 실행 중입니다.")
        if not str(template or "").strip():
            raise ValueError("공개 버전에서는 사용할 HWP/HWPX 양식을 먼저 선택해야 합니다.")
        source_template = Path(str(template)).resolve()
        if source_template.suffix.lower() not in {".hwp", ".hwpx"}:
            raise ValueError("시험지 양식은 HWP 또는 HWPX 파일이어야 합니다.")
        self.document_type = self.school_profile.document_type(document_type_key)
        destination = Path(output).resolve()
        if not source_template.is_file():
            raise FileNotFoundError(source_template)
        if source_template == destination:
            raise ValueError("양식 원본과 새 수합본은 같은 파일일 수 없습니다.")
        if destination.suffix.lower() != source_template.suffix.lower():
            destination = destination.with_suffix(source_template.suffix)
        if destination.exists():
            raise FileExistsError(f"기존 파일에는 저장하지 않습니다: {destination}")
        plan = RegisteredTemplate(registration, source_template, self.work_dir) if registration is not None else None
        destination.parent.mkdir(parents=True, exist_ok=True)
        working = plan.directory / destination.name if plan else destination
        with working.open("xb") as stream, source_template.open("rb") as original:
            shutil.copyfileobj(original, stream)
        if plan and sha256(working) != plan.digest:
            raise ValueError("복사하는 중 등록 양식이 변경되었습니다. 양식을 다시 선택하세요.")
        backup = create_timestamped_backup(working, destination.parent / "backup")
        working.chmod(working.stat().st_mode | stat.S_IWRITE)

        self.on_progress = on_progress
        self.template_path = source_template
        self.output_path = destination
        self.backup_path = backup
        self.template_ready = False
        self.registration = plan
        self.format_report = self.format_check = None
        self.template_analysis = None
        self.template_spacing_learned = False
        self.final_block_path = None
        self.final_block_expected_controls = {}
        self.final_block_text = ""
        self.final_block_added = False
        self._use_builtin_school_template = False
        self._final_check_added = False
        self._final_check_layout = None
        self._progress(f"시험지 편집본 열기: {destination.name}")
        self.hwp = HwpApplication(visible=visible)
        self.hwp.diagnostic = self.diagnostic
        try:
            self.hwp.__enter__()
            if self.hwp.security_module_registered is False:
                self._progress(
                    "한글 접근 승인 창이 뜨면 ‘모두 허용’을 한 번 눌러주세요. "
                    "이 수합 세션에서는 다시 묻지 않습니다."
                )
            # The IHwpObject open path avoids an HWP 2022 stall observed on
            # the initial blank XHwpDocument during a visible import session.
            self.hwp.open_via_object(working)
            self.target_document = self.hwp.active_document()
            analysis = template_operations.analyze_user_template(self)
            if plan:
                plan.prepare(self)
            if plan or not defer_template_setup:
                self.finalize_template_setup()
            self._progress(
                f"문항 구성 유형: {self.document_type.label}"
            )
            self.hwp.visual_step(0.12)
            return analysis
        except Exception:
            self.hwp.close()
            self.hwp = None
            self.registration = None
            raise

    def _apply_document_type_warnings(
        self, analysis: StructureAnalysis
    ) -> StructureAnalysis:
        """Cross-check the selected type without forcing unsafe regrouping."""
        selected = self.document_type
        if selected.key == "none":
            return analysis
        warnings = list(analysis.warnings)
        if (
            selected.expected_structure_modes
            and analysis.suggested_mode not in selected.expected_structure_modes
        ):
            warnings.append(
                f"선택한 ‘{selected.label}’과 자동 분석 결과가 다릅니다. "
                "묶음 미리보기에서 범위를 확인하세요."
            )
        if selected.group_policy == "required" and not analysis.groups:
            warnings.append(
                f"‘{selected.label}’을 선택했지만 공통 묶음 표지를 찾지 못했습니다."
            )
        if selected.group_policy == "forbidden" and analysis.groups:
            warnings.append(
                f"‘{selected.label}’을 선택했지만 공통 묶음이 발견되었습니다. "
                "해당 묶음은 안전을 위해 여전히 한 블록으로 처리합니다."
            )
        return replace(analysis, warnings=tuple(dict.fromkeys(warnings)))

    def _validate_target_identity_graph(
        self, manager: QuestionFieldManager, *, phase: str
    ) -> None:
        """Verify top-level GID/QID order and every group-child relation."""
        expected_order = tuple(item.id for item in self.registered)
        actual_order = tuple(manager.ordered_question_ids())
        self._diag("qid_order", phase=phase, expected_order=expected_order, actual_order=actual_order,
                   match=actual_order == expected_order)
        if actual_order != expected_order:
            raise RuntimeError(
                f"{phase} 문항 ID 순서 검증 실패: "
                f"expected={expected_order!r}, actual={actual_order!r}"
            )
        expected_all_children: list[tuple[str, str]] = []
        for item in self.registered:
            actual_children = manager.child_question_fields(item.id)
            actual_child_ids = tuple(child.question_id for child in actual_children)
            self._diag("qid_children", phase=phase, unit_id=item.id, expected=item.child_ids,
                       actual=actual_child_ids, match=actual_child_ids == item.child_ids)
            if actual_child_ids != item.child_ids:
                raise RuntimeError(
                    f"{phase} {item.id} 하위 QID 검증 실패: "
                    f"expected={item.child_ids!r}, actual={actual_child_ids!r}"
                )
            if item.child_ids:
                if sum(child.kind == "objective" for child in actual_children) != item.objective_count:
                    raise RuntimeError(
                        f"{phase} {item.id} 선택형 하위 문항 수 검증 실패"
                    )
                if sum(child.kind == "written" for child in actual_children) != item.written_count:
                    raise RuntimeError(
                        f"{phase} {item.id} 서답형 하위 문항 수 검증 실패"
                    )
            expected_all_children.extend((item.id, child_id) for child_id in item.child_ids)
        actual_all_children = [
            (child.group_id, child.question_id)
            for child in manager.child_question_fields()
        ]
        if actual_all_children != expected_all_children:
            raise RuntimeError(
                f"{phase} 전체 하위 QID 소유 관계 검증 실패: "
                f"expected={expected_all_children!r}, actual={actual_all_children!r}"
            )

    def open_source(self, source: str | Path) -> None:
        if self.hwp is None:
            raise RuntimeError("먼저 시험지 수합 세션을 시작하세요.")
        if not self.template_ready:
            raise RuntimeError(
                "먼저 실제 한글 창에서 양식을 확인하고 ‘양식 정리 완료’를 누르세요."
            )
        self._close_source_document()
        original = Path(source).resolve()
        if not original.is_file():
            raise FileNotFoundError(original)
        runtime_dir = self.work_dir / f"live-import-{uuid.uuid4().hex}"
        runtime_dir.mkdir(parents=True)
        runtime = runtime_dir / original.name
        shutil.copy2(original, runtime)
        self._progress(f"출제 파일 열기: {original.name}")
        self.source_document = self.hwp.open_additional_document(runtime)
        self.source_path = original
        self._require_non_target_bridge(
            self.source_document, operation="출제 파일 열기 후"
        )
        self.hwp.visual_step(0.12)

    def _close_source_document(self) -> None:
        """Discard one temporary teacher-document tab after its import."""
        if self.hwp is None or self.source_document is None:
            return
        source_document = self.source_document
        close_document = getattr(self.hwp, "close_document", None)
        try:
            self._require_non_target_bridge(
                source_document, operation="출제 파일 임시 탭 닫기 전"
            )
            if callable(close_document):
                close_document(source_document, discard_changes=True)
            self.source_document = None
        finally:
            if self.target_document is not None:
                self.hwp.activate_document(self.target_document)

    def import_source_file(
        self,
        source: str | Path,
        *,
        author: str = "",
        on_structure_review: Callable[[StructureAnalysis], bool] | None = None,
    ) -> tuple[RegisteredQuestion, ...]:
        try:
            result = self._import_source_file_impl(
                source,
                author=author,
                on_structure_review=on_structure_review,
            )
        except BaseException as import_error:
            self._diag("import_error", stage=self._diagnostic_stage, error=safe_error(import_error))
            try:
                self._close_source_document()
            except Exception as cleanup_error:
                import_error.add_note(
                    "출제 파일 임시 탭 정리도 실패했습니다: "
                    f"{cleanup_error}"
                )
            raise
        else:
            self._stage("source_document_cleanup")
            try:
                self._close_source_document()
            except Exception as error:
                self._diag("source_cleanup_result", success=False, error=safe_error(error))
                raise
            self._diag("source_cleanup_result", success=True)
            return result

    def _import_source_file_impl(
        self,
        source: str | Path,
        *,
        author: str = "",
        on_structure_review: Callable[[StructureAnalysis], bool] | None = None,
    ) -> tuple[RegisteredQuestion, ...]:
        """Detect and import every question block from one teacher file."""
        started_at = time.perf_counter()
        self.open_source(source)
        if self.diagnostic is not None:
            self._diag("source_opened", source=str(Path(source).resolve()), source_sha256=sha256(Path(source)))
        if self.hwp is None or self.source_document is None:
            raise RuntimeError("출제 파일을 열지 못했습니다.")

        self._require_non_target_bridge(
            self.source_document, operation="출제 파일 문항 탐지 전"
        )
        self._progress(f"{Path(source).name} 전체에서 문항 번호 찾기")
        self._stage("source_detection")
        records = scan_paragraphs(self.hwp)
        detected = detect_questions_from_records(records)
        if not detected:
            raise ValueError(
                "문항 시작을 자동으로 찾지 못했습니다. "
                "문항이 한글 문단 번호, '1.'/'1)' 또는 '[서답형 1]'로 시작하는지 확인하세요."
            )
        analysis = analyze_structure(records, detected, self.school_profile)
        analysis = self._apply_document_type_warnings(analysis)
        self.structure_analyses.append(analysis)
        self._progress(
            f"시험지 구조 분석: {analysis.summary()} · {analysis.suggested_mode}"
        )
        if on_structure_review is not None and not on_structure_review(analysis):
            self._progress(f"{Path(source).name} 수합을 사용자가 취소했습니다.")
            return ()
        units = plan_import_units(detected, analysis)
        validate_import_units(detected, analysis, units)
        self._progress(
            f"채점 문항 {len(detected)}개를 안전한 이동 단위 {len(units)}개로 구성 · "
            "누락·중복·겹침 검증 완료"
        )
        if analysis.fixed_blocks:
            self._progress(
                f"고정 안내 후보 {len(analysis.fixed_blocks)}개는 출제 파일에서 복사하지 않음"
            )
        # Work only on the temporary source copy. Directly typed numbers are
        # removed so the target's school numbering style does not duplicate
        # them; native paragraph numbers are replaced when finish() runs.
        self._require_non_target_bridge(
            self.source_document, operation="출제 파일 원본 번호 정리 전"
        )
        self._stage("source_copy_number_cleanup")
        strip_literal_number_prefixes(self.hwp, detected)
        self._stage("range_planning")

        boundaries = plan_import_boundaries(self, units, detected)
        copy_ranges = boundaries.copy_ranges
        integrity_ranges = boundaries.integrity_ranges
        expected_by_question = self._transfer_control_summaries_for_ranges(
            tuple(integrity_ranges)
        )
        copy_summaries, anchor_exclusions = import_diagnostics.collect_range_diagnostics(self, boundaries)
        self._progress("원본 수식·그림·표를 한 번에 집계 완료")

        imported: list[RegisteredQuestion] = []
        for unit_index, (unit, (start, end), expected_controls) in enumerate(zip(
            units, copy_ranges, expected_by_question, strict=True
        )):
            child_ids: tuple[str, ...] = ()
            child_field_specs: tuple[tuple[str, int], ...] = ()
            if unit.kind == "group":
                next_id = self._allocate_group_id()
                allocated_children: list[str] = []
                allocated_fields: list[tuple[str, int]] = []
                for child in unit.questions:
                    child_id = self._allocate_question_id(tuple(allocated_children))
                    allocated_children.append(child_id)
                    allocated_fields.append(
                        (
                            child_question_field(next_id, child_id, child.kind),
                            child.paragraph - unit.start_paragraph,
                        )
                    )
                child_ids = tuple(allocated_children)
                child_field_specs = tuple(allocated_fields)
            else:
                next_id = self._allocate_question_id()
            import_diagnostics.record_unit_ranges(
                self, unit_index=unit_index, unit=unit, next_id=next_id,
                source=source, child_ids=child_ids, boundaries=boundaries,
                expected_controls=expected_controls, copy_summaries=copy_summaries,
                anchor_exclusions=anchor_exclusions,
            )
            transfer = native_transfer.transfer_import_unit(
                self, next_id, source, start, end, expected_controls, child_field_specs,
            )
            selected_position = transfer.selected_position
            selected_text = transfer.selected_text
            delta = transfer.control_delta
            manual_required = transfer.manual_required
            import_error = transfer.import_error
            result = RegisteredQuestion(
                id=next_id,
                author=author.strip(),
                source=str(Path(source).resolve()),
                preview=self._preview(selected_text),
                numbered=unit.numbered,
                selected_position=selected_position,
                control_delta=delta,
                kind=unit.kind,
                source_label=unit.label,
                child_ids=child_ids,
                objective_count=unit.objective_count,
                written_count=unit.written_count,
                manual_required=manual_required,
                import_error=import_error,
            )
            self.registered.append(result)
            imported.append(result)
            if not manual_required:
                verified_text = ", ".join(
                    f"{key} {count}개" for key, count in delta.items()
                ) or "별도 개체 없음"
                self._progress(
                    f"{next_id} 붙여넣기 개체 검증 완료: {verified_text}"
                )
            self.hwp.visual_step(0.06 if self.hwp.visible else 0.0)

        if self.diagnostic is not None:
            self.diagnostic.context = {}
        self._stage("import_save")
        self.hwp.activate_document(self.target_document)
        self.hwp.save_document(self.target_document)
        self._diag("import_saved")
        manual_count = sum(item.manual_required for item in imported)
        manual_text = (
            f" · 직접 입력할 빈 자리 {manual_count}개" if manual_count else ""
        )
        self._progress(
            f"{Path(source).name}의 문항 {len(imported)}개를 수합본에 추가했습니다"
            f"{manual_text} ({time.perf_counter() - started_at:.1f}초)."
        )
        return tuple(imported)

    @staticmethod
    def _preview(text: str) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        return compact[:119] + ("…" if len(compact) > 119 else "")

    def register_selection(
        self,
        question_id: str,
        *,
        author: str = "",
        numbered: bool = True,
    ) -> RegisteredQuestion:
        if self.hwp is None or self.source_document is None or self.target_document is None:
            raise RuntimeError("출제 파일을 연 뒤 한글에서 문항을 선택하세요.")
        normalized_id = question_id.strip().upper()
        if not re.fullmatch(r"Q[A-Z0-9_]{3,31}", normalized_id):
            raise ValueError("문항 ID는 Q로 시작하는 영문·숫자 형식이어야 합니다.")
        if normalized_id in {item.id for item in self.registered}:
            raise ValueError(f"이미 등록한 문항 ID입니다: {normalized_id}")

        self._require_non_target_bridge(
            self.source_document, operation=f"{normalized_id} 수동 선택 확인 전"
        )
        selected_position = self.hwp.selected_position()
        if selected_position is None:
            raise ValueError("한글 원본 창에서 문항 전체를 블록 선택하세요.")
        selected_text = self.hwp.selected_plain_text()
        if not selected_text.strip():
            raise ValueError("선택한 문항에서 텍스트를 읽을 수 없습니다.")
        range_start, range_end = sorted(
            (selected_position[:3], selected_position[3:])
        )
        expected_controls = self._transfer_control_summaries_for_ranges(
            ((range_start, range_end),)
        )[0]
        if self.diagnostic is not None:
            self.diagnostic.context = {
                "unit_id": normalized_id, "question_id": normalized_id, "group_id": None,
                "attempt": 1, "source": str(self.source_path), "semantic_range": None,
                "copy_range": (range_start, range_end), "integrity_range": (range_start, range_end),
            }
        self._diagnostic_comparison = {}
        self._diag("source_selection", selected_position=selected_position,
                   source_selected_text=fingerprint(selected_text))
        self._stage("copy")
        # Control traversal can disturb HWP's active block selection. Restore
        # the exact range so Copy follows Select without another inspection.
        selected_position = self.hwp.select_position_range(range_start, range_end)
        self._progress(f"{normalized_id} 문항 블록 선택 확인")
        self.hwp.copy_selection()
        self.hwp.visual_step(0.02 if self.hwp.visible else 0.01)
        self._progress(f"{normalized_id} 네이티브 블록 복사")

        self.hwp.activate_document(self.target_document)
        active = self.hwp.active_document()
        if int(active.DocumentID) != int(self.target_document.DocumentID):
            raise RuntimeError(
                "한글 대상 문서 활성화 실패: "
                f"active={active.DocumentID}, target={self.target_document.DocumentID}"
            )
        delta = self._append_copied_block(
            normalized_id, expected_controls, expected_text=selected_text
        )
        self._progress(
            "대상 문서 상태: "
            f"edit_mode={self.target_document.EditMode}, "
            f"modified={self.target_document.Modified}"
        )
        # FileSave acts on the globally active editor and can race with a
        # recently activated source/target window. Save the exact target
        # IXHwpDocument instead.
        self.hwp.save_document(self.target_document)
        result = RegisteredQuestion(
            id=normalized_id,
            author=author.strip(),
            source=str(self.source_path),
            preview=self._preview(selected_text),
            numbered=numbered,
            selected_position=selected_position,
            control_delta=delta,
            objective_count=1 if numbered else 0,
            written_count=0 if numbered else 1,
        )
        self.registered.append(result)
        self._diag("question_result", success=True, placeholder=False)
        self._progress(f"{normalized_id} 시험지 문제 영역에 붙여넣기 완료")
        self.hwp.visual_step(0.08)
        self._require_non_target_bridge(
            self.source_document, operation=f"{normalized_id} 등록 완료 후"
        )
        return result

    def finish(self, *, open_after: bool = True) -> Path:
        if self.diagnostic is not None:
            self.diagnostic.context = {}
        self._stage("finish")
        try:
            result = self._finish_impl(open_after=open_after)
        except Exception as error:
            self._diag("finish_result", success=False, stage=self._diagnostic_stage, error=safe_error(error))
            raise
        self._diag("finish_result", success=True)
        return result

    def _finish_impl(self, *, open_after: bool = True) -> Path:
        if self.hwp is None or self.target_document is None or self.output_path is None:
            raise RuntimeError("실행 중인 수합 세션이 없습니다.")
        if not self.template_ready:
            raise RuntimeError("양식 정리를 완료한 뒤 수합본을 저장하세요.")
        if not self.registered:
            raise ValueError("등록된 문항이 없습니다.")
        self._require_target_bridge(operation="최종 저장 전")
        manager = QuestionFieldManager(self.hwp)
        self._validate_target_identity_graph(manager, phase="저장 전")
        plan = self.registration
        if plan:
            manager = plan.assemble(self, manager)
        self.hwp.move_to_end()
        if not self.hwp.automation.FieldExist(TERMINAL_FIELD):
            manager.create_at_current_position(TERMINAL_FIELD)
        profile = self.numbering_style
        numbered_ids = tuple(
            item.id
            for item in self.registered
            if item.numbered or item.objective_count > 0
        )
        self._stage("final_numbering")
        renumber_visible_questions(
            manager,
            numbered_ids,
            style=profile,
            on_progress=self._progress,
            step_delay=0.05 if self.hwp.visible else 0.0,
        )
        self._stage("final_block")
        self._append_final_check_box()
        self._require_target_bridge(
            operation="최종 개체 검증 전", require_terminal=True
        )
        self._stage("final_controls")
        controls = manager.question_control_summaries()
        for item in self.registered:
            self._diag("final_question_controls", unit_id=item.id,
                       **comparison(item.control_delta, controls.get(item.id, {}), None))
        registered_control_mismatches = {
            item.id: {
                "registered": item.control_delta,
                "current": controls.get(item.id, {}),
            }
            for item in self.registered
            if item.control_delta != controls.get(item.id, {})
        }
        if registered_control_mismatches:
            raise NativeBlockIntegrityError(
                "최종 저장 전 문항별 네이티브 개체 검증 실패: "
                f"{registered_control_mismatches!r}"
            )
        self._progress("수합본 저장")
        verification_path = self.output_path
        if plan:
            verification_path = plan.directory / ("verified" + self.output_path.suffix)
            self.hwp.save_exam_copy(verification_path)
        else:
            self.hwp.save_document(self.target_document)
        expected_order = tuple(manager.ordered_question_ids())
        output = self.output_path
        visible = self.hwp.visible
        insertion_position = self.insertion_position
        target_sentinel_field = self._target_sentinel_field
        structure_analyses = tuple(self.structure_analyses)
        used_builtin_template = False
        final_block_added = self.final_block_added
        final_block_expected_controls = dict(self.final_block_expected_controls)
        final_block_text = self.final_block_text
        expected_document_controls = (
            dict(self._target_control_totals)
            if self._target_control_totals is not None
            else None
        )
        numbering_style = self.numbering_style
        numbering_style_source = self.numbering_style_source
        self._progress("저장 파일을 같은 한글 세션에서 다시 열어 검증")
        self._stage("save_reopen")
        self.hwp.reset_documents()
        try:
            self.hwp.open(verification_path)
            field_exists = getattr(self.hwp.automation, "FieldExist", None)
            if (
                target_sentinel_field
                and callable(field_exists)
                and not bool(field_exists(target_sentinel_field))
            ):
                raise RuntimeError(
                    "저장 후 대상 문서 식별 필드가 사라졌습니다: "
                    f"{target_sentinel_field}"
                )
            disk_manager = QuestionFieldManager(self.hwp)
            self._validate_target_identity_graph(disk_manager, phase="재개방 후")
            disk_order = tuple(disk_manager.ordered_question_ids())
            disk_controls = disk_manager.question_control_summaries()
            self._diag("saved_qid_verification", expected_order=expected_order, actual_order=disk_order,
                       match=disk_order == expected_order)
            for unit_id in sorted(set(controls) | set(disk_controls)):
                self._diag("saved_question_controls", unit_id=unit_id,
                           **comparison(controls.get(unit_id, {}), disk_controls.get(unit_id, {}), None))
            disk_final_check_layout: dict[str, int] | None = None
            disk_document_controls = self._transfer_control_summary()
            if plan:
                plan.verify_preserved(self.hwp)
            if self.format_report is not None:
                try:
                    self.format_check = compare_output(self.format_report, capture_format_report(self.hwp))
                except Exception as error:
                    self._progress(f"서식 비교 기록을 저장하지 못했습니다: {error}")
            if final_block_added:
                if expected_document_controls is not None and (
                    disk_document_controls != expected_document_controls
                ):
                    raise NativeBlockIntegrityError(
                        "마지막 고정 블록을 포함한 네이티브 개체 수가 저장 전후 달라졌습니다: "
                        f"memory={expected_document_controls!r}, "
                        f"disk={disk_document_controls!r}"
                    )
                expected_final_text = re.sub(r"\s+", " ", final_block_text).strip()
                if expected_final_text:
                    disk_text = re.sub(r"\s+", " ", self.hwp.plain_text()).strip()
                    if expected_final_text not in disk_text:
                        raise NativeBlockIntegrityError(
                            "저장 후 마지막 고정 블록의 텍스트를 찾지 못했습니다."
                        )
        finally:
            self.close()
        if disk_order != expected_order:
            raise RuntimeError(
                "저장 후 QID 검증 실패: "
                f"memory={expected_order}, disk={disk_order}"
            )
        if disk_controls != controls:
            changed_controls = {
                question_id: {
                    "memory": controls.get(question_id, {}),
                    "disk": disk_controls.get(question_id, {}),
                }
                for question_id in sorted(set(controls) | set(disk_controls))
                if controls.get(question_id, {})
                != disk_controls.get(question_id, {})
            }
            raise NativeBlockIntegrityError(
                "저장 후 문항별 네이티브 개체 검증 실패: "
                f"{changed_controls!r}"
            )
        controls = disk_controls
        if plan:
            plan.publish(verification_path, output)
        try:
            if plan and self.format_report is not None:
                self.format_report.save(output.with_suffix(output.suffix + ".format-review.json"))
            if self.format_check is not None:
                output.with_suffix(output.suffix + ".format-check.json").write_text(
                    json.dumps(self.format_check, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            self._progress(f"서식 기록을 저장하지 못했습니다: {error}")
        import_diagnostics.write_import_audit(
            self, used_builtin_template=used_builtin_template,
            final_check_added=final_block_added, disk_final_check_layout=disk_final_check_layout,
            insertion_position=insertion_position, structure_analyses=structure_analyses,
            disk_order=disk_order, controls=controls,
            numbering_style=numbering_style,
            numbering_style_source=numbering_style_source,
            final_block_expected_controls=final_block_expected_controls,
        )
        if visible and open_after:
            os.startfile(output)
        return output

    def close(self) -> None:
        if self.hwp is not None:
            self.hwp.close()
        self.hwp = None
        self.target_document = None
        self.source_document = None
        self.insertion_position = None
        self._target_control_totals = None
        self._target_sentinel_field = None
        self.structure_analyses.clear()
        self.template_ready = False
        self.registration = None
        self.template_analysis = None
        self.template_spacing_learned = False
        self.final_block_path = None
        self.final_block_expected_controls = {}
        self.final_block_text = ""
        self.final_block_added = False
        self._use_builtin_school_template = False
        self._final_check_added = False
        self._final_check_layout = None
