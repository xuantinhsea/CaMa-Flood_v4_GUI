"""1-arc-minute inundation downscaling: redistributing routing-grid flood stage onto real terrain.

CaMa-Flood routes on the 0.25 deg unit-catchment grid; a cell near the equator is ~750 km2, and
painting `flddph` at that resolution produces blocky cells that look like an inundation map and
are not (see 01-rri-vs-camaflood.md #c3). The package ships 1-arcmin sub-grid topography
(`combine_hires`, in `map/<region>/1min/`) precisely so the routing-grid stage can be
redistributed over ~1.85 km pixels using HAND (height above nearest drainage) and floodplain
relief (`flddif`). **This is a diagnostic redistribution of stage the model already computed,
not a re-route** -- it adds no new physics and cannot correct a wrong routing-grid answer.

The redistribution itself is `downscale_flddph_trib`, a standalone Fortran program (verified
against `etc/downscale_flddph/src/downscale_flddph_trib.F90`) that this module wraps. Two
things about it are easy to get wrong:

**The map directory is not a runtime argument.** The Fortran source hardcodes
``character*256 :: mapdir; parameter (mapdir='./map/')`` -- a compile-time constant, not one of
the 8 command-line arguments. The only way to point the tool at a different regional map is to
control the *working directory* it runs in, so it finds ``./map/params.txt``, ``./map/nextxy.bin``
etc. This module does that with an NTFS junction (Windows) or a symlink (POSIX) rather than
copying the sub-grid data, which is exactly the "never load a whole output file" discipline
applied to the map side instead of the output side.

**The nodata sentinel is -9999, not the model's RMIS (1e20).** The Fortran initialises its
output array to -9999 and only overwrites pixels that fall inside a mapped unit catchment; it
never touches the model's own missing-value convention. A pixel at -9999 means "outside any
unit catchment in this window" (the window's rectangular buffer, or open ocean); 0 means "mapped
and dry". Conflating the two would silently corrupt coverage statistics.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import CamaError
from .output import TimeAxis

#: Pixel size in degrees for each resolution tag the Fortran tool accepts.
CSIZE_DEG: dict[str, float] = {
    "1sec": 1 / 3600, "3sec": 1 / 1200, "5sec": 1 / 720,
    "15sec": 1 / 240, "30sec": 1 / 120, "1min": 1 / 60,
}

#: The tool's own "outside any unit catchment" sentinel -- see the module docstring.
NODATA = -9999.0


class DownscaleError(CamaError):
    """The downscale tool failed, or its output could not be read back."""

    def __init__(self, returncode: int, output: str):
        self.returncode = returncode
        self.output = output
        tail = "\n".join(output.strip().splitlines()[-8:]) if output.strip() else "(no output)"
        super().__init__(f"downscale_flddph_trib failed (exit {returncode})\n{tail}")


@dataclass(frozen=True)
class DownscaleWindow:
    """The output window: bounds in degrees, at a chosen sub-grid resolution.

    A 5-routing-cell buffer is added internally by the Fortran tool on every side (so a flood
    wave crossing the window edge is redistributed correctly); the buffer is cropped back out
    before the file is written, so callers never see it.
    """

    west: float
    east: float
    south: float
    north: float
    resolution: str = "1min"

    def __post_init__(self):
        if self.resolution not in CSIZE_DEG:
            raise ValueError(f"unknown resolution {self.resolution!r}; "
                             f"choose from {sorted(CSIZE_DEG)}")
        if self.east <= self.west or self.north <= self.south:
            raise ValueError("the window has zero or negative extent")

    @property
    def csize(self) -> float:
        return CSIZE_DEG[self.resolution]

    @property
    def shape(self) -> tuple[int, int]:
        """(ny, nx) of the downscaled output -- matches the Fortran tool's own ``nint`` rounding."""
        nx = round((self.east - self.west) / self.csize + 0.001)
        ny = round((self.north - self.south) / self.csize + 0.001)
        return ny, nx

    @property
    def args(self) -> list[str]:
        return [f"{self.west}", f"{self.east}", f"{self.south}", f"{self.north}", self.resolution]


def _mapped_working_dir(map_dir: Path, scratch: Path) -> Path:
    """A scratch directory with ``./map`` pointing at ``map_dir``.

    An NTFS junction (Windows) needs no admin rights or Developer Mode, unlike a symlink; a
    POSIX symlink needs neither. Either way the multi-gigabyte sub-grid data is referenced, not
    copied -- the same "link large data" principle as everywhere else in the project.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    link = scratch / "map"
    if link.is_symlink() or link.exists():
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "rmdir", str(link)], check=False, capture_output=True)
        else:
            link.unlink()
    if sys.platform == "win32":
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(map_dir)],
                              check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            raise DownscaleError(proc.returncode, proc.stdout + proc.stderr)
    else:
        link.symlink_to(map_dir, target_is_directory=True)
    return scratch


def read_downscaled(path: Path, window: DownscaleWindow) -> np.ndarray:
    """Read a downscaled field: ``(ny, nx)`` float32, north to south, matching every other
    CaMa-Flood binary layer's layout. Pixels are :data:`NODATA` outside any unit catchment."""
    ny, nx = window.shape
    arr = np.fromfile(path, dtype="<f4")
    if arr.size != nx * ny:
        raise DownscaleError(-1, f"{path} has {arr.size} values, expected {nx * ny} "
                                 f"({nx} x {ny} at {window.resolution})")
    return arr.reshape(ny, nx)


def mask_nodata(arr: np.ndarray) -> np.ndarray:
    """True where a pixel has no unit-catchment mapping in this window (not merely dry)."""
    return arr <= NODATA + 1.0


def record_for_date(time: TimeAxis, when: dt.datetime | dt.date) -> int:
    """The 1-indexed record number ``downscale_flddph_trib`` expects for a given date.

    ``TimeAxis.record_of`` (core/output.py) is 0-indexed, matching ``OutputFile.record()`` and
    every other SDK reader. The Fortran tool's ``trec`` argument is a direct-access record
    number, which Fortran numbers from 1. Passing a 0-indexed value straight through would
    silently downscale the day *before* the one requested.
    """
    if isinstance(when, dt.date) and not isinstance(when, dt.datetime):
        when = dt.datetime(when.year, when.month, when.day)
    return time.record_of(when) + 1


@dataclass
class DownscaleTool:
    """Where ``downscale_flddph_trib`` lives, and how to run it."""

    exe: Path
    runtime_path: Path | None = None

    def environment(self) -> dict:
        env = dict(os.environ)
        if self.runtime_path:
            # Prepend, never replace -- see risk R4.
            env["PATH"] = str(self.runtime_path) + os.pathsep + env.get("PATH", "")
        return env

    def run(self, map_dir: Path, flddph_path: Path, record: int, window: DownscaleWindow,
           out_path: Path, *, scratch: Path | None = None, timeout_s: float = 600.0) -> np.ndarray:
        """Downscale one record of a routing-grid flood-depth file and return the result.

        ``flddph_path`` and ``out_path`` may be anywhere; only the sub-grid lookup is
        constrained to a working directory with ``./map`` linked to ``map_dir`` (see
        :func:`_mapped_working_dir`). Returns the array; also leaves it at ``out_path``.
        """
        map_dir, flddph_path, out_path = Path(map_dir), Path(flddph_path), Path(out_path)
        if not self.exe.exists():
            raise DownscaleError(-1, f"tool not found: {self.exe}")
        if not flddph_path.exists():
            raise DownscaleError(-1, f"flood-depth input not found: {flddph_path}")
        hires_dir = map_dir / window.resolution
        if not (hires_dir / "location.txt").exists():
            raise DownscaleError(
                -1, f"{window.resolution} sub-grid data not found under {map_dir} -- it is "
                   f"produced by combine_hires in the Domain Builder, and is a separate step "
                   f"from cutting the routing-grid domain itself")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        args = [str(self.exe), *window.args, str(flddph_path), str(out_path), str(record)]

        def invoke(work: Path) -> None:
            proc = subprocess.run(args, cwd=str(work), env=self.environment(),
                                  capture_output=True, text=True, errors="replace",
                                  timeout=timeout_s)
            if proc.returncode != 0:
                raise DownscaleError(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))

        if scratch is not None:
            invoke(_mapped_working_dir(map_dir, scratch))
        else:
            with tempfile.TemporaryDirectory(prefix="cmf_downscale_") as tmp:
                invoke(_mapped_working_dir(map_dir, Path(tmp)))

        return read_downscaled(out_path, window)


# --------------------------------------------------------------------------- statistics


@dataclass
class InundationStats:
    """Area, volume and depth summary of one downscaled field -- what Module 9 reports."""

    valid_pixels: int
    inundated_pixels: int
    inundated_area_km2: float
    inundated_volume_mcm: float
    mean_depth_m: float
    p90_depth_m: float
    max_depth_m: float

    def summary(self) -> str:
        if self.inundated_pixels == 0:
            return f"no inundation ({self.valid_pixels:,} valid pixels)"
        return (f"{self.inundated_pixels:,} pixels = {self.inundated_area_km2:,.0f} km2, "
                f"{self.inundated_volume_mcm:,.0f} Mm3  |  "
                f"depth: mean {self.mean_depth_m:.2f} m, p90 {self.p90_depth_m:.2f} m, "
                f"max {self.max_depth_m:.2f} m")


def _pixel_area_km2(window: DownscaleWindow, ny: int, nx: int) -> np.ndarray:
    """(ny, nx) pixel area, varying with latitude -- a flat degrees-squared conversion
    overstates area enough to matter even at Mekong latitudes. This formula is the one measured
    against the published reference (~41,000 km2 for the October Mekong peak, see
    01-rri-vs-camaflood.md #c3)."""
    dlat_km = (window.north - window.south) / ny * 111.32
    lat = window.north - (np.arange(ny) + 0.5) * (window.north - window.south) / ny
    dlon_km = (window.east - window.west) / nx * 111.32 * np.cos(np.radians(lat))
    return (dlat_km * dlon_km)[:, None] * np.ones((1, nx))


def inundation_stats(arr: np.ndarray, window: DownscaleWindow, *,
                     threshold_m: float = 0.01) -> InundationStats:
    """Inundated area, volume and depth distribution for one day."""
    valid = ~mask_nodata(arr)
    wet = valid & (arr > threshold_m)

    ny, nx = arr.shape
    area_km2 = _pixel_area_km2(window, ny, nx)

    if not wet.any():
        return InundationStats(int(valid.sum()), 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    depths = arr[wet]
    # depth [m] * area [km2 -> m2] summed over wet pixels, in millions of m3.
    volume_mcm = float(np.sum(depths * area_km2[wet] * 1e6) / 1e6)
    return InundationStats(
        valid_pixels=int(valid.sum()),
        inundated_pixels=int(wet.sum()),
        inundated_area_km2=float(area_km2[wet].sum()),
        inundated_volume_mcm=volume_mcm,
        mean_depth_m=float(depths.mean()),
        p90_depth_m=float(np.percentile(depths, 90)),
        max_depth_m=float(depths.max()),
    )


# --------------------------------------------------------------------------- batch over a period


@dataclass
class PeriodResult:
    """A batch downscale: a per-day time series plus a maximum-depth composite.

    The composite is the natural "one map" summary of a flood period -- the deepest water any
    pixel saw, regardless of which day. It is not a single physical instant; label it as a
    maximum, not a snapshot.
    """

    records: list[int]                 # 1-indexed, as passed to the tool
    dates: list[dt.datetime]
    area_km2: np.ndarray
    volume_mcm: np.ndarray
    mean_depth_m: np.ndarray
    max_depth_m: np.ndarray
    max_depth_field: np.ndarray        # (ny, nx), NODATA outside any unit catchment

    @property
    def peak_index(self) -> int:
        """Index of the day with the largest inundated area -- the natural default day to show
        alongside the composite."""
        return int(np.argmax(self.area_km2)) if len(self.area_km2) else 0


def downscale_period(tool: DownscaleTool, map_dir: Path, flddph_path: Path, time: TimeAxis,
                     window: DownscaleWindow, start_record: int, end_record: int, *,
                     progress=None) -> PeriodResult:
    """Downscale every day in ``[start_record, end_record]`` (0-indexed, inclusive, the SDK's
    own convention -- see :func:`record_for_date`) and summarise the period.

    Each day is a separate invocation of the same tool used for a single day: the Fortran
    program is not designed to be run once per period, and re-merging the sub-grid tiles per
    day is the cost of that simplicity. Fine for the regional windows this application targets
    under decision D1; a global, multi-year batch would need a different approach (tiling,
    parallel workers) that is out of scope here.

    ``progress(record, i, n)`` is called after each day, 1-indexed ``i`` of ``n``.
    """
    if not 0 <= start_record <= end_record < time.count:
        raise ValueError(f"record range {start_record}..{end_record} is outside "
                         f"0..{time.count - 1}")

    n = end_record - start_record + 1
    dates: list[dt.datetime] = []
    records: list[int] = []
    area = np.zeros(n)
    volume = np.zeros(n)
    mean_depth = np.zeros(n)
    max_depth = np.zeros(n)
    composite: np.ndarray | None = None

    with tempfile.TemporaryDirectory(prefix="cmf_downscale_batch_") as tmp:
        scratch = Path(tmp)
        for i, rec0 in enumerate(range(start_record, end_record + 1)):
            fortran_rec = rec0 + 1
            out_path = scratch / f"day_{fortran_rec}.bin"
            arr = tool.run(map_dir, flddph_path, fortran_rec, window, out_path, scratch=scratch)
            stats = inundation_stats(arr, window)

            dates.append(time.date_of(rec0))
            records.append(fortran_rec)
            area[i] = stats.inundated_area_km2
            volume[i] = stats.inundated_volume_mcm
            mean_depth[i] = stats.mean_depth_m
            max_depth[i] = stats.max_depth_m

            valid_today = ~mask_nodata(arr)
            if composite is None:
                composite = np.where(valid_today, arr, NODATA).astype("<f4")
            else:
                composite = np.where(valid_today, np.maximum(composite, arr), composite)

            if progress:
                progress(fortran_rec, i + 1, n)
            out_path.unlink(missing_ok=True)   # only the composite and the series are kept

    assert composite is not None    # n >= 1, guaranteed by the range check above
    return PeriodResult(records=records, dates=dates, area_km2=area, volume_mcm=volume,
                        mean_depth_m=mean_depth, max_depth_m=max_depth, max_depth_field=composite)
