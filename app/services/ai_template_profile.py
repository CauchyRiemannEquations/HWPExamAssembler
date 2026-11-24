"""Versioned interchange for externally produced template analyses. No AI calls."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .format_analysis import Evidence, ROLES, PROPERTIES, numeric

SCHEMA_ID = 'hwp-exam-template-analysis'
PROFILE_ROLES = {**ROLES, 'page': '용지·단', 'header': '머리말', 'footer': '꼬리말',
                 'score': '배점', 'equation': '수식', 'answer': '답안 영역'}
# type, units, lower and upper bound (inclusive), allowed enum if any.
SPECS = {
    'font': ('string', ['none'], None, None, None),
    'font_latin': ('string', ['none'], None, None, None),
    'size': ('number', ['pt'], 1, 200, None),
    'line_spacing': ('number', ['percent', 'pt'], 1, 1000, None),
    'bold': ('boolean', ['none'], None, None, None),
    'italic': ('boolean', ['none'], None, None, None),
    'character_spacing': ('number', ['percent'], -100, 100, None),
    'width_ratio': ('number', ['percent'], 1, 500, None),
    'alignment': ('string', ['none'], None, None, ['Left', 'Right', 'Center', 'Justify']),
    'indent': ('number', ['mm', 'pt'], -200, 200, None),
    'before': ('number', ['mm', 'pt'], 0, 200, None),
    'after': ('number', ['mm', 'pt'], 0, 200, None),
    'number_format': ('string', ['none'], None, None, None),
    'markers': ('string', ['none'], None, None, None),
    'arrangement': ('string', ['none'], None, None,
                    ['single_paragraph', 'multiple_paragraphs', 'single_line', 'multiple_lines']),
    'width': ('number', ['mm'], 0.01, 2000, None),
    'height': ('number', ['mm'], 0.01, 2000, None),
    'treat_as_char': ('boolean', ['none'], None, None, None),
    'wrap': ('string', ['none'], None, None, ['TopAndBottom', 'Square', 'InFrontOfText', 'BehindText']),
    'horizontal_reference': ('string', ['none'], None, None, ['Paper', 'Page', 'Column', 'Para']),
    'vertical_reference': ('string', ['none'], None, None, ['Paper', 'Page', 'Para']),
    'horizontal_alignment': ('string', ['none'], None, None, ['Left', 'Center', 'Right']),
    'vertical_alignment': ('string', ['none'], None, None, ['Top', 'Center', 'Bottom']),
    'horizontal_offset': ('number', ['mm'], -2000, 2000, None),
    'vertical_offset': ('number', ['mm'], -2000, 2000, None),
    'allow_overlap': ('boolean', ['none'], None, None, None),
    'inside_left': ('number', ['mm'], 0, 200, None),
    'inside_right': ('number', ['mm'], 0, 200, None),
    'inside_top': ('number', ['mm'], 0, 200, None),
    'inside_bottom': ('number', ['mm'], 0, 200, None),
    'outside_left': ('number', ['mm'], 0, 200, None),
    'outside_right': ('number', ['mm'], 0, 200, None),
    'outside_top': ('number', ['mm'], 0, 200, None),
    'outside_bottom': ('number', ['mm'], 0, 200, None),
    'border_width': ('number', ['mm'], 0, 20, None),
    'border_style': ('string', ['none'], None, None, ['solid', 'dashed', 'dotted', 'double', 'none']),
    'columns': ('number', ['count'], 1, 10, None),
    'column_gap': ('number', ['mm'], 0, 200, None),
    'margin_left': ('number', ['mm'], 0, 200, None),
    'margin_right': ('number', ['mm'], 0, 200, None),
    'margin_top': ('number', ['mm'], 0, 200, None),
    'margin_bottom': ('number', ['mm'], 0, 200, None),
    'orientation': ('string', ['none'], None, None, ['portrait', 'landscape']),
}
REQUIRED_CHECKS = {(r, p) for r in ('number', 'body', 'passage', 'example', 'choice') for p in ('font', 'size')}
REQUIRED_CHECKS |= {(r, 'line_spacing') for r in ('body', 'passage', 'example', 'choice')}
REQUIRED_CHECKS |= {('number', 'number_format'), ('example', 'markers'), ('choice', 'markers'), ('choice', 'arrangement')}
REQUIRED_CHECKS |= {(r, p) for r in ('box', 'picture') for p in ('width', 'height', 'treat_as_char',
                   'horizontal_reference', 'vertical_reference', 'horizontal_alignment', 'vertical_alignment', 'wrap')}
REQUIRED_CHECKS |= {('page', p) for p in ('width', 'height', 'orientation', 'columns', 'column_gap',
                                       'margin_left', 'margin_right', 'margin_top', 'margin_bottom')}


class ProfileError(ValueError):
    pass


def _object(value, fields, path):
    if not isinstance(value, dict) or set(value) != set(fields):
        actual = set(value) if isinstance(value, dict) else set()
        raise ProfileError(f'{path}: 필드 확인 필요 (누락 {sorted(set(fields)-actual)}, 추가 {sorted(actual-set(fields))})')


def _string(value, path, *, nullable=False, maximum=1000):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProfileError(f'{path}: 비어 있지 않은 {maximum}자 이하 문자열이 필요합니다.')


def validate_profile(data: object) -> dict:
    _object(data, ('schema', 'version', 'profile_name', 'template', 'evidence', 'rules', 'notes'), '$')
    if data['schema'] != SCHEMA_ID or type(data['version']) is not int or data['version'] != 1:
        raise ProfileError('schema/version: 지원 형식은 hwp-exam-template-analysis / 1입니다.')
    _string(data['profile_name'], 'profile_name', maximum=120)
    template = data['template']
    _object(template, ('name', 'sha256', 'input_kind', 'pages_checked'), 'template')
    _string(template['name'], 'template.name', maximum=200)
    digest = template['sha256']
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or
                               any(c not in '0123456789abcdefABCDEF' for c in digest)):
        raise ProfileError('template.sha256: 실제 파일의 SHA-256 또는 null이 필요합니다.')
    if template['input_kind'] not in ('native', 'pdf', 'image', 'mixed', 'text'):
        raise ProfileError('template.input_kind: native/pdf/image/mixed/text 중 하나여야 합니다.')
    for name, values, maximum in [('template.pages_checked', template['pages_checked'], 100), ('notes', data['notes'], 100)]:
        if not isinstance(values, list) or len(values) > maximum:
            raise ProfileError(f'{name}: {maximum}개 이하 배열이 필요합니다.')
        for value in values:
            _string(value, name)
    if not isinstance(data['evidence'], list) or len(data['evidence']) > 500:
        raise ProfileError('evidence: 근거는 500개 이하 배열이어야 합니다.')
    evidence = {}
    for index, item in enumerate(data['evidence']):
        path = f'evidence[{index}]'
        _object(item, ('id', 'kind', 'location', 'quote'), path)
        _string(item['id'], path + '.id', maximum=60)
        if item['id'] in evidence:
            raise ProfileError(f'{path}: 중복 근거 ID {item["id"]}')
        if item['kind'] not in ('instruction_text', 'native_property', 'visual', 'user_description'):
            raise ProfileError(f'{path}.kind: 지원하지 않는 근거 유형입니다.')
        _string(item['location'], path + '.location', maximum=500)
        _string(item['quote'], path + '.quote', nullable=True, maximum=500)
        evidence[item['id']] = item
    if not isinstance(data['rules'], list) or not 1 <= len(data['rules']) <= 300:
        raise ProfileError('rules: 규칙은 1~300개 배열이어야 합니다.')
    ids = set()
    for index, rule in enumerate(data['rules']):
        path = f'rules[{index}]'
        _object(rule, ('id', 'role', 'property', 'scope', 'condition', 'status', 'value', 'unit',
                       'confidence', 'evidence_ids', 'notes'), path)
        _string(rule['id'], path + '.id', maximum=60)
        if rule['id'] in ids:
            raise ProfileError(f'{path}: 중복 규칙 ID {rule["id"]}')
        ids.add(rule['id'])
        if (not isinstance(rule['role'], str) or not isinstance(rule['property'], str) or
                rule['role'] not in PROFILE_ROLES or rule['property'] not in SPECS):
            raise ProfileError(f'{path}: 지원하지 않는 role/property입니다.')
        if rule['scope'] not in ('all', 'objective', 'written', 'specific'):
            raise ProfileError(f'{path}.scope: 지원하지 않는 적용 범위입니다.')
        for name in ('condition', 'notes'):
            _string(rule[name], path + '.' + name, nullable=True)
        if rule['status'] not in ('explicit', 'observed', 'inferred', 'unknown'):
            raise ProfileError(f'{path}.status: explicit/observed/inferred/unknown 중 하나여야 합니다.')
        if rule['confidence'] not in ('high', 'medium', 'low', 'unknown'):
            raise ProfileError(f'{path}.confidence: high/medium/low/unknown 중 하나여야 합니다.')
        refs = rule['evidence_ids']
        if not isinstance(refs, list) or len(refs) > 30 or any(not isinstance(e, str) or e not in evidence for e in refs):
            raise ProfileError(f'{path}.evidence_ids: 존재하는 근거 ID를 30개 이하로 지정하세요.')
        if len(set(refs)) != len(refs):
            raise ProfileError(f'{path}.evidence_ids: 중복 근거 ID를 제거하세요.')
        if rule['status'] == 'unknown':
            if rule['value'] is not None or rule['unit'] is not None or rule['confidence'] != 'unknown':
                raise ProfileError(f'{path}: unknown은 value/unit=null, confidence=unknown이어야 합니다.')
            continue
        if not refs or rule['confidence'] == 'unknown':
            raise ProfileError(f'{path}: 확인된 값에는 근거와 신뢰도 구분이 필요합니다.')
        if rule['status'] == 'explicit' and not any(evidence[e]['kind'] == 'instruction_text' and evidence[e]['quote'] for e in refs):
            raise ProfileError(f'{path}: explicit에는 원문 인용이 있는 instruction_text 근거가 필요합니다.')
        kind, units, minimum, maximum, enum = SPECS[rule['property']]
        value = rule['value']
        if rule['unit'] not in units:
            raise ProfileError(f'{path}.unit: 허용 단위는 {units}입니다.')
        if kind == 'number':
            if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
                raise ProfileError(f'{path}.value: {minimum}~{maximum} 사이 유한한 숫자가 필요합니다.')
            if rule['unit'] == 'count' and int(value) != value:
                raise ProfileError(f'{path}.value: 개수는 정수여야 합니다.')
        elif kind == 'boolean':
            if type(value) is not bool:
                raise ProfileError(f'{path}.value: true 또는 false가 필요합니다.')
        else:
            _string(value, path + '.value', maximum=200)
            if enum and value not in enum:
                raise ProfileError(f'{path}.value: 허용값은 {enum}입니다.')
    return data


def load_profile(path: str | Path) -> dict:
    if Path(path).stat().st_size > 1_000_000:
        raise ProfileError('JSON 파일은 1 MB 이하만 읽을 수 있습니다.')
    content = Path(path).read_bytes()
    if len(content) > 1_000_000:
        raise ProfileError('JSON 파일은 1 MB 이하만 읽을 수 있습니다.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProfileError(f'중복 JSON 키: {key}')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ProfileError(f'JSON에서 사용할 수 없는 숫자: {value}')
    try:
        data = json.loads(content.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=invalid_constant)
        return validate_profile(data)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ProfileError(f'UTF-8 JSON을 읽지 못했습니다. 코드블록과 설명문을 제외하고 JSON만 저장하세요: {error}') from error


def profile_evidence(data: dict, template_path: Path | None = None) -> tuple[list[Evidence], list[str], bool]:
    """Return candidates, full review notes and whether file identity permits use.

    References are labels only: no path or URL from the JSON is ever opened.
    The explicit template_path comes from the existing document selection UI.
    """
    validate_profile(data)
    notes, candidates = list(data['notes']), []
    allowed = True
    digest = data['template']['sha256']
    if digest:
        if template_path is None:
            allowed = False
            notes.insert(0, '양식 파일이 지정되지 않아 해시를 확인할 수 없습니다. JSON 기준 사용을 보류합니다.')
        elif hashlib.sha256(template_path.read_bytes()).hexdigest().lower() != digest.lower():
            allowed = False
            notes.insert(0, '양식 파일 SHA-256 불일치: 다른 양식의 JSON일 수 있어 비교 기준으로 사용할 수 없습니다.')
        else:
            notes.insert(0, '선택한 양식 파일의 SHA-256이 JSON과 일치합니다.')
    else:
        notes.insert(0, 'SHA-256 없음: 양식 동일성 미확인. JSON의 양식 이름과 근거를 직접 대조하세요.')
    evidence = {e['id']: e for e in data['evidence']}
    for rule in data['rules']:
        role, prop, value = rule['role'], rule['property'], rule['value']
        basis = [evidence[e] for e in rule['evidence_ids']]
        location = '; '.join(f"{e['id']} · {e['location']}" + (f" · {e['quote']}" if e['quote'] else '') for e in basis)
        label = f"{rule['id']} · {PROFILE_ROLES[role]} / {PROPERTIES.get(prop, prop)}"
        rationale = f"{rule['status']} / {rule['confidence']} / {rule['scope']}"
        eligible = (role in ROLES and rule['scope'] == 'all' and rule['condition'] is None and
                    rule['confidence'] == 'high' and (rule['status'] == 'explicit' or
                    (rule['status'] == 'observed' and any(e['kind'] == 'native_property' for e in basis))))
        converted = None
        if prop in ('font', 'font_latin', 'number_format', 'markers', 'alignment', 'wrap'):
            converted = value
        elif prop in ('size', 'width', 'height', 'character_spacing', 'width_ratio', 'line_spacing') and value is not None:
            converted = numeric(value) + ' ' + {'percent': '%', 'pt': 'pt', 'mm': 'mm'}[rule['unit']]
        elif prop in ('bold', 'italic', 'treat_as_char') and type(value) is bool:
            converted = 'true' if value else 'false'
        elif prop == 'arrangement':
            converted = {'single_paragraph': '한 문단 (화면 줄바꿈 미검증)', 'multiple_paragraphs': '여러 문단'}.get(value)
        if eligible and converted is not None:
            candidates.append(Evidence(role, prop, converted, 'AI JSON · ' + rule['status'], label + ' · ' + location))
            disposition = '비교 가능 후보 (사용자 선택 필요)'
        else:
            disposition = '수동 확인 · 자동 비교 제외'
        notes.append(f"{label}: {value if value is not None else '확인 불가'} {rule['unit'] or ''}\n"
                     f"{rationale} · {disposition}\n조건: {rule['condition'] or '없음'}\n"
                     f"근거: {location or '없음'}\n메모: {rule['notes'] or '없음'}")
    missing = REQUIRED_CHECKS - {(r['role'], r['property']) for r in data['rules']}
    if missing:
        notes.append('필수 확인 항목 누락 (기본값으로 채우지 않음): ' + ', '.join(f'{r}.{p}' for r, p in sorted(missing)))
    return candidates, notes, allowed
