"""Parameter sweeps: N case variants that differ in exactly one namelist field.

Deliberately thin. Everything a sweep needs already exists: cases are already isolated by
directory (risk-free to run many at once, see runner.py), ``JobQueue`` already runs a list of
cases sequentially (services/execution.py), and the Analysis tab already diffs two cases'
output. A sweep is just the missing piece that generates the N cases in the first place --
copying the base case's namelist byte for byte except the swept field and a distinguishing
``COUTTAG``, so a comparison across the sweep is never comparing anything else by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import schema
from .namelist import Namelist
from .project import Case, Project


def slugify(value: Any) -> str:
    """A filesystem- and COUTTAG-safe token for a swept value: ``0.15`` -> ``0p15``."""
    s = str(value).strip()
    s = s.replace(".", "p").replace("-", "m").replace(" ", "_")
    return "".join(c for c in s if c.isalnum() or c in "p_m") or "x"


@dataclass(frozen=True)
class SweepSpec:
    """One field, swept over a list of values."""

    group: str
    key: str
    values: tuple[Any, ...]
    name_prefix: str = ""

    def __post_init__(self):
        if not self.values:
            raise ValueError("a sweep needs at least one value")
        if schema.field_for(self.group, self.key) is None:
            raise ValueError(f"{self.group}/{self.key} is not in the known namelist schema")

    def case_name(self, value: Any) -> str:
        prefix = self.name_prefix or self.key.lower()
        return f"{prefix}_{slugify(value)}"


def parse_sweep_values(field: schema.Field, raw: str) -> list[Any]:
    """Parse a comma-separated string of sweep values according to a schema field's type.

    Existing purely so the GUI's text box goes through the same typed parsing as everything
    else that touches a namelist value -- never hand a raw string straight to ``Namelist.set``.
    """
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("no values given")
    if field.kind == "logical":
        return [p.strip().upper() in ("TRUE", "T", "1", "YES", ".TRUE.", ".T.") for p in parts]
    if field.kind == "int":
        return [int(p) for p in parts]
    if field.kind == "real":
        return [float(p) for p in parts]
    return parts     # string / path: passed through as given


def build_sweep(project: Project, base_case: Case, spec: SweepSpec, *,
                overwrite: bool = False) -> list[Case]:
    """One new case per value in ``spec.values``, identical to ``base_case`` except for
    ``spec.group``/``spec.key`` and a unique ``COUTTAG`` -- so a later comparison is only ever
    seeing the effect of the swept field, and output files never collide even if inspected
    outside their own case directories.
    """
    if not base_case.exists():
        raise FileNotFoundError(f"base case {base_case.name!r} has no namelist yet")

    cases: list[Case] = []
    for value in spec.values:
        name = spec.case_name(value)
        target_dir = project.root / Project.CASES / name
        if target_dir.exists():
            if not overwrite:
                raise FileExistsError(f"case {name!r} already exists "
                                      f"(pass overwrite=True to replace it)")
            import shutil
            shutil.rmtree(target_dir)

        nml = Namelist.load(base_case.namelist_path)
        nml.set(spec.group, spec.key, value)
        nml.set("NOUTPUT", "COUTTAG", name)
        cases.append(project.add_case(name, namelist=nml))
    return cases
