"""Lazy readers for CaMa-Flood output.

**The rule this module enforces: never load a whole output file.**

Under decision D1 development is regional, where a global read would fit in memory -- which is
precisely the trap. A 15-min global run writes ~1.5 GB per variable per simulated year
(4.15 MB per record); at 6-min resolution a single record is 25.9 MB. Code written against
"just read the array" works perfectly on the Mekong and is unusable on the globe, and by then
it is threaded through the GUI.

So every accessor here is windowed or strided over a ``np.memmap``. The interface is the same
whether the domain is 536 cells or 30 million.

Two on-disk layouts:

* **map**    ``NX*NY`` float32 per record -- the default
* **vector** ``NSEQMAX`` float32 per record (``LOUTVEC=.TRUE.``) -- about 6x smaller globally,
  because only river cells are stored. Needs the index table CaMa writes alongside it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, Sequence

import numpy as np

from .errors import FileFormatError
from .maps import Grid

Layout = Literal["map", "vector"]

#: CaMa-Flood's real missing value (``RMIS`` in the namelist, default 1e20). Cells outside the
#: routing network carry this, so any statistic that ignores it reports a maximum of 1e20.
RMIS = 1.0e20

#: Output variables CaMa-Flood can write, with units and a short meaning.
VARIABLES: dict[str, tuple[str, str]] = {
    "outflw": ("m3/s", "total outflow (channel + floodplain)"),
    "rivout": ("m3/s", "channel discharge"),
    "fldout": ("m3/s", "floodplain discharge"),
    "storge": ("m3", "total storage"),
    "rivsto": ("m3", "channel storage"),
    "fldsto": ("m3", "floodplain storage"),
    "rivdph": ("m", "channel water depth"),
    "flddph": ("m", "floodplain water depth"),
    "fldare": ("m2", "flooded area"),
    "fldfrc": ("-", "flooded fraction"),
    "sfcelv": ("m", "water surface elevation"),
    "rivvel": ("m/s", "flow velocity"),
    "pthflw": ("m3/s", "bifurcation channel flow"),
    "pthout": ("m3/s", "net bifurcation outflow"),
    "maxdph": ("m", "maximum water depth over the period"),
    "maxflw": ("m3/s", "maximum outflow over the period"),
    "maxsto": ("m3", "maximum storage over the period"),
    "runoff": ("m3/s", "input runoff"),
    "rofsub": ("m3/s", "input sub-surface runoff"),
    "gdwrtn": ("m3/s", "groundwater return flow"),
    "damsto": ("m3", "reservoir storage"),
    "daminf": ("m3/s", "reservoir inflow"),
    "wevap": ("m3/s", "extracted open-water evaporation"),
    "levsto": ("m3", "protected-side storage (levee scheme)"),
    "levdph": ("m", "protected-side water depth (levee scheme)"),
    "gdwsto": ("m3", "groundwater storage"),
    "outins": ("m3/s", "instantaneous discharge"),
}

def display_name(variable: str) -> str:
    """A human-readable label, e.g. ``flddph`` -> ``Floodplain water depth``.

    The GUI shows this instead of the bare short name wherever a person is picking a variable to
    look at -- the short name is CaMa-Flood's own on-disk/namelist (``CVARSOUT``) convention, not
    something a reader should have to already know.
    """
    doc = VARIABLES.get(variable, ("", ""))[1]
    return (doc[:1].upper() + doc[1:]) if doc else variable


#: Variables whose value is a period maximum, not an instantaneous state.
MAXIMA = {"maxdph", "maxflw", "maxsto"}

#: Diagnostics whose interpretation needs a caveat in the GUI.
CAVEATS: dict[str, str] = {
    "fldare": "Bistable on cells with no floodplain relief (fldhgt == 0 in every layer): "
              "flooded fraction can only be 0 or 1 there, so totals jump. Prefer downscaled "
              "inundation for area statistics.",
    "fldfrc": "See fldare -- same bistability on degenerate-hypsometry cells.",
    "rivvel": "Diagnostic only; not used by the routing (except by the sediment scheme).",
    "levsto": "Only meaningful with LLEVEE on. Zero everywhere on a case without a levee scheme "
              "-- that is the model working as intended (there is no 'protected side' to have "
              "storage), not a missing-data problem.",
    "levdph": "Only meaningful with LLEVEE on. Depth on the *protected* side of the levee -- "
              "distinct from flddph, which is depth on the river side. Zero until the river "
              "side rises high enough to overtop the levee for that cell.",
}

_RE_NAME = re.compile(r"^([a-z]+)(.*)$")


def split_filename(path: Path | str) -> tuple[str, str]:
    """``outflwphl.bin`` -> ``('outflw', 'phl')``: variable name and COUTTAG."""
    stem = Path(path).stem
    for v in sorted(VARIABLES, key=len, reverse=True):
        if stem.startswith(v):
            return v, stem[len(v):]
    m = _RE_NAME.match(stem)
    return (m.group(1), m.group(2)) if m else (stem, "")


# --------------------------------------------------------------------------- the reader


@dataclass(frozen=True)
class TimeAxis:
    """Maps record index to date. CaMa writes records at a fixed interval."""

    start: dt.datetime
    step_hours: int
    count: int

    def date_of(self, rec: int) -> dt.datetime:
        return self.start + dt.timedelta(hours=self.step_hours * rec)

    def record_of(self, when: dt.datetime) -> int:
        delta = (when - self.start).total_seconds() / 3600.0
        return int(round(delta / self.step_hours))

    def dates(self) -> list[dt.datetime]:
        return [self.date_of(i) for i in range(self.count)]


class OutputFile:
    """One CaMa-Flood output variable, opened lazily.

    ``data`` is a ``np.memmap`` of shape ``(nrec, ny, nx)`` for the map layout, or
    ``(nrec, nseq)`` for the vector layout. Slicing it reads only the pages touched.
    """

    def __init__(self, path: Path | str, grid: Grid, *, layout: Layout = "map",
                 nseq: int | None = None, dtype: str = "<f4",
                 time: TimeAxis | None = None):
        self.path = Path(path)
        self.grid = grid
        self.layout = layout
        self.dtype = np.dtype(dtype)
        self.variable, self.tag = split_filename(self.path)
        self.time = time

        if layout == "vector":
            if nseq is None:
                raise ValueError("vector layout needs nseq (NSEQMAX)")
            self.record_len = nseq
        else:
            self.record_len = grid.nx * grid.ny
        self.nseq = nseq

        size = self.path.stat().st_size
        rec_bytes = self.record_len * self.dtype.itemsize
        if size % rec_bytes:
            raise FileFormatError(
                self.path,
                f"size {size:,} is not a whole number of {rec_bytes:,}-byte records",
                hint=f"Expected {self.grid.nx} x {self.grid.ny} x 4 bytes per record for the "
                     f"map layout. If the run used LOUTVEC=.TRUE. the layout is 'vector' and "
                     f"needs NSEQMAX.",
            )
        self.nrec = size // rec_bytes
        self._mm: np.memmap | None = None

    # ---------------------------------------------------------------- access

    @property
    def data(self) -> np.memmap:
        """The memory map. Slice it; do not call ``np.asarray`` on the whole thing."""
        if self._mm is None:
            shape = ((self.nrec, self.grid.ny, self.grid.nx) if self.layout == "map"
                     else (self.nrec, self.record_len))
            self._mm = np.memmap(self.path, dtype=self.dtype, mode="r", shape=shape)
        return self._mm

    @property
    def unit(self) -> str:
        return VARIABLES.get(self.variable, ("?", ""))[0]

    @property
    def description(self) -> str:
        return VARIABLES.get(self.variable, ("", "unknown variable"))[1]

    @property
    def caveat(self) -> str | None:
        return CAVEATS.get(self.variable)

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size

    def record(self, rec: int, *, window: tuple[int, int, int, int] | None = None) -> np.ndarray:
        """One timestep, optionally windowed to ``(ix0, iy0, nx, ny)``.

        Only the requested window is materialised.
        """
        if not 0 <= rec < self.nrec:
            raise IndexError(f"record {rec} out of range (0..{self.nrec - 1})")
        if self.layout == "vector" or window is None:
            return np.array(self.data[rec])
        ix0, iy0, nx, ny = window
        return np.array(self.data[rec, iy0:iy0 + ny, ix0:ix0 + nx])

    def series_at(self, ix: int, iy: int, *, records: Sequence[int] | None = None) -> np.ndarray:
        """Time series at one cell -- a strided read, never a full load.

        This is the hot path for gauge hydrographs.
        """
        if self.layout == "vector":
            raise NotImplementedError("use series_at_seq() for vector-layout output")
        if records is None:
            return np.array(self.data[:, iy, ix], dtype=np.float64)
        return np.array([self.data[r, iy, ix] for r in records], dtype=np.float64)

    def series_at_seq(self, iseq: int) -> np.ndarray:
        """Time series at one 1-D vector index (vector layout)."""
        if self.layout != "vector":
            raise NotImplementedError("use series_at() for map-layout output")
        return np.array(self.data[:, iseq], dtype=np.float64)

    def reduce(self, op: Literal["max", "min", "mean", "sum"], *,
               mask: np.ndarray | None = None, chunk: int = 32) -> np.ndarray:
        """Reduce over time, processing ``chunk`` records at a time.

        Chunking is what keeps this usable on a global multi-year file: peak memory is
        ``chunk`` records, not the file.
        """
        acc: np.ndarray | None = None
        n = 0
        for lo in range(0, self.nrec, chunk):
            block = np.asarray(self.data[lo:lo + chunk], dtype=np.float64)
            if mask is not None:
                block = np.where(mask[None], block, np.nan)
            if op == "max":
                part = np.nanmax(block, axis=0)
                acc = part if acc is None else np.fmax(acc, part)
            elif op == "min":
                part = np.nanmin(block, axis=0)
                acc = part if acc is None else np.fmin(acc, part)
            else:
                part = np.nansum(block, axis=0)
                acc = part if acc is None else acc + part
                n += block.shape[0]
        if acc is None:
            raise ValueError("file has no records")
        return acc / n if op == "mean" else acc

    def check_finite(self, *, chunk: int = 32, mask: np.ndarray | None = None,
                     undef: float = RMIS) -> tuple[int, float, float]:
        """(non-finite count, min, max) over the whole file, chunked.

        Part of post-run Gate 4: a NaN anywhere means the run is not usable.

        ``mask`` should normally be ``RiverMap.land_mask``, and ``undef`` defaults to CaMa's
        ``RMIS``. Without both, every variable reports a maximum of 1e20 -- the fill value in
        non-river cells -- which would give the map a useless colour range and hide any real
        outlier. Learned by doing exactly that.
        """
        bad = 0
        lo_v, hi_v = np.inf, -np.inf
        for lo in range(0, self.nrec, chunk):
            block = np.asarray(self.data[lo:lo + chunk], dtype=np.float64)
            if mask is not None:
                block = np.where(mask[None], block, np.nan)
            valid = np.isfinite(block) & (np.abs(block) < undef * 0.5)
            # NaN/Inf inside the masked domain is a genuine failure; the fill value is not.
            bad += int((~np.isfinite(block) & (mask[None] if mask is not None else True)).sum())
            if valid.any():
                lo_v = min(lo_v, float(block[valid].min()))
                hi_v = max(hi_v, float(block[valid].max()))
        if not np.isfinite(lo_v):
            lo_v = hi_v = float("nan")
        return bad, lo_v, hi_v

    def value_range(self, *, mask: np.ndarray | None = None, undef: float = RMIS,
                    percentiles: tuple[float, float] = (2.0, 98.0),
                    sample_records: int = 8) -> tuple[float, float]:
        """A sensible default colour range, from a sample of records.

        Percentiles rather than min/max, because a single extreme cell otherwise flattens the
        whole map to one colour. Sampling rather than a full pass, because this is called on
        every layer the user adds.
        """
        step = max(1, self.nrec // max(1, sample_records))
        vals: list[np.ndarray] = []
        for r in range(0, self.nrec, step):
            block = np.asarray(self.data[r], dtype=np.float64)
            if mask is not None:
                block = block[mask]
            block = block[np.isfinite(block) & (np.abs(block) < undef * 0.5)]
            if block.size:
                vals.append(block)
        if not vals:
            return (0.0, 1.0)
        allv = np.concatenate(vals)
        lo, hi = np.percentile(allv, percentiles)
        if hi <= lo:
            hi = lo + 1.0
        return float(lo), float(hi)

    def close(self) -> None:
        self._mm = None

    def __enter__(self) -> "OutputFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return (f"<OutputFile {self.path.name} {self.variable} [{self.unit}] "
                f"{self.nrec} records, {self.layout} layout, "
                f"{self.size_bytes / 1e6:.1f} MB>")


# --------------------------------------------------------------------------- a run's outputs


class OutputSet:
    """Every output file a case produced, discovered from its directory."""

    def __init__(self, directory: Path | str, grid: Grid, *, tag: str | None = None,
                 time: TimeAxis | None = None, nseq: int | None = None):
        self.dir = Path(directory)
        self.grid = grid
        self.tag = tag
        self.time = time
        self.nseq = nseq
        self._files: dict[str, OutputFile] | None = None

    def discover(self) -> dict[str, OutputFile]:
        if self._files is not None:
            return self._files

        found: dict[str, OutputFile] = {}
        for p in sorted(self.dir.glob("*.bin")):
            if p.name.startswith("restart"):
                continue
            var, tag = split_filename(p)
            if var not in VARIABLES:
                continue
            if self.tag is not None and tag != self.tag:
                continue
            try:
                found[var] = OutputFile(p, self.grid, layout="map", time=self.time)
            except FileFormatError:
                if self.nseq:
                    try:
                        found[var] = OutputFile(p, self.grid, layout="vector",
                                                nseq=self.nseq, time=self.time)
                    except FileFormatError:
                        continue
        self._files = found
        return found

    def __getitem__(self, variable: str) -> OutputFile:
        files = self.discover()
        if variable not in files:
            raise KeyError(f"{variable} not in {self.dir} "
                           f"(found: {', '.join(sorted(files)) or 'nothing'})")
        return files[variable]

    def __contains__(self, variable: str) -> bool:
        return variable in self.discover()

    @property
    def variables(self) -> list[str]:
        return sorted(self.discover())

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.discover().values())

    def summary(self) -> str:
        files = self.discover()
        if not files:
            return f"{self.dir}: no output files found"
        lines = [f"{self.dir.name}: {len(files)} variables, {self.total_bytes / 1e6:.1f} MB"]
        for v in sorted(files):
            f = files[v]
            lines.append(f"   {v:8s} [{f.unit:5s}] {f.nrec:5d} rec  {f.description}")
        return "\n".join(lines)
