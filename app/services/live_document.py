"""Synchronize the school editor with one native, user-owned Hangul document tab."""
from __future__ import annotations

import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from app.hwp.application import HwpApplication, HwpAutomationError
from app.hwp.fields import QuestionFieldManager, TERMINAL_FIELD, question_field
from app.hwp.reorder import create_timestamped_backup
from app.hwp.transaction import apply_reorder_transaction, sha256
from app.hwp.user_document import UserDocument, is_document_open
from .exam_service import DocumentSnapshot
from .external_edit import file_is_in_use


class LiveDocumentSession:
    """Own one apartment-local connection. All methods run on the same worker."""

    def __init__(self, service):
        self.service = service
        self.target = None
        self.last_warning = ""

    def open(self, snapshot):
        self.release()
        target = UserDocument(snapshot.path)
        target.connect(file_is_in_use)
        self.target = target
        try:
            fields = self._check_structure(snapshot)
            if fields.ordered_question_ids() == snapshot.order and not target.doc.Modified and sha256(target.path) == snapshot.content_sha256:
                return snapshot
            return self.refresh(snapshot)
        except BaseException:
            self.release()
            raise

    def probe(self):
        return self.target.state() if self.target else "closed"

    def _check_structure(self, expected, *, exact_order=False):
        target = self.target
        if not target or target.path != expected.path.resolve():
            raise HwpAutomationError("선택한 편집본과 연결한 한글 문서가 다릅니다.")
        target.require_target()
        fields = QuestionFieldManager(target)
        actual = fields.ordered_question_ids()
        if len(actual) != len(set(actual)) or set(actual) != set(expected.order) or (exact_order and actual != expected.order):
            raise HwpAutomationError("한글에서 문항 순서나 내부 ID가 달라졌습니다. 한글 편집본을 확인하세요.")
        for question in expected.questions:
            children = tuple(child.question_id for child in fields.child_question_fields(question.id))
            if children != question.child_ids:
                raise HwpAutomationError("공통 지문에 속한 문항이 달라져 반영할 수 없습니다.")
        if (not target.automation.FieldExist(TERMINAL_FIELD)
                or any(child.group_id not in expected.order for child in fields.child_question_fields())):
            raise HwpAutomationError("문항 경계를 확인할 수 없어 반영하지 않았습니다.")
        return fields

    def navigate(self, qid):
        target = self.target
        if not target:
            raise HwpAutomationError("한글 편집본을 먼저 연결하세요.")
        target.show_target()
        if qid:
            manager = QuestionFieldManager(target)
            if qid not in manager.ordered_question_ids():
                raise HwpAutomationError("한글에 해당 문항이 없습니다.")
            target.set_position(manager.position(question_field(qid)))

    def _read_copy(self, file, original):
        with HwpApplication(visible=False) as reader:
            reader.open(file)
            fields = QuestionFieldManager(reader)
            return tuple(self.service.read_open_questions(
                reader, fields, fields.ordered_question_ids(), self.service._import_metadata(original)
            ))

    def refresh(self, expected):
        self._check_structure(expected)
        target = self.target
        # Native memory export includes unsaved edits without saving, selecting,
        # scrolling or changing the teacher's caret in the live document.
        with target.guard_input():
            payload = target.automation.GetTextFile("HWP", "")
            if not payload:
                raise HwpAutomationError("한글에서 현재 문항을 읽을 수 없습니다.")
            content = base64.b64decode(payload)
            if not content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
                raise HwpAutomationError("한글 문서 사본을 확인할 수 없습니다.")
            digest = None if target.doc.Modified else sha256(target.path)
        self.service.work_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.service.work_dir) as directory:
            copy = Path(directory) / "live.hwp"
            copy.write_bytes(content)
            questions = self._read_copy(copy, target.path)
        result = DocumentSnapshot(target.path, questions, content_sha256=digest)
        if len(result.order) != len(expected.order) or set(result.order) != set(expected.order):
            raise HwpAutomationError("한글 문항 순서가 바뀌어 다시 확인해야 합니다.")
        return result

    def apply(self, snapshot, order, focus_qid=None):
        order = tuple(order)
        self.last_warning = ""
        self._check_structure(snapshot, exact_order=True)
        target = self.target
        source = target.path
        if not order or len(order) != len(set(order)) or not set(order).issubset(snapshot.order):
            raise ValueError("반영할 문항 순서가 올바르지 않습니다.")
        if order == snapshot.order:
            return self.refresh(snapshot)
        if int(target.doc.EditMode) != 1 or not os.access(source, os.W_OK):
            raise PermissionError("한글 편집본이 읽기 전용입니다.")

        # Save only the selected document. Preserve both the old on-disk
        # document and the teacher's current, possibly newer, edits.
        backups = source.parent / "backup"
        with target.guard_input():
            self._check_structure(snapshot, exact_order=True)
            if target.doc.Modified:
                # Serialize pending native equation/shape edits before checkpointing.
                target.content_fingerprint()
                create_timestamped_backup(source, backups)
                target.save_current()
            target.require_target()
            checkpoint = target.content_fingerprint()
            backup = create_timestamped_backup(source, backups)
            before = sha256(backup)
            if sha256(source) != before:
                raise HwpAutomationError("백업을 만드는 중 편집본이 바뀌었습니다. 다시 시도하세요.")

        directory = self.service.work_dir / f"live-reorder-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        staged = directory / source.name
        try:
            shutil.copyfile(backup, staged)
            profile = self.service._numbering_profile_for(source)
            numbered = frozenset(snapshot.numbered_ids)
            with HwpApplication(visible=False) as worker:
                audit = apply_reorder_transaction(
                    worker, staged, order, tuple(qid for qid in order if qid in numbered),
                    numbering_style=profile, backup_dir=directory / "backup", work_dir=directory,
                    allow_deletions=True, expected_sha256=before,
                )
            questions = self._read_copy(staged, source)
            final_hash = sha256(staged)
            if tuple(q.id for q in questions) != order or final_hash != audit.after_sha256:
                raise HwpAutomationError("재배열 사본의 문항 순서를 확인할 수 없습니다.")
            # User input stays available during the expensive native reorder.
            # The short publication phase operates on the bound IXHwpDocument,
            # preserving its DocumentID and HWND across both native Open calls.
            self._publish(staged, backup, before, final_hash, order, snapshot, checkpoint)
            result = DocumentSnapshot(source, questions, content_sha256=final_hash)
            warnings = []
            if focus_qid in order:
                try:
                    target.require_target()
                    manager = QuestionFieldManager(target)
                    target.set_position(manager.position(question_field(focus_qid)))
                except Exception as error:
                    warnings.append(f"선택 문항 위치 이동 실패: {error}")
            try:
                audit = replace(audit, document=str(source), backup=str(backup))
                audit_path = source.with_suffix(source.suffix + ".audit.json")
                audit_path.write_text(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as error:
                warnings.append(f"작업 기록 저장 실패: {error}")
            self.last_warning = " · ".join(warnings)
        finally:
            # If recovery failed and the tab still displays the staging copy,
            # retain it. The user's permanent backup is kept independently.
            try:
                still_on_source = Path(target.doc.FullName).resolve() == source
            except Exception:
                still_on_source = False
            if still_on_source:
                shutil.rmtree(directory, ignore_errors=True)
        return result

    def _publish(self, staged, backup, before_hash, final_hash, order, snapshot, checkpoint):
        target = self.target
        source = target.path
        fd, name = tempfile.mkstemp(dir=source.parent, prefix=".school-reorder-", suffix=".tmp")
        os.close(fd)
        incoming = Path(name)
        try:
            shutil.copyfile(staged, incoming)
            if sha256(incoming) != final_hash:
                raise HwpAutomationError("재배열 사본의 파일 지문이 다릅니다.")
            with target.guard_input():
                self._check_structure(snapshot, exact_order=True)
                if target.content_fingerprint() != checkpoint:
                    raise HwpAutomationError("재배열을 준비하는 동안 한글 내용이 달라져 반영하지 않았습니다. 다시 시도하세요.")
                if sha256(source) != before_hash:
                    raise HwpAutomationError("재배열을 준비하는 동안 디스크의 한글 파일이 바뀌어 반영하지 않았습니다. 다시 시도하세요.")
                published = False
                displayed_content = None
                try:
                    # This releases the old file lock without closing the
                    # document object or the user's window.
                    target.reopen(source, staged, unchanged_content=checkpoint)
                    displayed_content = target.content_fingerprint()
                    if sha256(source) != before_hash:
                        raise HwpAutomationError("반영 직전 파일이 변경되었습니다.")
                    os.replace(incoming, source)
                    published = True
                    target.reopen(staged, source, unchanged_content=displayed_content)
                    if QuestionFieldManager(target).ordered_question_ids() != order:
                        raise HwpAutomationError("한글 화면의 문항 순서가 재배열본과 다릅니다.")
                except Exception as error:
                    try:
                        self._recover(staged, backup, published, final_hash, incoming, displayed_content)
                    except Exception as recovery:
                        raise HwpAutomationError(f"반영을 마치지 못했습니다. 최근 수정 백업: {backup}\n{recovery}") from error
                    raise
        finally:
            incoming.unlink(missing_ok=True)

    def _recover(self, staged, backup, published, final_hash, temporary, displayed_content):
        target = self.target
        source = target.path
        current = Path(target.doc.FullName).resolve()
        if current not in (source, staged):
            raise HwpAutomationError("다른 편집 내용이 있어 자동 복구를 중단했습니다.")
        if not published and current == source:
            return  # The original tab and disk file were never replaced.
        target.require_target(current)
        if target.doc.Modified and (displayed_content is None or target.content_fingerprint() != displayed_content):
            raise HwpAutomationError("한글에 다른 수정 내용이 있어 백업 복원을 중단했습니다.")
        if published and sha256(source) == final_hash:
            if current == source:
                target.reopen(source, staged, unchanged_content=displayed_content)
                current = staged
            shutil.copyfile(backup, temporary)
            if sha256(source) != final_hash:
                raise HwpAutomationError("다른 프로그램이 문서를 변경해 백업 복원을 중단했습니다.")
            os.replace(temporary, source)
        if current == staged:
            target.reopen(staged, source, unchanged_content=displayed_content)

    def release(self):
        if self.target:
            self.target.close()
            self.target = None
