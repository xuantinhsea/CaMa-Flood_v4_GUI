"""Turning model arrays into images.

Kept out of the widget layer so it can be tested without a display, and so the eventual tile
server (Phase 3) can reuse it unchanged.

Two things this handles that naive colormapping does not:

* **CaMa-Flood's undefined value.** Cells outside the routing network carry ``RMIS`` (1e20).
  Colouring them alongside real data flattens the whole map to one colour.
* **Sensible default ranges.** Percentiles rather than min/max, because one extreme cell
  otherwise dominates the scale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

import numpy as np

from ..core.output import RMIS

#: Colormaps offered in the UI. Names are matplotlib's; the first is the default.
COLORMAPS: tuple[str, ...] = (
    "viridis", "Blues", "YlGnBu", "jet", "turbo", "RdYlBu_r", "magma", "terrain", "gray",
)

#: Per-variable defaults, so a newly added layer looks right without fiddling.
VARIABLE_STYLE: dict[str, tuple[str, str]] = {
    # variable -> (colormap, scale)
    "outflw": ("Blues", "log"),
    "rivout": ("Blues", "log"),
    "storge": ("Blues", "log"),
    "rivdph": ("YlGnBu", "linear"),
    "flddph": ("jet", "linear"),
    "fldare": ("YlGnBu", "linear"),
    "fldfrc": ("YlGnBu", "linear"),
    "sfcelv": ("terrain", "linear"),
    "rivvel": ("magma", "linear"),
    "maxdph": ("jet", "linear"),
    "maxflw": ("Blues", "log"),
    "uparea": ("Blues", "log"),
    "elevtn": ("terrain", "linear"),
    "rivwth": ("Blues", "log"),
    "rivhgt": ("YlGnBu", "linear"),
}

Scale = Literal["linear", "log"]


@dataclass(frozen=True)
class ColorPreset:
    """A named, discrete colour ramp: a handful of hex colours at fixed break-values.

    Unlike a continuous matplotlib colormap (interpolated over ``[vmin, vmax]``), a preset's
    bounds are absolute values a reader can look up in the legend -- "orange starts at 1.0 m" --
    which is what a discrete flood-depth or rainfall-intensity legend conventionally looks like.
    """

    colors: tuple[str, ...]      #: hex, e.g. "#2E86AB", one per bound
    bounds: tuple[float, ...]    #: break-values, same length as colors, ascending
    #: Values below this are drawn fully transparent rather than the first band's colour --
    #: without it, every dry cell in the domain paints an opaque sheet over whatever is
    #: underneath (the basemap, once one exists; another layer today).
    transparent_below: float | None = None
    #: Values below bounds[0] take colors[0] instead of vanishing -- for a diverging scale
    #: (a negative tail that should stay visible), not for a depth/rainfall-style ramp.
    clamp_low: bool = False

    def __post_init__(self) -> None:
        if len(self.colors) != len(self.bounds):
            raise ValueError("ColorPreset.colors and .bounds must be the same length")
        if len(self.colors) < 2:
            raise ValueError("ColorPreset needs at least 2 bands")


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


#: Named presets for CaMa-Flood's own variables. Seeded from a sister tool's (RRI_Floodcast)
#: field-tested palettes, including the "transparent below" cutoffs that keep a dry cell from
#: masking whatever is under it.
COLOR_PRESETS: dict[str, ColorPreset] = {
    "Floodplain depth (blue)": ColorPreset(
        colors=("#FFFFFF", "#BDE5F8", "#7EC8E3", "#4BA3C3", "#2E86AB", "#1B4F72", "#0B2545"),
        bounds=(0.0, 0.1, 0.3, 0.5, 1.0, 2.0, 5.0),
        transparent_below=1e-3,
    ),
    "River depth (blue)": ColorPreset(
        colors=("#FFFFFF", "#BDE5F8", "#7EC8E3", "#4BA3C3", "#2E86AB", "#1B4F72", "#0B2545"),
        bounds=(0.0, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0),
    ),
    "Discharge (blue)": ColorPreset(
        colors=("#F7FBFF", "#DEEBF7", "#9ECAE1", "#4292C6", "#2171B5", "#08519C", "#08306B"),
        bounds=(0.0, 10.0, 100.0, 1000.0, 5000.0, 20000.0, 100000.0),
    ),
    "Difference (diverging)": ColorPreset(
        colors=("#053061", "#2166AC", "#92C5DE", "#F7F7F7", "#F4A582", "#B2182B", "#67001F"),
        bounds=(-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0),
        clamp_low=True,
    ),
    "Velocity (orange)": ColorPreset(
        colors=("#FFFFFF", "#FED976", "#FEB24C", "#FD8D3C", "#FC4E2A", "#E31A1C", "#800026"),
        bounds=(0.0, 0.1, 0.3, 0.5, 1.0, 2.0, 5.0),
    ),
    "Elevation (terrain)": ColorPreset(
        colors=("#006837", "#31A354", "#A1D99B", "#FFFFCC", "#FEB24C", "#E31A1C", "#800026"),
        bounds=(0.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0),
    ),
}

#: variable -> default preset name, mirroring VARIABLE_STYLE's role for continuous colormaps.
VARIABLE_PRESET: dict[str, str] = {
    "flddph": "Floodplain depth (blue)",
    "maxdph": "Floodplain depth (blue)",
    "rivdph": "River depth (blue)",
    "outflw": "Discharge (blue)",
    "rivout": "Discharge (blue)",
    "maxflw": "Discharge (blue)",
    "rivvel": "Velocity (orange)",
    "elevtn": "Elevation (terrain)",
    "sfcelv": "Elevation (terrain)",
}


def set_color_preset(name: str, colors: Sequence[str], bounds: Sequence[float], *,
                     transparent_below: float | None = None,
                     clamp_low: bool | None = None) -> ColorPreset:
    """Install or replace a named preset.

    ``transparent_below``/``clamp_low`` are carried over from the existing preset of that name
    when not given -- an editor dialog that only exposes colours and bounds must not silently
    turn off a preset's dry-cell transparency or its diverging clamp when the user just tweaks
    a colour.
    """
    existing = COLOR_PRESETS.get(name)
    if transparent_below is None and existing is not None:
        transparent_below = existing.transparent_below
    if clamp_low is None:
        clamp_low = existing.clamp_low if existing is not None else False
    preset = ColorPreset(colors=tuple(colors), bounds=tuple(bounds),
                         transparent_below=transparent_below, clamp_low=bool(clamp_low))
    COLOR_PRESETS[name] = preset
    return preset


def save_presets(path: str | Path) -> None:
    """Write every named preset to a JSON file (global or per-project)."""
    data: dict[str, dict] = {}
    for name, p in COLOR_PRESETS.items():
        entry: dict = {"colors": list(p.colors), "bounds": list(p.bounds)}
        if p.transparent_below is not None:
            entry["transparent_below"] = p.transparent_below
        if p.clamp_low:
            entry["clamp_low"] = True
        data[name] = entry
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_presets(path: str | Path) -> int:
    """Load presets from a JSON file into ``COLOR_PRESETS``, returning how many were loaded.

    Missing file is not an error -- a project or a fresh install simply has none saved yet.
    """
    path = Path(path)
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    for name, entry in data.items():
        if isinstance(entry, dict) and "colors" in entry and "bounds" in entry:
            set_color_preset(name, entry["colors"], entry["bounds"],
                             transparent_below=entry.get("transparent_below"),
                             clamp_low=entry.get("clamp_low"))
            n += 1
    return n


@dataclass
class ColorSpec:
    """How one layer is coloured.

    Two modes, chosen by whether ``colors``/``bounds`` are set:

    * **Continuous** (default): a matplotlib colormap (``cmap``) interpolated over
      ``[vmin, vmax]``, linear or log. This is what percentile-ranged layers use today.
    * **Discrete preset**: ``colors``/``bounds`` from a named :class:`ColorPreset` (see
      :meth:`from_preset`) -- a handful of bands at fixed, labelled break-values instead of a
      smooth ramp. ``vmin``/``vmax`` still hold ``bounds[0]``/``bounds[-1]`` so callers that only
      look at those two fields (e.g. an "is this in range" check) keep working.
    """

    cmap: str = "viridis"
    vmin: float = 0.0
    vmax: float = 1.0
    scale: Scale = "linear"
    opacity: float = 0.85
    #: Values at or below this are drawn fully transparent. For depth-like variables this
    #: stops a dry floodplain being painted the "zero" colour of the ramp, which reads as
    #: shallow water and is the single most misleading thing a flood map can do.
    transparent_below: float | None = None
    #: Discrete-preset mode when both are set (see class docstring); ``None``/``None`` keeps
    #: the continuous-colormap behaviour every existing caller already relies on.
    colors: tuple[str, ...] | None = None
    bounds: tuple[float, ...] | None = None
    #: Values below bounds[0] take colors[0] instead of being drawn transparent. Discrete mode
    #: only; see :class:`ColorPreset`.
    clamp_low: bool = False
    #: Name of the active preset, if any -- purely informational, for a layer panel's combo box
    #: to show which preset is selected. Editing colours/bounds directly does not require this
    #: to stay in sync with ``COLOR_PRESETS``.
    preset: str | None = None

    @classmethod
    def for_variable(cls, name: str, vmin: float, vmax: float) -> "ColorSpec":
        cmap, scale = VARIABLE_STYLE.get(name, ("viridis", "linear"))
        spec = cls(cmap=cmap, vmin=vmin, vmax=vmax, scale=scale)
        if name in ("flddph", "fldare", "fldfrc", "maxdph"):
            spec.transparent_below = max(vmin, 1e-3)
        return spec

    @classmethod
    def from_preset(cls, name: str, *, opacity: float = 0.85) -> "ColorSpec":
        """Build a discrete-band spec from a named entry in ``COLOR_PRESETS``."""
        p = COLOR_PRESETS[name]
        return cls(vmin=p.bounds[0], vmax=p.bounds[-1], opacity=opacity,
                  colors=p.colors, bounds=p.bounds,
                  transparent_below=p.transparent_below, clamp_low=p.clamp_low, preset=name)


def _lut(cmap: str, n: int = 256) -> np.ndarray:
    """(n, 4) uint8 RGBA lookup table from a matplotlib colormap."""
    try:
        import matplotlib
        cm = matplotlib.colormaps[cmap]
    except Exception:
        import matplotlib
        cm = matplotlib.colormaps["viridis"]
    xs = np.linspace(0.0, 1.0, n)
    return (np.asarray(cm(xs)) * 255.0).astype(np.uint8)


def normalise(values: np.ndarray, spec: ColorSpec) -> np.ndarray:
    """Map values to 0..1, honouring the scale. NaN stays NaN."""
    v = np.asarray(values, dtype=np.float64)
    if spec.scale == "log":
        lo = max(spec.vmin, 1e-6)
        hi = max(spec.vmax, lo * 10.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = (np.log10(np.maximum(v, lo)) - np.log10(lo)) / (np.log10(hi) - np.log10(lo))
    else:
        span = spec.vmax - spec.vmin
        out = (v - spec.vmin) / (span if span else 1.0)
    return np.clip(out, 0.0, 1.0)


def _band_colorize(v: np.ndarray, spec: ColorSpec,
                   valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Discrete-preset colouring: linear interpolation within each band, flat beyond the ends.

    Returns (rgb, valid) -- ``valid`` is narrowed to drop cells below ``bounds[0]`` unless
    ``clamp_low`` is set, matching :class:`ColorPreset`'s documented behaviour.
    """
    bounds = spec.bounds
    rgbs = [hex_to_rgb(c) for c in spec.colors]
    rgb = np.zeros(v.shape + (3,), dtype=np.uint8)

    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        band = valid & (v >= lo) & (v < hi)
        frac = np.clip((v - lo) / (hi - lo + 1e-12), 0.0, 1.0)
        for k in range(3):
            c0, c1 = rgbs[i][k], rgbs[i + 1][k]
            rgb[..., k] = np.where(band, np.round(c0 + (c1 - c0) * frac).astype(np.uint8),
                                   rgb[..., k])

    above = valid & (v >= bounds[-1])
    for k in range(3):
        rgb[..., k] = np.where(above, rgbs[-1][k], rgb[..., k])

    if spec.clamp_low:
        below = valid & (v < bounds[0])
        for k in range(3):
            rgb[..., k] = np.where(below, rgbs[0][k], rgb[..., k])
    else:
        valid = valid & (v >= bounds[0])

    return rgb, valid


def colorize(values: np.ndarray, spec: ColorSpec, *,
             mask: np.ndarray | None = None, undef: float = RMIS) -> np.ndarray:
    """Array -> (ny, nx, 4) uint8 RGBA, with undefined cells fully transparent.

    ``mask`` is normally ``RiverMap.land_mask``: anything outside it is not part of the model
    domain and must not be coloured.
    """
    v = np.asarray(values, dtype=np.float64)
    valid = np.isfinite(v) & (np.abs(v) < undef * 0.5)
    if mask is not None:
        valid &= mask
    if spec.transparent_below is not None:
        valid &= v > spec.transparent_below

    if spec.colors is not None and spec.bounds is not None:
        rgb, valid = _band_colorize(v, spec, valid)
    else:
        idx = (normalise(np.where(valid, v, spec.vmin), spec) * 255.0).astype(np.uint8)
        rgb = _lut(spec.cmap)[idx][..., :3]

    rgba = np.zeros(v.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = np.where(valid, int(np.clip(spec.opacity, 0, 1) * 255), 0).astype(np.uint8)
    return rgba


def legend_stops(spec: ColorSpec, n: int = 7) -> list[tuple[float, tuple[int, int, int]]]:
    """(value, rgb) pairs for drawing a legend.

    Discrete-preset mode returns exactly the preset's own band edges (so the legend always shows
    the same break-values ``colorize()`` actually used), ignoring ``n``.
    """
    if spec.colors is not None and spec.bounds is not None:
        return [(float(b), hex_to_rgb(c)) for b, c in zip(spec.bounds, spec.colors)]

    if spec.scale == "log":
        lo = max(spec.vmin, 1e-6)
        hi = max(spec.vmax, lo * 10.0)
        vals = np.logspace(np.log10(lo), np.log10(hi), n)
    else:
        vals = np.linspace(spec.vmin, spec.vmax, n)
    lut = _lut(spec.cmap)
    idx = (normalise(vals, spec) * 255.0).astype(np.uint8)
    return [(float(v), tuple(int(c) for c in lut[i][:3])) for v, i in zip(vals, idx)]


def format_value(v: float, unit: str = "") -> str:
    """Compact, readable numbers for legends and probes."""
    if not np.isfinite(v):
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        s = f"{v / 1e9:.2f}e9"
    elif a >= 1e6:
        s = f"{v / 1e6:.2f}e6"
    elif a >= 1000:
        s = f"{v:,.0f}"
    elif a >= 1:
        s = f"{v:.2f}"
    elif a > 0:
        s = f"{v:.3g}"
    else:
        s = "0"
    return f"{s} {unit}".strip()
