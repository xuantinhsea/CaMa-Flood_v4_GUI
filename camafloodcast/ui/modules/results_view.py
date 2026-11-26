"""Map & Results tab: the main viewer.

Layout follows RRI_Floodcast -- gauges and variables on the left, map in the centre with a time
bar above it, layer stack on the right, plot below.

One deliberate addition: a permanent **resolution badge** under the map stating what is being
displayed. A 0.25 deg cell is roughly 28 km across and ~750 km2; painting `flddph` at that
resolution produces blocky cells that look like an inundation map and are not. Downscaling to
1 arc-min arrives in Phase 3; until then the badge says plainly what the user is looking at.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.analysis import (
    Gauge,
    extract_hydrograph,
    interpretation_notes,
    levee_protection_stats,
    snap_gauge,
)
from ...core.maps import RiverMap
from ...core.output import CAVEATS, OutputSet, display_name
from ...core.project import Case
from ...services.render import ColorSpec, format_value
from ..map import make_map_view
from ..map.renderer import MapLayer
from .. import theme
from ..widgets.charts import HydrographChart
from ..widgets.controls import LayerPanel, TimeBar


class ResultsView(QWidget):
    """The Map & Results tab."""

    statusMessage = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.case: Case | None = None
        self.river_map: RiverMap | None = None
        self.outputs: OutputSet | None = None
        self.gauges: list[Gauge] = []

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ------------------------------------------------ left: gauges + variables
        left = QWidget()
        left.setMaximumWidth(330)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(6)

        g_box = QGroupBox("Gauges")
        gl = QVBoxLayout(g_box)
        btns = QHBoxLayout()
        self.btn_add_gauge = QPushButton("+ Add via map")
        self.btn_import_gauges = QPushButton("Import CSV...")
        btns.addWidget(self.btn_add_gauge)
        btns.addWidget(self.btn_import_gauges)
        gl.addLayout(btns)

        self.gauge_table = QTableWidget(0, 4)
        self.gauge_table.setHorizontalHeaderLabels(["Name", "Lon", "Lat", "Area km2"])
        self.gauge_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.gauge_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.gauge_table.verticalHeader().setVisible(False)
        self.gauge_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.gauge_table.setMaximumHeight(170)
        gl.addWidget(self.gauge_table)

        self.btn_del_gauge = QPushButton("Remove selected")
        gl.addWidget(self.btn_del_gauge)
        ll.addWidget(g_box)

        v_box = QGroupBox("Variables")
        vl = QVBoxLayout(v_box)
        self.var_list = QComboBox()
        vl.addWidget(self.var_list)
        self.var_caveat = QLabel("")
        self.var_caveat.setWordWrap(True)
        self.var_caveat.setStyleSheet(f"color:{theme.WARN}; font-size:10px;")
        vl.addWidget(self.var_caveat)
        self.btn_add_layer = QPushButton("Show on map")
        vl.addWidget(self.btn_add_layer)
        self.btn_plot = QPushButton("Plot at gauges")
        self.btn_plot.setStyleSheet("font-weight:600;")
        vl.addWidget(self.btn_plot)
        exp = QHBoxLayout()
        self.btn_csv = QPushButton("Export CSV")
        self.btn_png = QPushButton("Export PNG")
        exp.addWidget(self.btn_csv)
        exp.addWidget(self.btn_png)
        vl.addLayout(exp)
        ll.addWidget(v_box)

        self.probe = QLabel("Double-click the map to inspect a cell.")
        self.probe.setWordWrap(True)
        self.probe.setStyleSheet(f"color:{theme.MUTED};")
        ll.addWidget(self.probe)
        ll.addStretch(1)
        root.addWidget(left)

        # ------------------------------------------------ centre: map
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)

        self.timebar = TimeBar()
        cl.addWidget(self.timebar)

        self.map = make_map_view()
        cl.addWidget(self.map, 3)

        info = QHBoxLayout()
        self.resolution = QLabel("no data")
        self.resolution.setStyleSheet(
            f"color:{theme.WARN}; background:#fff3e0; padding:2px 6px; border-radius:3px;")
        self.resolution.setToolTip(
            "CaMa-Flood routes on the model grid. Downscaling to 1 arc-min (~1.85 km) for "
            "credible inundation mapping arrives in Phase 3.")
        self.coords = QLabel("")
        self.coords.setStyleSheet(f"color:{theme.MUTED};")
        info.addWidget(self.resolution)
        info.addStretch(1)
        info.addWidget(self.coords)
        cl.addLayout(info)

        self.chart = HydrographChart()
        cl.addWidget(self.chart, 2)

        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet(f"color:{theme.WARN}; font-size:10px;")
        cl.addWidget(self.notes)
        root.addWidget(centre, 1)

        # ------------------------------------------------ right: layers
        self.layers = LayerPanel()
        # Wide enough that a row's colour-preset combo (preset names run to "Floodplain depth
        # (blue)") and its legend (a fixed 170px-wide swatch, see LayerRow.set_legend) aren't
        # clipped -- the old 260px cap truncated both to a few characters.
        self.layers.setMinimumWidth(300)
        self.layers.setMaximumWidth(360)
        root.addWidget(self.layers)

        # ------------------------------------------------ wiring
        self.timebar.recordChanged.connect(self.map.set_record)
        self.layers.layersChanged.connect(self._layers_changed)
        self.map.cursorMoved.connect(self._cursor)
        self.map.cellClicked.connect(self._clicked)
        self.var_list.currentIndexChanged.connect(
            lambda _i: self._variable_changed(self.var_list.currentData()))
        self.btn_add_layer.clicked.connect(self._add_layer)
        self.btn_plot.clicked.connect(self._plot)
        self.btn_csv.clicked.connect(self._export_csv)
        self.btn_png.clicked.connect(self._export_png)
        self.btn_add_gauge.clicked.connect(self._arm_gauge_pick)
        self.btn_import_gauges.clicked.connect(self._import_gauges)
        self.btn_del_gauge.clicked.connect(self._remove_gauge)

        self._picking_gauge = False
        self._set_enabled(False)

    # ---------------------------------------------------------------- loading

    def load(self, case: Case | None, map_dir: Path | None) -> None:
        self.case = case
        self.layers.set_project_dir(case.project.root if case and case.project else None)
        self.layers.set_layers([])
        self.map.set_layers([])
        self.chart.clear()
        self.var_list.clear()
        self.notes.setText("")

        if case is None or map_dir is None or not Path(map_dir, "params.txt").exists():
            self.resolution.setText("no data")
            self._set_enabled(False)
            return

        try:
            self.river_map = RiverMap(map_dir)
        except Exception as exc:
            self.statusMessage.emit(f"could not open the map: {exc}")
            self._set_enabled(False)
            return

        grid = self.river_map.grid
        tag = ""
        try:
            tag = str(case.namelist().get("NOUTPUT", "COUTTAG", "") or "")
        except Exception:
            pass
        self.outputs = OutputSet(case.dir, grid, tag=tag or None)
        variables = self.outputs.variables

        if not variables:
            self.resolution.setText("no results yet -- run the case first")
            self._set_enabled(False)
            return

        for v in variables:
            self.var_list.addItem(display_name(v), v)
        nrec = self.outputs[variables[0]].nrec
        self.timebar.configure(nrec)

        km = grid.gsize * 111.32
        self.resolution.setText(
            f"model grid {grid.gsize}° (~{km:.0f} km cells) — not downscaled")

        # A static base layer so the domain is visible before any result is added.
        base = MapLayer(
            name="uparea (base)", grid=grid,
            provider=lambda _r: np.asarray(self.river_map.layer("uparea")),
            spec=ColorSpec(cmap="Blues", vmin=1e7, vmax=float(np.nanmax(
                np.where(self.river_map.land_mask, self.river_map.layer("uparea"), np.nan))),
                scale="log", opacity=0.9),
            mask=self.river_map.land_mask, unit="m2", static=True, variable="uparea")
        self.layers.set_layers([base])
        self._layers_changed()
        self.map.zoom_to_layers()

        self._set_enabled(True)
        self._variable_changed(self.var_list.currentData())
        self._add_layer()

    def _set_enabled(self, on: bool) -> None:
        for w in (self.var_list, self.btn_add_layer, self.btn_plot, self.btn_csv,
                  self.btn_png, self.btn_add_gauge, self.btn_import_gauges,
                  self.btn_del_gauge):
            w.setEnabled(on)

    # ---------------------------------------------------------------- layers

    def _variable_changed(self, name: str) -> None:
        caveat = CAVEATS.get(name, "")
        self.var_caveat.setText(caveat)
        self.var_caveat.setVisible(bool(caveat))

    def _add_layer(self) -> None:
        if self.outputs is None or self.river_map is None:
            return
        name = self.var_list.currentData()
        if not name:
            return
        try:
            f = self.outputs[name]
        except KeyError:
            return

        lo, hi = f.value_range(mask=self.river_map.land_mask)
        spec = ColorSpec.for_variable(name, lo, hi)
        layer = MapLayer(
            name=f"{display_name(name)} [{f.unit}]", grid=self.river_map.grid,
            provider=lambda r, f=f: f.record(min(r, f.nrec - 1)),
            spec=spec, mask=self.river_map.land_mask, unit=f.unit, variable=name)
        self.layers.add_layer(layer)
        self.map.set_record(self.timebar.record)

    def _layers_changed(self) -> None:
        self.map.set_layers(self.layers.layers)

    # ---------------------------------------------------------------- map interaction

    def _cursor(self, lon: float, lat: float) -> None:
        self.coords.setText(f"Lat {lat:.5f}   Lon {lon:.5f}")

    def _clicked(self, lon: float, lat: float, ix: int, iy: int) -> None:
        if self._picking_gauge:
            self._picking_gauge = False
            self.btn_add_gauge.setText("+ Add via map")
            name = f"Gauge {len(self.gauges) + 1}"
            g = snap_gauge(Gauge(name, lon, lat), self.river_map)
            self.gauges.append(g)
            self._refresh_gauges()
            return

        rows = self.map.probe(lon, lat)
        parts = [f"cell ({ix}, {iy})  lon {lon:.4f}  lat {lat:.4f}"]
        if self.river_map is not None and self.river_map.land_mask[iy, ix]:
            up = float(self.river_map.layer("uparea")[iy, ix]) / 1e6
            parts.append(f"drainage area {up:,.0f} km2")
        parts += [f"{n}: {v}" for n, v in rows]
        self.probe.setText("\n".join(parts))

    def _arm_gauge_pick(self) -> None:
        self._picking_gauge = True
        self.btn_add_gauge.setText("double-click the map...")

    # ---------------------------------------------------------------- gauges

    def _refresh_gauges(self) -> None:
        self.gauge_table.setRowCount(len(self.gauges))
        for r, g in enumerate(self.gauges):
            vals = [g.name, f"{g.lon:.4f}", f"{g.lat:.4f}",
                    f"{g.matched_area_km2:,.0f}" if g.matched_area_km2 else "-"]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 3 and g.area_error is not None:
                    item.setToolTip(g.match_quality)
                self.gauge_table.setItem(r, c, item)

    def _import_gauges(self) -> None:
        from ...core.analysis import load_gauges

        path, _ = QFileDialog.getOpenFileName(self, "Import gauges", "", "CSV (*.csv)")
        if not path:
            return
        try:
            loaded = load_gauges(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import gauges", f"Could not read the file:\n\n{exc}")
            return
        if self.river_map is not None:
            loaded = [snap_gauge(g, self.river_map) for g in loaded]
        self.gauges.extend(loaded)
        self._refresh_gauges()
        self.statusMessage.emit(f"imported {len(loaded)} gauge(s)")

    def _remove_gauge(self) -> None:
        rows = self.gauge_table.selectionModel().selectedRows()
        for r in sorted((i.row() for i in rows), reverse=True):
            if 0 <= r < len(self.gauges):
                self.gauges.pop(r)
        self._refresh_gauges()

    # ---------------------------------------------------------------- plotting

    def _plot(self) -> None:
        if self.outputs is None or self.river_map is None:
            return
        if not self.gauges:
            QMessageBox.information(self, "Plot",
                                    "Add at least one gauge first, via the map or a CSV.")
            return
        name = self.var_list.currentData()
        f = self.outputs[name]

        series = []
        for g in self.gauges:
            if not g.snapped:
                g = snap_gauge(g, self.river_map)
            if not g.snapped:
                continue
            h = extract_hydrograph(f, g)
            series.append((g.name, h.values))

        self.chart.plot(series, unit=f.unit)

        if name == "levdph" and series:
            # Protected-side depth is more usefully read as "did the levee get overtopped, and
            # when" than as a generic hydrograph -- see core.analysis.levee_protection_stats.
            stats = [f"{gname}: {levee_protection_stats(vals).summary()}"
                    for gname, vals in series]
            self.notes.setText("  ".join(stats))
        else:
            notes = interpretation_notes(
                spinup_records=0, forcing_resolution_deg=None,
                gauge_area_km2=self.gauges[0].matched_area_km2,
                grid=self.river_map.grid)
            self.notes.setText("  ".join(f"NOTE: {n}" for n in notes[:2]))
        self.statusMessage.emit(f"plotted {len(series)} series of {name}")

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "hydrograph.csv", "CSV (*.csv)")
        if not path:
            return
        n = self.chart.export_csv(path)
        self.statusMessage.emit(f"exported {n} records to {Path(path).name}"
                                if n else "nothing plotted yet")

    def _export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export PNG", "hydrograph.png", "PNG (*.png)")
        if not path:
            return
        self.chart.export_png(path)
        self.statusMessage.emit(f"saved {Path(path).name}")
