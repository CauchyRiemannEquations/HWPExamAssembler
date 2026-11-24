"""Local index of confirmed HWP templates and their original file locations."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class RegisteredTemplate:
    source: Path
    profile: Path


def template_data_root() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'HWPExamAssembler'


def recent_templates(root: Path) -> list[RegisteredTemplate]:
    entries = []
    for profile in (root / 'profiles').glob('*.json'):
        if not re.fullmatch(r'[a-f0-9]{64}', profile.stem):
            continue
        try:
            doc = json.loads(profile.read_text(encoding='utf-8'))
            if doc['version'] != 1 or doc['source_sha256'] != profile.stem:
                continue
            source = Path(doc['source_path'])
            if not source.is_absolute() or source.suffix.lower() not in {'.hwp', '.hwpx'}:
                continue
            entries.append((profile.stat().st_mtime_ns, RegisteredTemplate(source, profile)))
        except (OSError, KeyError, TypeError, ValueError):
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    # If a school replaced a template in place, offer its latest registration.
    newest = {}
    for _, entry in entries:
        newest.setdefault(entry.source, entry)
    return list(newest.values())
