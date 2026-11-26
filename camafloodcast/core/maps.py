"""River-network map I/O: params.txt, diminfo, and the binary map layers.

Two traps this module exists to neutralise.

**params.txt has two flavours.** Global maps write ``1440      !! grid number (east-west)``
while ``set_map`` writes ``68            grid number (east-west)`` -- no ``!!``. Parsing by
comment marker works on one and fails on the other, so we take the first whitespace token.

**params.txt and diminfo order their edges differently.** params.txt is
west / east / **south / north**; diminfo is west / east / **north / south**. Verified against
``map/glb_15min/params.txt`` and ``map/glb_15min/diminfo_test-1deg.txt``. Getting this wrong
flips the domain north-to-south, which looks like a plausible map of somewhere else.

All binary layers are Fortran direct-access, 4-byte, little-endian by default, ``NX*NY`` per
record, written north to south. Every read is size-validated (risk R3): Fortran ``RECL`` means
bytes under gfortran but 4-byte *words* under some compilers, and a mismatch silently reads
plausible garbage rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

from .errors import DiagnosticList, FileFormatError

# --------------------------------------------------------------------------- layer registry

#: name -> (numpy dtype, number of records, description)
#: ``None`` records means "NLFP records", resolved from the grid.
MAP_LAYERS: dict[str, tuple[str, int | None, str]] = {
    "nextxy":         ("<i4", 2,    "downstream cell (x, y); -9 mouth, -10 inland sink, -9999 land"),
    "downxy":         ("<i4", 2,    "downstream direction offsets"),
    "ctmare":         ("<f4", 1,    "unit-catchment area [m2]"),
    "grdare":         ("<f4", 1,    "grid-cell area [m2]"),
    "elevtn":         ("<f4", 1,    "channel bank-top elevation [m]"),
    "nxtdst":         ("<f4", 1,    "distance to the downstream cell [m]"),
    "rivlen":         ("<f4", 1,    "channel length [m]"),
    "fldhgt":         ("<f4", None, "floodplain hypsometry, NLFP layers [m]"),
    "rivwth":         ("<f4", 1,    "channel width, empirical [m]"),
    "rivwth_gwdlr":   ("<f4", 1,    "channel width, GWD-LR observed [m]"),
    "rivhgt":         ("<f4", 1,    "channel bankfull depth [m]"),
    "rivhgt_inf":     ("<f4", 1,    "channel depth for infinite-bank runs [m]"),
    "rivman":         ("<f4", 1,    "channel Manning coefficient"),
    "fldman":         ("<f4", 1,    "floodplain Manning coefficient (CFLDMAN)"),
    "landclass":      ("<i4", 1,    "land-cover class code (IGBP-like; source for CFLDMAN)"),
    "levhgt":         ("<f4", 1,    "levee crown height above the channel bank top [m] (CLEVHGT)"),
    "levfrc":         ("<f4", 1,    "unprotected fraction, relative channel-to-levee distance [0-1] (CLEVFRC)"),
    "uparea":         ("<f4", 1,    "upstream drainage area [m2] -- GLOBAL value, see note"),
    "basin":          ("<i4", 1,    "basin ID"),
    "bsncol":         ("<i4", 1,    "basin colour index"),
    "rivseq":         ("<i4", 1,    "river sequence order"),
    "lonlat":         ("<f4", 2,    "cell centre longitude, latitude"),
    "lsmask":         ("<i4", 1,    "land-sea mask"),
    "upgrid":         ("<i4", 1,    "number of upstream grid cells"),
    "width":          ("<f4", 1,    "channel width (source)"),
}

#: Values of ``nextxy[0]`` that are not a downstream index.
NEXTXY_LAND = -9999   # not a river cell
NEXTXY_MOUTH = -9     # drains to the sea
NEXTXY_SINK = -10     # inland termination


# --------------------------------------------------------------------------- grid


@dataclass(frozen=True)
class Grid:
    """A regular lat/lon routing grid. CaMa-Flood is WGS84 lat/lon only."""

    nx: int
    ny: int
    nlfp: int
    gsize: float
    west: float
    east: float
    south: float
    north: float

    # -------------------------------------------------- geometry
    @property
    def shape(self) -> tuple[int, int]:
        return (self.ny, self.nx)

    @property
    def ncell(self) -> int:
        return self.nx * self.ny

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(west, south, east, north) -- the order every GIS library expects."""
        return (self.west, self.south, self.east, self.north)

    def lons(self) -> np.ndarray:
        """Cell-centre longitudes, west to east."""
        return self.west + (np.arange(self.nx) + 0.5) * self.gsize

    def lats(self) -> np.ndarray:
        """Cell-centre latitudes, north to south (the storage order)."""
        return self.north - (np.arange(self.ny) + 0.5) * self.gsize

    def cell_of(self, lon: float, lat: float) -> tuple[int, int] | None:
        """(ix, iy) containing this point, or None if outside the domain."""
        ix = int((lon - self.west) / self.gsize)
        iy = int((self.north - lat) / self.gsize)
        if 0 <= ix < self.nx and 0 <= iy < self.ny:
            return ix, iy
        return None

    def centre_of(self, ix: int, iy: int) -> tuple[float, float]:
        return (self.west + (ix + 0.5) * self.gsize,
                self.north - (iy + 0.5) * self.gsize)

    def cell_area_km2(self) -> np.ndarray:
        """Approximate cell area on a sphere, for quick statistics.

        Prefer ``ctmare.bin`` for anything quantitative -- that is the model's own
        unit-catchment area and is what the mass balance uses.
        """
        r = 6371.0
        dlat = np.deg2rad(self.gsize)
        lat_n = np.deg2rad(self.north - np.arange(self.ny) * self.gsize)
        lat_s = lat_n - dlat
        band = (r ** 2) * np.deg2rad(self.gsize) * (np.sin(lat_n) - np.sin(lat_s))
        return np.repeat(band[:, None], self.nx, axis=1)

    def offset_in(self, parent: "Grid") -> tuple[int, int]:
        """(ix0, iy0) of this grid's origin inside a coarser-or-equal parent grid.

        Used to window channel parameters out of a global map (see ``domain.py``).
        """
        if abs(self.gsize - parent.gsize) > 1e-9:
            raise ValueError(f"grid sizes differ: {self.gsize} vs {parent.gsize}")
        return (int(round((self.west - parent.west) / parent.gsize)),
                int(round((parent.north - self.north) / parent.gsize)))

    def describe(self) -> str:
        return (f"{self.nx} x {self.ny} @ {self.gsize} deg  "
                f"({self.west}..{self.east} E, {self.south}..{self.north} N), NLFP={self.nlfp}")


def _tokens(path: Path) -> list[str]:
    """First whitespace token of each non-blank line.

    Handles both params.txt flavours: ``1440      !! comment`` and ``68      comment``.
    """
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s:
            out.append(s.split()[0])
    return out


def read_params(path: Path | str) -> Grid:
    """Read ``params.txt``: nx, ny, nlfp, gsize, west, east, **south, north**."""
    p = Path(path)
    t = _tokens(p)
    if len(t) < 8:
        raise FileFormatError(p, f"expected 8 values, found {len(t)}",
                              hint="params.txt is nx, ny, nlfp, gsize, west, east, south, north.")
    try:
        return Grid(int(t[0]), int(t[1]), int(t[2]), float(t[3]),
                    float(t[4]), float(t[5]), float(t[6]), float(t[7]))
    except ValueError as exc:
        raise FileFormatError(p, f"could not parse params.txt: {exc}") from exc


@dataclass(frozen=True)
class DimInfo:
    """``diminfo_*.txt`` -- the routing grid plus the forcing grid it is fed from."""

    grid: Grid
    nxin: int
    nyin: int
    inpn: int
    inpmat: str

    @property
    def forcing_matches_routing(self) -> bool:
        """True when the forcing grid equals the routing grid.

        In that case LINTERP=.FALSE. (nearest point) is exact and needs no input matrix.
        """
        return self.nxin == self.grid.nx and self.nyin == self.grid.ny


def read_diminfo(path: Path | str) -> DimInfo:
    """Read ``diminfo``: nx, ny, nlfp, nxin, nyin, inpn, inpmat, west, east, **north, south**."""
    p = Path(path)
    t = _tokens(p)
    if len(t) < 11:
        raise FileFormatError(p, f"expected 11 values, found {len(t)}",
                              hint="diminfo is nx, ny, nlfp, nxin, nyin, inpn, inpmat, "
                                   "west, east, north, south.")
    try:
        # NOTE the edge order differs from params.txt: north before south.
        grid = Grid(int(t[0]), int(t[1]), int(t[2]),
                    gsize=(float(t[8]) - float(t[7])) / int(t[0]),
                    west=float(t[7]), east=float(t[8]),
                    south=float(t[10]), north=float(t[9]))
        return DimInfo(grid, int(t[3]), int(t[4]), int(t[5]), t[6])
    except (ValueError, ZeroDivisionError) as exc:
        raise FileFormatError(p, f"could not parse diminfo: {exc}") from exc


# --------------------------------------------------------------------------- binary layers


def expected_size(grid: Grid, layer: str) -> int:
    dtype, nrec, _ = MAP_LAYERS[layer]
    nrec = grid.nlfp if nrec is None else nrec
    return grid.nx * grid.ny * nrec * np.dtype(dtype).itemsize


def read_layer(path: Path | str, grid: Grid, layer: str | None = None, *,
               big_endian: bool = False) -> np.ndarray:
    """Read a map layer, validating its size first.

    Returns ``(ny, nx)`` for single-record layers and ``(nrec, ny, nx)`` otherwise.

    The size check is the point: a wrong ``RECL`` convention or a truncated download produces
    an array of the right dtype and the wrong content, which no downstream check would catch.
    """
    p = Path(path)
    name = layer or p.stem
    if name not in MAP_LAYERS:
        raise FileFormatError(p, f"unknown map layer {name!r}",
                              hint=f"known layers: {', '.join(sorted(MAP_LAYERS))}")

    dtype, nrec, _ = MAP_LAYERS[name]
    nrec = grid.nlfp if nrec is None else nrec
    if big_endian:
        dtype = dtype.replace("<", ">")

    want = expected_size(grid, name)
    have = p.stat().st_size
    if have != want:
        raise FileFormatError(
            p,
            f"size {have:,} bytes but the grid implies {want:,} "
            f"({grid.nx} x {grid.ny} x {nrec} x {np.dtype(dtype).itemsize})",
            hint="Either the grid is wrong (check params.txt) or the file was written with a "
                 "different Fortran RECL convention -- gfortran counts RECL in bytes, some "
                 "compilers in 4-byte words.",
        )

    arr = np.fromfile(p, dtype=dtype)
    return arr.reshape(grid.ny, grid.nx) if nrec == 1 else arr.reshape(nrec, grid.ny, grid.nx)


def open_layer(path: Path | str, grid: Grid, layer: str | None = None, *,
               big_endian: bool = False) -> np.memmap:
    """Memory-map a layer instead of reading it.

    Preferred everywhere in the GUI. Under decision D1 development is regional, where a plain
    read would be harmless -- which is exactly the trap: code written against ``read_layer``
    silently becomes unusable when the same code meets a global domain.
    """
    p = Path(path)
    name = layer or p.stem
    dtype, nrec, _ = MAP_LAYERS[name]
    nrec = grid.nlfp if nrec is None else nrec
    if big_endian:
        dtype = dtype.replace("<", ">")

    want = expected_size(grid, name)
    have = p.stat().st_size
    if have != want:
        raise FileFormatError(p, f"size {have:,} bytes but the grid implies {want:,}",
                              hint="See read_layer(); the grid or the RECL convention is wrong.")

    shape = (grid.ny, grid.nx) if nrec == 1 else (nrec, grid.ny, grid.nx)
    return np.memmap(p, dtype=dtype, mode="r", shape=shape)


# --------------------------------------------------------------------------- the map set


class RiverMap:
    """A CaMa-Flood map directory: grid, layers, and derived masks.

    Layers are memory-mapped lazily on first access and cached, so constructing this is cheap
    even for a global map.
    """

    def __init__(self, directory: Path | str, *, big_endian: bool = False):
        self.dir = Path(directory)
        self.big_endian = big_endian
        params = self.dir / "params.txt"
        if not params.exists():
            raise FileFormatError(params, "no params.txt: this is not a CaMa-Flood map directory")
        self.grid = read_params(params)
        self._cache: dict[str, np.ndarray] = {}

    # -------------------------------------------------- layers
    def path_of(self, layer: str) -> Path:
        return self.dir / f"{layer}.bin"

    def has(self, layer: str) -> bool:
        return self.path_of(layer).exists()

    def layer(self, name: str) -> np.ndarray:
        if name not in self._cache:
            self._cache[name] = open_layer(self.path_of(name), self.grid, name,
                                           big_endian=self.big_endian)
        return self._cache[name]

    def available(self) -> list[str]:
        return sorted(n for n in MAP_LAYERS if self.has(n))

    # -------------------------------------------------- derived
    @property
    def nextxy(self) -> np.ndarray:
        return self.layer("nextxy")

    @property
    def land_mask(self) -> np.ndarray:
        """True where the cell is a river cell (i.e. part of the routing network)."""
        key = "_land"
        if key not in self._cache:
            self._cache[key] = self.nextxy[0] != NEXTXY_LAND
        return self._cache[key]

    @property
    def outlet_mask(self) -> np.ndarray:
        """True at river mouths and inland sinks -- cells with no downstream neighbour."""
        key = "_outlet"
        if key not in self._cache:
            nx0 = self.nextxy[0]
            self._cache[key] = (nx0 == NEXTXY_MOUTH) | (nx0 == NEXTXY_SINK)
        return self._cache[key]

    @property
    def n_river_cells(self) -> int:
        return int(self.land_mask.sum())

    @property
    def n_outlets(self) -> int:
        return int(self.outlet_mask.sum())

    def diminfo_files(self) -> list[Path]:
        return sorted(self.dir.glob("diminfo*.txt"))

    def summary(self) -> str:
        return (f"{self.dir.name}: {self.grid.describe()}\n"
                f"  {self.n_river_cells:,} river cells, {self.n_outlets:,} outlets\n"
                f"  layers: {', '.join(self.available())}")

    def __repr__(self) -> str:
        return f"<RiverMap {self.dir.name} {self.grid.nx}x{self.grid.ny}>"


# --------------------------------------------------------------------------- bifurcation


@dataclass
class BifurcationTable:
    """``bifprm.txt`` -- the bifurcation channel list read by the model."""

    npath: int
    nlev: int
    ix: np.ndarray
    iy: np.ndarray
    jx: np.ndarray
    jy: np.ndarray
    length: np.ndarray
    elevtn: np.ndarray
    depth: np.ndarray
    width: np.ndarray          # (npath, nlev)
    lat: np.ndarray
    lon: np.ndarray


def read_bifprm(path: Path | str) -> BifurcationTable:
    """Read ``bifprm.txt``.

    Layout: a header line giving ``npath nlev``, then one line per path:
    ``ix iy jx jy length elevtn depth width(1..nlev) lat lon [basins...]``.
    """
    p = Path(path)
    lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    if not lines:
        raise FileFormatError(p, "file is empty")

    head = lines[0].split()
    try:
        npath, nlev = int(head[0]), int(head[1])
    except (IndexError, ValueError) as exc:
        raise FileFormatError(p, f"bad header line: {lines[0]!r}") from exc

    rows = lines[1:]
    if len(rows) < npath:
        raise FileFormatError(p, f"header declares {npath} paths but only {len(rows)} rows follow")

    n = npath
    ix = np.empty(n, "i4"); iy = np.empty(n, "i4")
    jx = np.empty(n, "i4"); jy = np.empty(n, "i4")
    length = np.empty(n, "f8"); elevtn = np.empty(n, "f8"); depth = np.empty(n, "f8")
    width = np.empty((n, nlev), "f8")
    lat = np.empty(n, "f8"); lon = np.empty(n, "f8")

    for i, row in enumerate(rows[:n]):
        f = row.split()
        ix[i], iy[i], jx[i], jy[i] = (int(f[0]), int(f[1]), int(f[2]), int(f[3]))
        length[i], elevtn[i], depth[i] = float(f[4]), float(f[5]), float(f[6])
        width[i] = [float(x) for x in f[7:7 + nlev]]
        lat[i], lon[i] = float(f[7 + nlev]), float(f[8 + nlev])

    return BifurcationTable(npath, nlev, ix, iy, jx, jy, length, elevtn, depth, width, lat, lon)
