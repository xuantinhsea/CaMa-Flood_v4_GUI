"""Run tab: queue, live monitoring, and the water-balance chart.

The distinctive part is the water-balance panel. CaMa-Flood writes a global budget line on
every adaptive substep, so closure drift is visible *during* the run rather than afterwards.
On an eight-hour global run that is the difference between losing an evening and losing a day.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.project import Case
from ...core.runner import ExitKind, RunProgress, explain_exit
from ...services.execution import JobQueue, QueuedJob
from .. import theme
from ..widgets.charts import WaterBalanceChart
from ..widgets.docks import LogDock

JOB_COLOR = {
    "queued": theme.INFO,
    "running": theme.ACCENT,
    "done": theme.GOOD,
    "failed": theme.BAD,
    "cancelled": theme.MUTED,
}


class RunView(QWidget):
    """The Run Manager tab."""

    logLines = pyqtSignal(list)
    runFinished = pyqtSignal(object, int)      # Case, exit code

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.queue: JobQueue | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        bar = QHBoxLayout()
        self.btn_stop = QPushButton("Stop")
        self.btn_clear = QPushButton("Clear finished")
        self.status = QLabel("Idle")
        self.status.setStyleSheet(f"color:{theme.MUTED};")
        bar.addWidget(QLabel("Queue"))
        bar.addWidget(self.status, 1)
        bar.addWidget(self.btn_clear)
        bar.addWidget(self.btn_stop)
        lay.addLayout(bar)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Case", "State", "Exit"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setMaximumHeight(150)
        ll.addWidget(self.table)

        live = QGroupBox("Live diagnostics")
        form = QFormLayout(live)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.lbl_time = QLabel("-")
        self.lbl_cells = QLabel("-")
        self.lbl_substeps = QLabel("-")
        self.lbl_error = QLabel("-")
        form.addRow("Progress", self.progress)
        form.addRow("Simulated time", self.lbl_time)
        form.addRow("River cells", self.lbl_cells)
        form.addRow("Adaptive substeps", self.lbl_substeps)
        form.addRow("Worst closure error", self.lbl_error)
        ll.addWidget(live)

        self.balance = WaterBalanceChart()
        ll.addWidget(self.balance, 1)
        split.addWidget(left)

        self.log = LogDock()
        split.addWidget(self.log)
        split.setSizes([460, 620])
        lay.addWidget(split, 1)

        self.btn_stop.clicked.connect(self._stop)
        self.btn_clear.clicked.connect(self._clear_finished)
        self.btn_stop.setEnabled(False)

    # ---------------------------------------------------------------- wiring

    def set_queue(self, queue: JobQueue | None) -> None:
        self.queue = queue
        if queue is None:
            return
        queue.queueChanged.connect(self._refresh_table)
        queue.jobStarted.connect(self._job_started)
        queue.jobFinished.connect(self._job_finished)
        queue.queueDrained.connect(self._drained)
        self._refresh_table()

    def enqueue(self, case: Case) -> None:
        if self.queue is None:
            return
        self.queue.enqueue(case)

    # ---------------------------------------------------------------- events

    def _job_started(self, job: QueuedJob) -> None:
        self.log.clear()
        self.balance.clear()
        self.progress.setValue(0)
        self.status.setText(f"Running: {job.case.name}")
        self.status.setStyleSheet(f"color:{theme.ACCENT};")
        self.btn_stop.setEnabled(True)

        proc = self.queue.current if self.queue else None
        if proc is not None:
            proc.logLines.connect(self._on_log)
            proc.stdoutLines.connect(self._on_log)
            proc.progressChanged.connect(self._on_progress)

    def _on_log(self, lines: list[str]) -> None:
        self.log.append(lines)
        self.logLines.emit(lines)

    def _on_progress(self, p: RunProgress) -> None:
        f = p.fraction
        if f is not None:
            self.progress.setValue(int(f * 100))
        if p.current_time:
            self.lbl_time.setText(p.current_time.strftime("%Y-%m-%d %H:%M"))
        if p.nseqmax:
            self.lbl_cells.setText(f"{p.nseqmax:,}")
        if p.last_substeps:
            self.lbl_substeps.setText(f"{p.last_substeps} (max {p.max_substeps})")

        err = p.worst_relative_error
        if err:
            ok = err < 1e-9
            self.lbl_error.setText(f"{err:.2e}" + ("" if ok else "   ABOVE LIMIT"))
            self.lbl_error.setStyleSheet(f"color:{theme.GOOD if ok else theme.BAD};")
        self.balance.update_from(p)

    def _job_finished(self, job: QueuedJob) -> None:
        ok = job.state == "done"
        self.status.setText(
            f"{'Completed' if ok else 'FAILED'}: {job.case.name} (exit {job.returncode})")
        self.status.setStyleSheet(f"color:{theme.GOOD if ok else theme.BAD};")
        if not ok:
            self.log.append(["", explain_exit(job.returncode)])
        self.progress.setValue(100 if ok else self.progress.value())
        self.runFinished.emit(job.case, job.returncode if job.returncode is not None else -1)

    def _drained(self) -> None:
        self.btn_stop.setEnabled(False)
        if self.queue and not any(j.state == "running" for j in self.queue.jobs):
            if all(j.state in ("done", "failed", "cancelled") for j in self.queue.jobs):
                pass  # keep the last message; it says what happened

    def _stop(self) -> None:
        if self.queue:
            self.queue.cancel_all()
            self.status.setText("Stopping...")

    def _clear_finished(self) -> None:
        if self.queue:
            self.queue.jobs = [j for j in self.queue.jobs
                               if j.state in ("queued", "running")]
            self._refresh_table()

    def _refresh_table(self) -> None:
        jobs = self.queue.jobs if self.queue else []
        self.table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            for c, v in enumerate([j.case.name, j.state,
                                   "" if j.returncode is None else str(j.returncode)]):
                item = QTableWidgetItem(v)
                if c == 1:
                    item.setForeground(QBrush(QColor(JOB_COLOR.get(j.state, theme.TEXT))))
                self.table.setItem(r, c, item)
