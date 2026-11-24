"""Run a single user-requested AI operation away from the Qt and Hancom threads."""
from queue import Empty, Queue
import threading

from PySide6.QtCore import QObject, QTimer, Signal


class AnalysisTask(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)
    progress = Signal(str)

    def __init__(self, parent):
        super().__init__(parent)
        self.last_status = None
        self._cancel = None
        self._queue = None
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._poll)

    @property
    def running(self):
        return self._cancel is not None

    def start(self, kind, operation):
        if self.running:
            return
        self.last_status = None
        cancelled = self._cancel = threading.Event()
        events = self._queue = Queue()
        def run():
            try:
                value = operation(cancelled, lambda text: events.put(("progress", text)))
                events.put(("done", value))
            except Exception as error:
                # Only provider exceptions with teacher-facing messages reach the UI.
                from app.services.chatgpt_analysis import ChatGPTAnalysisError
                from app.services.chatgpt_auth import AuthError
                from app.services.gemini_analysis import GeminiAnalysisError
                known = isinstance(error, (AuthError, ChatGPTAnalysisError, GeminiAnalysisError))
                message = str(error) if known else "연결을 완료하지 못했습니다. 다시 시도해 주세요."
                events.put(("error", (getattr(error, "status", None), message)))
        self.kind = kind
        self._timer.start()
        threading.Thread(target=run, daemon=True, name="template-analysis").start()

    def cancel(self):
        if self._cancel:
            self._cancel.set()
        self._timer.stop()
        self._cancel = self._queue = None

    def _poll(self):
        events = self._queue
        if events is None:
            return
        while True:
            try:
                kind, value = events.get_nowait()
            except Empty:
                return
            if kind == "progress":
                self.progress.emit(value)
            else:
                operation = self.kind
                self._timer.stop()
                self._cancel = self._queue = None
                if kind == "done":
                    self.completed.emit(operation, value)
                else:
                    self.last_status, message = value
                    self.failed.emit(operation, message)
                return
