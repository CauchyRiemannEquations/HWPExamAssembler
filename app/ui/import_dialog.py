from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import QEventLoop, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QCheckBox,
    QHeaderView,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
)

from app.services.live_import import LiveImportSession
from app.services.template_catalog import recent_templates, template_data_root
from app.ui.template_registration import StudioController, arrange
from app.ui.template_window import StudioWindow
from app.services import StructureAnalysis
from app.ui.structure_preview import DIALOG_STYLE, StructurePreviewDialog


class ImportDialog(QDialog):
    PHASE_CONFIGURE = "configure"
    PHASE_IMPORT = "import"

    def __init__(self, project_root: Path, parent=None, *, data_root=None) -> None:
        super().__init__(parent)
        self.session = LiveImportSession(project_root)
        self.data_root = Path(data_root) if data_root is not None else template_data_root()
        self.registrar = None
        self.format_summary = ""
        self.extra_format_instructions = ""
        self.busy = False
        self._file_progress = ""
        self.session_active = False
        self.phase = self.PHASE_CONFIGURE
        self.completed_output: Path | None = None
        self.added_source_count = 0
        self.setWindowTitle("새 시험지 수합")
        self.setAcceptDrops(True)
        self.resize(760, 390)
        self._build_ui()
        self._load_templates()
        self._set_phase(self.PHASE_CONFIGURE)

    def _build_ui(self) -> None:
        self.setStyleSheet(DIALOG_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)
        self.step_label = QLabel()
        self.step_label.setObjectName("stageStep")
        self.title = QLabel()
        self.title.setObjectName("stageTitle")
        layout.addWidget(self.step_label)
        layout.addWidget(self.title)

        self.setup_page = QWidget()
        self.setup_page.setObjectName("stage")
        setup = QVBoxLayout(self.setup_page)
        setup.setContentsMargins(0, 0, 0, 0)
        setup.setSpacing(14)
        info = QLabel("학교 양식에 출제 파일의 문항을 모아 새 편집본을 만듭니다. 처음 쓰는 양식은 한글에서 새 문항이 들어갈 자리를 확인하세요.")
        info.setWordWrap(True)
        info.setObjectName("notice")
        setup.addWidget(info)
        self.template_combo = QComboBox()
        self.template_combo.setAccessibleName("등록한 학교 양식")
        self.template_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.template_combo.setMinimumContentsLength(12)
        self.template_combo.setPlaceholderText("등록한 양식이 없습니다")
        self.template_button = QPushButton("내 학교 양식 등록…")
        self.review_button = QPushButton("영역 확인…")
        selection = QHBoxLayout()
        selection.addWidget(self.template_combo, 1)
        selection.addWidget(self.review_button)
        selection.addWidget(self.template_button)
        self.output_edit = QLineEdit(str(self._default_output()))
        self.output_button = QPushButton("찾아보기…")
        destination = QHBoxLayout()
        destination.addWidget(self.output_edit, 1)
        destination.addWidget(self.output_button)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(14)
        form.addRow("학교 양식", selection)
        form.addRow("새 편집본", destination)
        self.show_hwp = QCheckBox("수합 중 한글 작업 창 표시")
        self.show_hwp.setToolTip("작업 화면을 보거나 한글의 파일 접근 요청을 직접 확인할 때 선택하세요.")
        form.addRow("", self.show_hwp)
        setup.addLayout(form)
        setup.addStretch()
        layout.addWidget(self.setup_page, 1)

        self.sources_page = QWidget()
        self.sources_page.setObjectName("stage")
        sources = QVBoxLayout(self.sources_page)
        sources.setContentsMargins(0, 0, 0, 0)
        sources.setSpacing(10)
        self.destination_label = QLabel()
        self.destination_label.setTextFormat(Qt.TextFormat.PlainText)
        self.destination_label.setObjectName("notice")
        self.destination_label.setWordWrap(True)
        sources.addWidget(self.destination_label)
        top = QHBoxLayout()
        self.author_edit = QLineEdit()
        self.author_edit.setPlaceholderText("선택 입력")
        self.author_edit.setMaximumWidth(200)
        top.addWidget(QLabel("출제자"))
        top.addWidget(self.author_edit)
        top.addStretch()
        self.format_button = QPushButton("서식 기준 보기…")
        self.format_button.clicked.connect(self.review_template_format)
        top.addWidget(self.format_button)
        self.source_button = QPushButton("출제 파일 추가…")
        self.source_button.setToolTip("HWP 또는 HWPX 파일을 여러 개 선택할 수 있습니다.")
        top.addWidget(self.source_button)
        sources.addLayout(top)
        self.source_label = QLabel()
        self.source_label.setObjectName("sectionTitle")
        sources.addWidget(self.source_label)
        self.empty_label = QLabel("‘출제 파일 추가’에서 수합할 HWP 또는 HWPX 파일을 선택하세요.")
        self.empty_label.setObjectName("muted")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sources.addWidget(self.empty_label, 1)
        self.registered_list = QTreeWidget()
        self.registered_list.setHeaderLabels(["출제 파일 · 문항", "수합 결과"])
        self.registered_list.setAccessibleName("파일별 문항 수합 결과")
        self.registered_list.header().setStretchLastSection(False)
        self.registered_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.registered_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.registered_list.setAlternatingRowColors(True)
        sources.addWidget(self.registered_list, 1)
        layout.addWidget(self.sources_page, 1)

        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("muted")
        layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.log = QPlainTextEdit()
        self.log.setPlaceholderText("작업 세부 기록")
        self.log.setMaximumHeight(95)
        self.log.setReadOnly(True)
        self.log.hide()
        layout.addWidget(self.log)
        bottom = QHBoxLayout()
        self.log_button = QPushButton("작업 기록 펼치기")
        self.log_button.setCheckable(True)
        bottom.addWidget(self.log_button)
        bottom.addStretch()
        self.cancel_button = QPushButton("닫기")
        self.start_button = QPushButton("출제 파일 단계로")
        self.finish_button = QPushButton("저장하고 편집기로")
        for button in (self.start_button, self.finish_button):
            button.setObjectName("stagePrimary")
        bottom.addWidget(self.cancel_button)
        bottom.addWidget(self.start_button)
        bottom.addWidget(self.finish_button)
        layout.addLayout(bottom)
        self.log_button.toggled.connect(self._toggle_log)
        self.cancel_button.clicked.connect(self.reject)
        self.template_button.clicked.connect(self.choose_template)
        self.output_button.clicked.connect(self.choose_output)
        self.start_button.clicked.connect(self.start_session)
        self.source_button.clicked.connect(self.choose_source)
        self.finish_button.clicked.connect(self.finish_session)
        self.review_button.clicked.connect(self.review_registered)
        self.template_combo.currentIndexChanged.connect(self._template_selected)
        self.output_edit.textChanged.connect(self._refresh_start_button)

    @staticmethod
    def _default_output(extension=".hwp") -> Path:
        from PySide6.QtCore import QStandardPaths
        folder = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation))
        base = "시험지_수합편집본"
        path = folder / f"{base}{extension}"
        suffix = 2
        while path.exists():
            path = folder / f"{base}_{suffix}{extension}"
            suffix += 1
        return path

    def _load_templates(self, selected=None):
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        current_index = 0
        for entry in recent_templates(self.data_root):
            self.template_combo.addItem(entry.source.name, entry)
            index = self.template_combo.count() - 1
            self.template_combo.setItemData(index, str(entry.source), Qt.ItemDataRole.ToolTipRole)
            if selected is not None and selected.profile == entry.profile:
                current_index = index
        if self.template_combo.count():
            self.template_combo.setCurrentIndex(current_index)
        self.template_combo.blockSignals(False)
        self._template_selected()

    def _template_selected(self):
        entry = self.template_combo.currentData()
        self.template_combo.setToolTip(str(entry.source) if entry else "")
        self.template_combo.setVisible(entry is not None)
        self.review_button.setVisible(entry is not None)
        if entry:
            extension = entry.source.suffix.lower()
            current = self.output_edit.text().strip()
            if not current or Path(current).name.startswith("시험지_수합편집본"):
                self.output_edit.setText(str(self._default_output(extension)))
            elif Path(current).suffix.lower() != extension:
                self.output_edit.setText(str(Path(current).with_suffix(extension)))
        self._refresh_start_button()

    def _toggle_log(self, expanded: bool) -> None:
        self.log.setVisible(expanded)
        self.log_button.setText("작업 기록 접기" if expanded else "작업 기록 펼치기")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.progress_bar.setVisible(busy)
        self.cancel_button.setEnabled(not busy)
        self.setup_page.setEnabled(not busy)
        self.format_button.setEnabled(not busy and getattr(self.session, "format_report", None) is not None)
        self.source_button.setEnabled(not busy and self.phase == self.PHASE_IMPORT)
        self.author_edit.setEnabled(not busy and self.phase == self.PHASE_IMPORT)
        self.finish_button.setEnabled(not busy and self.phase == self.PHASE_IMPORT and bool(self.session.registered))
        self._refresh_start_button()

    def _set_phase(self, phase: str) -> None:
        self.phase = phase
        self.session_active = phase != self.PHASE_CONFIGURE
        configuring = phase == self.PHASE_CONFIGURE
        importing = phase == self.PHASE_IMPORT
        self.setup_page.setVisible(configuring)
        self.sources_page.setVisible(importing)
        self.start_button.setVisible(configuring)
        self.finish_button.setVisible(importing and bool(self.session.registered))
        self.step_label.setText("시험지 수합   ·   2 / 2" if importing else "시험지 수합   ·   1 / 2")
        self.title.setText("출제 파일 수합" if importing else "양식과 저장 위치")
        if importing:
            destination = self.session.output_path or Path(self.output_edit.text())
            self.destination_label.setText(f"편집본   {Path(destination).name}")
            self.destination_label.setToolTip(str(destination))
            self._refresh_totals()
        self._set_busy(self.busy)
        if importing and self.height() < 520:
            self.resize(self.width(), 520)



    def _refresh_start_button(self) -> None:
        can_start = bool(
            not self.busy
            and self.phase == self.PHASE_CONFIGURE
            and self.template_combo.currentData() is not None
            and self.output_edit.text().strip()
        )
        self.start_button.setEnabled(can_start)

    def _progress(self, message: str) -> None:
        hwp = self.session.hwp
        if hwp is not None and hwp.security_module_registered is False and not hwp.visible:
            # The native approval dialog needs a visible owner before the first Open.
            hwp.visible = True
        text = f"{self._file_progress}  ·  {message}" if self._file_progress else message
        self.status_label.setText(text)
        self.log.appendPlainText(text)
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def choose_template(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "학교 시험지 양식 등록", "", "한글 문서 (*.hwp *.hwpx)")
        if path:
            self.open_registrar(Path(path))

    def review_registered(self):
        entry = self.template_combo.currentData()
        if entry:
            self.open_registrar(entry.source)

    def open_registrar(self, path):
        if self.registrar is not None or self.phase != self.PHASE_CONFIGURE or self.busy:
            return
        controller = StudioController(self.data_root)
        self.registrar = StudioWindow(controller, self)
        self.registrar.setWindowModality(Qt.WindowModality.WindowModal)
        controller.native_opened.connect(self._arrange_registrar)
        self.registrar.finished.connect(self._registration_finished)
        self.registrar.open()
        controller.open(path)

    def _arrange_registrar(self, hwnd):
        if self.registrar is not None:
            arrange(self.registrar, hwnd)

    def _registration_finished(self, result):
        registrar = self.registrar
        self.registrar = None
        if registrar.registration is not None:
            self._load_templates(registrar.registration)
            self._progress(f"{registrar.registration.source.name} · 등록한 양식을 사용합니다.")
        registrar.deleteLater()

    def dragEnterEvent(self, event):
        if (not self.busy and self.phase == self.PHASE_CONFIGURE and self.registrar is None
                and StudioWindow._dropped_path(event) is not None):
            event.acceptProposedAction()

    def dropEvent(self, event):
        path = StudioWindow._dropped_path(event)
        if path and not self.busy and self.phase == self.PHASE_CONFIGURE and self.registrar is None:
            event.acceptProposedAction()
            self.open_registrar(path)

    def choose_output(self) -> None:
        entry = self.template_combo.currentData()
        extension = entry.source.suffix.lower() if entry else ".hwp"
        path, _ = QFileDialog.getSaveFileName(
            self, "새 편집본 저장 위치", self.output_edit.text(), f"학교 양식 (*{extension})",
            options=QFileDialog.Option.DontConfirmOverwrite,
        )
        if path:
            self.output_edit.setText(path)

    def start_session(self) -> None:
        if self.busy or self.phase != self.PHASE_CONFIGURE:
            return
        entry = self.template_combo.currentData()
        if entry is None:
            return
        template_path = entry.source
        if not template_path.is_file():
            QMessageBox.warning(self, "학교 양식 위치 확인", "등록한 양식 파일을 찾을 수 없습니다. ‘내 학교 양식 등록’에서 현재 위치의 파일을 선택하세요.")
            return
        output_text = self.output_edit.text().strip()
        if not output_text:
            QMessageBox.information(
                self, "수합본 경로 필요", "새 수합본을 저장할 경로를 선택하세요."
            )
            return
        try:
            output_path = Path(output_text).resolve()
            destination = output_path.with_suffix(template_path.suffix)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "저장 위치 확인", str(error))
            return
        if template_path.resolve() == destination:
            QMessageBox.warning(
                self,
                "원본 양식 보호",
                "양식 원본과 새 수합본은 같은 파일일 수 없습니다. 다른 저장 경로를 선택하세요.",
            )
            return
        if destination.exists():
            QMessageBox.warning(self, "새 파일 이름 필요", f"이미 있는 파일입니다. 다른 이름을 선택하세요.\n{destination}")
            return
        self.output_edit.setText(str(destination))
        self._set_busy(True)
        try:
            self._progress("등록한 양식의 작업 사본을 여는 중…")
            self.session.start(
                template_path, destination, registration=entry.profile,
                visible=self.show_hwp.isChecked(), on_progress=self._progress,
            )
            self._set_phase(self.PHASE_IMPORT)
            self._progress("양식 준비 완료 · 출제 파일을 추가하세요.")
        except Exception as error:
            QMessageBox.critical(self, "양식 작업 사본 열기 실패", str(error))
            self._set_phase(self.PHASE_CONFIGURE)
            self._progress("준비를 마치지 못했습니다. 저장 위치와 한글 창 표시 설정을 확인하세요.")
        finally:
            self._set_busy(False)

    def review_template_format(self):
        if self.busy or self.phase != self.PHASE_IMPORT or self.session.format_report is None:
            return
        from app.ui.format_review import FormatReviewDialog
        dialog = FormatReviewDialog(deepcopy(self.session.format_report), self.extra_format_instructions, self,
                                    template_path=self.session.template_path)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                path = self.session.output_path
                dialog.report.save(path.with_suffix(path.suffix + ".format-review.json"))
            except OSError as error:
                QMessageBox.warning(self, "서식 기준 기록 실패", str(error))
                return
            self.session.format_report = dialog.report
            self.extra_format_instructions = dialog.instructions.toPlainText()
            self._progress("수합본에 적용할 서식 비교 기준을 저장했습니다.")

    @staticmethod
    def _counts(entries):
        copied = manual = 0
        for entry in entries:
            count = max(1, len(entry.child_ids), entry.objective_count + entry.written_count)
            if entry.manual_required:
                manual += count
            else:
                copied += count
        return copied, manual

    def _refresh_totals(self):
        has_items = bool(self.session.registered)
        has_files = self.registered_list.topLevelItemCount() > 0
        self.empty_label.setVisible(not has_files)
        self.registered_list.setVisible(has_files)
        self.source_label.setVisible(has_files)
        self.finish_button.setVisible(self.phase == self.PHASE_IMPORT and has_items)
        name = "" if has_items else "stagePrimary"
        if self.source_button.objectName() != name:
            self.source_button.setObjectName(name)
            self.source_button.style().unpolish(self.source_button)
            self.source_button.style().polish(self.source_button)
        copied, manual = self._counts(self.session.registered)
        self.source_label.setText(
            f"출제 파일 {self.added_source_count}개    ·    수합 {copied}문항    ·    직접 입력 {manual}문항"
        )

    def _show_file_result(self, header, entries, state=""):
        copied, manual = self._counts(entries)
        if entries:
            self.added_source_count += 1
        parts = [f"수합 {copied}문항"]
        if manual:
            parts.append(f"직접 입력 {manual}문항")
        if state:
            parts.append(state)
        header.setText(1, " · ".join(parts))
        header.setForeground(1, QColor("#894c18" if manual or state else "#105c5a"))
        for entry in entries:
            prompt = entry.preview or entry.source_label or "원본 문항 범위"
            if entry.kind == "group":
                prompt = f"[공통 묶음] {prompt}"
            label = "직접 입력 필요" if entry.manual_required else "수합됨"
            row = QTreeWidgetItem(header, [prompt, label])
            ids = " · ".join(entry.child_ids) or entry.id
            details = [prompt, f"{entry.author or Path(entry.source).stem}  ·  {ids}"]
            if entry.import_error:
                details.append(entry.import_error)
            row.setToolTip(0, "\n".join(details))
            row.setToolTip(1, "\n".join(details))
            if entry.manual_required:
                row.setForeground(1, QColor("#894c18"))
        header.setExpanded(bool(manual))
        self._refresh_totals()

    def choose_source(self) -> None:
        if self.busy or self.phase != self.PHASE_IMPORT:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "출제 파일 추가", "", "한글 문서 (*.hwp *.hwpx)"
        )
        if not paths:
            return
        self._set_busy(True)
        self.progress_bar.setRange(0, len(paths))
        self.progress_bar.setValue(0)
        try:
            for index, path in enumerate(paths, 1):
                name = Path(path).name
                self._file_progress = f"{index}/{len(paths)}  {name}"
                header = QTreeWidgetItem(self.registered_list, [name, "수합 중"])
                header.setToolTip(0, str(path))
                self._refresh_totals()
                self.registered_list.scrollToItem(header)
                self._progress("문항을 확인하는 중…")
                before = len(self.session.registered)
                try:
                    result = self.session.import_source_file(
                        path, author=self.author_edit.text().strip(),
                        on_structure_review=lambda analysis, source=name: self._review_structure(analysis, source),
                    )
                except Exception as error:
                    self._show_file_result(header, self.session.registered[before:], "파일 처리 중단")
                    header.setToolTip(1, str(error))
                    left = len(paths) - index
                    note = f" · 남은 {left}개 파일 미처리" if left else ""
                    self._progress(f"파일 처리 중단{note}")
                    QMessageBox.critical(self, f"{name} 수합 중단", str(error))
                    return
                self._show_file_result(header, result, "" if result else "건너뜀")
                self.progress_bar.setValue(index)
            self._file_progress = ""
            manual_count = self._counts(self.session.registered)[1]
            if manual_count:
                self._progress(f"직접 입력할 {manual_count}문항이 있습니다. 파일을 펼쳐 해당 문항을 확인하세요.")
            elif self.session.registered:
                self._progress("파일을 더 추가하거나 편집본을 저장하세요.")
            else:
                self._progress("출제 파일을 추가해 주세요.")
        finally:
            self._file_progress = ""
            self._set_busy(False)
            self.progress_bar.setRange(0, 0)



    def _review_structure(
        self, analysis: StructureAnalysis, source_name: str
    ) -> bool:
        dialog = StructurePreviewDialog(analysis, source_name, self)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def finish_session(self) -> None:
        if self.busy or not self.session_active or not self.session.registered:
            return
        self._set_busy(True)
        try:
            self._progress("수합본을 저장하고 문항과 학교 양식을 검증하는 중…")
            output = self.session.finish(open_after=False)
            self.completed_output = output
            check = getattr(self.session, "format_check", None)
            if check:
                issues = sum(row["status"] != "일치" for row in check.get("checks", ()))
                self.format_summary = f"서식 대조 {check['status']} · 확인할 항목 {issues}개"
            self.session_active = False
            manual = [entry for entry in self.session.registered if entry.manual_required]
            if manual:
                details = "\n".join(
                    f"• {Path(entry.source).name} · {entry.id}   {entry.preview or entry.source_label}"
                    for entry in manual
                )
                notice = QMessageBox(self)
                notice.setIcon(QMessageBox.Icon.Information)
                notice.setWindowTitle("직접 입력할 문항")
                notice.setText(f"편집본을 저장했습니다. {self._counts(manual)[1]}문항은 번호만 남겨 두었습니다.")
                notice.setInformativeText("편집기에서 해당 문항을 확인하고 한글에서 원본 내용을 붙여 넣으세요.")
                notice.setDetailedText(details)
                notice.addButton("편집기로", QMessageBox.ButtonRole.AcceptRole)
                notice.exec()
            self.accept()
        except Exception as error:
            if self.session.hwp is None:
                self.session_active = False
                self.source_button.hide()
                self.finish_button.hide()
            self._progress("저장 검증을 마치지 못했습니다. 작업 기록을 확인하세요.")
            QMessageBox.critical(self, "수합본 저장 실패", str(error))
        finally:
            self._set_busy(False)

    def reject(self) -> None:
        if self.registrar is not None:
            self.registrar.close()
            return
        if self.busy:
            return
        if self.session_active:
            answer = QMessageBox.question(
                self, "수합 종료",
                "최종 저장과 검증을 마치기 전에 수합을 종료할까요? 편집본에는 마지막으로 저장된 내용만 남습니다.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.session.close()
            self.session_active = False
            self.phase = self.PHASE_CONFIGURE
        super().reject()
