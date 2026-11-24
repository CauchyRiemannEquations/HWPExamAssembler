"""Teacher-entered difficulty, stored alongside each working document by QID."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

DIFFICULTIES = ("미지정", "하", "중", "상")


def difficulty_path(document: Path) -> Path:
    return document.with_suffix(document.suffix + ".difficulty.json")


def load_difficulties(document: Path) -> dict[str, str]:
    path = difficulty_path(document)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("지원하지 않는 난이도 파일 형식입니다.")
    values = data.get("questions")
    if not isinstance(values, dict) or any(
        not isinstance(key, str) or value not in DIFFICULTIES
        for key, value in values.items()
    ):
        raise ValueError("난이도 파일의 문항 정보가 올바르지 않습니다.")
    return values


def save_difficulties(document: Path, values: dict[str, str]) -> None:
    if any(value not in DIFFICULTIES for value in values.values()):
        raise ValueError("올바른 난이도를 선택하세요.")
    path = difficulty_path(document)
    payload = json.dumps(
        {"schema_version": 1, "questions": values}, ensure_ascii=False, indent=2
    ) + "\n"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
