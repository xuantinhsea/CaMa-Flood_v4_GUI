# CaMa_Floodcast User Manual

**Version 1.0** · Desktop application for the CaMa-Flood global river hydrodynamic model

Developed by Nguyen Xuan TINH (Ph.D.)\
© 2026 Nippon Koei Co.,Ltd. Water Resources & Energy Dept.

---

## 1. Introduction

CaMa_Floodcast is a desktop GUI for the CaMa-Flood global river routing model. It does not
replace CaMa-Flood -- it runs the CaMa-Flood model binary (`MAIN_cmf`) on your map and forcing
files, gives you a form-based editor for the namelist that controls a run, and shows you the
results on a map and as hydrographs. If you already run CaMa-Flood by hand from a terminal,
everything in this manual maps onto files you already know: the namelist (`input_cmf.nam`), the
map directory, the runoff forcing directory, and the `.bin` output files.

The Windows installer ships everything needed to start straight away: the application, a built
CaMa-Flood v4.30 model with its runtime libraries, the domain-cutting and downscaling tools, and
four ready-to-open example projects with their maps, forcing and results.

This manual walks through the tool in the order you would normally use it: install it and open an
example, then set up your own project -- a domain and forcing, the physics, a run -- and look at
the results. Reference sections at the end cover mouse/keyboard controls, troubleshooting and
licensing.

## 2. System requirements

| Item | Requirement |
|---|---|
| Operating system | Windows 10 or Windows 11, 64-bit |
| Disk space | about 0.8 GB for the application, plus about 0.6 GB for the example projects |
| Memory | 8 GB RAM recommended (regional domains); more for large or global domains |
| Display | 1366 × 768 or larger |
| Internet | Optional -- only for basemap tiles and for downloading forcing data |
| Administrator rights | Not needed for the default (per-user) installation |

## 3. Installation

### 3.1 Running the installer

1. Run **`CaMa_Floodcast_Setup_1.0.exe`**.
2. Choose **Install for me only** (recommended, no administrator rights needed) or **Install for
   all users** (needs administrator rights).
3. Read and accept the licence and copyright notice.
4. Choose the **application folder**. The default is
   `%LOCALAPPDATA%\Programs\CaMa_Floodcast` for a per-user installation, or
   `C:\Program Files\CaMa_Floodcast` for all users. Keep the path short (under about 100
   characters): the map viewer's components sit deep inside this folder, and Windows refuses
   to load a library whose full path exceeds 260 characters.
5. Choose the **components**:

   | Component | Contents | Size |
   |---|---|---|
   | CaMa_Floodcast application | the program, CaMa-Flood v4.30 model, Fortran tools, user manual | required |
   | Example: Mekong basin | 4 cases -- baseline, no bifurcation, with dams, with levee | ~310 MB |
   | Example: Vietnam (national) | 1 case -- a deliberately truncated national cut | ~36 MB |
   | Example: Philippines (national) | 1 case -- a hydrologically closed national cut | ~48 MB |
   | Example: Cagayan River basin | 1 case -- a small catchment-scale cut | ~3 MB |
   | Shared example forcing | 1-degree daily runoff, 2000-2001, used by every example | ~180 MB |

   **Full installation** selects everything; **Application only** installs no examples.
6. If you selected any example, choose the **workspace folder** -- where the example projects
   and their map and forcing data go. The default is `%USERPROFILE%\CaMa_Floodcast`. It must be a
   folder you can write to, because running a case writes its results there. Avoid a folder
   synchronised by OneDrive or similar: model runs write hundreds of megabytes of results.
7. Optionally tick **Create a desktop shortcut**, then **Install**.
8. On the last page, leave **Launch CaMa_Floodcast** ticked to start the program; if the Mekong
   example was installed, it opens straight into that project.

### 3.2 What goes where

**Application folder** (`%LOCALAPPDATA%\Programs\CaMa_Floodcast` by default):

| Path | Contents |
|---|---|
| `CaMa_Floodcast.exe` | the program |
| `_internal\` | the program's bundled Python, Qt and libraries -- do not modify |
| `bin\win64\MAIN_cmf.exe` | the CaMa-Flood v4.30 model (single precision, OpenMP) with `manifest.json`, its build signature |
| `bin\win64\*.exe` | domain-cutting tools (`cut_domain`, `cut_bifway`, `set_map`, `combine_hires`, `generate_inpmat`, `set_bifparam`) and `downscale_flddph_trib` |
| `bin\win64\lib*.dll` | the GCC/MinGW runtime libraries the Fortran programs need |
| `docs\` | this manual (`USER_MANUAL.docx` and `USER_MANUAL.md`), `NOTICE.txt`, `CaMa-Flood_LICENSE.txt` |

**Workspace folder** (`%USERPROFILE%\CaMa_Floodcast` by default):

| Path | Contents |
|---|---|
| `projects\mekong`, `projects\vietnam`, `projects\philippines`, `projects\cagayan` | the example projects -- open these with **File → Open project** |
| `map\<region>_15min\` | the regional river maps the examples use, including the 1-arcminute sub-grid data the Downscaling tab needs |
| `inp\test_1deg\runoff\` | the shared 1-degree daily runoff forcing (years 2000-2001) |

Every path inside the example projects is **relative** -- a namelist refers to
`../../../../map/mekong_15min/nextxy.bin`, not to a fixed drive letter. You can therefore move or
copy the whole workspace folder anywhere, as long as `projects\`, `map\` and `inp\` stay side by
side.

**Start Menu** (*CaMa_Floodcast* group): the program, *Mekong example*, *Example projects folder*,
*User Manual*, and *Uninstall CaMa_Floodcast*.

### 3.3 First launch

- The model binary is found automatically: a project with no model binary set picks up
  `bin\win64\MAIN_cmf.exe` from the installation, together with its runtime libraries. You can
  check it at any time with **Simulation → Model binary...** (see [§7.4](#74-setting-the-model-binary)).
- The map tabs fetch basemap tiles from the internet. Without a connection, the maps still show
  every model layer -- only the background imagery is missing.
- If Windows SmartScreen warns about an unrecognised app the first time, choose **More info →
  Run anyway**.

### 3.4 Updating and uninstalling

- **Updating**: run a newer installer over the existing installation. The application is
  replaced; example project files that already exist in your workspace are **not** overwritten,
  so your own edits and runs are kept.
- **Uninstalling**: **Settings → Apps → CaMa_Floodcast → Uninstall**, or *Uninstall
  CaMa_Floodcast* in the Start Menu. This removes the application folder and the shortcuts. Your
  **workspace folder is never deleted** -- it holds your projects and results. Delete it by hand
  if you no longer need it.
- Personal settings (saved colour-scale presets, the Earth Engine project ID) are kept in
  `%USERPROFILE%\.camafloodcast\`, which uninstalling also leaves in place.

### 3.5 Running from source (developers)

The same application runs from its Python source, which is how it is developed and tested:

```bash
pip install -e ".[gui]"           # optional extras: ".[gui,geo,netcdf,download]"
python -m camafloodcast
python -m camafloodcast --project path/to/project      # open a project on startup
python -m camafloodcast --import path/to/input_cmf.nam # start from an existing namelist
```

The installed program accepts the same `--project` and `--import` options. A source checkout
looks for the model in `src/MAIN_cmf.exe` near the package and for the tools in `bin/win64`.
`build_installer.bat` rebuilds the installer from source (it needs Python with the app's
dependencies, MSYS2's MinGW runtime and Inno Setup 6).

## 4. Example projects

Four complete, already-run projects are installed in `projects\` of your workspace. Each opens
with **File → Open project...** (pick the project's folder, the one containing `project.json`).
All cases have results, so **Map & Results** and **Analysis** work immediately -- no run needed.
All use the 15-arcminute (0.25°, ~28 km) routing grid and the shared 1-degree daily runoff
forcing.

| Project | Domain | Cases | What it demonstrates |
|---|---|---|---|
| **Mekong basin** (`mekong`) | whole Mekong catchment, 68 × 104 cells, 5,528 river cells, year 2000 | `baseline`, `no_bifurcation`, `with_dams`, `with_levee` | the full workflow; scenario comparison; reservoir operation; the levee scheme; longitudinal profiles; downscaling (1-arcmin data included) |
| **Vietnam (national)** (`vietnam`) | Vietnam's political boundary box, 102-110°E, 8-24°N, 1,430 river cells, year 2000 | `baseline` | what Gate 1 is for: a country-shaped cut that truncates the Mekong (40.6% of its area inside) and the Red River |
| **Philippines (national)** (`philippines`) | 116-127°E, 4-21°N, 536 river cells, year 2000 | `baseline` | a hydrologically closed national domain -- every basin at 0.0% deficit |
| **Cagayan River basin** (`cagayan`) | 120.0-122.5°E, 15.5-19.0°N, 96 river cells, year 2000 | `baseline` | a tight catchment-scale cut that runs in under a minute |

**Mekong cases.** `baseline` is the reference run, with bifurcation channels on (268 bifurcation
paths). `no_bifurcation` is identical except that bifurcation is off (`LPTHOUT = .FALSE.`).
`with_dams` adds five real
Lancang/upper-Mekong dams (Wunonglong, Lidi, Huangdeng, Dahuaqiao, Miaowei, from the GRanD
database) through `dam_params.csv`: compared with `baseline`, dry-season flow downstream is
higher and the flood peak lower. `with_levee` adds an illustrative levee (`levee_hgt.bin` /
`levee_frc.bin`) at the cell that floods deepest in the baseline run; its protected side overtops
during the wet season.

**Gauges.** Each project has an `analysis\gauges.csv` used by the Analysis tab, and importable in
Map & Results: Kratie, Pakse and Chiang Saen on the Mekong; Cagayan @ Aparri, Mindanao @ Cotabato
and Agusan @ Butuan in the Philippines; Cagayan @ Aparri for the Cagayan basin. The Vietnam
project's gauges are labelled by location, and its Mekong outlet row is marked *TRUNCATED -- do
not trust*: it is there so that the truncation is visible when you plot it.

### 4.1 A ten-minute tour with the Mekong example

1. Start **Mekong example** from the Start Menu (or **File → Open project...** →
   `projects\mekong`). The **Project** tab lists the four cases, all *completed*.
2. Select `baseline`, open **Map & Results**, choose *River discharge* and **Show on map**. Drag
   the time bar to the wet season (August-September) and watch the main stem.
3. **Import CSV...** `analysis\gauges.csv` and **Plot at gauges** for hydrographs at Kratie,
   Pakse and Chiang Saen.
4. Open **Analysis**, set **Case A** = `with_dams`, **Case B** = `baseline`, variable *River
   discharge*: **Show difference map**, then **Compare hydrographs** at Chiang Saen to see the
   reservoirs' effect.
5. Still in Analysis, **Show profile: gauge → outlet (Case A)** from Kratie for a longitudinal
   profile of bed and water surface to the delta.
6. Open **Downscaling**, click **Fit to domain**, pick a wet-season day on the time bar and click
   **Downscale this day** for a 1-arcminute inundation-depth map.
7. Select `with_levee` in the Project tab, then in Map & Results plot *levdph* (protected-side
   depth) at a gauge to see the overtopping summary.
8. To run a case yourself, select it and press **F5**: the Mekong cases take about two minutes
   each, Cagayan under a minute. Re-running replaces that case's results.

To keep the original examples untouched while you experiment, use **Duplicate** in the Project
tab and work on the copy, or copy the whole workspace folder first.

## 5. Before you start your own study

You need, on disk somewhere:

- **A CaMa-Flood model binary** -- the installed one (`bin\win64\MAIN_cmf.exe`) is used
  automatically; you only need your own if you want a different build (e.g. double precision or
  netCDF support).
- **Map/master files** for the domain you want to simulate (`nextxy.bin`, `params.txt`, and the
  rest of a standard CaMa-Flood map directory) -- either a ready-made regional map, or a larger
  map (e.g. the CaMa-Flood global 15-minute map, a separate download) to cut your own region from
  inside the tool with the bundled tools (see [§9](#9-domain-tab-point-at-or-build-a-map)).
- **Runoff forcing** for the period you want to simulate. CaMa-Flood routes *runoff*, not
  rainfall -- if you only have precipitation you need to have already run it through a
  land-surface model. ERA5-Land or GLDAS runoff can be downloaded from inside the tool (see
  [§10](#10-forcing-tab-runoff-and-proving-its-usable)).
- Optionally, an existing `input_cmf.nam` from a setup you already run by hand. This is the
  fastest way into the tool (see [§7.1](#71-fastest-path-import-an-existing-namelist)).

## 6. The window at a glance

- **Menu bar**: `File` (new/open/import a project, save the current configuration, exit),
  `Simulation` (run, stop, choose the model binary), `Forcing` (download forcing), `Help` (about).
- **Toolbar**: New / Open / Import project buttons, the green **▶ Run Simulation** button, and a
  label showing which case is currently selected.
- **Workflow strip**, just under the toolbar: five steps -- *1 Domain, 2 Forcing, 3 Config, 4 Run,
  5 Results* -- each marked ✓ (ok), ! (warning) or ○ (not done yet). Hover a step for why it is in
  that state. This is the fastest way to see what is stopping you from running or viewing results.
- **Tabs**, left to right: **Project, Domain, Forcing, Model Setup, Physics, Run Manager,
  Map & Results, Analysis, Downscaling.** The first five are about setting a case up; the last
  four are about running it and looking at what came out.
- **Bottom dock**: a **Log** tab (the model's own output while it runs) and a **Validation** tab
  (a clickable list of configuration problems -- double-click an entry to jump straight to the
  offending field in Model Setup).

Every tab with a map (Map & Results, Domain, Forcing, Physics, Downscaling, Analysis) uses the
same map widget, with a real basemap available via the layer-switcher icon in its top-right
corner -- Street, Satellite, Topographic, Gray, or none. **Street is the default**, and each tab
zooms itself to the project's domain (or the layer it just loaded) as soon as there is real data
to show -- a different basemap or "No Base Map" is one click away if the imagery isn't wanted.

Everything in the tool revolves around two objects:

- A **project** is one study: one domain, one forcing set, and a folder of **cases**
  (`<project>\cases\<name>\`). It lives in its own folder on disk and is portable.
- A **case** is one simulation: a namelist plus (once run) its output files. Cases in the same
  project share the same routing grid, which is what makes comparing two of them meaningful (see
  [§15](#15-analysis-tab-comparing-two-cases)).

## 7. Opening or creating a project

### 7.1 Fastest path: import an existing namelist

**File → Import CaMa-Flood namelist...** This is the normal way to get started, and the only way
to create the very first case in a brand-new project -- the tool always starts a case from a real
namelist, it does not build one from a blank schema. (A case from one of the example projects
makes a good starting namelist too.)

1. Pick an existing `input_cmf.nam`.
2. If no project is open yet, you'll be asked for a folder to create one in; the project is named
   after the namelist's parent folder. If a project is already open, the case is simply added to
   it.
3. Give the case a name (defaults to the source folder's name).
4. The tool reads the domain from the namelist's `CNEXTXY` entry and wires it in as the project's
   map automatically. Any `.bin` result files already sitting next to the namelist are copied in
   too, so an already-run setup is viewable immediately in **Map & Results**.

### 7.2 Starting a brand-new project

**File → New project...** -- pick a folder and a name. This creates the project's folder
structure (`cases\`, a domain folder, a forcing folder, `analysis\`, `cache\`) but no case yet.
Use **File → Import CaMa-Flood namelist...** right afterwards to create the first case.

### 7.3 Reopening a project

**File → Open project...** and pick the project's folder (the one containing `project.json`).

### 7.4 Setting the model binary

A project with no model binary set uses the installed `bin\win64\MAIN_cmf.exe` automatically, so
normally there is nothing to do. To use a different build, choose **Simulation → Model
binary...** and pick its `MAIN_cmf` executable. CaMa_Floodcast then runs a quick self-test: it
launches the binary in an empty folder and reports whether it started cleanly, its build
signature (single/double precision, netCDF support, etc., read from a `manifest.json` beside it if
one exists), and where it resolved the binary's runtime libraries from. **Do this before the
first run with any new binary** -- on Windows, a binary that cannot find its runtime libraries
fails with no output and no error message at all, and this self-test is the only way to catch
that in advance rather than after a run silently produces nothing.

The toolbar's **Run** button stays disabled until a binary resolves to a real file.

## 8. Project tab: managing cases

The **Project** tab shows the project's name, location, domain and model binary at the top, and a
table of its cases below.

| Column | Meaning |
|---|---|
| Status | `not run` / `queued` / `running` / `completed` (green) / `failed` (red) / `stale` (orange) |
| Period | the case's simulated start → end dates, read from its namelist |
| Runtime | wall-clock time of the last run |
| Outputs | size of the case's output files |
| Last run | when it last finished |

A **stale** case is one whose configuration has changed since it was last run -- its results no
longer match its inputs. This is tracked automatically (by hashing the namelist and every file it
references), so you never have to remember whether you re-ran after an edit.

Buttons above the table:

- **New case** creates another case by copying the *first* case listed in the table under a new
  name. To copy a *specific* case instead, select it and use **Duplicate**.
- **Duplicate** copies the selected case's namelist into a new case (default name
  `<name>_copy`) -- results are not copied, only the configuration.
- **Delete** removes a case; if it has results, you're warned how much disk space will be freed.
- **New sweep...** builds several cases at once that vary exactly one namelist field -- see
  [§12](#12-parameter-sweeps).
- **Run selected** queues the selected case in Run Manager (same as the toolbar's Run button).

Selecting a row loads that case into every other tab.

## 9. Domain tab: point at, or build, a map

Every case needs a routing grid. If you imported a namelist, its `CNEXTXY` map is already wired
in and you can skip this tab. Use it when you need a **new regional cut** from a larger map --
for example, cutting a river basin out of the global 15-minute map so a case runs in minutes
instead of hours.

1. Pick a **source map** -- a map directory placed in your project's domain folder (for example a
   copy of the CaMa-Flood global map), or the project's own active map. The map preview shows its
   drainage area (`uparea`) so you can see where basins are.
2. Give the domain a **Name** and set **West / East / South / North** bounds (degrees); the
   rectangle is drawn live on the map, and the **Grid** field shows the resulting cell count.
3. Click **Build domain**. This runs the real 7-step CaMa-Flood cutting chain
   (`cut_domain → cut_bifway → set_map → combine_hires → generate_inpmat → channel parameters →
   set_bifparam`) in the background; a progress bar tracks the 7 steps. It uses the tools
   installed in `bin\win64\` -- if any are missing, **Build domain** is disabled and the tab says
   which ones.
4. When it finishes, the new domain becomes this project's active map automatically, and the
   right-hand **Closure (Gate 1)** panel shows the result.

**Gate 1 is the single most important check in the whole tool.** CaMa-Flood has no lateral
boundary inflow: if a rectangular cut severs a basin's headwaters, the missing upstream runoff is
simply absent from the simulation, and discharge comes out biased low with **no error and no
warning from the model itself**. The closure table lists the largest outlets in the domain,
colour-coded green (closed -- the whole basin is inside your cut), orange (partially truncated)
or red (badly truncated), with the percentage of drainage area lost. Click a row to make it the
"target basin" and see a pass/fail verdict. If your study is about a basin shown red, extend the
domain until it turns green before trusting any discharge numbers from it. The **Vietnam** example
project shows exactly this failure; the **Mekong** example shows the fix.

The **Roughness (CFLDMAN)** sub-tab turns a land-cover raster into a 2-D floodplain-Manning map
instead of the single scalar value CaMa-Flood uses by default -- point it at a land-class raster,
adjust the per-class Manning-n table if needed (it starts from sensible IGBP-class defaults),
**Preview**, **Write fldman.bin**, then **Apply to case (CFLDMAN)** to wire it into the currently
selected case's namelist. This is the single highest calibration-value-for-effort change
available in the tool (roughly 9% RMS sensitivity in discharge, measured against a uniform
value). The land-class raster can be either a file already gridded onto your domain (CaMa's own
raw binary format), **or a real-world GeoTIFF in any coordinate system** -- point the file picker
at, say, a downloaded ESA WorldCover tile, and the tool detects its actual CRS and reprojects it
onto the domain automatically (nearest-neighbour, since class codes are categorical, not a
continuous field), reporting what it reprojected from in the summary line.

## 10. Forcing tab: runoff, and proving it's usable

CaMa-Flood consumes **runoff**, not rainfall, and generates none itself.

- Point **Directory** at your forcing files, and set the **Prefix**/**Suffix** naming convention,
  grid size (**Columns/Rows**), edges, **Unit**, **Row order** and **Big-endian** flag to match
  how the files were written. If you already have a case loaded, these are pre-filled from its
  namelist.
- Set the **Simulation period** you intend to run.
- Click **Scan and check (Gate 2)**. This is the check that catches the three failure modes that
  otherwise produce a plausible-looking hydrograph and a normal exit code: **wrong units**,
  **missing days** within your simulation period, and a **flipped latitude order**. A pass/fail
  message and a table of missing dates appear on the right; the map preview lets you page through
  individual forcing days to spot an orientation mistake visually.
- Once it passes, **Apply to case (CROFDIR / CROFPRE / CROFSUF / DROFUNIT)** writes these settings
  into the selected case's namelist.
- If your forcing is netCDF, use **Import from netCDF**: **Inspect** a file to see its grid, unit
  and time span, then **Convert to binary...** to extract it onto your project's routing grid (a
  project with an active domain must already be open, since the extraction windows onto that
  grid).

### 10.1 Downloading forcing

**Forcing menu → Download forcing...** fetches ERA5-Land or GLDAS runoff directly -- no manual
download or netCDF wrangling needed. Everything the download needs is in the dialog itself:

- **Source** -- the label under it says whether the credentials that source needs are already
  found:
  - **ERA5-Land (Copernicus CDS)** needs a free account (cds.climate.copernicus.eu) and its API
    key saved to `%USERPROFILE%\.cdsapirc`, exactly as the CDS website's own setup instructions
    describe.
  - **ERA5-Land or GLDAS via Google Earth Engine** needs a free Earth Engine sign-in *and* a
    Google Cloud project ID, entered into the **Earth Engine project ID** field that appears for
    these sources and saved with the button beside it -- Earth Engine requires a project to bill
    API calls against, so this one setting is remembered locally (never a secret, just which
    project's quota is used).
  - **GLDAS Noah (NASA GES DISC)** needs a NASA Earthdata Login saved to `%USERPROFILE%\.netrc`
    *and* the `earthaccess` Python library, which the installed program does not include. With
    the installed program, download GLDAS **via Google Earth Engine** instead; the direct NASA
    source works when running from source with `pip install -e ".[gui,netcdf,download]"`.
  - No key or password is ever typed into this app -- it only checks whether the relevant file
    exists (and, for Earth Engine, whether a project is also set).
- **Model domain**: West/East/South/North/Resolution, filled in automatically from the open
  project's own routing grid -- click **Fit to domain** to reset them if you've edited them.
- **Duration**: Start/End, seeded from the Forcing tab's own Simulation period each time the
  dialog is opened -- edit them here without changing that period.
- **Output**: pick a **Directory**, and leave **Convert to CaMa binary forcing after download**
  ticked (the default) to get straight to a ready-to-use forcing directory; untick it to keep
  just the daily-total netCDF file instead -- useful if the domain above doesn't match your
  project's own grid (conversion needs that match and will otherwise fail clearly), or you just
  want to inspect the file first.
- Click **Download and convert...**. This makes a real network request, then collapses the
  sub-daily product into one calendar-day total per day, then -- if the checkbox is ticked --
  runs it through the exact same conversion and **Gate 2** audit as a manually-imported netCDF
  file, so a units or coverage problem still surfaces the same way. Three status labels above the
  button track Fetch / Aggregate / Convert live.
- This can take a while for a long period -- ERA5-Land's own API ships one value per hour, so a
  full year is 8,760 values to fetch and collapse before conversion even starts.
- Downloaded forcing lies on your project's 0.25° routing grid. A case that uses it directly
  needs `LINTERP = .FALSE.` (nearest point, exact when the grids match) or an input matrix
  (`CINPMAT`) built for that forcing grid -- the examples' `inpmat_test-1deg.bin` is for the
  1-degree example forcing only.
- ERA5-Land's own CDS API has been run end to end against the real service; the Earth Engine
  sources have not yet been tried against real accounts.

## 11. Configuring the model

Two tabs edit the *same* namelist -- editing in one is instantly reflected in the other, and
saving from either saves the same file.

### 11.1 Model Setup: the complete namelist

Every field CaMa-Flood understands, organised into the model's own namelist groups (NRUNVER,
NDIMTIME, NPARAM, NSIMTIME, NMAP, NRESTART, NFORCE, NOUTPUT, NLEVEE, NBOUND, NDAMOUT) as tabs
along the top of the form. Hover any field for its meaning, unit, default, and any extra
guidance. Fields you (or an imported file) have changed from the CaMa-Flood default are shown in
**orange**. Tick **Show advanced** to reveal the less-commonly-touched fields, hidden by default
so the form stays usable.

- **Diff vs defaults** lists every key that differs from the stock CaMa-Flood value -- a fast way
  to see exactly what makes this case non-standard.
- **Validate** re-runs the schema checks and sends the results to the Validation dock.
- **Save** (Ctrl+S) writes only the fields that actually changed, preserving the rest of the file
  byte-for-byte -- comments, ordering, alignment -- and keeps a timestamped backup. Nothing is
  ever rewritten by blind text substitution.

The panel on the right shows the **raw namelist text** at all times, in sync with the form. If you
prefer to hand-edit the text directly, type your changes and click **Apply edits** to re-parse it
back into the form (and re-validate).

File paths in the namelist may be absolute or **relative to the case folder** (the model's
working directory) -- the example projects use relative paths so they keep working wherever the
workspace is.

### 11.2 Physics: the choices explained

A curated, explained subset of the same fields, grouped by what they're *for* rather than by
which namelist block they live in -- useful when you know you want "bifurcation on" but don't want
to hunt through 90 raw keys to find it. Turning a section's main switch off automatically greys out
its detail fields, so the dependency is visible, not just documented in a tooltip.

- **Routing scheme and time step** -- floodplain flow, kinematic vs. local-inertial, the adaptive
  (CFL-limited) time step and its safety coefficient.
- **Bifurcation channels** -- turn on, point at the channel table, and **Refresh** to see how many
  channels it defines and where, plotted on a small map. (The table itself, `bifprm.txt`, comes
  from **Build domain**'s `set_bifparam` step.)
- **Levee scheme** -- crown height (`CLEVHGT`) and unprotected-fraction (`CLEVFRC`) maps;
  **Analyse maps** reports their mean and range over the domain. **Read the two together, not
  `CLEVHGT` alone**: the model derives the levee's *base* height from `CLEVFRC`'s position on the
  floodplain's own hypsometry layers, then silently raises `CLEVHGT` to match that base if you set
  it any lower -- an innocuous-looking `CLEVFRC` like 0.5 can put the base far above an intended
  crown height and neutralise the levee with no warning. Keeping `CLEVFRC` below `1/NLFP` (NLFP,
  the number of floodplain layers, is fixed by the map's own `params.txt`, not editable here)
  keeps the base a small, predictable fraction of the first floodplain layer's own height -- see
  the field tooltips for the exact relationship. Once a case with `LLEVEE` on has run, Map &
  Results recognises its `levdph` (protected-side depth) output and reports overtopping
  statistics when you plot it -- see [§14](#14-map--results-tab-viewing-output).
- **Reservoirs / dams** -- point at a dam parameter file and **Read dam file** to see a read-only
  table of each dam's location, capacity and normal/flood discharge thresholds. Building or
  editing a dam list from inside the GUI isn't available yet; prepare the file externally (the
  Mekong example's `dam_params.csv` shows the format) and point `CDAMFILE` at it here.
- **Sea level** -- a static mean-sea-level map, or a time-varying boundary (plain binary or
  netCDF) with its own update frequency.
- **Storage-change limiter**, **floodplain roughness** (with a shortcut button straight to the
  Domain tab's CFLDMAN builder), and **diagnostics** (the per-substep water-balance log that
  drives the live monitor in Run Manager).

## 12. Parameter sweeps

**Project tab → New sweep...** builds several cases at once that are identical except for one
namelist field, so a later comparison is only ever seeing the effect you changed:

1. Select the case to use as the template, then click **New sweep...**.
2. Pick the **namelist group** and **field** to vary (the same schema as Model Setup, with the
   same tooltips).
3. Enter the **values** to sweep over, comma-separated (e.g. `0.03, 0.05, 0.10`).
4. Optionally set a **case name prefix** (defaults to the field's name).

One case is created per value, each with a distinct `COUTTAG` so their output files never
collide. Run them from the Project tab, then compare any pair in the **Analysis** tab.

## 13. Running the model

With a case selected, click the toolbar's **▶ Run Simulation** (or press **F5**, or use
**Simulation → Run case**, or **Run selected** on the Project tab).

Before anything runs, the tool performs **pre-flight validation** against the same checks used
elsewhere -- if the configuration would fail, the run is blocked, the Validation tab opens
automatically, and a summary of what's wrong is shown. This is deliberate: CaMa-Flood itself
fails *silently* on many of these problems (a truncated basin, a forcing gap, a stale input map),
producing a plausible hydrograph and exit code 0 rather than an error.

Once queued, the **Run Manager** tab shows:

- The **job queue** table (case / state / exit code).
- **Live diagnostics** while it runs: simulated time, active river cells, adaptive substep count,
  and the worst water-balance closure error seen so far (flagged red if it exceeds the
  acceptable tolerance).
- The **water-balance chart**, updated live -- CaMa-Flood writes a global budget line on every
  adaptive substep, so a closure problem is visible *during* a long run instead of only after it
  finishes.
- The **log**, streamed from the model's own output.

**Stop** cancels the running/queued jobs; **Clear finished** tidies the table. When a run
finishes successfully the tool switches you straight to **Map & Results**; on failure it explains
the exit code (for example, distinguishing "the binary couldn't even start" from "it ran and
crashed").

## 14. Map & Results tab: viewing output

This is the main viewer, once a case has produced output.

- **Variables**: pick one from the dropdown by its explicit name (e.g. "Floodplain water depth",
  not `flddph`) and **Show on map** to add it as a layer, labelled the same way in the layer
  stack on the right, the probe panel and the map's colour-scale legend (colour scale chosen
  per-variable, and a caveat note appears for variables that need one, e.g. instantaneous vs.
  accumulated quantities). CaMa-Flood's own short code -- the one you type into Model Setup's
  `CVARSOUT` and the one that names the case's output file on disk (`flddphmekong.bin`, say) --
  is unchanged; only how the GUI displays it differs.
- **Time bar**, above the map: scrub through output records.
- **Gauges**: **+ Add via map** arms pick mode -- double-click the map to drop a gauge, which is
  snapped onto the nearest plausible river cell automatically (matching a published drainage area
  if you gave one, otherwise the local mainstem). Or **Import CSV...** a file with columns
  `name,lon,lat[,area_km2][,observed]`.
- **Plot at gauges** draws a hydrograph for every gauge in the table for the selected variable;
  **Export CSV** / **Export PNG** save the plotted series or the map image. Plotting `levdph`
  (the levee scheme's protected-side depth, only present when a case ran with `LLEVEE` on) also
  reports, per gauge, whether and when the levee was overtopped and how deep it got -- see
  [§11.2](#112-physics-the-choices-explained).
- **Double-click** any cell (when not in gauge-picking mode) to probe its values across every
  layer currently shown, plus its drainage area.
- The permanent **resolution badge** under the map states the model grid's cell size in km, and
  says plainly that this is the routing grid, not a downscaled inundation map -- a 0.25° cell is
  roughly 28 km across, and painting flood depth at that resolution looks like an inundation map
  without being one. For that, see **Downscaling** ([§16](#16-downscaling-tab-high-resolution-inundation)).

**The layer panel**, on the right, shows one row per layer, all visible and editable at once:

- The **checkbox** toggles visibility; **▲**/**▼** reorder the stack (the top row is the top of
  the map -- it paints last, over everything below it); **✕** removes the layer.
- The **colour dropdown** offers both continuous colormaps (a smooth ramp scaled to the layer's
  own min/max) and named **discrete presets** -- a handful of colours at fixed, labelled
  break-values, e.g. "Floodplain depth (blue)" bands at 0.1/0.3/0.5/1.0/2.0/5.0 m.
- **🎨 Edit colour scale...** opens an editor for the discrete presets: pick a starting preset,
  click a colour swatch to change it, edit a break-value directly, add or remove a band, and
  optionally **Save Preset** so it's available (and remembered) next time -- globally
  (`%USERPROFILE%\.camafloodcast\color_presets.json`) or just for this project
  (`<project>\cache\color_presets.json`).
- Each row shows its own small legend, matching exactly what's painted on the map.

## 15. Analysis tab: comparing two cases

Pick **Case A** and **Case B** from the same project (they share a routing grid by construction,
which is what makes a cell-by-cell comparison meaningful) and a **Variable** common to both.

- **Show difference map** plots A − B, coloured on a scale clipped at the 98th percentile of
  |difference| rather than the raw maximum, so one outlier cell can't wash out every other cell
  into "no visible change". Mean/max/RMS absolute difference are reported alongside.
- **Compare hydrographs** needs a gauge, loaded from `<project>\analysis\gauges.csv` (same format
  as the Map & Results CSV import -- create this file yourself if your project doesn't have one
  yet, since this tab reads it rather than offering its own import button; every example project
  has one). It plots both cases' hydrographs together and reports goodness-of-fit metrics
  (NSE/KGE/PBIAS) describing how far B departs from A as a reference.
- **Show profile: gauge → outlet (Case A)** traces the river network downstream from a gauge to
  its river mouth or an inland sink, and plots bed elevation and water surface along that path for
  the day selected on the time bar. This only needs Case A -- no B required. It needs the case to
  have written `sfcelv` (and, for water depth, `rivdph`) among its output variables.

## 16. Downscaling tab: high-resolution inundation

The routing grid is coarse (commonly 0.25°, ~28 km cells); this tab redistributes one day's
routing-grid flood depth onto the real ~1.85 km (1 arc-minute) sub-grid terrain within a window
you choose, using the elevation data CaMa-Flood's own map-building tools already compute.

**This is a diagnostic redistribution of the answer the model already computed -- not a re-route,
and it adds no new physics.** It cannot fix a wrong 0.25° answer; it only shows where that
answer's water would plausibly sit on real terrain. Every panel in this tab repeats that framing
for a reason: it is the easiest thing about this feature to overstate.

- Set the **Window** bounds and **Resolution** (1min down to 1sec, subject to what sub-grid data
  your map directory actually has -- the tab tells you if it's missing). **Fit to domain** fills
  the four bounds from this project's own domain instead of typing them by hand.
- **Downscale this day** processes the record selected on the time bar.
- **Downscale period → max-depth map** loops over a number of days you choose, running the tool
  once per day, and keeps a per-day area/volume time series plus a maximum-depth composite (the
  deepest value seen at each pixel over the whole period, not a single day's snapshot). The chart
  plots inundated **area** (km²) and **volume** (Mm³) together on two axes, since one shared axis
  would flatten whichever of the two has the smaller numbers.
- **Export**, once a downscale has run: **Export GeoTIFF...** saves the result as a georeferenced
  raster (WGS84) that opens directly in QGIS, ArcGIS, or any other GIS tool. **Export map
  PNG...** saves a snapshot of the map exactly as shown. **Export CSV (period)...** saves the
  period's date/area/volume series -- only available after a period run, since a single day has
  no time series to export.

Requires the `downscale_flddph_trib` tool (installed in `bin\win64\`), the sub-grid data for your
chosen resolution under the active map directory (the Mekong example ships its 1-arcminute data),
and a case that wrote `flddph` and has already been run.

## 17. Reference: mouse and keyboard controls

| Action | Control |
|---|---|
| Pan the map | Click and drag |
| Zoom the map | Mouse wheel |
| Probe a cell / place a gauge (when armed) | Double-click |
| Switch basemap, or turn it off | Layer-switcher icon, top-right of the map |
| New project | Ctrl+N |
| Open project | Ctrl+O |
| Save configuration | Ctrl+S |
| Run the selected case | F5 |

## 18. Troubleshooting

- **The installer or the program will not start, or the map tabs stay blank.** Check the
  application folder's path length: the program's map components sit about 80 characters deep
  inside it, and Windows will not load a library past 260 characters. Reinstall into a shorter
  folder.
- **An example project opens but its map is missing, or the workflow strip shows no forcing.**
  The workspace's `projects\`, `map\` and `inp\` folders must stay side by side -- the examples
  refer to their map and forcing by relative path. If you moved a single project folder on its
  own, move it back, or move the whole workspace together.
- **Run button stays disabled.** No case is selected, or no model binary resolves to a real file
  -- check **Simulation → Model binary...**.
- **A run "succeeds" (exit 0) but Run Manager showed no live diagnostics / water-balance chart.**
  On Windows this is almost always a MinGW-built binary that can't find `libgfortran` /
  `libgomp` / `libwinpthread` -- it exits immediately with no output at all. The installed model
  has these libraries beside it; a binary of your own may not. Use **Simulation → Model
  binary...**'s self-test to catch this before you rely on a run.
- **A run fails writing its output.** The case folder must be writable. Keep the workspace in your
  user folder, not under `C:\Program Files`.
- **A case is marked "stale".** Its namelist (or a file it references) changed after it was last
  run. Re-run it before trusting its results in Map & Results or Analysis.
- **Analysis tab's gauge dropdown is empty.** It reads `<project>\analysis\gauges.csv` directly --
  create that file (same `name,lon,lat[,area_km2][,observed]` format as the Map & Results CSV
  import) rather than looking for an import button on this tab.
- **A basin's discharge looks too low after cutting a regional domain.** Check the Domain tab's
  Gate 1 closure table for that basin -- a truncated basin loses its upstream runoff with no
  error from the model itself. Extend the domain until the target basin shows green.
- **Forcing "passes" visually but the run behaves oddly.** Re-check Gate 2's row-order and unit
  settings against your data provider's documentation -- a flipped latitude order or a wrong unit
  both produce a plausible-looking run, which is exactly why Gate 2 exists as an explicit check
  rather than something you're expected to eyeball.
- **A levee case's protected side (`levdph`) never shows any depth.** Check `CLEVFRC` against
  `CLEVHGT` on the Physics tab -- see [§11.2](#112-physics-the-choices-explained).
- **Run Manager flags a levee-enabled run's water balance "ABOVE LIMIT" even though nothing looks
  wrong.** Expected: a run with an active, overtopped levee closes its water balance to roughly
  1e-8 rather than the ~1e-14 a plain run holds, a known property of the levee scheme's own
  flood-stage calculation, not a sign of a broken configuration.
- **No basemap behind the layers.** The basemap tiles come from the internet -- check the
  connection or proxy. Everything else (layers, probing, gauges, colour scales) works without it.
- **Download forcing's credential label never turns green.** For ERA5-Land (Copernicus CDS),
  confirm `%USERPROFILE%\.cdsapirc` exists and has both a `url:` and `key:` line (the CDS
  website's own account page shows the exact two lines to save). For Google Earth Engine, confirm
  you have signed in to Earth Engine on this computer (`%USERPROFILE%\.config\earthengine\credentials`
  exists) *and* that you've entered and saved a Google Cloud project ID in the dialog's own field
  -- the label distinguishes "not signed in" from "signed in, but no project" so you know which
  half is missing. This app never reads a credential file's contents beyond checking that the
  entry exists.
- **"Download and convert" fails immediately with an import error for GLDAS (NASA GES DISC).**
  The installed program does not include `earthaccess`; use **GLDAS via Google Earth Engine**
  instead (see [§10.1](#101-downloading-forcing)).

## 19. About, licence and contact

**CaMa_Floodcast 1.0** -- developed by **Nguyen Xuan TINH (Ph.D.)**.

**License and Copyright © 2026**\
Nippon Koei Co.,Ltd. Water Resources & Energy Dept.\
〒102-8539: 5-4 Kojimachi, Chiyoda-ku, Tokyo, JAPAN\
Tel: +81-80-4689-7461\
E-Mail: [tinh-ng@n-koei.jp](mailto:tinh-ng@n-koei.jp)\
URL: <https://www.n-koei.co.jp/consulting/english/>

The CaMa-Flood model and its tools are distributed under the Apache License 2.0
(`docs\CaMa-Flood_LICENSE.txt`). Other third-party components bundled with the program are listed
in `docs\NOTICE.txt`. The same information is shown by **Help → About** inside the program.
