"""Teacher-reviewed, content-free paragraph map for template registration."""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, replace
from itertools import groupby

from app.services.template_model import Region, area_detail, validate_registration


# These are human-readable categories, never substrings copied from the document.
_SIGNALS = {
    "시험 제목": r"중간고사|기말고사|정기고사|정기평가|지필평가|학업성취도",
    "학생 기입란": r"수험번호|학번|성명|이름",
    "선택형 안내": r"^(?:[0-9오사]지선다형|선택형|객관식)(?:문항)?$",
    "서답형 안내": r"^(?:서답형|서술형|단답형)(?:문항)?$",
    "응시 안내": r"답안지|OMR|수험생은|유의사항",
    "종료 안내": r"확인사항|시험종료후|수고하셨습니다|저작권",
    "문항 표현": r"\?|구하시오|고르시오|옳은|쓰시오|서술하시오",
    "보기": r"[①②③④⑤]",
}
_CONTROLS = {"TABLE": "표", "EQUATION": "수식", "PICTURE": "그림", "DRAWINGOBJECT": "도형",
             "HEADER": "쪽 머리말", "FOOTER": "쪽 꼬리말", "ENDNOTE": "미주", "FOOTNOTE": "각주",
             "SECDEF": "구역", "COLDEF": "단"}
_ROLES = {"keep": "보존", "replace": "기존 문항 교체", "insert": "삽입점"}


def _signals(paragraph):
    compact = re.sub(r"\s+", "", paragraph.text)
    signals = [label for label, pattern in _SIGNALS.items() if re.search(pattern, compact)]
    signals.extend(label for tag, label in _CONTROLS.items() if tag in paragraph.controls)
    if paragraph.numbered or re.match(r"\s*(?:\d{1,3}[.)]|\[?(?:서답형|서술형)\s*\d+)", paragraph.body_text):
        signals.append("문항 번호 후보")
    return tuple(signals) or (("일반 문단",) if compact else ("빈 문단",))


@dataclass(frozen=True)
class AnalysisRequest:
    request_id: str
    prompt: str
    boundaries: tuple
    regions: tuple[Region, ...]


def make_request(paragraphs, regions) -> AnalysisRequest:
    if not paragraphs:
        raise ValueError("양식을 한글에서 다시 열어 주세요.")
    boundaries = tuple(p.start for p in paragraphs) + (paragraphs[-1].end,)
    index = {pos: i for i, pos in enumerate(boundaries)}
    blocks = []
    cursor = 0
    for signals, group in groupby(paragraphs, key=_signals):
        count = sum(1 for _ in group)
        blocks.append([cursor, cursor + count, list(signals)])
        cursor += count
    regions = tuple(regions)
    areas = []
    for i, region in enumerate(regions):
        areas.append({"region": i, "role": _ROLES[region.role], "locked": region.corrected,
                      "start": index.get(region.start), "end": index.get(region.end)})
    request_id = secrets.token_hex(8)
    payload = json.dumps({"request_id": request_id, "paragraph_count": len(paragraphs),
                          "blocks": blocks, "regions": areas}, ensure_ascii=False, separators=(",", ":"))
    if len(payload) > 42000:
        raise ValueError("문단 구조가 길어 한 번에 분석하기 어렵습니다. 한글에서 필요한 범위를 직접 지정하세요.")
    prompt = (
        "학교 시험지 양식에서 보존할 범위와 기존 문항 교체 범위, 새 문항 삽입점을 검토해 주세요. "
        "문서 원문은 제공되지 않습니다. blocks의 각 항목은 [시작 문단, 끝 경계(미포함), 구조 단서]입니다. "
        "경계는 0부터 paragraph_count까지입니다. regions의 잠긴 항목은 직접 지정된 위치입니다. "
        "보존할 제목, 표, 유형 안내, 끝 안내가 교체 범위에 들어가지 않게 하세요. "
        "근거가 충분한 변경만 제시하세요. 확신할 수 없거나 모두 맞으면 changes를 비우세요. "
        "변경 뒤에는 범위가 겹치면 안 됩니다. 삽입점은 시작과 끝이 같습니다. "
        "아래의 request_id를 그대로 쓰고 기존 region 번호만 사용하세요. "
        '응답은 {"request_id":"…","changes":[{"region":0,"start":0,"end":1}]} 형식의 JSON 하나로 작성하세요.\n\n'
        + payload
    )
    return AnalysisRequest(request_id, prompt, boundaries, regions)


def read_suggestions(request: AnalysisRequest, answer: str) -> tuple[Region, ...]:
    if len(answer) > 65000:
        raise ValueError("응답이 너무 깁니다. 분석 결과 JSON만 붙여넣으세요.")
    answer = answer.strip()
    fences = re.findall(r"```(?:json)?\s*([{][\s\S]*?[}])\s*```", answer, re.I)
    if len(fences) == 1:
        answer = fences[0]
    try:
        data = json.loads(answer)
    except (ValueError, RecursionError) as exc:
        raise ValueError("응답을 읽을 수 없습니다. AI가 답한 JSON을 처음부터 끝까지 복사하세요.") from exc
    if not isinstance(data, dict) or data.get("request_id") != request.request_id:
        raise ValueError("이번 분석 요청의 응답인지 확인하세요. 양식을 다시 열면 새 요청이 필요합니다.")
    changes = data.get("changes")
    if not isinstance(changes, list) or len(changes) > len(request.regions):
        raise ValueError("응답의 changes 목록을 확인하세요.")
    result = list(request.regions)
    seen = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("범위 형식이 올바르지 않습니다.")
        idx, a, b = (change.get(field) for field in ("region", "start", "end"))
        if any(type(v) is not int for v in (idx, a, b)) or not (0 <= idx < len(result) and
                0 <= a <= b < len(request.boundaries)) or idx in seen:
            raise ValueError("응답에 중복된 항목 또는 양식에 없는 위치가 있습니다.")
        seen.add(idx)
        region = result[idx]
        start, end = request.boundaries[a], request.boundaries[b]
        if region.corrected or (start == end) != (region.role == "insert"):
            raise ValueError("직접 지정한 범위나 항목의 유형과 맞지 않는 제안입니다.")
        if (start, end) != (region.start, region.end):
            result[idx] = replace(region, start=start, end=end, detail=area_detail(start, end))
    if changes:
        validate_registration(result)
        anchor = next(r.start for r in result if r.role == "insert")
        if any(r.role == "keep" and r.start and r.start < anchor < r.end for r in result):
            raise ValueError("삽입점이 보존할 범위 안에 있습니다. 한글에서 위치를 확인해 주세요.")
    return tuple(result)
