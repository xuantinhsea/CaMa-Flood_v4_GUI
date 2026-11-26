"""Leaflet basemap renderer: the same layer-stack contract as ``native.NativeMapView``, with a
real basemap underneath (decision D3, finally filled in -- see ``renderer.py``'s docstring).

Built on ``QWebEngineView`` + Leaflet.js, communicating with the page over ``QWebChannel``. The
page is loaded exactly once (``setHtml()`` at construction); every later call -- zoom, add a
layer, drop a marker -- runs JavaScript against that same live page rather than reloading it, so
there is no "markers vanish on reload" state to manage (a real problem the map this was adapted
from, RRI_Floodcast, solves by remembering and replaying its gauges after every reload -- this
renderer has no reloads to replay after).

Everything that touches pixels reuses ``services.render.colorize()`` unchanged, including item 1's
discrete presets, and ``native._FrameCache``/``_spec_fingerprint`` for the same cache this app's
other renderer uses, so switching renderers changes nothing about how a layer is coloured.
"""

from __future__ import annotations

import base64
import json
import logging

from PyQt6.QtCore import pyqtSignal

from ...services.render import colorize, format_value, legend_stops
from .native import _FrameCache, _spec_fingerprint, array_to_pixmap
from .renderer import MapLayer

logger = logging.getLogger(__name__)

#: Free, keyless tile providers -- the same handful RRI_Floodcast uses. "Street" is the default
#: (see DEFAULT_BASEMAP below); "No Base Map" stays selectable in the layer switcher for a
#: blank canvas when a real basemap under a coarse model cell would invite false precision.
BASEMAPS: dict[str, str] = {
    "Street": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "Satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                 "World_Imagery/MapServer/tile/{z}/{y}/{x}"),
    "Topographic": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    "Gray": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
            "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"),
    "No Base Map": "",
}

#: Which of BASEMAPS is active when a map first loads.
DEFAULT_BASEMAP = "Street"

_LEAFLET_CSS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css"
_LEAFLET_JS = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"

_HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<link rel="stylesheet" href="__LEAFLET_CSS__"/>
<script src="__LEAFLET_JS__"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
  * { margin:0; padding:0; }
  html, body, #map { width:100%; height:100%; background:#e4e7eb; }
  .info-panel {
    padding:5px 9px; background:rgba(255,255,255,0.92); color:#1c2430;
    border-radius:4px; font:11px 'Segoe UI',sans-serif; box-shadow:0 1px 5px rgba(0,0,0,0.25);
  }
  .colorbar-panel {
    padding:8px 12px 6px 12px; background:rgba(255,255,255,0.95); color:#1c2430;
    border-radius:5px; font:11px 'Segoe UI',sans-serif; box-shadow:0 1px 6px rgba(0,0,0,0.3);
  }
  .colorbar-title { font-weight:600; margin-bottom:4px; text-align:center; }
  .colorbar-tick { font-size:10px; color:#444; line-height:1; white-space:nowrap; }
  /* One PNG pixel is one model cell -- an upscaled image must stay crisp, not be smoothed
     into invented intermediate values. */
  .raster-crisp {
    image-rendering: pixelated; image-rendering: -moz-crisp-edges; image-rendering: crisp-edges;
  }
  .north-arrow {
    background:rgba(255,255,255,0.88); color:#1c2430; padding:3px 7px; border-radius:4px;
    text-align:center; font:12px 'Segoe UI',sans-serif; line-height:1.2;
    box-shadow:0 1px 5px rgba(0,0,0,0.25);
  }
</style>
</head>
<body>
<div id="map"></div>
<script>
var map = L.map('map', {
    center: [__LAT__, __LON__], zoom: __ZOOM__, zoomControl: true, doubleClickZoom: false,
    attributionControl: false,
});

var _basemapLayers = {};
var _basemapDefs = __BASEMAPS_JSON__;
for (var name in _basemapDefs) {
    _basemapLayers[name] = L.tileLayer(_basemapDefs[name], {subdomains:'abc', maxZoom:19});
}
L.control.layers(_basemapLayers, {}, {position:'topright', collapsed:true}).addTo(map);
var _defaultBasemap = _basemapLayers['__DEFAULT_BASEMAP__'];
if (_defaultBasemap) { _defaultBasemap.addTo(map); }

var coordInfo = L.control({position:'bottomleft'});
coordInfo.onAdd = function() {
    var div = L.DomUtil.create('div', 'info-panel');
    div.id = 'coord-display';
    div.innerHTML = 'Move mouse over map';
    return div;
};
coordInfo.addTo(map);

L.control.scale({position:'bottomleft', maxWidth:150, imperial:false, metric:true}).addTo(map);

var northArrow = L.control({position:'topleft'});
northArrow.onAdd = function() {
    var div = L.DomUtil.create('div', 'north-arrow');
    div.innerHTML = '&#9650;<br>N';
    return div;
};
northArrow.addTo(map);

var colorbar = L.control({position:'bottomright'});
var _colorbarVisible = false;
colorbar.onAdd = function() { return L.DomUtil.create('div', 'colorbar-panel'); };

function showColorbar(title, unit, stopsJson) {
    if (!_colorbarVisible) { colorbar.addTo(map); _colorbarVisible = true; }
    var stops = JSON.parse(stopsJson);
    var div = colorbar.getContainer();
    var html = '<div class="colorbar-title">' + title + (unit ? ' [' + unit + ']' : '') + '</div>';
    html += '<table style="border-spacing:0;"><tr><td style="vertical-align:top;padding-right:6px;">';
    for (var i = stops.length - 1; i >= 0; i--) {
        html += '<div style="width:22px;height:18px;background:rgb(' + stops[i][1].join(',') +
                ');border:1px solid #333;"></div>';
    }
    html += '</td><td style="vertical-align:top;">';
    for (var j = stops.length - 1; j >= 0; j--) {
        html += '<div class="colorbar-tick" style="height:18px;line-height:18px;">' +
                stops[j][0] + '</div>';
    }
    html += '</td></tr></table>';
    div.innerHTML = html;
}

function hideColorbar() {
    if (_colorbarVisible) { map.removeControl(colorbar); _colorbarVisible = false; }
}

// ---- overlays: rasters (image), markers, domain rectangle ----
var _overlays = {};
var _overlayBoundsKey = {};

function setLayerImage(name, dataUrl, south, west, north, east, opacity, zIndex, crisp) {
    var boundsKey = south + '_' + west + '_' + north + '_' + east;
    var existing = _overlays[name];
    if (existing && existing.setUrl && _overlayBoundsKey[name] === boundsKey) {
        existing.setUrl(dataUrl);
        existing.setOpacity(opacity);
        if (existing.setZIndex) existing.setZIndex(zIndex);
        if (!map.hasLayer(existing)) map.addLayer(existing);
        return;
    }
    if (existing) map.removeLayer(existing);
    var opts = {opacity: opacity, zIndex: zIndex};
    if (crisp) opts.className = 'raster-crisp';
    var layer = L.imageOverlay(dataUrl, [[south, west], [north, east]], opts).addTo(map);
    _overlays[name] = layer;
    _overlayBoundsKey[name] = boundsKey;
}

function removeOverlay(name) {
    if (_overlays[name]) { map.removeLayer(_overlays[name]); delete _overlays[name]; delete _overlayBoundsKey[name]; }
}

function clearAllOverlays() {
    for (var name in _overlays) map.removeLayer(_overlays[name]);
    _overlays = {}; _overlayBoundsKey = {};
}

var _markerLayer = L.layerGroup().addTo(map);
function setMarkers(markersJson) {
    _markerLayer.clearLayers();
    var markers = JSON.parse(markersJson);
    for (var i = 0; i < markers.length; i++) {
        var m = markers[i];
        var marker = L.circleMarker([m[1], m[0]], {
            radius:5, color:'#111', weight:1, fillColor:m[2], fillOpacity:0.95,
        });
        if (m[3]) marker.bindTooltip(m[3], {permanent:true, direction:'right', offset:[6,0]});
        marker.addTo(_markerLayer);
    }
}

var _domainRect = null;
function setDomainRect(south, west, north, east) {
    if (_domainRect) { map.removeLayer(_domainRect); _domainRect = null; }
    if (south === null) return;
    _domainRect = L.rectangle([[south, west], [north, east]], {
        color:'#d9622b', weight:2, fill:false, dashArray:'6,4',
    }).addTo(map);
}

function fitBounds(south, west, north, east) {
    map.fitBounds([[south, west], [north, east]]);
}

// ---- per-layer value grids, for the coordinate-bar readout ----
var _valueGrids = {};
function setValueGrid(name, spec) { _valueGrids[name] = JSON.parse(spec); }
function clearValueGrid(name) { delete _valueGrids[name]; }

function _readValues(lat, lng) {
    var parts = [];
    for (var name in _valueGrids) {
        var g = _valueGrids[name];
        if (!_overlays[name] || !map.hasLayer(_overlays[name])) continue;
        var r = Math.floor((g.north - lat) / g.dLat);
        var c = Math.floor((lng - g.west) / g.dLon);
        if (r < 0 || c < 0 || r >= g.values.length) continue;
        var row = g.values[r];
        if (!row || c >= row.length) continue;
        var v = row[c];
        if (v === null || v === g.noData) continue;
        parts.push(g.label + ': ' + v.toFixed(g.decimals) + (g.unit ? ' ' + g.unit : ''));
    }
    return parts;
}

// ---- QWebChannel bridge: mouse move / double-click -> Python ----
if (typeof QWebChannel !== 'undefined') {
    new QWebChannel(qt.webChannelTransport, function(channel) {
        window.pyBridge = channel.objects.pyBridge;
    });
}

map.on('mousemove', function(e) {
    var text = 'Lat ' + e.latlng.lat.toFixed(5) + '   Lon ' + e.latlng.lng.toFixed(5);
    var vals = _readValues(e.latlng.lat, e.latlng.lng);
    if (vals.length) text += '   ' + vals.join('   ');
    document.getElementById('coord-display').innerHTML = text;
    if (window.pyBridge) window.pyBridge.onCursorMoved(e.latlng.lat, e.latlng.lng);
});

map.on('dblclick', function(e) {
    if (window.pyBridge) window.pyBridge.onCellClicked(e.latlng.lat, e.latlng.lng);
});

function invalidateSize() { map.invalidateSize(); }
</script>
</body></html>"""


try:
    from PyQt6.QtCore import QBuffer, QIODevice, QObject, QUrl, pyqtSlot
    from PyQt6.QtWebChannel import QWebChannel
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QVBoxLayout, QWidget

    HAS_LEAFLET_MAP = True
except ImportError as exc:                                        # pragma: no cover
    HAS_LEAFLET_MAP = False
    logger.info("Leaflet map unavailable (%s) -- falling back to the native renderer", exc)


def _pixmap_to_data_url(pm) -> str:
    """``QPixmap`` -> a PNG data URL, using Qt's own encoder -- no Pillow dependency needed."""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    b64 = base64.b64encode(bytes(buf.data())).decode("ascii")
    return f"data:image/png;base64,{b64}"


if HAS_LEAFLET_MAP:

    class _MapBridge(QObject):
        """QWebChannel target: forwards a Leaflet mouse event back into Python."""

        cursorMoved = pyqtSignal(float, float)     # lat, lon
        cellClicked = pyqtSignal(float, float)     # lat, lon

        @pyqtSlot(float, float)
        def onCursorMoved(self, lat: float, lon: float) -> None:
            self.cursorMoved.emit(lat, lon)

        @pyqtSlot(float, float)
        def onCellClicked(self, lat: float, lon: float) -> None:
            self.cellClicked.emit(lat, lon)

    class LeafletMapView(QWidget):
        """Same contract as ``native.NativeMapView`` (see that module), with a real basemap.

        ``self.layers[0]`` is the top of the stack, matching ``LayerPanel``'s own convention
        (see the z-order fix in ``native.py``'s ``refresh()``).
        """

        cursorMoved = pyqtSignal(float, float)              # lon, lat
        cellClicked = pyqtSignal(float, float, int, int)    # lon, lat, ix, iy

        def __init__(self, parent: QWidget | None = None):
            super().__init__(parent)
            self.layers: list[MapLayer] = []
            self.record: int = 0
            self._cache = _FrameCache()
            self._domain_rect: tuple[float, float, float, float] | None = None
            self._markers: list[tuple[float, float, str, str]] = []
            self._shown_names: set[str] = set()
            self._page_ready = False
            self._pending_js: list[str] = []
            self._last_zoom_bounds: tuple[float, float, float, float] | None = None
            self._zoom_pending_reapply = False

            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)

            self.web_view = QWebEngineView()
            self.web_view.loadFinished.connect(self._on_page_loaded)
            settings = self.web_view.settings()
            settings.setAttribute(
                QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

            self._bridge = _MapBridge()
            self._bridge.cursorMoved.connect(self._on_cursor_moved)
            self._bridge.cellClicked.connect(self._on_cell_clicked)
            self._channel = QWebChannel()
            self._channel.registerObject("pyBridge", self._bridge)
            self.web_view.page().setWebChannel(self._channel)

            lay.addWidget(self.web_view)

            html = (_HTML_TEMPLATE
                   .replace("__LEAFLET_CSS__", _LEAFLET_CSS)
                   .replace("__LEAFLET_JS__", _LEAFLET_JS)
                   .replace("__LAT__", "0").replace("__LON__", "0").replace("__ZOOM__", "2")
                   .replace("__BASEMAPS_JSON__", json.dumps(BASEMAPS))
                   .replace("__DEFAULT_BASEMAP__", _js_str(DEFAULT_BASEMAP)))
            self.web_view.setHtml(html)

        # ---------------------------------------------------------------- page lifecycle

        def _on_page_loaded(self, ok: bool) -> None:
            self._page_ready = bool(ok)
            if not ok:
                logger.warning("Leaflet map page failed to load -- queued calls dropped")
                self._pending_js.clear()
                return
            pending, self._pending_js = self._pending_js, []
            for code in pending:
                self.web_view.page().runJavaScript(code)

        def run_js(self, code: str) -> None:
            if self._page_ready:
                self.web_view.page().runJavaScript(code)
            else:
                self._pending_js.append(code)

        def showEvent(self, event) -> None:
            """A WebEngineView on a non-current tab can be sized zero until shown; tell
            Leaflet to re-measure once it actually is, or overlays added while hidden can end
            up mispositioned against the stale size (see native.py's own showEvent for the
            equivalent QGraphicsView concern). A ``fitBounds()`` computed against that zero
            size lands on the whole-world zoom rather than the intended extent, so re-issue
            the last requested zoom too -- but only the one time it was actually requested
            while hidden, so this never overrides a pan/zoom the user made by hand afterwards.
            """
            super().showEvent(event)
            self.run_js("invalidateSize();")
            if self._zoom_pending_reapply and self._last_zoom_bounds is not None:
                self._zoom_pending_reapply = False
                w, s, e, n = self._last_zoom_bounds
                self.run_js(f"fitBounds({s},{w},{n},{e});")

        # ---------------------------------------------------------------- annotations

        def set_domain_rect(self, bounds: tuple[float, float, float, float] | None) -> None:
            self._domain_rect = bounds
            if bounds is None:
                self.run_js("setDomainRect(null, null, null, null);")
            else:
                w, s, e, n = bounds
                self.run_js(f"setDomainRect({s},{w},{n},{e});")

        def set_markers(self, markers: list[tuple[float, float, str, str]]) -> None:
            self._markers = markers
            payload = json.dumps([[lon, lat, colour, label]
                                  for lon, lat, colour, label in markers])
            self.run_js(f"setMarkers('{payload}');")

        # ---------------------------------------------------------------- layers

        def set_layers(self, layers: list[MapLayer]) -> None:
            self._cache.clear()
            self.layers = layers
            self.refresh()

        def set_record(self, record: int) -> None:
            if record != self.record:
                self.record = record
                self.refresh()

        @property
        def cache_stats(self) -> dict[str, int]:
            return self._cache.stats

        def refresh(self) -> None:
            n = len(self.layers)
            wanted: set[str] = set()
            top_visible: MapLayer | None = None

            for i, layer in enumerate(self.layers):
                if not layer.visible:
                    continue
                wanted.add(layer.name)
                if top_visible is None:
                    top_visible = layer   # self.layers[0] is the top of the stack

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

                url = _pixmap_to_data_url(pm)
                g = layer.grid
                z_index = 200 + (n - i)
                self.run_js(
                    f"setLayerImage('{_js_str(layer.name)}','{url}',"
                    f"{g.south},{g.west},{g.north},{g.east},"
                    f"{layer.spec.opacity},{z_index},true);")

            for stale in self._shown_names - wanted:
                self.run_js(f"removeOverlay('{_js_str(stale)}');")
            self._shown_names = wanted

            if top_visible is not None:
                stops = legend_stops(top_visible.spec, n=7)
                stops_json = json.dumps([[format_value(v, top_visible.unit),
                                          list(rgb)] for v, rgb in stops])
                title = top_visible.variable or top_visible.name
                self.run_js(
                    f"showColorbar('{_js_str(title)}','{_js_str(top_visible.unit)}',"
                    f"'{stops_json}');")
            else:
                self.run_js("hideColorbar();")

        # ---------------------------------------------------------------- navigation

        def zoom_to(self, west: float, south: float, east: float, north: float) -> None:
            self._last_zoom_bounds = (west, south, east, north)
            if not self.isVisible():
                self._zoom_pending_reapply = True
            self.run_js(f"fitBounds({south},{west},{north},{east});")

        def zoom_to_layers(self) -> None:
            if not self.layers:
                return
            g = self.layers[0].grid
            self.zoom_to(g.west, g.south, g.east, g.north)

        # ---------------------------------------------------------------- interaction

        def _on_cursor_moved(self, lat: float, lon: float) -> None:
            self.cursorMoved.emit(lon, lat)

        def _on_cell_clicked(self, lat: float, lon: float) -> None:
            if not self.layers:
                return
            cell = self.layers[0].grid.cell_of(lon, lat)
            if cell:
                self.cellClicked.emit(lon, lat, cell[0], cell[1])

        def probe(self, lon: float, lat: float) -> list[tuple[str, str]]:
            """Same semantics as ``NativeMapView.probe`` -- pure Python, no JS round trip,
            since both renderers share the same ``self.layers`` model."""
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


def _js_str(s: str) -> str:
    """Escape a Python string for embedding inside a single-quoted JS string literal."""
    return str(s).replace("\\", "\\\\").replace("'", "\\'")
