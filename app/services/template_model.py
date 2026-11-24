"""Local suggestions and native Hangul coordinates for the registration preview."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

Pos = tuple[int, int, int]


@dataclass(frozen=True)
class Region:
    key: str
    role: str
    label: str
    start: Pos | None
    end: Pos | None
    detail: str
    corrected: bool = False


@dataclass(frozen=True)
class StudioState:
    title: str
    source: Path
    regions: tuple[Region, ...]
    registered: bool = False
    paragraphs: tuple = ()


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def area_detail(start, end):
    if start is None:
        return '지정하지 않음'
    if start == end:
        return f'본문 {start[1] + 1}번째 문단 · 커서 위치'
    last = end[1] + (1 if end[2] else 0)
    return f'본문 {start[1] + 1}번째 문단' if last <= start[1] + 1 else f'본문 {start[1] + 1}~{last}번째 문단'


def suggest(paragraphs):
    if not paragraphs:
        raise ValueError('한글에서 본문 위치를 읽을 수 없습니다.')
    questions = []
    previous_number = None
    sections = []
    tails = []
    for p in paragraphs:
        body = ' '.join(p.body_text.split())
        compact = re.sub(r'\s+', '', p.text)
        if re.fullmatch(r'(?:[0-9오사]지선다형|선택형|객관식|단답형|서답형|서술형)(?:문항)?', compact):
            sections.append(p)
        if re.match(r'^(?:[※＊*·◆▣□◇◈\[【(]*)(?:확인사항|답안작성시유의사항|시험종료후|수고하셨습니다|이문제지에관한저작권)', compact):
            tails.append(p)
        prefix = re.match(r'^\s*(?:(\d{1,3})\s*[.)]|\[?(?:서답형|서술형)\s*\d+\]?)', body)
        number = int(prefix.group(1)) if prefix and prefix.group(1) else None
        continued = previous_number is not None and number == previous_number + 1
        looks_like_prompt = bool(re.search(r'\?|구하|고르|옳은|옳지|값은|쓰시오|서술|알맞|점\s*[\]】]', body))
        instruction = re.match(r'^\s*(?:\d+[.)]\s*)?(?:OMR|답안지|수험번호|수험생은)', body)
        if not instruction and (p.numbered or prefix) and (continued or looks_like_prompt or 'EQUATION' in p.controls or len(body) < 5):
            questions.append(p)
            previous_number = number

    begin, end = paragraphs[0].start, paragraphs[-1].end
    first = questions[0].start if questions else None
    final = next((p.start for p in tails if questions and p.start > questions[-1].start), None)
    if first is None and sections and sections[0].end < end:
        candidate = sections[0].end
    else:
        candidate = first
    head_end = first or candidate
    header = Region('header', 'keep', '시험지 머리말', begin if head_end and begin < head_end else None,
                    head_end if head_end and begin < head_end else None,
                    '시험지 제목과 첫 문항 앞의 안내' if head_end and begin < head_end else '필요한 경우 한글에서 지정')
    body = Region('examples', 'replace', '기존 문항', first, (final or end) if first else None,
                  f'첫 문항부터 마지막 문항까지 · 문항 시작 후보 {len(questions)}곳' if first else '예시 문항을 찾지 못했습니다')
    anchor = Region('insert', 'insert', '새 문항 시작 위치', candidate, candidate,
                    '첫 문항이 시작되는 자리' if first else '유형 안내 바로 다음' if candidate else '한글에서 커서를 놓아 지정')
    tail = Region('footer', 'keep', '마지막 안내', final, end if final else None,
                  '문항 뒤의 확인 사항과 고정 안내' if final else '마지막 안내가 있으면 직접 지정')
    breaks = [p for p in sections if first and first < p.start < (final or end)]
    if not breaks:
        return header, body, anchor, tail
    result = [header, anchor]
    heading = ''.join(re.sub(r'\s+', '', p.text) for p in paragraphs if p.start < first)
    label = '선택형 문항' if re.search(r'(?:지선다형|객관식|선택형)', heading) else '앞부분 문항'
    block_start, key = first, 'examples'
    def question_detail(a, b):
        matches = [q for q in questions if a <= q.start < b]
        number_text = r'(?:\[?(?:서답형|서술형)\s*)?(\d{1,3})\s*[.)\]]'
        first_id = re.match(number_text, matches[0].body_text) if matches else None
        last_id = re.match(number_text, matches[-1].body_text) if matches else None
        if first_id and last_id and int(last_id[1]) - int(first_id[1]) + 1 == len(matches):
            return f'{first_id[1]}번부터 {last_id[1]}번까지' if len(matches) > 1 else f'{first_id[1]}번 문항'
        return f'문항 시작 후보 {len(matches)}곳'
    for p in breaks:
        after = next((q for q in questions if q.start >= p.end), None)
        if not after or block_start >= p.start:
            continue
        result.append(Region(key, 'replace', label, block_start, p.start, question_detail(block_start, p.start)))
        section_text = ' '.join(p.text.split())[:40]
        result.append(Region(f'section-{p.paragraph}', 'keep', '문항 유형 안내', p.start, after.start, section_text))
        block_start, key, label = after.start, f'examples-{p.paragraph}', section_text + ' 문항'
    result.append(Region(key, 'replace', label, block_start, final or end, question_detail(block_start, final or end)))
    return tuple([*result, tail])


def revise(regions, key, start, end):
    old = next(r for r in regions if r.key == key)
    if (start is None) != (end is None):
        raise ValueError('선택의 시작과 끝을 확인해 주세요.')
    if start is None and old.role == 'insert':
        raise ValueError('새 문항을 넣을 커서를 지정해 주세요.')
    if start is not None and (start[0] != 0 or end[0] != 0 or (old.role == 'insert' and start != end)
                              or (old.role != 'insert' and start >= end)):
        raise ValueError('본문에서 위치 또는 범위를 다시 선택해 주세요.')

    edits = {r.key: r for r in regions}
    def set_area(k, a, b, manual=False):
        region = edits[k]
        edits[k] = replace(region, start=a, end=b, detail=area_detail(a, b), corrected=manual or region.corrected)

    set_area(key, start, end, manual=True)
    examples = next(r for r in regions if r.key == 'examples')
    # Keep boundaries that were proposed together linked until the teacher
    # explicitly sets each one. Manually placed anchors always stay put.
    if old.role == 'replace' and start is not None:
        for region in regions:
            if region.role == 'keep' and region.start is not None and not region.corrected:
                if region.end == old.start and region.start < start:
                    set_area(region.key, region.start, start)
                elif region.start == old.end and end < region.end:
                    set_area(region.key, end, region.end)
    if key.startswith('section-') and start and end:
        for region in regions:
            if region.role != 'replace' or region.corrected or region.start is None:
                continue
            if region.end == old.start and region.start < start:
                set_area(region.key, region.start, start)
            elif region.start == old.end and end < region.end:
                set_area(region.key, end, region.end)
    if key == 'header' and end and not examples.corrected and examples.start == old.end and examples.end and end < examples.end:
        set_area('examples', end, examples.end)
    if key == 'footer' and start:
        for region in regions:
            if region.role == 'replace' and not region.corrected and region.end == old.start and region.start and region.start < start:
                set_area(region.key, region.start, start)
    anchor = edits['insert']
    if key != 'insert' and not anchor.corrected and anchor.start == examples.start and edits['examples'].start is not None:
        at = edits['examples'].start
        if anchor.start != at:
            set_area('insert', at, at)
    return tuple(edits[r.key] for r in regions)


def validate_registration(regions):
    if not any(r.role == 'insert' and r.start is not None for r in regions):
        raise ValueError('새 문항을 넣을 위치를 먼저 지정하세요.')
    ranges = sorted((r for r in regions if r.role != 'insert' and r.start is not None), key=lambda r: r.start)
    for first, following in zip(ranges, ranges[1:]):
        if first.end > following.start:
            raise ValueError(f'「{first.label}」와 「{following.label}」 범위가 겹칩니다. 둘 중 한 곳의 경계를 다시 짚어 주세요.')


def save_registration(path, signature, regions, *, source_path=None):
    validate_registration(regions)
    doc = {'version': 1, 'source_sha256': signature,
           'regions': [{'key': r.key, 'role': r.role, 'start': r.start, 'end': r.end, 'corrected': r.corrected} for r in regions]}
    if source_path is not None:
        doc['source_path'] = str(Path(source_path).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, mode='w', encoding='utf-8') as stream:
        temporary = Path(stream.name)
        try:
            json.dump(doc, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def restore_registration(path, signature, suggestions, last):
    if not path.exists():
        return suggestions, False
    try:
        doc = json.loads(path.read_text(encoding='utf-8'))
        saved = doc['regions']
        if doc['version'] != 1 or doc['source_sha256'] != signature or len(saved) != len(suggestions):
            return suggestions, False
        restored = []
        for region, entry in zip(suggestions, saved, strict=True):
            if entry['key'] != region.key or entry['role'] != region.role:
                return suggestions, False
            a, b = (None if entry[k] is None else tuple(entry[k]) for k in ('start', 'end'))
            if (a is None) != (b is None):
                return suggestions, False
            if a is not None and any(len(p) != 3 or not all(type(v) is int for v in p) or p[0] != 0 or min(p) < 0 or p > last for p in (a,b)):
                return suggestions, False
            if region.role == 'insert' and (a is None or a != b) or a is not None and region.role != 'insert' and a >= b:
                return suggestions, False
            restored.append(replace(region, start=a, end=b, corrected=bool(entry['corrected']),
                                    detail=area_detail(a,b) if entry['corrected'] or (a,b) != (region.start,region.end) else region.detail))
        return tuple(restored), True
    except (OSError, KeyError, TypeError, ValueError):
        return suggestions, False
