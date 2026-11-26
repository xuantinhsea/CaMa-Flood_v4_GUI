"""Dialog for editing named colour-scale presets (services.render.COLOR_PRESETS).

A preset is a handful of hex colours at fixed break-values -- see ``ColorPreset`` for why that
differs from the continuous matplotlib colormaps ``ColorSpec.for_variable`` already uses. This
dialog only ever touches the discrete-preset path; a layer using a continuous colormap has
nothing here to edit (the Layer Panel's colormap combo already covers that case).
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...services.render import COLOR_PRESETS, ColorSpec, load_presets, save_presets, set_color_preset
from ..interactive import InteractiveMixin

#: Where "Save Preset" writes when no project directory is given -- a plain per-user default,
#: mirroring how a project-less case still gets somewhere sensible to save to.
GLOBAL_PRESETS_PATH = Path.home() / ".camafloodcast" / "color_presets.json"


class ColorBandSwatch(QWidget):
    """A clickable colour swatch; opens a picker and reports the chosen hex colour."""

    def __init__(self, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = color
        self._on_change = None
        self.setFixedSize(28, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to change colour")

    @property
    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def on_change(self, callback) -> None:
        self._on_change = callback

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#666"), 1))
        p.setBrush(QBrush(QColor(self._color)))
        p.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 3, 3)
        p.end()

    def mousePressEvent(self, event) -> None:
        dlg = QColorDialog(QColor(self._color), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._color = dlg.currentColor().name()
            self.update()
            if self._on_change:
                self._on_change(self._color)


class ColorScalePreview(QWidget):
    """Horizontal gradient preview strip showing the current bands and break-values."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._colors: list[str] = []
        self._bounds: list[float] = []
        self.setFixedHeight(28)
        self.setMinimumWidth(240)

    def set_scale(self, colors: list[str], bounds: list[float]) -> None:
        self._colors = list(colors)
        self._bounds = list(bounds)
        self.update()

    def paintEvent(self, event) -> None:
        if not self._colors:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        n = len(self._colors)
        band_w = w / max(n, 1)
        for i, c in enumerate(self._colors):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(c)))
            p.drawRect(int(i * band_w), 0, int(band_w) + 1, h)
        p.setPen(QPen(QColor("#111")))
        for i, b in enumerate(self._bounds):
            x = int(i * band_w)
            p.drawText(x + 2, h - 4, f"{b:g}")
        p.end()


class ColorScaleEditor(InteractiveMixin, QDialog):
    """Edit a named preset: pick colours per band, edit break-values, save/load.

    Usage::

        dlg = ColorScaleEditor(parent, project_dir=project.root)
        dlg.set_current("Floodplain depth (blue)")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            result = dlg.get_result()   # {"name", "colors", "bounds"}

    Set ``.interactive = False`` before calling ``_save_preset``/``_load_from_file`` in a
    headless context -- like every other dialog in this app, their confirmation/error boxes
    would otherwise block an offscreen event loop with nothing to dismiss them (see
    ``ui.interactive.InteractiveMixin``).
    """

    #: Routed through by InteractiveMixin when .interactive is False.
    statusMessage = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None, project_dir: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Colour Scale")
        self.setMinimumWidth(440)
        self._project_dir = Path(project_dir) if project_dir else None
        self._colors: list[str] = []
        self._bounds: list[float] = []
        self._swatches: list[ColorBandSwatch] = []
        self._spins: list[QDoubleSpinBox] = []
        self._build_ui()

    # ---------------------------------------------------------------- build

    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)

        preset_row = QHBoxLayout()
        from PyQt6.QtWidgets import QComboBox

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(sorted(COLOR_PRESETS.keys()))
        self.preset_combo.currentTextChanged.connect(self._load_preset)
        preset_row.addWidget(self.preset_combo, 1)

        save_btn = QPushButton("Save Preset")
        save_btn.clicked.connect(self._save_preset)
        preset_row.addWidget(save_btn)

        load_btn = QPushButton("Load File...")
        load_btn.clicked.connect(self._load_from_file)
        preset_row.addWidget(load_btn)
        lay.addLayout(preset_row)

        self.preview = ColorScalePreview()
        lay.addWidget(self.preview)

        bands_box = QGroupBox("Bands (click a swatch to change its colour)")
        self._bands_layout = QVBoxLayout(bands_box)
        scroll = QScrollArea()
        scroll.setWidget(bands_box)
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(220)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(scroll)

        band_btns = QHBoxLayout()
        add_btn = QPushButton("+ Add band")
        add_btn.clicked.connect(self._add_band)
        band_btns.addWidget(add_btn)
        rm_btn = QPushButton("- Remove last")
        rm_btn.clicked.connect(self._remove_band)
        band_btns.addWidget(rm_btn)
        band_btns.addStretch(1)
        lay.addLayout(band_btns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        if self.preset_combo.count():
            self._load_preset(self.preset_combo.currentText())

    # ---------------------------------------------------------------- state

    def set_current(self, preset_name: str) -> None:
        """Open the editor already showing a specific preset, if it exists."""
        i = self.preset_combo.findText(preset_name)
        if i >= 0:
            self.preset_combo.setCurrentIndex(i)

    def _load_preset(self, name: str) -> None:
        preset = COLOR_PRESETS.get(name)
        if preset is not None:
            self._set_bands(list(preset.colors), list(preset.bounds))

    def _set_bands(self, colors: list[str], bounds: list[float]) -> None:
        self._colors = list(colors)
        self._bounds = list(bounds)

        while self._bands_layout.count():
            item = self._bands_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._swatches.clear()
        self._spins.clear()

        for i in range(len(self._colors)):
            row = QHBoxLayout()
            swatch = ColorBandSwatch(self._colors[i])
            swatch.on_change(lambda c, idx=i: self._on_color_changed(idx, c))
            row.addWidget(swatch)
            self._swatches.append(swatch)

            spin = QDoubleSpinBox()
            spin.setRange(-1e9, 1e9)
            spin.setDecimals(4)
            spin.setValue(self._bounds[i])
            spin.valueChanged.connect(lambda v, idx=i: self._on_bound_changed(idx, v))
            row.addWidget(spin)
            self._spins.append(spin)

            row.addWidget(QLabel(f"Band {i + 1}"))
            row.addStretch(1)

            container = QWidget()
            container.setLayout(row)
            self._bands_layout.addWidget(container)

        self._update_preview()

    def _on_color_changed(self, idx: int, color: str) -> None:
        if 0 <= idx < len(self._colors):
            self._colors[idx] = color
            self._update_preview()

    def _on_bound_changed(self, idx: int, value: float) -> None:
        if 0 <= idx < len(self._bounds):
            self._bounds[idx] = value
            self._update_preview()

    def _add_band(self) -> None:
        last = self._bounds[-1] if self._bounds else 0.0
        self._colors.append("#888888")
        self._bounds.append(last * 2 if last > 0 else 1.0)
        self._set_bands(self._colors, self._bounds)

    def _remove_band(self) -> None:
        if len(self._colors) > 2:
            self._colors.pop()
            self._bounds.pop()
            self._set_bands(self._colors, self._bounds)

    def _update_preview(self) -> None:
        self.preview.set_scale(self._colors, self._bounds)

    # ---------------------------------------------------------------- result

    def get_result(self) -> dict:
        return {
            "name": self.preset_combo.currentText(),
            "colors": list(self._colors),
            "bounds": list(self._bounds),
        }

    def get_spec(self, *, opacity: float = 0.85) -> ColorSpec:
        """The edited bands as a ready-to-use :class:`ColorSpec`, without touching the
        global ``COLOR_PRESETS`` registry (that only happens via "Save Preset")."""
        r = self.get_result()
        existing = COLOR_PRESETS.get(r["name"])
        return ColorSpec(
            colors=tuple(r["colors"]), bounds=tuple(r["bounds"]),
            vmin=r["bounds"][0], vmax=r["bounds"][-1], opacity=opacity,
            transparent_below=existing.transparent_below if existing else None,
            clamp_low=existing.clamp_low if existing else False,
            preset=r["name"],
        )

    # ---------------------------------------------------------------- persistence

    def _save_preset(self) -> None:
        result = self.get_result()
        if len(result["colors"]) < 2:
            self._warn("Save Preset", "A preset needs at least 2 bands.")
            return
        set_color_preset(result["name"], result["colors"], result["bounds"])
        path = (self._project_dir / "cache" / "color_presets.json"
               if self._project_dir else GLOBAL_PRESETS_PATH)
        try:
            save_presets(path)
        except OSError as exc:
            self._critical("Save Preset", f"Could not save:\n\n{exc}")
            return
        if self.preset_combo.findText(result["name"]) < 0:
            self.preset_combo.addItem(result["name"])
        self._inform("Saved", f"Preset '{result['name']}' saved to:\n{path}")

    def _load_from_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Colour Presets", "", "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            n = load_presets(path)
        except (OSError, ValueError) as exc:
            self._critical("Load Presets", f"Could not read the file:\n\n{exc}")
            return
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(sorted(COLOR_PRESETS.keys()))
        self.preset_combo.blockSignals(False)
        if n:
            self._load_preset(self.preset_combo.currentText())
        self._inform("Loaded", f"Loaded {n} preset(s) from:\n{path}")


def load_saved_presets(project_dir: Path | None = None) -> None:
    """Load global and (if given) per-project presets into ``COLOR_PRESETS`` at startup.

    Call once when a project opens -- mirrors how the project itself is opened before any view
    reads its configuration, so a saved preset is available the first time a layer needs one.
    """
    if GLOBAL_PRESETS_PATH.exists():
        load_presets(GLOBAL_PRESETS_PATH)
    if project_dir:
        proj_path = Path(project_dir) / "cache" / "color_presets.json"
        if proj_path.exists():
            load_presets(proj_path)
