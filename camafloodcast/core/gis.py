"""Importing real-world GIS rasters (GeoTIFF, any CRS) onto CaMa-Flood's own WGS84 grid.

CaMa-Flood's own maps have no CRS concept at all -- the model is WGS84 lat/lon throughout, and
every reader in ``core.maps`` assumes a raster is *already* gridded onto the domain (see
``read_layer``). That is fine for CaMa's own map-building tools, but a real user pointing this
tool at a real-world product -- ESA WorldCover, a national land-cover map, a locally surveyed
levee raster -- almost never gets one pre-gridded onto this exact domain in this exact
projection. This module is the missing step: detect what CRS a GeoTIFF is actually in, and warp
it onto the routing grid before anything downstream (e.g. ``domain_builder.build_floodplain_roughness``)
ever sees it.

``rasterio``/``pyproj`` are optional (the ``geo`` extra) -- everything here raises a clear,
actionable error rather than failing at import time if they are missing, matching how
``core.forcing`` guards its own optional netCDF import.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from .errors import FileFormatError
from .maps import Grid

Method = Literal["nearest", "bilinear", "cubic"]

_GEO_HINT = "pip install -e \".[geo]\"  (needs rasterio and pyproj)"


@dataclass(frozen=True)
class GeoTiffData:
    """A GeoTIFF's first band, read as-is -- still in its own CRS, not yet on any model grid."""

    data: np.ndarray
    #: (west, south, east, north) in the file's own CRS units -- degrees for a geographic CRS,
    #: metres for a projected one.
    bounds: tuple[float, float, float, float]
    #: EPSG code, or None when the file carries no CRS at all.
    epsg: int | None
    nodata: float


def detect_crs(path: Path | str) -> int | None:
    """The EPSG code a GeoTIFF declares, or ``None`` if it declares none."""
    try:
        import rasterio
    except ImportError as exc:
        raise FileFormatError(path, "detecting a GeoTIFF's CRS needs the 'geo' extra",
                              hint=_GEO_HINT) from exc

    with rasterio.open(Path(path)) as src:
        return src.crs.to_epsg() if src.crs else None


def read_geotiff(path: Path | str) -> GeoTiffData:
    """Read a GeoTIFF's first band and its own georeferencing, without reprojecting anything."""
    try:
        import rasterio
    except ImportError as exc:
        raise FileFormatError(path, "reading a GeoTIFF needs the 'geo' extra",
                              hint=_GEO_HINT) from exc

    with rasterio.open(Path(path)) as src:
        data = np.asarray(src.read(1), dtype=np.float64)
        b = src.bounds
        epsg = src.crs.to_epsg() if src.crs else None
        nodata = float(src.nodata) if src.nodata is not None else -9999.0

    return GeoTiffData(data=data, bounds=(b.left, b.bottom, b.right, b.top),
                       epsg=epsg, nodata=nodata)


def reproject_to_grid(
    data: np.ndarray,
    src_bounds: tuple[float, float, float, float],
    src_epsg: int,
    grid: Grid,
    *,
    method: Method = "nearest",
    src_nodata: float = -9999.0,
    dst_nodata: float = -9999.0,
) -> np.ndarray:
    """Warp ``data`` from its own CRS onto ``grid`` (CaMa-Flood's own WGS84 routing grid).

    ``method`` defaults to nearest-neighbour because the first caller (land cover) is
    categorical -- interpolating between class codes produces values that are not any real
    class. Pick ``"bilinear"``/``"cubic"`` explicitly for a continuous field (elevation, a
    fractional cover map).
    """
    try:
        from rasterio.crs import CRS
        from rasterio.transform import from_bounds
        from rasterio.warp import Resampling, reproject
    except ImportError as exc:
        raise ImportError(
            f"reprojecting a raster needs the 'geo' extra: {_GEO_HINT}") from exc

    w, s, e, n = src_bounds
    src_transform = from_bounds(w, s, e, n, data.shape[1], data.shape[0])
    src_crs = CRS.from_epsg(src_epsg)
    dst_crs = CRS.from_epsg(4326)
    dst_transform = from_bounds(grid.west, grid.south, grid.east, grid.north, grid.nx, grid.ny)

    resampling = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
    }[method]

    dst = np.full((grid.ny, grid.nx), dst_nodata, dtype=np.float64)
    reproject(
        source=np.asarray(data, dtype=np.float64),
        destination=dst,
        src_transform=src_transform, src_crs=src_crs,
        dst_transform=dst_transform, dst_crs=dst_crs,
        src_nodata=src_nodata, dst_nodata=dst_nodata,
        resampling=resampling,
    )
    return dst


def write_geotiff(path: Path | str, data: np.ndarray,
                  bounds: tuple[float, float, float, float], *,
                  nodata: float = -9999.0, epsg: int = 4326) -> None:
    """Write ``data`` as a GeoTIFF covering ``bounds`` (west, south, east, north).

    The counterpart to :func:`read_geotiff`/:func:`reproject_to_grid`: something downscaled or
    computed onto CaMa-Flood's own grid, saved so a real GIS tool can open it. ``epsg`` defaults
    to WGS84 because every array this app hands here (routing-grid or downscaled sub-grid) is
    already on that grid -- CaMa-Flood has no other CRS concept, same assumption
    :func:`reproject_to_grid` makes for its own ``dst_crs``.
    """
    try:
        import rasterio
        from rasterio.crs import CRS
        from rasterio.transform import from_bounds
    except ImportError as exc:
        raise FileFormatError(path, "writing a GeoTIFF needs the 'geo' extra",
                              hint=_GEO_HINT) from exc

    w, s, e, n = bounds
    arr = np.asarray(data, dtype=np.float32)
    transform = from_bounds(w, s, e, n, arr.shape[1], arr.shape[0])
    with rasterio.open(
        Path(path), "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
        count=1, dtype="float32", crs=CRS.from_epsg(epsg), transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(arr, 1)


def import_geotiff_to_grid(path: Path | str, grid: Grid, *, method: Method = "nearest",
                           assume_epsg: int = 4326) -> tuple[np.ndarray, int]:
    """Read a GeoTIFF and land it on ``grid`` in one call -- what a file-picker handler wants.

    Returns ``(array, source_epsg)`` so a caller can report what it reprojected from (or that it
    assumed WGS84, when the file carries no CRS at all -- some regional products ship without
    one, and refusing to proceed would be worse than assuming the common case and saying so).
    """
    tif = read_geotiff(path)
    epsg = tif.epsg if tif.epsg is not None else assume_epsg
    out = reproject_to_grid(tif.data, tif.bounds, epsg, grid, method=method,
                            src_nodata=tif.nodata)
    return out, epsg
