"""Building a regional map by cutting a global one.

Seven steps producing ~20 files, where the failure mode is invisible. CaMa-Flood has no
lateral boundary inflow, so if a basin's headwaters fall outside the domain the model simply
never receives that runoff: discharge is biased low by roughly the missing area fraction, with
no error and a hydrograph that looks entirely plausible.

    cut_domain -> cut_bifway -> set_map -> combine_hires
               -> generate_inpmat -> channel params -> set_bifparam

**Step 6 deliberately windows the channel parameters out of the global map** rather than
re-running ``s01-channel_params.sh``. That script re-estimates width and depth from the annual
discharge accumulated *inside the domain* (``W = 2.5 Qav^0.6``, ``H = 0.1 Qav^0.5``), so any
truncated basin silently gets channels that are too narrow and too shallow -- worst exactly
where the truncation is worst. Windowing keeps the geometry identical to a global run, which
also makes regional results directly comparable.

The external tools are plain Fortran executables; this module runs them with the right working
directory and arguments, reports progress, and validates the result with a closure check.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .errors import CamaError, DiagnosticList
from .maps import MAP_LAYERS, Grid, RiverMap, read_params

#: Channel parameters windowed from the global map. Missing ones are skipped.
CHANNEL_LAYERS = ("rivhgt", "rivwth", "rivwth_gwdlr", "rivman", "rivhgt_inf")

ProgressFn = Callable[[str, int, int], None]      # (step name, index, total)


class DomainBuildError(CamaError):
    """A step of the cut chain failed."""

    def __init__(self, step: str, returncode: int, output: str):
        self.step = step
        self.returncode = returncode
        self.output = output
        tail = "\n".join(output.strip().splitlines()[-8:])
        super().__init__(f"{step} failed (exit {returncode})\n{tail}")


@dataclass
class DomainRequest:
    """What to cut."""

    name: str
    source_map: Path
    west: float
    east: float
    south: float
    north: float
    hires: str = "1min"
    bifurcation_layers: int = 5

    def snapped(self, gsize: float) -> "DomainRequest":
        """Snap the edges to the source grid; a half-cell offset corrupts every later step."""
        r = round
        return DomainRequest(
            self.name, self.source_map,
            west=r(self.west / gsize) * gsize, east=r(self.east / gsize) * gsize,
            south=r(self.south / gsize) * gsize, north=r(self.north / gsize) * gsize,
            hires=self.hires, bifurcation_layers=self.bifurcation_layers)

    def validate(self, source_grid: Grid) -> DiagnosticList:
        d = DiagnosticList()
        if self.east <= self.west or self.north <= self.south:
            d.error("DOM_BOUNDS", "the domain has zero or negative extent")
            return d
        if (self.west < source_grid.west or self.east > source_grid.east
                or self.south < source_grid.south or self.north > source_grid.north):
            d.error("DOM_OUTSIDE",
                    f"the domain is not inside the source map "
                    f"({source_grid.west}..{source_grid.east} E, "
                    f"{source_grid.south}..{source_grid.north} N)")
        nx = int(round((self.east - self.west) / source_grid.gsize))
        ny = int(round((self.north - self.south) / source_grid.gsize))
        if nx < 2 or ny < 2:
            d.error("DOM_TINY", f"the domain is only {nx} x {ny} cells")
        elif nx * ny > 4_000_000:
            d.warning("DOM_HUGE",
                      f"{nx} x {ny} cells is a very large domain; the sub-grid step will be slow",
                      hint="Consider whether the study really needs this extent.")
        return d


@dataclass
class ToolSet:
    """The Fortran executables the chain needs, and where they live."""

    directory: Path
    suffix: str = ".exe" if sys.platform == "win32" else ""
    runtime_path: Path | None = None

    REQUIRED = ("cut_domain", "cut_bifway", "set_map", "combine_hires", "generate_inpmat")
    PARAM = ("set_bifparam",)

    def path(self, name: str) -> Path:
        return self.directory / f"{name}{self.suffix}"

    def missing(self) -> list[str]:
        return [n for n in (*self.REQUIRED, *self.PARAM) if not self.path(n).exists()]

    def environment(self) -> dict:
        import os

        env = dict(os.environ)
        if self.runtime_path:
            # Prepend, never replace -- see risk R4.
            env["PATH"] = str(self.runtime_path) + os.pathsep + env.get("PATH", "")
        return env


@dataclass
class BuildResult:
    map_dir: Path
    grid: Grid
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.steps)


class DomainBuilder:
    """Runs the seven-step chain into ``map/<name>/``."""

    STEPS = ("cut_domain", "cut_bifway", "set_map", "combine_hires",
             "generate_inpmat", "channel parameters", "set_bifparam")

    def __init__(self, tools: ToolSet, maps_root: Path):
        self.tools = tools
        self.maps_root = Path(maps_root)

    # ---------------------------------------------------------------- helpers

    def _run(self, exe: Path, args: list[str], cwd: Path, step: str) -> str:
        if not exe.exists():
            raise DomainBuildError(step, -1, f"tool not found: {exe}")
        try:
            proc = subprocess.run([str(exe), *args], cwd=str(cwd),
                                  env=self.tools.environment(),
                                  capture_output=True, text=True, errors="replace",
                                  timeout=3600)
        except subprocess.TimeoutExpired as exc:
            raise DomainBuildError(step, -1, "timed out after one hour") from exc
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            raise DomainBuildError(step, proc.returncode, out)
        return out

    def prepare(self, request: DomainRequest) -> Path:
        """Create the target directory and write ``region_info.txt``."""
        dst = self.maps_root / request.name
        work = dst / "src_region"
        work.mkdir(parents=True, exist_ok=True)
        (dst / request.hires).mkdir(parents=True, exist_ok=True)

        # Relative to the tool's own working directory (dst/src_region), wherever the source
        # actually lives -- not assumed to be a sibling of maps_root. A hardcoded "../../name"
        # only happens to be correct when the source sits directly inside maps_root; a source
        # opened from elsewhere (an imported project's active map, say) would silently point
        # cut_domain at the wrong place.
        rel = Path(os.path.relpath(request.source_map, work))
        (work / "region_info.txt").write_text(
            f"{rel.as_posix()}/\n{request.west}\n{request.east}\n"
            f"{request.south}\n{request.north}\n", encoding="utf-8")
        return dst

    # ---------------------------------------------------------------- the chain

    def build(self, request: DomainRequest, *, progress: ProgressFn | None = None) -> BuildResult:
        """Run every step. Raises :class:`DomainBuildError` on the first failure."""
        source = RiverMap(request.source_map)
        request = request.snapped(source.grid.gsize)

        diags = request.validate(source.grid)
        if not diags.ok:
            raise DomainBuildError("validate", -1,
                                   "\n".join(str(d) for d in diags))

        dst = self.prepare(request)
        work = dst / "src_region"
        result = BuildResult(map_dir=dst, grid=source.grid)
        total = len(self.STEPS)

        def step(i: int, name: str) -> None:
            if progress:
                progress(name, i, total)

        # 1-3: topography, bifurcation list, derived fields.
        for i, name in enumerate(("cut_domain", "cut_bifway", "set_map")):
            step(i, name)
            out = self._run(self.tools.path(name), [], work, name)
            result.steps.append((name, True, out))

        # 4: sub-grid data. Needed by generate_inpmat and by downscaling.
        step(3, "combine_hires")
        out = self._run(self.tools.path("combine_hires"), [request.hires], work, "combine_hires")
        result.steps.append(("combine_hires", True, out))

        # 5: the mass-conservative input matrix.
        step(4, "generate_inpmat")
        out = self._run(self.tools.path("generate_inpmat"), [request.hires],
                        work, "generate_inpmat")
        result.steps.append(("generate_inpmat", True, out))

        # 6: channel parameters, WINDOWED from the global map (see the module docstring).
        step(5, "channel parameters")
        report = window_channel_parameters(request.source_map, dst)
        result.steps.append(("channel parameters", True, report))

        # 7: bifurcation parameters, which need rivhgt from step 6.
        step(6, "set_bifparam")
        diminfo = "./diminfo_test-1deg.txt"
        out = self._run(self.tools.path("set_bifparam"),
                        ["rivhgt.bin", "bifprm.txt", str(request.bifurcation_layers), diminfo],
                        dst, "set_bifparam")
        result.steps.append(("set_bifparam", True, out))

        result.grid = read_params(dst / "params.txt")
        if progress:
            progress("done", total, total)
        return result


def window_channel_parameters(source_map: Path, target_map: Path) -> str:
    """Copy rivhgt / rivwth / rivman out of the global map onto the regional grid.

    Preferred over re-deriving them inside the domain: ``calc_outclm`` accumulates annual
    discharge across the domain only, so a truncated basin produces channels that are too
    narrow and too shallow, silently, and worst exactly where the truncation is worst.
    """
    src = RiverMap(source_map)
    dst_grid = read_params(Path(target_map) / "params.txt")

    if abs(src.grid.gsize - dst_grid.gsize) > 1e-9:
        raise DomainBuildError("channel parameters", -1,
                               f"grid sizes differ: {src.grid.gsize} vs {dst_grid.gsize}")

    ix0, iy0 = dst_grid.offset_in(src.grid)
    lines = [f"window offset ix0={ix0} iy0={iy0}"]

    for name in CHANNEL_LAYERS:
        source_file = Path(source_map) / f"{name}.bin"
        if not source_file.exists():
            lines.append(f"  {name}: not in the source map, skipped")
            continue
        try:
            whole = src.layer(name)
        except (KeyError, OSError) as exc:
            lines.append(f"  {name}: unreadable ({exc}), skipped")
            continue
        window = np.ascontiguousarray(
            np.asarray(whole)[iy0:iy0 + dst_grid.ny, ix0:ix0 + dst_grid.nx])
        window.tofile(Path(target_map) / f"{name}.bin")
        # Channel parameter maps use -9999 for "not a river cell", not RMIS. Excluding both
        # keeps the reported range physical rather than showing the fill value as a minimum.
        valid = window[np.isfinite(window) & (np.abs(window) < 1e19) & (window > -9998)]
        rng = (f"{valid.min():.4g} .. {valid.max():.4g}" if valid.size else "all undefined")
        lines.append(f"  {name}: {window.shape[1]}x{window.shape[0]}  {rng}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- roughness


#: Floodplain Manning's n by broad land-cover class. Starting values for the lookup editor,
#: from the usual open-channel and floodplain literature -- a defensible prior, not a
#: calibration. The point of CFLDMAN is that these become editable and spatially variable
#: instead of one global 0.10.
DEFAULT_ROUGHNESS: dict[str, float] = {
    "water": 0.030,
    "bare / sparse": 0.035,
    "cropland": 0.045,
    "paddy": 0.050,
    "grassland": 0.050,
    "urban": 0.080,
    "shrubland": 0.080,
    "wetland / marsh": 0.100,
    "savanna": 0.100,
    "forest (deciduous)": 0.120,
    "forest (evergreen)": 0.150,
    "mangrove / swamp forest": 0.150,
}

#: MODIS/IGBP 17-class legend (code -> (label, broad category in ``DEFAULT_ROUGHNESS``). The
#: scheme GLDAS, most reanalysis-driven LSMs and a lot of published global land-cover products
#: already use, so a class raster in this coding can be turned into a Manning map with no
#: reclassification step. "paddy" and "mangrove / swamp forest" have no IGBP equivalent -- they
#: stay in ``DEFAULT_ROUGHNESS`` for a finer product's own lookup, not this one.
IGBP_CLASSES: dict[int, tuple[str, str]] = {
    1: ("Evergreen Needleleaf Forest", "forest (evergreen)"),
    2: ("Evergreen Broadleaf Forest", "forest (evergreen)"),
    3: ("Deciduous Needleleaf Forest", "forest (deciduous)"),
    4: ("Deciduous Broadleaf Forest", "forest (deciduous)"),
    5: ("Mixed Forest", "forest (deciduous)"),
    6: ("Closed Shrublands", "shrubland"),
    7: ("Open Shrublands", "shrubland"),
    8: ("Woody Savannas", "savanna"),
    9: ("Savannas", "savanna"),
    10: ("Grasslands", "grassland"),
    11: ("Permanent Wetlands", "wetland / marsh"),
    12: ("Croplands", "cropland"),
    13: ("Urban and Built-up", "urban"),
    14: ("Cropland/Natural Vegetation Mosaic", "cropland"),
    15: ("Snow and Ice", "bare / sparse"),
    16: ("Barren or Sparsely Vegetated", "bare / sparse"),
    17: ("Water Bodies", "water"),
}


def default_igbp_lookup(overrides: dict[str, float] | None = None) -> dict[int, float]:
    """IGBP class code -> Manning n, composed from ``DEFAULT_ROUGHNESS`` via ``IGBP_CLASSES``.

    ``overrides`` replaces individual *broad-category* values (e.g. ``{"urban": 0.09}``) before
    the per-code table is built -- the shape a UI editor naturally produces, one row per class
    code, mapped back through the category it belongs to.
    """
    table = dict(DEFAULT_ROUGHNESS)
    if overrides:
        table.update(overrides)
    return {code: table[category] for code, (_, category) in IGBP_CLASSES.items()}


def build_floodplain_roughness(classes: np.ndarray, lookup: dict[int, float], grid: Grid,
                               *, default: float = 0.10,
                               out_path: Path | None = None) -> np.ndarray:
    """Turn a land-cover class raster on the routing grid into a floodplain Manning map.

    Floodplain roughness was a single global scalar (``PMANFLD = 0.10``) applied to papyrus
    swamp, boreal forest, paddy and urban floodplain alike, while the channel got a full map.
    Measured sensitivity of swapping it for a two-zone map on a 5-day global run: 9.0% RMS
    change in discharge, 4.3% in flooded area. This is the highest calibration-value-per-effort
    change available.

    ``classes`` must already be on the routing grid. ``lookup`` maps class value to Manning n;
    anything unmapped gets ``default``.
    """
    if classes.shape != (grid.ny, grid.nx):
        raise ValueError(f"class raster is {classes.shape}, expected {(grid.ny, grid.nx)}")

    out = np.full((grid.ny, grid.nx), float(default), dtype="<f4")
    for value, manning in lookup.items():
        out[classes == value] = float(manning)

    if out_path is not None:
        out.tofile(Path(out_path))
    return out


def roughness_summary(fldman: np.ndarray, mask: np.ndarray | None = None) -> str:
    v = fldman[mask] if mask is not None else fldman.ravel()
    v = v[np.isfinite(v) & (v > 0)]
    if not v.size:
        return "no valid roughness values"
    uniq, counts = np.unique(np.round(v, 4), return_counts=True)
    order = np.argsort(counts)[::-1][:6]
    parts = [f"n={u:.3f} ({100 * c / v.size:.0f}%)" for u, c in zip(uniq[order], counts[order])]
    return (f"floodplain Manning: mean {v.mean():.3f}, range {v.min():.3f}-{v.max():.3f}  |  "
            + ", ".join(parts))
