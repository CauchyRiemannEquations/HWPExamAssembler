"""Opt-in, text-free native transfer evidence. No COM dependencies."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid
import warnings


def fingerprint(text: str) -> dict:
    normalized = re.sub(r"\s+", " ", text).strip()
    return {"length": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "normalized_length": len(normalized),
            "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()}


def structural(summary: dict[str, int]) -> dict[str, int]:
    result: Counter = Counter()
    for key, count in summary.items():
        ctrl_id = key.split(":", 1)[0]
        if ctrl_id in {"eqed", "gso", "tbl"}:
            result[ctrl_id] += count
    return dict(sorted((key, value) for key, value in result.items() if value))


def safe_summary(summary: dict[str, int]) -> dict[str, int]:
    # UserDesc may be user supplied. Preserve strict distinctions without
    # leaking an object caption or any portion of the exam text.
    result = {}
    for key, value in summary.items():
        ctrl_id, sep, description = key.partition(":")
        if ctrl_id in {"eqed", "gso", "tbl"}:
            safe_key = ctrl_id + (":sha256=" + fingerprint(description)["sha256"] if sep else "")
            result[safe_key] = value
    return dict(sorted(result.items()))


def comparison(expected: dict | None, actual: dict, text_match: bool | None) -> dict:
    strict_match = None if expected is None else expected == actual
    structure_match = None if expected is None else structural(expected) == structural(actual)
    category = ("A" if strict_match is False and structure_match is False else
                "B" if strict_match is False and structure_match is True else
                "C" if strict_match is True and text_match is False else
                "MATCH" if strict_match is True and text_match is True else "UNASSESSED")
    return {"strict_match": strict_match, "structural_match": structure_match,
            "text_match": text_match, "classification": category,
            "expected_native_controls": None if expected is None else safe_summary(expected),
            "actual_control_delta": safe_summary(actual),
            "expected_structural": None if expected is None else structural(expected),
            "actual_structural": structural(actual)}


def safe_error(error: BaseException) -> dict:
    # Arbitrary COM/ValueError messages can contain body text. Store the
    # exact message fingerprint; stage and comparison carry the safe details.
    return {"type": type(error).__name__, "message": fingerprint(str(error))}


class DiagnosticLog:
    def __init__(self, directory: str | Path):
        directory = Path(directory).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex
        self.path = directory / f"native-paste-{self.run_id}.jsonl"
        self.context: dict = {}
        self.disabled = False
        self.emit("run_started", schema_version=1)

    def emit(self, event: str, **data) -> None:
        if self.disabled:
            return
        record = {"run_id": self.run_id, "time_utc": datetime.now(timezone.utc).isoformat(),
                  **self.context, "event": event, **data}
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
        except OSError as error:
            # Observability must never break a Paste or its rollback. Report
            # the failure once; do not silently pretend logging succeeded.
            self.disabled = True
            warnings.warn(f"Diagnostic log unavailable ({type(error).__name__}); logging disabled", RuntimeWarning)
