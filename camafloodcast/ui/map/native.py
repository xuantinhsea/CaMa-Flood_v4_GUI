"""Native Qt map canvas: georeferenced raster layers with pan, zoom and probe.

No external dependency, so it works wherever PyQt6 does. It is not a GIS -- there is no basemap
and no reprojection, because CaMa-Flood is WGS84 lat/lon throughout and the MVP did not need
one. This is now the *fallback* renderer, used when ``PyQt6-WebEngine`` isn't installed --
``ui.map.leaflet.LeafletMapView`` (decision D3) is the default, with a real basemap. See
``ui.map.make_map_view()``.

Scene coordinates are degrees with the y axis flipped so north is up, which makes
pixel <-> lon/lat conversion trivial and keeps the probe honest.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

from .. import theme
from ...services.render import ColorSpec, colorize, format_value
from .renderer import MapLayer

#: Keep the whole canvas inside a sane zoom band, so a stray wheel event cannot lose the map.
MIN_SCALE = 0.5
MAX_SCALE = 20000.0


def array_to_pixmap(rgba: np.ndarray) -> QPixmap:
    """(ny, nx, 4) uint8 -> QPixmap.

    ``QImage`` does not own the buffer, so the array must stay alive until the pixmap is made.
    ``np.ascontiguousarray`` plus an immediate ``fromImage`` copy is the safe sequence; getting
    this wrong produces intermittently garbled tiles that are very hard to diagnose.
    """
    buf = np.ascontiguousarray(rgba, dtype=np.uint8)
    h, w = buf.shape[:2]
    img = QImage(buf.data, w, h, 4 * w, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(img.copy())


def _spec_fingerprint(spec: ColorSpec) -> tuple:
    """Every ``ColorSpec`` field that changes the painted pixels, as a hashable tuple.

    Two renders with an identical fingerprint produce an identical image, which is exactly
    what a cache key needs -- opacity is included because it changes alpha, not just the
    lookup, and belongs in the key rather than being applied after a cache hit.
    """
    return (spec.cmap, spec.vmin, spec.vmax, spec.scale, spec.opacity,
           spec.transparent_below, spec.colors, spec.bounds, spec.clamp_low)


class _FrameCache:
    """Bounded cache of already-rendered frames, keyed by ``(layer name, record, style)``.

    Scrubbing the time bar back to a record shown a moment ago -- or the Downscaling tab
    replaying the same maximum-depth composite -- would otherwise repeat a full
    ``colorize()`` + ``QImage`` encode on every visit. Bounded by an approximate byte budget
    (RGBA8888, so ``w*h*4``) rather than an entry count, since one frame is a few KB on a
    small regional domain and can be much larger on a downscaled composite.

    Not safe to reuse across a different *set* of layers -- see ``NativeMapView.set_layers``,
    which clears this on every call for exactly that reason: a fresh layer list can reuse a
    name that means something entirely different (a new case, a different variable).
    """

    def __init__(self, limit_bytes: int = 64 * 1024 * 1024):
        self._data: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._bytes = 0
        self.limit_bytes = limit_bytes

    @staticmethod
    def _size_of(pm: QPixmap) -> int:
        return pm.width() * pm.height() * 4

    def get(self, key: tuple) -> QPixmap | None:
        pm = self._data.get(key)
        if pm is not None:
            self._data.move_to_end(key)
        return pm

    def put(self, key: tuple, pm: QPixmap) -> None:
        if key in self._data:
            self._bytes -= self._size_of(self._data[key])
        self._data[key] = pm
        self._data.move_to_end(key)
        self._bytes += self._size_of(pm)
        while self._bytes > self.limit_bytes and len(self._data) > 1:
            _, evicted = self._data.popitem(last=False)
            self._bytes -= self._size_of(evicted)

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"frames": len(self._data), "bytes": self._bytes}


class NativeMapView(QGraphicsView):
    """Raster layer stack with pan, wheel zoom and click-to-probe."""

    #: lon, lat of the cursor
    cursorMoved = pyqtSignal(float, float)
    #: lon, lat, ix, iy of a clicked cell
    cellClicked = pyqtSignal(float, float, int, int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QColor(theme.MAP_EMPTY))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)

        self.layers: list[MapLayer] = []
        self.record: int = 0
        self._items: dict[str, QGraphicsPixmapItem] = {}
        self._pixmap_refs: list[QPixmap] = []
        self._probe_layer: str | None = None
        self._cache = _FrameCache()
        #: (west, south, east, north) drawn as a dashed rectangle, for the Domain Builder.
        self._domain_rect: tuple[float, float, float, float] | None = None
        #: markers: (lon, lat, rgb hex, label)
        self._markers: list[tuple[float, float, str, str]] = []

    # ---------------------------------------------------------------- annotations

    def set_domain_rect(self, bounds: tuple[float, float, float, float] | None) -> None:
        """Show a proposed cut window over the source map."""
        self._domain_rect = bounds
        self.viewport().update()

    def set_markers(self, markers: list[tuple[float, float, str, str]]) -> None:
        """Points of interest: gauges, or basin outlets coloured by closure status."""
        self._markers = markers
        self.viewport().update()

    # ---------------------------------------------------------------- layers

    def set_layers(self, layers: list[MapLayer]) -> None:
        # A fresh layer list can reuse a name that means something entirely different (a new
        # case, a swapped variable) -- see _FrameCache's own docstring for why this must be a
        # full clear rather than a per-layer invalidation.
        self._cache.clear()
        self.layers = layers
        self.refresh()

    @property
    def cache_stats(self) -> dict[str, int]:
        return self._cache.stats

    def set_record(self, record: int) -> None:
        if record != self.record:
            self.record = record
            self.refresh()

    def set_probe_layer(self, name: str | None) -> None:
        self._probe_layer = name

    def refresh(self) -> None:
        """Rebuild the scene from the current layer stack.

        ``self.layers[0]`` is the top of the stack -- the ``LayerPanel``'s own convention
        (``add_layer`` inserts at index 0, and its "Up" button moves a layer to a lower index),
        so it must also paint last/highest here. A bare ``enumerate`` z-value once did the
        opposite: a newly-added variable landed *underneath* the base layer's own ~90%-opaque
        fill, which does the one thing least helpful to a reader -- softening the very layer
        they just asked to see. Found while porting this ordering to the Leaflet renderer,
        which would otherwise have inherited the same inversion.
        """
        self._scene.clear()
        self._items.clear()
        self._pixmap_refs.clear()

        n = len(self.layers)
        for i, layer in enumerate(self.layers):
            if not layer.visible:
                continue

            record = 0 if layer.static else self.record
            key = (layer.name, record, _spec_fingerprint(layer.spec))
            pm = self._cache.get(key)
            if pm is None:
                try:
                    values = layer.array_at(self.record)
                except (IndexError, ValueError, OSError):
                    continue
                rgba = colorize(values, layer.spec, mask=layer.mask)
                pm = array_to_pixmap(rgba)
                self._cache.put(key, pm)
            self._pixmap_refs.append(pm)

            item = QGraphicsPixmapItem(pm)
            g = layer.grid
            # Place and scale into degrees: the pixmap is nx x ny pixels covering the domain.
            item.setPos(g.west, -g.north)
            item.setScale(g.gsize)
            item.setZValue(n - i)
            item.setTransformationMode(Qt.TransformationMode.FastTransformation)
            self._scene.addItem(item)
            self._items[layer.name] = item

        if self.layers:
            g = self.layers[0].grid
            self._scene.setSceneRect(QRectF(g.west, -g.north,
                                            g.east - g.west, g.north - g.south))

    # ---------------------------------------------------------------- navigation

    def zoom_to(self, west: float, south: float, east: float, north: float) -> None:
        rect = QRectF(west, -north, east - west, north - south)
        if not self.layers:
            # refresh() only sets a scene rect when there is a raster layer to size it from.
            # With none (markers only, e.g. a traced river path with no base layer), the scene
            # rect stays at its post-clear() default, and a QGraphicsView with no items and no
            # scene rect can end up not painting its viewport at all -- including
            # drawForeground() overlays like markers, even though fitInView() below still
            # computes a perfectly correct transform. Verified by comparing a screenshot taken
            # before this fix (transform fitted the marker bounds correctly; nothing rendered)
            # and after (the same transform, path visible).
            self._scene.setSceneRect(rect)
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_to_layers(self) -> None:
        if not self.layers:
            return
        g = self.layers[0].grid
        self.zoom_to(g.west, g.south, g.east, g.north)

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
        scale = self.transform().m11() * factor
        if MIN_SCALE <= scale <= MAX_SCALE:
            self.scale(factor, factor)

    # ---------------------------------------------------------------- interaction

    def _geo_at(self, pos) -> tuple[float, float]:
        p: QPointF = self.mapToScene(pos)
        return p.x(), -p.y()

    def mouseMoveEvent(self, event) -> None:
        lon, lat = self._geo_at(event.pos())
        self.cursorMoved.emit(lon, lat)
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        lon, lat = self._geo_at(event.pos())
        if self.layers:
            cell = self.layers[0].grid.cell_of(lon, lat)
            if cell:
                self.cellClicked.emit(lon, lat, cell[0], cell[1])
        super().mouseDoubleClickEvent(event)

    def probe(self, lon: float, lat: float) -> list[tuple[str, str]]:
        """(layer name, formatted value) at a point, for the status bar and probe panel."""
        out: list[tuple[str, str]] = []
        for layer in self.layers:
            if not layer.visible:
                continue
            cell = layer.grid.cell_of(lon, lat)
            if cell is None:
                continue
            ix, iy = cell
            try:
                v = float(layer.array_at(self.record)[iy, ix])
            except (IndexError, ValueError, OSError):
                continue
            if layer.mask is not None and not layer.mask[iy, ix]:
                out.append((layer.name, "outside the model domain"))
            else:
                out.append((layer.name, format_value(v, layer.unit)))
        return out

    # ---------------------------------------------------------------- overlays

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """Domain rectangle, markers, and the scale bar."""
        super().drawForeground(painter, rect)

        # Annotations first, in scene coordinates so they track pan and zoom.
        if self._domain_rect or self._markers:
            painter.save()
            if self._domain_rect:
                w, s, e, n = self._domain_rect
                pen = QPen(QColor(theme.MARKER_ORANGE), 0)
                pen.setStyle(Qt.PenStyle.DashLine)
                pen.setCosmetic(True)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.drawRect(QRectF(w, -n, e - w, n - s))
            for lon, lat, colour, _label in self._markers:
                pen = QPen(QColor(colour), 0)
                pen.setCosmetic(True)
                pen.setWidth(2)
                painter.setPen(pen)
                painter.setBrush(QColor(colour))
                r = 3.0 / max(self.transform().m11(), 1e-9)
                painter.drawEllipse(QPointF(lon, -lat), r, r)
            painter.restore()

        painter.save()
        painter.resetTransform()

        vp = self.viewport().rect()
        deg_per_px = 1.0 / max(self.transform().m11(), 1e-9)

        # Scale bar: pick a round distance close to 140 px at this zoom.
        mid_lat = -self.mapToScene(vp.center()).y()
        km_per_deg = 111.32 * max(np.cos(np.radians(mid_lat)), 0.05)
        target_km = 140 * deg_per_px * km_per_deg
        nice = min([1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000],
                   key=lambda k: abs(k - target_km))
        px = nice / km_per_deg / deg_per_px

        x0, y0 = 14, vp.height() - 20
        painter.setPen(QPen(QColor(theme.TEXT), 2))
        painter.drawLine(int(x0), int(y0), int(x0 + px), int(y0))
        painter.drawLine(int(x0), int(y0 - 4), int(x0), int(y0 + 4))
        painter.drawLine(int(x0 + px), int(y0 - 4), int(x0 + px), int(y0 + 4))
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(int(x0), int(y0 - 8), f"{nice} km")

        painter.restore()
