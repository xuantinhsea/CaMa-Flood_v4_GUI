"""Runoff Forcing tab: import, inspect, and prove coverage.

**CaMa-Flood consumes runoff, not rainfall**, and generates none of it itself -- so this tab is
not about interpolating gauges, it is about catching the three failure modes that otherwise
produce a plausible hydrograph and exit code 0: wrong units, missing days, and a flipped
latitude order (see ``core/forcing.py``). The scan-and-check button *is* Gate 2; the map preview
overlays the routing domain on the forcing field so a coverage or orientation mistake is visible
before the model ever runs.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QDate, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.errors import DiagnosticList, Severity
from ...core.forcing import (
    UNIT_DIVISOR,
    ForcingReport,
    ForcingSpec,
    audit,
    inspect_netcdf,
    interpolation_advice,
    netcdf_to_binary,
    read_day,
    scan_directory,
)
from ...core.forcing_download import (
    DownloadRequest,
    credentials_status,
    get_gee_project,
    save_gee_project,
)
from ...core.maps import Grid
from ...core.project import Case
from ...services.render import ColorSpec
from .. import theme
from ..interactive import InteractiveMixin
from ..map import make_map_view
from ..map.renderer import MapLayer

PASS_COLOUR = theme.GOOD
BLOCK_COLOUR = theme.BAD


class _Worker(QThread):
    """Run one no-argument callable off the UI thread and report its outcome."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:                                   # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


class ConvertThread(QThread):
    """netCDF -> plain binary extraction, which can take a while for a multi-year series."""

    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)          # ForcingSpec
    failed = pyqtSignal(str)

    def __init__(self, path: Path, outdir: Path, grid: Grid, variable: str | None,
                 year: int, parent=None):
        super().__init__(parent)
        self.path, self.outdir, self.grid = path, outdir, grid
        self.variable, self.year = variable, year

    def run(self) -> None:
        try:
            spec = netcdf_to_binary(
                self.path, self.outdir, grid=self.grid, variable=self.variable or None,
                year=self.year, progress=lambda i, n: self.progress.emit(i, n))
        except Exception as exc:                                   # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(spec)


class DownloadThread(QThread):
    """Fetch -> collapse to a daily total -> optionally convert to binary, chained on one
    background thread since fetching makes real network calls. When conversion runs, this emits
    the same ``ForcingSpec`` type as ``ConvertThread``, so a downloaded product flows into the
    exact same handlers (and the exact same ``scan_directory`` coverage/unit audit) a
    manually-imported netCDF file already gets -- no separate, less-scrutinised path for
    downloaded data. When conversion is skipped, it emits the daily netCDF's own ``Path``
    instead.

    The CDS/GES DISC sources fetch a raw file and aggregate it in two separate steps; the two
    Earth Engine sources fuse both into one call, since Earth Engine sums each day server-side
    before anything is downloaded -- either way this reports the same three named steps.
    """

    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)          # ForcingSpec, or Path when conversion was skipped
    failed = pyqtSignal(str)

    def __init__(self, request: DownloadRequest, outdir: Path, grid: Grid | None,
                convert: bool, parent=None):
        super().__init__(parent)
        self.request, self.outdir, self.grid, self.convert = request, outdir, grid, convert

    def run(self) -> None:
        from ...core.forcing_download import (
            era5_land_daily_totals,
            era5_land_download,
            era5_land_gee_download,
            gldas_daily_totals,
            gldas_download,
            gldas_gee_download,
        )

        try:
            raw_dir = self.outdir / "_download_raw"
            daily_nc = self.outdir / "_daily_runoff.nc"
            source = self.request.source
            self.progress.emit(0, 3)
            if source == "era5-land":
                raw = era5_land_download(self.request, raw_dir)
                self.progress.emit(1, 3)
                era5_land_daily_totals(raw, daily_nc)
            elif source == "gldas":
                raw_paths = gldas_download(self.request, raw_dir)
                self.progress.emit(1, 3)
                gldas_daily_totals(raw_paths, daily_nc, self.request)
            elif source == "era5-land-gee":
                daily_nc = era5_land_gee_download(self.request, self.outdir)
                self.progress.emit(1, 3)
            elif source == "gldas-gee":
                daily_nc = gldas_gee_download(self.request, self.outdir)
                self.progress.emit(1, 3)
            else:
                raise ValueError(f"unknown forcing download source: {source!r}")
            self.progress.emit(2, 3)

            if self.convert:
                if self.grid is None:
                    raise ValueError("no routing grid configured -- cannot convert to binary")
                result = netcdf_to_binary(daily_nc, self.outdir, grid=self.grid,
                                          variable="runoff")
            else:
                result = daily_nc
            self.progress.emit(3, 3)
        except Exception as exc:                                   # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


class ForcingView(InteractiveMixin, QWidget):
    """The Runoff Forcing tab."""

    statusMessage = pyqtSignal(str)
    validationChanged = pyqtSignal(object)   # DiagnosticList -- Gate 2 + interpolation advice
    caseUpdated = pyqtSignal(object)         # Case, after NFORCE fields were written

    UNITS = ("mm/day", "mm/hr", "mm/s", "kg/m2/s", "m/s", "m/day")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.grid: Grid | None = None
        self.case: Case | None = None
        self.report: ForcingReport | None = None
        self._scan: _Worker | None = None
        self._preview: _Worker | None = None
        self._convert: ConvertThread | None = None
        self._download: DownloadThread | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ------------------------------------------------ left: source
        left = QWidget()
        left.setMaximumWidth(340)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        src = QGroupBox("Runoff source")
        sf = QFormLayout(src)
        dirrow = QHBoxLayout()
        self.txt_dir = QLineEdit()
        btn_dir = QPushButton("...")
        btn_dir.setMaximumWidth(30)
        btn_dir.clicked.connect(self._browse_dir)
        dirrow.addWidget(self.txt_dir, 1)
        dirrow.addWidget(btn_dir)
        sf.addRow("Directory", dirrow)
        self.txt_prefix = QLineEdit("Roff____")
        self.txt_suffix = QLineEdit(".one")
        sf.addRow("Prefix", self.txt_prefix)
        sf.addRow("Suffix", self.txt_suffix)

        self.spn_nx = QSpinBox()
        self.spn_nx.setRange(1, 100_000)
        self.spn_nx.setValue(360)
        self.spn_ny = QSpinBox()
        self.spn_ny.setRange(1, 100_000)
        self.spn_ny.setValue(180)
        sf.addRow("Columns (nx)", self.spn_nx)
        sf.addRow("Rows (ny)", self.spn_ny)
        self.spn_west = QDoubleSpinBox()
        self.spn_west.setRange(-360.0, 360.0)
        self.spn_west.setValue(-180.0)
        self.spn_east = QDoubleSpinBox()
        self.spn_east.setRange(-360.0, 360.0)
        self.spn_east.setValue(180.0)
        sf.addRow("West edge", self.spn_west)
        sf.addRow("East edge", self.spn_east)

        self.cmb_unit = QComboBox()
        self.cmb_unit.addItems(self.UNITS)
        sf.addRow("Unit", self.cmb_unit)
        self.cmb_order = QComboBox()
        self.cmb_order.addItem("North to South (most products)", "NtoS")
        self.cmb_order.addItem("South to North", "StoN")
        sf.addRow("Row order", self.cmb_order)
        self.chk_bigendian = QCheckBox("Big-endian")
        sf.addRow(self.chk_bigendian)
        ll.addWidget(src)

        period = QGroupBox("Simulation period (for Gate 2)")
        pf = QFormLayout(period)
        self.dt_start = QDateEdit(QDate(2000, 1, 1))
        self.dt_start.setCalendarPopup(True)
        self.dt_start.setDisplayFormat("yyyy-MM-dd")
        self.dt_end = QDateEdit(QDate(2000, 1, 6))
        self.dt_end.setCalendarPopup(True)
        self.dt_end.setDisplayFormat("yyyy-MM-dd")
        pf.addRow("Start", self.dt_start)
        pf.addRow("End", self.dt_end)
        ll.addWidget(period)

        self.btn_scan = QPushButton("Scan and check (Gate 2)")
        self.btn_scan.setStyleSheet("font-weight:600;")
        self.btn_scan.clicked.connect(self._scan_now)
        ll.addWidget(self.btn_scan)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        ll.addWidget(self.progress)

        self.btn_apply = QPushButton("Apply to case (CROFDIR / CROFPRE / CROFSUF / DROFUNIT)")
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._apply_to_case)
        ll.addWidget(self.btn_apply)

        conv = QGroupBox("Import from netCDF")
        cf = QFormLayout(conv)
        ncrow = QHBoxLayout()
        self.txt_nc = QLineEdit()
        btn_nc = QPushButton("...")
        btn_nc.setMaximumWidth(30)
        btn_nc.clicked.connect(self._browse_nc)
        ncrow.addWidget(self.txt_nc, 1)
        ncrow.addWidget(btn_nc)
        cf.addRow("File", ncrow)
        self.txt_variable = QLineEdit()
        self.txt_variable.setPlaceholderText("auto-detect")
        cf.addRow("Variable", self.txt_variable)
        self.spn_year = QSpinBox()
        self.spn_year.setRange(1900, 2100)
        self.spn_year.setValue(2000)
        cf.addRow("Base year (no time units)", self.spn_year)
        self.lbl_nc_info = QLabel("Inspect a file to see its grid, unit and time span.")
        self.lbl_nc_info.setWordWrap(True)
        self.lbl_nc_info.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        cf.addRow(self.lbl_nc_info)
        btnrow = QHBoxLayout()
        self.btn_inspect = QPushButton("Inspect")
        self.btn_inspect.clicked.connect(self._inspect_nc)
        self.btn_convert = QPushButton("Convert to binary...")
        self.btn_convert.clicked.connect(self._convert_nc)
        btnrow.addWidget(self.btn_inspect)
        btnrow.addWidget(self.btn_convert)
        cf.addRow(btnrow)
        note = QLabel(
            "Extraction windows the product onto the current project's routing domain. Open a "
            "project with a domain before converting.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        cf.addRow(note)
        ll.addWidget(conv)
        ll.addStretch(1)
        root.addWidget(left)

        self._build_download_dialog()

        # ------------------------------------------------ centre: map
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        daybar = QHBoxLayout()
        daybar.addWidget(QLabel("Preview day"))
        self.cmb_day = QComboBox()
        self.cmb_day.setMinimumWidth(120)
        daybar.addWidget(self.cmb_day)
        self.btn_show_day = QPushButton("Show")
        self.btn_show_day.clicked.connect(self._show_day)
        daybar.addWidget(self.btn_show_day)
        daybar.addStretch(1)
        cl.addLayout(daybar)
        self.map = make_map_view()
        cl.addWidget(self.map, 1)
        self.coords = QLabel("")
        self.coords.setStyleSheet(f"color:{theme.MUTED};")
        cl.addWidget(self.coords)
        root.addWidget(centre, 1)

        # ------------------------------------------------ right: Gate 2
        right = QWidget()
        right.setMaximumWidth(400)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        gate = QGroupBox("Gate 2 — forcing coverage")
        gl = QVBoxLayout(gate)
        self.lbl_gate = QLabel("Scan a directory to check it.")
        self.lbl_gate.setWordWrap(True)
        gl.addWidget(self.lbl_gate)
        self.lbl_report = QLabel("-")
        self.lbl_report.setWordWrap(True)
        self.lbl_report.setStyleSheet("font-family:Consolas;")
        gl.addWidget(self.lbl_report)
        rl.addWidget(gate)

        gaps = QGroupBox("Missing days")
        gvl = QVBoxLayout(gaps)
        self.tbl_missing = QTableWidget(0, 1)
        self.tbl_missing.setHorizontalHeaderLabels(["date"])
        self.tbl_missing.verticalHeader().setVisible(False)
        self.tbl_missing.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_missing.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        gvl.addWidget(self.tbl_missing, 1)
        rl.addWidget(gaps, 1)

        self.lbl_interp = QLabel("")
        self.lbl_interp.setWordWrap(True)
        self.lbl_interp.setStyleSheet(f"color:{theme.MUTED};")
        rl.addWidget(self.lbl_interp)
        root.addWidget(right)

        self.map.cursorMoved.connect(
            lambda lo, la: self.coords.setText(f"Lat {la:.4f}   Lon {lo:.4f}"))
        self._update_download_creds()
        self._set_busy(False)

    def _build_download_dialog(self) -> None:
        """A standalone window (Forcing menu -> Download forcing...) rather than a panel
        embedded in this tab, so it's reachable without navigating here first. Model domain,
        duration, output location and the convert-to-binary step are all explicit and editable
        here, defaulted from what's already known (the project's own grid, this tab's own
        Start/End dates) rather than hidden state."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Download forcing")
        dlg.setMinimumWidth(420)
        dl = QVBoxLayout(dlg)

        src_row = QFormLayout()
        self.cmb_download_source = QComboBox()
        self.cmb_download_source.addItem("ERA5-Land (Copernicus CDS)", "era5-land")
        self.cmb_download_source.addItem("GLDAS Noah (NASA GES DISC)", "gldas")
        self.cmb_download_source.addItem("ERA5-Land (Google Earth Engine)", "era5-land-gee")
        self.cmb_download_source.addItem("GLDAS Noah (Google Earth Engine)", "gldas-gee")
        self.cmb_download_source.currentIndexChanged.connect(self._on_download_source_changed)
        src_row.addRow("Source", self.cmb_download_source)
        dl.addLayout(src_row)

        self.lbl_download_creds = QLabel("")
        self.lbl_download_creds.setWordWrap(True)
        self.lbl_download_creds.setStyleSheet("font-size:10px;")
        dl.addWidget(self.lbl_download_creds)

        self.w_gee_project = QWidget()
        gee_row = QHBoxLayout(self.w_gee_project)
        gee_row.setContentsMargins(0, 0, 0, 0)
        gee_row.addWidget(QLabel("Earth Engine project ID"))
        self.txt_gee_project = QLineEdit()
        self.txt_gee_project.setPlaceholderText("my-gcp-project-id")
        gee_row.addWidget(self.txt_gee_project, 1)
        btn_gee_save = QPushButton("Save")
        btn_gee_save.clicked.connect(self._save_gee_project)
        gee_row.addWidget(btn_gee_save)
        dl.addWidget(self.w_gee_project)

        domain_box = QGroupBox("Model domain")
        df = QFormLayout(domain_box)
        self.spn_dl: dict[str, QDoubleSpinBox] = {}
        for key in ("west", "east", "south", "north"):
            s = QDoubleSpinBox()
            s.setRange(-360.0, 360.0)
            s.setDecimals(3)
            self.spn_dl[key] = s
            df.addRow(key.capitalize(), s)
        self.spn_dl_gsize = QDoubleSpinBox()
        self.spn_dl_gsize.setRange(0.001, 10.0)
        self.spn_dl_gsize.setDecimals(4)
        self.spn_dl_gsize.setValue(0.25)
        df.addRow("Resolution (deg)", self.spn_dl_gsize)
        self.btn_dl_fit = QPushButton("Fit to domain")
        self.btn_dl_fit.setToolTip(
            "Set West/East/South/North/Resolution from the project's own routing grid, instead "
            "of typing them by hand. Only ERA5-Land's regrid request and both Earth Engine "
            "paths honour Resolution -- GLDAS's own API always returns its native 0.25 deg.")
        self.btn_dl_fit.clicked.connect(self._fit_download_domain)
        df.addRow("", self.btn_dl_fit)
        dl.addWidget(domain_box)

        duration_box = QGroupBox("Duration")
        duf = QFormLayout(duration_box)
        self.dt_dl_start = QDateEdit(self.dt_start.date())
        self.dt_dl_start.setCalendarPopup(True)
        self.dt_dl_start.setDisplayFormat("yyyy-MM-dd")
        self.dt_dl_end = QDateEdit(self.dt_end.date())
        self.dt_dl_end.setCalendarPopup(True)
        self.dt_dl_end.setDisplayFormat("yyyy-MM-dd")
        duf.addRow("Start", self.dt_dl_start)
        duf.addRow("End", self.dt_dl_end)
        dl.addWidget(duration_box)

        output_box = QGroupBox("Output")
        of = QFormLayout(output_box)
        outrow = QHBoxLayout()
        self.txt_dl_outdir = QLineEdit()
        btn_outdir = QPushButton("...")
        btn_outdir.setMaximumWidth(30)
        btn_outdir.clicked.connect(self._browse_download_outdir)
        outrow.addWidget(self.txt_dl_outdir, 1)
        outrow.addWidget(btn_outdir)
        of.addRow("Directory", outrow)
        self.chk_dl_convert = QCheckBox("Convert to CaMa binary forcing after download")
        self.chk_dl_convert.setChecked(True)
        of.addRow(self.chk_dl_convert)
        dl.addWidget(output_box)

        steps_row = QHBoxLayout()
        self.lbl_dl_step1 = QLabel("1. Fetch")
        self.lbl_dl_step2 = QLabel("2. Aggregate to daily")
        self.lbl_dl_step3 = QLabel("3. Convert to binary")
        for lbl in (self.lbl_dl_step1, self.lbl_dl_step2, self.lbl_dl_step3):
            steps_row.addWidget(lbl)
        steps_row.addStretch(1)
        self._reset_download_steps()
        dl.addLayout(steps_row)

        self.btn_download = QPushButton("Download and convert...")
        self.btn_download.setStyleSheet("font-weight:600;")
        self.btn_download.clicked.connect(self._download_forcing)
        dl.addWidget(self.btn_download)

        note3 = QLabel(
            "Neither product is natively daily -- ERA5-Land's hourly accumulation or GLDAS's "
            "3-hourly Qs+Qsb are collapsed into one calendar-day total each. The Google Earth "
            "Engine sources reach the same two datasets a different way (their own precomputed "
            "increment bands, sampled directly onto the domain above) and need a Google Cloud "
            "project instead of the other two sources' own credential files.")
        note3.setWordWrap(True)
        note3.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        dl.addWidget(note3)

        self._download_dialog = dlg
        self._on_download_source_changed()

    def open_download_dialog(self) -> None:
        self.dt_dl_start.setDate(self.dt_start.date())
        self.dt_dl_end.setDate(self.dt_end.date())
        self._download_dialog.show()
        self._download_dialog.raise_()
        self._download_dialog.activateWindow()

    def _on_download_source_changed(self) -> None:
        source = self.cmb_download_source.currentData()
        is_gee = source in ("era5-land-gee", "gldas-gee")
        self.w_gee_project.setVisible(is_gee)
        if is_gee and not self.txt_gee_project.text().strip():
            saved = get_gee_project()
            if saved:
                self.txt_gee_project.setText(saved)
        self._update_download_creds()

    def _save_gee_project(self) -> None:
        project = self.txt_gee_project.text().strip()
        if not project:
            return
        save_gee_project(project)
        self._update_download_creds()
        self.statusMessage.emit(f"saved Earth Engine project '{project}'")

    def _fit_download_domain(self) -> None:
        if self.grid is None:
            return
        self.spn_dl["west"].setValue(self.grid.west)
        self.spn_dl["east"].setValue(self.grid.east)
        self.spn_dl["south"].setValue(self.grid.south)
        self.spn_dl["north"].setValue(self.grid.north)
        self.spn_dl_gsize.setValue(self.grid.gsize)

    def _browse_download_outdir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Where should the downloaded forcing go?",
                                             self.txt_dl_outdir.text())
        if d:
            self.txt_dl_outdir.setText(d)

    def _reset_download_steps(self) -> None:
        for lbl in (self.lbl_dl_step1, self.lbl_dl_step2, self.lbl_dl_step3):
            lbl.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")

    # ---------------------------------------------------------------- setup

    def configure(self, grid: Grid | None) -> None:
        """The routing grid: needed to compare cell size and to window a netCDF import."""
        self.grid = grid
        self.btn_convert.setEnabled(grid is not None)
        self._fit_download_domain()
        self._update_download_creds()
        if self.report is not None:
            self._update_gate()

    def load_case(self, case: Case | None) -> None:
        """Prefill from a case's NFORCE / NSIMTIME groups, if it has them."""
        self.case = case
        self.btn_apply.setEnabled(case is not None and case.exists())
        if case is None or not case.exists():
            return
        try:
            nml = case.namelist()
        except Exception:                                          # noqa: BLE001
            return

        d = str(nml.get("NFORCE", "CROFDIR", "") or "")
        if d:
            # Relative paths are relative to the case directory, the model's own cwd
            # (an absolute path is returned unchanged by the join).
            self.set_directory(case.dir / d,
                               prefix=str(nml.get("NFORCE", "CROFPRE", "Roff____")),
                               suffix=str(nml.get("NFORCE", "CROFSUF", ".one")))
        du = nml.get("NFORCE", "DROFUNIT", None)
        if du:
            unit = next((u for u, v in UNIT_DIVISOR.items() if abs(v - float(du)) < 1.0), None)
            if unit:
                i = self.cmb_unit.findText(unit)
                if i >= 0:
                    self.cmb_unit.setCurrentIndex(i)

        try:
            start = dt.date(int(nml.get("NSIMTIME", "SYEAR", 2000)),
                            int(nml.get("NSIMTIME", "SMON", 1)),
                            int(nml.get("NSIMTIME", "SDAY", 1)))
            end = dt.date(int(nml.get("NSIMTIME", "EYEAR", 2000)),
                          int(nml.get("NSIMTIME", "EMON", 1)),
                          int(nml.get("NSIMTIME", "EDAY", 1)))
            self.set_period(start, end)
        except (TypeError, ValueError):
            pass

    def set_period(self, start: dt.date, end: dt.date) -> None:
        self.dt_start.setDate(QDate(start.year, start.month, start.day))
        self.dt_end.setDate(QDate(end.year, end.month, end.day))

    def set_directory(self, path: Path, *, prefix: str = "Roff____", suffix: str = ".one",
                      unit: str = "mm/day") -> None:
        self.txt_dir.setText(str(path))
        self.txt_prefix.setText(prefix)
        self.txt_suffix.setText(suffix)
        i = self.cmb_unit.findText(unit)
        if i >= 0:
            self.cmb_unit.setCurrentIndex(i)

    # ---------------------------------------------------------------- spec

    def _spec(self) -> ForcingSpec:
        return ForcingSpec(
            directory=Path(self.txt_dir.text().strip() or "."),
            prefix=self.txt_prefix.text(), suffix=self.txt_suffix.text(),
            nx=self.spn_nx.value(), ny=self.spn_ny.value(),
            unit=self.cmb_unit.currentText(),
            lat_order=self.cmb_order.currentData(),
            big_endian=self.chk_bigendian.isChecked(),
            west=self.spn_west.value(), east=self.spn_east.value())

    def _period(self) -> tuple[dt.date, dt.date]:
        s, e = self.dt_start.date(), self.dt_end.date()
        return (dt.date(s.year(), s.month(), s.day()), dt.date(e.year(), e.month(), e.day()))

    # ---------------------------------------------------------------- scan / Gate 2

    def _set_busy(self, busy: bool) -> None:
        self.progress.setVisible(busy)
        self.btn_scan.setEnabled(not busy)
        self.btn_convert.setEnabled(not busy and self.grid is not None)

    # ---------------------------------------------------------------- download

    def _update_download_creds(self) -> None:
        source = self.cmb_download_source.currentData()
        status = credentials_status()
        ok = bool(status.get(source, False))
        if ok:
            self.lbl_download_creds.setText("Credentials found.")
            self.lbl_download_creds.setStyleSheet(f"color:{theme.GOOD}; font-size:10px;")
        elif source == "era5-land":
            self.lbl_download_creds.setText(
                "No ~/.cdsapirc found -- register a free account at "
                "cds.climate.copernicus.eu, generate an API key, and save it there first.")
            self.lbl_download_creds.setStyleSheet(f"color:{theme.WARN}; font-size:10px;")
        elif source == "gldas":
            self.lbl_download_creds.setText(
                "No urs.earthdata.nasa.gov entry in ~/.netrc found -- register a free NASA "
                "Earthdata Login at urs.earthdata.nasa.gov and save it there first.")
            self.lbl_download_creds.setStyleSheet(f"color:{theme.WARN}; font-size:10px;")
        else:
            gee_signed_in = (Path.home() / ".config" / "earthengine" / "credentials").is_file()
            if not gee_signed_in:
                self.lbl_download_creds.setText(
                    "Not signed in to Google Earth Engine -- run 'earthengine authenticate' or "
                    "ee.Authenticate() once, then set a project ID above.")
            else:
                self.lbl_download_creds.setText(
                    "Signed in to Earth Engine, but no project ID is set -- enter one above "
                    "and click Save.")
            self.lbl_download_creds.setStyleSheet(f"color:{theme.WARN}; font-size:10px;")
        self.btn_download.setEnabled(ok and self.grid is not None)

    def _download_forcing(self) -> None:
        source = self.cmb_download_source.currentData()
        is_gee = source in ("era5-land-gee", "gldas-gee")
        if is_gee and not self.txt_gee_project.text().strip():
            self._warn("Download forcing", "Set and save an Earth Engine project ID first.")
            return
        outdir_text = self.txt_dl_outdir.text().strip()
        if not outdir_text:
            self._inform("Download forcing", "Choose an output directory first.")
            return
        convert = self.chk_dl_convert.isChecked()
        if convert and self.grid is None:
            self._inform("Download forcing", "No routing grid configured -- open a project "
                                              "with a domain first, or uncheck 'Convert to "
                                              "CaMa binary forcing after download'.")
            return

        start = self.dt_dl_start.date().toPyDate()
        end = self.dt_dl_end.date().toPyDate()
        try:
            request = DownloadRequest(
                source=source,
                west=self.spn_dl["west"].value(), south=self.spn_dl["south"].value(),
                east=self.spn_dl["east"].value(), north=self.spn_dl["north"].value(),
                gsize=self.spn_dl_gsize.value(), start=start, end=end)
        except ValueError as exc:
            self._warn("Download forcing", str(exc))
            return

        self._reset_download_steps()
        self._set_download_busy(True)
        self.statusMessage.emit(f"downloading {self.cmb_download_source.currentText()} ...")
        self._download = DownloadThread(request, Path(outdir_text), self.grid, convert, self)
        self._download.progress.connect(self._on_download_progress)
        self._download.done.connect(self._on_download_done)
        self._download.failed.connect(self._on_download_failed)
        self._download.start()

    def _set_download_busy(self, busy: bool) -> None:
        self.btn_download.setEnabled(not busy)
        self.cmb_download_source.setEnabled(not busy)
        if not busy:
            self._update_download_creds()

    def _on_download_progress(self, i: int, n: int) -> None:
        self.statusMessage.emit(f"download step {i}/{n}")
        for k, lbl in enumerate((self.lbl_dl_step1, self.lbl_dl_step2, self.lbl_dl_step3)):
            lbl.setStyleSheet(f"color:{theme.GOOD if i > k else theme.MUTED}; font-size:10px;")

    def _on_download_done(self, result) -> None:
        self._set_download_busy(False)
        if isinstance(result, ForcingSpec):
            self._on_converted(result)
        else:
            self.statusMessage.emit(f"saved daily-total netCDF to {result}")
            self._inform("Download forcing",
                         f"Saved the daily-total netCDF to:\n{result}\n\n"
                         "Conversion to binary was skipped ('Convert to CaMa binary forcing "
                         "after download' was unchecked).")

    def _on_download_failed(self, message: str) -> None:
        self._set_download_busy(False)
        self._critical("Download forcing", message)
        self.statusMessage.emit("download failed")

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Runoff directory", self.txt_dir.text())
        if d:
            self.txt_dir.setText(d)

    def _scan_now(self) -> None:
        d = Path(self.txt_dir.text().strip())
        if not d.is_dir():
            self._warn("Scan", f"'{d}' is not a directory.")
            return
        spec = self._spec()
        self._set_busy(True)
        self.statusMessage.emit(f"scanning {spec.directory} ...")
        self._scan = _Worker(lambda: scan_directory(spec))
        self._scan.done.connect(self._on_scanned)
        self._scan.failed.connect(self._on_scan_failed)
        self._scan.start()

    def _on_scan_failed(self, message: str) -> None:
        self._set_busy(False)
        self.report = None
        self.lbl_gate.setText(f"could not scan: {message}")
        self.lbl_gate.setStyleSheet(f"color:{BLOCK_COLOUR};")
        self.statusMessage.emit("forcing scan failed")

    def _on_scanned(self, report: ForcingReport) -> None:
        self._set_busy(False)
        self.report = report
        self.lbl_report.setText(report.summary())
        self.statusMessage.emit(f"scanned {report.n_files} forcing file(s)")

        self.cmb_day.clear()
        if report.first_day and report.last_day:
            span = (report.last_day - report.first_day).days
            step = max(1, span // 30)
            d = report.first_day
            while d <= report.last_day:
                self.cmb_day.addItem(d.isoformat(), d)
                d += dt.timedelta(days=step)
            if self.cmb_day.findData(report.last_day) < 0:
                self.cmb_day.addItem(report.last_day.isoformat(), report.last_day)

        shown = report.missing[:200]
        self.tbl_missing.setRowCount(len(shown))
        for r, day in enumerate(shown):
            self.tbl_missing.setItem(r, 0, QTableWidgetItem(day.isoformat()))

        self._update_gate()

    def _update_gate(self) -> None:
        if self.report is None:
            return
        spec = self.report.spec
        start, end = self._period()
        diags = audit(spec, start, end, report=self.report)
        if self.grid is not None:
            diags.extend(interpolation_advice(spec, self.grid))
        self.validationChanged.emit(diags)

        if diags.ok:
            self.lbl_gate.setText(
                "PASS — forcing covers the simulated period in plausible units.")
            self.lbl_gate.setStyleSheet(f"color:{PASS_COLOUR};")
        else:
            errors = "; ".join(d.message for d in diags.of(Severity.ERROR))
            self.lbl_gate.setText(f"BLOCKED — {errors}")
            self.lbl_gate.setStyleSheet(f"color:{BLOCK_COLOUR};")

        interp = next((d for d in diags if d.code in ("FRC_DIRECT_OK", "FRC_NEEDS_INPMAT")), None)
        if interp:
            self.lbl_interp.setText(interp.message)
        elif self.grid is None:
            self.lbl_interp.setText(
                "Open a project with a domain to compare the forcing grid against the "
                "routing grid.")
        else:
            self.lbl_interp.setText("")

    # ---------------------------------------------------------------- preview

    def _show_day(self) -> None:
        if self.report is None:
            return
        day = self.cmb_day.currentData()
        if day is None:
            return
        spec = self.report.spec
        self.btn_show_day.setEnabled(False)
        self._preview = _Worker(lambda: read_day(spec, day))
        self._preview.done.connect(lambda arr, d=day, s=spec: self._on_day_ready(arr, d, s))
        self._preview.failed.connect(self._on_day_failed)
        self._preview.start()

    def _on_day_failed(self, message: str) -> None:
        self.btn_show_day.setEnabled(True)
        self.statusMessage.emit(f"could not read forcing day: {message}")

    def _on_day_ready(self, arr: np.ndarray, day: dt.date, spec: ForcingSpec) -> None:
        self.btn_show_day.setEnabled(True)
        grid = Grid(nx=spec.nx, ny=spec.ny, nlfp=1, gsize=spec.gsize,
                   west=spec.west, east=spec.east, south=spec.south, north=spec.north)
        finite = arr[np.isfinite(arr)]
        vmax = max(float(np.nanpercentile(finite, 98)), 1e-6) if finite.size else 1.0
        layer = MapLayer(
            name="runoff", grid=grid,
            provider=lambda _r, a=arr: a,
            spec=ColorSpec(cmap="YlGnBu", vmin=0.0, vmax=vmax, scale="linear", opacity=0.9),
            unit=spec.unit, static=True, variable="runoff")
        self.map.set_layers([layer])
        self.map.zoom_to_layers()
        if self.grid is not None:
            self.map.set_domain_rect(
                (self.grid.west, self.grid.south, self.grid.east, self.grid.north))
        self.statusMessage.emit(f"showing forcing for {day.isoformat()}")

    # ---------------------------------------------------------------- netCDF import

    def _browse_nc(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a netCDF runoff file", "", "netCDF (*.nc *.nc4);;All files (*)")
        if path:
            self.txt_nc.setText(path)

    def _inspect_nc(self) -> None:
        path = self.txt_nc.text().strip()
        if not path:
            return
        try:
            info = inspect_netcdf(path, self.txt_variable.text().strip() or None)
        except Exception as exc:                                   # noqa: BLE001
            self._warn("Inspect netCDF", str(exc))
            return
        self.txt_variable.setText(info["variable"])
        self.lbl_nc_info.setText(
            f"{info['nx']} x {info['ny']} x {info['ntime']} step(s), unit '{info['unit']}', "
            f"{info['lat_order']} (lat {info['lat_first']:.2f} .. {info['lat_last']:.2f}), "
            f"time units '{info['time_units']}'")

    def _convert_nc(self) -> None:
        if self.grid is None:
            self._inform("Convert", "No routing grid configured -- open a project with a "
                                    "domain first, so the extraction knows what to window onto.")
            return
        path = self.txt_nc.text().strip()
        if not path:
            return
        outdir = QFileDialog.getExistingDirectory(self, "Where should the binary forcing go?")
        if not outdir:
            return
        self._set_busy(True)
        self.statusMessage.emit("converting netCDF forcing ...")
        self._convert = ConvertThread(Path(path), Path(outdir), self.grid,
                                      self.txt_variable.text().strip(),
                                      self.spn_year.value(), self)
        self._convert.done.connect(self._on_converted)
        self._convert.failed.connect(self._on_convert_failed)
        self._convert.start()

    def _on_convert_failed(self, message: str) -> None:
        self._set_busy(False)
        self._critical("Convert", message)
        self.statusMessage.emit("netCDF conversion failed")

    def _on_converted(self, spec: ForcingSpec) -> None:
        self._set_busy(False)
        self.txt_dir.setText(str(spec.directory))
        self.txt_prefix.setText(spec.prefix)
        self.txt_suffix.setText(spec.suffix)
        self.spn_nx.setValue(spec.nx)
        self.spn_ny.setValue(spec.ny)
        self.spn_west.setValue(spec.west)
        self.spn_east.setValue(spec.east)
        i = self.cmb_unit.findText(spec.unit)
        if i >= 0:
            self.cmb_unit.setCurrentIndex(i)
        i = self.cmb_order.findData(spec.lat_order)
        if i >= 0:
            self.cmb_order.setCurrentIndex(i)
        self.statusMessage.emit(f"converted to {spec.directory}")
        self._scan_now()

    # ---------------------------------------------------------------- apply

    def _apply_to_case(self) -> None:
        if self.case is None:
            return
        spec = self._spec()
        directory = str(spec.directory)
        if not directory.endswith(("/", "\\")):
            directory += "/"
        try:
            nml = self.case.namelist()
            nml.update("NFORCE", {
                "CROFDIR": directory, "CROFPRE": spec.prefix, "CROFSUF": spec.suffix,
                "DROFUNIT": spec.drofunit,
            })
            nml.save(backup=True)
        except Exception as exc:                                   # noqa: BLE001
            self._critical("Apply to case", str(exc))
            return
        self.statusMessage.emit(f"forcing settings written to {self.case.name}")
        self.caseUpdated.emit(self.case)
