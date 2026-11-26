"""Build a complete, permanent Mekong test project in the app's own project layout.

Unlike ``phase0_end_to_end.py`` (which builds a throwaway demo and is really a Phase 0 exit-
criterion script), this creates a project meant to stay around: ``projects/mekong/``, openable
directly via File -> Open project in the GUI, with a full year run, real output, and a gauge
list ready to import. It reuses the exact configuration ``phase0_end_to_end.py --case mekong``
already validated (v4.30, 5,528 cells, 268 bifurcation paths -- see 08-model-binaries.md #8.7).

    python examples/make_mekong_project.py [--days 365] [--threads 4]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camafloodcast.adapters.camaflood import CamaFloodAdapter
from camafloodcast.core import RiverMap, read_params
from camafloodcast.core.analysis import Gauge, extract_hydrograph, snap_gauge, save_gauges
from camafloodcast.core.domain import analyse_domain, find_outlet_near, validate_domain
from camafloodcast.core.namelist import new_namelist
from camafloodcast.core.output import OutputSet
from camafloodcast.core.project import CaseStatus, Project, RunRecord
from camafloodcast.core.runner import ModelRunner

PKG = Path(__file__).resolve().parents[2]
MAP_DIR = PKG / "map" / "mekong_15min"
EXE = PKG / "src" / ("MAIN_cmf.exe" if sys.platform == "win32" else "MAIN_cmf")
PROJECT_ROOT = Path(__file__).resolve().parents[1] / "projects" / "mekong"

GAUGES = [
    Gauge("Mekong @ Kratie", 106.02, 12.48, 646000),
    Gauge("Mekong @ Pakse", 105.80, 15.12, 545000),
    Gauge("Mekong @ Chiang Saen", 100.08, 20.27, 189000),
]


def banner(text: str) -> None:
    print()
    print("=" * 74)
    print(f" {text}")
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    if not (MAP_DIR / "params.txt").exists():
        print(f"map not built: {MAP_DIR}")
        return 1
    if not EXE.exists():
        print(f"model binary not found: {EXE}")
        return 1

    adapter = CamaFloodAdapter()

    # ---------------------------------------------------------------- 1. domain, Gate 1
    banner("1. DOMAIN -- Mekong basin (93-110E, 8-34N)")
    rmap = RiverMap(MAP_DIR)
    report = analyse_domain(rmap)
    print(report.summary())

    target = find_outlet_near(report, 106.38, 9.62, radius_deg=0.5)
    diags = validate_domain(report, target_basin=target)
    print()
    print(f"Gate 1 (domain closure): {diags.summary()} -> {'PASS' if diags.ok else 'BLOCKED'}")
    if not diags.ok:
        for d in diags:
            print(f"   {d}")
        return 1

    # ---------------------------------------------------------------- 2. project + case
    banner("2. PROJECT AND CASE")
    if PROJECT_ROOT.exists():
        import shutil
        shutil.rmtree(PROJECT_ROOT)
    proj = Project.create(PROJECT_ROOT, name="Mekong basin",
                          map_dir=str(MAP_DIR), model_binary=str(EXE),
                          default_threads=args.threads)

    grid = read_params(MAP_DIR / "params.txt")
    end = dt.date(2000, 1, 1) + dt.timedelta(days=args.days)

    nml = new_namelist(["NRUNVER", "NDIMTIME", "NPARAM", "NSIMTIME",
                        "NMAP", "NRESTART", "NFORCE", "NOUTPUT"])
    nml.update("NRUNVER", dict(LADPSTP=True, LFLDOUT=True, LPTHOUT=True,
                               LDAMOUT=False, LRESTART=False, LWBLOG=True))
    nml.update("NDIMTIME", {"CDIMINFO": str(MAP_DIR / "diminfo_test-1deg.txt"),
                            "DT": 3600, "IFRQ_INP": 24})
    nml.update("NPARAM", dict(PMANRIV=0.03, PMANFLD=0.10, PDSTMTH=10000.0,
                              PCADP=0.7, PSTOLIM=0.05))
    nml.update("NSIMTIME", dict(SYEAR=2000, SMON=1, SDAY=1, SHOUR=0,
                                EYEAR=end.year, EMON=end.month, EDAY=end.day, EHOUR=0))
    nml.update("NMAP", {k: str(MAP_DIR / v) for k, v in {
        "CNEXTXY": "nextxy.bin", "CGRAREA": "ctmare.bin", "CELEVTN": "elevtn.bin",
        "CNXTDST": "nxtdst.bin", "CRIVLEN": "rivlen.bin", "CFLDHGT": "fldhgt.bin",
        "CRIVWTH": "rivwth_gwdlr.bin", "CRIVHGT": "rivhgt.bin", "CRIVMAN": "rivman.bin",
        "CPTHOUT": "bifprm.txt"}.items()} | {"LMAPCDF": False, "CFLDMAN": "NONE"})
    nml.update("NRESTART", {"CRESTSTO": "", "CRESTDIR": "./", "CVNREST": "restart",
                            "LRESTCDF": False, "IFRQ_RST": 0})
    nml.update("NFORCE", {"LINPCDF": False, "LINTERP": True,
                          "CINPMAT": str(MAP_DIR / "inpmat_test-1deg.bin"),
                          "DROFUNIT": 86400000,
                          "CROFDIR": str(PKG / "inp" / "test_1deg" / "runoff") + "/",
                          "CROFPRE": "Roff____", "CROFSUF": ".one"})
    nml.update("NOUTPUT", {"COUTDIR": "./", "COUTTAG": "mekong", "LOUTVEC": False,
                           "LOUTCDF": False, "NDLEVEL": 0, "IFRQ_OUT": 24,
                           "CVARSOUT": "outflw,storge,rivdph,flddph,fldfrc,sfcelv"})

    case = proj.add_case("baseline", namelist=nml)
    print(f"created case '{case.name}' at {case.dir}")

    # ---------------------------------------------------------------- 3. Gate 3
    banner("3. PRE-FLIGHT VALIDATION (Gate 3)")
    diags = adapter.validate(case.namelist(), base_dir=case.dir)
    print(f"{diags.summary()} -> {'PASS' if diags.ok else 'BLOCKED'}")
    if not diags.ok:
        for d in diags:
            print(f"   {d}")
        return 1

    # ---------------------------------------------------------------- 4. run
    banner(f"4. RUN -- {args.days} days")
    runner = ModelRunner(EXE, threads=args.threads,
                         extra_path=Path(r"C:\msys64\ucrt64\bin") if sys.platform == "win32" else None)

    last = {"pct": -1}

    def on_progress(p):
        f = p.fraction
        if f is None:
            return
        pct = int(f * 100)
        if pct >= last["pct"] + 10:
            last["pct"] = pct
            err = p.worst_relative_error
            print(f"   {pct:3d}%  substeps={p.last_substeps}  worst balance error={err:.2e}")

    rec = case.record()
    rec.status = CaseStatus.RUNNING
    rec.started = dt.datetime.now().isoformat(timespec="seconds")
    case.save_record(rec)

    result = runner.run(case.dir, on_progress=on_progress)
    print(f"\nexit {result.returncode} ({result.kind.value}) in {result.elapsed_s:.1f} s")
    print(f"   {result.explanation.splitlines()[0]}")

    # ---------------------------------------------------------------- 5. Gate 4
    banner("5. POST-RUN CHECK (Gate 4)")
    p = result.progress
    print(f"max adaptive substeps   : {p.max_substeps}")
    print(f"worst relative error    : {p.worst_relative_error:.3e}  (limit 1e-9)")
    balance_ok = p.worst_relative_error < 1e-9

    outs = OutputSet(case.dir, grid, tag="mekong")
    finite_ok = True
    for var in outs.variables:
        f = outs[var]
        bad, lo, hi = f.check_finite(mask=rmap.land_mask)
        flag = "" if bad == 0 else "   <-- NON-FINITE"
        finite_ok &= bad == 0
        print(f"   {var:8s} {f.nrec:3d} rec  min={lo:12.4g} max={hi:12.4g}{flag}")

    gate4 = result.ok and balance_ok and finite_ok
    print(f"\nGate 4: {'PASS' if gate4 else 'FAIL'}")

    rec = case.record()
    rec.status = CaseStatus.COMPLETED if gate4 else CaseStatus.FAILED
    rec.finished = dt.datetime.now().isoformat(timespec="seconds")
    rec.elapsed_s = round(result.elapsed_s, 2)
    rec.returncode = result.returncode
    rec.input_hash = case.input_hash()
    rec.model_binary = str(EXE)
    rec.max_substeps = p.max_substeps
    rec.worst_balance_error = p.worst_relative_error
    rec.outputs = {f.name: f.stat().st_size for f in case.output_files()}
    case.save_record(rec)

    # ---------------------------------------------------------------- 6. gauges
    banner("6. GAUGES")
    snapped = [snap_gauge(g, rmap) for g in GAUGES]
    q = outs["outflw"]
    print(f"{'gauge':24s} {'area km2':>10s} {'matched':>10s} {'mean':>9s} {'peak':>9s}  match")
    print("-" * 78)
    for g in snapped:
        if not g.snapped:
            print(f"{g.name:24s}  -- not on the river network --")
            continue
        h = extract_hydrograph(q, g)
        print(f"{g.name:24s} {g.area_km2 or 0:10,.0f} {g.matched_area_km2:10,.0f} "
              f"{h.mean:9.1f} {h.peak:9.1f}  {g.match_quality}")

    (proj.root / "analysis").mkdir(parents=True, exist_ok=True)
    save_gauges(snapped, proj.root / "analysis" / "gauges.csv")
    print(f"\nwrote {len(snapped)} gauge(s) to {proj.root / 'analysis' / 'gauges.csv'}")

    banner("DONE")
    print(f"project : {proj.root}")
    print(f"case    : {case.name}  ({case.status().value})")
    print("Open it with File -> Open project in the GUI.")
    return 0 if gate4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
