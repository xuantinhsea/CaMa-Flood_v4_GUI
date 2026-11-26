"""Runoff forcing: import, inspect, convert, and audit coverage.

**CaMa-Flood consumes runoff, not rainfall.** It performs no runoff generation; the forcing is
a runoff flux field from an external land-surface model (ERA5-Land, E2O/eartH2Observe, ISIMIP,
GLDAS, or a coupled LSM). So the problems here are not gauge interpolation and radar bias --
they are units, grid alignment, and gaps.

Three failure modes this module exists to catch, all of which otherwise produce a plausible
hydrograph and exit code 0:

* **Wrong units.** ``DROFUNIT`` divides the raw value to reach m/s before multiplying by cell
  area. mm/day needs 86400000; mm/s needs 1000. Getting it wrong by 86400 looks like a
  calibration problem, not a units problem.
* **Missing days.** A gap in the series produces a smooth, believable recession.
* **Flipped latitude.** Products stored south-to-north loaded as north-to-south put the
  Mekong's runoff over Siberia. The domain still fills, just from the wrong place.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

import numpy as np

from .errors import DiagnosticList, FileFormatError
from .maps import Grid

LatOrder = Literal["NtoS", "StoN"]

#: DROFUNIT divisors by source unit. value / DROFUNIT -> m/s, then x cell area -> m3/s.
UNIT_DIVISOR: dict[str, float] = {
    "mm/day": 86_400_000.0,
    "mm/hr": 3_600_000.0,
    "mm/s": 1000.0,
    "kg/m2/s": 1000.0,        # the CF spelling of mm/s for water
    "m/s": 1.0,
    "m/day": 86_400.0,
}

#: Plausible global-mean land runoff, used only to sanity-check units.
PLAUSIBLE_MM_DAY = (0.05, 40.0)


@dataclass
class ForcingSpec:
    """How to find and interpret a runoff dataset."""

    directory: Path
    prefix: str = "Roff____"
    suffix: str = ".one"
    nx: int = 360
    ny: int = 180
    unit: str = "mm/day"
    lat_order: LatOrder = "NtoS"
    big_endian: bool = False
    dtype: str = "<f4"
    #: Geographic extent of the forcing grid. Defaults to global, which is what every shipped
    #: product uses. Needed to compare cell SIZE with the routing grid -- comparing column
    #: counts is meaningless when the two grids cover different extents -- and to place a
    #: preview field correctly on the map.
    west: float = -180.0
    east: float = 180.0
    south: float = -90.0
    north: float = 90.0

    @property
    def drofunit(self) -> float:
        return UNIT_DIVISOR.get(self.unit, 86_400_000.0)

    @property
    def gsize(self) -> float:
        """Cell size in degrees."""
        return (self.east - self.west) / self.nx if self.nx else 0.0

    def path_for(self, day: dt.date) -> Path:
        return self.directory / f"{self.prefix}{day:%Y%m%d}{self.suffix}"

    @property
    def record_bytes(self) -> int:
        return self.nx * self.ny * np.dtype(self.dtype).itemsize


@dataclass
class ForcingReport:
    """What we found when we looked at the data."""

    spec: ForcingSpec
    n_files: int
    first_day: dt.date | None
    last_day: dt.date | None
    missing: list[dt.date] = field(default_factory=list)
    mean_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    negative_fraction: float = 0.0
    inferred_unit: str | None = None

    @property
    def mean_mm_day(self) -> float | None:
        """Domain-mean runoff converted to mm/day, for a units sanity check."""
        if self.mean_value is None:
            return None
        return self.mean_value * 86_400_000.0 / self.spec.drofunit

    def summary(self) -> str:
        lines = [f"directory : {self.spec.directory}",
                 f"pattern   : {self.spec.prefix}YYYYMMDD{self.spec.suffix}",
                 f"grid      : {self.spec.nx} x {self.spec.ny}  ({self.spec.lat_order})",
                 f"unit      : {self.spec.unit}  (DROFUNIT = {self.spec.drofunit:,.0f})",
                 f"files     : {self.n_files}"]
        if self.first_day and self.last_day:
            lines.append(f"period    : {self.first_day} .. {self.last_day}")
        if self.mean_value is not None:
            lines.append(f"values    : mean {self.mean_value:.4g}, "
                         f"range {self.min_value:.4g} .. {self.max_value:.4g}")
            if self.mean_mm_day is not None:
                lines.append(f"            = {self.mean_mm_day:.2f} mm/day domain mean")
        if self.missing:
            lines.append(f"MISSING   : {len(self.missing)} day(s), first {self.missing[0]}")
        return "\n".join(lines)


_RE_DATE = re.compile(r"(\d{8})")


def scan_directory(spec: ForcingSpec, *, sample: int = 5) -> ForcingReport:
    """List the available days and sample a few files for values.

    Sampling rather than reading everything: a decade of global daily forcing is thousands of
    files, and this runs whenever the user changes the directory.
    """
    d = Path(spec.directory)
    if not d.is_dir():
        raise FileFormatError(d, "not a directory")

    days: list[dt.date] = []
    for p in sorted(d.glob(f"{spec.prefix}*{spec.suffix}")):
        m = _RE_DATE.search(p.name[len(spec.prefix):] if spec.prefix else p.name)
        if not m:
            continue
        try:
            days.append(dt.datetime.strptime(m.group(1), "%Y%m%d").date())
        except ValueError:
            continue
    days.sort()

    report = ForcingReport(spec=spec, n_files=len(days),
                           first_day=days[0] if days else None,
                           last_day=days[-1] if days else None)
    if not days:
        return report

    # Gaps within the available span.
    expected = {days[0] + dt.timedelta(days=i)
                for i in range((days[-1] - days[0]).days + 1)}
    report.missing = sorted(expected - set(days))

    # Sample values.
    step = max(1, len(days) // max(sample, 1))
    vals: list[np.ndarray] = []
    for day in days[::step][:sample]:
        try:
            a = read_day(spec, day)
        except (FileFormatError, OSError):
            continue
        finite = a[np.isfinite(a)]
        if finite.size:
            vals.append(finite.ravel())
    if vals:
        allv = np.concatenate(vals)
        positive = allv[allv > 0]
        report.mean_value = float(positive.mean()) if positive.size else 0.0
        report.min_value = float(allv.min())
        report.max_value = float(allv.max())
        report.negative_fraction = float((allv < 0).sum()) / allv.size
        report.inferred_unit = infer_unit(report.mean_value)
    return report


def infer_unit(mean_value: float) -> str | None:
    """Guess the unit from the magnitude of a domain-mean positive value.

    Only a hint for the UI -- a suggestion the user confirms, never applied silently. Runoff
    means cluster near ~1 mm/day, which in m/s is ~1e-8: the magnitudes are far enough apart to
    be informative.
    """
    if mean_value <= 0:
        return None
    for unit in ("mm/day", "mm/hr", "mm/s", "m/s"):
        mm_day = mean_value * 86_400_000.0 / UNIT_DIVISOR[unit]
        if PLAUSIBLE_MM_DAY[0] <= mm_day <= PLAUSIBLE_MM_DAY[1]:
            return unit
    return None


def read_day(spec: ForcingSpec, day: dt.date) -> np.ndarray:
    """One day's field, size-validated (see risk R3)."""
    p = spec.path_for(day)
    if not p.exists():
        raise FileFormatError(p, "no forcing file for this date")
    size = p.stat().st_size
    if size != spec.record_bytes:
        raise FileFormatError(
            p, f"size {size:,} but the grid implies {spec.record_bytes:,} "
               f"({spec.nx} x {spec.ny} x 4)",
            hint="Check NXIN/NYIN in the diminfo file, or the endianness.")
    dtype = spec.dtype.replace("<", ">") if spec.big_endian else spec.dtype
    a = np.fromfile(p, dtype=dtype).reshape(spec.ny, spec.nx)
    return a[::-1] if spec.lat_order == "StoN" else a


def audit(spec: ForcingSpec, start: dt.date, end: dt.date, *,
          report: ForcingReport | None = None) -> DiagnosticList:
    """Gate 2. Does this forcing actually cover the simulation, in sane units?"""
    d = DiagnosticList()
    rep = report or scan_directory(spec)

    if rep.n_files == 0:
        d.error("FRC_EMPTY", f"no forcing files matching "
                             f"{spec.prefix}YYYYMMDD{spec.suffix} in {spec.directory}",
                hint="Check CROFDIR, CROFPRE and CROFSUF. CaMa-Flood needs RUNOFF, not rainfall.")
        return d

    # Coverage of the simulated period.
    needed = [start + dt.timedelta(days=i) for i in range((end - start).days)]
    have = set()
    if rep.first_day and rep.last_day:
        have = {rep.first_day + dt.timedelta(days=i)
                for i in range((rep.last_day - rep.first_day).days + 1)} - set(rep.missing)
    gaps = [day for day in needed if day not in have]

    if gaps:
        shown = ", ".join(str(g) for g in gaps[:5])
        more = f" (and {len(gaps) - 5} more)" if len(gaps) > 5 else ""
        d.error("FRC_COVERAGE",
                f"{len(gaps)} simulated day(s) have no forcing: {shown}{more}",
                hint="A missing day does not stop the model -- it produces a plausible but "
                     "wrong recession. Extend the forcing or shorten the simulation period.")
    else:
        d.info("FRC_COVERAGE_OK",
               f"forcing covers the whole period ({len(needed)} days)")

    if rep.missing:
        d.warning("FRC_INTERNAL_GAPS",
                  f"{len(rep.missing)} gap(s) inside the available span "
                  f"({rep.first_day} .. {rep.last_day})")

    # Units.
    mm_day = rep.mean_mm_day
    if mm_day is not None:
        if not (PLAUSIBLE_MM_DAY[0] <= mm_day <= PLAUSIBLE_MM_DAY[1]):
            hint = (f"With unit '{spec.unit}' (DROFUNIT = {spec.drofunit:,.0f}) the domain mean "
                    f"is {mm_day:.3g} mm/day, which is not plausible for runoff.")
            if rep.inferred_unit and rep.inferred_unit != spec.unit:
                hint += f" The magnitudes suggest '{rep.inferred_unit}'."
            d.error("FRC_UNITS", f"implied mean runoff of {mm_day:.3g} mm/day is implausible",
                    hint=hint)
        else:
            d.info("FRC_UNITS_OK", f"domain-mean runoff {mm_day:.2f} mm/day looks plausible")

    if rep.negative_fraction > 0.01:
        d.warning("FRC_NEGATIVE",
                  f"{rep.negative_fraction * 100:.1f}% of sampled values are negative",
                  hint="CaMa-Flood clamps negative runoff to zero, so the water is simply lost. "
                       "Check whether this is a fill value being read as data.")

    return d


def interpolation_advice(spec: ForcingSpec, grid: Grid) -> DiagnosticList:
    """Interpolated (``inpmat``) or direct (``CONV_RESOL``)?

    When the forcing grid equals the routing grid, nearest-point is exact and needs no matrix.
    Otherwise the mass-conservative input matrix is required -- it is an area-overlap table
    built from the 1-arcmin sub-grid, not a resample, and must be rebuilt whenever either grid
    changes.
    """
    d = DiagnosticList()
    if (spec.nx, spec.ny) == (grid.nx, grid.ny):
        d.info("FRC_DIRECT_OK",
               "the forcing grid matches the routing grid, so LINTERP=.FALSE. is exact "
               "and no input matrix is needed")
        return d

    # Compare cell SIZE, not column count: the grids cover different extents.
    fg, rg = spec.gsize, grid.gsize
    if fg > 0 and rg > 0:
        factor = fg / rg
        how = (f"{factor:.1f}x coarser" if factor > 1.05 else
               f"{1/factor:.1f}x finer" if factor < 0.95 else "the same resolution as")
        detail = f"forcing is {fg:g} deg, routing is {rg:g} deg ({how} the routing grid)"
    else:
        detail = f"forcing is {spec.nx}x{spec.ny}, routing is {grid.nx}x{grid.ny}"

    d.info("FRC_NEEDS_INPMAT",
           f"{detail} -- LINTERP=.TRUE. with an input matrix is required",
           hint="The input matrix is a mass-conservative area-overlap table built from the "
                "1-arcmin sub-grid, not a resample. Rebuild it whenever the domain or the "
                "forcing grid changes.")
    return d


# --------------------------------------------------------------------------- netCDF import


def inspect_netcdf(path: Path | str, variable: str | None = None) -> dict:
    """Describe a netCDF runoff file without committing to reading it.

    Returns grid shape, latitude order, units and time span. netCDF4 is an optional extra, so
    this raises a clear error rather than failing at import time.
    """
    try:
        import netCDF4 as nc
    except ImportError as exc:                                    # pragma: no cover
        raise FileFormatError(path, "reading netCDF needs the 'netcdf' extra: "
                                    "pip install camafloodcast[netcdf]") from exc

    p = Path(path)
    ds = nc.Dataset(p)
    try:
        if variable is None:
            candidates = [v for v in ds.variables
                          if ds.variables[v].ndim == 3 and v.lower() not in ("time",)]
            if not candidates:
                raise FileFormatError(p, "no 3-D variable found in this file")
            variable = candidates[0]

        var = ds.variables[variable]
        lat = ds.variables["lat"][:] if "lat" in ds.variables else None
        lon = ds.variables["lon"][:] if "lon" in ds.variables else None
        tvar = ds.variables.get("time")

        order: LatOrder = "NtoS"
        if lat is not None and len(lat) > 1 and float(lat[0]) < float(lat[-1]):
            order = "StoN"

        info = {
            "variable": variable,
            "shape": tuple(int(s) for s in var.shape),
            "nx": int(var.shape[2]),
            "ny": int(var.shape[1]),
            "ntime": int(var.shape[0]),
            "unit": getattr(var, "units", "unknown"),
            "lat_order": order,
            "lat_first": float(lat[0]) if lat is not None else None,
            "lat_last": float(lat[-1]) if lat is not None else None,
            "lon_first": float(lon[0]) if lon is not None else None,
            "lon_last": float(lon[-1]) if lon is not None else None,
            "time_units": getattr(tvar, "units", "") if tvar is not None else "",
        }
        return info
    finally:
        ds.close()


def netcdf_to_binary(path: Path | str, outdir: Path | str, *, grid: Grid,
                     variable: str | None = None, year: int | None = None,
                     prefix: str = "Roff____", suffix: str = ".one",
                     progress=None) -> ForcingSpec:
    """Extract a netCDF runoff product onto a routing grid as plain binary.

    This avoids requiring netCDF-Fortran: the model reads plain binary with ``LINPCDF=.FALSE.``.
    When the product is already on the routing grid (E2O and glb_15min share the 0.25 degree
    grid), the result needs no input matrix at all -- each routing cell gets exactly its own
    forcing cell.
    """
    try:
        import netCDF4 as nc
    except ImportError as exc:                                    # pragma: no cover
        raise FileFormatError(path, "reading netCDF needs the 'netcdf' extra") from exc

    p = Path(path)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    info = inspect_netcdf(p, variable)
    variable = info["variable"]

    ds = nc.Dataset(p)
    try:
        var = ds.variables[variable]
        lat = ds.variables["lat"][:]
        lon = ds.variables["lon"][:]

        gsize = grid.gsize
        # Window the routing domain out of the source grid.
        src_west = float(lon[0]) - gsize / 2
        src_north = float(lat[0]) + gsize / 2
        ix0 = int(round((grid.west - src_west) / gsize))
        iy0 = int(round((src_north - grid.north) / gsize))
        if ix0 < 0 or iy0 < 0 or ix0 + grid.nx > info["nx"] or iy0 + grid.ny > info["ny"]:
            raise FileFormatError(
                p, f"the routing domain is not inside this product "
                   f"(window {ix0},{iy0} + {grid.nx}x{grid.ny} vs {info['nx']}x{info['ny']})",
                hint="Many reanalysis products stop at 60S. Check the domain's southern edge.")

        tvar = ds.variables.get("time")
        base = dt.date(year or 2000, 1, 1)
        if tvar is not None and getattr(tvar, "units", "").startswith("days since"):
            try:
                base = dt.datetime.strptime(
                    tvar.units.split("since")[1].strip()[:10], "%Y-%m-%d").date()
            except ValueError:
                pass

        n = info["ntime"]
        for it in range(n):
            block = var[it, iy0:iy0 + grid.ny, ix0:ix0 + grid.nx]
            arr = np.ma.filled(np.ma.masked_invalid(block), 0.0).astype("<f4")
            np.maximum(arr, 0.0, out=arr)
            day = base + dt.timedelta(days=it)
            arr.tofile(out / f"{prefix}{day:%Y%m%d}{suffix}")
            if progress and it % 20 == 0:
                progress(it, n)
        if progress:
            progress(n, n)
    finally:
        ds.close()

    unit = str(info["unit"]).replace(" ", "")
    unit = {"mmday-1": "mm/day", "mm/day": "mm/day", "kgm-2s-1": "kg/m2/s",
            "mms-1": "mm/s"}.get(unit, "mm/day")

    # The extracted files are exactly the routing grid's window, not the source product's
    # global extent -- carry the true bounds so a later size/preview comparison is honest.
    return ForcingSpec(directory=out, prefix=prefix, suffix=suffix,
                       nx=grid.nx, ny=grid.ny, unit=unit, lat_order="NtoS",
                       west=grid.west, east=grid.east, south=grid.south, north=grid.north)


def write_diminfo(path: Path | str, grid: Grid, spec: ForcingSpec,
                  *, inpmat: str = "NONE", inpn: int = 1) -> Path:
    """Write a diminfo file. Note the edge order: west, east, **north, south**."""
    p = Path(path)
    p.write_text(
        f"{grid.nx:>10d}     !! nXX\n"
        f"{grid.ny:>10d}     !! nYY\n"
        f"{grid.nlfp:>10d}     !! floodplain layer\n"
        f"{spec.nx:>10d}     !! input nXX\n"
        f"{spec.ny:>10d}     !! input nYY\n"
        f"{inpn:>10d}     !! input num\n"
        f"{inpmat}\n"
        f"{grid.west:>12.3f}     !! west  edge\n"
        f"{grid.east:>12.3f}     !! east  edge\n"
        f"{grid.north:>12.3f}     !! north edge\n"
        f"{grid.south:>12.3f}     !! south edge\n",
        encoding="utf-8")
    return p
