from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QuestionBlock:
    id: str
    start_marker: str
    end_marker: str | None
    author: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class ImportManifest:
    template_content_start: str
    questions: tuple[QuestionBlock, ...]


def load_manifest(path: str | Path) -> ImportManifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    questions = tuple(QuestionBlock(**item) for item in data["questions"])
    ids = [question.id for question in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("Question IDs must be unique")
    if not questions:
        raise ValueError("Manifest must contain at least one question")
    return ImportManifest(
        template_content_start=data["template_content_start"],
        questions=questions,
    )
