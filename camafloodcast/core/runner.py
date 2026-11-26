"""Running CaMa-Flood and understanding what it says while it runs.

The invocation contract, verified against v4.30 source and by test:

* ``MAIN_cmf`` takes **no command-line arguments**
* it reads ``input_cmf.nam`` from the **current working directory**
* it writes ``./log_CaMa.txt`` there
* threads come from ``OMP_NUM_THREADS``
* exit 0 = success, 2 = Fortran runtime error, 9 = the model's own ``STOP 9`` validation

One consequence worth stating: ``log_CaMa.txt`` is created *before* the model validates its
input, so the log existing is **not** evidence that the run started successfully. Always check
the exit code. (Confirmed by running with no namelist present: exit 2, log file created.)

The log parser is what makes the live water-balance monitor possible. CaMa-Flood writes a
global budget line on every adaptive substep when ``LWBLOG=.TRUE.``; if the closure error
starts growing, the user should see it at minute three of an eight-hour run, not afterwards.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

from .errors import ModelRunError

# --------------------------------------------------------------------------- exit codes


class ExitKind(Enum):
    SUCCESS = "success"
    RUNTIME_ERROR = "fortran_runtime_error"   # 2: missing file, bad namelist, bad RECL
    MODEL_STOP = "model_validation_stop"      # 9: CMF_CONFIG_CHECK rejected the setup
    KILLED = "killed"
    UNKNOWN = "unknown"


def classify_exit(code: int | None) -> ExitKind:
    if code is None:
        return ExitKind.KILLED
    return {0: ExitKind.SUCCESS, 2: ExitKind.RUNTIME_ERROR, 9: ExitKind.MODEL_STOP}.get(
        code, ExitKind.UNKNOWN)


def explain_exit(code: int | None, log_tail: str = "") -> str:
    """A human explanation, because the raw codes mean nothing to a hydrologist."""
    kind = classify_exit(code)
    if kind is ExitKind.SUCCESS:
        return "Run completed."
    if kind is ExitKind.RUNTIME_ERROR:
        return ("The model hit a Fortran runtime error (exit 2). This is usually a missing or "
                "unreadable file, or a malformed namelist. The message names the source file "
                "and line, which is precise but not user-friendly.")
    if kind is ExitKind.MODEL_STOP:
        return ("CaMa-Flood rejected the configuration (STOP 9). Its own check failed -- for "
                "example DT not a multiple of 60, or LKINE together with LPTHOUT. The log "
                "explains which.")
    if kind is ExitKind.KILLED:
        return "The model process was terminated before it finished."
    return f"The model exited with code {code}."


# --------------------------------------------------------------------------- log parsing


@dataclass(frozen=True)
class WaterBalance:
    """One global budget line.

    CaMa-Flood prints storages in units of 1e9 m3 (or m2 for area). The two closure errors are
    what matters: a healthy run holds them near 1e-14 relative to total storage.
    """

    when: dt.datetime
    substep: int
    # flux budget
    sto_pre: float
    sto_nxt: float
    sto_new: float
    err_flux: float
    inflow: float
    outflow: float
    # stage budget
    sto_pre2: float
    sto_new2: float
    err_stage: float
    riv_storage: float
    fld_storage: float
    flooded_area: float

    @property
    def relative_flux_error(self) -> float:
        return abs(self.err_flux) / max(abs(self.sto_new2), 1e-30)

    @property
    def relative_stage_error(self) -> float:
        return abs(self.err_stage) / max(abs(self.sto_new2), 1e-30)

    @property
    def healthy(self) -> bool:
        return max(self.relative_flux_error, self.relative_stage_error) < 1e-9


@dataclass(frozen=True)
class AdaptiveStep:
    """An ``ADPSTP:`` line -- how finely the model had to subdivide the outer step."""

    substeps: int
    outer_dt: float
    cfl_dt: float
    actual_dt: float


@dataclass
class RunProgress:
    """Accumulated understanding of a run, updated line by line."""

    nseqmax: int | None = None
    flat_layers: int | None = None
    degenerate_cells: int | None = None
    current_time: dt.datetime | None = None
    start_time: dt.datetime | None = None
    end_time: dt.datetime | None = None
    last_substeps: int | None = None
    max_substeps: int = 0
    balances: list[WaterBalance] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def fraction(self) -> float | None:
        """0..1 through the simulated period, from the log's own date stamps."""
        if not (self.start_time and self.end_time and self.current_time):
            return None
        total = (self.end_time - self.start_time).total_seconds()
        if total <= 0:
            return None
        done = (self.current_time - self.start_time).total_seconds()
        return max(0.0, min(1.0, done / total))

    @property
    def worst_relative_error(self) -> float:
        if not self.balances:
            return 0.0
        return max(max(b.relative_flux_error, b.relative_stage_error) for b in self.balances)


_RE_WB = re.compile(
    r"(\d{4})/(\d{2})/(\d{2})_(\d{2}):(\d{2})\s+(\d+)\s+flx:\s+(.*?)\s+stg:\s+(.*)$"
)
_RE_ADPSTP = re.compile(r"ADPSTP:\s*NT=\s*(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)")
_RE_NSEQ = re.compile(r"NSEQMAX=\s*(\d+)")
_RE_FLAT = re.compile(r"flat layers\s*=\s*(\d+)")
_RE_DEGEN = re.compile(r"NO floodplain relief.*?:\s*(\d+)")
_RE_ADVANCE = re.compile(r"DRV_ADVANCE END.*?:\s*\d+\s+(\d{8})\s+(\d{1,4})")
_RE_SIMTIME = re.compile(r"(Start|End)\s+Date.*?(\d{8})", re.I)


def parse_log_line(line: str) -> WaterBalance | AdaptiveStep | dict | None:
    """Turn one log line into something structured, or None if it carries no state."""
    m = _RE_WB.search(line)
    if m:
        try:
            when = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                               int(m.group(4)) % 24, int(m.group(5)))
        except ValueError:
            return None
        flx = [float(x) for x in m.group(7).split()]
        stg = [float(x) for x in m.group(8).split()]
        if len(flx) < 6 or len(stg) < 6:
            return None
        return WaterBalance(when, int(m.group(6)), *flx[:6], *stg[:6])

    m = _RE_ADPSTP.search(line)
    if m:
        return AdaptiveStep(int(m.group(1)), float(m.group(2)),
                            float(m.group(3)), float(m.group(4)))

    for rx, key in ((_RE_NSEQ, "nseqmax"), (_RE_FLAT, "flat_layers"),
                    (_RE_DEGEN, "degenerate_cells")):
        m = rx.search(line)
        if m:
            return {key: int(m.group(1))}

    if "Fortran runtime error" in line or "Error termination" in line:
        return {"error": line.strip()}

    return None


class LogReader:
    """Incremental log reader: parse only what has been appended since last time.

    The GUI polls this from a worker thread. ``offset`` is persisted in ``run.json`` so a
    reattached run resumes where it left off rather than re-reading a 12 MB log.
    """

    def __init__(self, path: Path | str, *, keep_balances: int = 5000):
        self.path = Path(path)
        self.offset = 0
        self.progress = RunProgress()
        self.keep_balances = keep_balances

    def poll(self) -> list[str]:
        """Read new lines, update ``progress``, and return the raw lines for the log dock."""
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:        # file was truncated: a new run started here
            self.offset = 0
            self.progress = RunProgress()
        if size == self.offset:
            return []

        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            text = fh.read()
            self.offset = fh.tell()

        lines = text.splitlines()
        for line in lines:
            self._apply(parse_log_line(line))
        return lines

    def _apply(self, item) -> None:
        p = self.progress
        if isinstance(item, WaterBalance):
            p.current_time = item.when
            if p.start_time is None:
                p.start_time = item.when
            p.balances.append(item)
            if len(p.balances) > self.keep_balances:
                # Thin by half rather than dropping the head: keeps the whole run visible.
                p.balances = p.balances[::2]
        elif isinstance(item, AdaptiveStep):
            p.last_substeps = item.substeps
            p.max_substeps = max(p.max_substeps, item.substeps)
        elif isinstance(item, dict):
            if "error" in item:
                p.errors.append(item["error"])
            else:
                for k, v in item.items():
                    setattr(p, k, v)

    def read_all(self) -> RunProgress:
        self.poll()
        return self.progress


# --------------------------------------------------------------------------- execution


@dataclass
class RunResult:
    returncode: int | None
    kind: ExitKind
    explanation: str
    elapsed_s: float
    progress: RunProgress
    log_path: Path

    @property
    def ok(self) -> bool:
        return self.kind is ExitKind.SUCCESS


class ModelRunner:
    """Launch CaMa-Flood in a case directory.

    This is the headless Phase 0 implementation using ``subprocess``. Phase 1 swaps in
    ``QProcess`` for the GUI but reuses :class:`LogReader` unchanged -- the parsing is the part
    worth getting right, and it should not be duplicated.
    """

    LOG_NAME = "log_CaMa.txt"
    NAMELIST_NAME = "input_cmf.nam"

    def __init__(self, executable: Path | str, *, threads: int | None = None,
                 extra_path: Path | str | None = None):
        self.executable = Path(executable)
        self.threads = threads
        #: Prepend to PATH, never replace it. On Windows a MinGW build needs its own bin
        #: directory early or it dies with STATUS_ENTRYPOINT_NOT_FOUND and no message;
        #: replacing PATH outright breaks Python and everything else. See risk R4.
        self.extra_path = Path(extra_path) if extra_path else None

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.threads:
            env["OMP_NUM_THREADS"] = str(self.threads)
        if self.extra_path:
            env["PATH"] = str(self.extra_path) + os.pathsep + env.get("PATH", "")
        return env

    def run(self, case_dir: Path | str, *,
            on_line: Callable[[str], None] | None = None,
            on_progress: Callable[[RunProgress], None] | None = None,
            poll_s: float = 0.5,
            timeout_s: float | None = None) -> RunResult:
        """Run to completion, streaming the log.

        ``on_line`` receives raw log lines (for the log dock); ``on_progress`` receives the
        accumulated :class:`RunProgress` (for the progress bar and water-balance plot).
        """
        case = Path(case_dir)
        if not (case / self.NAMELIST_NAME).exists():
            raise ModelRunError(
                f"{self.NAMELIST_NAME} not found in {case}. CaMa-Flood reads it from the "
                f"working directory and takes no command-line arguments."
            )
        if not self.executable.exists():
            raise ModelRunError(f"model executable not found: {self.executable}")

        log_path = case / self.LOG_NAME
        if log_path.exists():
            log_path.unlink()          # so LogReader offsets start clean

        reader = LogReader(log_path)
        t0 = time.monotonic()

        proc = subprocess.Popen(
            [str(self.executable)],
            cwd=str(case),
            env=self.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )

        stdout_tail: list[str] = []
        try:
            while proc.poll() is None:
                for line in reader.poll():
                    if on_line:
                        on_line(line)
                if on_progress:
                    on_progress(reader.progress)
                if timeout_s and (time.monotonic() - t0) > timeout_s:
                    proc.kill()
                    raise ModelRunError(f"run exceeded {timeout_s} s and was killed")
                time.sleep(poll_s)
        finally:
            if proc.stdout:
                stdout_tail = proc.stdout.read().splitlines()[-40:]
            proc.wait()

        for line in reader.poll():     # drain whatever landed after the last poll
            if on_line:
                on_line(line)
        if on_progress:
            on_progress(reader.progress)

        elapsed = time.monotonic() - t0
        kind = classify_exit(proc.returncode)
        explanation = explain_exit(proc.returncode)
        if kind is not ExitKind.SUCCESS and stdout_tail:
            explanation += "\n" + "\n".join(stdout_tail[-6:])

        return RunResult(proc.returncode, kind, explanation, elapsed,
                         reader.progress, log_path)
