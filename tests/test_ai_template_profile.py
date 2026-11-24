from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from app.services.ai_template_profile import load_profile, validate_profile, profile_evidence, ProfileError, REQUIRED_CHECKS
from app.services.format_analysis import analyze_hwpml, compare_output
from test_format_analysis import XML, row

ASSETS = ROOT / 'ai-template'


def rejected(data):
    try:
        validate_profile(data)
    except ProfileError:
        return
    raise AssertionError('invalid profile accepted')


def test_validation_and_assets():
    sample = load_profile(ASSETS / 'example-profile.json')
    blank = load_profile(ASSETS / 'blank-profile.json')
    assert REQUIRED_CHECKS <= {(r['role'], r['property']) for r in blank['rules']}
    candidates, notes, allowed = profile_evidence(sample)
    assert allowed and len(candidates) == 4
    assert {(r.role, r.property, r.value) for r in candidates} >= {('body', 'size', '10 pt'), ('body', 'size', '11 pt')}
    assert not any(r.role in ('example', 'picture') for r in candidates)
    assert any('조건: 보기 상자' in n for n in notes)
    assert not profile_evidence(blank)[0]
    index = next(i for i, r in enumerate(sample['rules']) if r['role'] == 'body' and r['property'] == 'size')
    for field, value in [('value', '10pt'), ('value', True), ('value', float('inf')), ('value', 10**400),
                         ('unit', 'mm'), ('role', []), ('property', {}), ('evidence_ids', ['missing']),
                         ('evidence_ids', ['E1', 'E1']), ('evidence_ids', []), ('status', 'unknown')]:
        broken = deepcopy(sample)
        broken['rules'][index][field] = value
        rejected(broken)
    broken = deepcopy(sample)
    broken['rules'].append(deepcopy(broken['rules'][0]))
    rejected(broken)
    broken = deepcopy(sample)
    broken['evidence'][0]['quote'] = None
    rejected(broken)
    broken = deepcopy(sample)
    broken['command'] = 'never execute'
    rejected(broken)
    for status, confidence, scope, condition, kind in [
            ('inferred', 'high', 'all', None, 'native_property'),
            ('observed', 'high', 'all', None, 'visual'),
            ('observed', 'low', 'all', None, 'native_property'),
            ('observed', 'high', 'objective', None, 'native_property'),
            ('observed', 'high', 'all', '10pt 이하', 'native_property')]:
        limited = deepcopy(sample)
        limited['rules'] = [limited['rules'][index]]
        limited['rules'][0].update(status=status, confidence=confidence, scope=scope, condition=condition)
        limited['evidence'][0]['kind'] = kind
        assert not profile_evidence(limited)[0]
    prompt = (ASSETS / 'analysis-prompt.txt').read_text(encoding='utf-8')
    assert '10pt 이하' in prompt and '이미지/PDF' in prompt and 'JSON 객체 하나' in prompt


def test_loading_hash_and_no_reference_access():
    sample = load_profile(ASSETS / 'example-profile.json')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'input.json'
        for content in ['{"schema":1,"schema":2}', '{"x":NaN}', '```json\n{}\n```', 'x' * 1_000_001]:
            path.write_text(content, encoding='utf-8')
            try:
                load_profile(path)
            except ProfileError:
                pass
            else:
                raise AssertionError('malformed file accepted')
        path.write_text(json.dumps(sample), encoding='utf-8-sig')
        assert load_profile(path) == sample
        template = Path(directory) / 'template.hwp'
        template.write_bytes(b'synthetic template')
        sample['template']['sha256'] = hashlib.sha256(template.read_bytes()).hexdigest()
        assert profile_evidence(sample, template)[2]
        assert not profile_evidence(sample)[2]
        sample['template']['sha256'] = '0' * 64
        assert not profile_evidence(sample, template)[2]
        sample['template']['sha256'] = None
        sample['template']['name'] = 'https://example.invalid/never-open'
        sample['evidence'][0]['location'] = r'C:\never-open\file'
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('reference opened')):
            assert profile_evidence(sample)[2]


def test_dialog_adoption_conflict_export_and_saved_check():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from app.ui.format_review import FormatReviewDialog
    app = QApplication.instance() or QApplication([])
    report = analyze_hwpml(XML)
    dialog = FormatReviewDialog(report)
    dialog.import_ai_profile(ASSETS / 'example-profile.json')
    assert not report.external_enabled
    assert row(report, 'body', 'size')['status'] == '일치'
    dialog.acknowledge.setChecked(True)
    dialog.external_toggle.setChecked(True)
    assert not dialog.accept_button.isEnabled()
    assert row(report, 'body', 'size')['status'] == '지시문 충돌'
    checks = compare_output(report, analyze_hwpml(XML))['checks']
    assert any(c['role'] == 'body' and c['property'] == 'size' and c['status'] == '지시문 충돌' for c in checks)
    dialog.instructions.setPlainText('본문 10pt')
    dialog.override.setChecked(True)
    assert row(report, 'body', 'size')['status'] == '일치'
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'review.json'
        report.save(path)
        saved = json.loads(path.read_text(encoding='utf-8'))
        assert saved['external_enabled'] and saved['external_profile']['rules']
        assert saved['overrides'] and saved['external_notes']
        invalid = Path(directory) / 'bad.json'
        invalid.write_text('{}', encoding='utf-8')
        previous = deepcopy(report.to_dict())
        try:
            dialog.import_ai_profile(invalid)
        except ProfileError:
            pass
        assert report.to_dict() == previous
        export = Path(directory) / 'native.json'
        with patch('app.ui.format_review.QFileDialog.getSaveFileName', return_value=(str(export), 'JSON')):
            dialog.export_native_evidence()
        native = json.loads(export.read_text(encoding='utf-8'))
        assert native['type'] == 'hwp-exam-native-evidence' and native['observed']
        assert 'external_profile' not in native
        mismatch = deepcopy(report.external_profile)
        mismatch['template']['sha256'] = '0' * 64
        invalid.write_text(json.dumps(mismatch), encoding='utf-8')
        dialog.import_ai_profile(invalid)
        assert not dialog.external_toggle.isEnabled() and not report.external_enabled
    dialog.copy_ai_prompt()
    assert 'hwp-exam-template-analysis' in app.clipboard().text()
    dialog.clear_ai_profile()
    assert not report.external_rules and report.external_profile is None
    assert not dialog.external_toggle.isEnabled()
    dialog.close()
    app.processEvents()


def main():
    for name, value in list(globals().items()):
        if name.startswith('test_'):
            value()
    print('AI profile validation, candidate review, export and persistence tests passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
