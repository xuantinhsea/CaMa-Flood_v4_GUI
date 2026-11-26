"""Model Setup: the namelist editor.

Dual view -- a schema-driven form and the raw text -- kept in sync, because users hand-edit
these files and keep notes in them. Editing goes through the typed structure and rewrites only
the changed value, preserving comments, alignment and ordering, with a timestamped backup.
Never by text substitution (risk R12).

Advanced fields are hidden by default. The namelist has ~90 keys and showing all of them at
once is how a configuration screen becomes unusable.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import schema
from ...core.errors import DiagnosticList
from ...core.namelist import Namelist
from ...core.project import Case
from .. import theme

CHANGED_STYLE = f"color:{theme.WARN}; font-weight:600;"
NORMAL_STYLE = f"color:{theme.TEXT};"


class FieldEditor(QWidget):
    """One namelist key, typed according to the schema."""

    changed = pyqtSignal()

    def __init__(self, field: schema.Field, base_dir: Path | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.field = field
        self.base_dir = base_dir

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        if field.kind == "logical":
            self.widget = QCheckBox()
            self.widget.toggled.connect(self.changed)
        elif field.kind == "int":
            self.widget = QSpinBox()
            self.widget.setRange(int(field.minimum if field.minimum is not None else -2**31),
                                 int(field.maximum if field.maximum is not None else 2**31 - 1))
            self.widget.valueChanged.connect(self.changed)
        elif field.kind == "real":
            self.widget = QDoubleSpinBox()
            self.widget.setDecimals(6)
            self.widget.setRange(field.minimum if field.minimum is not None else -1e15,
                                 field.maximum if field.maximum is not None else 1e15)
            self.widget.setSingleStep(0.01)
            self.widget.valueChanged.connect(self.changed)
        else:
            self.widget = QLineEdit()
            self.widget.textChanged.connect(self.changed)

        lay.addWidget(self.widget, 1)

        if field.kind == "path":
            browse = QPushButton("...")
            browse.setMaximumWidth(30)
            browse.clicked.connect(self._browse)
            lay.addWidget(browse)

        tip = f"<b>{field.name}</b><br>{field.doc}"
        if field.unit:
            tip += f"<br>unit: {field.unit}"
        tip += f"<br>default: <code>{field.default}</code>"
        if field.guidance:
            tip += f"<br><br><i>{field.guidance}</i>"
        self.setToolTip(tip)

    def _browse(self) -> None:
        start = str(self.base_dir or Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, f"Select {self.field.name}", start)
        if path:
            self.widget.setText(path)

    def value(self):
        if isinstance(self.widget, QCheckBox):
            return self.widget.isChecked()
        if isinstance(self.widget, QSpinBox):
            return self.widget.value()
        if isinstance(self.widget, QDoubleSpinBox):
            return self.widget.value()
        return self.widget.text()

    def set_value(self, v) -> None:
        self.widget.blockSignals(True)
        try:
            if isinstance(self.widget, QCheckBox):
                self.widget.setChecked(bool(v))
            elif isinstance(self.widget, QSpinBox):
                self.widget.setValue(int(v) if v is not None else 0)
            elif isinstance(self.widget, QDoubleSpinBox):
                self.widget.setValue(float(v) if v is not None else 0.0)
            else:
                self.widget.setText("" if v is None else str(v))
        except (TypeError, ValueError):
            pass
        finally:
            self.widget.blockSignals(False)

    @property
    def is_default(self) -> bool:
        return self.value() == self.field.default


class SetupView(QWidget):
    """The Model Setup tab."""

    validationChanged = pyqtSignal(object)      # DiagnosticList
    dirtyChanged = pyqtSignal(bool)
    #: Fired whenever a field's value changes for any reason (direct edit, raw-text apply, or
    #: an external caller via set_value) and whenever a case is (re)loaded. The Physics Options
    #: tab edits these same fields through get_value/set_value rather than holding its own
    #: Namelist, so this is what keeps the two views from silently diverging (see risk R7).
    namelistChanged = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.case: Case | None = None
        self.namelist: Namelist | None = None
        self._editors: dict[tuple[str, str], FieldEditor] = {}
        self._labels: dict[tuple[str, str], QLabel] = {}
        self._dirty = False
        self._loading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)

        bar = QHBoxLayout()
        self.title = QLabel("No case selected")
        self.title.setStyleSheet("font-weight:600;")
        self.chk_advanced = QCheckBox("Show advanced")
        self.chk_advanced.toggled.connect(self._apply_visibility)
        self.btn_defaults = QPushButton("Diff vs defaults")
        self.btn_validate = QPushButton("Validate")
        self.btn_save = QPushButton("Save")
        self.btn_save.setEnabled(False)
        bar.addWidget(self.title, 1)
        bar.addWidget(self.chk_advanced)
        bar.addWidget(self.btn_defaults)
        bar.addWidget(self.btn_validate)
        bar.addWidget(self.btn_save)
        outer.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.tabs = QTabWidget()
        split.addWidget(self.tabs)

        raw_box = QWidget()
        raw_lay = QVBoxLayout(raw_box)
        raw_lay.setContentsMargins(0, 0, 0, 0)
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Raw namelist"))
        self.btn_apply_raw = QPushButton("Apply edits")
        self.btn_apply_raw.setToolTip("Re-parse the text and load it back into the form")
        hdr.addStretch(1)
        hdr.addWidget(self.btn_apply_raw)
        raw_lay.addLayout(hdr)
        self.raw = QPlainTextEdit()
        self.raw.setFont(QFont("Consolas", 9))
        self.raw.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        raw_lay.addWidget(self.raw, 1)
        split.addWidget(raw_box)
        split.setSizes([620, 380])
        outer.addWidget(split, 1)

        self._build_form()

        self.btn_save.clicked.connect(self.save)
        self.btn_validate.clicked.connect(self.validate)
        self.btn_defaults.clicked.connect(self._show_diff)
        self.btn_apply_raw.clicked.connect(self._apply_raw)

    # ---------------------------------------------------------------- build

    def _build_form(self) -> None:
        for group in schema.ALL_GROUPS:
            page = QWidget()
            form = QFormLayout(page)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

            note = QLabel(group.doc)
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{theme.MUTED}; font-style:italic;")
            form.addRow(note)

            for field in group.fields:
                ed = FieldEditor(field)
                ed.changed.connect(lambda g=group.name, f=field.name: self._field_changed(g, f))
                lbl = QLabel(field.name)
                lbl.setStyleSheet(NORMAL_STYLE)
                lbl.setToolTip(field.doc)
                form.addRow(lbl, ed)
                self._editors[(group.name, field.name)] = ed
                self._labels[(group.name, field.name)] = lbl

            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            self.tabs.addTab(scroll, group.name)

        self._apply_visibility()

    def _apply_visibility(self) -> None:
        show = self.chk_advanced.isChecked()
        for (g, k), ed in self._editors.items():
            field = schema.field_for(g, k)
            if field and field.advanced:
                ed.setVisible(show)
                self._labels[(g, k)].setVisible(show)

    # ---------------------------------------------------------------- load / save

    def load_case(self, case: Case | None) -> None:
        self.case = case
        if case is None or not case.exists():
            self.namelist = None
            self.title.setText("No case selected")
            self.raw.setPlainText("")
            self._set_dirty(False)
            self.namelistChanged.emit()
            return

        self.namelist = case.namelist()
        self.title.setText(f"Case: {case.name}")
        self._loading = True
        for (g, k), ed in self._editors.items():
            field = schema.field_for(g, k)
            value = self.namelist.get(g, k, field.default if field else None)
            ed.set_value(value)
            present = self.namelist.has(g, k)
            self._labels[(g, k)].setStyleSheet(
                CHANGED_STYLE if (present and not ed.is_default) else NORMAL_STYLE)
        self._loading = False
        self.raw.setPlainText(self.namelist.render())
        self._set_dirty(False)
        self.validate()
        self.namelistChanged.emit()

    def _field_changed(self, group: str, key: str) -> None:
        if self._loading or self.namelist is None:
            return
        ed = self._editors[(group, key)]
        # Only write keys the file already has, or ones the user has actually moved off the
        # default -- so opening a case does not silently add 90 lines to their namelist.
        if self.namelist.has(group, key) or not ed.is_default:
            self.namelist.set(group, key, ed.value())
            self._labels[(group, key)].setStyleSheet(
                CHANGED_STYLE if not ed.is_default else NORMAL_STYLE)
            self.raw.setPlainText(self.namelist.render())
            self._set_dirty(True)
            self.namelistChanged.emit()

    # ---------------------------------------------------------------- external binding
    #
    # The Physics Options tab presents a curated, explained view over some of these same
    # fields. It edits them through here rather than loading its own Namelist, so there is
    # exactly one in-memory copy and one save/dirty cycle -- see the namelistChanged docstring.

    def get_value(self, group: str, key: str, default=None):
        ed = self._editors.get((group.upper(), key.upper()))
        if ed is not None:
            return ed.value()
        if self.namelist is not None:
            return self.namelist.get(group, key, default)
        return default

    def set_value(self, group: str, key: str, value) -> None:
        pair = (group.upper(), key.upper())
        ed = self._editors.get(pair)
        if ed is None or self.namelist is None:
            return
        ed.set_value(value)
        self._field_changed(*pair)

    def _apply_raw(self) -> None:
        try:
            nml = Namelist.parse(self.raw.toPlainText(),
                                 self.case.namelist_path if self.case else None)
        except Exception as exc:
            QMessageBox.warning(self, "Namelist error", f"Could not parse the text:\n\n{exc}")
            return
        self.namelist = nml
        self._loading = True
        for (g, k), ed in self._editors.items():
            field = schema.field_for(g, k)
            ed.set_value(nml.get(g, k, field.default if field else None))
        self._loading = False
        self._set_dirty(True)
        self.validate()

    def save(self) -> bool:
        if self.case is None or self.namelist is None:
            return False
        try:
            self.namelist.save(self.case.namelist_path, backup=True)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return False
        self._set_dirty(False)
        self.validate()
        return True

    def _set_dirty(self, on: bool) -> None:
        self._dirty = on
        self.btn_save.setEnabled(on)
        self.btn_save.setText("Save *" if on else "Save")
        self.dirtyChanged.emit(on)

    @property
    def dirty(self) -> bool:
        return self._dirty

    # ---------------------------------------------------------------- validation

    def validate(self) -> DiagnosticList:
        if self.namelist is None:
            d = DiagnosticList()
        else:
            base = self.case.dir if self.case else None
            d = schema.validate(self.namelist, base_dir=base)
        self.validationChanged.emit(d)
        return d

    def focus_location(self, where: str) -> None:
        """Jump to a field from the validation pane."""
        if "/" not in where:
            return
        group, key = where.split("/", 1)
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).upper() == group.upper():
                self.tabs.setCurrentIndex(i)
                break
        ed = self._editors.get((group.upper(), key.upper()))
        if ed is None:
            for (g, k), e in self._editors.items():
                if g.upper() == group.upper() and k.upper() == key.upper():
                    ed = e
                    break
        if ed is not None:
            if not ed.isVisible():
                self.chk_advanced.setChecked(True)
            ed.setFocus()

    def _show_diff(self) -> None:
        if self.namelist is None:
            return
        rows: list[str] = []
        for (g, k), ed in sorted(self._editors.items()):
            if not self.namelist.has(g, k):
                continue
            field = schema.field_for(g, k)
            v = self.namelist.get(g, k)
            if field and v != field.default:
                rows.append(f"{g}/{k}\n    {field.default!r}  ->  {v!r}")
        text = "\n".join(rows) if rows else "Every set key is at its default value."
        box = QMessageBox(self)
        box.setWindowTitle("Differences from defaults")
        box.setText(f"{len(rows)} key(s) differ from the CaMa-Flood defaults.")
        box.setDetailedText(text)
        box.exec()
