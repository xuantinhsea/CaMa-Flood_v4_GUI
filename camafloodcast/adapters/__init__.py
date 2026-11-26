"""Model adapters. CaMa-Flood today; the interface exists so RRI can follow (decision D2)."""

from .base import BinarySpec, ModelAdapter, OutputVariable, ProgressEvent
from .camaflood import CamaFloodAdapter

__all__ = ["ModelAdapter", "BinarySpec", "OutputVariable", "ProgressEvent", "CamaFloodAdapter"]
