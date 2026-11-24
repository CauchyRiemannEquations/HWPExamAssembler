"""Reject school-specific data before invoking a public-package build."""
import json
from pathlib import Path
import sys


def require_neutral_project(root: Path) -> None:
    project = root / "project"
    profile = json.loads((project / "school_profile.json").read_text(encoding="utf-8"))
    numbering = json.loads((project / "target_numbering_style.json").read_text(encoding="utf-8"))
    template = profile["template"]
    closing = profile["layout"]["final_check"]
    school_documents = [
        path for path in (root / "assets").rglob("*")
        if path.is_file() and path.suffix.lower() in {".hwp", ".hwpx"}
    ]
    if (
        profile["status"] != "generic" or profile["school"] or school_documents
        or template["sha256"] or template["bundled_filename"] or template["body_start_marker"]
        or closing["enabled"] or closing["title"] or closing["body"] or closing["source_markers"]
        or numbering["character"] is not None
    ):
        raise ValueError("학교 전용 설정·양식은 공유판에 포함할 수 없습니다. 학교판은 build_school.ps1로 빌드하세요.")


if __name__ == "__main__":
    try:
        require_neutral_project(Path(sys.argv[1]))
    except (OSError, KeyError, ValueError) as error:
        sys.exit(str(error))
