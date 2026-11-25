"""Local, read-only HWPML format evidence and conservative Korean rule parsing.

No content is reconstructed or uploaded. Candidate roles are deliberately exposed;
an observed style is not an assertion that the school requires that style.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


ROLES = {"number": "문제 번호", "body": "발문·본문 후보", "passage": "지문 후보",
         "example": "보기 내용", "choice": "선택지", "box": "보기 상자",
         "picture": "그림", "table": "표·상자 후보"}
PROPERTIES = {"font": "한글 글꼴", "font_latin": "영문 글꼴", "size": "글자 크기", "line_spacing": "줄간격",
              "bold": "굵게", "italic": "기울임", "character_spacing": "자간", "width_ratio": "장평",
              "alignment": "문단 정렬", "indent": "들여쓰기", "before": "문단 앞 간격",
              "after": "문단 뒤 간격", "number_format": "번호 형식", "markers": "기호 구성",
              "arrangement": "배열", "width": "너비", "height": "높이",
              "position": "배치 기준·정렬·위치", "wrap": "본문 배치", "outside": "바깥 여백",
              "inside": "안쪽 여백", "border": "테두리", "treat_as_char": "글자처럼 취급"}
SCOPE = re.compile(r"문제\s*번호|문항\s*번호|보기\s*상자|글\s*상자|선택지|선지|발문|본문|지문|보기|그림|사진")
SCOPE_MAP = {"문제번호": "number", "문항번호": "number", "보기상자": "box", "글상자": "box",
             "선택지": "choice", "선지": "choice", "발문": "body", "본문": "body",
             "지문": "passage", "보기": "example", "그림": "picture", "사진": "picture"}
NUMBER = re.compile(r"^\s*(?:(?:서답형|서술형)\s*)?(\d{1,3})\s*([.)])")
CHOICE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩]")
EXAMPLE = re.compile(r"(?:[<〈《\[]\s*보기\s*[>〉》\]]|^\s*[ㄱ-ㅎ][.)])")
RULE_HINT = re.compile(r"\d\s*(?:pt\b|포인트|%|퍼센트)|줄\s*간격|글꼴|폰트|글자\s*크기|서식|글자처럼", re.I)


@dataclass(frozen=True)
class Evidence:
    role: str
    property: str
    value: str
    source: str
    location: str


@dataclass
class FormatReport:
    observed: list[Evidence] = field(default_factory=list)
    declared: list[Evidence] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    instruction_lines: list[str] = field(default_factory=list)
    reviewed: bool = False
    overrides: list[Evidence] = field(default_factory=list)
    external_rules: list[Evidence] = field(default_factory=list)
    external_notes: list[str] = field(default_factory=list)
    external_profile: dict | None = None
    external_enabled: bool = False

    def rows(self) -> list[dict]:
        keys = {(role, prop) for role in ("number", "body", "passage", "example", "choice")
                for prop in (("font", "size") if role == "number" else ("font", "size", "line_spacing"))}
        keys |= {("number", "number_format"), ("choice", "markers"), ("choice", "arrangement"),
                 ("example", "markers")}
        keys |= {(role, prop) for role in ("box", "picture") for prop in
                 ("width", "height", "position", "treat_as_char", "wrap", "outside")}
        keys |= {(e.role, e.property) for e in self.observed + self.declared + self.overrides + self.external_rules}
        result = []
        role_order = list(ROLES)
        for role, prop in sorted(keys, key=lambda k: (role_order.index(k[0]), k[1])):
            observed = [e for e in self.observed if (e.role, e.property) == (role, prop)]
            declared = [e for e in self.declared if (e.role, e.property) == (role, prop)]
            external = [e for e in self.external_rules if (e.role, e.property) == (role, prop)]
            if self.external_enabled:
                declared = declared + external
            override = [e for e in self.overrides if (e.role, e.property) == (role, prop)]
            actual = sorted({e.value for e in observed})
            expected = sorted({e.value for e in override or declared})
            if len(expected) > 1:
                status = "지시문 충돌"
            elif expected and actual:
                status = "일치" if actual == expected else "불일치·확인 필요"
            elif expected:
                status = "지시문만 확인"
            elif len(actual) > 1:
                status = "여러 서식·확인 필요"
            elif actual:
                status = "예시에서 확인"
            else:
                status = "확인 불가"
            if external and not self.external_enabled and not expected:
                status += ' · AI 후보 미선택'
            result.append({"role": role, "property": prop, "label": f"{ROLES[role]} / {PROPERTIES[prop]}",
                           "observed": actual, "declared": expected, "status": status,
                           "external": sorted({e.value for e in external}),
                           "user_override": bool(override),
                           "evidence": [asdict(e) for e in observed + declared + override + ([] if self.external_enabled else external)]})
        return result

    def to_dict(self) -> dict:
        return {"schema_version": 1, **asdict(self), "rows": self.rows(),
                "limits": ["역할 분류는 후보이며 실제 양식과 대조해야 합니다.",
                           "줄바꿈·겹침·잘림의 시각적 검증은 수행하지 않습니다.",
                           "분석 결과는 문서 서식을 자동 변경하지 않습니다."]}

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def numeric(value: str | float, divisor: float = 1) -> str:
    return f"{float(value) / divisor:g}"


def parse_instructions(lines: list[str], fonts: tuple[str, ...] = ()) -> tuple[list[Evidence], list[str]]:
    """Only recognize explicit targets and units; retain unsupported instructions."""
    evidence, unresolved = [], []
    known_fonts = sorted(set(fonts) | {"바탕", "돋움", "굴림", "궁서", "맑은 고딕", "함초롬바탕",
                                    "함초롬돋움", "신명조", "휴먼명조", "한컴바탕", "한컴돋움"}, key=len, reverse=True)
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        scopes = list(SCOPE.finditer(line))
        if not scopes or re.search(r"이상|이하|이내|제외|경우|(?:^|[,;]\s*)단[ ,]|권장|가급적|말고|아니|않|금지|하지\s*말|또는|대신", line):
            unresolved.append(f"지시문 {line_number}: {line}")
            continue
        pending = []
        matched_any = False
        for index, scope in enumerate(scopes):
            role = SCOPE_MAP[re.sub(r"\s+", "", scope.group())]
            pending.append(role)
            segment = line[scope.end(): scopes[index + 1].start() if index + 1 < len(scopes) else len(line)]
            values: list[tuple[str, str]] = []
            consumed: list[tuple[int, int]] = []
            for m in re.finditer(r"(너비|폭|높이)\s*(?:은|는|:|=)?\s*(\d+(?:\.\d+)?)\s*(mm|cm|밀리미터|센티미터)", segment, re.I):
                amount = float(m[2]) * (10 if m[3].lower() in {'cm', '센티미터'} else 1)
                values.append(('height' if m[1] == '높이' else 'width', numeric(round(amount, 2)) + ' mm'))
                consumed.append(m.span())
            for m in re.finditer(r"줄\s*간격\s*(?:은|는|을|를|:|=)?\s*(\d+(?:\.\d+)?)\s*(%|퍼센트|pt\b|포인트)", segment, re.I):
                values.append(("line_spacing", numeric(m[1]) + (" %" if m[2] in {"%", "퍼센트"} else " pt")))
                consumed.append(m.span())
            for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:pt\b|포인트)", segment, re.I):
                if not any(a <= m.start() < b for a, b in consumed):
                    if re.search(r"간격|여백|들여쓰기|너비|높이|폭", segment[max(0, m.start()-12):m.start()]):
                        continue
                    values.append(("size", numeric(m[1]) + " pt"))
                    consumed.append(m.span())
            font_found = False
            # Explicit font labels also support installed fonts unknown to this parser.
            explicit = re.search(r"(?:글꼴|폰트)\s*(?:은|는|:|=)?\s*[\"'‘“]?([가-힣A-Za-z][가-힣A-Za-z0-9 _-]*?)(?=[\"'’”,;]|\s+\d|$)", segment)
            if explicit:
                name = re.sub(r"(?:으로|로)\s*(?:설정|지정|한다|합니다)?[.\s]*$", "", explicit[1]).strip()
                if name:
                    values.append(("font", name))
                    consumed.append(explicit.span())
                    font_found = True
            if not font_found:
                for font in known_fonts:
                    m = re.search(re.escape(font), segment)
                    if m:
                        values.append(("font", font))
                        consumed.append(m.span())
                        break
            if "글자처럼" in segment:
                values.append(("treat_as_char", "true"))
                consumed.append((segment.index("글자처럼"), len(segment)))
            if values:
                for target in dict.fromkeys(pending):
                    evidence.extend(Evidence(target, prop, value, "지시문", f"지시문 {line_number}: {line}")
                                    for prop, value in values)
                pending = []
                matched_any = True
                # Preserve constraints we did not parse rather than silently dropping them.
                remainder = ''.join(c if not any(a <= i < b for a, b in consumed) else ' '
                                    for i, c in enumerate(segment))
                if re.search(r"\d|간격|여백|들여쓰기|정렬|가운데|오른쪽|왼쪽|굵게|가로|세로|테두리", remainder):
                    unresolved.append(f"지시문 {line_number}의 추가 조건: {scope.group()}{segment}")
            elif not re.fullmatch(r"[\s,·/]*(?:과|와|및|은|는)?[\s,·/]*", segment):
                unresolved.append(f"지시문 {line_number}의 미해석 부분: {scope.group()}{segment}")
                pending = []
        if not matched_any and not any(f"지시문 {line_number}" in item for item in unresolved):
            unresolved.append(f"지시문 {line_number}: {line}")
    return evidence, list(dict.fromkeys(unresolved))


def _direct_text(node: ET.Element) -> str:
    """Do not attribute nested table/shape paragraphs to the anchor paragraph."""
    return ''.join(''.join(char.itertext()) for run in node.findall('TEXT') for char in run.findall('CHAR'))


def analyze_hwpml(xml: str, extra_instructions: str = "") -> FormatReport:
    if not isinstance(xml, str) or not xml.strip():
        raise ValueError("한글의 서식 XML을 읽지 못했습니다.")
    if len(xml) > 100_000_000 or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", xml, re.I):
        raise ValueError("지원하지 않는 XML 선언 또는 지나치게 큰 문서입니다.")
    root = ET.fromstring(xml)
    if root.tag != "HWPML":
        raise ValueError("HWPML 서식 문서가 아닙니다.")
    report = FormatReport()
    parents = {child: parent for parent in root.iter() for child in parent}
    font_faces = {face.get('Lang'): {f.get('Id'): f.get('Name', '') for f in face.findall('FONT')}
                  for face in root.iter('FONTFACE')}
    chars = {e.get('Id'): e for e in root.iter('CHARSHAPE')}
    paras = {e.get('Id'): e for e in root.iter('PARASHAPE')}
    numbering = {e.get('Id'): e for e in root.iter('NUMBERING')}
    borders = {e.get('Id'): e for e in root.iter('BORDERFILL')}
    all_fonts = tuple(name for face in font_faces.values() for name in face.values() if name)

    def add(role, prop, value, location):
        if value is not None and value != "":
            report.observed.append(Evidence(role, prop, str(value), "예시 서식", location))

    def char_style(role, char_id, location):
        shape = chars.get(char_id)
        if shape is None:
            return
        font_id = shape.find('FONTID')
        if font_id is not None:
            add(role, 'font', font_faces.get('Hangul', {}).get(font_id.get('Hangul')), location)
            add(role, 'font_latin', font_faces.get('Latin', {}).get(font_id.get('Latin')), location)
        if shape.get('Height'):
            add(role, 'size', numeric(shape.get('Height'), 100) + ' pt', location)
        add(role, 'bold', 'true' if shape.find('BOLD') is not None else 'false', location)
        add(role, 'italic', 'true' if shape.find('ITALIC') is not None else 'false', location)
        for tag, prop in [('CHARSPACING', 'character_spacing'), ('RATIO', 'width_ratio')]:
            item = shape.find(tag)
            if item is not None and item.get('Hangul') is not None:
                add(role, prop, item.get('Hangul') + ' %', location)

    def ancestors(node):
        while node in parents:
            node = parents[node]
            yield node

    def is_body(node):
        return any(p.tag == 'BODY' for p in ancestors(node)) and not any(
            p.tag in {'HEADER', 'FOOTER', 'FOOTNOTE', 'ENDNOTE'} for p in ancestors(node))

    paragraphs = [p for p in root.iter('P') if is_body(p)]
    positions = {p: index for index, p in enumerate(paragraphs, 1)}
    # Instruction paragraphs are excluded from sample style inference.
    instructions = {p for p in paragraphs if RULE_HINT.search(_direct_text(p)) and SCOPE.search(_direct_text(p))}
    report.instruction_lines = [_direct_text(p) for p in paragraphs if p in instructions]
    report.instruction_lines.extend(extra_instructions.splitlines())
    report.declared, report.unresolved = parse_instructions(report.instruction_lines, all_fonts)
    inside_question = False
    choice_paragraphs: list[str] = []

    def finish_choices():
        if choice_paragraphs:
            value = "한 문단 (화면 줄바꿈 미검증)" if len(choice_paragraphs) == 1 else "여러 문단"
            add('choice', 'arrangement', value, ', '.join(choice_paragraphs))
            choice_paragraphs.clear()

    for paragraph in paragraphs:
        text = _direct_text(paragraph).strip()
        if paragraph in instructions or not text:
            continue
        location = f"분석 문단 {positions[paragraph]}"
        shape = paras.get(paragraph.get('ParaShape'))
        is_native = shape is not None and shape.get('HeadingType') == 'Number'
        literal = NUMBER.match(text)
        nested = any(p.tag in {'TABLE', 'TEXTBOX', 'RECTANGLE', 'ELLIPSE', 'POLYGON'} for p in ancestors(paragraph))
        if literal or is_native:
            finish_choices()
            inside_question = True
            role = 'body'
            prefix = literal.end() if literal else 0
            if literal:
                add('number', 'number_format', '^1' + literal[2], location)
            if is_native:
                num = numbering.get(shape.get('Heading', shape.get('NumberingId', '')))
                if num is not None:
                    head = next((h for h in num.findall('PARAHEAD') if h.get('Level') == str(int(shape.get('Level', '0')) + 1)), None)
                    if head is not None:
                        add('number', 'number_format', head.text, location)
                        char_style('number', head.get('CharShape'), location)
                    else:
                        report.warnings.append(f'{location}: 문단 번호의 단계별 서식을 찾지 못했습니다.')
                else:
                    report.warnings.append(f'{location}: 문단 번호의 서식 정의를 찾지 못했습니다.')
        elif CHOICE.match(text):
            role, prefix = 'choice', 0
            markers = ''.join(CHOICE.findall(text))
            add(role, 'markers', markers, location)
            choice_paragraphs.append(location)
        elif EXAMPLE.search(text):
            role, prefix = 'example', 0
            markers = re.findall(r'[ㄱ-ㅎ][.)]', text)
            if markers:
                add(role, 'markers', ' '.join(markers), location)
        elif re.match(r'^\s*[<〈\[]\s*지문', text):
            role, prefix = 'passage', 0
        elif inside_question and not nested:
            role, prefix = 'body', 0
        else:
            continue
        offset = 0
        for run in paragraph.findall('TEXT'):
            run_text = ''.join(''.join(c.itertext()) for c in run.findall('CHAR'))
            if run_text.strip():
                if literal and offset < prefix:
                    char_style('number', run.get('CharShape'), location)
                if offset + len(run_text) > prefix:
                    char_style(role, run.get('CharShape'), location)
            offset += len(run_text)
        if shape is not None:
            add(role, 'alignment', shape.get('Align'), location)
            margin = shape.find('PARAMARGIN')
            if margin is not None:
                value, unit = margin.get('LineSpacing'), margin.get('LineSpacingType')
                if value is not None:
                    spacing = numeric(value) + ' %' if unit == 'Percent' else (
                        numeric(value, 100) + ' pt' if unit == 'Fixed' else f'{value} ({unit or "단위 미상"})')
                    add(role, 'line_spacing', spacing, location)
                for key, prop in [('Indent', 'indent'), ('Prev', 'before'), ('Next', 'after')]:
                    if margin.get(key) is not None:
                        add(role, prop, margin.get(key) + ' HWPUNIT', location)
    finish_choices()

    for index, obj in enumerate(root.iter('SHAPEOBJECT'), 1):
        if not is_body(obj):
            continue
        container = parents.get(obj)
        if container is None:
            continue
        owner_text = ' '.join(_direct_text(p) for p in container.iter('P'))
        role = 'picture' if container.tag == 'PICTURE' else ('box' if EXAMPLE.search(owner_text) else 'table')
        if container.tag not in {'PICTURE', 'TABLE', 'RECTANGLE', 'ELLIPSE', 'POLYGON', 'TEXTBOX'}:
            continue
        anchor = next((p for p in ancestors(obj) if p.tag == 'P'), None)
        location = f"개체 {index} / 분석 문단 {positions.get(anchor, '?')} / {container.tag}"
        size, pos = obj.find('SIZE'), obj.find('POSITION')
        if size is not None:
            for key in ['Width', 'Height']:
                if size.get(key):
                    unit = size.get(key + 'RelTo', '단위 미상')
                    value = (numeric(round(float(size.get(key)) * 25.4 / 7200, 2)) + ' mm'
                             if unit == 'Absolute' else size.get(key) + f' ({unit}, 상대값)')
                    add(role, key.lower(), value, location)
        if pos is not None:
            add(role, 'treat_as_char', pos.get('TreatAsChar'), location)
            add(role, 'position', json.dumps(pos.attrib, ensure_ascii=False, sort_keys=True), location)
        add(role, 'wrap', obj.get('TextWrap'), location)
        for node, prop in [(obj.find('OUTSIDEMARGIN'), 'outside'), (container.find('INSIDEMARGIN'), 'inside')]:
            if node is not None:
                add(role, prop, json.dumps(node.attrib, sort_keys=True) + ' HWPUNIT', location)
        border = borders.get(container.get('BorderFill'))
        if border is not None:
            add(role, 'border', json.dumps({child.tag: child.attrib for child in border}, sort_keys=True), location)
    report.warnings.append('발문·지문·보기의 역할은 문항 번호와 표지를 기준으로 분류한 후보입니다. 근거 위치를 확인하세요.')
    return report


def capture_format_report(hwp, extra_instructions: str = "", *, xml: str | None = None) -> FormatReport:
    try:
        if xml is None:
            xml = hwp.automation.GetTextFile('HWPML2X', '')
        return analyze_hwpml(xml, extra_instructions)
    except Exception as error:
        declared, unresolved = parse_instructions(extra_instructions.splitlines())
        return FormatReport(declared=declared, unresolved=unresolved,
                            warnings=[f'서식 읽기 실패 ({type(error).__name__}): {error}'],
                            instruction_lines=extra_instructions.splitlines())


def compare_output(template: FormatReport, output: FormatReport) -> dict:
    """Check only explicit, unambiguous declarations, never majority guesses."""
    comparison = FormatReport(observed=output.observed, declared=template.declared,
                              overrides=template.overrides, warnings=output.warnings,
                              external_rules=template.external_rules, external_enabled=template.external_enabled)
    rows = [row for row in comparison.rows() if row['declared']]
    return {"schema_version": 1, "checks": rows, "warnings": output.warnings,
            "status": "확인할 명시 규칙 없음" if not rows else (
                "명시 규칙 속성 일치 (역할 후보 기준)" if all(row['status'] == '일치' for row in rows)
                else "검토 필요"),
            "visual_check": "줄바꿈·겹침·잘림은 한글에서 직접 확인 필요"}
