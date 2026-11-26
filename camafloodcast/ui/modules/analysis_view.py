"""Analysis tab: scenario comparison -- A minus B difference maps and paired hydrographs.

Two cases in the same project share the same routing grid by construction (one project, one
domain, many cases), so comparing them is well posed: a cell's difference means the same thing
in both. This tab is deliberately restricted to that -- comparing across two different domains
would be comparing different things entirely, and `diff_field` (core/analysis.py) refuses it.

No worker thread here, unlike Domain Builder or Downscaling: a difference is two memory-mapped
reads and a subtraction, not a subprocess call, so it stays well under the ~200ms rule of thumb
for blocking the UI thread.
"""

from __future__ import annotations

import numpy as np
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.analysis import (
    Gauge,
    compare,
    diff_field,
    extract_hydrograph,
    load_gauges,
    longitudinal_profile,
    snap_gauge,
    trace_downstream,
)
from ...core.maps import RiverMap
from ...core.output import OutputSet
from ...core.project import Project
from ...services.render import ColorSpec
from .. import theme
from ..interactive import InteractiveMixin
from ..map import make_map_view
from ..map.renderer import MapLayer
from ..widgets.charts import HydrographChart
from ..widgets.controls import TimeBar

FRAMING_NOTE = (
    "A − B at each cell. Both cases share this project's routing grid, so a cell means "
    "the same thing in both -- this compares two configurations of the same study, not two "
    "different domains.")


class AnalysisView(InteractiveMixin, QWidget):
    """The Analysis tab: scenario comparison."""

    statusMessage = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.project: Project | None = None
        self.river_map: RiverMap | None = None
        self.outs_a: OutputSet | None = None
        self.outs_b: OutputSet | None = None
        self.gauges: list[Gauge] = []
        self._zoom_timer: QTimer | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ------------------------------------------------ left: scenarios + variable
        left = QWidget()
        left.setMaximumWidth(300)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        note = QLabel(FRAMING_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        ll.addWidget(note)

        box = QGroupBox("Scenarios")
        bf = QFormLayout(box)
        self.cmb_a = QComboBox()
        self.cmb_b = QComboBox()
        self.cmb_var = QComboBox()
        bf.addRow("Case A", self.cmb_a)
        bf.addRow("Case B", self.cmb_b)
        bf.addRow("Variable", self.cmb_var)
        ll.addWidget(box)

        self.lbl_missing = QLabel("")
        self.lbl_missing.setWordWrap(True)
        self.lbl_missing.setStyleSheet(f"color:{theme.WARN}; font-size:10px;")
        ll.addWidget(self.lbl_missing)

        self.time = TimeBar()
        ll.addWidget(self.time)

        self.btn_map = QPushButton("Show difference map")
        self.btn_map.setStyleSheet("font-weight:600;")
        self.btn_map.clicked.connect(self._show_diff_map)
        ll.addWidget(self.btn_map)

        gbox = QGroupBox("Gauge (from analysis/gauges.csv)")
        gf = QFormLayout(gbox)
        self.cmb_gauge = QComboBox()
        gf.addRow("Gauge", self.cmb_gauge)
        ll.addWidget(gbox)
        self.btn_hydro = QPushButton("Compare hydrographs")
        self.btn_hydro.clicked.connect(self._show_hydrograph)
        ll.addWidget(self.btn_hydro)
        self.btn_profile = QPushButton("Show profile: gauge → outlet (Case A)")
        self.btn_profile.clicked.connect(self._show_profile)
        ll.addWidget(self.btn_profile)
        pnote = QLabel("Traces nextxy from the gauge to a river mouth or inland sink and plots "
                       "bed elevation and water surface at the day selected above -- a "
                       "diagnostic trace, not a hydraulic solve of its own.")
        pnote.setWordWrap(True)
        pnote.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        ll.addWidget(pnote)

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

        # ------------------------------------------------ right: stats + chart
        right = QWidget()
        right.setMaximumWidth(300)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        stats_box = QGroupBox("Comparison")
        sbl = QVBoxLayout(stats_box)
        self.lbl_stats = QLabel("Pick two cases and compare.")
        self.lbl_stats.setWordWrap(True)
        self.lbl_stats.setStyleSheet("font-family:Consolas;")
        sbl.addWidget(self.lbl_stats)
        rl.addWidget(stats_box)
        self.chart = HydrographChart()
        self.chart.setMinimumHeight(200)
        self.chart.setVisible(False)
        rl.addWidget(self.chart, 1)
        root.addWidget(right)

        self.cmb_a.currentIndexChanged.connect(self._cases_changed)
        self.cmb_b.currentIndexChanged.connect(self._cases_changed)
        self._check_available()

    # ---------------------------------------------------------------- setup

    def set_project(self, project: Project | None) -> None:
        self.project = project
        self.river_map = None
        self.outs_a = self.outs_b = None
        self.gauges = []

        for cmb in (self.cmb_a, self.cmb_b, self.cmb_var, self.cmb_gauge):
            cmb.blockSignals(True)
            cmb.clear()
            cmb.blockSignals(False)
        self.time.configure(0)
        self.chart.setVisible(False)

        if project is None:
            self._check_available()
            return

        names = [c.name for c in project.cases() if c.exists()]
        self.cmb_a.addItems(names)
        self.cmb_b.addItems(names)
        if len(names) >= 2:
            self.cmb_b.setCurrentIndex(1)

        try:
            self.river_map = RiverMap(project.map_dir)
        except Exception:                                           # noqa: BLE001
            self.river_map = None

        gauges_csv = project.root / "analysis" / "gauges.csv"
        if gauges_csv.exists():
            try:
                loaded = load_gauges(gauges_csv)
            except Exception:                                       # noqa: BLE001
                loaded = []
            self.gauges = ([snap_gauge(g, self.river_map) for g in loaded]
                           if self.river_map is not None else loaded)
        self.cmb_gauge.addItems([g.name for g in self.gauges])

        self._cases_changed()

    def _check_available(self) -> None:
        problems: list[str] = []
        if self.project is None:
            problems.append("No project open.")
        elif self.river_map is None:
            problems.append("No active map.")
        elif self.cmb_a.count() < 1:
            problems.append("No cases in this project yet.")
        self.lbl_missing.setText("  ".join(problems))

        # Difference map / hydrograph comparison need both A and B; the profile only needs A
        # (it traces one case's own output along the river network, not a comparison).
        both = not problems and self.outs_a is not None and self.outs_b is not None \
            and self.cmb_var.count() > 0
        self.btn_map.setEnabled(both)
        self.btn_hydro.setEnabled(both and self.cmb_gauge.count() > 0)
        self.btn_profile.setEnabled(not problems and self.outs_a is not None
                                    and self.cmb_gauge.count() > 0)

    def _cases_changed(self) -> None:
        self.outs_a = self.outs_b = None
        self.cmb_var.blockSignals(True)
        self.cmb_var.clear()
        self.cmb_var.blockSignals(False)
        self.time.configure(0)

        if self.project is None or self.river_map is None:
            self._check_available()
            return

        grid = self.river_map.grid
        name_a = self.cmb_a.currentText()
        case_a = self.project.case(name_a) if name_a else None
        if case_a is not None and case_a.exists():
            tag_a = str(case_a.namelist().get("NOUTPUT", "COUTTAG", "") or "") or None
            self.outs_a = OutputSet(case_a.dir, grid, tag=tag_a)

        name_b = self.cmb_b.currentText()
        if self.outs_a is not None and name_b and name_b != name_a:
            case_b = self.project.case(name_b)
            if case_b.exists():
                tag_b = str(case_b.namelist().get("NOUTPUT", "COUTTAG", "") or "") or None
                self.outs_b = OutputSet(case_b.dir, grid, tag=tag_b)

        if self.outs_a is not None and self.outs_b is not None:
            variables = sorted(set(self.outs_a.variables) & set(self.outs_b.variables))
        elif self.outs_a is not None:
            variables = self.outs_a.variables       # profile-only mode: just A's own variables
        else:
            variables = []
        self.cmb_var.addItems(variables)

        if self.outs_a is not None and variables:
            f = self.outs_a[variables[0]]
            f.time = case_a.time_axis(f.nrec)
            self.time.configure(f.nrec, dates=f.time.dates())

        self._check_available()

    def _defer_zoom(self, bounds: tuple[float, float, float, float]) -> None:
        """Zoom on the next event-loop tick, after any pending layout reflow settles.

        Calling zoom_to() in the same method as chart.setVisible() can fit against a viewport
        size that changes moments later (see the module-level note in downscale_view.py's
        equivalent). The timer is parented to this widget, not a bare
        ``QTimer.singleShot(0, ...)``, so it is destroyed (and cannot fire on a half-torn-down
        widget) if this view closes before the next tick -- an unparented static timer would
        outlive it and call back into a possibly-deleted map widget.
        """
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(lambda: self.map.zoom_to(*bounds))
        self._zoom_timer.start(0)

    # ---------------------------------------------------------------- difference map

    def _show_diff_map(self) -> None:
        if self.outs_a is None or self.outs_b is None or self.river_map is None:
            return
        var = self.cmb_var.currentText()
        if not var:
            return
        try:
            d = diff_field(self.outs_a[var], self.outs_b[var], self.time.record,
                           mask=self.river_map.land_mask)
        except (KeyError, IndexError, ValueError) as exc:
            self._warn("Analysis", str(exc))
            return

        self.chart.setVisible(False)
        self.lbl_stats.setText(
            f"{self.cmb_a.currentText()} − {self.cmb_b.currentText()}  ({var})\n\n"
            f"mean |diff|: {d.mean_abs_diff:.4g}\n"
            f"max  |diff|: {d.max_abs_diff:.4g}\n"
            f"rms  diff  : {d.rms_diff:.4g}\n\n{FRAMING_NOTE}")

        grid = self.river_map.grid
        # The 98th percentile, not the raw max: one outlier cell otherwise sets the whole
        # scale and every more-typical difference disappears into the "no change" colour
        # (see services/render.py's own docstring on exactly this failure mode).
        masked = np.abs(d.diff[self.river_map.land_mask])
        vmax = max(float(np.percentile(masked, 98)) if masked.size else 0.0, 1e-6)
        layer = MapLayer(
            name=f"{var}: A − B", grid=grid,
            provider=lambda _r, a=d.diff: a,
            spec=ColorSpec(cmap="RdBu_r", vmin=-vmax, vmax=vmax, scale="linear", opacity=0.9),
            mask=self.river_map.land_mask, unit=self.outs_a[var].unit, static=True, variable=var)
        self.map.set_layers([layer])
        self._defer_zoom(grid.bounds)
        self.statusMessage.emit(f"difference map: {var} at record {self.time.record + 1}")

    # ---------------------------------------------------------------- hydrograph comparison

    def _show_hydrograph(self) -> None:
        if self.outs_a is None or self.outs_b is None:
            return
        var = self.cmb_var.currentText()
        gname = self.cmb_gauge.currentText()
        gauge = next((g for g in self.gauges if g.name == gname), None)
        if not var or gauge is None or not gauge.snapped:
            return
        try:
            ha = extract_hydrograph(self.outs_a[var], gauge)
            hb = extract_hydrograph(self.outs_b[var], gauge)
        except KeyError as exc:
            self._warn("Analysis", str(exc))
            return

        metrics = compare(hb.analysed, ha.analysed)     # B relative to A as the reference
        self.chart.setVisible(True)
        self.chart.plot([(self.cmb_a.currentText(), ha.values),
                        (self.cmb_b.currentText(), hb.values)], unit=ha.unit)
        self.lbl_stats.setText(
            f"{gauge.name}  ({var})\n\n"
            f"{self.cmb_a.currentText()}: mean {ha.mean:,.1f}, peak {ha.peak:,.1f}\n"
            f"{self.cmb_b.currentText()}: mean {hb.mean:,.1f}, peak {hb.peak:,.1f}\n\n"
            f"B vs A: {metrics.summary()}\n\n{FRAMING_NOTE}")
        self.statusMessage.emit(f"hydrograph comparison: {var} at {gauge.name}")

    # ---------------------------------------------------------------- longitudinal profile

    def _show_profile(self) -> None:
        if self.outs_a is None or self.river_map is None:
            return
        gname = self.cmb_gauge.currentText()
        gauge = next((g for g in self.gauges if g.name == gname), None)
        if gauge is None or not gauge.snapped:
            return
        if "sfcelv" not in self.outs_a.variables:
            self._inform("Analysis", f"{self.cmb_a.currentText()} did not write sfcelv. Add it "
                                     "to CVARSOUT in Model Setup and re-run to see a "
                                     "water-surface profile.")
            return

        try:
            path = trace_downstream(self.river_map, gauge.ix, gauge.iy)
        except ValueError as exc:
            self._warn("Analysis", str(exc))
            return

        record = self.time.record if self.time.count else 0
        rivdph = self.outs_a["rivdph"] if "rivdph" in self.outs_a.variables else None
        profile = longitudinal_profile(self.river_map, path, self.outs_a["sfcelv"], record,
                                       rivdph=rivdph)

        self.chart.setVisible(True)
        self.chart.plot([("water surface", profile.sfcelv_m), ("channel bed", profile.elevtn_m)],
                        unit="m", x=profile.distance_km, xlabel="distance from gauge [km]")

        self.lbl_stats.setText(
            f"{self.cmb_a.currentText()}: {gauge.name} → outlet  "
            f"({len(path)} cells, {profile.distance_km[-1]:,.0f} km)\n\n"
            f"bed elevation  : {profile.elevtn_m[0]:.1f} → {profile.elevtn_m[-1]:.1f} m\n"
            f"water surface  : {profile.sfcelv_m[0]:.1f} → {profile.sfcelv_m[-1]:.1f} m\n\n"
            f"Traces nextxy from the gauge to a river mouth or inland sink; a diagnostic trace "
            f"along the network, not a hydraulic solve of its own.")

        grid = self.river_map.grid
        lonlat = [grid.centre_of(ix, iy) for ix, iy in path]
        self.map.set_layers([])
        self.map.set_markers([(lo, la, theme.MARKER_BLUE, "") for lo, la in lonlat])
        lons, lats = [p[0] for p in lonlat], [p[1] for p in lonlat]
        pad = grid.gsize * 2
        bounds = (min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad)
        self._defer_zoom(bounds)
        self.statusMessage.emit(f"profile: {gauge.name} to outlet ({len(path)} cells)")
