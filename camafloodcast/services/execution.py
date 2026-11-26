"""Running the model from the GUI, without freezing it.

``QProcess`` rather than ``subprocess`` + thread: it is native to the Qt event loop, streams
output as signals, and its lifecycle integrates with the window's.

A subtlety worth stating: CaMa-Flood's *useful* output goes to ``log_CaMa.txt``, not to stdout.
stdout carries only Fortran runtime errors. So a run is monitored two ways at once -- QProcess
for lifecycle and crash messages, and a polling timer over the log file for progress and water
balance. Both feed the same :class:`~camafloodcast.core.runner.LogReader`, so the parsing lives
in exactly one place and is tested headless.

Long runs must survive the GUI: state goes to ``run.json`` (PID, log offset) so a reopened
window can reattach by tailing the log from where it left off.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal

from ..core.project import Case, CaseStatus, RunRecord
from ..core.runner import ExitKind, LogReader, RunProgress, classify_exit, explain_exit


class ModelProcess(QObject):
    """One model run, monitored live."""

    logLines = pyqtSignal(list)          # list[str] of new log lines
    stdoutLines = pyqtSignal(list)       # list[str] from the process itself (errors)
    progressChanged = pyqtSignal(object)  # RunProgress
    started = pyqtSignal()
    finished = pyqtSignal(int, object)   # exit code, ExitKind

    POLL_MS = 400

    def __init__(self, case: Case, executable: Path, *, threads: int | None = None,
                 extra_path: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.case = case
        self.executable = Path(executable)
        self.threads = threads
        self.extra_path = extra_path

        self._proc: QProcess | None = None
        self._reader = LogReader(case.log_path)
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._t0: float | None = None

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        import os
        import time

        if self._proc is not None:
            raise RuntimeError("this run has already been started")

        # Remove the old log so LogReader offsets start clean, and so a stale log can never be
        # mistaken for output from this run.
        if self.case.log_path.exists():
            self.case.log_path.unlink()
        self._reader = LogReader(self.case.log_path)

        proc = QProcess(self)
        proc.setWorkingDirectory(str(self.case.dir))
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        env = proc.processEnvironment()
        from PyQt6.QtCore import QProcessEnvironment
        env = QProcessEnvironment.systemEnvironment()
        if self.threads:
            env.insert("OMP_NUM_THREADS", str(self.threads))
        if self.extra_path:
            # Prepend, never replace: on Windows a MinGW build needs its own bin directory
            # early or it dies with STATUS_ENTRYPOINT_NOT_FOUND and no message, but replacing
            # PATH outright breaks everything else. See risk R4.
            env.insert("PATH", str(self.extra_path) + os.pathsep + env.value("PATH", ""))
        proc.setProcessEnvironment(env)

        proc.readyReadStandardOutput.connect(self._drain_stdout)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        self._proc = proc
        self._t0 = time.monotonic()

        rec = self.case.record()
        rec.status = CaseStatus.RUNNING
        rec.started = dt.datetime.now().isoformat(timespec="seconds")
        rec.model_binary = str(self.executable)
        self.case.save_record(rec)

        proc.start(str(self.executable), [])
        self._timer.start()
        self.started.emit()

    def stop(self) -> None:
        if self._proc and self._proc.state() != QProcess.ProcessState.NotRunning:
            self._proc.kill()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning

    @property
    def progress(self) -> RunProgress:
        return self._reader.progress

    # ---------------------------------------------------------------- internals

    def _drain_stdout(self) -> None:
        if not self._proc:
            return
        text = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if lines:
            self.stdoutLines.emit(lines)

    def _poll(self) -> None:
        lines = self._reader.poll()
        if lines:
            self.logLines.emit(lines)
        self.progressChanged.emit(self._reader.progress)

    def _on_error(self, err) -> None:
        self.stdoutLines.emit([f"process error: {err}"])

    def _on_finished(self, code: int, _status) -> None:
        import time

        self._timer.stop()
        self._poll()                       # drain whatever landed after the last tick
        self._drain_stdout()

        kind = classify_exit(code)
        elapsed = (time.monotonic() - self._t0) if self._t0 else 0.0
        p = self._reader.progress

        rec = self.case.record()
        rec.status = CaseStatus.COMPLETED if kind is ExitKind.SUCCESS else CaseStatus.FAILED
        rec.finished = dt.datetime.now().isoformat(timespec="seconds")
        rec.elapsed_s = round(elapsed, 2)
        rec.returncode = code
        rec.log_offset = self._reader.offset
        rec.max_substeps = p.max_substeps
        rec.worst_balance_error = p.worst_relative_error
        if kind is ExitKind.SUCCESS:
            rec.input_hash = self.case.input_hash()
            rec.outputs = {f.name: f.stat().st_size for f in self.case.output_files()}
        self.case.save_record(rec)

        self.finished.emit(code, kind)


@dataclass
class QueuedJob:
    case: Case
    label: str = ""
    state: str = "queued"       # queued | running | done | failed | cancelled
    returncode: int | None = None


class JobQueue(QObject):
    """Sequential execution of cases, with one place to see and cancel everything.

    Sequential rather than parallel by default: CaMa-Flood is already OpenMP-parallel, so
    running two cases at once usually just contends for the same cores.
    """

    jobStarted = pyqtSignal(object)        # QueuedJob
    jobFinished = pyqtSignal(object)       # QueuedJob
    queueChanged = pyqtSignal()
    queueDrained = pyqtSignal()

    def __init__(self, executable: Path, *, threads: int | None = None,
                 extra_path: Path | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.executable = Path(executable)
        self.threads = threads
        self.extra_path = extra_path
        self.jobs: list[QueuedJob] = []
        self.current: ModelProcess | None = None
        self._current_job: QueuedJob | None = None

    def enqueue(self, case: Case, label: str = "") -> QueuedJob:
        job = QueuedJob(case, label or case.name)
        self.jobs.append(job)
        self.queueChanged.emit()
        if self.current is None:
            self._advance()
        return job

    def cancel_all(self) -> None:
        for j in self.jobs:
            if j.state == "queued":
                j.state = "cancelled"
        if self.current:
            self.current.stop()
        self.queueChanged.emit()

    @property
    def busy(self) -> bool:
        return self.current is not None

    def _advance(self) -> None:
        nxt = next((j for j in self.jobs if j.state == "queued"), None)
        if nxt is None:
            self.current = None
            self._current_job = None
            self.queueDrained.emit()
            return

        nxt.state = "running"
        self._current_job = nxt
        proc = ModelProcess(nxt.case, self.executable,
                            threads=self.threads, extra_path=self.extra_path, parent=self)
        proc.finished.connect(self._on_job_finished)
        self.current = proc
        self.queueChanged.emit()
        self.jobStarted.emit(nxt)
        proc.start()

    def _on_job_finished(self, code: int, kind) -> None:
        job = self._current_job
        if job is not None:
            job.state = "done" if kind is ExitKind.SUCCESS else "failed"
            job.returncode = code
            self.jobFinished.emit(job)
        self.current = None
        self._current_job = None
        self.queueChanged.emit()
        self._advance()
