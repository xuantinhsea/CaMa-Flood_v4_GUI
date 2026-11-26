"""Dam parameters: the ``CDAMFILE`` reservoir-operation input (``NDAMOUT``/``LDAMOUT``).

Verified against ``src/cmf_ctrl_damout_mod.F90`` (``CMF_DAMOUT_NMLIST`` / ``CMF_DAMOUT_INIT``).
The file is plain-text, free-format, comma-separated: a record count, a header line (skipped),
then one row per dam::

    DamID, DamName, DamLat, DamLon, upreal, DamIX, DamIY,
    FldVol_mcm, ConVol_mcm, TotVol_mcm, Qn, Qf  [, DamYear if LDAMYBY]

Two things worth stating plainly:

**``DamIX``/``DamIY`` are 1-indexed** (the same Fortran convention as ``nextxy`` --
see ``core.analysis.trace_downstream``), and the model **skips a dam silently** if they fall
outside the grid or land on a non-river cell (``cmf_ctrl_damout_mod.F90`` lines ~187-193: a bare
``cycle``, no warning, no error). A wrong allocation does not fail the run; the reservoir simply
never operates. This module validates the allocation against a real ``RiverMap`` before writing,
specifically to make that failure mode loud instead of silent, in keeping with the project's
general stance on CaMa-Flood's silent failure modes.

**``Qn`` is not used verbatim.** The model re-derives an operational value from it
(``min(user_Qn, dry-season storage-based estimate) * 1.5``) unless ``LDAMH22`` is set, so a
real annual-mean-inflow estimate is what the field wants -- not a tuned release rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .maps import RiverMap
from .output import OutputFile


@dataclass
class DamRecord:
    """One reservoir. Coordinates and grid indices use this codebase's usual 0-indexed
    convention; conversion to/from the file's 1-indexed ``DamIX``/``DamIY`` happens at the
    read/write boundary, the same pattern as ``trace_downstream``."""

    dam_id: int
    name: str
    lat: float
    lon: float
    upreal_km2: float
    ix: int
    iy: int
    fldvol_mcm: float
    convol_mcm: float
    totvol_mcm: float
    qn: float
    qf: float
    dam_year: int | None = None      # only meaningful when the file uses LDAMYBY

    @property
    def totvol_check(self) -> float:
        """FldVol + ConVol, for comparison against TotVol -- the model does not enforce these
        sum to the same value, but a large mismatch usually means a data-entry mistake."""
        return self.fldvol_mcm + self.convol_mcm


def read_dam_file(path: Path | str, *, year_by_year: bool = False) -> list[DamRecord]:
    """Read a ``CDAMFILE``. Free-format comma-separated, so extra whitespace is harmless."""
    p = Path(path)
    lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
             if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"{p}: expected a count line and a header, found {len(lines)} line(s)")

    try:
        n = int(lines[0].split(",")[0].strip())
    except (ValueError, IndexError) as exc:
        raise ValueError(f"{p}: first line should be the dam count, got {lines[0]!r}") from exc

    rows = lines[2:2 + n]
    if len(rows) < n:
        raise ValueError(f"{p}: header declares {n} dams but only {len(rows)} data row(s) follow")

    records: list[DamRecord] = []
    for row in rows:
        f = [x.strip() for x in row.split(",")]
        want = 13 if year_by_year else 12
        if len(f) < want:
            raise ValueError(f"{p}: row {f!r} has {len(f)} fields, expected {want}")
        records.append(DamRecord(
            dam_id=int(float(f[0])), name=f[1], lat=float(f[2]), lon=float(f[3]),
            upreal_km2=float(f[4]),
            ix=int(f[5]) - 1, iy=int(f[6]) - 1,          # 1-indexed in the file
            fldvol_mcm=float(f[7]), convol_mcm=float(f[8]), totvol_mcm=float(f[9]),
            qn=float(f[10]), qf=float(f[11]),
            dam_year=int(float(f[12])) if year_by_year else None,
        ))
    return records


def write_dam_file(records: list[DamRecord], path: Path | str, *,
                   river_map: RiverMap | None = None, year_by_year: bool = False) -> None:
    """Write a ``CDAMFILE``.

    ``river_map``, when given, is a validation pass, not an optional nicety: it is the only
    thing standing between a wrong allocation and a reservoir that silently never operates
    (see the module docstring). Every record is checked against the real grid and land mask.
    """
    if not records:
        raise ValueError("no dam records to write")

    if river_map is not None:
        grid = river_map.grid
        land = river_map.land_mask
        for r in records:
            if not (0 <= r.ix < grid.nx and 0 <= r.iy < grid.ny):
                raise ValueError(f"{r.name}: ({r.ix},{r.iy}) is outside the {grid.nx}x{grid.ny} "
                                 f"grid -- CaMa-Flood would skip this dam silently, not error")
            if not land[r.iy, r.ix]:
                raise ValueError(f"{r.name}: ({r.ix},{r.iy}) is not a river cell -- CaMa-Flood "
                                 f"would skip this dam silently, not error")

    lines = [str(len(records)),
            "DamID,DamName,DamLat,DamLon,upreal,DamIX,DamIY,FldVol_mcm,ConVol_mcm,TotVol_mcm,Qn,Qf"
            + (",DamYear" if year_by_year else "")]
    for r in records:
        row = [str(r.dam_id), r.name, f"{r.lat:.4f}", f"{r.lon:.4f}", f"{r.upreal_km2:.3f}",
              str(r.ix + 1), str(r.iy + 1),                # back to 1-indexed for the file
              f"{r.fldvol_mcm:.2f}", f"{r.convol_mcm:.2f}", f"{r.totvol_mcm:.2f}",
              f"{r.qn:.3f}", f"{r.qf:.3f}"]
        if year_by_year:
            row.append(str(r.dam_year if r.dam_year is not None else 0))
        lines.append(",".join(row))

    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def inflow_stats(outflw: OutputFile, ix: int, iy: int, *,
                 flood_percentile: float = 90.0) -> tuple[float, float]:
    """(Qn, Qf) estimated from a real simulated discharge series at one cell: the mean as the
    annual-inflow estimate ``Qn`` wants, and a high percentile as a flood-threshold ``Qf`` --
    both real, measured values rather than assumed constants. Include spin-up years in the
    series only if they are representative; a cold-start year will bias the mean low (see
    05-workflows.md on spin-up)."""
    series = np.asarray(outflw.series_at(ix, iy), dtype=np.float64)
    series = series[np.isfinite(series)]
    if series.size == 0:
        raise ValueError(f"no finite discharge values at ({ix},{iy})")
    return float(series.mean()), float(np.percentile(series, flood_percentile))
