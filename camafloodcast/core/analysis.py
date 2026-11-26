"""Gauge extraction, hydrographs and performance metrics.

Two things here are less obvious than they look.

**Gauge snapping.** A gauge's published coordinates rarely land on the intended unit catchment
at 0.25 deg -- a cell is ~28 km across, and the river may be in the neighbouring one. Snapping
by *drainage area* rather than by distance is far more reliable, because upstream area is the
quantity that identifies a point on a river network. Measured matches on the shipped 15-min map:
Kratie 646 676 vs 646 000 km2 published, Chiang Saen 190 369 vs 189 000, Cagayan 27 630 vs
~27 300.

**Interpretation caveats.** The metrics below are only meaningful on a run that is spun up and
forced at an adequate resolution. Both conditions are checkable, so the functions return them
alongside the numbers rather than leaving the user to remember.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .maps import Grid, RiverMap
from .output import OutputFile


# --------------------------------------------------------------------------- gauges


@dataclass
class Gauge:
    """A point of interest on the river network."""

    name: str
    lon: float
    lat: float
    #: Published drainage area [km2], used for snapping and for confirming the match.
    area_km2: float | None = None
    observed: Path | None = None
    notes: str = ""

    # filled in by snap()
    ix: int | None = None
    iy: int | None = None
    matched_area_km2: float | None = None

    @property
    def snapped(self) -> bool:
        return self.ix is not None and self.iy is not None

    @property
    def area_error(self) -> float | None:
        if self.area_km2 and self.matched_area_km2:
            return abs(self.matched_area_km2 - self.area_km2) / self.area_km2
        return None

    @property
    def match_quality(self) -> str:
        e = self.area_error
        if e is None:
            return "unverified (no published area given)"
        if e < 0.05:
            return f"good ({e * 100:.1f}% area difference)"
        if e < 0.20:
            return f"fair ({e * 100:.1f}% area difference)"
        return f"POOR ({e * 100:.1f}% area difference) -- check the coordinates"


def snap_gauge(gauge: Gauge, river_map: RiverMap, *, radius_cells: int = 2) -> Gauge:
    """Place a gauge on the river network.

    With a published area, pick the cell within ``radius_cells`` whose upstream area is closest
    to it. Without one, fall back to the largest-drainage river cell nearby -- which is usually
    the mainstem rather than a tributary, and is the more useful default.
    """
    grid = river_map.grid
    cell = grid.cell_of(gauge.lon, gauge.lat)
    if cell is None:
        return gauge

    ix0, iy0 = cell
    up = river_map.layer("uparea")
    land = river_map.land_mask

    best: tuple[float, int, int] | None = None
    for dy in range(-radius_cells, radius_cells + 1):
        for dx in range(-radius_cells, radius_cells + 1):
            ix, iy = ix0 + dx, iy0 + dy
            if not (0 <= ix < grid.nx and 0 <= iy < grid.ny) or not land[iy, ix]:
                continue
            a = float(up[iy, ix]) / 1e6
            score = (abs(a - gauge.area_km2) / max(gauge.area_km2, 1.0)
                     if gauge.area_km2 else -a)
            if best is None or score < best[0]:
                best = (score, ix, iy)

    if best is None:
        return gauge
    _, ix, iy = best
    gauge.ix, gauge.iy = ix, iy
    gauge.matched_area_km2 = float(up[iy, ix]) / 1e6
    return gauge


def load_gauges(path: Path | str) -> list[Gauge]:
    """Read a gauge CSV: ``name,lon,lat[,area_km2][,observed]``."""
    import csv

    out: list[Gauge] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not low.get("name"):
                continue
            area = low.get("area_km2") or low.get("area")
            obs = low.get("observed") or low.get("obs")
            out.append(Gauge(
                name=low["name"],
                lon=float(low["lon"]),
                lat=float(low["lat"]),
                area_km2=float(area) if area else None,
                observed=Path(obs) if obs else None,
            ))
    return out


def save_gauges(gauges: list[Gauge], path: Path | str) -> None:
    import csv

    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "lon", "lat", "area_km2", "observed",
                    "matched_ix", "matched_iy", "matched_area_km2"])
        for g in gauges:
            w.writerow([g.name, g.lon, g.lat, g.area_km2 or "", g.observed or "",
                        g.ix if g.ix is not None else "",
                        g.iy if g.iy is not None else "",
                        f"{g.matched_area_km2:.1f}" if g.matched_area_km2 else ""])


# --------------------------------------------------------------------------- hydrographs


@dataclass
class Hydrograph:
    gauge: Gauge
    variable: str
    unit: str
    values: np.ndarray
    dates: list[dt.datetime] | None = None
    #: records treated as spin-up and excluded from statistics
    spinup_records: int = 0

    @property
    def analysed(self) -> np.ndarray:
        return self.values[self.spinup_records:]

    @property
    def mean(self) -> float:
        return float(np.nanmean(self.analysed)) if self.analysed.size else float("nan")

    @property
    def peak(self) -> float:
        return float(np.nanmax(self.analysed)) if self.analysed.size else float("nan")

    @property
    def peak_record(self) -> int:
        return int(np.nanargmax(self.analysed)) + self.spinup_records

    def monthly_means(self) -> list[float]:
        """Rough monthly means, assuming daily records from 1 January."""
        edges = np.cumsum([0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31])
        out = []
        for i in range(12):
            lo, hi = int(edges[i]), int(edges[i + 1])
            if hi <= len(self.values):
                out.append(float(np.nanmean(self.values[lo:hi])))
        return out


def extract_hydrograph(out: OutputFile, gauge: Gauge, *, spinup_records: int = 0,
                       dates: list[dt.datetime] | None = None) -> Hydrograph:
    """Pull a time series at a snapped gauge. Strided read, never a full load."""
    if not gauge.snapped:
        raise ValueError(f"gauge {gauge.name!r} has not been snapped to the grid")
    values = out.series_at(gauge.ix, gauge.iy)
    return Hydrograph(gauge, out.variable, out.unit, values, dates, spinup_records)


# --------------------------------------------------------------------------- levee protection


@dataclass
class LeveeStats:
    """Protected-side overtopping summary from a ``levdph`` series at one point.

    ``levdph`` (see ``core.output.CAVEATS``) is zero at a cell/time until the river side rises
    high enough to spill over the levee into the protected side -- so "overtopped" here means
    "protected-side depth exceeds the threshold", not "the levee failed structurally".
    """

    n: int
    fraction_overtopped: float
    max_depth_m: float
    mean_depth_when_wet_m: float
    first_overtopped_record: int | None

    def summary(self) -> str:
        if self.n == 0:
            return "no records"
        if self.fraction_overtopped == 0:
            return f"never overtopped over {self.n} record(s) -- protected side stayed dry"
        return (f"overtopped {self.fraction_overtopped * 100:.1f}% of {self.n} record(s), "
                f"first at record {self.first_overtopped_record}, "
                f"max depth {self.max_depth_m:.2f} m, "
                f"mean {self.mean_depth_when_wet_m:.2f} m while overtopped")


def levee_protection_stats(levdph: np.ndarray, *, threshold_m: float = 0.01) -> LeveeStats:
    """Overtopping summary for a protected-side depth series (a ``levdph`` hydrograph's values).

    ``threshold_m`` filters out floating-point noise around zero rather than counting it as
    "overtopped" -- CaMa-Flood's own storage-change limiter (``PSTOLIM``) already keeps genuine
    zero-depth records exactly at 0.0, but this stays robust if that ever changes.
    """
    values = np.asarray(levdph, dtype=np.float64)
    n = int(values.size)
    if n == 0:
        return LeveeStats(0, float("nan"), float("nan"), float("nan"), None)
    wet = values > threshold_m
    any_wet = bool(np.any(wet))
    return LeveeStats(
        n=n,
        fraction_overtopped=float(np.count_nonzero(wet)) / n,
        max_depth_m=float(np.max(values)),
        mean_depth_when_wet_m=float(np.mean(values[wet])) if any_wet else 0.0,
        first_overtopped_record=int(np.argmax(wet)) if any_wet else None,
    )


# --------------------------------------------------------------------------- metrics


@dataclass
class Metrics:
    n: int
    nse: float
    kge: float
    pbias: float
    rmse: float
    r: float
    peak_error: float
    mean_sim: float
    mean_obs: float

    def summary(self) -> str:
        return (f"n={self.n}  NSE={self.nse:.3f}  KGE={self.kge:.3f}  "
                f"PBIAS={self.pbias:+.1f}%  RMSE={self.rmse:.1f}  "
                f"peak error={self.peak_error:+.1f}%")

    @property
    def verdict(self) -> str:
        if self.nse > 0.75:
            return "very good"
        if self.nse > 0.65:
            return "good"
        if self.nse > 0.50:
            return "satisfactory"
        return "unsatisfactory"


def compare(sim: np.ndarray, obs: np.ndarray) -> Metrics:
    """Standard hydrological goodness-of-fit on paired series (NaN-aware)."""
    sim = np.asarray(sim, dtype=np.float64)
    obs = np.asarray(obs, dtype=np.float64)
    n = min(sim.size, obs.size)
    sim, obs = sim[:n], obs[:n]
    ok = np.isfinite(sim) & np.isfinite(obs)
    sim, obs = sim[ok], obs[ok]
    if sim.size < 2:
        nan = float("nan")
        return Metrics(0, nan, nan, nan, nan, nan, nan, nan, nan)

    mo, ms = float(np.mean(obs)), float(np.mean(sim))
    denom = float(np.sum((obs - mo) ** 2))
    nse = 1.0 - float(np.sum((sim - obs) ** 2)) / denom if denom > 0 else float("nan")

    sd_o, sd_s = float(np.std(obs)), float(np.std(sim))
    r = float(np.corrcoef(sim, obs)[0, 1]) if sd_o > 0 and sd_s > 0 else float("nan")
    if sd_o > 0 and mo != 0 and np.isfinite(r):
        kge = 1.0 - float(np.sqrt((r - 1) ** 2 + (sd_s / sd_o - 1) ** 2 + (ms / mo - 1) ** 2))
    else:
        kge = float("nan")

    pbias = 100.0 * (ms - mo) / mo if mo != 0 else float("nan")
    rmse = float(np.sqrt(np.mean((sim - obs) ** 2)))
    po, ps = float(np.max(obs)), float(np.max(sim))
    peak_error = 100.0 * (ps - po) / po if po != 0 else float("nan")

    return Metrics(int(sim.size), nse, kge, pbias, rmse, r, peak_error, ms, mo)


# --------------------------------------------------------------------------- scenario comparison


@dataclass
class FieldDiff:
    """A - B for one output variable at one record, over the routing grid."""

    variable: str
    record: int
    diff: np.ndarray            # (ny, nx), A - B, only meaningful inside `mask`
    mean_abs_diff: float
    max_abs_diff: float
    rms_diff: float


def diff_field(a: OutputFile, b: OutputFile, record: int, *,
               mask: np.ndarray | None = None) -> FieldDiff:
    """Compare two cases' output at one record.

    Both files must be on the same routing grid -- diffing outputs from different domains
    would be comparing different things entirely, not scenario variants of the same study.
    """
    if a.grid != b.grid:
        raise ValueError("scenario comparison needs both cases on the same routing grid")
    if a.variable != b.variable:
        raise ValueError(f"comparing different variables: {a.variable!r} vs {b.variable!r}")

    diff = np.asarray(a.record(record), dtype=np.float64) - np.asarray(b.record(record),
                                                                        dtype=np.float64)
    vals = diff[mask] if mask is not None else diff.ravel()
    if vals.size == 0:
        return FieldDiff(a.variable, record, diff, 0.0, 0.0, 0.0)
    return FieldDiff(
        variable=a.variable, record=record, diff=diff,
        mean_abs_diff=float(np.mean(np.abs(vals))),
        max_abs_diff=float(np.max(np.abs(vals))),
        rms_diff=float(np.sqrt(np.mean(vals ** 2))),
    )


# --------------------------------------------------------------------------- longitudinal profiles


def trace_downstream(river_map: RiverMap, ix: int, iy: int, *,
                     max_steps: int = 1_000_000) -> list[tuple[int, int]]:
    """Follow ``nextxy`` downstream from ``(ix, iy)`` to a river mouth or inland sink.

    **``nextxy`` values are 1-indexed (the Fortran convention the file was written in), not
    0-indexed like every numpy array in this codebase.** Verified against the real Mekong map:
    at the Kratie gauge cell (51, 84, uparea 646,676 km2), the raw stored value is (53, 86);
    subtracting 1 gives (52, 85) with uparea 647,989 km2 -- a plausible, monotonically larger
    downstream neighbour. Using the raw value directly lands on (53, 86), uparea 4,130 km2: a
    real, in-bounds, land cell that is simply the wrong one -- exactly the silent, plausible-
    looking-but-wrong failure mode this project exists to catch, not a crash that would announce
    itself.
    """
    nxt = river_map.nextxy
    grid = river_map.grid
    path = [(ix, iy)]
    seen = {(ix, iy)}
    for _ in range(max_steps):
        raw_x, raw_y = int(nxt[0][iy, ix]), int(nxt[1][iy, ix])
        if raw_x <= 0:      # NEXTXY_LAND (-9999), NEXTXY_MOUTH (-9) or NEXTXY_SINK (-10)
            break
        jx, jy = raw_x - 1, raw_y - 1
        if not (0 <= jx < grid.nx and 0 <= jy < grid.ny):
            raise ValueError(f"nextxy at ({ix},{iy}) points outside the grid: ({jx},{jy})")
        if (jx, jy) in seen:
            raise ValueError(f"nextxy loops back on itself at ({jx},{jy})")
        ix, iy = jx, jy
        path.append((ix, iy))
        seen.add((ix, iy))
    return path


@dataclass
class LongitudinalProfile:
    """Water-surface profile along a traced river path, at one record."""

    path: list[tuple[int, int]]     # (ix, iy), in flow order, start to end
    distance_km: np.ndarray         # cumulative distance from the start cell
    elevtn_m: np.ndarray            # channel bank-top elevation (static)
    sfcelv_m: np.ndarray            # water surface elevation, at the requested record
    rivdph_m: np.ndarray | None     # channel depth, at the requested record, if available


def longitudinal_profile(river_map: RiverMap, path: list[tuple[int, int]],
                         sfcelv: OutputFile, record: int, *,
                         rivdph: OutputFile | None = None) -> LongitudinalProfile:
    """Build a profile along an already-traced path (see :func:`trace_downstream`).

    Distance accumulates ``nxtdst`` (the distance to *each* cell's own downstream neighbour),
    so the last cell's distance already reflects the full path length -- it is not, and should
    not be, evaluated at the final (outlet) cell, which typically has no further neighbour.
    """
    if not path:
        raise ValueError("path is empty")
    nxtdst = river_map.layer("nxtdst")
    elevtn = river_map.layer("elevtn")
    xs = np.array([p[0] for p in path])
    ys = np.array([p[1] for p in path])

    dist = np.zeros(len(path))
    for i in range(1, len(path)):
        px, py = path[i - 1]
        dist[i] = dist[i - 1] + float(nxtdst[py, px]) / 1000.0

    sfc = np.asarray(sfcelv.record(record))[ys, xs]
    dph = np.asarray(rivdph.record(record))[ys, xs] if rivdph is not None else None
    return LongitudinalProfile(path=path, distance_km=dist, elevtn_m=elevtn[ys, xs],
                               sfcelv_m=sfc, rivdph_m=dph)


# --------------------------------------------------------------------------- caveats


def interpretation_notes(*, spinup_records: int, forcing_resolution_deg: float | None,
                         gauge_area_km2: float | None, grid: Grid) -> list[str]:
    """Caveats the GUI should display beside the numbers.

    Generated rather than written into the docs, because a user reading a hydrograph is not
    reading the manual.
    """
    notes: list[str] = []

    if spinup_records == 0:
        notes.append(
            "No spin-up excluded. A cold start from zero storage spends the first months "
            "filling the channel network: measured January discharge on the Mekong was "
            "211 m3/s against an observed dry season of 1 500-2 500 m3/s. Run a spin-up year "
            "and analyse the next one."
        )

    if forcing_resolution_deg and forcing_resolution_deg >= 0.5:
        notes.append(
            f"Forcing is {forcing_resolution_deg} deg. Coarse daily runoff cannot resolve a "
            "typhoon or a convective event: simulated peaks will be low. Measured Cagayan "
            "peak 6 793 m3/s against ~17 000 observed. Use this run for water balance and "
            "seasonality, not for design flood peaks."
        )

    if gauge_area_km2:
        cell_km2 = float(np.mean(grid.cell_area_km2()))
        n_cells = gauge_area_km2 / max(cell_km2, 1.0)
        if n_cells < 50:
            notes.append(
                f"This basin is only about {n_cells:.0f} routing cells at "
                f"{grid.gsize} deg. Flood-wave timing and peak attenuation are poorly "
                f"constrained at that resolution."
            )

    return notes
