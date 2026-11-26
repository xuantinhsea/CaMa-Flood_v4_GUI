"""Project tab: project metadata and the case table.

The column that matters is **Status**. A case that completed but whose configuration has since
changed shows as STALE -- results that no longer match the inputs that produced them. One small
feature, and it prevents an entire class of published-the-wrong-figure errors.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import batch, schema
from ...core.project import Case, CaseStatus, Project
from .. import theme

STATUS_COLOR = {
    CaseStatus.COMPLETED: theme.GOOD,
    CaseStatus.STALE: theme.WARN,
    CaseStatus.FAILED: theme.BAD,
    CaseStatus.RUNNING: theme.ACCENT,
    CaseStatus.QUEUED: theme.INFO,
    CaseStatus.NOT_RUN: theme.MUTED,
}

STATUS_HINT = {
    CaseStatus.STALE: "Results are older than the configuration that produced them. Re-run.",
    CaseStatus.FAILED: "The run did not complete. Check the log in the Run tab.",
    CaseStatus.NOT_RUN: "No results yet.",
}


class NewSweepDialog(QDialog):
    """Build N cases that vary exactly one namelist field, via ``core.batch.build_sweep``.

    Everything the sweep needs -- typed parsing, the slug used in case names, copying the
    base case's namelist byte for byte otherwise -- already lives in ``core.batch``. This
    dialog is only the field picker and the values textbox in front of it.
    """

    def __init__(self, base_case: Case, parent: QWidget | None = None):
        super().__init__(parent)
        self.base_case = base_case
        self.new_cases: list[Case] = []
        self.setWindowTitle(f"New sweep from '{base_case.name}'")

        lay = QVBoxLayout(self)
        form = QFormLayout()

        self.cmb_group = QComboBox()
        self.cmb_group.addItems([g.name for g in schema.ALL_GROUPS])
        form.addRow("Namelist group", self.cmb_group)

        self.cmb_field = QComboBox()
        form.addRow("Field", self.cmb_field)

        self.lbl_doc = QLabel("")
        self.lbl_doc.setWordWrap(True)
        self.lbl_doc.setStyleSheet(f"color:{theme.MUTED};")
        self.lbl_doc.setMaximumWidth(360)
        form.addRow("", self.lbl_doc)

        self.txt_values = QLineEdit()
        self.txt_values.setPlaceholderText("comma-separated, e.g. 0.03, 0.05, 0.10")
        form.addRow("Values", self.txt_values)

        self.txt_prefix = QLineEdit()
        form.addRow("Case name prefix", self.txt_prefix)

        self.chk_overwrite = QCheckBox("Overwrite existing cases with the same name")
        form.addRow("", self.chk_overwrite)

        lay.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.cmb_group.currentTextChanged.connect(self._group_changed)
        self.cmb_field.currentTextChanged.connect(self._field_changed)
        self._group_changed(self.cmb_group.currentText())

    def _group_changed(self, group_name: str) -> None:
        self.cmb_field.blockSignals(True)
        self.cmb_field.clear()
        group = schema.GROUPS_BY_NAME.get(group_name)
        if group is not None:
            self.cmb_field.addItems([f.name for f in group.fields])
        self.cmb_field.blockSignals(False)
        self._field_changed(self.cmb_field.currentText())

    def _field_changed(self, field_name: str) -> None:
        field = schema.field_for(self.cmb_group.currentText(), field_name) if field_name else None
        if field is None:
            self.lbl_doc.setText("")
            self.txt_prefix.setPlaceholderText("(defaults to the field name)")
            return
        text = field.doc
        if field.guidance:
            text += "  " + field.guidance
        self.lbl_doc.setText(text)
        self.txt_prefix.setPlaceholderText(f'(defaults to "{field_name.lower()}")')

    def _accept(self) -> None:
        field_name = self.cmb_field.currentText()
        field = schema.field_for(self.cmb_group.currentText(), field_name)
        if field is None:
            QMessageBox.warning(self, "New sweep", "Pick a namelist field.")
            return
        try:
            values = batch.parse_sweep_values(field, self.txt_values.text())
        except ValueError as exc:
            QMessageBox.warning(self, "New sweep", f"Could not parse values: {exc}")
            return
        try:
            spec = batch.SweepSpec(
                group=self.cmb_group.currentText(),
                key=field_name,
                values=tuple(values),
                name_prefix=self.txt_prefix.text().strip(),
            )
            self.new_cases = batch.build_sweep(
                self.base_case.project, self.base_case, spec,
                overwrite=self.chk_overwrite.isChecked())
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            QMessageBox.warning(self, "New sweep", str(exc))
            return
        self.accept()


class ProjectView(QWidget):
    """The Project tab."""

    caseSelected = pyqtSignal(object)        # Case | None
    runRequested = pyqtSignal(object)        # Case

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.project: Project | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        meta = QGroupBox("Project")
        form = QFormLayout(meta)
        self.lbl_name = QLabel("-")
        self.lbl_root = QLabel("-")
        self.lbl_map = QLabel("-")
        self.lbl_binary = QLabel("-")
        for w in (self.lbl_root, self.lbl_map, self.lbl_binary):
            w.setStyleSheet(f"color:{theme.MUTED};")
            w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Name", self.lbl_name)
        form.addRow("Location", self.lbl_root)
        form.addRow("Domain", self.lbl_map)
        form.addRow("Model binary", self.lbl_binary)
        lay.addWidget(meta)

        cases = QGroupBox("Cases")
        cl = QVBoxLayout(cases)

        bar = QHBoxLayout()
        self.btn_new = QPushButton("New case")
        self.btn_dup = QPushButton("Duplicate")
        self.btn_del = QPushButton("Delete")
        self.btn_sweep = QPushButton("New sweep...")
        self.btn_run = QPushButton("Run selected")
        self.btn_run.setStyleSheet("font-weight:600;")
        for b in (self.btn_new, self.btn_dup, self.btn_del, self.btn_sweep):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.btn_run)
        cl.addLayout(bar)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Case", "Status", "Period", "Runtime", "Outputs", "Last run"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in range(1, 6):
            h.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        cl.addWidget(self.table, 1)
        lay.addWidget(cases, 1)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color:{theme.WARN};")
        lay.addWidget(self.hint)

        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.btn_new.clicked.connect(self._new_case)
        self.btn_dup.clicked.connect(self._duplicate_case)
        self.btn_del.clicked.connect(self._delete_case)
        self.btn_sweep.clicked.connect(self._new_sweep)
        self.btn_run.clicked.connect(self._run_selected)
        self._set_buttons(False)

    # ---------------------------------------------------------------- data

    def set_project(self, project: Project | None) -> None:
        self.project = project
        if project is None:
            self.lbl_name.setText("-")
            self.lbl_root.setText("-")
            self.lbl_map.setText("-")
            self.lbl_binary.setText("-")
            self.table.setRowCount(0)
            self._set_buttons(False)
            return

        self.lbl_name.setText(project.config.name)
        self.lbl_root.setText(str(project.root))
        self.lbl_map.setText(str(project.map_dir))
        self.lbl_binary.setText(project.config.model_binary or "(not set)")
        self.btn_new.setEnabled(True)
        self.refresh()

    def refresh(self) -> None:
        if self.project is None:
            return
        selected = self.selected_case
        cases = self.project.cases()
        self.table.setRowCount(len(cases))

        for r, case in enumerate(cases):
            rec = case.record()
            st = case.status()
            period = "-"
            try:
                if case.exists():
                    nml = case.namelist()
                    period = (f"{nml.get('NSIMTIME','SYEAR','?')}-"
                              f"{int(nml.get('NSIMTIME','SMON',1)):02d}-"
                              f"{int(nml.get('NSIMTIME','SDAY',1)):02d}"
                              f" → {nml.get('NSIMTIME','EYEAR','?')}-"
                              f"{int(nml.get('NSIMTIME','EMON',1)):02d}-"
                              f"{int(nml.get('NSIMTIME','EDAY',1)):02d}")
            except Exception:
                period = "(unreadable)"

            values = [
                case.name,
                st.value.replace("_", " "),
                period,
                f"{rec.elapsed_s:.0f} s" if rec.elapsed_s else "-",
                f"{case.output_bytes() / 1e6:.1f} MB" if case.output_bytes() else "-",
                (rec.finished or "-")[:16].replace("T", " "),
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(str(v))
                if c == 1:
                    item.setForeground(QBrush(QColor(STATUS_COLOR.get(st, theme.TEXT))))
                    if st in STATUS_HINT:
                        item.setToolTip(STATUS_HINT[st])
                item.setData(Qt.ItemDataRole.UserRole, case.name)
                self.table.setItem(r, c, item)

        if selected:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).text() == selected.name:
                    self.table.selectRow(r)
                    break
        self._selection_changed()

    # ---------------------------------------------------------------- selection

    @property
    def selected_case(self) -> Case | None:
        if self.project is None:
            return None
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        name = self.table.item(rows[0].row(), 0).text()
        return self.project.case(name)

    def _selection_changed(self) -> None:
        case = self.selected_case
        self._set_buttons(case is not None)
        self.caseSelected.emit(case)
        if case is not None and case.status() is CaseStatus.STALE:
            self.hint.setText(
                "This case is STALE: its configuration changed after the results were produced. "
                "Re-run it before using the outputs.")
        else:
            self.hint.setText("")

    def _set_buttons(self, on: bool) -> None:
        for b in (self.btn_dup, self.btn_del, self.btn_sweep, self.btn_run):
            b.setEnabled(on)
        self.btn_new.setEnabled(self.project is not None)

    # ---------------------------------------------------------------- actions

    def _new_case(self) -> None:
        if self.project is None:
            return
        existing = self.project.cases()
        name, ok = QInputDialog.getText(self, "New case", "Case name:")
        if not ok or not name.strip():
            return
        try:
            if existing:
                self.project.add_case(name.strip(), copy_from=existing[0].name)
            else:
                QMessageBox.information(
                    self, "New case",
                    "This project has no case to copy from yet. Create the first case from an "
                    "existing CaMa-Flood namelist via File → Import namelist.")
                return
        except (FileExistsError, FileNotFoundError) as exc:
            QMessageBox.warning(self, "New case", str(exc))
            return
        self.refresh()

    def _duplicate_case(self) -> None:
        case = self.selected_case
        if case is None or self.project is None:
            return
        name, ok = QInputDialog.getText(self, "Duplicate case", "New case name:",
                                        text=f"{case.name}_copy")
        if not ok or not name.strip():
            return
        try:
            self.project.add_case(name.strip(), copy_from=case.name)
        except (FileExistsError, FileNotFoundError) as exc:
            QMessageBox.warning(self, "Duplicate case", str(exc))
            return
        self.refresh()

    def _delete_case(self) -> None:
        case = self.selected_case
        if case is None or self.project is None:
            return
        n = case.output_bytes()
        msg = f"Delete case '{case.name}'?"
        if n:
            msg += f"\n\nThis also deletes {n / 1e6:.1f} MB of results."
        if QMessageBox.question(self, "Delete case", msg) != QMessageBox.StandardButton.Yes:
            return
        self.project.remove_case(case.name)
        self.refresh()

    def _new_sweep(self) -> None:
        case = self.selected_case
        if case is None or self.project is None:
            return
        if not case.exists():
            QMessageBox.warning(self, "New sweep",
                                f"'{case.name}' has no namelist yet -- nothing to sweep from.")
            return
        dlg = NewSweepDialog(case, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.new_cases:
            QMessageBox.information(
                self, "New sweep",
                f"Created {len(dlg.new_cases)} case(s): "
                + ", ".join(c.name for c in dlg.new_cases))
        self.refresh()

    def _run_selected(self) -> None:
        case = self.selected_case
        if case is not None:
            self.runRequested.emit(case)
