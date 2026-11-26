"""Fetching runoff forcing directly from its source: ERA5-Land (Copernicus Climate Data Store),
GLDAS Noah (NASA GES DISC), or either one again via Google Earth Engine, instead of requiring a
file the user already downloaded by hand.

Neither product is natively daily, which is the one thing that makes this more than a network
call plus the existing :func:`core.forcing.netcdf_to_binary`:

* **ERA5-Land**'s ``runoff`` is hourly-*accumulated*, resetting at every 00 UTC. CDS's own daily
  statistics dataset (``derived-era5-land-daily-statistics``) explicitly excludes accumulated
  variables like runoff, so there is no shortcut -- a calendar day's total has to be computed from
  the hourly file here. Because the series is monotonically non-decreasing within a day and resets
  near each day boundary, the day's total equals its own last (chronologically latest) hourly
  sample -- this holds regardless of the exact hour a reset happens to land on.
* **GLDAS Noah** (``GLDAS_NOAH025_3H``) ships 3-hourly, split into ``Qs`` (surface runoff) and
  ``Qsb`` (subsurface baseflow), both rates in kg/m2/s (= mm/s) -- CaMa-Flood wants their sum, and
  a full day is 8 files.

Both aggregation functions write a daily-total netCDF using exactly the ``lat``/``lon``/``time``
("days since ...") conventions and ``"mm/day"`` unit string
:func:`core.forcing.inspect_netcdf`/:func:`core.forcing.netcdf_to_binary` already recognise --
nothing downstream of this module needs to change.

**Two things a real CDS request (2026-09-22) revealed that no amount of reading the documentation
did:** its current infrastructure can return a *zip archive* containing one ``.nc`` file even for
``"format": "netcdf"`` (:func:`_unwrap_if_zip` handles it), and names its coordinates
``valid_time``/``latitude``/``longitude`` rather than ``time``/``lat``/``lon``
(:func:`era5_land_daily_totals` checks both). GLDAS's request shape and variable names
(``Qs_tavg``/``Qsb_tavg``) remain unverified against a live response.

``cdsapi``/``earthaccess``/``earthengine-api`` are all optional (the ``download`` extra); each
raises a clear, actionable error rather than failing at import time if missing, matching how
:mod:`core.gis`/:mod:`core.forcing` guard their own optional imports. Credentials are never
stored or read by this app -- all three libraries already read their own well-known files
(``~/.cdsapirc``, ``~/.netrc``, ``~/.config/earthengine/credentials``), and
:func:`credentials_status` only checks whether those files exist, never their contents.

**Google Earth Engine, as a fourth access path for the same two datasets** (``ECMWF/ERA5_LAND
/HOURLY`` and ``NASA/GLDAS/V021/NOAH/G025/T3H``): checked directly against the real GEE catalog
pages and API docs before building this, not assumed. Two things make it genuinely simpler than
the raw CDS/GES DISC APIs, and one thing makes it more complex:

* Earth Engine's own ``runoff_hourly`` band is *already* the hourly increment (not the raw
  cumulative total CDS returns), and GLDAS's ``Qs_acc``/``Qsb_acc`` bands are already 3-hour
  accumulated depths in mm -- no day-boundary handling and no rate-to-depth conversion needed;
  summing a day's images is the whole aggregation step.
* ``ee.Image.getDownloadURL`` with an explicit ``crsTransform``/``dimensions`` (rather than
  ``region``/``scale``) makes Earth Engine sample exactly onto the routing grid's own pixel edges
  *server-side* -- alignment is guaranteed by construction, not just checked afterward. It is
  also a plain synchronous HTTP GET (capped at 32 MB / 10000 px per call, comfortably inside a
  regional domain), not Earth Engine's asynchronous Export-to-Drive batch flow.
* The complexity: ``ee.Initialize()`` now requires an explicit Google Cloud Project ID with no
  equivalent credential file, so one is kept separately in
  ``~/.camafloodcast/gee_project.txt`` (:func:`get_gee_project`/:func:`save_gee_project`) --
  the same non-secret local-settings convention already used for saved colour presets, or an
  ``EARTHENGINE_PROJECT`` environment variable.

No live Earth Engine account exists in this development environment, so unlike the ERA5-Land CDS
path (verified against a real request), the GEE paths are exercised in tests only against a
mocked ``ee`` module -- worth a first real check against an actual account.
"""

from __future__ import annotations

import datetime as dt
import netrc
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from .errors import FileFormatError

Source = Literal["era5-land", "gldas", "era5-land-gee", "gldas-gee"]

_CDSAPI_HINT = "pip install -e \".[download]\"  (needs cdsapi; also needs the 'netcdf' extra)"
_EARTHACCESS_HINT = "pip install -e \".[download]\"  (needs earthaccess; also needs 'netcdf')"
_GEE_HINT = "pip install -e \".[download]\"  (needs earthengine-api; also needs the 'geo' extra)"
_NETCDF_HINT = "pip install -e \".[netcdf]\"  (needs netCDF4)"

#: NASA Earthdata Login host, as it appears in a working ~/.netrc entry.
EARTHDATA_HOST = "urs.earthdata.nasa.gov"

#: Earth Engine collection IDs and the runoff-related bands each one carries.
ERA5_LAND_GEE_COLLECTION = "ECMWF/ERA5_LAND/HOURLY"
GLDAS_GEE_COLLECTION = "NASA/GLDAS/V021/NOAH/G025/T3H"

ProgressFn = Callable[[int, int], None]


def gee_project_path(*, home: Path | None = None) -> Path:
    home = home if home is not None else Path.home()
    return home / ".camafloodcast" / "gee_project.txt"


def get_gee_project(*, home: Path | None = None) -> str | None:
    """The Earth Engine Cloud Project ID to initialise with -- ``EARTHENGINE_PROJECT`` first
    (matching how every other environment-driven override in this app works), then the small
    local settings file :func:`save_gee_project` writes. Never a secret -- just which project's
    quota Earth Engine calls are billed against."""
    import os

    env = os.environ.get("EARTHENGINE_PROJECT")
    if env:
        return env
    p = gee_project_path(home=home)
    if p.is_file():
        text = p.read_text(encoding="utf-8").strip()
        return text or None
    return None


def save_gee_project(project_id: str, *, home: Path | None = None) -> None:
    p = gee_project_path(home=home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(project_id.strip() + "\n", encoding="utf-8")


@dataclass(frozen=True)
class DownloadRequest:
    """What to fetch: a region (the project's own routing grid), a period, and a source."""

    source: Source
    west: float
    south: float
    east: float
    north: float
    gsize: float
    start: dt.date
    end: dt.date

    def __post_init__(self):
        if self.end < self.start:
            raise ValueError("end date is before start date")
        if self.east <= self.west or self.north <= self.south:
            raise ValueError("the region has zero or negative extent")

    def dates(self) -> list[dt.date]:
        n = (self.end - self.start).days + 1
        return [self.start + dt.timedelta(days=i) for i in range(n)]


def credentials_status(*, home: Path | None = None) -> dict[str, bool]:
    """Whether the credential files ``cdsapi``/``earthaccess``/Earth Engine already expect are
    present -- and, for Earth Engine, whether a project id is also configured, since it has no
    equivalent credential file.

    Existence only -- this never opens a credential file for its contents beyond what Python's
    own ``netrc`` parser needs to find the right host entry, and never logs or returns a
    key/token. The two Earth Engine sources share one login, so they map to the same result.
    """
    home = home if home is not None else Path.home()
    status = {"era5-land": (home / ".cdsapirc").is_file(), "gldas": False}
    netrc_path = home / ("_netrc" if _is_windows() else ".netrc")
    if netrc_path.is_file():
        try:
            parsed = netrc.netrc(str(netrc_path))
            status["gldas"] = EARTHDATA_HOST in parsed.hosts
        except (netrc.NetrcParseError, OSError):
            status["gldas"] = False

    gee_ready = ((home / ".config" / "earthengine" / "credentials").is_file()
                and get_gee_project(home=home) is not None)
    status["gee"] = gee_ready
    status["era5-land-gee"] = gee_ready
    status["gldas-gee"] = gee_ready
    return status


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"


# --------------------------------------------------------------------------- shared netCDF I/O


def _write_daily_netcdf(out_path: Path, daily: np.ndarray, lat: np.ndarray, lon: np.ndarray,
                        dates: list[dt.date]) -> None:
    """One 3-D ``runoff`` variable in mm/day, ``lat``/``lon``/``time`` coordinates -- the exact
    shape ``core.forcing.inspect_netcdf``/``netcdf_to_binary`` already read."""
    import netCDF4 as nc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds = nc.Dataset(out_path, "w", format="NETCDF4")
    try:
        ds.createDimension("time", len(dates))
        ds.createDimension("lat", len(lat))
        ds.createDimension("lon", len(lon))

        v_lat = ds.createVariable("lat", "f8", ("lat",))
        v_lat[:] = np.asarray(lat, dtype="f8")
        v_lon = ds.createVariable("lon", "f8", ("lon",))
        v_lon[:] = np.asarray(lon, dtype="f8")

        base = dates[0]
        v_time = ds.createVariable("time", "f8", ("time",))
        v_time.units = f"days since {base:%Y-%m-%d}"
        v_time.calendar = "standard"
        v_time[:] = [(d - base).days for d in dates]

        v_runoff = ds.createVariable("runoff", "f4", ("time", "lat", "lon"))
        v_runoff.units = "mm/day"
        v_runoff[:] = daily.astype("f4")
    finally:
        ds.close()


def _group_indices_by_date(times: list[dt.datetime]) -> dict[dt.date, list[int]]:
    groups: dict[dt.date, list[int]] = {}
    for i, t in enumerate(times):
        groups.setdefault(t.date(), []).append(i)
    return groups


def _unwrap_if_zip(path: Path) -> None:
    """CDS's current infrastructure sometimes delivers a zip archive containing one netCDF file
    even when ``format: netcdf`` is requested -- confirmed against a real download, not
    documented anywhere obvious. Replaces ``path`` in place with the archive's own netCDF file
    when that happens; a no-op otherwise."""
    if not zipfile.is_zipfile(path):
        return
    with zipfile.ZipFile(path) as zf:
        members = [m for m in zf.namelist() if m.lower().endswith((".nc", ".nc4"))]
        if len(members) != 1:
            raise FileFormatError(
                path, f"expected exactly one netCDF file inside the CDS zip, found {members}")
        with zf.open(members[0]) as src, tempfile.NamedTemporaryFile(
                delete=False, dir=path.parent, suffix=".nc") as tmp:
            shutil.copyfileobj(src, tmp)
            tmp_path = Path(tmp.name)
    tmp_path.replace(path)


# --------------------------------------------------------------------------- ERA5-Land


def era5_land_download(request: DownloadRequest, out_dir: Path | str, *,
                       progress: ProgressFn | None = None) -> Path:
    """One CDS request for the whole period, pre-regridded to the project's own routing-grid
    cell size and cropped to its bounds -- so the result lands already aligned and
    ``netcdf_to_binary``'s existing same-grid windowing needs no changes to read it."""
    try:
        import cdsapi
    except ImportError as exc:
        raise FileFormatError(out_dir, "downloading ERA5-Land needs the 'download' extra",
                              hint=_CDSAPI_HINT) from exc

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "era5_land_hourly_runoff.nc"

    dates = request.dates()
    years = sorted({f"{d.year:04d}" for d in dates})
    months = sorted({f"{d.month:02d}" for d in dates})
    days = sorted({f"{d.day:02d}" for d in dates})

    if progress:
        progress(0, 1)
    client = cdsapi.Client()
    client.retrieve(
        "reanalysis-era5-land",
        {
            "variable": "runoff",
            "year": years,
            "month": months,
            "day": days,
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": [request.north, request.west, request.south, request.east],
            "grid": f"{request.gsize}/{request.gsize}",
            "format": "netcdf",
        },
        str(target),
    )
    _unwrap_if_zip(target)
    if progress:
        progress(1, 1)
    return target


def era5_land_daily_totals(raw_path: Path | str, out_path: Path | str) -> int:
    """Collapse an hourly ERA5-Land runoff file into one calendar-day total per day, in mm/day.

    Each day's total is its own last (chronologically latest) hourly sample -- the series
    accumulates monotonically within a UTC day and resets near the day boundary, so the last
    sample already equals the full day's sum regardless of exactly how many hours the reset
    itself spans (see the module docstring). Returns the number of days written.
    """
    try:
        import netCDF4 as nc
    except ImportError as exc:
        raise FileFormatError(raw_path, "reading netCDF needs the 'netcdf' extra",
                              hint=_NETCDF_HINT) from exc

    p = Path(raw_path)
    ds = nc.Dataset(p)
    try:
        var_name = next((v for v in ("ro", "runoff") if v in ds.variables), None)
        if var_name is None:
            raise FileFormatError(p, "no 'ro'/'runoff' variable in this ERA5-Land file")
        # CDS's current infrastructure names these valid_time/latitude/longitude; older-style
        # downloads (and this module's own daily-total output) use time/lat/lon -- confirmed
        # both exist in the wild against a real download, not documented anywhere obvious.
        time_name = next(v for v in ("time", "valid_time") if v in ds.variables)
        lat_name = next(v for v in ("lat", "latitude") if v in ds.variables)
        lon_name = next(v for v in ("lon", "longitude") if v in ds.variables)

        tvar = ds.variables[time_name]
        times = nc.num2date(tvar[:], tvar.units, getattr(tvar, "calendar", "standard"),
                            only_use_cftime_datetimes=False, only_use_python_datetimes=True)
        groups = _group_indices_by_date(list(times))
        unique_dates = sorted(groups)

        lat = np.asarray(ds.variables[lat_name][:])
        lon = np.asarray(ds.variables[lon_name][:])
        data = np.asarray(ds.variables[var_name][:])       # (ntime, ny, nx), metres
        if data.ndim == 4:
            data = data[:, 0]                              # drop a length-1 'number' axis

        daily = np.empty((len(unique_dates), data.shape[1], data.shape[2]), dtype="f4")
        for di, d in enumerate(unique_dates):
            last = max(groups[d])
            daily[di] = np.ma.filled(np.ma.masked_invalid(data[last]), 0.0) * 1000.0  # m -> mm
        np.maximum(daily, 0.0, out=daily)
    finally:
        ds.close()

    _write_daily_netcdf(Path(out_path), daily, lat, lon, unique_dates)
    return len(unique_dates)


# --------------------------------------------------------------------------- GLDAS


def gldas_download(request: DownloadRequest, out_dir: Path | str, *,
                   progress: ProgressFn | None = None) -> list[Path]:
    """Search and download GLDAS Noah 3-hourly granules for the period.

    ``earthaccess`` has no server-side regional subsetting for this collection yet, so every
    returned file covers the whole globe at GLDAS's native 0.25 deg -- cropping happens locally
    in :func:`gldas_daily_totals`.
    """
    try:
        import earthaccess
    except ImportError as exc:
        raise FileFormatError(out_dir, "downloading GLDAS needs the 'download' extra",
                              hint=_EARTHACCESS_HINT) from exc

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    earthaccess.login(strategy="netrc")
    results = earthaccess.search_data(
        short_name="GLDAS_NOAH025_3H", version="2.1",
        temporal=(request.start.isoformat(), request.end.isoformat()),
        bounding_box=(request.west, request.south, request.east, request.north),
    )
    if progress:
        progress(0, len(results))
    paths = earthaccess.download(results, str(out))
    if progress:
        progress(len(results), len(results))
    return [Path(p) for p in paths]


def gldas_daily_totals(raw_paths: list[Path], out_path: Path | str,
                       request: DownloadRequest) -> int:
    """Sum Qs + Qsb across each day's 8 three-hourly files, in mm/day, cropped to ``request``.

    Tries the LIS flux-naming convention (``Qs_tavg``/``Qsb_tavg``) first, falling back to
    ``Qs``/``Qsb`` -- unverified against a live downloaded file, see the module docstring.
    """
    try:
        import netCDF4 as nc
    except ImportError as exc:
        raise FileFormatError(out_path, "reading netCDF needs the 'netcdf' extra",
                              hint=_NETCDF_HINT) from exc

    if not raw_paths:
        raise FileFormatError(out_path, "no GLDAS files to aggregate")

    lat_crop = lon_crop = None
    lat = lon = None
    per_day: dict[dt.date, np.ndarray] = {}

    for p in sorted(raw_paths):
        ds = nc.Dataset(Path(p))
        try:
            surf = next((v for v in ("Qs_tavg", "Qs") if v in ds.variables), None)
            sub = next((v for v in ("Qsb_tavg", "Qsb") if v in ds.variables), None)
            if surf is None or sub is None:
                raise FileFormatError(p, "no Qs/Qsb surface-runoff variables in this GLDAS file")

            if lat is None:
                lat = np.asarray(ds.variables["lat"][:])
                lon = np.asarray(ds.variables["lon"][:])
                lat_crop = np.where((lat >= request.south) & (lat <= request.north))[0]
                lon_crop = np.where((lon >= request.west) & (lon <= request.east))[0]
                if lat_crop.size == 0 or lon_crop.size == 0:
                    raise FileFormatError(p, "the requested region falls outside this GLDAS file")

            tvar = ds.variables["time"]
            t = nc.num2date(tvar[:], tvar.units, getattr(tvar, "calendar", "standard"),
                            only_use_cftime_datetimes=False, only_use_python_datetimes=True)
            day = t[0].date() if hasattr(t, "__len__") else t.date()

            def crop(v):
                arr = np.asarray(ds.variables[v][:])
                arr = arr[0] if arr.ndim == 3 else arr        # drop a length-1 time axis
                return arr[np.ix_(lat_crop, lon_crop)]

            rate = crop(surf) + crop(sub)                      # kg/m2/s = mm/s
            mm_this_step = np.ma.filled(np.ma.masked_invalid(rate), 0.0) * 3 * 3600.0

            per_day.setdefault(day, np.zeros_like(mm_this_step))
            per_day[day] += mm_this_step
        finally:
            ds.close()

    unique_dates = sorted(per_day)
    daily = np.stack([per_day[d] for d in unique_dates]).astype("f4")
    np.maximum(daily, 0.0, out=daily)
    _write_daily_netcdf(Path(out_path), daily, lat[lat_crop], lon[lon_crop], unique_dates)
    return len(unique_dates)


# --------------------------------------------------------------------------- Google Earth Engine


def _gee_grid_axes(request: DownloadRequest) -> tuple[list[float], list[float], int, int]:
    """lat/lon cell-centre coordinates and (ny, nx) for ``request``'s bounds/gsize -- the same
    grid ``crsTransform``/``dimensions`` will make Earth Engine sample onto server-side."""
    nx = int(round((request.east - request.west) / request.gsize))
    ny = int(round((request.north - request.south) / request.gsize))
    lon = [request.west + (i + 0.5) * request.gsize for i in range(nx)]
    lat = [request.north - (i + 0.5) * request.gsize for i in range(ny)]
    return lat, lon, ny, nx


def _gee_initialize() -> None:
    try:
        import ee
    except ImportError as exc:
        raise FileFormatError("earth-engine", "using Google Earth Engine needs the "
                                              "'download' extra", hint=_GEE_HINT) from exc

    project = get_gee_project()
    if not project:
        raise FileFormatError(
            "earth-engine", "no Earth Engine Cloud Project configured",
            hint="set it once via the Download forcing dialog, save_gee_project(), or the "
                 "EARTHENGINE_PROJECT environment variable")
    ee.Initialize(project=project)


def _gee_day_image(collection_id: str, bands: list[str], day: dt.date):
    import ee

    next_day = day + dt.timedelta(days=1)
    col = (ee.ImageCollection(collection_id)
          .filterDate(day.isoformat(), next_day.isoformat())
          .select(bands))
    return col.sum()


def _gee_fetch_daily(request: DownloadRequest, out_dir: Path | str, *, collection_id: str,
                     bands: list[str], combine: Callable[[np.ndarray], np.ndarray],
                     progress: ProgressFn | None = None) -> Path:
    """Shared driver for both Earth Engine sources: sum the requested bands per UTC day
    server-side, download each day as a small GeoTIFF sampled exactly onto ``request``'s grid,
    and write the same daily-total netCDF convention every other source in this module writes.

    Earth Engine fuses what the CDS/GES DISC paths do in two separate steps (download, then
    aggregate) into one, since the per-day sum happens server-side before anything is fetched.
    """
    try:
        import rasterio
    except ImportError as exc:
        raise FileFormatError(out_dir, "reading a GeoTIFF needs the 'geo' extra",
                              hint="pip install -e \".[geo]\"") from exc

    _gee_initialize()
    import urllib.request

    lat, lon, ny, nx = _gee_grid_axes(request)
    crs_transform = [request.gsize, 0, request.west, 0, -request.gsize, request.north]
    dates = request.dates()
    n = len(dates)

    daily = np.empty((n, ny, nx), dtype="f4")
    for i, day in enumerate(dates):
        if progress:
            progress(i, n)
        image = _gee_day_image(collection_id, bands, day)
        url = image.getDownloadURL({
            "crs": "EPSG:4326",
            "crsTransform": crs_transform,
            "dimensions": f"{nx}x{ny}",
            "format": "GEO_TIFF",
        })
        with urllib.request.urlopen(url) as resp:                # noqa: S310 (GEE-signed URL)
            tif_bytes = resp.read()
        with rasterio.io.MemoryFile(tif_bytes) as mem, mem.open() as src:
            band = np.asarray(src.read(), dtype="f8")             # (nbands, ny, nx)
        daily[i] = combine(band)
    if progress:
        progress(n, n)
    np.maximum(daily, 0.0, out=daily)

    out_path = Path(out_dir) / f"{collection_id.replace('/', '_')}_daily.nc"
    _write_daily_netcdf(out_path, daily, lat, lon, dates)
    return out_path


def era5_land_gee_download(request: DownloadRequest, out_dir: Path | str, *,
                           progress: ProgressFn | None = None) -> Path:
    """ERA5-Land runoff via Earth Engine: ``runoff_hourly`` is already the hourly increment
    (metres), so a day's sum needs no accumulation-reset handling at all."""
    return _gee_fetch_daily(
        request, out_dir, collection_id=ERA5_LAND_GEE_COLLECTION, bands=["runoff_hourly"],
        combine=lambda band: band[0] * 1000.0,                    # m -> mm
        progress=progress)


def gldas_gee_download(request: DownloadRequest, out_dir: Path | str, *,
                       progress: ProgressFn | None = None) -> Path:
    """GLDAS runoff via Earth Engine: ``Qs_acc``/``Qsb_acc`` are already 3-hourly accumulated
    depths in mm, so a day's sum of both bands needs no rate-to-depth conversion."""
    return _gee_fetch_daily(
        request, out_dir, collection_id=GLDAS_GEE_COLLECTION, bands=["Qs_acc", "Qsb_acc"],
        combine=lambda band: band.sum(axis=0),                    # Qs_acc + Qsb_acc, already mm
        progress=progress)
