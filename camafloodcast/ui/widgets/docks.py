"""Bottom-dock widgets: the log, and the validation pane.

The validation pane is the one that earns its place. CaMa-Flood's failures are mostly *silent* --
a truncated basin, a forcing gap, a stale input matrix all produce plausible output and exit 0.
Showing those as a clickable, sorted problem list, the way a compiler shows errors, is what a
hand-run setup cannot give you.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ...core.errors import Diagnostic, DiagnosticList, Severity

SEVERITY_COLOR = {
    Severity.ERROR: theme.BAD,
    Severity.WARNING: theme.WARN,
    Severity.INFO: theme.INFO,
}

LOG_RULES: tuple[tuple[str, str], ...] = (
    ("error", theme.BAD),
    ("Error termination", theme.BAD),
    ("STOP", theme.BAD),
    ("WARNING", theme.WARN),
    ("CAUTION", theme.WARN),
    ("ADPSTP", theme.PURPLE),
    ("flx:", theme.GOOD),
    ("CMF::", theme.INFO),
)


class LogDock(QWidget):
    """Streaming log with severity colouring and a live filter.

    Bounded: a global run's log can exceed 600 000 lines, and an unbounded ``QPlainTextEdit``
    would consume the session.
    """

    MAX_BLOCKS = 5000

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filter (substring)...")
        self.filter.setClearButtonEnabled(True)
        self.autoscroll = QCheckBox("follow")
        self.autoscroll.setChecked(True)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        bar.addWidget(QLabel("Log"))
        bar.addWidget(self.filter, 1)
        bar.addWidget(self.autoscroll)
        bar.addWidget(clear)
        lay.addLayout(bar)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(self.MAX_BLOCKS)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.view, 1)

        self.filter.textChanged.connect(self._reapply)
        self._lines: list[str] = []

    def append(self, lines: list[str]) -> None:
        self._lines.extend(lines)
        if len(self._lines) > self.MAX_BLOCKS * 2:
            self._lines = self._lines[-self.MAX_BLOCKS:]
        needle = self.filter.text().strip().lower()
        for ln in lines:
            if needle and needle not in ln.lower():
                continue
            self._emit(ln)
        if self.autoscroll.isChecked():
            self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())

    def _emit(self, line: str) -> None:
        colour = None
        for needle, c in LOG_RULES:
            if needle in line:
                colour = c
                break
        if colour:
            safe = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            self.view.appendHtml(f'<span style="color:{colour}">{safe}</span>')
        else:
            self.view.appendPlainText(line)

    def _reapply(self) -> None:
        self.view.clear()
        needle = self.filter.text().strip().lower()
        for ln in self._lines:
            if not needle or needle in ln.lower():
                self._emit(ln)

    def clear(self) -> None:
        self._lines.clear()
        self.view.clear()


class ValidationPane(QWidget):
    """Structured diagnostics. Double-click emits the location so callers can jump to it."""

    locationActivated = pyqtSignal(str)      # e.g. 'NDIMTIME/DT'

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        self.summary = QLabel("No checks run yet.")
        self.summary.setStyleSheet(f"color:{theme.MUTED};")
        bar.addWidget(self.summary, 1)
        lay.addLayout(bar)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["", "Problem", "Where"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(self._activated)
        lay.addWidget(self.tree, 1)

    def show_diagnostics(self, diags: DiagnosticList, *, context: str = "") -> None:
        self.tree.clear()
        ordered = sorted(diags, key=lambda d: -int(d.severity))
        for d in ordered:
            item = QTreeWidgetItem([d.severity.label, d.message, d.where or ""])
            item.setForeground(0, QBrush(QColor(SEVERITY_COLOR[d.severity])))
            if d.hint:
                item.setToolTip(1, d.hint)
                item.setText(1, f"{d.message}   —  {d.hint}")
            item.setData(0, Qt.ItemDataRole.UserRole, d.where)
            self.tree.addTopLevelItem(item)

        prefix = f"{context}: " if context else ""
        if not diags:
            self.summary.setText(f"{prefix}no problems found.")
            self.summary.setStyleSheet(f"color:{theme.GOOD};")
        elif diags.ok:
            self.summary.setText(f"{prefix}{diags.summary()} — nothing blocking.")
            self.summary.setStyleSheet(f"color:{theme.WARN};")
        else:
            self.summary.setText(f"{prefix}{diags.summary()} — this would block the run.")
            self.summary.setStyleSheet(f"color:{theme.BAD};")

    def _activated(self, item: QTreeWidgetItem, _col: int) -> None:
        where = item.data(0, Qt.ItemDataRole.UserRole)
        if where:
            self.locationActivated.emit(str(where))
