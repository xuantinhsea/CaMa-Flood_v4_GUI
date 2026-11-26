"""Domain Builder tab: cut a regional map, and prove it is usable.

The module with no RRI_Floodcast analogue, and the highest-risk step in the whole workflow.
CaMa-Flood has no lateral boundary inflow, so a cut that severs a basin's headwaters biases
discharge low with no error at all -- and ``uparea.bin`` cannot reveal it, because it is
windowed verbatim from the global map and still reports the true *global* area.

So the closure check is not a report the user may consult; it is **Gate 1**, shown on the map
with every outlet coloured by its area deficit.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.domain import DomainReport, Outlet, analyse_domain, find_outlet_near, validate_domain
from ...core.domain_builder import (
    DEFAULT_ROUGHNESS,
    IGBP_CLASSES,
    DomainBuilder,
    DomainBuildError,
    DomainRequest,
    ToolSet,
    build_floodplain_roughness,
    default_igbp_lookup,
    roughness_summary,
)
from ...core.errors import DiagnosticList
from ...core.maps import RiverMap, read_layer
from ...core.project import Case
from ...services.render import ColorSpec
from .. import theme
from ..interactive import InteractiveMixin
from ..map import make_map_view
from ..map.renderer import MapLayer

CLOSED = theme.GOOD
TRUNCATED = theme.BAD
PARTIAL = theme.WARN


def outlet_colour(o: Outlet) -> str:
    if o.closed:
        return CLOSED
    return TRUNCATED if o.deficit > 0.25 else PARTIAL


class BuildThread(QThread):
    """The cut chain on a worker thread -- ``combine_hires`` alone can take a minute."""

    step = pyqtSignal(str, int, int)
    done = pyqtSignal(object)         # BuildResult
    failed = pyqtSignal(str)

    def __init__(self, builder: DomainBuilder, request: DomainRequest, parent=None):
        super().__init__(parent)
        self.builder = builder
        self.request = request

    def run(self) -> None:
        try:
            result = self.builder.build(
                self.request,
                progress=lambda n, i, t: self.step.emit(n, i, t))
        except DomainBuildError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                                   # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.done.emit(result)


class DomainView(InteractiveMixin, QWidget):
    """The Domain Builder tab."""

    statusMessage = pyqtSignal(str)
    domainBuilt = pyqtSignal(object)       # Path to the new map directory
    validationChanged = pyqtSignal(object)  # DiagnosticList
    caseUpdated = pyqtSignal(object)       # Case, after CFLDMAN was written

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.maps_root: Path | None = None
        self.tools: ToolSet | None = None
        self.source: RiverMap | None = None
        self.report: DomainReport | None = None
        self.case: Case | None = None
        #: The built (or opened) regional map that Gate 1 and the roughness editor operate on
        #: -- distinct from ``source``, which is what a NEW cut is taken from and is usually
        #: the (often global) parent map.
        self.active_map: RiverMap | None = None
        self._fldman: np.ndarray | None = None
        self._reprojection_note: str = ""
        self._thread: BuildThread | None = None

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ------------------------------------------------ left: definition
        left = QWidget()
        left.setMaximumWidth(330)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)

        src = QGroupBox("Source map")
        sf = QFormLayout(src)
        self.cmb_source = QComboBox()
        self.lbl_source = QLabel("-")
        self.lbl_source.setStyleSheet(f"color:{theme.MUTED};")
        self.lbl_source.setWordWrap(True)
        sf.addRow("Global map", self.cmb_source)
        sf.addRow(self.lbl_source)
        ll.addWidget(src)

        dom = QGroupBox("Domain")
        df = QFormLayout(dom)
        self.txt_name = QLineEdit("my_region_15min")
        self.spn = {}
        for key, val in (("west", 93.0), ("east", 110.0), ("south", 8.0), ("north", 34.0)):
            s = QDoubleSpinBox()
            s.setRange(-180.0, 180.0)
            s.setDecimals(2)
            s.setSingleStep(0.25)
            s.setValue(val)
            s.valueChanged.connect(self._bounds_changed)
            self.spn[key] = s
        df.addRow("Name", self.txt_name)
        for k in ("west", "east", "south", "north"):
            df.addRow(k.capitalize(), self.spn[k])
        self.lbl_size = QLabel("-")
        self.lbl_size.setStyleSheet(f"color:{theme.MUTED};")
        df.addRow("Grid", self.lbl_size)
        ll.addWidget(dom)

        self.btn_build = QPushButton("Build domain")
        self.btn_build.setStyleSheet("font-weight:600;")
        ll.addWidget(self.btn_build)

        self.progress = QProgressBar()
        self.progress.setRange(0, 7)
        self.progress.setFormat("%v / 7")
        ll.addWidget(self.progress)
        self.lbl_step = QLabel("")
        self.lbl_step.setStyleSheet(f"color:{theme.MUTED};")
        self.lbl_step.setWordWrap(True)
        ll.addWidget(self.lbl_step)

        note = QLabel(
            "Channel parameters are windowed from the global map rather than re-derived. "
            "Re-deriving uses discharge accumulated inside the domain, so a truncated basin "
            "silently gets channels that are too narrow and too shallow.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        ll.addWidget(note)
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

        # ------------------------------------------------ right: closure / roughness
        self.right = QTabWidget()
        self.right.setMaximumWidth(400)
        self.right.addTab(self._build_closure_tab(), "Closure (Gate 1)")
        self.right.addTab(self._build_roughness_tab(), "Roughness (CFLDMAN)")
        root.addWidget(self.right)

        self.btn_build.clicked.connect(self._build)
        self.map.cursorMoved.connect(
            lambda lo, la: self.coords.setText(f"Lat {la:.4f}   Lon {lo:.4f}"))
        self.tbl.itemSelectionChanged.connect(self._target_changed)
        self.cmb_source.currentIndexChanged.connect(self._source_changed)
        self._set_busy(False)

    def _build_closure_tab(self) -> QWidget:
        w = QWidget()
        rl = QVBoxLayout(w)
        rl.setContentsMargins(4, 4, 4, 4)

        gate = QGroupBox("Gate 1 — domain closure")
        gl = QVBoxLayout(gate)
        self.lbl_gate = QLabel("Build a domain, or open one, to check it.")
        self.lbl_gate.setWordWrap(True)
        gl.addWidget(self.lbl_gate)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["Outlet", "uparea km2", "in domain", "deficit"])
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        gl.addWidget(self.tbl, 1)

        self.lbl_target = QLabel("Select a row to make it the target basin.")
        self.lbl_target.setWordWrap(True)
        self.lbl_target.setStyleSheet(f"color:{theme.MUTED};")
        gl.addWidget(self.lbl_target)
        rl.addWidget(gate, 1)

        stats = QGroupBox("Domain")
        sl = QVBoxLayout(stats)
        self.lbl_stats = QLabel("-")
        self.lbl_stats.setWordWrap(True)
        self.lbl_stats.setStyleSheet("font-family:Consolas;")
        sl.addWidget(self.lbl_stats)
        rl.addWidget(stats)
        return w

    def _build_roughness_tab(self) -> QWidget:
        """Land cover -> ``CFLDMAN``: the single highest calibration-value-per-effort change
        available (9.0% RMS discharge sensitivity measured swapping the scalar ``PMANFLD`` for
        a two-zone map)."""
        w = QWidget()
        rl = QVBoxLayout(w)
        rl.setContentsMargins(4, 4, 4, 4)

        self.lbl_rough_target = QLabel("Build or open a domain first.")
        self.lbl_rough_target.setWordWrap(True)
        self.lbl_rough_target.setStyleSheet(f"color:{theme.MUTED};")
        rl.addWidget(self.lbl_rough_target)

        src = QGroupBox("Land-cover class raster")
        sf = QFormLayout(src)
        row = QHBoxLayout()
        self.txt_landclass = QLineEdit()
        self.txt_landclass.setPlaceholderText("int32 class codes, already on this domain's grid")
        btn_browse = QPushButton("...")
        btn_browse.setMaximumWidth(30)
        btn_browse.clicked.connect(self._browse_landclass)
        row.addWidget(self.txt_landclass, 1)
        row.addWidget(btn_browse)
        sf.addRow("File", row)
        note = QLabel(
            "Must already be gridded onto the domain (no reprojection here). Codes below "
            "follow the MODIS/IGBP 17-class legend most global land-cover products already use.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.MUTED}; font-size:10px;")
        sf.addRow(note)
        rl.addWidget(src)

        self.tbl_lc = QTableWidget(len(IGBP_CLASSES), 3)
        self.tbl_lc.setHorizontalHeaderLabels(["code", "IGBP class", "Manning n"])
        self.tbl_lc.verticalHeader().setVisible(False)
        self.tbl_lc.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_lc.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._lc_spin: dict[int, QDoubleSpinBox] = {}
        defaults = default_igbp_lookup()
        for r, (code, (label, category)) in enumerate(IGBP_CLASSES.items()):
            self.tbl_lc.setItem(r, 0, QTableWidgetItem(str(code)))
            self.tbl_lc.setItem(r, 1, QTableWidgetItem(f"{label} ({category})"))
            spin = QDoubleSpinBox()
            spin.setRange(0.005, 0.5)
            spin.setDecimals(3)
            spin.setSingleStep(0.005)
            spin.setValue(defaults[code])
            self.tbl_lc.setCellWidget(r, 2, spin)
            self._lc_spin[code] = spin
        rl.addWidget(self.tbl_lc, 1)

        deff = QFormLayout()
        self.spn_lc_default = QDoubleSpinBox()
        self.spn_lc_default.setRange(0.005, 0.5)
        self.spn_lc_default.setDecimals(3)
        self.spn_lc_default.setSingleStep(0.005)
        self.spn_lc_default.setValue(0.10)
        deff.addRow("Default n (unmapped codes)", self.spn_lc_default)
        rl.addLayout(deff)

        btns = QHBoxLayout()
        self.btn_lc_preview = QPushButton("Preview")
        self.btn_lc_preview.clicked.connect(self._preview_roughness)
        self.btn_lc_write = QPushButton("Write fldman.bin")
        self.btn_lc_write.clicked.connect(self._write_roughness)
        self.btn_lc_apply = QPushButton("Apply to case (CFLDMAN)")
        self.btn_lc_apply.clicked.connect(self._apply_roughness_to_case)
        btns.addWidget(self.btn_lc_preview)
        btns.addWidget(self.btn_lc_write)
        rl.addLayout(btns)
        rl.addWidget(self.btn_lc_apply)

        self.lbl_lc_summary = QLabel("")
        self.lbl_lc_summary.setWordWrap(True)
        self.lbl_lc_summary.setStyleSheet("font-family:Consolas; font-size:11px;")
        rl.addWidget(self.lbl_lc_summary)

        self._set_roughness_enabled(False)
        return w

    # ---------------------------------------------------------------- setup

    def show_roughness_tab(self) -> None:
        """Jump to the CFLDMAN builder -- used by Physics Options' 'Open Domain Builder' link."""
        self.right.setCurrentIndex(1)

    def configure(self, maps_root: Path, tools: ToolSet, *, active_map: Path | None = None) -> None:
        """``active_map`` is the project's currently-wired map (e.g. one just imported from
        outside ``maps_root``), offered as an extra cut source even though it is not one of
        ``maps_root``'s own subdirectories."""
        self.maps_root = Path(maps_root)
        self.tools = tools

        self.cmb_source.blockSignals(True)
        self.cmb_source.clear()
        seen: dict[Path, int] = {}
        if self.maps_root.exists():
            for d in sorted(self.maps_root.iterdir()):
                if d.is_dir() and (d / "params.txt").exists() and (d / "nextxy.bin").exists():
                    seen[d.resolve()] = self.cmb_source.count()
                    self.cmb_source.addItem(d.name, d)
        active_index = -1
        if active_map is not None:
            am = Path(active_map)
            if (am / "params.txt").exists() and (am / "nextxy.bin").exists():
                if am.resolve() in seen:
                    active_index = seen[am.resolve()]
                else:
                    active_index = self.cmb_source.count()
                    self.cmb_source.addItem(f"{am.name}  (active project map)", am)
        self.cmb_source.blockSignals(False)

        missing = tools.missing() if tools else ["(no tool directory configured)"]
        if missing:
            self.btn_build.setEnabled(False)
            self.lbl_step.setText(
                "Cannot build: missing tools — " + ", ".join(missing) +
                ". Build them from map/src/src_region and map/src/src_param.")
        else:
            self.btn_build.setEnabled(True)
            self.lbl_step.setText("")

        if active_index >= 0:
            # A domain already exists for this project -- open zoomed into it rather than
            # the whole-world cut source, which only matters before a domain has been built.
            self.cmb_source.setCurrentIndex(active_index)
        else:
            for i in range(self.cmb_source.count()):
                if "glb" in self.cmb_source.itemText(i):
                    self.cmb_source.setCurrentIndex(i)
                    break
        self._source_changed(self.cmb_source.currentIndex())

    def _source_changed(self, index: int) -> None:
        path = self.cmb_source.itemData(index) if index >= 0 else None
        if path is None:
            return
        try:
            self.source = RiverMap(path)
        except Exception as exc:                                   # noqa: BLE001
            self.lbl_source.setText(f"could not open: {exc}")
            self.source = None
            return

        g = self.source.grid
        self.lbl_source.setText(f"{g.describe()}\n{self.source.n_river_cells:,} river cells")

        up = np.asarray(self.source.layer("uparea"))
        vmax = float(np.nanmax(np.where(self.source.land_mask, up, np.nan)))
        layer = MapLayer(
            name="uparea", grid=g,
            provider=lambda _r, a=up: a,
            spec=ColorSpec(cmap="Blues", vmin=1e7, vmax=vmax, scale="log", opacity=0.95),
            mask=self.source.land_mask, unit="m2", static=True, variable="uparea")
        self.map.set_layers([layer])
        self.map.zoom_to_layers()
        self._bounds_changed()

    # ---------------------------------------------------------------- bounds

    def _bounds(self) -> tuple[float, float, float, float]:
        return (self.spn["west"].value(), self.spn["south"].value(),
                self.spn["east"].value(), self.spn["north"].value())

    def _bounds_changed(self) -> None:
        w, s, e, n = self._bounds()
        self.map.set_domain_rect((w, s, e, n))
        if self.source:
            g = self.source.grid
            nx = int(round((e - w) / g.gsize))
            ny = int(round((n - s) / g.gsize))
            self.lbl_size.setText(f"{nx} x {ny} cells @ {g.gsize}°"
                                  if nx > 0 and ny > 0 else "invalid extent")

    def set_bounds(self, west: float, south: float, east: float, north: float) -> None:
        for k, v in (("west", west), ("south", south), ("east", east), ("north", north)):
            self.spn[k].blockSignals(True)
            self.spn[k].setValue(v)
            self.spn[k].blockSignals(False)
        self._bounds_changed()

    # ---------------------------------------------------------------- build

    def _set_busy(self, busy: bool) -> None:
        self.btn_build.setEnabled(not busy and bool(self.tools and not self.tools.missing()))
        for w in (self.txt_name, self.cmb_source, *self.spn.values()):
            w.setEnabled(not busy)

    def _build(self) -> None:
        if self.source is None or self.tools is None or self.maps_root is None:
            return
        name = self.txt_name.text().strip()
        if not name:
            self._warn("Build domain", "Give the domain a name.")
            return
        target = self.maps_root / name
        if target.exists() and (target / "params.txt").exists():
            if not self._ask("Build domain", f"'{name}' already exists. Overwrite it?"):
                return

        w, s, e, n = self._bounds()
        req = DomainRequest(name=name, source_map=self.source.dir,
                            west=w, east=e, south=s, north=n)
        diags = req.snapped(self.source.grid.gsize).validate(self.source.grid)
        if not diags.ok:
            self.validationChanged.emit(diags)
            self._warn("Build domain", "\n".join(d.message for d in diags))
            return

        self._set_busy(True)
        self.progress.setValue(0)
        builder = DomainBuilder(self.tools, self.maps_root)
        self._thread = BuildThread(builder, req, self)
        self._thread.step.connect(self._on_step)
        self._thread.done.connect(self._on_done)
        self._thread.failed.connect(self._on_failed)
        self._thread.start()

    def _on_step(self, name: str, i: int, total: int) -> None:
        self.progress.setValue(min(i + 1, total))
        self.lbl_step.setText(f"step {min(i + 1, total)}/{total}: {name}")
        self.statusMessage.emit(f"building domain: {name}")

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.lbl_step.setText("build failed")
        self._critical("Domain build failed", message)
        self.statusMessage.emit("domain build failed")

    def _on_done(self, result) -> None:
        self._set_busy(False)
        self.lbl_step.setText(f"built {result.map_dir.name}")
        self.statusMessage.emit(f"domain built: {result.map_dir.name}")
        self.show_domain(result.map_dir)
        self.domainBuilt.emit(result.map_dir)

    # ---------------------------------------------------------------- Gate 1

    def load_case(self, case: Case | None) -> None:
        """The case that 'Apply to case (CFLDMAN)' writes into, if any."""
        self.case = case
        self.btn_lc_apply.setEnabled(
            case is not None and case.exists() and self.active_map is not None)

    def show_domain(self, map_dir: Path) -> None:
        """Analyse a built domain and present the closure check."""
        try:
            rmap = RiverMap(map_dir)
            self.report = analyse_domain(rmap)
        except Exception as exc:                                   # noqa: BLE001
            self.lbl_gate.setText(f"could not analyse the domain: {exc}")
            return

        self.active_map = rmap
        self._fldman = None
        self.lbl_lc_summary.setText("")
        self.lbl_rough_target.setText(f"Domain: {map_dir.name}  ({rmap.grid.describe()})")
        self._set_roughness_enabled(True)
        auto = rmap.dir / "landclass.bin"
        self.txt_landclass.setText(str(auto) if auto.exists() else "")

        rep = self.report
        self.lbl_stats.setText(rep.summary().split("\n\n")[0])

        top = rep.largest[:12]
        self.tbl.setRowCount(len(top))
        for r, o in enumerate(top):
            vals = [f"{o.lon:.2f}E {o.lat:.2f}N",
                    f"{o.uparea_km2:,.0f}", f"{o.in_domain_km2:,.0f}",
                    f"{o.deficit * 100:.1f}%"]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 3:
                    from PyQt6.QtGui import QBrush, QColor

                    item.setForeground(QBrush(QColor(outlet_colour(o))))
                    item.setToolTip(o.discharge_bias_note)
                self.tbl.setItem(r, c, item)

        self.map.set_markers([(o.lon, o.lat, outlet_colour(o), "") for o in top])
        self.map.set_domain_rect(None)

        # Default target: the largest closed basin, which is normally what the study is about.
        target = next((o for o in top if o.closed), None)
        self._present_gate(target)

    def _target_changed(self) -> None:
        if self.report is None:
            return
        rows = self.tbl.selectionModel().selectedRows()
        if not rows:
            return
        top = self.report.largest[:12]
        r = rows[0].row()
        if 0 <= r < len(top):
            self._present_gate(top[r])

    def _present_gate(self, target: Outlet | None) -> None:
        if self.report is None:
            return
        diags = validate_domain(self.report, target_basin=target)
        self.validationChanged.emit(diags)

        if target is None:
            self.lbl_target.setText("No closed basin found in this domain.")
        else:
            self.lbl_target.setText(
                f"Target: {target.uparea_km2:,.0f} km2 at {target.lon:.2f}E {target.lat:.2f}N "
                f"— {target.discharge_bias_note}")

        if diags.ok:
            self.lbl_gate.setText("PASS — the target basin is closed. "
                                  "Other basins truncated by a rectangular cut are expected "
                                  "and harmless unless you need their discharge.")
            self.lbl_gate.setStyleSheet(f"color:{CLOSED};")
        else:
            self.lbl_gate.setText(
                "BLOCKED — the target basin is truncated. CaMa-Flood has no lateral "
                "boundary inflow, so the missing upstream runoff is simply absent and "
                "discharge will be biased low. Extend the domain.")
            self.lbl_gate.setStyleSheet(f"color:{TRUNCATED};")

    # ---------------------------------------------------------------- roughness (CFLDMAN)

    def _set_roughness_enabled(self, on: bool) -> None:
        for w in (self.txt_landclass, self.tbl_lc, self.spn_lc_default,
                 self.btn_lc_preview, self.btn_lc_write):
            w.setEnabled(on)
        self.btn_lc_apply.setEnabled(on and self.case is not None and self.case.exists())

    def _browse_landclass(self) -> None:
        start = str(self.active_map.dir) if self.active_map else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a land-cover class raster", start,
            "Land cover (*.bin *.tif *.tiff);;CaMa-Flood binary (*.bin);;"
            "GeoTIFF (*.tif *.tiff);;All files (*)")
        if path:
            self.txt_landclass.setText(path)

    def _lookup(self) -> dict[int, float]:
        return {code: spin.value() for code, spin in self._lc_spin.items()}

    def _compute_roughness(self) -> np.ndarray | None:
        if self.active_map is None:
            return None
        path = self.txt_landclass.text().strip()
        if not path:
            self._inform("Floodplain roughness", "Select a land-cover class raster first.")
            return None

        self._reprojection_note = ""
        try:
            if Path(path).suffix.lower() in (".tif", ".tiff"):
                # A real-world land-cover product almost never arrives pre-gridded onto this
                # exact domain in this exact projection -- reproject it (nearest-neighbour:
                # class codes, not a continuous field) rather than requiring the user to do
                # that externally first. See core.gis for why this needs the 'geo' extra.
                from ...core.gis import import_geotiff_to_grid

                classes, epsg = import_geotiff_to_grid(
                    path, self.active_map.grid, method="nearest")
                if epsg != 4326:
                    self._reprojection_note = f"reprojected from EPSG:{epsg} to WGS84 -- "
            else:
                classes = np.asarray(read_layer(Path(path), self.active_map.grid, "landclass"))
        except Exception as exc:                                   # noqa: BLE001
            self._warn("Floodplain roughness", str(exc))
            return None
        return build_floodplain_roughness(classes, self._lookup(), self.active_map.grid,
                                          default=self.spn_lc_default.value())

    def _preview_roughness(self) -> None:
        fldman = self._compute_roughness()
        if fldman is None:
            return
        self._fldman = fldman
        self.lbl_lc_summary.setText(
            self._reprojection_note + roughness_summary(fldman, self.active_map.land_mask))
        self.statusMessage.emit("floodplain roughness computed (not yet written)")

    def _write_roughness(self) -> None:
        fldman = self._fldman if self._fldman is not None else self._compute_roughness()
        if fldman is None or self.active_map is None:
            return
        out = self.active_map.dir / "fldman.bin"
        if out.exists() and not self._ask("Write fldman.bin",
                                          f"'{out}' already exists. Overwrite it?"):
            return
        fldman.astype("<f4").tofile(out)
        self._fldman = fldman
        self.lbl_lc_summary.setText(
            self._reprojection_note + roughness_summary(fldman, self.active_map.land_mask))
        self.btn_lc_apply.setEnabled(self.case is not None and self.case.exists())
        self.statusMessage.emit(f"wrote {out}")

    def _apply_roughness_to_case(self) -> None:
        if self.case is None or self.active_map is None:
            return
        out = self.active_map.dir / "fldman.bin"
        if not out.exists():
            self._inform("Apply to case", "Write fldman.bin first, then apply it to the case.")
            return
        try:
            nml = self.case.namelist()
            nml.set("NMAP", "CFLDMAN", str(out))
            nml.save(backup=True)
        except Exception as exc:                                   # noqa: BLE001
            self._critical("Apply to case", str(exc))
            return
        self.statusMessage.emit(f"CFLDMAN written to {self.case.name}")
        self.caseUpdated.emit(self.case)
