"""Keep all live Hangul COM references in one Qt worker thread."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot


def find_open_document(path):
    from app.services.live_document import is_document_open

    return is_document_open(path)


def create_companion(service):
    from app.services.live_document import LiveDocumentSession

    return LiveDocumentSession(service)


class CompanionWorker(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)
    warning = Signal(str)
    stopped = Signal()

    def __init__(self, service, factory):
        super().__init__()
        self.service = service
        self.factory = factory or create_companion
        self.session = None

    def _release(self):
        if self.session is not None:
            try:
                self.session.release()
            finally:
                self.session = None

    @Slot(str, object)
    def perform(self, operation: str, args: tuple) -> None:
        try:
            if operation == "stop":
                try:
                    self._release()
                finally:
                    self.stopped.emit()
                return
            if operation == "find":
                self.completed.emit(operation, find_open_document(*args))
                return
            if operation in {"open", "attach"}:
                self._release()
                self.session = self.factory(self.service)
            if self.session is None:
                raise RuntimeError("연결한 한글 문서가 닫혔습니다.")
            if operation == "attach":
                result = self.session.open(*args, only_if_open=True)
            else:
                action = {
                    "open": self.session.open,
                    "apply": self.session.apply,
                    "navigate": self.session.navigate,
                    "refresh": self.session.refresh,
                    "probe": self.session.probe,
                }[operation]
                result = action(*args)
            if (operation == "probe" and result == "closed") or (operation == "attach" and result is None):
                self._release()
            self.completed.emit(operation, result)
            if operation == "apply":
                warning = getattr(self.session, "last_warning", "")
                if warning:
                    self.warning.emit(warning)
        except Exception as error:
            if operation in {"open", "attach"}:
                try:
                    self._release()
                except Exception:
                    pass
            self.failed.emit(operation, str(error))
