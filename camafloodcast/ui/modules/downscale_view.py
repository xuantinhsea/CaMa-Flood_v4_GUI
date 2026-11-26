"""Downscaling tab: 1-arcmin inundation from the routing-grid stage the model already computed.

The routing grid is 0.25 deg -- a cell near the equator is ~750 km2, and painting `flddph` at
that resolution produces blocky cells that look like an inundation map and are not. This tab
redistributes one day's flood depth over the ~1.85 km sub-grid pixels (`combine_hires`'s
``1min/`` data) using HAND and floodplain relief.

**This is a diagnostic redistribution of the stage the model already computed, not a re-route.**
It adds no new physics, cannot correct a wrong routing-grid answer, and every rendered field
says so -- see the framing note built into this view, which the design plan calls out
explicitly (Module 9) because it is the easiest thing about this feature to misrepresent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.downscale import (
    NODATA,
    DownscaleError,
    DownscaleTool,
    DownscaleWindow,
    InundationStats,
    PeriodResult,
    downscale_period,
    inundation_stats,
    mask_nodata,
)
from ...core.gis import write_geotiff
from ...core.maps import Grid, RiverMap
from ...core.output import OutputSet, TimeAxis
from ...core.project import Case
from ...services.render import ColorSpec
from .. import theme
from ..interactive import InteractiveMixin
from ..map import make_map_view
from ..map.renderer import MapLayer
from ..widgets.charts import HydrographChart
from ..widgets.controls import TimeBar

FRAMING_NOTE = (
    "Diagnostic redistribution of the routing-grid flood stage the model already computed -- "
    "not a re-route, and not additional physics. It cannot correct a wrong 0.25° answer; "
    "it only shows where that answer's water would sit on real terrain.")


class DownscaleThread(QThread):
    """Runs the real Fortran tool on a worker thread -- it is a blocking subprocess call."""

    done = pyqtSignal(object, object)      # (np.ndarray, InundationStats)
    failed = pyqtSignal(str)

    def __init__(self, tool: DownscaleTool, map_dir: Path, flddph_path: Path, record: int,
                window: DownscaleWindow, out_path: Path, parent=None):
        super().__init__(parent)
        self.tool = tool
        self.map_dir = map_dir
        self.flddph_path = flddph_path
        self.record = record
        self.window = window
        self.out_path = out_path

    def run(self) -> None:
        try:
            arr = self.tool.run(self.map_dir, self.flddph_path, self.record, self.window,
                                self.out_path)
        except DownscaleError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                                   # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(arr, inundation_stats(arr, self.window))


class PeriodThread(QThread):
    """Runs :func:`downscale_period` on a worker thread -- one real subprocess call per day."""

    stepped = pyqtSignal(int, int, int)    # (record, i, n)
    done = pyqtSignal(object)              # PeriodResult
    failed = pyqtSignal(str)

    def __init__(self, tool: DownscaleTool, map_dir: Path, flddph_path: Path, time: TimeAxis,
                window: DownscaleWindow, start_record: int, end_record: int, parent=None):
        super().__init__(parent)
        self.tool = tool
        self.map_dir = map_dir
        self.flddph_path = flddph_path
        self.time = time
        self.window = window
        self.start_record = start_record
        self.end_record = end_record

    def run(self) -> None:
        try:
            result = downscale_period(
                self.tool, self.map_dir, self.flddph_path, self.time, self.window,
                self.start_record, self.end_record,
                progress=lambda rec, i, n: self.stepped.emit(rec, i, n))
        except DownscaleError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                                   # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


class DownscaleView(InteractiveMixin, QWidget):
    """The Downscaling tab."""

    statusMessage = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.case: Case | None = None
        self.map_dir: Path | None = None
        self.tool: DownscaleTool | None = None
        self.outputs: OutputSet | None = None
        self._thread: DownscaleThread | None = None
        self._pthread: PeriodThread | None = None
        self._zoom_timer: QTimer | None = None
        self._last_array: np.ndarray | None = None
        self._last_bounds: tuple[float, float, float, float] | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ------------------------------------------------ left: window + controls
        left = QWidget()
        left.setMaximumWidth(300)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        note = QLabel(FRAMING_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        ll.addWidget(note)

        win = QGroupBox("Window")
        wf = QFormLayout(win)
        self.spn = {}
        for key, val in (("west", 104.0), ("east", 108.0), ("south", 8.5), ("north", 12.0)):
            s = QDoubleSpinBox()
            s.setRange(-180.0, 180.0)
            s.setDecimals(3)
            s.setSingleStep(0.25)
            s.setValue(val)
            self.spn[key] = s
            wf.addRow(key.capitalize(), s)
        self.cmb_res = QComboBox()
        self.cmb_res.addItems(["1min", "30sec", "15sec", "5sec", "3sec", "1sec"])
        wf.addRow("Resolution", self.cmb_res)
        self.btn_fit = QPushButton("Fit to domain")
        self.btn_fit.setToolTip("Set West/East/South/North from this project's own domain, "
                                "instead of the generic default window.")
        self.btn_fit.clicked.connect(self._fit_to_domain)
        wf.addRow("", self.btn_fit)
        ll.addWidget(win)

        self.lbl_missing = QLabel("")
        self.lbl_missing.setWordWrap(True)
        self.lbl_missing.setStyleSheet(f"color:{theme.WARN}; font-size:10px;")
        ll.addWidget(self.lbl_missing)

        self.time = TimeBar()
        ll.addWidget(self.time)

        self.btn_run = QPushButton("Downscale this day")
        self.btn_run.setStyleSheet("font-weight:600;")
        self.btn_run.clicked.connect(self._run)
        ll.addWidget(self.btn_run)

        period_row = QHBoxLayout()
        period_row.addWidget(QLabel("Period"))
        self.spn_period = QSpinBox()
        self.spn_period.setRange(1, 1)
        self.spn_period.setValue(1)
        self.spn_period.setSuffix(" day(s)")
        period_row.addWidget(self.spn_period, 1)
        ll.addLayout(period_row)

        self.btn_period = QPushButton("Downscale period → max-depth map")
        self.btn_period.clicked.connect(self._run_period)
        ll.addWidget(self.btn_period)
        note2 = QLabel("A period run downscales every day in the range separately (one real "
                       "invocation each) and keeps only the per-day area/volume and a "
                       "maximum-depth composite -- not every day's full field.")
        note2.setWordWrap(True)
        note2.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        ll.addWidget(note2)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        ll.addWidget(self.progress)
        ll.addStretch(1)
        root.addWidget(left)

        # ------------------------------------------------ centre: map
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        self.map = make_map_view()
        cl.addWidget(self.map, 1)
        self.coords = QLabel("")
        self.coords.setStyleSheet(f"color:{theme.MUTED};")
        cl.addWidget(self.coords)
        root.addWidget(centre, 1)
        self.map.cursorMoved.connect(
            lambda lo, la: self.coords.setText(f"Lat {la:.4f}   Lon {lo:.4f}"))

        # ------------------------------------------------ right: statistics
        right = QWidget()
        right.setMaximumWidth(300)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)

        stats_box = QGroupBox("Inundation")
        sbl = QVBoxLayout(stats_box)
        self.lbl_stats = QLabel("Run a downscale to see statistics.")
        self.lbl_stats.setWordWrap(True)
        self.lbl_stats.setStyleSheet("font-family:Consolas;")
        sbl.addWidget(self.lbl_stats)
        rl.addWidget(stats_box)

        export_box = QGroupBox("Export")
        ebl = QVBoxLayout(export_box)
        self.btn_export_tif = QPushButton("Export GeoTIFF...")
        self.btn_export_png = QPushButton("Export map PNG...")
        self.btn_export_csv = QPushButton("Export CSV (period)...")
        self.btn_export_tif.clicked.connect(self._export_geotiff)
        self.btn_export_png.clicked.connect(self._export_map_png)
        self.btn_export_csv.clicked.connect(self._export_period_csv)
        for b in (self.btn_export_tif, self.btn_export_png, self.btn_export_csv):
            b.setEnabled(False)
            ebl.addWidget(b)
        rl.addWidget(export_box)

        self.chart = HydrographChart()
        self.chart.setMinimumHeight(200)
        self.chart.setVisible(False)
        rl.addWidget(self.chart, 1)
        root.addWidget(right)

        self._set_busy(False)

    # ---------------------------------------------------------------- setup

    def configure(self, map_dir: Path | None, tool: DownscaleTool | None) -> None:
        self.map_dir = Path(map_dir) if map_dir else None
        self.tool = tool
        self._check_available()

    def _check_available(self) -> None:
        problems: list[str] = []
        if self.tool is None or not self.tool.exe.exists():
            problems.append("downscale_flddph_trib is not built (see bin/win64).")
        if self.map_dir is not None:
            res = self.cmb_res.currentText()
            if not (self.map_dir / res / "location.txt").exists():
                problems.append(f"{res} sub-grid data not found under {self.map_dir.name} -- "
                               f"produced by combine_hires in the Domain Builder.")
        else:
            problems.append("No active map.")
        self.lbl_missing.setText("  ".join(problems))
        ready = not problems and self.outputs is not None \
            and "flddph" in (self.outputs.variables if self.outputs else [])
        self.btn_run.setEnabled(ready)
        self.btn_period.setEnabled(ready and self.spn_period.maximum() > 1)
        self.btn_fit.setEnabled(self.map_dir is not None)

    def load_case(self, case: Case | None) -> None:
        self.case = case
        self.outputs = None
        self.time.configure(0)
        self.spn_period.setRange(1, 1)
        self._last_array = None
        self._last_bounds = None
        self.btn_export_tif.setEnabled(False)
        self.btn_export_png.setEnabled(False)
        self.btn_export_csv.setEnabled(False)
        if case is None or not case.exists() or self.map_dir is None:
            self._check_available()
            return
        try:
            grid = RiverMap(self.map_dir).grid
        except Exception:                                           # noqa: BLE001
            self._check_available()
            return

        tag = str(case.namelist().get("NOUTPUT", "COUTTAG", "") or "")

        outs = OutputSet(case.dir, grid, tag=tag or None)
        if "flddph" in outs.variables:
            f = outs["flddph"]
            f.time = case.time_axis(f.nrec)
            self.time.configure(f.nrec, dates=f.time.dates())
            self.spn_period.setRange(1, f.nrec)
        self.outputs = outs
        self._check_available()

    # ---------------------------------------------------------------- run

    def _set_busy(self, busy: bool, *, period_days: int | None = None) -> None:
        self.progress.setVisible(busy)
        if busy:
            if period_days:
                self.progress.setRange(0, period_days)
                self.progress.setValue(0)
            else:
                self.progress.setRange(0, 0)
        self.btn_run.setEnabled(not busy)
        self.btn_period.setEnabled(not busy)
        for w in (*self.spn.values(), self.cmb_res, self.time, self.spn_period, self.btn_fit):
            w.setEnabled(not busy)
        if not busy:
            self._check_available()

    def _window(self) -> DownscaleWindow | None:
        try:
            return DownscaleWindow(west=self.spn["west"].value(), east=self.spn["east"].value(),
                                   south=self.spn["south"].value(),
                                   north=self.spn["north"].value(),
                                   resolution=self.cmb_res.currentText())
        except ValueError as exc:
            self._warn("Downscale", str(exc))
            return None

    def _fit_to_domain(self) -> None:
        """Set West/East/South/North from this project's own domain, replacing whatever generic
        default (or another project's leftover bounds) happens to be in the spinboxes."""
        if self.map_dir is None:
            return
        try:
            grid = RiverMap(self.map_dir).grid
        except Exception as exc:                                     # noqa: BLE001
            self._warn("Downscale", f"could not read the domain: {exc}")
            return
        for key, val in (("west", grid.west), ("east", grid.east),
                         ("south", grid.south), ("north", grid.north)):
            self.spn[key].setValue(val)

    def _defer_zoom(self, bounds: tuple[float, float, float, float]) -> None:
        """Zoom on the next event-loop tick, after chart.setVisible()'s layout reflow settles.

        Parented to this widget rather than a bare ``QTimer.singleShot(0, ...)``, so it is
        destroyed (and cannot fire into a half-torn-down map) if this view closes before the
        next tick -- an unparented static timer would outlive it.
        """
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(lambda: self.map.zoom_to(*bounds))
        self._zoom_timer.start(0)

    def _run(self) -> None:
        if self.tool is None or self.map_dir is None or self.outputs is None or self.case is None:
            return
        if "flddph" not in self.outputs.variables:
            self._inform("Downscale", "This case did not write flddph. Add it to CVARSOUT in "
                                      "Model Setup and re-run before downscaling.")
            return
        window = self._window()
        if window is None:
            return

        record = self.time.record + 1     # the tool numbers records from 1; the SDK from 0
        flddph_path = self.outputs["flddph"].path
        out_path = self.case.dir / "downscale" / f"flood_rec{record}_{window.resolution}.bin"

        self._set_busy(True)
        self.statusMessage.emit(f"downscaling record {record} over "
                                f"{window.west}..{window.east}E, {window.south}..{window.north}N")
        self._thread = DownscaleThread(self.tool, self.map_dir, flddph_path, record, window,
                                       out_path, self)
        self._thread.done.connect(lambda arr, stats: self._on_done(arr, stats, window))
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_done(self, arr: np.ndarray, stats: InundationStats, window: DownscaleWindow) -> None:
        self._set_busy(False)
        self.statusMessage.emit("downscale complete")
        self.chart.setVisible(False)
        self.lbl_stats.setText(f"{window.west}..{window.east}E, {window.south}..{window.north}N "
                               f"@ {window.resolution}\n\n{stats.summary()}\n\n{FRAMING_NOTE}")

        grid = Grid(nx=arr.shape[1], ny=arr.shape[0], nlfp=1, gsize=window.csize,
                   west=window.west, east=window.east, south=window.south, north=window.north)
        vmax = max(stats.max_depth_m, 0.5)
        layer = MapLayer(
            name=f"downscaled flddph ({window.resolution})", grid=grid,
            provider=lambda _r, a=arr: a,
            spec=ColorSpec.for_variable("flddph", 0.0, vmax),
            mask=~mask_nodata(arr), unit="m", static=True, variable="flddph")
        self.map.set_layers([layer])
        self._defer_zoom((window.west, window.south, window.east, window.north))

        self._last_array = arr
        self._last_bounds = (window.west, window.south, window.east, window.north)
        self.btn_export_tif.setEnabled(True)
        self.btn_export_png.setEnabled(True)
        self.btn_export_csv.setEnabled(False)      # no time series for a single day

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.statusMessage.emit("downscale failed")
        self._critical("Downscale failed", message)

    # ---------------------------------------------------------------- run: batch over a period

    def _run_period(self) -> None:
        if self.tool is None or self.map_dir is None or self.outputs is None or self.case is None:
            return
        if "flddph" not in self.outputs.variables:
            self._inform("Downscale", "This case did not write flddph. Add it to CVARSOUT in "
                                      "Model Setup and re-run before downscaling.")
            return
        window = self._window()
        if window is None:
            return

        f = self.outputs["flddph"]
        if f.time is None:
            self._warn("Downscale", "No time axis available for this case's output.")
            return

        start0 = self.time.record
        end0 = min(start0 + self.spn_period.value() - 1, f.nrec - 1)
        n = end0 - start0 + 1

        self._set_busy(True, period_days=n)
        self.statusMessage.emit(f"downscaling {n} day(s) from {f.time.date_of(start0):%Y-%m-%d}")
        self._pthread = PeriodThread(self.tool, self.map_dir, f.path, f.time, window,
                                     start0, end0, self)
        self._pthread.stepped.connect(self._on_period_step)
        self._pthread.done.connect(lambda result: self._on_period_done(result, window))
        self._pthread.failed.connect(self._on_failed)
        self._pthread.start()

    def _on_period_step(self, record: int, i: int, n: int) -> None:
        self.progress.setMaximum(n)
        self.progress.setValue(i)
        self.statusMessage.emit(f"downscaling day {i}/{n} (record {record})")

    def _on_period_done(self, result: PeriodResult, window: DownscaleWindow) -> None:
        self._set_busy(False)
        self.statusMessage.emit("period downscale complete")

        peak = result.peak_index
        arr = result.max_depth_field
        overall_max = float(result.max_depth_m.max()) if len(result.max_depth_m) else 0.0
        self.lbl_stats.setText(
            f"{window.west}..{window.east}E, {window.south}..{window.north}N @ {window.resolution}\n"
            f"{len(result.records)} day(s), {result.dates[0]:%Y-%m-%d} .. {result.dates[-1]:%Y-%m-%d}\n\n"
            f"peak day: {result.dates[peak]:%Y-%m-%d}  "
            f"{result.area_km2[peak]:,.0f} km2, {result.volume_mcm[peak]:,.0f} Mm3\n"
            f"max depth over the period: {overall_max:.2f} m\n\n"
            f"Map shows the MAXIMUM depth per pixel over the whole period -- not a single day's "
            f"snapshot, and each pixel's maximum may fall on a different day.\n\n{FRAMING_NOTE}")

        self.chart.setVisible(True)
        self.chart.plot([("inundated area", result.area_km2)], unit="km2",
                        dates=result.dates,
                        series2=[("inundated volume", result.volume_mcm)], unit2="Mm3")

        grid = Grid(nx=arr.shape[1], ny=arr.shape[0], nlfp=1, gsize=window.csize,
                   west=window.west, east=window.east, south=window.south, north=window.north)
        vmax = max(overall_max, 0.5)
        layer = MapLayer(
            name=f"max flddph, {len(result.records)}d ({window.resolution})", grid=grid,
            provider=lambda _r, a=arr: a,
            spec=ColorSpec.for_variable("flddph", 0.0, vmax),
            mask=~mask_nodata(arr), unit="m", static=True, variable="flddph")
        self.map.set_layers([layer])
        self._defer_zoom((window.west, window.south, window.east, window.north))

        self._last_array = arr
        self._last_bounds = (window.west, window.south, window.east, window.north)
        self.btn_export_tif.setEnabled(True)
        self.btn_export_png.setEnabled(True)
        self.btn_export_csv.setEnabled(True)

    # ---------------------------------------------------------------- export

    def _export_geotiff(self) -> None:
        if self._last_array is None or self._last_bounds is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export GeoTIFF", "downscaled.tif",
                                              "GeoTIFF (*.tif)")
        if not path:
            return
        try:
            write_geotiff(path, self._last_array, self._last_bounds, nodata=NODATA)
        except Exception as exc:                                     # noqa: BLE001
            self._critical("Export GeoTIFF", str(exc))
            return
        self.statusMessage.emit(f"saved {Path(path).name}")

    def _export_map_png(self) -> None:
        if self._last_array is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export map PNG", "downscaled.png",
                                              "PNG (*.png)")
        if not path:
            return
        self.map.grab().save(path)
        self.statusMessage.emit(f"saved {Path(path).name}")

    def _export_period_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "downscale_period.csv",
                                              "CSV (*.csv)")
        if not path:
            return
        n = self.chart.export_csv(path)
        self.statusMessage.emit(f"exported {n} day(s) to {Path(path).name}"
                                if n else "nothing plotted yet")
