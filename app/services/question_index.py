"""Keep the last verified list in the app's local work directory."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


_NAME = "last-public-question-list.json"
_LIMIT = 8 * 1024 * 1024


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def load_questions(work_dir: Path, document_sha: str, metadata: dict):
    from .exam_service import QuestionSummary

    try:
        with (work_dir / _NAME).open("rb") as stream:
            data = stream.read(_LIMIT + 1)
        if len(data) > _LIMIT:
            return None
        entry = json.loads(data)
        if (entry["version"] != 1 or entry["document_sha256"] != document_sha
                or entry["metadata_sha256"] != _digest(metadata)):
            return None
        # A truncated or accidentally edited local index must never supply a
        # different set of IDs to the editor for an otherwise unchanged file.
        checksum = entry.pop("checksum")
        if checksum != _digest(entry):
            return None
        questions = []
        for row in entry["questions"]:
            row["child_ids"] = tuple(row["child_ids"])
            questions.append(QuestionSummary(**row))
        if questions and len({q.id for q in questions}) == len(questions):
            return tuple(questions)
    except (OSError, TypeError, KeyError, ValueError, RecursionError):
        pass
    return None


def store_questions(work_dir: Path, snapshot, metadata: dict) -> None:
    if not snapshot.content_sha256 or not snapshot.questions:
        return
    temporary = None
    try:
        entry = {
            "version": 1,
            "document_sha256": snapshot.content_sha256,
            "metadata_sha256": _digest(metadata),
            "questions": [asdict(q) for q in snapshot.questions],
        }
        entry["checksum"] = _digest(entry)
        data = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        if len(data) > _LIMIT:
            return
        work_dir.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=work_dir, prefix=".question-list-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, work_dir / _NAME)
    except (OSError, ValueError, RecursionError):
        # Indexing is optional, including on a managed PC with read-only app
        # data. A verified save or native inspection has already succeeded.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
