"""Layer stack and time navigation -- the two controls that flank the map.

Laid out to match RRI_Floodcast: time controls above the canvas, layer stack to its right.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...services.render import COLOR_PRESETS, COLORMAPS, ColorSpec, legend_stops
from .. import theme
from ..map.renderer import MapLayer


class TimeBar(QWidget):
    """Play / step / scrub through the output records."""

    recordChanged = pyqtSignal(int)

    SPEEDS = ("0.5x", "1x", "2x", "4x", "8x")
    BASE_MS = 400

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.count = 0
        self.record = 0
        self.dates: list[dt.datetime] | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(6)

        self.btn_play = QToolButton()
        self.btn_play.setText("▶")
        self.btn_play.setCheckable(True)
        self.btn_play.setToolTip("Play / pause")
        self.btn_prev = QToolButton()
        self.btn_prev.setText("◀")
        self.btn_next = QToolButton()
        self.btn_next.setText("▶|")
        for b in (self.btn_play, self.btn_prev, self.btn_next):
            b.setAutoRaise(True)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)

        self.label = QLabel("--")
        self.label.setMinimumWidth(150)
        self.label.setFont(QFont("Consolas", 9))
        self.counter = QLabel("0/0")
        self.counter.setStyleSheet(f"color:{theme.MUTED};")

        self.speed = QComboBox()
        self.speed.addItems(self.SPEEDS)
        self.speed.setCurrentText("1x")
        self.speed.setMaximumWidth(64)

        lay.addWidget(self.btn_play)
        lay.addWidget(self.btn_prev)
        lay.addWidget(self.btn_next)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.label)
        lay.addWidget(self.counter)
        lay.addWidget(self.speed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self.btn_play.toggled.connect(self._toggle_play)
        self.btn_prev.clicked.connect(lambda: self.set_record(self.record - 1))
        self.btn_next.clicked.connect(lambda: self.set_record(self.record + 1))
        self.slider.valueChanged.connect(self.set_record)
        self.speed.currentTextChanged.connect(self._retime)

    def configure(self, count: int, dates: list[dt.datetime] | None = None) -> None:
        self.count = max(0, count)
        self.dates = dates
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(0, self.count - 1))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.record = 0
        self._update_labels()
        enabled = self.count > 1
        for w in (self.btn_play, self.btn_prev, self.btn_next, self.slider, self.speed):
            w.setEnabled(enabled)

    def set_record(self, record: int) -> None:
        if self.count == 0:
            return
        record = max(0, min(self.count - 1, int(record)))
        if record == self.record:
            return
        self.record = record
        self.slider.blockSignals(True)
        self.slider.setValue(record)
        self.slider.blockSignals(False)
        self._update_labels()
        self.recordChanged.emit(record)

    def _update_labels(self) -> None:
        self.counter.setText(f"{self.record + 1}/{max(self.count, 1)}")
        if self.dates and self.record < len(self.dates):
            self.label.setText(self.dates[self.record].strftime("%Y-%m-%d %H:%M"))
        else:
            self.label.setText(f"record {self.record + 1}")

    def _toggle_play(self, on: bool) -> None:
        self.btn_play.setText("⏸" if on else "▶")
        if on:
            self._retime()
            self._timer.start()
        else:
            self._timer.stop()

    def _retime(self) -> None:
        factor = float(self.speed.currentText().rstrip("x"))
        self._timer.setInterval(int(self.BASE_MS / factor))

    def _tick(self) -> None:
        nxt = self.record + 1
        if nxt >= self.count:
            nxt = 0
        self.set_record(nxt)


class LayerRow(QWidget):
    """One layer's controls, always visible (no separate "selected layer" step): visibility,
    a static/series badge, colour (a continuous colormap or a named discrete preset, from the
    same combo), an inline opacity slider, its own legend, and reorder/remove buttons."""

    visibilityChanged = pyqtSignal(str, bool)
    opacityChanged = pyqtSignal(str, float)
    styleChanged = pyqtSignal(str)
    moveRequested = pyqtSignal(str, int)          # name, direction (-1 up, +1 down)
    removeRequested = pyqtSignal(str)
    editRequested = pyqtSignal(str)

    def __init__(self, layer: MapLayer, parent: QWidget | None = None):
        super().__init__(parent)
        self._name = layer.name

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(4)
        self.chk_visible = QCheckBox()
        self.chk_visible.setChecked(layer.visible)
        self.chk_visible.toggled.connect(lambda on: self.visibilityChanged.emit(self._name, on))
        top.addWidget(self.chk_visible)

        name_lbl = QLabel(f"<b>{layer.name}</b>")
        name_lbl.setStyleSheet("font-size:11px;")
        top.addWidget(name_lbl, 1)

        badge = QLabel("static" if layer.static else "series")
        badge.setToolTip("static base layer" if layer.static
                         else "changes with the time bar's record")
        badge.setStyleSheet(
            "font-size:9px;color:#0d3a66;background:#dbeafe;padding:1px 5px;border-radius:3px;")
        top.addWidget(badge)

        self.btn_up = QToolButton()
        self.btn_up.setText("▲")
        self.btn_up.setAutoRaise(True)
        self.btn_up.setToolTip("Move up (toward the top of the stack)")
        self.btn_up.setFixedSize(20, 18)
        self.btn_up.clicked.connect(lambda: self.moveRequested.emit(self._name, -1))
        top.addWidget(self.btn_up)

        self.btn_down = QToolButton()
        self.btn_down.setText("▼")
        self.btn_down.setAutoRaise(True)
        self.btn_down.setToolTip("Move down")
        self.btn_down.setFixedSize(20, 18)
        self.btn_down.clicked.connect(lambda: self.moveRequested.emit(self._name, 1))
        top.addWidget(self.btn_down)

        self.btn_remove = QToolButton()
        self.btn_remove.setText("✕")
        self.btn_remove.setAutoRaise(True)
        self.btn_remove.setToolTip("Remove this layer")
        self.btn_remove.setFixedSize(20, 18)
        self.btn_remove.setStyleSheet(f"color:{theme.BAD};")
        self.btn_remove.clicked.connect(lambda: self.removeRequested.emit(self._name))
        top.addWidget(self.btn_remove)
        lay.addLayout(top)

        style_row = QHBoxLayout()
        style_row.setSpacing(4)
        self.style_combo = QComboBox()
        self._fill_style_combo(layer.spec)
        self.style_combo.currentTextChanged.connect(lambda _t: self.styleChanged.emit(self._name))
        style_row.addWidget(self.style_combo, 1)

        self.btn_edit = QToolButton()
        self.btn_edit.setText("\U0001f3a8")
        self.btn_edit.setAutoRaise(True)
        self.btn_edit.setToolTip("Edit colour scale...")
        self.btn_edit.clicked.connect(lambda: self.editRequested.emit(self._name))
        style_row.addWidget(self.btn_edit)
        lay.addLayout(style_row)

        opac_row = QHBoxLayout()
        opac_row.setSpacing(4)
        opac_lbl = QLabel("opacity")
        opac_lbl.setStyleSheet(f"font-size:10px;color:{theme.MUTED};")
        opac_row.addWidget(opac_lbl)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(10, 100)
        self.opacity.setValue(int(layer.spec.opacity * 100))
        opac_row.addWidget(self.opacity, 1)
        self.opacity_lbl = QLabel(f"{int(layer.spec.opacity * 100)}%")
        self.opacity_lbl.setMinimumWidth(32)
        self.opacity_lbl.setStyleSheet(f"color:{theme.MUTED};font-size:10px;")
        opac_row.addWidget(self.opacity_lbl)
        self.opacity.valueChanged.connect(self._on_opacity)
        lay.addLayout(opac_row)

        self.legend = QLabel()
        self.legend.setAlignment(Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.legend)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{theme.LINE};")
        lay.addWidget(sep)

        self.set_legend(layer)

    def _fill_style_combo(self, spec: ColorSpec) -> None:
        """Continuous colormaps first, then a separator, then named discrete presets --
        one dropdown covers both colouring paradigms ``services.render.ColorSpec`` supports."""
        self.style_combo.blockSignals(True)
        self.style_combo.clear()
        self.style_combo.addItems(COLORMAPS)
        self.style_combo.insertSeparator(len(COLORMAPS))
        self.style_combo.addItems(sorted(COLOR_PRESETS.keys()))
        current = spec.preset if spec.preset else spec.cmap
        idx = self.style_combo.findText(current)
        if idx >= 0:
            self.style_combo.setCurrentIndex(idx)
        self.style_combo.blockSignals(False)

    def current_style_choice(self) -> str:
        return self.style_combo.currentText()

    def refresh_style_combo(self, spec: ColorSpec) -> None:
        """Re-sync the combo after the spec changed elsewhere (e.g. the colour-scale editor)."""
        self._fill_style_combo(spec)

    def _on_opacity(self, v: int) -> None:
        self.opacity_lbl.setText(f"{v}%")
        self.opacityChanged.emit(self._name, v / 100.0)

    def set_legend(self, layer: MapLayer) -> None:
        from PyQt6.QtGui import QPainter

        from ...services.render import format_value

        stops = legend_stops(layer.spec, n=6)
        w, box = 170, 11
        h = 6 + len(stops) * (box + 1)
        pm = QPixmap(w, max(h, 1))
        pm.fill(QColor(theme.PANEL2))
        p = QPainter(pm)
        p.setFont(QFont("Segoe UI", 7))
        for i, (value, rgb) in enumerate(reversed(stops)):
            y = 4 + i * (box + 1)
            p.fillRect(4, y, box * 2, box, QColor(*rgb))
            p.setPen(QColor(theme.TEXT))
            p.drawText(4 + box * 2 + 5, y + box - 1, format_value(value, layer.unit))
        p.end()
        self.legend.setPixmap(pm)


class LayerPanel(QWidget):
    """Layer stack: one always-visible :class:`LayerRow` per layer, instead of a single style
    panel shared by whichever row happens to be selected."""

    layersChanged = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.layers: list[MapLayer] = []
        self._rows: dict[str, LayerRow] = {}
        self._project_dir: Path | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(6)

        title = QLabel("Layers")
        title.setStyleSheet("font-weight:600;")
        lay.addWidget(title)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_widget)
        lay.addWidget(self._scroll, 1)

    # ---------------------------------------------------------------- model

    def set_project_dir(self, path) -> None:
        """Where "Save Preset" in the colour-scale editor writes a per-project copy.

        ``None`` (the default) means only the global presets file is offered.
        """
        self._project_dir = Path(path) if path else None

    def set_layers(self, layers: list[MapLayer]) -> None:
        self.layers = layers
        self._rebuild()

    def add_layer(self, layer: MapLayer) -> None:
        self.layers.insert(0, layer)
        self._rebuild()
        self.layersChanged.emit()

    def _rebuild(self) -> None:
        for row in self._rows.values():
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for layer in self.layers:
            row = LayerRow(layer)
            row.visibilityChanged.connect(self._on_visibility)
            row.opacityChanged.connect(self._on_opacity)
            row.styleChanged.connect(self._on_style)
            row.moveRequested.connect(self._on_move)
            row.removeRequested.connect(self._on_remove)
            row.editRequested.connect(self._on_edit)
            self._list_layout.addWidget(row)
            self._rows[layer.name] = row

        self._list_layout.addStretch(1)

    def _layer(self, name: str) -> MapLayer | None:
        return next((l for l in self.layers if l.name == name), None)

    # ---------------------------------------------------------------- events

    def _on_visibility(self, name: str, visible: bool) -> None:
        layer = self._layer(name)
        if layer is not None:
            layer.visible = visible
            self.layersChanged.emit()

    def _on_opacity(self, name: str, opacity: float) -> None:
        layer = self._layer(name)
        if layer is None:
            return
        layer.spec.opacity = opacity
        self.layersChanged.emit()

    def _on_style(self, name: str) -> None:
        layer = self._layer(name)
        row = self._rows.get(name)
        if layer is None or row is None:
            return
        choice = row.current_style_choice()
        if choice in COLOR_PRESETS:
            layer.spec = ColorSpec.from_preset(choice, opacity=layer.spec.opacity)
        elif choice in COLORMAPS:
            layer.spec.colors = None
            layer.spec.bounds = None
            layer.spec.preset = None
            layer.spec.cmap = choice
        else:
            return   # the separator item, or a stale selection -- nothing to apply
        row.set_legend(layer)
        self.layersChanged.emit()

    def _on_move(self, name: str, direction: int) -> None:
        idx = next((i for i, l in enumerate(self.layers) if l.name == name), None)
        if idx is None:
            return
        new = idx + direction
        if not (0 <= new < len(self.layers)):
            return
        self.layers[idx], self.layers[new] = self.layers[new], self.layers[idx]
        self._rebuild()
        self.layersChanged.emit()

    def _on_remove(self, name: str) -> None:
        self.layers = [l for l in self.layers if l.name != name]
        self._rebuild()
        self.layersChanged.emit()

    def _on_edit(self, name: str) -> None:
        layer = self._layer(name)
        row = self._rows.get(name)
        if layer is None or row is None:
            return
        from .color_scale_editor import ColorScaleEditor

        dlg = ColorScaleEditor(self, project_dir=self._project_dir)
        dlg.set_current(layer.spec.preset or sorted(COLOR_PRESETS.keys())[0])
        if dlg.exec() == dlg.DialogCode.Accepted:
            layer.spec = dlg.get_spec(opacity=layer.spec.opacity)
            row.refresh_style_combo(layer.spec)
            row.set_legend(layer)
            self.layersChanged.emit()
