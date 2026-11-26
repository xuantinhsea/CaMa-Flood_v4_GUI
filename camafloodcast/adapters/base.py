"""The model-adapter interface.

Defined now even though CaMa-Flood is the only implementation (decision D2). It costs a few
days and it:

* keeps model-specific logic out of the GUI, which otherwise accretes ``if model == ...``
* makes a shared Floodcast platform with RRI possible
* enables a rainfall -> runoff -> continental routing chain across both applications

Anything the GUI needs to know about a model goes through this interface.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..core.errors import DiagnosticList


@dataclass(frozen=True)
class BinarySpec:
    """One executable the model needs, and what it is for."""

    role: str                    # 'model', 'cut_domain', 'downscale', ...
    filename: str                # without platform suffix
    purpose: str
    required_from_phase: str     # 'mvp' | 'domain' | 'downscale' | 'optional'


@dataclass(frozen=True)
class OutputVariable:
    name: str
    unit: str
    description: str
    caveat: str | None = None


@dataclass(frozen=True)
class ProgressEvent:
    """Something the log told us. Carried to the GUI by signal."""

    kind: str                    # 'progress' | 'balance' | 'substeps' | 'error' | 'info'
    fraction: float | None = None
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


class ModelAdapter(abc.ABC):
    """What CaMa_Floodcast needs from any hydrological model."""

    #: short identifier used in project files
    key: str = "abstract"
    #: name shown in the UI
    display_name: str = "Abstract model"

    # ------------------------------------------------------------------ inputs
    @abc.abstractmethod
    def describe_inputs(self) -> Sequence[Any]:
        """The configuration schema, for the GUI to render as a form."""

    @abc.abstractmethod
    def required_binaries(self) -> Sequence[BinarySpec]:
        """Executables this model needs, tiered by the phase that first needs them."""

    @abc.abstractmethod
    def required_capabilities(self, config: Any) -> set[str]:
        """Build-flag capabilities this configuration needs (e.g. ``{'netcdf'}``).

        Cross-checked against the binary registry at pre-flight, so a user with a non-netCDF
        build is told before the run rather than after it fails.
        """

    # ------------------------------------------------------------------ validation
    @abc.abstractmethod
    def validate(self, config: Any, *, base_dir: Path | None = None) -> DiagnosticList:
        """Check a configuration. Returns problems; does not raise."""

    # ------------------------------------------------------------------ execution
    @abc.abstractmethod
    def write_config(self, config: Any, case_dir: Path) -> Path:
        """Write the model's input files into a case directory. Returns the main one."""

    @abc.abstractmethod
    def launch_command(self, case_dir: Path) -> list[str]:
        """Argv to start the model. The working directory is the case directory."""

    @abc.abstractmethod
    def parse_log_line(self, line: str) -> ProgressEvent | None:
        """Turn one log line into a progress event, or None."""

    @abc.abstractmethod
    def log_filename(self) -> str:
        """Name of the log file the model writes, relative to the case directory."""

    # ------------------------------------------------------------------ outputs
    @abc.abstractmethod
    def enumerate_outputs(self, case_dir: Path) -> Sequence[OutputVariable]:
        """Variables this case actually produced."""

    def explain_exit(self, code: int | None) -> str:
        """Plain-language explanation of an exit code."""
        return f"Process exited with code {code}."
