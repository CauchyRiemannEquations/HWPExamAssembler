from __future__ import annotations

from pathlib import Path
import traceback

from PySide6.QtCore import QObject, Signal, Slot

from app.services import DocumentSnapshot, ExamDocumentService


class DocumentWorker(QObject):
    inspected = Signal(object)
    refreshed = Signal(object)
    applied = Signal(str)
    failed = Signal(str)
    finished = Signal()
    progress = Signal(str)

    def __init__(self, service: ExamDocumentService) -> None:
        super().__init__()
        self.service = service

    @Slot(str, bool)
    def inspect(self, path: str, visible: bool) -> None:
        try:
            self.inspected.emit(
                self.service.inspect(
                    Path(path), visible=visible, on_progress=self.progress.emit
                )
            )
        except Exception as error:
            self.failed.emit(f"{error}\n\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

    @Slot(object, str, object, bool)
    def apply(
        self,
        snapshot: DocumentSnapshot,
        destination: str,
        requested_order: tuple[str, ...],
        visible: bool,
    ) -> None:
        try:
            output = self.service.apply_to_new_file(
                snapshot,
                Path(destination),
                requested_order,
                visible=visible,
                on_progress=self.progress.emit,
                open_after=False,
            )
            self.applied.emit(str(output))
        except Exception as error:
            self.failed.emit(f"{error}\n\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

    @Slot(object, object, bool)
    def apply_in_place(
        self,
        snapshot: DocumentSnapshot,
        requested_order: tuple[str, ...],
        visible: bool,
    ) -> None:
        try:
            output = self.service.apply_in_place(
                snapshot,
                requested_order,
                visible=visible,
                on_progress=self.progress.emit,
                open_after=False,
            )
            self.applied.emit(str(output))
        except Exception as error:
            self.failed.emit(f"{error}\n\n{traceback.format_exc()}")
        finally:
            self.finished.emit()

    @Slot(str, bool, object)
    def refresh(
        self,
        path: str,
        visible: bool,
        expected_ids: tuple[str, ...] | DocumentSnapshot,
    ) -> None:
        try:
            self.refreshed.emit(
                self.service.refresh_after_external_edit(
                    Path(path),
                    expected_ids,
                    visible=visible,
                    on_progress=self.progress.emit,
                )
            )
        except Exception as error:
            self.failed.emit(f"{error}\n\n{traceback.format_exc()}")
        finally:
            self.finished.emit()
