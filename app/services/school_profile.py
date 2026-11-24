from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class SubjectLayoutProfile:
    key: str
    label: str
    density: str
    separator_blank_paragraphs: int
    observed_text_density: float
    rule: str
    expected_structure_modes: tuple[str, ...] = ()
    group_policy: str = "auto"


@dataclass(frozen=True)
class FinalCheckProfile:
    enabled: bool
    title: str
    body: str
    placement: str
    preferred_column: str
    source_control: str
    source_markers: tuple[str, ...]


@dataclass(frozen=True)
class SchoolProfile:
    schema_version: int
    profile_id: str
    school: str
    display_name: str
    status: str
    template_sha256: str
    template_filename: str
    template_contains_examples: bool
    body_start_marker: str
    body_columns: int
    written_labels: tuple[str, ...]
    fixed_phrase_prefixes: tuple[str, ...]
    subject_modes: tuple[str, ...]
    subject_layouts: tuple[SubjectLayoutProfile, ...]
    final_check: FinalCheckProfile

    def subject_layout(self, key: str) -> SubjectLayoutProfile:
        for profile in self.subject_layouts:
            if profile.key == key:
                return profile
        raise ValueError(f"시험지 양식 설정에 없는 문서 유형입니다: {key}")

    @property
    def document_types(self) -> tuple[SubjectLayoutProfile, ...]:
        return self.subject_layouts

    def document_type(self, key: str) -> SubjectLayoutProfile:
        return self.subject_layout(key)


def load_school_profile(path: str | Path) -> SchoolProfile:
    source = Path(path).resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    recognition = data.get("recognition", {})
    template = data.get("template", {})
    layout = data.get("layout", {})
    type_data = layout.get("document_types") or layout.get("subject_profiles", {})
    subject_layouts = tuple(
        SubjectLayoutProfile(
            key=str(key),
            label=str(value["label"]),
            density=str(value["density"]),
            separator_blank_paragraphs=int(
                value["separator_blank_paragraphs"]
            ),
            observed_text_density=float(value.get("observed_text_density", 0.0)),
            rule=str(value["rule"]),
            expected_structure_modes=tuple(
                str(item) for item in value.get("expected_structure_modes", ())
            ),
            group_policy=str(value.get("group_policy", "auto")),
        )
        for key, value in type_data.items()
    )
    if not subject_layouts:
        subject_layouts = (
            SubjectLayoutProfile(
                key="none",
                label="선택 안 함",
                density="source-default",
                separator_blank_paragraphs=0,
                observed_text_density=0.0,
                rule="문서 유형을 강제하지 않음",
            ),
        )
    final_check_data = layout.get("final_check", {})
    profile = SchoolProfile(
        schema_version=int(data["schema_version"]),
        profile_id=str(data["profile_id"]),
        school=str(data["school"]),
        display_name=str(data.get("display_name", "학교 시험지 양식")),
        status=str(data.get("status", "unknown")),
        template_sha256=str(template.get("sha256", "")),
        template_filename=str(template.get("bundled_filename", "")),
        template_contains_examples=bool(template.get("contains_examples", False)),
        body_start_marker=str(template.get("body_start_marker", "")),
        body_columns=int(layout.get("body_columns", 1)),
        written_labels=tuple(str(item) for item in recognition["written_labels"]),
        fixed_phrase_prefixes=tuple(
            str(item) for item in recognition["fixed_phrase_prefixes"]
        ),
        subject_modes=tuple(str(item) for item in data["subject_modes"]),
        subject_layouts=subject_layouts,
        final_check=FinalCheckProfile(
            # The v4 school profile predates the explicit enabled flag.
            enabled=bool(final_check_data.get(
                "enabled", int(data["schema_version"]) < 5 and bool(final_check_data.get("title"))
            )),
            title=str(final_check_data.get("title", "")),
            body=str(
                final_check_data.get(
                    "body",
                    "",
                )
            ),
            placement=str(
                final_check_data.get("placement", "template_preserved")
            ),
            preferred_column=str(final_check_data.get("preferred_column", "template")),
            source_control=str(
                final_check_data.get("source_control", "template")
            ),
            source_markers=tuple(
                str(item)
                for item in final_check_data.get(
                    "source_markers",
                    (),
                )
            ),
        ),
    )
    if profile.schema_version not in {1, 2, 3, 4, 5}:
        raise ValueError(
            f"지원하지 않는 학교 프로필 버전입니다: {profile.schema_version}"
        )
    if profile.schema_version < 5 and profile.body_columns != 2:
        raise ValueError("이전 내장 양식 프로필은 2단 시험지만 지원합니다.")
    if not profile.written_labels:
        raise ValueError("학교 프로필에 서답형 표지가 없습니다.")
    if 2 <= profile.schema_version < 5:
        if not profile.template_filename or not profile.body_start_marker:
            raise ValueError("내장 시험지 양식 정보가 없습니다.")
        if profile.final_check.placement not in {
            "after_last_question_once",
            "last_page_bottom_once",
        }:
            raise ValueError("지원하지 않는 마지막 고정 블록 배치 규칙입니다.")
    if profile.schema_version >= 3:
        expected_keys = {
            "none",
            "sequential",
            "mixed_passage",
            "long_passage",
            "shared_visual",
            "independent_material",
        }
        actual_keys = {item.key for item in profile.document_types}
        if actual_keys != expected_keys:
            raise ValueError(
                "시험지 문서 유형 구성이 올바르지 않습니다: "
                f"{sorted(actual_keys)!r}"
            )
        invalid_policy = {
            item.group_policy
            for item in profile.document_types
            if item.group_policy not in {"auto", "optional", "required", "forbidden"}
        }
        if invalid_policy:
            raise ValueError(f"지원하지 않는 묶음 정책입니다: {invalid_policy!r}")
    return profile
