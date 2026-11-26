"""Project and case model, with provenance and stale-result detection.

A *project* is a study area: one domain, one forcing set, many *cases* (scenario variants).
Each case owns a directory, because CaMa-Flood reads ``input_cmf.nam`` from the working
directory -- so cases are isolated by construction and can run in parallel with no locking.

The feature worth the effort here is **stale-result detection**. Every completed case records a
hash of the inputs that produced it. When the configuration drifts from the results, the case
is marked stale. Almost no scientific GUI does this, and every modeller has at some point
published a figure from a run whose configuration had since moved.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from .errors import DiagnosticList
from .namelist import Namelist
from .output import TimeAxis

SCHEMA_VERSION = 1


class CaseStatus(str, Enum):
    NOT_RUN = "not_run"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"          # completed, but the inputs have changed since

    @property
    def is_terminal(self) -> bool:
        return self in (CaseStatus.COMPLETED, CaseStatus.FAILED, CaseStatus.STALE)


def _hash_file(path: Path, h: "hashlib._Hash", *, max_bytes: int = 8 << 20) -> None:
    """Fold a file into a hash.

    Large binaries (maps, forcing) are sampled rather than read whole: head, tail and size.
    A global map set is gigabytes and hashing it fully on every configuration change would
    make the GUI feel broken. Sampling catches replacement and truncation, which is what we
    are actually guarding against.
    """
    st = path.stat()
    h.update(path.name.encode())
    h.update(str(st.st_size).encode())
    with path.open("rb") as fh:
        if st.st_size <= max_bytes:
            h.update(fh.read())
        else:
            h.update(fh.read(max_bytes // 2))
            fh.seek(-max_bytes // 2, 2)
            h.update(fh.read())


@dataclass
class RunRecord:
    """``run.json`` -- what happened, and what produced it."""

    status: CaseStatus = CaseStatus.NOT_RUN
    started: str | None = None
    finished: str | None = None
    elapsed_s: float | None = None
    returncode: int | None = None
    pid: int | None = None
    log_offset: int = 0
    input_hash: str | None = None
    model_binary: str | None = None
    model_build: dict[str, Any] = field(default_factory=dict)
    period: dict[str, Any] = field(default_factory=dict)
    max_substeps: int | None = None
    worst_balance_error: float | None = None
    outputs: dict[str, int] = field(default_factory=dict)
    gate_overrides: list[str] = field(default_factory=list)
    notes: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        d["status"] = self.status.value
        d["schema_version"] = SCHEMA_VERSION
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "RunRecord":
        d = json.loads(text)
        d.pop("schema_version", None)
        d["status"] = CaseStatus(d.get("status", "not_run"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class Case:
    """One simulation: a directory containing a namelist, outputs and a run record."""

    NAMELIST = "input_cmf.nam"
    RUNFILE = "run.json"
    LOG = "log_CaMa.txt"

    def __init__(self, directory: Path | str, project: "Project | None" = None):
        self.dir = Path(directory)
        self.project = project

    # ---------------------------------------------------------------- basics
    @property
    def name(self) -> str:
        return self.dir.name

    @property
    def namelist_path(self) -> Path:
        return self.dir / self.NAMELIST

    @property
    def log_path(self) -> Path:
        return self.dir / self.LOG

    @property
    def output_dir(self) -> Path:
        return self.dir

    def exists(self) -> bool:
        return self.namelist_path.exists()

    def namelist(self) -> Namelist:
        return Namelist.load(self.namelist_path)

    def time_axis(self, count: int) -> TimeAxis:
        """The output record -> date mapping for this case, from its own NSIMTIME/NOUTPUT.

        ``count`` (an output file's own record count) is the caller's, not derived here --
        letting this method open output files itself would tie a plain namelist read to a
        specific variable's file existing on disk.
        """
        nml = self.namelist()
        try:
            start = dt.datetime(int(nml.get("NSIMTIME", "SYEAR", 2000)),
                                int(nml.get("NSIMTIME", "SMON", 1)),
                                int(nml.get("NSIMTIME", "SDAY", 1)),
                                int(nml.get("NSIMTIME", "SHOUR", 0)))
        except (ValueError, TypeError):
            start = dt.datetime(2000, 1, 1)
        step_hours = int(nml.get("NOUTPUT", "IFRQ_OUT", 24) or 24)
        return TimeAxis(start=start, step_hours=step_hours, count=count)

    # ---------------------------------------------------------------- run record
    def record(self) -> RunRecord:
        p = self.dir / self.RUNFILE
        if not p.exists():
            return RunRecord()
        try:
            return RunRecord.from_json(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return RunRecord(notes="run.json was unreadable and has been reset")

    def save_record(self, rec: RunRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / self.RUNFILE).write_text(rec.to_json(), encoding="utf-8")

    # ---------------------------------------------------------------- provenance
    def input_hash(self) -> str:
        """Hash of everything that determines the result.

        The namelist is hashed by *value*, not by file bytes, so reformatting or adding a
        comment does not mark results stale -- only a real change does. Referenced map and
        forcing files are folded in by name, size and sample.
        """
        h = hashlib.sha256()
        h.update(f"v{SCHEMA_VERSION}".encode())

        if self.namelist_path.exists():
            nml = self.namelist()
            for group in sorted(nml.groups):
                for key, value in sorted(nml.group_items(group).items()):
                    h.update(f"{group}/{key}={value!r};".encode())

            # Fold in the referenced inputs themselves.
            for group in ("NMAP", "NFORCE", "NDIMTIME", "NLEVEE"):
                if group not in nml:
                    continue
                for key, value in sorted(nml.group_items(group).items()):
                    if not isinstance(value, str) or not value.strip():
                        continue
                    if value.strip().upper() == "NONE":
                        continue
                    p = Path(value)
                    if not p.is_absolute():
                        p = self.dir / value
                    try:
                        if p.is_file():
                            _hash_file(p, h)
                    except OSError:
                        h.update(f"UNREADABLE:{value}".encode())
        return h.hexdigest()[:32]

    def status(self) -> CaseStatus:
        """Current status, re-deriving staleness from the input hash."""
        rec = self.record()
        if rec.status is not CaseStatus.COMPLETED:
            return rec.status
        if rec.input_hash and rec.input_hash != self.input_hash():
            return CaseStatus.STALE
        return CaseStatus.COMPLETED

    def is_stale(self) -> bool:
        return self.status() is CaseStatus.STALE

    # ---------------------------------------------------------------- outputs
    def output_files(self) -> list[Path]:
        return sorted(p for p in self.dir.glob("*.bin") if not p.name.startswith("restart"))

    def restart_files(self) -> list[Path]:
        return sorted(self.dir.glob("restart*"))

    def output_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.output_files())

    def clear_outputs(self) -> int:
        n = 0
        for p in self.output_files() + self.restart_files():
            p.unlink()
            n += 1
        return n

    def __repr__(self) -> str:
        return f"<Case {self.name} [{self.status().value}]>"


@dataclass
class ProjectConfig:
    name: str = "Untitled"
    description: str = ""
    created: str = ""
    schema_version: int = SCHEMA_VERSION
    #: Map directory. Relative paths resolve against the project root so projects are portable.
    map_dir: str = "domain"
    forcing_dir: str = "forcing"
    model_binary: str = ""
    default_threads: int = 4

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ProjectConfig":
        d = json.loads(text)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class Project:
    """A study: one domain, one forcing set, many cases."""

    CONFIG = "project.json"
    CASES = "cases"

    def __init__(self, root: Path | str, config: ProjectConfig | None = None):
        self.root = Path(root)
        self.config = config or ProjectConfig(name=self.root.stem)

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def create(cls, root: Path | str, name: str | None = None, **kw) -> "Project":
        root = Path(root)
        cfg = ProjectConfig(name=name or root.stem,
                            created=dt.datetime.now().isoformat(timespec="seconds"), **kw)
        proj = cls(root, cfg)
        for sub in (cls.CASES, cfg.map_dir, cfg.forcing_dir, "analysis", "cache"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        proj.save()
        (root / "cache" / "README.txt").write_text(
            "Cache: rendered tiles, downscaled fields, colour renders.\n"
            "Safe to delete at any time; it will be rebuilt on demand.\n", encoding="utf-8")
        return proj

    @classmethod
    def open(cls, root: Path | str) -> "Project":
        root = Path(root)
        cfg_path = root / cls.CONFIG
        if not cfg_path.exists():
            raise FileNotFoundError(f"not a CaMa_Floodcast project: {cfg_path} is missing")
        return cls(root, ProjectConfig.from_json(cfg_path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / self.CONFIG).write_text(self.config.to_json(), encoding="utf-8")

    # ---------------------------------------------------------------- paths
    def _resolve(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (self.root / value)

    @property
    def map_dir(self) -> Path:
        return self._resolve(self.config.map_dir)

    @property
    def forcing_dir(self) -> Path:
        return self._resolve(self.config.forcing_dir)

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    # ---------------------------------------------------------------- cases
    def cases(self) -> list[Case]:
        base = self.root / self.CASES
        if not base.exists():
            return []
        return [Case(d, self) for d in sorted(base.iterdir()) if d.is_dir()]

    def case(self, name: str) -> Case:
        return Case(self.root / self.CASES / name, self)

    def add_case(self, name: str, *, namelist: Namelist | None = None,
                 copy_from: str | None = None) -> Case:
        case = self.case(name)
        if case.dir.exists() and any(case.dir.iterdir()):
            raise FileExistsError(f"case '{name}' already exists and is not empty")
        case.dir.mkdir(parents=True, exist_ok=True)

        if copy_from:
            src = self.case(copy_from)
            if not src.exists():
                raise FileNotFoundError(f"cannot copy from '{copy_from}': it has no namelist")
            shutil.copy2(src.namelist_path, case.namelist_path)
        elif namelist is not None:
            namelist.save(case.namelist_path, backup=False)

        case.save_record(RunRecord(status=CaseStatus.NOT_RUN))
        return case

    def remove_case(self, name: str, *, keep_outputs: bool = False) -> None:
        case = self.case(name)
        if not case.dir.exists():
            return
        if keep_outputs:
            case.namelist_path.unlink(missing_ok=True)
        else:
            shutil.rmtree(case.dir)

    def cache_bytes(self) -> int:
        if not self.cache_dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.cache_dir.rglob("*") if p.is_file())

    def clear_cache(self) -> int:
        n = self.cache_bytes()
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return n

    def summary(self) -> str:
        cs = self.cases()
        lines = [f"{self.config.name}  ({self.root})",
                 f"  domain : {self.map_dir}",
                 f"  forcing: {self.forcing_dir}",
                 f"  cases  : {len(cs)}"]
        for c in cs:
            st = c.status()
            flag = {"completed": "OK", "stale": "STALE", "failed": "FAIL"}.get(st.value, st.value)
            lines.append(f"     {c.name:24s} {flag:8s} {c.output_bytes() / 1e6:8.1f} MB")
        if self.cache_bytes():
            lines.append(f"  cache  : {self.cache_bytes() / 1e6:.1f} MB (safe to delete)")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"<Project {self.config.name!r} {len(self.cases())} cases>"
