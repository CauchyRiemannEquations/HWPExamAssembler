"""Synchronize the school editor with one native, user-owned Hangul document tab."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
from threading import Condition
import uuid
import pythoncom

from app.hwp.application import HwpApplication, HwpAutomationError
from app.hwp.fields import QuestionFieldManager, TERMINAL_FIELD, question_field
from app.hwp.reorder import create_timestamped_backup
from app.hwp.transaction import apply_reorder_transaction, sha256
from app.hwp.user_document import UserDocument, is_document_open, native_content_fingerprint
from .exam_service import DocumentSnapshot
from .external_edit import file_is_in_use


class LiveOrderBatch:
    """Share list changes with the native worker until it commits to publishing."""

    def __init__(self, order, focus_qid=None):
        self._order = tuple(order)
        self._ids = frozenset(self._order)
        self._focus = focus_qid
        self._condition = Condition()
        self._editing = False
        self._sealed = False

    def update(self, order, focus_qid):
        order = tuple(order)
        with self._condition:
            if self._sealed or len(order) != len(self._ids) or frozenset(order) != self._ids:
                return False
            self._order, self._focus = order, focus_qid
            self._condition.notify_all()
            return True

    def latest(self):
        with self._condition:
            return self._order, self._focus

    def set_editing(self, editing):
        with self._condition:
            self._editing = editing
            self._condition.notify_all()

    def wait_until_idle(self):
        with self._condition:
            self._condition.wait_for(lambda: not self._editing)

    def claim(self, order):
        # No native or disk operation holds this lock on the UI thread.
        with self._condition:
            if self._sealed or self._editing or self._order != order:
                return False
            self._sealed = True
            return True


class _OrderChanged(Exception):
    """Discard private staging and restart from the one original checkpoint."""


class _ScratchWorker:
    """Create, use and release the private HWP server on one COM apartment."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hwp-exam-scratch")
        self._hwp = None

    def _start(self):
        if self._hwp is None:
            self._hwp = HwpApplication(visible=False).__enter__()

    def prewarm(self):
        # Startup never touches a document or the clipboard. If it fails, a
        # subsequent real operation will retry and can report a relevant error.
        self._executor.submit(self._start)

    def run(self, action):
        # The owner apartment must not move the visible caret while the private
        # Hangul instance is selecting, cutting or pasting a question.
        return self._executor.submit(self._run, action).result()

    def _run(self, action):
        self._start()
        try:
            return action(self._hwp)
        except BaseException:
            try:
                self._close_hwp()
            except Exception:
                pass  # Preserve the operation's error when the COM server disconnected.
            raise

    def _close_hwp(self):
        hwp, self._hwp = self._hwp, None
        if hwp is not None:
            hwp.close()

    def close(self):
        try:
            self._executor.submit(self._close_hwp).result()
        finally:
            self._executor.shutdown(wait=True)


class LiveDocumentSession:
    """Own one apartment-local connection. All methods run on the same worker."""

    def __init__(self, service):
        self.service = service
        self.target = None
        self._scratch = None
        self.last_warning = ""

    def open(self, snapshot, *, only_if_open=False):
        self.release()
        target = UserDocument(snapshot.path)
        if not target.connect(file_is_in_use, only_if_open=only_if_open):
            return None
        self.target = target
        try:
            fields = self._check_structure(snapshot)
            if fields.ordered_question_ids() == snapshot.order and not target.doc.Modified and sha256(target.path) == snapshot.content_sha256:
                self._scratch_worker().prewarm()
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
        children_by_group = {}
        for child in fields.child_question_fields():
            children_by_group.setdefault(child.group_id, []).append(child.question_id)
        if not target.automation.FieldExist(TERMINAL_FIELD) or children_by_group.keys() - set(actual):
            raise HwpAutomationError("문항 경계를 확인할 수 없어 반영하지 않았습니다.")
        for question in expected.questions:
            if tuple(children_by_group.get(question.id, ())) != question.child_ids:
                raise HwpAutomationError("공통 지문에 속한 문항이 달라져 반영할 수 없습니다.")
        return fields

    def navigate(self, qid, activate=True):
        target = self.target
        if not target:
            raise HwpAutomationError("한글 편집본을 먼저 연결하세요.")
        if activate:
            target.show_target()
        else:
            target.require_target()
            if not target.input_available():
                raise HwpAutomationError("한글 대화상자를 먼저 닫아 주세요.")
        if qid:
            manager = QuestionFieldManager(target)
            if qid not in manager.ordered_question_ids():
                raise HwpAutomationError("한글에 해당 문항이 없습니다.")
            self._show_question(manager, qid)

    @staticmethod
    def _show_question(manager, qid):
        # Move the native viewport without bringing Hangul in front of the list.
        manager.focus_field(question_field(qid))

    def _focus_result(self, result, qid):
        if qid in result.order:
            try:
                self.target.require_target()
                self._show_question(QuestionFieldManager(self.target), qid)
            except Exception as error:
                self.last_warning = f"선택 문항 위치 이동 실패: {error}"
        return result

    def _scratch_worker(self):
        if self._scratch is None:
            self._scratch = _ScratchWorker()
        return self._scratch

    def _close_scratch(self):
        scratch, self._scratch = self._scratch, None
        if scratch is not None:
            scratch.close()

    def _read_copy(self, file, original):
        def read(reader):
            reader.open(file)
            fields = QuestionFieldManager(reader)
            questions = tuple(self.service.read_open_questions(
                reader, fields, fields.ordered_question_ids(), self.service._import_metadata(original)
            ))
            reader.reset_documents()
            return questions
        return self._scratch_worker().run(read)

    def _verify_live_copy(self, payload, baseline, checkpoint):
        """Keep a memory export only if Hancom reopens it as the same native document."""
        if not payload:
            return False
        try:
            content = base64.b64decode(payload)
        except ValueError:
            return False
        if not content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
            return False
        export = baseline if baseline.suffix.lower() == ".hwp" else baseline.with_suffix(".hwp")
        export.write_bytes(content)

        def verify(reader):
            reader.open(export)
            if export != baseline:
                reader.save_exam_copy(baseline)
                reader.reset_documents()
                reader.open(baseline)
            matched = native_content_fingerprint(reader) == checkpoint
            reader.reset_documents()
            return matched

        try:
            return self._scratch_worker().run(verify)
        except Exception:
            # Some Hancom versions/layouts cannot round-trip the memory format.
            # Preserve those edits through the original native format instead.
            return False

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

    def apply(self, snapshot, order, focus_qid=None, batch=None):
        order = tuple(order)
        self.last_warning = ""
        target = self.target
        if target is None or target.path != snapshot.path.resolve():
            raise HwpAutomationError("선택한 편집본과 연결한 한글 문서가 다릅니다.")
        if not order or len(order) != len(set(order)) or not set(order).issubset(snapshot.order):
            raise ValueError("반영할 문항 순서가 올바르지 않습니다.")
        live_updates = batch is not None
        if live_updates and set(order) != set(snapshot.order):
            raise ValueError("자동 이동 중에는 문항을 삭제할 수 없습니다.")
        if batch is None:
            if order == snapshot.order:
                self._check_structure(snapshot, exact_order=True)
                return self.refresh(snapshot)
            batch = LiveOrderBatch(order, focus_qid)
        if set(batch.latest()[0]) != set(order):
            raise ValueError("이동 중 문항 목록이 달라졌습니다.")
        if batch.latest()[0] == snapshot.order and batch.claim(snapshot.order):
            # The list can return to its original order before the worker
            # even starts. Read the live buffer without saving pending edits.
            self._check_structure(snapshot, exact_order=True)
            return self._focus_result(self.refresh(snapshot), batch.latest()[1])

        source = target.path
        directory = self.service.work_dir / f"live-reorder-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        baseline = directory / "original" / source.name
        baseline.parent.mkdir()
        staged = directory / source.name
        shown = None

        def follow_focus():
            nonlocal shown
            _, qid = batch.latest()
            if qid is None or qid == shown:
                return
            try:
                # Call only between scratch operations on the owner apartment.
                # Never activate a different tab or interrupt a native dialog.
                target.require_target()
                if target.input_available():
                    self._show_question(QuestionFieldManager(target), qid)
                    shown = qid
            except Exception:
                pass  # The publication checkpoint will report a native conflict.

        try:
            with target.guard_input():
                if int(target.doc.EditMode) != 1 or not os.access(source, os.W_OK):
                    raise PermissionError("한글 편집본이 읽기 전용입니다.")
                dirty = bool(target.doc.Modified)
                manager = self._check_structure(snapshot, exact_order=True)
                order, focus_qid = batch.latest()
                if focus_qid in order:
                    self._show_question(manager, focus_qid)
                    shown = focus_qid
                target.require_target()
                dirty = dirty or bool(target.doc.Modified)
                checkpoint = target.content_fingerprint()
                disk_before = sha256(source)
                payload = None
                if dirty:
                    try:
                        payload = target.automation.GetTextFile("HWP", "")
                    except pythoncom.com_error:
                        pass  # A native save in the original format is the fallback.
                else:
                    shutil.copyfile(source, baseline)

            live_copy = False
            if dirty:
                follow_focus()
                live_copy = self._verify_live_copy(payload, baseline, checkpoint)
                follow_focus()
            if dirty and not live_copy:
                if batch.latest()[0] == snapshot.order:
                    batch.wait_until_idle()
                    if batch.claim(snapshot.order):
                        return self._focus_result(self.refresh(snapshot), batch.latest()[1])
                with target.guard_input():
                    self._check_checkpoint(disk_before, checkpoint)
                    # Native export was unavailable or lost information.
                    # A true SaveAs in the original format preserves those edits.
                    create_timestamped_backup(source, source.parent / "backup")
                    target.save_current()
                    checkpoint = target.content_fingerprint()
                    disk_before = sha256(source)
                    shutil.copyfile(source, baseline)
            before = sha256(baseline)
            if not live_copy and before != disk_before:
                raise HwpAutomationError("작업 사본을 만드는 중 편집본이 바뀌었습니다. 다시 시도하세요.")

            profile = self.service._numbering_profile_for(source)
            metadata = self.service._import_metadata(source)
            captured = None
            audit = None
            while True:
                batch.wait_until_idle()
                follow_focus()
                order, _ = batch.latest()
                if order == snapshot.order:
                    # Undoing all moves requires no native reopen, backup, or
                    # original-file write. Detect external changes as usual.
                    with target.guard_input():
                        self._check_checkpoint(disk_before, checkpoint)
                        if not batch.claim(order):
                            continue
                    result = snapshot if not dirty and before == snapshot.content_sha256 else self.refresh(snapshot)
                    break

                if captured is None or captured.order != order:
                    shutil.copyfile(baseline, staged)
                    captured, audit = self._stage_order(
                        staged, snapshot, order, batch, before, profile, metadata, live_updates,
                    )
                    follow_focus()
                    if captured is None:
                        continue
                    batch.wait_until_idle()
                    if batch.latest()[0] != order:
                        continue

                dirty_backup = (baseline, before) if live_copy else None
                backup = self._publish(staged, disk_before, captured.content_sha256, order, snapshot, checkpoint,
                                       batch, dirty_backup)
                if backup is None:
                    continue
                result = replace(captured, path=source)
                break

            self._focus_result(result, batch.latest()[1])
            if result.order != snapshot.order:
                self.service.remember_inspection(result, metadata)
                try:
                    audit = replace(audit, document=str(source), backup=str(backup))
                    audit_path = source.with_suffix(source.suffix + ".audit.json")
                    audit_path.write_text(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError as error:
                    self.last_warning = " · ".join(filter(None, (self.last_warning, f"작업 기록 저장 실패: {error}")))
            return result
        finally:
            # Retain the staging copy if publication recovery left it displayed.
            try:
                still_on_source = Path(target.doc.FullName).resolve() == source
            except Exception:
                still_on_source = False
            if still_on_source:
                shutil.rmtree(directory, ignore_errors=True)

    def _stage_order(self, staged, snapshot, order, batch, before, profile, metadata, live_updates):
        captured = None
        numbered = frozenset(snapshot.numbered_ids)

        def check_order(_message):
            if batch.latest()[0] != order:
                raise _OrderChanged

        def read_verified(manager, controls):
            nonlocal captured
            check_order(None)
            captured = self.service.capture_verified_list(manager, order, controls, metadata, required=True)

        def reorder(worker):
            with tempfile.TemporaryDirectory(dir=staged.parent) as attempt:
                try:
                    return apply_reorder_transaction(
                        worker, staged, order, tuple(qid for qid in order if qid in numbered),
                        numbering_style=profile, backup_dir=Path(attempt) / "backup", work_dir=attempt,
                        allow_deletions=True, expected_sha256=before, on_verified=read_verified,
                        expected_order=snapshot.order, on_progress=check_order if live_updates else None,
                        focus_qid=batch.latest()[1], prefer_whole_paragraphs=live_updates,
                    )
                except _OrderChanged:
                    # The transaction has discarded its private native
                    # tab. Keep the warm scratch server for the latest order.
                    return None

        audit = self._scratch_worker().run(reorder)
        if audit is None:
            return None, None
        final_hash = sha256(staged)
        if (captured is None or captured.order != order or captured.content_sha256 != final_hash
                or final_hash != audit.after_sha256):
            raise HwpAutomationError("재배열 사본의 문항 순서를 확인할 수 없습니다.")

        return captured, audit

    def _check_checkpoint(self, before_hash, checkpoint):
        target = self.target
        if target.content_fingerprint() != checkpoint:
            raise HwpAutomationError("재배열을 준비하는 동안 한글 내용이 달라져 반영하지 않았습니다. 다시 시도하세요.")
        if sha256(target.path) != before_hash:
            raise HwpAutomationError("재배열을 준비하는 동안 디스크의 한글 파일이 바뀌어 반영하지 않았습니다. 다시 시도하세요.")

    def _publish(self, staged, before_hash, final_hash, order, snapshot, checkpoint, batch, dirty_backup=None):
        target = self.target
        if target is None or target.path != snapshot.path.resolve():
            raise HwpAutomationError("재배열을 준비하는 동안 연결한 한글 문서가 바뀌었습니다.")
        source = target.path
        fd, name = tempfile.mkstemp(dir=source.parent, prefix=".exam-reorder-", suffix=".tmp")
        os.close(fd)
        incoming = Path(name)
        try:
            shutil.copyfile(staged, incoming)
            if sha256(incoming) != final_hash:
                raise HwpAutomationError("재배열 사본의 파일 지문이 다릅니다.")
            with target.guard_input():
                # The checkpoint covers the entire native document, including
                # the QIDs and group structure validated before staging.
                self._check_checkpoint(before_hash, checkpoint)
                if not batch.claim(order):
                    return None
                backup = create_timestamped_backup(source, source.parent / "backup")
                if sha256(backup) != before_hash:
                    raise HwpAutomationError("백업을 만드는 중 편집본이 바뀌었습니다. 다시 시도하세요.")
                if dirty_backup is not None:
                    live_copy, live_hash = dirty_backup
                    backup = create_timestamped_backup(live_copy, source.parent / "backup")
                    if sha256(backup) != live_hash:
                        raise HwpAutomationError("한글의 최신 수정 내용을 백업하지 못했습니다.")
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
                        self._recover(staged, backup, published, final_hash, incoming, displayed_content,
                                      dirty_disk_hash=before_hash if dirty_backup is not None else None)
                    except Exception as recovery:
                        raise HwpAutomationError(f"반영을 마치지 못했습니다. 최근 수정 백업: {backup}\n{recovery}") from error
                    raise
                return backup
        finally:
            incoming.unlink(missing_ok=True)

    def _recover(self, staged, backup, published, final_hash, temporary, displayed_content, *, dirty_disk_hash=None):
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
        # If the tab released an unsaved native buffer before publication failed,
        # restore the verified copy of that buffer alongside its pre-edit backup.
        expected_disk = final_hash if published else dirty_disk_hash
        if dirty_disk_hash and sha256(source) != expected_disk:
            raise HwpAutomationError(f"다른 프로그램이 원본을 변경했습니다. 한글의 최신 수정 백업: {backup}")
        if expected_disk and sha256(source) == expected_disk:
            if current == source:
                target.reopen(source, staged, unchanged_content=displayed_content)
                current = staged
            shutil.copyfile(backup, temporary)
            if sha256(source) != expected_disk:
                raise HwpAutomationError("다른 프로그램이 문서를 변경해 백업 복원을 중단했습니다.")
            os.replace(temporary, source)
        if current == staged:
            target.reopen(staged, source, unchanged_content=displayed_content)

    def release(self):
        try:
            self._close_scratch()
        finally:
            if self.target:
                self.target.close()
                self.target = None
