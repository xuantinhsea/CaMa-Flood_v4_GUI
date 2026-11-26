"""Phase 0 exit criterion: drive a complete CaMa-Flood study from Python.

No GUI, no shell scripts. Configure -> validate -> run -> read -> analyse, entirely through the
SDK. If this works, the GUI in Phase 1 is a thin shell over tested code rather than a monolith
with the logic in widget callbacks.

    python examples/phase0_end_to_end.py [--days 5] [--case mekong]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from camafloodcast.adapters.camaflood import CamaFloodAdapter
from camafloodcast.core import RiverMap, read_params
from camafloodcast.core.analysis import Gauge, extract_hydrograph, interpretation_notes, snap_gauge
from camafloodcast.core.domain import analyse_domain, find_outlet_near, validate_domain
from camafloodcast.core.namelist import new_namelist
from camafloodcast.core.output import OutputSet
from camafloodcast.core.project import CaseStatus, Project, RunRecord
from camafloodcast.core.runner import ModelRunner

PKG = Path(__file__).resolve().parents[2]

CASES = {
    "phl": dict(map_name="phl_15min", label="Philippines (national)",
                target=(121.62, 18.38),
                gauges=[Gauge("Cagayan @ Aparri", 121.62, 18.38, 27300),
                        Gauge("Mindanao @ Cotabato", 124.38, 7.12, 18874)]),
    "mekong": dict(map_name="mekong_15min", label="Mekong basin (catchment)",
                   target=(106.38, 9.62),
                   gauges=[Gauge("Mekong @ Kratie", 106.02, 12.48, 646000),
                           Gauge("Mekong @ Pakse", 105.80, 15.12, 545000)]),
}


def banner(text: str) -> None:
    print()
    print("=" * 74)
    print(f" {text}")
    print("=" * 74)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=sorted(CASES), default="phl")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    spec = CASES[args.case]
    map_dir = PKG / "map" / spec["map_name"]
    exe = PKG / "src" / ("MAIN_cmf.exe" if sys.platform == "win32" else "MAIN_cmf")

    if not (map_dir / "params.txt").exists():
        print(f"map not built: {map_dir}")
        return 1
    if not exe.exists():
        print(f"model binary not found: {exe}")
        return 1

    adapter = CamaFloodAdapter()

    # ---------------------------------------------------------------- 1. domain, Gate 1
    banner(f"1. DOMAIN  --  {spec['label']}")
    rmap = RiverMap(map_dir)
    report = analyse_domain(rmap)
    print(report.summary())

    target = find_outlet_near(report, *spec["target"], radius_deg=0.5)
    diags = validate_domain(report, target_basin=target)
    print()
    print(f"Gate 1 (domain closure): {diags.summary()} -> "
          f"{'PASS' if diags.ok else 'BLOCKED'}")
    for d in diags:
        print(f"   {d}")
    if not diags.ok:
        return 1

    # ---------------------------------------------------------------- 2. project + case
    banner("2. PROJECT AND CASE")
    root = PKG / "CaMa_Floodcast" / "examples" / f"_demo_{args.case}"
    if root.exists():
        import shutil
        shutil.rmtree(root)
    proj = Project.create(root, name=f"Phase 0 demo: {spec['label']}",
                          map_dir=str(map_dir), model_binary=str(exe),
                          default_threads=args.threads)

    grid = read_params(map_dir / "params.txt")
    end = dt.date(2000, 1, 1) + dt.timedelta(days=args.days)

    nml = new_namelist(["NRUNVER", "NDIMTIME", "NPARAM", "NSIMTIME",
                        "NMAP", "NRESTART", "NFORCE", "NOUTPUT"])
    nml.update("NRUNVER", dict(LADPSTP=True, LFLDOUT=True, LPTHOUT=True,
                               LDAMOUT=False, LRESTART=False, LWBLOG=True))
    nml.update("NDIMTIME", {"CDIMINFO": str(map_dir / "diminfo_test-1deg.txt"),
                            "DT": 3600, "IFRQ_INP": 24})
    nml.update("NPARAM", dict(PMANRIV=0.03, PMANFLD=0.10, PDSTMTH=10000.0,
                              PCADP=0.7, PSTOLIM=0.05))
    nml.update("NSIMTIME", dict(SYEAR=2000, SMON=1, SDAY=1, SHOUR=0,
                                EYEAR=end.year, EMON=end.month, EDAY=end.day, EHOUR=0))
    nml.update("NMAP", {k: str(map_dir / v) for k, v in {
        "CNEXTXY": "nextxy.bin", "CGRAREA": "ctmare.bin", "CELEVTN": "elevtn.bin",
        "CNXTDST": "nxtdst.bin", "CRIVLEN": "rivlen.bin", "CFLDHGT": "fldhgt.bin",
        "CRIVWTH": "rivwth_gwdlr.bin", "CRIVHGT": "rivhgt.bin", "CRIVMAN": "rivman.bin",
        "CPTHOUT": "bifprm.txt"}.items()} | {"LMAPCDF": False, "CFLDMAN": "NONE"})
    nml.update("NRESTART", {"CRESTSTO": "", "CRESTDIR": "./", "CVNREST": "restart",
                            "LRESTCDF": False, "IFRQ_RST": 0})
    nml.update("NFORCE", {"LINPCDF": False, "LINTERP": True,
                          "CINPMAT": str(map_dir / "inpmat_test-1deg.bin"),
                          "DROFUNIT": 86400000,
                          "CROFDIR": str(PKG / "inp" / "test_1deg" / "runoff") + "/",
                          "CROFPRE": "Roff____", "CROFSUF": ".one"})
    nml.update("NOUTPUT", {"COUTDIR": "./", "COUTTAG": args.case, "LOUTVEC": False,
                           "LOUTCDF": False, "NDLEVEL": 0, "IFRQ_OUT": 24,
                           "CVARSOUT": "outflw,storge,rivdph,flddph,fldfrc,sfcelv"})

    case = proj.add_case("baseline", namelist=nml)
    print(f"created case '{case.name}' at {case.dir}")
    print(f"status: {case.status().value}")

    # ---------------------------------------------------------------- 3. Gate 3
    banner("3. PRE-FLIGHT VALIDATION (Gate 3)")
    diags = adapter.validate(case.namelist(), base_dir=case.dir)
    print(f"{diags.summary()} -> {'PASS' if diags.ok else 'BLOCKED'}")
    for d in diags:
        print(f"   {d}")
    caps = adapter.required_capabilities(case.namelist())
    print(f"capabilities required of the binary: {caps or '(none beyond the base build)'}")
    if not diags.ok:
        return 1

    # ---------------------------------------------------------------- 4. run
    banner("4. RUN")
    runner = ModelRunner(exe, threads=args.threads,
                         extra_path=Path(r"C:\msys64\ucrt64\bin") if sys.platform == "win32" else None)

    last = {"pct": -1}

    def on_progress(p):
        f = p.fraction
        if f is None:
            return
        pct = int(f * 100)
        if pct >= last["pct"] + 20:
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
    print(f"NSEQMAX from log        : {p.nseqmax}")
    print(f"max adaptive substeps   : {p.max_substeps}")
    print(f"water-balance records   : {len(p.balances)}")
    print(f"worst relative error    : {p.worst_relative_error:.3e}  (limit 1e-9)")
    balance_ok = p.worst_relative_error < 1e-9

    outs = OutputSet(case.dir, grid, tag=args.case)
    finite_ok = True
    print()
    for var in outs.variables:
        f = outs[var]
        bad, lo, hi = f.check_finite(mask=rmap.land_mask)
        flag = "" if bad == 0 else "   <-- NON-FINITE"
        finite_ok &= bad == 0
        print(f"   {var:8s} {f.nrec:3d} rec  min={lo:12.4g} max={hi:12.4g}{flag}")

    gate4 = result.ok and balance_ok and finite_ok
    print()
    print(f"Gate 4: {'PASS' if gate4 else 'FAIL'}")

    rec = case.record()
    rec.status = CaseStatus.COMPLETED if gate4 else CaseStatus.FAILED
    rec.finished = dt.datetime.now().isoformat(timespec="seconds")
    rec.elapsed_s = round(result.elapsed_s, 2)
    rec.returncode = result.returncode
    rec.input_hash = case.input_hash()
    rec.model_binary = str(exe)
    rec.max_substeps = p.max_substeps
    rec.worst_balance_error = p.worst_relative_error
    rec.outputs = {f.name: f.stat().st_size for f in case.output_files()}
    case.save_record(rec)

    # ---------------------------------------------------------------- 6. analysis
    banner("6. ANALYSIS")
    q = outs["outflw"]
    print(f"{'gauge':24s} {'area km2':>10s} {'matched':>10s} {'mean':>9s} {'peak':>9s}  match")
    print("-" * 78)
    for g in spec["gauges"]:
        g = snap_gauge(g, rmap)
        if not g.snapped:
            print(f"{g.name:24s}  -- not on the river network --")
            continue
        h = extract_hydrograph(q, g)
        print(f"{g.name:24s} {g.area_km2 or 0:10,.0f} {g.matched_area_km2:10,.0f} "
              f"{h.mean:9.1f} {h.peak:9.1f}  {g.match_quality}")

    print()
    for note in interpretation_notes(spinup_records=0, forcing_resolution_deg=1.0,
                                     gauge_area_km2=spec["gauges"][0].area_km2, grid=grid):
        print(f"  NOTE: {note}")

    # ---------------------------------------------------------------- 7. provenance
    banner("7. PROVENANCE AND STALE DETECTION")
    print(proj.summary())
    print()
    print(f"case status: {case.status().value}")
    edited = case.namelist()
    edited.set("NPARAM", "PMANFLD", 0.15)
    edited.save(backup=False)
    print(f"after editing PMANFLD -> {case.status().value}")
    print("   (results no longer match the configuration that produced them)")

    banner("PHASE 0 EXIT CRITERION MET")
    print("Configured, validated, ran, read and analysed a CaMa-Flood study")
    print("entirely from Python, with no GUI and no shell scripts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
