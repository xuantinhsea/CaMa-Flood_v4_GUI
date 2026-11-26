"""Add a real reservoir-operation scenario to the Mekong test project.

Uses five real, named dams on the Lancang (the Chinese/upper name for the Mekong mainstem):
Wunonglong, Lidi, Huangdeng, Dahuaqiao, Miaowei -- coordinates and capacity (CAP_MCM) from
``map/data/GRanD_alloc+Mekong.csv``, the GRanD database rows appended for this package but never
allocated to any built map (``ix,iy = -99``). This script allocates them onto ``mekong_15min``
by drainage-area snapping (the same method as a gauge, verified: all five match published
drainage area to within 0.3%), and derives Qn/Qf -- the model's normal/flood discharge
parameters -- from the *real*, already-simulated baseline discharge at those cells, rather than
assuming values: Qn is the annual mean, Qf the 90th percentile, both real numbers from
``projects/mekong/cases/baseline``.

    python examples/make_mekong_dams.py [--threads 4]
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
from camafloodcast.core.dams import DamRecord, inflow_stats, write_dam_file
from camafloodcast.core.namelist import Namelist
from camafloodcast.core.output import OutputFile, OutputSet
from camafloodcast.core.project import CaseStatus, Project
from camafloodcast.core.runner import ModelRunner

PKG = Path(__file__).resolve().parents[2]
MAP_DIR = PKG / "map" / "mekong_15min"
EXE = PKG / "src" / ("MAIN_cmf.exe" if sys.platform == "win32" else "MAIN_cmf")
PROJECT_ROOT = Path(__file__).resolve().parents[1] / "projects" / "mekong"

# id, name, lat, lon, drainage area [km2] (area_MERIT), total capacity [Mm3] (CAP_MCM) --
# map/data/GRanD_alloc+Mekong.csv, rows 7322-7326.
GRAND_LANCANG = [
    (10001, "Wunonglong", 27.939, 98.924, 79787.0, 190.4),
    (10002, "Lidi", 27.848, 99.031, 80088.867, 49.7),
    (10003, "Huangdeng", 26.559, 99.116, 85886.0, 992.6),
    (10004, "Dahuaqiao", 26.341, 99.143, 86353.102, 205.1),
    (10005, "Miaowei", 25.854, 99.161, 87596.398, 462.0),
]

# GRanD's own typical conservation/flood-control split when a dam-specific value is not
# published (p04_complete_damcsv.py's fallback, per the source investigation for this script).
CONVOL_FRACTION = 0.63

DOWNSTREAM_GAUGES = [
    Gauge("Mekong @ Chiang Saen", 100.08, 20.27, 189000),  # first gauge below all 5 dams
    Gauge("Mekong @ Kratie", 106.02, 12.48, 646000),
]


def banner(text: str) -> None:
    print()
    print("=" * 74)
    print(f" {text}")
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="with_dams")
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

    banner("1. ALLOCATE -- snap 5 real Lancang dams onto the Mekong grid")
    rmap = RiverMap(MAP_DIR)
    grid = read_params(MAP_DIR / "params.txt")
    baseline_outflw = OutputFile(baseline.dir / "outflwmekong.bin", grid)

    records: list[DamRecord] = []
    for dam_id, name, lat, lon, area_km2, cap_mcm in GRAND_LANCANG:
        g = snap_gauge(Gauge(name, lon, lat, area_km2=area_km2), rmap, radius_cells=3)
        if not g.snapped:
            print(f"  {name}: could not snap to the grid -- skipped")
            continue
        qn, qf = inflow_stats(baseline_outflw, g.ix, g.iy, flood_percentile=90.0)
        convol = cap_mcm * CONVOL_FRACTION
        fldvol = cap_mcm - convol
        print(f"  {name:12s} ({lat:.3f}N {lon:.3f}E)  ix,iy=({g.ix},{g.iy})  "
              f"area match {g.match_quality}  Qn={qn:,.1f} Qf={qf:,.1f} m3/s  "
              f"ConVol={convol:,.1f} FldVol={fldvol:,.1f} Mm3")
        records.append(DamRecord(
            dam_id=dam_id, name=name, lat=lat, lon=lon, upreal_km2=area_km2,
            ix=g.ix, iy=g.iy, fldvol_mcm=fldvol, convol_mcm=convol, totvol_mcm=cap_mcm,
            qn=qn, qf=qf))

    if not records:
        print("no dams could be allocated -- nothing to run")
        return 1

    dam_path = proj.root / "dam_params.csv"
    write_dam_file(records, dam_path, river_map=rmap)     # validated against the real grid
    print(f"\nwrote {len(records)} dam(s) to {dam_path}")

    banner(f"2. SCENARIO -- {args.name} (LDAMOUT=True vs baseline's LDAMOUT=False)")
    nml = Namelist.load(baseline.namelist_path)
    nml.set("NRUNVER", "LDAMOUT", True)
    nml.set("NDAMOUT", "CDAMFILE", str(dam_path))
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

    banner("3. RUN")
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

    outs = OutputSet(case.dir, grid, tag=args.name)
    finite_ok = True
    for var in outs.variables:
        f = outs[var]
        bad, lo, hi = f.check_finite(mask=rmap.land_mask)
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
    if not gate4:
        return 1

    banner("4. RESERVOIRS VS BASELINE, DOWNSTREAM")
    base_outs = OutputSet(baseline.dir, grid, tag="mekong")
    print(f"{'gauge':24s} {'baseline mean':>14s} {'with-dams mean':>15s} "
         f"{'baseline peak':>14s} {'with-dams peak':>15s}")
    print("-" * 92)
    for g in DOWNSTREAM_GAUGES:
        snapped = snap_gauge(g, rmap)
        hb = extract_hydrograph(base_outs["outflw"], snapped)
        hd = extract_hydrograph(outs["outflw"], snapped)
        print(f"{g.name:24s} {hb.mean:14.1f} {hd.mean:15.1f} {hb.peak:14.1f} {hd.peak:15.1f}")

    banner("DONE")
    print(f"project : {proj.root}")
    print(f"cases   : baseline (LDAMOUT=False), {case.name} (LDAMOUT=True, 5 real Lancang dams)")
    print("Open the project and use the Analysis tab to compare them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
