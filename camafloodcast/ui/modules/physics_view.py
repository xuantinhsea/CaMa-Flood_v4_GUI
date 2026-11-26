"""Physics Options tab: the scientific choices, explained.

Separated from Model Setup because these are physics decisions, not file paths, and deserve
explanation rather than a bare checkbox in a 90-key generic form: floodplain flow, bifurcation,
levees, dams, sea level, the adaptive time step, the storage limiter, floodplain roughness, and
water-balance logging.

This view does **not** hold its own ``Namelist``. It edits the same one Model Setup does, via
``SetupView.get_value``/``set_value``, so there is exactly one in-memory copy and one save/dirty
cycle -- two independent copies of a hand-edited namelist is exactly the divergence risk 07-R7
warns about. ``bind()`` wires the two views together; ``SetupView.namelistChanged`` is what keeps
this tab from showing stale values after a case (re)load or a raw-text edit made on the other tab.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import schema
from ...core.dams import read_dam_file
from ...core.errors import FileFormatError
from ...core.maps import read_bifprm, read_layer
from ...core.project import Case
from .. import theme
from ..interactive import InteractiveMixin
from ..map import make_map_view
from .setup_view import FieldEditor, SetupView

GUIDANCE_STYLE = f"color:{theme.MUTED}; font-size:10px;"
NOTE_STYLE = f"color:{theme.MUTED}; font-style:italic;"


class PhysicsView(InteractiveMixin, QWidget):
    """The Physics Options tab."""

    statusMessage = pyqtSignal(str)
    #: Ask the shell to switch to the Domain Builder's roughness (CFLDMAN) editor.
    openRoughnessBuilder = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setup: SetupView | None = None
        self.case: Case | None = None
        self._bound: dict[tuple[str, str], FieldEditor] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.lbl_nocase = QLabel("No case selected.")
        self.lbl_nocase.setStyleSheet(NOTE_STYLE)
        outer.addWidget(self.lbl_nocase)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.body = QWidget()
        col = QVBoxLayout(self.body)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(8)

        col.addWidget(self._build_routing_group())
        col.addWidget(self._build_bifurcation_group())
        col.addWidget(self._build_levee_group())
        col.addWidget(self._build_dam_group())
        col.addWidget(self._build_sealevel_group())
        col.addWidget(self._build_limiter_group())
        col.addWidget(self._build_roughness_group())
        col.addWidget(self._build_diagnostics_group())
        col.addStretch(1)

        scroll.setWidget(self.body)
        outer.addWidget(scroll, 1)
        self.body.setEnabled(False)

    # ---------------------------------------------------------------- field binding

    def _field(self, group: str, key: str, *, decimals: int | None = None) -> FieldEditor:
        """A ``FieldEditor`` for one schema field, wired to read/write through ``self.setup``
        instead of holding its own value."""
        f = schema.field_for(group, key)
        assert f is not None, f"{group}/{key} is not in the schema"
        ed = FieldEditor(f)
        if decimals is not None and hasattr(ed.widget, "setDecimals"):
            ed.widget.setDecimals(decimals)
        ed.changed.connect(lambda g=group.upper(), k=key.upper(), e=ed: self._on_edit(g, k, e))
        self._bound[(group.upper(), key.upper())] = ed
        return ed

    def _row(self, form: QFormLayout, label: str, group: str, key: str, **kw) -> FieldEditor:
        ed = self._field(group, key, **kw)
        lbl = QLabel(label)
        lbl.setToolTip(schema.field_for(group, key).doc)
        form.addRow(lbl, ed)
        return ed

    def _on_edit(self, group: str, key: str, ed: FieldEditor) -> None:
        if self.setup is not None:
            self.setup.set_value(group, key, ed.value())

    def bind(self, setup: SetupView) -> None:
        self.setup = setup
        setup.namelistChanged.connect(self._refresh)
        self._refresh()

    def load_case(self, case: Case | None) -> None:
        self.case = case
        self._refresh_bifurcation_stats()
        self._refresh_levee_stats()
        self._refresh_dam_table()

    def _refresh(self) -> None:
        ready = bool(self.setup is not None and self.setup.namelist is not None)
        self.body.setEnabled(ready)
        self.lbl_nocase.setVisible(not ready)
        if ready:
            for (g, k), ed in self._bound.items():
                ed.set_value(self.setup.get_value(g, k))
        self._sync_enabled()
        self._refresh_bifurcation_stats()
        self._refresh_levee_stats()
        self._refresh_dam_table()

    def _sync_enabled(self) -> None:
        """Grey out a section's detail fields when its switch is off -- the point of a
        dedicated tab is to make the dependency visible, not just documented in a tooltip."""
        def on(group: str, key: str) -> bool:
            return bool(self.setup and self.setup.namelist is not None
                       and self.setup.get_value(group, key))

        pth = on("NRUNVER", "LPTHOUT")
        self._bound[("NMAP", "CPTHOUT")].setEnabled(pth)
        self.btn_bif_refresh.setEnabled(pth)

        lev = on("NRUNVER", "LLEVEE")
        self._bound[("NLEVEE", "CLEVHGT")].setEnabled(lev)
        self._bound[("NLEVEE", "CLEVFRC")].setEnabled(lev)
        self.btn_lev_refresh.setEnabled(lev)

        sea = on("NRUNVER", "LMEANSL")
        self._bound[("NMAP", "CMEANSL")].setEnabled(sea)

        bnd = on("NRUNVER", "LSEALEV")
        for key in ("LSEALEVCDF", "CSEALEVDIR", "CSEALEVPRE", "CSEALEVSUF",
                   "CSEALEVCDF", "CVNSEALEV", "IFRQ_SL"):
            self._bound[("NBOUND", key)].setEnabled(bnd)

        dam = on("NRUNVER", "LDAMOUT")
        for key in ("CDAMFILE", "LDAMTXT", "LDAMH22", "LDAMYBY", "LIVNORM"):
            self._bound[("NDAMOUT", key)].setEnabled(dam)
        self.btn_dam_refresh.setEnabled(dam)

    # ---------------------------------------------------------------- routing scheme

    def _build_routing_group(self) -> QGroupBox:
        box = QGroupBox("Routing scheme and time step")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Floodplain flow (LFLDOUT)", "NRUNVER", "LFLDOUT")
        self._row(form, "Kinematic wave instead of local-inertial (LKINE)", "NRUNVER", "LKINE")
        self._row(form, "Adaptive (CFL-limited) time step (LADPSTP)", "NRUNVER", "LADPSTP")
        self._row(form, "CFL safety coefficient (PCADP)", "NPARAM", "PCADP", decimals=2)
        lay.addLayout(form)
        note = QLabel(
            "dt ≤ PCADP × dx / sqrt(g·h). Lower PCADP is more stable and slower. "
            "LKINE cannot be combined with bifurcation flow (LPTHOUT), and the adaptive step is "
            "tuned for the local-inertial scheme, not the kinematic wave. The realised substep "
            "count (NT) is reported live in Run Manager's water-balance monitor while a case runs "
            "-- it depends on the actual flow state, not just these settings.")
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)
        return box

    # ---------------------------------------------------------------- bifurcation

    def _build_bifurcation_group(self) -> QGroupBox:
        box = QGroupBox("Bifurcation channels (LPTHOUT)")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Enable bifurcation flow (LPTHOUT)", "NRUNVER", "LPTHOUT")
        self._row(form, "Channel table (CPTHOUT)", "NMAP", "CPTHOUT")
        lay.addLayout(form)
        note = QLabel("Essential for deltas and anabranching rivers; needs bifprm.txt, produced "
                      "by set_bifparam in the Domain Builder.")
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)

        row = QHBoxLayout()
        self.lbl_bif_stats = QLabel("-")
        self.btn_bif_refresh = QPushButton("Refresh")
        self.btn_bif_refresh.clicked.connect(self._refresh_bifurcation_stats)
        row.addWidget(self.lbl_bif_stats, 1)
        row.addWidget(self.btn_bif_refresh)
        lay.addLayout(row)

        self.bif_map = make_map_view()
        self.bif_map.setMinimumHeight(150)
        lay.addWidget(self.bif_map)
        return box

    def _resolve(self, value: str) -> Path | None:
        if not value or str(value).strip().upper() in ("", "NONE"):
            return None
        p = Path(str(value))
        if not p.is_absolute() and self.case is not None:
            p = self.case.dir / p
        return p

    def _refresh_bifurcation_stats(self) -> None:
        if self.setup is None or self.setup.namelist is None:
            self.lbl_bif_stats.setText("-")
            self.bif_map.set_markers([])
            return
        path = self._resolve(self.setup.get_value("NMAP", "CPTHOUT", "NONE"))
        if path is None or not path.exists():
            self.lbl_bif_stats.setText("CPTHOUT is not set to an existing file.")
            self.bif_map.set_markers([])
            return
        try:
            table = read_bifprm(path)
        except (FileFormatError, OSError, ValueError) as exc:
            self.lbl_bif_stats.setText(f"could not read {path.name}: {exc}")
            self.bif_map.set_markers([])
            return
        self.lbl_bif_stats.setText(
            f"{table.npath:,} bifurcation channels, {table.nlev} width layer(s) -- "
            f"width {table.width.min():.0f}-{table.width.max():.0f} m, "
            f"length {table.length.min():,.0f}-{table.length.max():,.0f} m")
        markers = [(float(lo), float(la), theme.MARKER_BLUE, "")
                  for lo, la in zip(table.lon, table.lat)]
        self.bif_map.set_markers(markers)
        if len(table.lon):
            pad = 0.5
            self.bif_map.zoom_to(float(table.lon.min()) - pad, float(table.lat.min()) - pad,
                                 float(table.lon.max()) + pad, float(table.lat.max()) + pad)

    # ---------------------------------------------------------------- levee

    def _build_levee_group(self) -> QGroupBox:
        box = QGroupBox("Levee scheme (LLEVEE)")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Enable levee scheme (LLEVEE)", "NRUNVER", "LLEVEE")
        self._row(form, "Crown height above bank top (CLEVHGT)", "NLEVEE", "CLEVHGT")
        self._row(form, "Unprotected fraction (CLEVFRC)", "NLEVEE", "CLEVFRC")
        lay.addLayout(form)
        note = QLabel(
            "CLEVFRC positions the levee's base on the floodplain's own hypsometry layers, not "
            "as a plain distance -- and the model silently raises CLEVHGT to match that base if "
            "it is set any lower, so a real crown height needs both fields checked together "
            "(see each field's tooltip). A run with an active, overtopped levee also reports a "
            "looser water-balance closure than usual (order 1e-8 rather than the ~1e-14 a plain "
            "run holds) -- bounded and not a sign of a broken configuration, but Run Manager's "
            "water-balance monitor may flag it 'ABOVE LIMIT' regardless.")
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)

        row = QHBoxLayout()
        self.lbl_lev_stats = QLabel("-")
        self.btn_lev_refresh = QPushButton("Analyse maps")
        self.btn_lev_refresh.clicked.connect(self._refresh_levee_stats)
        row.addWidget(self.lbl_lev_stats, 1)
        row.addWidget(self.btn_lev_refresh)
        lay.addLayout(row)
        return box

    def _grid(self):
        """The routing grid, from the map wired into ``CNEXTXY`` -- levee maps must match it."""
        if self.setup is None or self.setup.namelist is None:
            return None
        nextxy = self._resolve(self.setup.get_value("NMAP", "CNEXTXY", "./nextxy.bin"))
        if nextxy is None or not nextxy.exists():
            return None
        from ...core.maps import read_params

        try:
            return read_params(nextxy.parent / "params.txt")
        except (FileFormatError, OSError):
            return None

    def _refresh_levee_stats(self) -> None:
        if self.setup is None or self.setup.namelist is None:
            self.lbl_lev_stats.setText("-")
            return
        grid = self._grid()
        if grid is None:
            self.lbl_lev_stats.setText("could not determine the routing grid (check CNEXTXY)")
            return

        parts: list[str] = []
        for label, group, key, layer in (
                ("crown height", "NLEVEE", "CLEVHGT", "levhgt"),
                ("unprotected fraction", "NLEVEE", "CLEVFRC", "levfrc")):
            path = self._resolve(self.setup.get_value(group, key, "NONE"))
            if path is None:
                parts.append(f"{label}: not set")
                continue
            if not path.exists():
                parts.append(f"{label}: {path.name} not found")
                continue
            try:
                arr = read_layer(path, grid, layer=layer)
            except (FileFormatError, OSError, ValueError) as exc:
                parts.append(f"{label}: {exc}")
                continue
            valid = arr[(arr > -9998) & (arr < 1e19)]
            if valid.size:
                parts.append(f"{label}: mean {valid.mean():.2f}, range "
                             f"{valid.min():.2f}-{valid.max():.2f}")
            else:
                parts.append(f"{label}: no valid values")
        self.lbl_lev_stats.setText("  |  ".join(parts))

    # ---------------------------------------------------------------- dams

    def _build_dam_group(self) -> QGroupBox:
        box = QGroupBox("Reservoirs / dams (LDAMOUT)")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Enable dam operation (LDAMOUT)", "NRUNVER", "LDAMOUT")
        self._row(form, "Dam parameter file (CDAMFILE)", "NDAMOUT", "CDAMFILE")
        self._row(form, "Write per-dam inflow/outflow text (LDAMTXT)", "NDAMOUT", "LDAMTXT")
        self._row(form, "Hanazaki (2022) scheme instead of Yamazaki & Funato (LDAMH22)",
                 "NDAMOUT", "LDAMH22")
        self._row(form, "Year-by-year dam activation (LDAMYBY)", "NDAMOUT", "LDAMYBY")
        self._row(form, "Initialise storage at normal volume (LiVnorm)", "NDAMOUT", "LiVnorm")
        lay.addLayout(form)

        row = QHBoxLayout()
        self.lbl_dam_stats = QLabel("-")
        self.btn_dam_refresh = QPushButton("Read dam file")
        self.btn_dam_refresh.clicked.connect(self._refresh_dam_table)
        row.addWidget(self.lbl_dam_stats, 1)
        row.addWidget(self.btn_dam_refresh)
        lay.addLayout(row)

        self.tbl_dams = QTableWidget(0, 6)
        self.tbl_dams.setHorizontalHeaderLabels(
            ["Dam", "Lat", "Lon", "TotVol Mm3", "Qn m3/s", "Qf m3/s"])
        self.tbl_dams.verticalHeader().setVisible(False)
        self.tbl_dams.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_dams.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tbl_dams.setMaximumHeight(160)
        lay.addWidget(self.tbl_dams)

        note = QLabel(
            "Reads an existing CDAMFILE for a read-only summary. DamIX/DamIY are 1-indexed in "
            "the file, and a dam landing outside the grid or off the river network is skipped "
            "by the model silently, not rejected -- core.dams.write_dam_file validates an "
            "allocation before writing one, but building or editing a dam list from this tab "
            "(an allocate-by-map-click workflow) is not yet built.")
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)
        return box

    def _refresh_dam_table(self) -> None:
        self.tbl_dams.setRowCount(0)
        if self.setup is None or self.setup.namelist is None:
            self.lbl_dam_stats.setText("-")
            return
        path = self._resolve(self.setup.get_value("NDAMOUT", "CDAMFILE", "NONE"))
        if path is None or not path.exists():
            self.lbl_dam_stats.setText("CDAMFILE is not set to an existing file.")
            return
        try:
            records = read_dam_file(path)
        except (ValueError, OSError) as exc:
            self.lbl_dam_stats.setText(f"could not read {path.name}: {exc}")
            return

        self.lbl_dam_stats.setText(f"{len(records)} dam(s) in {path.name}")
        self.tbl_dams.setRowCount(len(records))
        for r, d in enumerate(records):
            vals = [d.name, f"{d.lat:.3f}", f"{d.lon:.3f}", f"{d.totvol_mcm:,.1f}",
                   f"{d.qn:,.1f}", f"{d.qf:,.1f}"]
            for c, v in enumerate(vals):
                self.tbl_dams.setItem(r, c, QTableWidgetItem(v))

    # ---------------------------------------------------------------- sea level

    def _build_sealevel_group(self) -> QGroupBox:
        box = QGroupBox("Sea level (LMEANSL / LSEALEV)")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Mean sea level at river mouths (LMEANSL)", "NRUNVER", "LMEANSL")
        self._row(form, "Mean sea level map (CMEANSL)", "NMAP", "CMEANSL")
        self._row(form, "Time-varying sea level boundary (LSEALEV)", "NRUNVER", "LSEALEV")
        self._row(form, "Boundary is netCDF (LSEALEVCDF)", "NBOUND", "LSEALEVCDF")
        self._row(form, "Boundary directory (CSEALEVDIR)", "NBOUND", "CSEALEVDIR")
        self._row(form, "Boundary filename prefix (CSEALEVPRE)", "NBOUND", "CSEALEVPRE")
        self._row(form, "Boundary filename suffix (CSEALEVSUF)", "NBOUND", "CSEALEVSUF")
        self._row(form, "netCDF boundary file (CSEALEVCDF)", "NBOUND", "CSEALEVCDF")
        self._row(form, "netCDF variable name (CVNSEALEV)", "NBOUND", "CVNSEALEV")
        self._row(form, "Update interval, minutes (IFRQ_SL)", "NBOUND", "IFRQ_SL")
        lay.addLayout(form)
        note = QLabel(
            "LMEANSL applies a static map; LSEALEV additionally applies a time series. With "
            "LSEALEVCDF off, the plain-binary directory/prefix/suffix fields are read; with it "
            "on, the netCDF file and variable name are used instead. IFRQ_SL must be set away "
            "from 9999 (its 'not used' sentinel) for LSEALEV to take effect.")
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)
        return box

    # ---------------------------------------------------------------- storage limiter

    def _build_limiter_group(self) -> QGroupBox:
        box = QGroupBox("Storage-change limiter (PSTOLIM)")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Max fraction of cell storage per substep", "NPARAM", "PSTOLIM",
                  decimals=3)
        lay.addLayout(form)
        note = QLabel(schema.field_for("NPARAM", "PSTOLIM").guidance)
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)
        return box

    # ---------------------------------------------------------------- roughness

    def _build_roughness_group(self) -> QGroupBox:
        box = QGroupBox("Floodplain roughness (CFLDMAN)")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Floodplain Manning map (CFLDMAN)", "NMAP", "CFLDMAN")
        lay.addLayout(form)
        note = QLabel(
            '"NONE" uses the scalar PMANFLD (Model Setup / NPARAM) worldwide and is '
            "bit-identical to v4.30. A 2-D map lets floodplain roughness vary by land cover -- "
            "measured 9.0% RMS sensitivity in discharge swapping a uniform 0.10 for a two-zone "
            "map. Build one from a land-cover raster in the Domain Builder's Roughness tab.")
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)
        btn = QPushButton("Open Domain Builder → Roughness")
        btn.clicked.connect(self.openRoughnessBuilder.emit)
        lay.addWidget(btn)
        return box

    # ---------------------------------------------------------------- diagnostics

    def _build_diagnostics_group(self) -> QGroupBox:
        box = QGroupBox("Diagnostics")
        lay = QVBoxLayout(box)
        form = QFormLayout()
        self._row(form, "Write the per-substep water balance line (LWBLOG)", "NRUNVER", "LWBLOG")
        lay.addLayout(form)
        note = QLabel(schema.field_for("NRUNVER", "LWBLOG").guidance)
        note.setWordWrap(True)
        note.setStyleSheet(GUIDANCE_STYLE)
        lay.addWidget(note)
        return box
