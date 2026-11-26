# CaMa_Floodcast

**Version 1.0** · A desktop application for the
[CaMa-Flood](https://github.com/global-hydrodynamics/CaMa-Flood_v4) v4.30 global river
hydrodynamic model, with an interface modelled on RRI_Floodcast.

CaMa_Floodcast takes a CaMa-Flood study from domain to results in one window: cut a regional
domain, prepare and check the runoff forcing, edit the namelist, run the model, and explore the
output on a map, as hydrographs and as scenario comparisons. It drives the real CaMa-Flood
model and Fortran tools rather than re-implementing them. Every file it touches is the ordinary
CaMa-Flood file you would edit by hand: the namelist, the map directory, the forcing files and
the `.bin` results.

Developed by **Nguyen Xuan TINH (Ph.D.)** · © 2026 Nippon Koei Co.,Ltd. Water Resources & Energy
Dept.

## Features

| Area | What you can do |
|---|---|
| **Domain** | Cut a regional domain from a larger map with the full CaMa-Flood tool chain (`cut_domain` → … → `set_bifparam`). **Gate 1** checks basin closure, so a cut that severs a river from its headwaters is caught before it biases discharge. Build a floodplain-roughness map (`CFLDMAN`) from land cover, including GeoTIFFs in any coordinate system. |
| **Forcing** | Import binary or netCDF runoff, or download ERA5-Land or GLDAS directly (Copernicus CDS, NASA GES DISC or Google Earth Engine). **Gate 2** audits units, missing days and latitude order. |
| **Configuration** | Edit the full namelist with validation and tooltips, plus an explained Physics tab for bifurcation, levees, dams, sea level, the adaptive time step and roughness. Create parameter sweeps that vary one field across several cases. |
| **Running** | Run cases from a job queue with pre-flight validation, a live log and a live water-balance monitor. Results that no longer match their inputs are flagged as stale. |
| **Results** | Map and probe output over Leaflet basemaps (street, satellite, topographic), with editable colour scales. Draw gauge hydrographs and export them to CSV or PNG. |
| **Analysis** | Compare scenarios with A − B difference maps and paired hydrographs (NSE, KGE, PBIAS). Trace longitudinal river profiles, and see levee overtopping statistics. |
| **Downscaling** | Map 1-arcminute inundation depth for one day or a period (maximum-depth composite and area/volume series), with GeoTIFF, PNG and CSV export. |

## Validated against real data

The 299 automated tests use real data rather than synthetic fixtures. They include:

- a full simulation run through the GUI's own execution path;
- regional domains cut from the real global map with the real Fortran tools;
- downscaling of a 365-day Mekong run onto the real 1-arcminute sub-grid;
- a reservoir run with 5 real dams that shifts downstream discharge in the expected direction (higher dry-season flow, lower flood peak);
- a levee run whose protected side overtops in the wet season and stays dry otherwise.

The Gate 1 check reproduces the published closure deficits for the Mekong regional cut exactly
(Mekong 0.0%, Irrawaddy 0.0%, Salween 10.2%, upper Yangtze 15.3%, Brahmaputra 51.0%). It also
correctly blocks a Vietnam-shaped cut, which truncates the Mekong (40.6%) and the Red River
(34.9%).

## Example projects

Four ready-to-open projects, with their results, river maps and forcing, are available as
**[CaMa_Floodcast_examples_1.0.zip](https://github.com/xuantinhsea/CaMa-Flood_v4_GUI/releases/download/v1.0/CaMa_Floodcast_examples_1.0.zip)**
(189 MB, from the [v1.0 release](https://github.com/xuantinhsea/CaMa-Flood_v4_GUI/releases/tag/v1.0)).
The Windows installer includes the same projects. Unzip anywhere, keeping `projects\`, `map\` and
`inp\` side by side, then choose **File → Open project** and pick a folder under `projects\`:

- **Mekong**: a full year, 5,528 river cells, 268 bifurcation paths, and gauges at Kratie, Pakse and Chiang Saen. It has four cases:
  - `baseline`;
  - `no_bifurcation`;
  - `with_dams`: 5 GRanD reservoirs on the Lancang/upper Mekong;
  - `with_levee`: an illustrative levee at the cell that floods deepest in the baseline run.
- **Vietnam (national)**: a deliberately truncated cut that demonstrates Gate 1.
- **Philippines (national)**: a hydrologically closed cut.
- **Cagayan**: a small catchment-scale cut.

From source, the Mekong project is rebuilt by `examples/make_mekong_project.py`. Its three extra
cases come from `make_mekong_scenario.py`, `make_mekong_dams.py` and `make_mekong_levee.py`.

## Documentation

The [User manual](USER_MANUAL.md) covers installation, the example projects and each tab in
turn. It is also available as [Word](USER_MANUAL.docx) and [PDF](USER_MANUAL.pdf). The sections
below are developer notes on running from source, building the installer, the architecture and
testing.

---

## Run it

```bash
pip install -e ".[gui]"
python -m camafloodcast

# or go straight to an existing setup
python -m camafloodcast --import /path/to/input_cmf.nam

# optional: GeoTIFF land-cover import (any CRS, auto-reprojected) needs the geo extra too
pip install -e ".[gui,geo]"

# optional: download ERA5-Land/GLDAS runoff forcing directly from the Forcing tab
pip install -e ".[gui,netcdf,download]"
```

**Standalone .exe (Windows):** double-click `build_exe.bat` (or `build_exe.bat debug` for a
variant that keeps a console open for tracebacks). It freezes the app with PyInstaller into
`dist\CaMa_Floodcast\CaMa_Floodcast.exe`, using the `python` on `PATH` (override with
`set PYTHON=...`); PyInstaller is installed if missing, the app's own dependencies must already
be. Optional extras (`geo`, `netcdf`, `download`) are bundled when installed, and the exe falls
back for any that are not exactly as the source does. The output is a folder, not a single
file -- move or zip the whole folder, and keep it at a path under ~170 characters (Windows
won't load its deepest DLLs past the 260-character path limit). `bin\win64` is copied into it; `MAIN_cmf.exe` is found in
`..\src` while the folder stays inside this package, otherwise set it once via **Simulation →
Model binary**. The icon (`camafloodcast/resources/camafloodcast.ico`) is drawn by
`python packaging/make_icon.py`.

**Windows installer:** double-click `build_installer.bat` (or `build_installer.bat noexe` to
reuse an existing `dist\CaMa_Floodcast`). It freezes the app via `build_exe.bat`, regenerates
`USER_MANUAL.docx` from `USER_MANUAL.md` (`packaging/md_to_docx.py`, then
`packaging/update_docx_fields.ps1` to fill in its contents page when Word is installed), stages
the example projects with `packaging/stage_installer.py`, and compiles
`packaging/CaMa_Floodcast.iss` with Inno Setup 6 into
`dist\installer\CaMa_Floodcast_Setup_<version>.exe`. Staging ships cleaned copies of the
Mekong, Vietnam, Philippines and Cagayan projects (no backups, run logs or download leftovers)
together with the maps and forcing they use and the MinGW runtime DLLs `MAIN_cmf.exe` needs, and
rewrites every absolute path in them to a relative one, so the installed workspace works
wherever the user puts it. The installer is per-user by default (no admin rights), installs the
examples into a writable workspace folder (`%USERPROFILE%\CaMa_Floodcast` by default) that
upgrades never overwrite and uninstalling never removes, and adds a *Mekong example* Start Menu
shortcut. The `global` project is not shipped (~10 GB with its map).

The fastest way in for someone who already runs CaMa-Flood by hand:
**File → Import CaMa-Flood namelist**. It creates a project, brings any existing `.bin`
results along, and opens them in the viewer.

The `gui` extra includes `PyQt6-WebEngine`, which the map's real basemap needs (see "The map
interface" below) -- genuinely optional, though: without it every map tab falls back to the
native Qt renderer automatically (no basemap, everything else unchanged), rather than failing
to start.

## What it does

| Tab | Capability |
|---|---|
| **Project** | Projects and cases; case table with **stale-result detection**; **New sweep...** builds N cases that vary exactly one namelist field, identical otherwise (each gets a distinguishing `COUTTAG` so a later comparison never picks up a stray difference) |
| **Domain Builder** | Full 7-step cut chain (`cut_domain` → … → `set_bifparam`) against the real global map, queued with per-step status; **domain closure check (Gate 1)**, coloured by outlet deficit; land cover → floodplain roughness (`CFLDMAN`) via an editable IGBP lookup table, from either CaMa's own pre-gridded binary or a real-world **GeoTIFF in any CRS** (auto-detected and reprojected onto the domain -- `core/gis.py`) |
| **Forcing** | Import/inspect runoff forcing, unit assistant, latitude-order and coverage audit (**Gate 2**), map preview of the forcing field over the routing domain; **download ERA5-Land or GLDAS runoff directly** (`core/forcing_download.py`) -- collapses either product's sub-daily data into a daily total, then runs it through the exact same conversion and Gate 2 audit as a manually-imported netCDF file |
| **Model Setup** | Schema-driven namelist editor, dual form/raw view, live validation — all 10 namelist groups |
| **Physics** | Explained controls for bifurcation, levees, dams, sea level, the adaptive time step and floodplain roughness — edits the same namelist as Model Setup (no divergent copy); bifurcation-channel map and levee-map statistics read from the real files, with guidance on the CLEVFRC/CLEVHGT interaction below; a **reservoir table** reading an existing `CDAMFILE` (name, lat/lon, capacity, Qn/Qf) |
| **Run Manager** | Job queue, `QProcess` execution, live log, **water-balance monitor** |
| **Map & Results** | A real basemap (Street/Satellite/Topographic/Gray/none, Street by default and auto-zoomed to the project's domain -- see "The map interface"), a wide layer stack with per-layer opacity/colour/reorder controls and its own colour-scale editor, time slider, click-to-probe, gauge hydrographs, CSV/PNG export; the variable picker and every layer/probe label show the explicit name (`Floodplain water depth`), not the bare short code (`flddph`) -- see `core.output.display_name`; recognises the levee scheme's `levdph`/`levsto` outputs and shows protected-side overtopping stats (fraction of records overtopped, when, and how deep) when plotting `levdph` |
| **Analysis** | Scenario comparison: A − B difference maps (percentile colour range, so one outlier cell can't wash out the rest) and paired hydrographs at gauges loaded from `analysis/gauges.csv`, with goodness-of-fit metrics (NSE/KGE/PBIAS) reused to describe how far a scenario departs from its reference; **longitudinal profiles** tracing bed elevation and water surface from a gauge to the river mouth (works from a single case — no B needed) |
| **Downscaling** | Redistributes routing-grid flood depth onto the real 1-arcmin sub-grid (`downscale_flddph_trib`) for a chosen window — one day, or a batch over a period (per-day area/volume time series plotted on a dual-axis chart, plus a maximum-depth composite map); **Fit to domain** sets the window from the project's own bounds instead of a generic default; export the result as a georeferenced **GeoTIFF** (`core.gis.write_geotiff`, opens directly in QGIS/ArcGIS), a **map PNG**, or the period's **area/volume CSV**; a permanent framing note that this is a diagnostic redistribution, not a re-route |

Plus a **workflow strip** (Domain → Forcing → Config → Run → Results) that shows where you are
and what is blocking you, and a **validation pane** that lists problems as a clickable list —
double-click jumps to the offending namelist field.

## The map interface

All six map-bearing tabs (Map & Results, Domain Builder, Forcing, Physics, Downscaling, Analysis)
share the same `MapLayer`/`ColorSpec` model (`services/render.py`, `ui/map/renderer.py`) behind
two interchangeable renderers, chosen once by `ui/map/make_map_view()` rather than per tab:

- **`ui/map/leaflet.py`** (the default when `PyQt6-WebEngine` is installed): Leaflet.js in a
  `QWebEngineView`, talking to Python over `QWebChannel`. Real basemaps (OpenStreetMap street,
  Esri satellite, OpenTopoMap, Esri gray, or none -- all free, keyless), an on-map colour-scale
  legend, and the same double-click-to-probe / hover-coordinate behaviour as the native renderer.
  **Street is the default** basemap on all six tabs, and each tab zooms to its own domain/layer
  extent as soon as real data is available -- a different basemap or "No Base Map" is one click
  away in the layer switcher.
- **`ui/map/native.py`** (the fallback, and the whole map stack's original implementation): a
  `QGraphicsView` painting georeferenced raster layers directly. No basemap, no extra
  dependency, works everywhere `PyQt6` does.

Both renderers colour every layer through the same `services.render.colorize()`, including a
new **discrete named-preset system** alongside the existing continuous-colormap path:
`COLOR_PRESETS` (a handful of hex colours at fixed break-values -- "orange starts at 1.0 m",
not a smooth ramp) with presets for depth, discharge, a diverging scale for Analysis-tab
difference maps, and more, each editable and savable via **`ui/widgets/color_scale_editor.py`**
(global presets in `~/.camafloodcast/color_presets.json`, or per-project in
`<project>/cache/`). The **Layer Panel** (`ui/widgets/controls.py`) shows one always-visible row
per layer -- visibility, opacity, a combined colormap/preset picker, an "edit colour scale"
button, reorder and remove -- instead of one style panel shared by whichever layer happened to
be selected.

Both renderers also share a bounded render cache (`native._FrameCache`, keyed by layer name,
record and a fingerprint of everything in `ColorSpec` that affects the pixels) so scrubbing the
time bar back to an already-shown record, or the Downscaling tab replaying the same
maximum-depth composite, reuses the rendered frame instead of recolouring and re-encoding it.

GeoTIFF import (`core/gis.py`, the `geo` extra) is the map-adjacent half of this: detect a
raster's real CRS and reproject it onto the routing grid, currently wired into the Domain
Builder's land-cover picker (any `.tif`/`.tiff`, not just CaMa's own pre-gridded binary).

**Three bugs found while building this:**

1. `LayerPanel.add_layer()` inserts a new layer at index 0 (the "top of the stack" by its own
   Up/Down convention), but `NativeMapView.refresh()`'s z-value was a bare `enumerate()` index --
   the *lowest*. A newly-added variable landed *underneath* the base layer's own ~90%-opaque
   fill, softening exactly the layer a reader just asked to see. Fixed in `native.py` (`zValue =
   n - i`) before porting the same ordering into the Leaflet renderer, which would otherwise
   have inherited the inversion.
2. `PyQt6-WebEngine` must be imported, or `Qt.ApplicationAttribute.AA_ShareOpenGLContexts` set,
   **before** the `QApplication` is constructed -- importing it lazily inside a tab (as
   `make_map_view()` does, deliberately, so a user without the WebEngine extra never pays for
   loading it) hits this after the fact and silently falls back to the native renderer with no
   error. `ui/app.py` sets the attribute right before constructing `QApplication`; the test
   suite's own `qapp` fixture needed the identical fix for the same reason.
3. `zoom_to()`/`zoom_to_layers()` called while a tab is not the active one hits a `QWebEngineView`
   sized zero, so Leaflet's `fitBounds()` computes the whole-world zoom instead of the intended
   extent -- and the existing `showEvent` fix for stale overlay positions (`invalidateSize()`)
   doesn't correct the zoom level, only the tile rendering at whatever (wrong) zoom it already
   picked. Reproduced on Domain Builder, which sets up and zooms its map before that tab is ever
   shown: the domain rectangle appeared as a speck on a whole-world view instead of the map
   zooming to it. Fixed by remembering the last requested bounds and re-issuing `fitBounds()`
   from `showEvent` -- but only when that specific `zoom_to()` call happened while hidden, so it
   never overrides a pan/zoom the user made by hand after the tab was first shown.

## Downloading forcing data

**Forcing menu → Download forcing...** (`core/forcing_download.py`) fetches runoff directly from
one of **four sources** -- ERA5-Land or GLDAS Noah, each reachable either via its own API
(Copernicus CDS / NASA GES DISC) or via Google Earth Engine -- rather than requiring a file
downloaded by hand first. It's a menu action rather than a panel on the Forcing tab so it's
reachable without navigating there first; clicking it opens a standalone dialog
(`ForcingView._download_dialog`/`open_download_dialog()`), non-modal so it can sit alongside the
rest of the app while a download runs on its own background thread. This is the first feature in
the app that makes real network calls -- everything else only ever reads local files.

The dialog makes every input explicit and editable rather than silently reusing state from
elsewhere:
- **Model domain** -- West/East/South/North/Resolution, auto-filled from the project's own
  routing grid whenever a project opens, with a **Fit to domain** button to reset them on demand
  (the same button and behaviour as the Downscaling tab's own). Editable, so a smaller test
  region is one edit away.
- **Duration** -- its own Start/End dates, seeded from the Forcing tab's own period each time the
  dialog is opened, editable independently afterward without disturbing Gate 2's own period.
- **Output** -- a persistent directory field with a browse button (matching "Import from
  netCDF"'s own convention), and a **"Convert to CaMa binary forcing after download"** checkbox
  (checked by default) -- unchecked, the pipeline stops at the daily-total netCDF, useful for a
  custom domain that doesn't match the routing grid, or just to inspect the file first.
- Three status labels ("Fetch", "Aggregate to daily", "Convert to binary") light up live as a
  download progresses.

Neither product is natively daily, which is more than a format conversion, and the two access
paths solve that differently:
- **ERA5-Land's own API**: `runoff` is hourly-*accumulated*, resetting near each 00 UTC boundary.
  CDS's own daily-statistics dataset explicitly excludes accumulated variables like runoff, so
  there is no shortcut -- a day's total is computed here as that day's own last (chronologically
  latest) hourly sample, correct regardless of exactly how many hours the reset spans, since the
  series is monotonically non-decreasing within a day and resets near the boundary (a telescoping
  sum).
- **GLDAS's own API** (`GLDAS_NOAH025_3H`, 0.25°): ships 3-hourly, split into `Qs` (surface) and
  `Qsb` (subsurface baseflow) in kg/m2/s -- summed across each day's 8 files here.
- **Both via Google Earth Engine**: checked directly against the real GEE catalog pages and API
  docs before building this. Earth Engine's `runoff_hourly` band is *already* the hourly
  increment (not the raw cumulative total the CDS API returns), and its `Qs_acc`/`Qsb_acc` bands
  are already 3-hour accumulated depths in mm -- summing a day's images is the whole aggregation
  step, no day-boundary handling or rate-to-depth conversion needed. `ee.Image.getDownloadURL`
  with an explicit `crsTransform`/`dimensions` (rather than `region`/`scale`) makes Earth Engine
  sample exactly onto the routing grid's own pixel edges *server-side* -- alignment guaranteed by
  construction. It's also a plain synchronous HTTP GET (capped at 32 MB / 10000 px per call,
  comfortably inside a regional domain), not Earth Engine's asynchronous Export-to-Drive flow.

All four paths write a daily-total netCDF and hand it to the *same* `core.forcing.netcdf_to_binary`
and Gate 2 audit a manually-imported file already goes through -- a download gets no special
treatment or reduced scrutiny.

**Credentials are never stored by this app.** `cdsapi` reads `~/.cdsapirc`; `earthaccess` reads
`~/.netrc` (checked for a `urs.earthdata.nasa.gov` entry); `earthengine-api` reads
`~/.config/earthengine/credentials` -- all three are the same files those libraries already
expect for any other use, set up once via each service's own free-registration page. Earth Engine
is the one exception with no equivalent credential file for *which* Google Cloud project to bill
against (mandatory as of the 2025-2026 API) -- that one non-secret setting lives in
`~/.camafloodcast/gee_project.txt` (same local-settings convention already used for saved colour
presets) or an `EARTHENGINE_PROJECT` environment variable, set from the dialog's own project-ID
field. `credentials_status()` only checks whether the credential files exist and a project is
configured, never any file's contents.

**Known limitation:** `netcdf_to_binary` only supports a forcing product already on the routing
grid (no `inpmat`). ERA5-Land's own API and both Earth Engine paths are requested pre-aligned to
the project's own cell size, so they always land matching; GLDAS's own API has no server-side
regridding via `earthaccess` yet, so it only aligns automatically when the project's own routing
grid is also 0.25° (true of every example project shipped here) -- otherwise the existing
conversion fails with a clear error rather than importing misaligned data. Full
arbitrary-resolution import (wiring up `generate_inpmat`) is a separate, larger feature, not
attempted here.

**Verified against the real CDS API** (2026-09-22, a live 1-day/1°x1° ERA5-Land request over the
Mekong Delta), which found two real bugs neither the documentation nor a synthetic test could
have caught, both fixed and now covered by regression tests reproducing the real response shape:

1. CDS's current infrastructure returned a **zip archive** containing one `.nc` file even though
   the request asked for `"format": "netcdf"` -- undocumented anywhere obvious. `era5_land_download`
   now detects this (`zipfile.is_zipfile`) and unwraps it transparently.
2. The real file names its coordinates `valid_time`/`latitude`/`longitude` (plus extra
   `number`/`expver` variables), not the `time`/`lat`/`lon` this module's own output (and the
   plan's original assumption, based on older-style CDS documentation) used. `era5_land_daily_totals`
   now checks both naming conventions.

The full pipeline -- real download, daily aggregation, `netcdf_to_binary` conversion, Gate 2 scan
-- was confirmed end to end against that live request (mean 4.2 mm/day, range 0..8.96 mm/day
across the 4x4 output grid, a plausible real spatial pattern, not uniform garbage). This
environment also happens to already carry real Earth Engine credentials (`ee.Initialize()` without
a project fails exactly as documented -- "Not signed up for Earth Engine or project is not
registered"), but no Google Cloud project id was available to test with, so **both Earth Engine
paths, and GLDAS's own API path** (`earthaccess` could not be installed here -- pip blocked by a
proxy in this environment), remain verified only against mocked responses, not a live service --
worth a first real check (GLDAS's `Qs_tavg`/`Qsb_tavg` variable names in particular, and the
Earth Engine paths' exact request/response shape) against real accounts.

## Architecture

```
camafloodcast/
  core/        pure Python SDK - no Qt, fully testable headless
  services/    render (array->image), execution (QProcess+queue), binaries (registry)
  adapters/    ModelAdapter interface + CaMa-Flood implementation
  ui/          main_window, theme, widgets/, map/, modules/
```

Three commitments the code enforces:

**Never load a whole output file.** Development targets are regional (decision D1) where a
plain read would work — which is exactly the trap. A global 15-min run writes ~1.5 GB per
variable per simulated year. Every accessor is windowed or strided over a `np.memmap`.

**Never edit model input by text substitution.** The namelist layer parses to a typed
structure, edits it, and rewrites only the changed value — preserving comments, alignment and
ordering, with a timestamped backup.

**Validation is a feature, not error handling.** CaMa-Flood fails *silently*: a truncated
basin, a forcing gap or a stale `inpmat` all produce plausible hydrographs and exit code 0. So
validators return a `DiagnosticList` for the GUI to render, never an exception on first problem.

## The model binary

CaMa-Flood is a **toolchain**, not one executable, and a given `MAIN_cmf` is not
interchangeable with another — `-DSinglePrec_CMF` changes results, `-DUseCDF_CMF` changes
capabilities. `services/binaries.py` therefore tracks a **build signature** (from
`manifest.json` beside the binary) and cross-checks it against what a configuration needs.

It also resolves the **runtime library directory** and prepends it to `PATH`. This is not
optional on Windows: a MinGW-built `MAIN_cmf.exe` that cannot reach `libgfortran` /
`libgomp` / `libwinpthread` dies with `STATUS_ENTRYPOINT_NOT_FOUND` (`0xC0000139`) — non-zero
exit, **no output at all**, nothing in the log. The integration test caught exactly this.

**Simulation → Model binary** runs a self-test: it launches the binary in an empty directory
and checks it reaches its own input handling, then reports the build signature.

## Tests

```bash
python -m pytest tests/ -q                                    # 299 tests (some skip w/o optional fixtures)
python examples/phase0_end_to_end.py --case mekong --days 60  # SDK, no GUI
python examples/make_mekong_project.py --days 365             # builds projects/mekong/
python examples/make_mekong_scenario.py                       # + the no_bifurcation case
python examples/make_mekong_dams.py                           # + the with_dams case
python examples/make_mekong_levee.py                          # + the with_levee case
```

| File | Covers |
|---|---|
| `test_core.py` | The SDK, headless — the bulk of the behaviour, including real Domain Builder runs (`TestDomainBuilderEndToEnd`) that cut real domains out of the real global map with the real Fortran tools, including the Vietnam-shaped bad cut that truncates the Mekong and Red River, real single-day and batch downscaling runs (`TestDownscaleEndToEnd`, `TestDownscalePeriod`) against `projects/mekong`'s real output, a real scenario-diff check (`TestFieldDiffEndToEnd`) that confirms turning bifurcation off measurably changes the flow field yet leaves far-upstream gauges untouched, a real longitudinal-profile trace (`TestLongitudinalProfileEndToEnd`) from Kratie to the delta outlet, a real parameter-sweep check (`TestBatch`), a real reservoir-operation check (`TestDamsEndToEnd`) that confirms the largest dam's own allocated cell is the single deepest pooled cell anywhere in the run, `TestOutputVariables`/`TestLeveeStats` covering the levee scheme's `levdph`/`levsto` outputs and protected-side overtopping statistics, `TestGeoTiffImport`/`TestGeoTiffExport` round-tripping a real GeoTIFF (import reprojects a UTM file onto a WGS84 grid; export writes downscaled output back out) with real `rasterio`/`pyproj`, and `TestForcingDownload` covering ERA5-Land/GLDAS daily aggregation against synthetic-but-known-value netCDF files, the CDS/Earthdata request-construction logic, and the two Earth Engine paths (a fake `ee` module plus a real, tiny, known-value GeoTIFF fed through a mocked `urlopen`, asserting the `crsTransform`/`dimensions`/band names Earth Engine was asked for and the resulting daily netCDF's values) -- all four network calls mocked, no live account in this environment |
| `test_ui.py` | Widget construction, signal wiring, adapter boundary (offscreen); `TestResultsViewLevee` plots a real levee run's `levdph` and checks the overtopping summary it produces; `TestColorScaleEditor`/`TestLayerPanel`/`TestNativeMapViewFrameCache`/`TestLeafletMapView` cover the map-interface upgrade -- the last against a real `QWebEngineView` instance, not a mock; `TestDownscaleView` exercises the Export GeoTIFF/PNG/CSV buttons and the Fit-to-domain button against the real Fortran tool and the real Mekong project; `TestHydrographChart` covers the dual-axis (`series2`) chart used for the Downscaling tab's area-vs-volume plot; the `TestForcingView` download tests exercise the Download dialog's domain auto-fill/Fit-to-domain, Earth Engine project-id row and its wiring into the existing netCDF-import handlers (both with and without the convert-to-binary step), with the network calls themselves replaced by fakes |
| `test_integration.py` | A **real simulation** through `QProcess`, log parsing, results loading |

GUI tests run with `QT_QPA_PLATFORM=offscreen`. `MainWindow.interactive = False` suppresses
modal dialogs, which would otherwise block the event loop with nothing to dismiss them.

## Not yet built

| Phase | Scope |
|---|---|
| **3** (remainder) | Exposure overlays (needs population/asset data this package does not ship), frequency analysis (needs a multi-year run; only 2000-2001 forcing ships here), **global scale** (vector output, tile server) |
| **4** (remainder) | Operation-scheme selector UI (today: `LDAMH22`/`LDAMYBY`/`LiVnorm` are plain namelist checkboxes, not an explained scheme picker), storage-curve preview graphics, an allocate-by-map-click workflow for building a new `CDAMFILE` (today: the dam table only *reads* an existing one — `core/dams.write_dam_file` exists and is tested but has no UI), a levee-map *builder* (today: the Physics tab reads and explains an existing `CLEVHGT`/`CLEVFRC` pair; there is no map-click or raster-derived way to create one), plugin API |
| **5** | Installers, docs, sample projects |

Also not built: a generic vector/GeoJSON overlay layer (the real basin boundary or river network
as vectors rather than only raster `MapLayer`s) and shapefile import -- the Leaflet renderer's
current overlay types are rasters, point markers and a domain rectangle, which is everything the
app's own tabs use today, not the full RRI-style vector stack.

Decision D3 (a web-based basemap behind `MapRenderer`) is filled in -- see "The map interface"
above -- with Leaflet rather than the originally-envisaged MapLibre, chosen for a lighter
dependency footprint since vector-tile styling was never actually needed here, just raster
basemaps and image overlays.

Map & Results still shows the **model grid** only, and says so permanently under the map — the
Downscaling tab is a separate view for now rather than a toggle on that same map. A 0.25° cell
is ~28 km across; painting `flddph` at that resolution produces blocky cells that look like an
inundation map and are not, which is exactly why the Downscaling tab exists.

`conv_flood` and `igetday` (the other two binaries in CaMa-Flood's downscaling tool set) are deliberately not built or wrapped: `conv_flood` converted downscaled depth
for a shell-script-era visualisation path this GUI's own renderer already supersedes, and
`igetday` was a day-in-month helper for a batch loop that `core/downscale.record_for_date`
does directly in Python. Only `downscale_flddph_trib` (the actual redistribution) is needed.

**Two `NativeMapView` bugs found and fixed while building the profile view**, both easy to miss
because a full-grid raster layer hides them: (1) `zoom_to()` called in the same method as
`chart.setVisible()` can fit against a viewport size that is mid-reflow and about to change —
fixed by deferring the `zoom_to()` call one event-loop tick (`QTimer.singleShot(0, ...)`); (2)
with no raster layer at all (markers only, as the profile path is), the scene rect is never set
and `QGraphicsView` can end up not painting `drawForeground()` overlays despite computing a
correct transform — fixed by having `zoom_to()` set the scene rect itself when there is no
layer to size it from. Confirmed by screenshot before/after, not just by the transform matching.

**Two levee-scheme quirks found while building `examples/make_mekong_levee.py`**, neither a bug
in this GUI, both worth knowing before trusting a `LLEVEE` run: (1) `CLEVHGT`'s crown height is
silently raised to match the levee's own base height if set any lower, and that base height is
derived from `CLEVFRC` via the floodplain hypsometry's own layer heights, not a plain fraction of
distance -- an innocuous-looking `CLEVFRC` like 0.5 can put the base far above an intended crown,
neutralising the levee with no warning (`src/cmf_ctrl_levee_mod.F90`'s `CMF_LEVEE_INIT`; see the
CLEVHGT/CLEVFRC tooltips in Model Setup and the note on the Physics tab's levee section for the
fix -- keep `CLEVFRC` below `1/NLFP` so the base stays a small, predictable fraction of the first
floodplain layer's own height). (2) A run with an active, overtopped levee reports a water-balance
closure roughly four to five orders of magnitude looser than a plain run (~1e-8 rather than
~1e-14) -- traced to the log directly: it is bounded, falls as the basin fills through the wet
season, and never diverges, so it is a property of `CMF_LEVEE_FLDSTG`'s extra branching, not mass
non-conservation. `examples/make_mekong_levee.py` judges its own Gate 4 against a bar an order of
magnitude looser than the model's stock 1e-9 for this reason (see the comment there); nothing in
the GUI's own Gate 3/4 was changed, so Run Manager's live water-balance monitor may still flag a
healthy levee-enabled run "ABOVE LIMIT" -- expected, not a regression.
