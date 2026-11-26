"""CaMa-Flood implementation of :class:`ModelAdapter`."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from ..core import schema
from ..core.errors import DiagnosticList
from ..core.namelist import Namelist
from ..core.output import CAVEATS, VARIABLES, OutputSet, split_filename
from ..core.runner import AdaptiveStep, WaterBalance, explain_exit, parse_log_line
from .base import BinarySpec, ModelAdapter, OutputVariable, ProgressEvent

#: The toolchain. CaMa-Flood is not one executable -- the package holds 35 Fortran programs and
#: a full-featured GUI needs about a dozen of them, tiered by the phase that first requires one.
BINARIES: tuple[BinarySpec, ...] = (
    BinarySpec("model", "MAIN_cmf", "The simulation itself", "mvp"),

    BinarySpec("cut_domain", "cut_domain", "Window topography out of a global map", "domain"),
    BinarySpec("cut_bifway", "cut_bifway", "Re-index bifurcation channels to the region", "domain"),
    BinarySpec("set_map", "set_map", "Derive lon/lat, area, river sequence, basin IDs", "domain"),
    BinarySpec("combine_hires", "combine_hires", "Cut the 1-arcmin sub-grid data", "domain"),
    BinarySpec("generate_inpmat", "generate_inpmat", "Build the runoff input matrix", "domain"),
    BinarySpec("set_bifparam", "set_bifparam", "bifori.txt -> bifprm.txt", "domain"),

    BinarySpec("downscale", "downscale_flddph_trib",
               "Redistribute flood stage onto the 1-arcmin sub-grid", "downscale"),
    BinarySpec("conv_flood", "conv_flood", "Convert downscaled depth for visualisation",
               "downscale"),
    BinarySpec("igetday", "igetday", "Days-in-month helper for the downscale loop", "downscale"),

    BinarySpec("alloc_gauge", "allocate_flow_gauge", "Allocate discharge gauges to cells",
               "optional"),
    BinarySpec("alloc_dam", "allocate_dam", "Allocate dams to cells", "optional"),
)


class CamaFloodAdapter(ModelAdapter):
    key = "camaflood"
    display_name = "CaMa-Flood v4.30"

    LOG = "log_CaMa.txt"
    NAMELIST = "input_cmf.nam"

    # ------------------------------------------------------------------ inputs
    def describe_inputs(self) -> Sequence[schema.Group]:
        return schema.ALL_GROUPS

    def required_binaries(self) -> Sequence[BinarySpec]:
        return BINARIES

    def required_capabilities(self, config: Namelist) -> set[str]:
        return schema.required_capabilities(config)

    # ------------------------------------------------------------------ validation
    def validate(self, config: Namelist, *, base_dir: Path | None = None) -> DiagnosticList:
        return schema.validate(config, base_dir=base_dir)

    # ------------------------------------------------------------------ execution
    def write_config(self, config: Namelist, case_dir: Path) -> Path:
        case_dir.mkdir(parents=True, exist_ok=True)
        return config.save(case_dir / self.NAMELIST, backup=True)

    def launch_command(self, case_dir: Path) -> list[str]:
        # MAIN_cmf takes no arguments: it reads input_cmf.nam from the working directory.
        # The caller sets cwd = case_dir.
        return ["MAIN_cmf"]

    def log_filename(self) -> str:
        return self.LOG

    def parse_log_line(self, line: str) -> ProgressEvent | None:
        item = parse_log_line(line)
        if item is None:
            return None
        if isinstance(item, WaterBalance):
            return ProgressEvent(
                "balance",
                message=f"{item.when:%Y-%m-%d %H:%M} storage {item.sto_new2:,.1f}e9 m3",
                payload={
                    "when": item.when,
                    "storage": item.sto_new2,
                    "err_flux": item.relative_flux_error,
                    "err_stage": item.relative_stage_error,
                    "healthy": item.healthy,
                    "riv_storage": item.riv_storage,
                    "fld_storage": item.fld_storage,
                    "flooded_area": item.flooded_area,
                },
            )
        if isinstance(item, AdaptiveStep):
            return ProgressEvent(
                "substeps",
                message=f"adaptive step: {item.substeps} substeps of {item.actual_dt:.0f} s",
                payload={"substeps": item.substeps, "dt": item.actual_dt},
            )
        if isinstance(item, dict):
            if "error" in item:
                return ProgressEvent("error", message=item["error"])
            return ProgressEvent("info", message="", payload=item)
        return None

    def explain_exit(self, code: int | None) -> str:
        return explain_exit(code)

    # ------------------------------------------------------------------ outputs
    def enumerate_outputs(self, case_dir: Path) -> Sequence[OutputVariable]:
        seen: list[OutputVariable] = []
        for p in sorted(Path(case_dir).glob("*.bin")):
            if p.name.startswith("restart"):
                continue
            var, _ = split_filename(p)
            if var in VARIABLES and not any(o.name == var for o in seen):
                unit, desc = VARIABLES[var]
                seen.append(OutputVariable(var, unit, desc, CAVEATS.get(var)))
        return seen
