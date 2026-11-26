"""Map rendering: the MapRenderer abstraction and its backends."""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget


def make_map_view(parent: QWidget | None = None):
    """The map widget every tab should use: Leaflet (a real basemap) when available, the
    native Qt canvas otherwise.

    One place decides -- every call site stays a one-line ``self.map = make_map_view()``
    instead of importing a specific backend, so choosing (or losing, if ``PyQt6-WebEngine``
    is not installed) a basemap renderer is not a per-tab decision.
    """
    from .leaflet import HAS_LEAFLET_MAP

    if HAS_LEAFLET_MAP:
        from .leaflet import LeafletMapView

        return LeafletMapView(parent)

    from .native import NativeMapView

    return NativeMapView(parent)
