"""Domain analysis: is this regional cut hydrologically usable?

CaMa-Flood has **no lateral boundary inflow**. If a basin's headwaters fall outside the domain
the model never receives that runoff, and discharge is biased low by roughly the missing area
fraction -- with no error, no warning, and a hydrograph that looks entirely plausible.

`uparea.bin` **cannot** detect this. It is windowed verbatim from the global map, so it still
reports the true *global* upstream area at every outlet. The only valid test is a catchment-area
mass balance:

    sum(ctmare) over the basin's cells INSIDE the domain   vs   uparea at its outlet

A rectangular cut always truncates something; what matters is whether the **target** basin is
closed. This module makes that checkable, which is Gate 1 of the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import DiagnosticList, Severity
from .maps import NEXTXY_MOUTH, NEXTXY_SINK, Grid, RiverMap


@dataclass(frozen=True)
class Outlet:
    """One basin outlet and its closure status."""

    ix: int
    iy: int
    lon: float
    lat: float
    basin_id: int
    uparea_km2: float           # true global upstream area (from the source map)
    in_domain_km2: float        # area actually present inside this domain
    is_mouth: bool              # river mouth (-9) rather than inland sink (-10)

    @property
    def deficit(self) -> float:
        """Fraction of the basin's area that is missing from the domain (0 = closed)."""
        if self.uparea_km2 <= 0:
            return 0.0
        return max(0.0, 1.0 - self.in_domain_km2 / self.uparea_km2)

    @property
    def closed(self) -> bool:
        return self.deficit <= 0.02

    @property
    def discharge_bias_note(self) -> str:
        if self.closed:
            return "closed"
        return (f"truncated: {self.deficit * 100:.1f}% of the contributing area is outside the "
                f"domain, so discharge here will be biased low by roughly that fraction")


@dataclass
class DomainReport:
    """Everything the Domain Builder needs to show after a cut."""

    grid: Grid
    n_river_cells: int
    n_outlets: int
    outlets: list[Outlet]
    flat_layers: int
    layer_cells: int
    cells_with_flat_layer: int
    degenerate_cells: int
    n_bifurcation: int | None = None

    @property
    def largest(self) -> list[Outlet]:
        return sorted(self.outlets, key=lambda o: o.uparea_km2, reverse=True)

    def truncated(self, top: int = 10) -> list[Outlet]:
        return [o for o in self.largest[:top] if not o.closed]

    def summary(self) -> str:
        lines = [
            f"grid        : {self.grid.describe()}",
            f"river cells : {self.n_river_cells:,}   outlets: {self.n_outlets:,}",
        ]
        if self.n_bifurcation is not None:
            lines.append(f"bifurcation : {self.n_bifurcation:,} channels")
        pct = 100.0 * self.flat_layers / max(self.layer_cells, 1)
        lines.append(f"hypsometry  : {self.flat_layers:,} flat layers of {self.layer_cells:,} "
                     f"({pct:.2f}%), {self.cells_with_flat_layer:,} cells affected")
        if self.degenerate_cells:
            lines.append(f"              {self.degenerate_cells:,} cells have NO floodplain "
                         f"relief -- flooded area is bistable there")
        lines.append("")
        lines.append(f"{'outlet lon':>10s} {'lat':>7s} {'uparea km2':>12s} "
                     f"{'in-domain':>12s} {'deficit':>8s}  status")
        lines.append("-" * 72)
        for o in self.largest[:10]:
            lines.append(f"{o.lon:10.2f} {o.lat:7.2f} {o.uparea_km2:12,.0f} "
                         f"{o.in_domain_km2:12,.0f} {o.deficit * 100:7.1f}%  "
                         f"{'closed' if o.closed else 'TRUNCATED'}")
        return "\n".join(lines)


def analyse_domain(river_map: RiverMap, *, top: int = 10) -> DomainReport:
    """Compute the closure report and hypsometry statistics for a map directory."""
    grid = river_map.grid
    nxt = river_map.nextxy
    land = river_map.land_mask
    ctm = np.asarray(river_map.layer("ctmare"), dtype=np.float64)
    up = np.asarray(river_map.layer("uparea"), dtype=np.float64)

    has_basin = river_map.has("basin")
    basin = np.asarray(river_map.layer("basin")) if has_basin else None

    # Area actually present in the domain, per basin.
    in_domain: dict[int, float] = {}
    if basin is not None:
        ids = basin[land]
        areas = ctm[land]
        order = np.argsort(ids, kind="stable")
        ids_s, areas_s = ids[order], areas[order]
        uniq, starts = np.unique(ids_s, return_index=True)
        sums = np.add.reduceat(areas_s, starts)
        in_domain = {int(k): float(v) for k, v in zip(uniq, sums)}

    outlets: list[Outlet] = []
    is_outlet = river_map.outlet_mask
    for iy, ix in zip(*np.where(is_outlet)):
        b = int(basin[iy, ix]) if basin is not None else -1
        lon, lat = grid.centre_of(int(ix), int(iy))
        outlets.append(Outlet(
            ix=int(ix), iy=int(iy), lon=lon, lat=lat, basin_id=b,
            uparea_km2=float(up[iy, ix]) / 1e6,
            in_domain_km2=in_domain.get(b, float(ctm[iy, ix])) / 1e6,
            is_mouth=bool(nxt[0][iy, ix] == NEXTXY_MOUTH),
        ))

    # Floodplain hypsometry: flat layers (FLDGRD == 0) and fully degenerate cells.
    fld = river_map.layer("fldhgt")
    prev = np.zeros((1, int(land.sum())), dtype=np.float32)
    prof = np.asarray(fld[:, land], dtype=np.float32)
    inc = np.diff(np.vstack([prev, prof]), axis=0)
    flat = inc == 0.0
    degenerate = int((prof[-1] <= 0.0).sum())

    n_bif = None
    bif = river_map.dir / "bifprm.txt"
    if bif.exists():
        try:
            first = bif.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            n_bif = int(first.split()[0])
        except (IndexError, ValueError):
            n_bif = None

    return DomainReport(
        grid=grid,
        n_river_cells=int(land.sum()),
        n_outlets=len(outlets),
        outlets=outlets,
        flat_layers=int(flat.sum()),
        layer_cells=int(flat.size),
        cells_with_flat_layer=int(flat.any(axis=0).sum()),
        degenerate_cells=degenerate,
        n_bifurcation=n_bif,
    )


def validate_domain(report: DomainReport, *, target_basin: Outlet | None = None,
                    top: int = 10) -> DiagnosticList:
    """Gate 1. Turn the closure report into diagnostics for the validation pane.

    ``target_basin`` is the basin the study is actually about. If given, its truncation is an
    ERROR; other basins truncated by a rectangular cut are only INFO, because truncating a
    neighbouring basin is expected and harmless.
    """
    d = DiagnosticList()

    if report.n_river_cells == 0:
        d.error("DOM_EMPTY", "the domain contains no river cells",
                hint="The cut window may be entirely over ocean, or the source map may be wrong.")
        return d

    truncated = report.truncated(top)
    if target_basin is not None:
        if not target_basin.closed:
            d.error("DOM_TARGET_TRUNCATED",
                    f"the target basin at {target_basin.lon:.2f}E {target_basin.lat:.2f}N is "
                    f"{target_basin.deficit * 100:.1f}% truncated",
                    hint="CaMa-Flood has no lateral boundary inflow, so the missing upstream "
                         "runoff is simply absent. Extend the domain to include the headwaters.")
        else:
            d.info("DOM_TARGET_CLOSED",
                   f"target basin closed ({target_basin.uparea_km2:,.0f} km2, 0.0% deficit)")

    for o in truncated:
        if target_basin is not None and (o.ix, o.iy) == (target_basin.ix, target_basin.iy):
            continue
        sev = Severity.WARNING if o.deficit > 0.25 else Severity.INFO
        d.add(sev, "DOM_TRUNCATED",
              f"basin at {o.lon:.2f}E {o.lat:.2f}N is {o.deficit * 100:.1f}% truncated "
              f"({o.uparea_km2:,.0f} km2 global, {o.in_domain_km2:,.0f} km2 in domain)",
              hint="Expected for a rectangular cut. Only a problem if you intend to use "
                   "discharge from this basin.")

    if report.degenerate_cells:
        d.warning("DOM_DEGENERATE_HYPSOMETRY",
                  f"{report.degenerate_cells:,} cells have no floodplain relief "
                  f"(fldhgt is zero in every layer)",
                  hint="Flooded fraction can only be 0 or 1 in those cells, so fldare totals "
                       "are bistable. Prefer downscaled inundation for area statistics.")

    if report.flat_layers:
        pct = 100.0 * report.flat_layers / max(report.layer_cells, 1)
        d.info("DOM_FLAT_LAYERS",
               f"{report.flat_layers:,} flat hypsometry layers ({pct:.2f}% of layer-cells), "
               f"affecting {report.cells_with_flat_layer:,} cells")

    if report.n_bifurcation == 0:
        d.info("DOM_NO_BIFURCATION",
               "no bifurcation channels in this domain; LPTHOUT will have no effect")

    return d


def find_outlet_near(report: DomainReport, lon: float, lat: float, *,
                     radius_deg: float = 1.0) -> Outlet | None:
    """The largest outlet within ``radius_deg`` of a point -- for picking the target basin."""
    best: Outlet | None = None
    for o in report.outlets:
        if abs(o.lon - lon) <= radius_deg and abs(o.lat - lat) <= radius_deg:
            if best is None or o.uparea_km2 > best.uparea_km2:
                best = o
    return best
