"""Add a real scenario case to the Mekong test project, for scenario comparison.

Turns bifurcation flow off (LPTHOUT=False) against the same domain, forcing and period as
``baseline`` -- "essential for deltas and anabranching rivers" per the plan (03-gui-design.md),
so this is a real, physically meaningful difference, not a synthetic one. Everything else is
copied from the baseline case's own namelist, so the two cases differ in exactly one switch.

    python examples/make_mekong_scenario.py [--name no_bifurcation] [--threads 4]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camafloodcast.adapters.camaflood import CamaFloodAdapter
from camafloodcast.core import RiverMap, read_params
from camafloodcast.core.analysis import Gauge, extract_hydrograph, snap_gauge
from camafloodcast.core.namelist import Namelist
from camafloodcast.core.output import OutputSet
from camafloodcast.core.project import CaseStatus, Project
from camafloodcast.core.runner import ModelRunner

PKG = Path(__file__).resolve().parents[2]
MAP_DIR = PKG / "map" / "mekong_15min"
EXE = PKG / "src" / ("MAIN_cmf.exe" if sys.platform == "win32" else "MAIN_cmf")
PROJECT_ROOT = Path(__file__).resolve().parents[1] / "projects" / "mekong"

GAUGES = [
    Gauge("Mekong @ Kratie", 106.02, 12.48, 646000),
    Gauge("Mekong @ Pakse", 105.80, 15.12, 545000),
]


def banner(text: str) -> None:
    print()
    print("=" * 74)
    print(f" {text}")
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="no_bifurcation")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    if not PROJECT_ROOT.exists():
        print(f"run examples/make_mekong_project.py first: {PROJECT_ROOT} does not exist")
        return 1
    proj = Project.open(PROJECT_ROOT)
    baseline = proj.case("baseline")
    if not baseline.exists():
        print("baseline case has no namelist -- run make_mekong_project.py first")
        return 1

    banner(f"1. SCENARIO -- {args.name} (LPTHOUT=False vs baseline's LPTHOUT=True)")
    nml = Namelist.load(baseline.namelist_path)
    nml.set("NRUNVER", "LPTHOUT", False)
    nml.set("NOUTPUT", "COUTTAG", args.name)

    if proj.root.joinpath("cases", args.name).exists():
        import shutil
        shutil.rmtree(proj.root / "cases" / args.name)
    case = proj.add_case(args.name, namelist=nml)
    print(f"created case '{case.name}' at {case.dir}")

    adapter = CamaFloodAdapter()
    diags = adapter.validate(case.namelist(), base_dir=case.dir)
    print(f"Gate 3: {diags.summary()} -> {'PASS' if diags.ok else 'BLOCKED'}")
    if not diags.ok:
        for d in diags:
            print(f"   {d}")
        return 1

    banner("2. RUN")
    runner = ModelRunner(EXE, threads=args.threads,
                         extra_path=Path(r"C:\msys64\ucrt64\bin") if sys.platform == "win32" else None)
    rec = case.record()
    rec.status = CaseStatus.RUNNING
    rec.started = dt.datetime.now().isoformat(timespec="seconds")
    case.save_record(rec)

    result = runner.run(case.dir)
    print(f"exit {result.returncode} ({result.kind.value}) in {result.elapsed_s:.1f} s")
    p = result.progress
    balance_ok = p.worst_relative_error < 1e-9
    print(f"worst relative water-balance error: {p.worst_relative_error:.3e}  (limit 1e-9)")

    grid = read_params(MAP_DIR / "params.txt")
    outs = OutputSet(case.dir, grid, tag=args.name)
    finite_ok = True
    for var in outs.variables:
        f = outs[var]
        bad, lo, hi = f.check_finite(mask=RiverMap(MAP_DIR).land_mask)
        finite_ok &= bad == 0
        print(f"   {var:8s} {f.nrec:3d} rec  min={lo:12.4g} max={hi:12.4g}"
              + ("" if bad == 0 else "   <-- NON-FINITE"))

    gate4 = result.ok and balance_ok and finite_ok
    print(f"Gate 4: {'PASS' if gate4 else 'FAIL'}")

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

    banner("3. SCENARIO VS BASELINE, AT THE SAME GAUGES")
    base_grid = read_params(MAP_DIR / "params.txt")
    base_outs = OutputSet(baseline.dir, base_grid, tag="mekong")
    rmap = RiverMap(MAP_DIR)
    print(f"{'gauge':24s} {'baseline mean':>14s} {'scenario mean':>14s} {'baseline peak':>14s} "
         f"{'scenario peak':>14s}")
    print("-" * 92)
    for g in GAUGES:
        snapped = snap_gauge(g, rmap)   # same domain for both cases -> snap once
        hb = extract_hydrograph(base_outs["outflw"], snapped)
        hs = extract_hydrograph(outs["outflw"], snapped)
        print(f"{g.name:24s} {hb.mean:14.1f} {hs.mean:14.1f} {hb.peak:14.1f} {hs.peak:14.1f}")

    banner("DONE")
    print(f"project : {proj.root}")
    print(f"cases   : baseline (LPTHOUT=True), {case.name} (LPTHOUT=False)")
    print("Open the project and use the Analysis tab to compare them.")
    return 0 if gate4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
