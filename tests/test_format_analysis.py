from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.format_analysis import analyze_hwpml, parse_instructions, capture_format_report, compare_output


XML = '''<HWPML><HEAD><FONTFACE Lang="Hangul"><FONT Id="0" Name="바탕"/></FONTFACE>
<CHARSHAPE Id="0" Height="1000"><FONTID Hangul="0"/></CHARSHAPE>
<CHARSHAPE Id="1" Height="1100"><FONTID Hangul="0"/></CHARSHAPE>
<PARASHAPE Id="0" Align="Justify"><PARAMARGIN LineSpacing="160" LineSpacingType="Percent"/></PARASHAPE>
<PARASHAPE Id="1" HeadingType="Number" Heading="1" Level="0"><PARAMARGIN LineSpacing="160" LineSpacingType="Percent"/></PARASHAPE>
<NUMBERING Id="1"><PARAHEAD Level="1" CharShape="1">^1.</PARAHEAD></NUMBERING></HEAD>
<BODY><SECTION>
<P ParaShape="0"><TEXT CharShape="1"><CHAR>본문 바탕 10pt, 줄간격 160%</CHAR></TEXT></P>
<P ParaShape="1"><TEXT CharShape="0"><CHAR>다음 물음에 답하시오.</CHAR></TEXT></P>
<P ParaShape="0"><TEXT CharShape="0"><CHAR>① 가 ② 나 ③ 다 ④ 라 ⑤ 마</CHAR></TEXT></P>
<P ParaShape="0"><TEXT CharShape="0"><TABLE><SHAPEOBJECT TextWrap="TopAndBottom">
<SIZE Width="10000" WidthRelTo="Absolute" Height="2000" HeightRelTo="Absolute"/>
<POSITION TreatAsChar="true" HorzRelTo="Column" HorzAlign="Center" VertRelTo="Para"/>
<OUTSIDEMARGIN Left="100" Right="100"/></SHAPEOBJECT>
<ROW><CELL><PARALIST><P ParaShape="0"><TEXT CharShape="0"><CHAR>&lt;보기&gt; ㄱ. 조건</CHAR></TEXT></P></PARALIST></CELL></ROW>
</TABLE><PICTURE><SHAPEOBJECT><SIZE Width="3000" WidthRelTo="Absolute"/>
<POSITION TreatAsChar="false" HorzRelTo="Column" HorzAlign="Center"/></SHAPEOBJECT></PICTURE></TEXT></P>
</SECTION></BODY></HWPML>'''


def row(report, role, prop):
    return next(r for r in report.rows() if (r['role'], r['property']) == (role, prop))


def test_roles_units_and_native_numbering():
    report = analyze_hwpml(XML)
    assert row(report, 'body', 'size')['observed'] == ['10 pt']  # excludes 11pt instruction text
    assert row(report, 'body', 'size')['status'] == '일치'
    assert row(report, 'number', 'size')['observed'] == ['11 pt']
    assert row(report, 'number', 'number_format')['observed'] == ['^1.']
    assert row(report, 'body', 'line_spacing')['observed'] == ['160 %']
    assert row(report, 'choice', 'markers')['observed'] == ['①②③④⑤']
    assert row(report, 'example', 'size')['observed'] == ['10 pt']
    assert row(report, 'box', 'treat_as_char')['observed'] == ['true']
    assert row(report, 'picture', 'treat_as_char')['observed'] == ['false']
    assert row(report, 'passage', 'font')['status'] == '확인 불가'


def test_scoped_prose_and_conflicts():
    rules, unknown = parse_instructions(['본문과 선택지는 바탕 10포인트, 줄간격은 160%',
                                        '보기 줄간격 14pt', '문제 번호 11pt'])
    assert not unknown, unknown
    assert {(r.role, r.property, r.value) for r in rules} >= {
        ('body', 'size', '10 pt'), ('choice', 'size', '10 pt'),
        ('body', 'line_spacing', '160 %'), ('example', 'line_spacing', '14 pt')}
    assert not any(r.role == 'example' and r.property == 'size' for r in rules)
    report = analyze_hwpml(XML, '본문 12pt')
    assert row(report, 'body', 'size')['status'] == '지시문 충돌'
    report = analyze_hwpml(XML.replace('본문 바탕 10pt', '본문 바탕 12pt'))
    assert row(report, 'body', 'size')['status'] == '불일치·확인 필요'


def test_unsupported_does_not_become_a_rule():
    lines = ['본문 10pt 이하', '본문 10pt로 하지 말 것', '선택지 줄간격 160',
             '그림이 있는 경우 본문 9pt', '10pt', '본문 바탕 10pt, 자간 -5%']
    rules, unknown = parse_instructions(lines)
    assert len(unknown) >= 6, unknown
    assert all(r.location.startswith('지시문 6:') for r in rules), rules
    assert not any(r.property == 'line_spacing' for r in rules)


def test_blank_failure_and_mixed_styles():
    report = analyze_hwpml('<HWPML><BODY><SECTION/></BODY></HWPML>')
    assert all(r['status'] == '확인 불가' for r in report.rows())
    class Broken:
        @property
        def automation(self):
            raise RuntimeError('test export failure')
    failure = capture_format_report(Broken(), '본문 10pt')
    assert failure.warnings and row(failure, 'body', 'size')['status'] == '지시문만 확인'
    mixed = XML.replace('다음 물음에 답하시오.</CHAR></TEXT>',
                        '다음 물음에 답하시오.</CHAR></TEXT><TEXT CharShape="1"><CHAR>강조</CHAR></TEXT>')
    assert row(analyze_hwpml(mixed), 'body', 'size')['observed'] == ['10 pt', '11 pt']
    assert compare_output(analyze_hwpml(XML), failure)['status'] == '검토 필요'
    for xml in ['<bad/>', '<!DOCTYPE HWPML [<!ENTITY a "bad">]><HWPML/>']:
        try:
            analyze_hwpml(xml)
        except ValueError:
            pass
        else:
            raise AssertionError('unsupported XML accepted')


def test_review_changes_require_new_acknowledgement():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from app.ui.format_review import FormatReviewDialog
    app = QApplication.instance() or QApplication([])
    dialog = FormatReviewDialog(analyze_hwpml(XML))
    assert not dialog.accept_button.isEnabled()
    dialog.acknowledge.setChecked(True)
    assert dialog.accept_button.isEnabled()
    dialog.instructions.setPlainText('본문 12pt')
    assert not dialog.accept_button.isEnabled()
    assert row(dialog.report, 'body', 'size')['status'] == '지시문 충돌'
    dialog.override.setChecked(True)
    assert row(dialog.report, 'body', 'size')['declared'] == ['12 pt']
    assert row(dialog.report, 'body', 'size')['status'] == '불일치·확인 필요'
    assert row(dialog.report, 'body', 'size')['user_override']
    dialog.instructions.clear()
    assert row(dialog.report, 'body', 'size')['status'] == '일치'
    dialog.close()
    app.processEvents()


def test_literal_number_after_section_controls():
    from app.services.auto_qid import DetectedQuestion, strip_literal_number_prefixes
    from unittest.mock import patch
    class Document:
        def __init__(self):
            self.text = "1. 다음 문항"
            self.automation = self

        def set_position(self, value):
            assert value == (0, 0, 0)
            self.position = (0, 0, 16)

        def Run(self, command):
            assert command == "Delete" and self.position == (0, 0, 16)
            self.text = self.text[1:]

        def control_summaries_for_ranges(self, ranges):
            return [{"EQUATION": 1} for span in ranges]

    doc = Document()
    with patch("app.services.auto_qid.paragraph_text", side_effect=lambda doc, index: doc.text):
        strip_literal_number_prefixes(doc, (DetectedQuestion('Q001', 0, 'test', True, 'literal', 3),))
    assert doc.text == "다음 문항"


def main():
    for name, value in list(globals().items()):
        if name.startswith('test_'):
            value()
    print('Format analysis and review tests passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
