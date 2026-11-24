"""Own the registrar's working copy and Hancom connection on one Qt worker."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal, Slot

from app.hwp.native_template import NativeTemplate
from app.services.template_catalog import RegisteredTemplate, template_data_root
from app.services.template_model import (
    StudioState, area_detail, file_digest, restore_registration, revise,
    save_registration, suggest, validate_registration,
)


class StudioWorker(QObject):
    updated = Signal(object)
    saved = Signal(object)
    native_opened = Signal(object)
    status = Signal(str)
    failed = Signal(str)
    finished = Signal()
    stopped = Signal()

    def __init__(self, data_root):
        super().__init__()
        self.data_root = data_root
        self.state = None
        self.native = None
        self.scratch = None
        self.signature = None

    def release(self):
        try:
            if self.native:
                self.native.close()
        finally:
            if self.scratch:
                self.scratch.cleanup()
            self.native = self.scratch = self.state = None

    @property
    def profile_path(self):
        return self.data_root / 'profiles' / (self.signature + '.json')

    def open(self, source):
        source = Path(source).resolve(strict=True)
        scratch = tempfile.TemporaryDirectory(prefix='HWP-Template-', ignore_cleanup_errors=True)
        native = NativeTemplate(scratch.name)
        try:
            self.status.emit('한글에서 양식의 작업 사본을 여는 중…')
            copy = native.open(source)
            signature = file_digest(copy)
            self.status.emit('문항 번호와 고정 안내를 읽는 중…')
            paragraphs = native.scan()
            candidates = suggest(paragraphs)
            saved = self.data_root / 'profiles' / (signature + '.json')
            if not saved.exists() and self.data_root == template_data_root():
                previous = self.data_root.parent / 'HWPExamTemplatePreview' / 'profiles' / saved.name
                if previous.is_file():
                    saved = previous
            regions, registered = restore_registration(saved, signature, candidates, paragraphs[-1].end)
            anchor = next(r for r in regions if r.key == 'insert')
            if anchor.start is not None:
                native.place(anchor.start)
            else:
                native.place(paragraphs[0].start)
            self.release()
        except Exception:
            try:
                native.close()
            finally:
                scratch.cleanup()
            raise
        self.native, self.scratch, self.signature = native, scratch, signature
        self.state = StudioState(source.stem, source, regions, registered, paragraphs)
        self.updated.emit(self.state)
        self.native_opened.emit(native.hwnd)
        self.status.emit('이 PC에 등록한 양식을 불러왔습니다.' if registered else '목록에서 영역을 누르면 한글에서 위치를 확인할 수 있습니다.')

    def show_region(self, region):
        if self.native.state() == 'closed':
            self.native.reopen()
            self.native_opened.emit(self.native.hwnd)
        self.native.show()
        if region.start is None:
            self.status.emit('이 영역은 직접 지정할 수 있습니다.')
            return
        if region.role == 'insert':
            self.native.place(region.start)
        else:
            self.native.select(region.start, region.end)
        self.status.emit('한글에서 선택한 항목을 표시했습니다.')

    def apply_proposal(self, proposed):
        current = self.state.regions
        valid_positions = {p for row in self.state.paragraphs for p in (row.start, row.end)}
        result = []
        for old, new in zip(current, proposed, strict=True):
            if (old.key, old.role) != (new.key, new.role):
                raise ValueError('양식 영역 목록이 달라졌습니다. 다시 분석해 주세요.')
            a, b = new.start, new.end
            if (a, b) == (old.start, old.end):
                result.append(old)
                continue
            if ((a is None) != (b is None)
                    or a is None and old.role == 'insert'
                    or a is not None and (
                        any(p not in valid_positions and p not in (old.start, old.end) for p in (a, b))
                        or (a != b if old.role == 'insert' else a >= b))):
                raise ValueError('AI 제안의 한글 위치를 확인할 수 없습니다.')
            result.append(replace(old, start=a, end=b, detail=area_detail(a, b), corrected=False))
        validate_registration(result)
        self.state = replace(self.state, regions=tuple(result), registered=False)
        self.updated.emit(self.state)
        self.status.emit('제안을 적용했습니다. 한글에서 위치를 확인한 뒤 양식을 등록하세요.')

    @Slot(str, object)
    def run(self, operation, key):
        try:
            if operation == 'close':
                try:
                    self.release()
                finally:
                    # All native handles have been released on their original
                    # apartment. Dispose of the Python QObject on the GUI thread.
                    self.moveToThread(QCoreApplication.instance().thread())
                    self.stopped.emit()
                return
            if operation == 'open':
                self.open(key)
            elif operation == 'save':
                if file_digest(self.state.source) != self.signature:
                    raise ValueError('원본 파일이 바뀌었습니다. 파일을 다시 열고 위치를 확인해 주세요.')
                save_registration(self.profile_path, self.signature, self.state.regions, source_path=self.state.source)
                self.state = replace(self.state, registered=True)
                self.updated.emit(self.state)
                self.saved.emit(RegisteredTemplate(self.state.source, self.profile_path))
            elif operation == 'proposal':
                self.apply_proposal(key)
            else:
                region = next(r for r in self.state.regions if r.key == key)
                if operation == 'show':
                    self.show_region(region)
                else:
                    if operation == 'capture':
                        if self.native.state() == 'closed':
                            self.show_region(region)
                            raise ValueError('한글 사본을 다시 열었습니다. 새 위치를 직접 짚은 뒤 적용하세요.')
                        a, b = self.native.capture(point=region.role == 'insert')
                    elif operation == 'clear':
                        a = b = None
                    else:
                        raise ValueError('알 수 없는 작업입니다.')
                    revised = revise(self.state.regions, key, a, b)
                    self.state = replace(self.state, regions=revised, registered=False)
                    self.updated.emit(self.state)
                    self.status.emit(f'{region.label} 지정을 해제했습니다.' if operation == 'clear' else f'{region.label}에 한글에서 짚은 위치를 적용했습니다.')
        except Exception as error:
            self.failed.emit(str(error) or '한글 연결을 확인한 뒤 다시 시도하세요.')
        finally:
            self.finished.emit()


class StudioController(QObject):
    state_changed = Signal(object)
    saved = Signal(object)
    native_opened = Signal(object)
    busy_changed = Signal(bool)
    failed = Signal(str)
    status = Signal(str)
    closed = Signal()
    request = Signal(str, object)

    def __init__(self, data_root=None):
        super().__init__()
        self._busy = False
        self._closing = False
        self.thread_handle = QThread(self)
        root = Path(data_root) if data_root is not None else template_data_root()
        self.worker = StudioWorker(root)
        self.worker.moveToThread(self.thread_handle)
        self.request.connect(self.worker.run)
        self.worker.updated.connect(self.state_changed)
        self.worker.saved.connect(self.saved)
        self.worker.native_opened.connect(self.native_opened)
        self.worker.failed.connect(self.failed)
        self.worker.status.connect(self.status)
        self.worker.finished.connect(self._done)
        self.worker.stopped.connect(self.thread_handle.quit)
        self._finish_timer = QTimer(self)
        self._finish_timer.setInterval(10)
        self._finish_timer.timeout.connect(self._thread_finished)
        self.thread_handle.finished.connect(self._thread_finished)
        self.thread_handle.start()

    @Slot()
    def _thread_finished(self):
        # finished() precedes thread-local COM/Python cleanup. The owning
        # dialog can be destroyed as soon as closed() reaches its parent.
        if self.thread_handle.wait(0):
            self._finish_timer.stop()
            self.worker.setParent(self)
            self.closed.emit()
        else:
            self._finish_timer.start()

    def _send(self, op, value=None):
        if self._busy or self._closing:
            return
        self._busy = True
        self.busy_changed.emit(True)
        self.request.emit(op, value)

    @Slot()
    def _done(self):
        if not self._closing:
            self._busy = False
            self.busy_changed.emit(False)

    def open(self, path): self._send('open', path)
    def show_region(self, key): self._send('show', key)
    def capture_region(self, key): self._send('capture', key)
    def clear_region(self, key): self._send('clear', key)
    def save_profile(self): self._send('save')
    def apply_proposal(self, regions): self._send('proposal', regions)

    def close_session(self):
        if not self._closing:
            self._closing = True
            self.request.emit('close', None)


def arrange(window, hwnd):
    """Place only the registrar and its own fresh Hangul window side by side."""
    if os.name != 'nt':
        return
    import win32api
    import win32con
    import win32gui
    if not win32gui.IsWindow(hwnd):
        return
    logical = window.screen().availableGeometry()
    if logical.width() < window.minimumWidth() + 400:
        return
    monitor = win32api.MonitorFromWindow(int(window.winId()), win32con.MONITOR_DEFAULTTONEAREST)
    left, top, right, bottom = win32api.GetMonitorInfo(monitor)['Work']
    width = min(615, max(window.minimumWidth(), round(logical.width() * .39)))
    split = left + round(width * (right-left) / logical.width())
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
    win32gui.SetWindowPos(hwnd, 0, split, top, right-split, bottom-top, flags)
    win32gui.SetWindowPos(int(window.winId()), 0, left, top, split-left, bottom-top, flags)
    window.raise_()
