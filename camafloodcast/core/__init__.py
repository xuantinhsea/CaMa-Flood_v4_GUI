"""CaMa_Floodcast core SDK -- pure Python, no Qt.

Everything in this package is usable headless: from a notebook, a script, or CI. The GUI is a
thin shell over it. That separation is deliberate (see doc/camaflood_gui_plan/02-architecture.md):
it keeps model logic out of widget callbacks and makes the hard parts testable without a display.
"""

from .errors import (
    CamaError,
    Diagnostic,
    DiagnosticList,
    FileFormatError,
    ModelRunError,
    NamelistError,
    Severity,
)
from .maps import (
    BifurcationTable,
    DimInfo,
    Grid,
    RiverMap,
    read_bifprm,
    read_diminfo,
    read_params,
)
from .namelist import Namelist, new_namelist

__all__ = [
    "CamaError", "Diagnostic", "DiagnosticList", "FileFormatError", "ModelRunError",
    "NamelistError", "Severity",
    "Grid", "DimInfo", "RiverMap", "BifurcationTable",
    "read_params", "read_diminfo", "read_bifprm",
    "Namelist", "new_namelist",
]
