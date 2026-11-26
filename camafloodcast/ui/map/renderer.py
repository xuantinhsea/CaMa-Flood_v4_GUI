"""Map rendering: the abstraction, and the layer model both backends share.

Decision D3 is deliberately deferred by putting an interface here. The native Qt renderer
(``native.py``) needs no extra dependency and works everywhere; a QtWebEngine + MapLibre
backend can be dropped in later for basemaps and tiles without the rest of the application
noticing.

The one rule both backends obey: **never hand a whole global array to the display layer.**
Rendering takes a window, not a file.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np

from ...core.maps import Grid
from ...services.render import ColorSpec


@dataclass
class MapLayer:
    """One displayed layer.

    ``provider`` returns the array for a given record index. Keeping it a callable rather than
    an array means the layer holds no data: the map asks for exactly the timestep it is showing,
    and the memory-mapped reader underneath serves it without loading the file.
    """

    name: str
    grid: Grid
    provider: Callable[[int], np.ndarray]
    spec: ColorSpec
    visible: bool = True
    mask: np.ndarray | None = None
    unit: str = ""
    #: True when this layer is a static map (topography, network) rather than a time series.
    static: bool = False
    variable: str = ""

    def array_at(self, record: int) -> np.ndarray:
        return self.provider(0 if self.static else record)


class MapRenderer(Protocol):
    """What the rest of the application needs from a map canvas."""

    def set_layers(self, layers: list[MapLayer]) -> None: ...
    def set_record(self, record: int) -> None: ...
    def refresh(self) -> None: ...
    def zoom_to(self, west: float, south: float, east: float, north: float) -> None: ...
    def zoom_to_layers(self) -> None: ...


def geo_to_scene(lon: float, lat: float) -> tuple[float, float]:
    """Scene coordinates are degrees with y flipped, so north is up."""
    return lon, -lat


def scene_to_geo(x: float, y: float) -> tuple[float, float]:
    return x, -y
