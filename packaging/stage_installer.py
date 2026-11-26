"""Stage everything the Windows installer ships *besides* the frozen app itself.

Run by ``build_installer.bat`` after ``build_exe.bat``; writes ``build/installer_stage/``::

    runtime/     MinGW runtime DLLs + manifest.json -> installed into {app}\\bin\\win64
    docs/        USER_MANUAL.md / .docx, NOTICE.txt, CaMa-Flood_LICENSE.txt
    data/        the example workspace          -> installed into the user's data folder
      projects/  mekong, vietnam, philippines, cagayan (cleaned copies)
      map/       the regional maps those projects use (incl. 1-arcmin downscaling data)
      inp/       the shared 1-degree runoff forcing

Why this is more than a copy
----------------------------
The development projects reference their maps and forcing by **absolute** path
(``C:\\...\\cmf_v430_pkg_20260312\\map\\mekong_15min\\nextxy.bin``). Here every such path is
rewritten **relative** -- namelist paths relative to the case directory (the model's own working
directory), ``project.json``'s ``map_dir`` relative to the project root -- so the installed
workspace works wherever the user puts it, with no install-time patching.

A namelist's values are part of each case's ``input_hash`` (stale-result detection), so changing
the paths changes the hash. A case that was up to date in the source tree is therefore re-hashed
here against the staged inputs, which are byte-identical to the originals; a case that was
already stale stays stale. Namelists are edited through the app's own ``Namelist`` class --
never by text substitution -- so only the changed values are rewritten.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # .../CaMa_Floodcast
PKG = ROOT.parent                                   # .../cmf_v430_pkg_20260312
sys.path.insert(0, str(ROOT))

from camafloodcast.core.namelist import Namelist    # noqa: E402
from camafloodcast.core.project import CaseStatus, Project  # noqa: E402
from camafloodcast.core.schema import validate      # noqa: E402

#: Example projects to ship, and what to leave out of each (paths relative to the project root,
#: fnmatch patterns allowed). ``rename`` moves a sub-folder to a clearer name; namelist paths
#: that pointed into it follow it. ``restore`` sets namelist values per case before anything
#: else, and ``rerun`` re-runs those cases in the staged tree with the bundled model.
PROJECTS: dict[str, dict] = {
    "mekong": {},
    "vietnam": {},
    "philippines": {
        # The working copy was last edited for a Nov-2020 ERA5-Land experiment that never
        # produced output (its 0.25-degree files do not match the 1-degree input matrix the
        # namelist still uses); the result files on disk are the documented year-2000 run.
        # Ship the documented configuration -- the earliest backup differs from the working
        # namelist in exactly these six values -- and re-run it so results and namelist are
        # provably from the same run.
        "drop": ["forcing/*"],
        "restore": {"baseline": {
            ("NSIMTIME", "SYEAR"): 2000, ("NSIMTIME", "SMON"): 1,
            ("NSIMTIME", "EYEAR"): 2000, ("NSIMTIME", "EMON"): 12, ("NSIMTIME", "EDAY"): 31,
            ("NFORCE", "CROFDIR"): str(PKG / "inp" / "test_1deg" / "runoff") + "/",
        }},
        "rerun": ["baseline"],
    },
    "cagayan": {"drop": ["baseline_project"]},
}

#: Dropped from every project: namelist backups, run logs (only read during a live run, and
#: deleted by the app before every new one), cached downscaling renders (rebuilt on demand).
DROP_EVERYWHERE = ["*.bak", "log_CaMa.txt", "downscale"]

#: DLLs that Windows itself provides -- never bundled.
SYSTEM_DLL = re.compile(r"^(api-ms-win-.*|kernel32|user32|advapi32|msvcrt|ws2_32|shell32|"
                        r"ole32|oleaut32|gdi32|ucrtbase|ntdll|bcrypt|secur32|crypt32)\.dll$", re.I)


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(msg, flush=True)


def rel(target: Path, start: Path) -> str:
    """Relative path, forward slashes (gfortran and Python both accept them on Windows)."""
    return os.path.relpath(target, start).replace("\\", "/")


def norm(p: str) -> str:
    return p.replace("\\", "/").rstrip("/").lower()


class PathMapper:
    """Maps an absolute development path onto its location in the staged workspace."""

    def __init__(self, data: Path):
        self.data = data
        self.maps: set[str] = set()        # map/<region> directories to copy
        self.inputs: set[str] = set()      # inp/... directories to copy

    def staged(self, value: str) -> Path | None:
        v = value.replace("\\", "/")
        pkg = PKG.as_posix().rstrip("/") + "/"
        if not v.lower().startswith(pkg.lower()):
            return None
        tail = v[len(pkg):]
        parts = [p for p in tail.split("/") if p]
        if parts[:1] == ["map"] and len(parts) >= 2:
            self.maps.add(parts[1])
            return self.data.joinpath(*parts)
        if parts[:1] == ["inp"] and len(parts) >= 2:
            # Forcing is referenced as a directory (CROFDIR) or a file in one.
            src = PKG.joinpath(*parts)
            d = parts if src.is_dir() else parts[:-1]
            self.inputs.add("/".join(d))
            return self.data.joinpath(*parts)
        if parts[:2] == ["CaMa_Floodcast", "projects"] and len(parts) >= 3:
            name, rest = parts[2], "/".join(parts[3:])
            for old, new in PROJECTS.get(name, {}).get("rename", {}).items():
                if rest == old or rest.startswith(old + "/"):
                    rest = new + rest[len(old):]
            return self.data / "projects" / name / rest
        raise SystemExit(f"ERROR: don't know where to stage referenced path {value!r}")


def ignore_for(project_root: Path, spec: dict):
    drops = list(spec.get("drop", [])) + list(spec.get("rename", {}))

    def ignore(directory: str, names: list[str]) -> set[str]:
        out = set()
        here = Path(directory)
        for n in names:
            relp = (here / n).relative_to(project_root).as_posix()
            if any(fnmatch.fnmatch(n, pat) for pat in DROP_EVERYWHERE):
                out.add(n)
            elif any(fnmatch.fnmatch(relp, pat) for pat in drops):
                out.add(n)
            elif here.name == "cache" and n != "README.txt":
                out.add(n)
        return out
    return ignore


# --------------------------------------------------------------------------- stages

def stage_project(name: str, spec: dict, data: Path, mapper: PathMapper) -> None:
    src_root = ROOT / "projects" / name
    dst_root = data / "projects" / name
    log(f"  project {name}")
    src_proj = Project.open(src_root)

    shutil.copytree(src_root, dst_root, ignore=ignore_for(src_root, spec))
    for old, new in spec.get("rename", {}).items():
        shutil.copytree(src_root / old, dst_root / new)

    # Which source cases were up to date? Only those get re-hashed against staged inputs.
    fresh = {c.name for c in src_proj.cases() if c.status() is CaseStatus.COMPLETED}

    proj = Project.open(dst_root)
    for case in proj.cases():
        nml = case.namelist()
        for (group, key), value in spec.get("restore", {}).get(case.name, {}).items():
            nml.set(group, key, value)
        changed = 0
        for entry in nml.entries:
            if not isinstance(entry.value, str) or not entry.value.strip():
                continue
            target = mapper.staged(entry.value)
            if target is None:
                continue
            new = rel(target, case.dir) + ("/" if entry.value.rstrip().endswith(("/", "\\")) else "")
            nml.set(entry.group, entry.key, new)
            changed += 1
        nml.save(backup=False)

        # Restart files left over from earlier runs of a different period.
        end = "{:04d}{:02d}{:02d}".format(int(nml.get("NSIMTIME", "EYEAR", 0)),
                                          int(nml.get("NSIMTIME", "EMON", 0)),
                                          int(nml.get("NSIMTIME", "EDAY", 0)))
        for r in case.dir.glob("restart*"):
            if end not in r.name:
                r.unlink()

        rec = case.record()
        if rec.model_binary:
            rec.model_binary = Path(rec.model_binary.replace("\\", "/")).name
        if case.name in fresh:
            rec.input_hash = None          # recomputed after the data tree exists
        case.save_record(rec)
        log(f"    case {case.name}: {changed} path(s) made relative")

    cfg = proj.config
    target = mapper.staged(cfg.map_dir)
    if target is not None:
        cfg.map_dir = rel(target, dst_root)
    cfg.model_binary = ""          # resolved on first open to the installed bin\win64\MAIN_cmf.exe
    proj.save()


def rerun_cases(data: Path, runtime: Path) -> list[str]:
    """Re-run the cases a project spec lists under ``rerun``, recording the run the way the
    GUI's own job queue does (services/execution.py ``_on_finished``)."""
    import datetime as dt

    from camafloodcast.core.runner import ExitKind, ModelRunner

    problems = []
    exe = ROOT / "bin" / "win64" / "MAIN_cmf.exe"
    runner = ModelRunner(exe, threads=4, extra_path=runtime)
    for name, spec in PROJECTS.items():
        for cname in spec.get("rerun", []):
            case = Project.open(data / "projects" / name).case(cname)
            for f in case.output_files() + case.restart_files():
                f.unlink()
            log(f"  running {name}/{cname} ...")
            started = dt.datetime.now().isoformat(timespec="seconds")
            res = runner.run(case.dir)
            rec = case.record()
            rec.started = started
            rec.finished = dt.datetime.now().isoformat(timespec="seconds")
            rec.elapsed_s = round(res.elapsed_s, 2)
            rec.returncode = res.returncode
            rec.max_substeps = res.progress.max_substeps
            rec.worst_balance_error = res.progress.worst_relative_error
            rec.model_binary = exe.name
            rec.log_offset = 0
            if res.kind is ExitKind.SUCCESS:
                rec.status = CaseStatus.COMPLETED
                rec.input_hash = case.input_hash()
                rec.outputs = {f.name: f.stat().st_size for f in case.output_files()}
                log(f"    completed in {res.elapsed_s:.0f} s, worst water-balance error "
                    f"{res.progress.worst_relative_error:.2e}")
            else:
                rec.status = CaseStatus.FAILED
                problems.append(f"{name}/{cname}: re-run failed: {res.explanation}")
            case.save_record(rec)
            res.log_path.unlink(missing_ok=True)     # logs are not shipped (see DROP_EVERYWHERE)
    return problems


def finalize_hashes(data: Path) -> list[str]:
    """Re-hash up-to-date cases against the staged inputs, then check every case."""
    problems = []
    for pdir in sorted((data / "projects").iterdir()):
        proj = Project.open(pdir)
        if not (proj.map_dir / "params.txt").exists():
            problems.append(f"{pdir.name}: map_dir does not resolve ({proj.config.map_dir})")
        for case in proj.cases():
            rec = case.record()
            if rec.status is CaseStatus.COMPLETED and rec.input_hash is None:
                rec.input_hash = case.input_hash()
                case.save_record(rec)
            diags = validate(case.namelist(), base_dir=case.dir, check_files=True)
            missing = [d for d in diags if "MISSING" in d.code or d.code.endswith("_FILE")]
            for d in missing:
                problems.append(f"{pdir.name}/{case.name}: {d.message}")
            crof = case.namelist().get("NFORCE", "CROFDIR", "")
            if crof and not (case.dir / crof).is_dir():
                problems.append(f"{pdir.name}/{case.name}: CROFDIR {crof} not found")
            log(f"  {pdir.name}/{case.name}: {case.status().value}")
    return problems


def stage_runtime(out: Path) -> None:
    """MinGW runtime DLLs the Fortran tools need, resolved recursively, plus the manifest."""
    import pefile

    tools = ROOT / "bin" / "win64"
    gfortran = shutil.which("gfortran")
    search = [Path(gfortran).parent] if gfortran else []
    search += [Path(r"C:\msys64\ucrt64\bin"), Path(r"C:\msys64\mingw64\bin")]
    search = [d for d in search if d.is_dir()]
    if not search:
        raise SystemExit("ERROR: no MinGW bin directory found (gfortran / C:\\msys64\\ucrt64\\bin)")

    def imports(path: Path) -> list[str]:
        pe = pefile.PE(str(path), fast_load=True)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
        return [e.dll.decode() for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])]

    out.mkdir(parents=True)
    todo = [p for p in tools.glob("*.exe")]
    seen: set[str] = set()
    while todo:
        for dll in imports(todo.pop()):
            if SYSTEM_DLL.match(dll) or dll.lower() in seen:
                continue
            seen.add(dll.lower())
            found = next((d / dll for d in search if (d / dll).is_file()), None)
            if found is None:
                log(f"  note: {dll} not found in MinGW directories (assumed to be a system DLL)")
                continue
            shutil.copy2(found, out / found.name)
            todo.append(found)
            log(f"  runtime {found.name}  (from {found.parent})")

    manifest = PKG / "src" / "manifest.json"
    if manifest.exists():
        shutil.copy2(manifest, out / "manifest.json")
        log("  manifest.json (model build signature)")


def stage_docs(out: Path) -> None:
    out.mkdir(parents=True)
    for f in ("USER_MANUAL.md", "USER_MANUAL.docx"):
        if not (ROOT / f).exists():
            raise SystemExit(f"ERROR: {f} is missing -- run packaging/md_to_docx.py first")
        shutil.copy2(ROOT / f, out / f)
    shutil.copy2(ROOT / "packaging" / "NOTICE.txt", out / "NOTICE.txt")
    if (PKG / "LICENSE").exists():
        shutil.copy2(PKG / "LICENSE", out / "CaMa-Flood_LICENSE.txt")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "installer_stage")
    args = ap.parse_args()
    out: Path = args.out.resolve()

    if out.exists():
        shutil.rmtree(out)
    data = out / "data"
    mapper = PathMapper(data)

    log("[stage] example projects")
    for name, spec in PROJECTS.items():
        stage_project(name, spec, data, mapper)

    log("[stage] shared map and forcing data")
    for region in sorted(mapper.maps):
        shutil.copytree(PKG / "map" / region, data / "map" / region)
        log(f"  map/{region}")
    def empty_files(directory: str, names: list[str]) -> set[str]:
        # e.g. a zero-byte Roff____20210401.one left in the 2000-2001 forcing by a download test
        return {n for n in names if (Path(directory) / n).is_file()
                and (Path(directory) / n).stat().st_size == 0}

    for d in sorted(mapper.inputs):
        shutil.copytree(PKG / d, data / d, ignore=empty_files)
        log(f"  {d}")

    log("[stage] model runtime")
    stage_runtime(out / "runtime")

    log("[stage] re-running cases whose configuration was restored")
    problems = rerun_cases(data, out / "runtime")

    log("[stage] re-hashing and checking the staged cases")
    problems += finalize_hashes(data)

    log("[stage] documentation")
    stage_docs(out / "docs")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    log(f"[stage] done: {out}  ({size / 2**20:,.0f} MB)")
    if problems:
        log("PROBLEMS:")
        for p in problems:
            log("  " + p)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
