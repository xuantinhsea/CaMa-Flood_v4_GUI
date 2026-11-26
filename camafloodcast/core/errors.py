"""Diagnostics shared by every core module.

The whole point of CaMa_Floodcast is that CaMa-Flood fails silently: a truncated basin, a
forcing gap or a stale inpmat all produce plausible output and exit code 0. So diagnostics are
a first-class return value here, not exceptions -- validation collects a *list* of problems and
hands it to the GUI to render, rather than raising on the first one.

Exceptions are reserved for "this file is not what you said it was".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class Severity(enum.IntEnum):
    """Ordered so that ``max()`` over a list gives the worst problem."""

    INFO = 0
    WARNING = 1
    ERROR = 2

    @property
    def label(self) -> str:
        return {0: "INFO", 1: "WARNING", 2: "ERROR"}[int(self)]


@dataclass(frozen=True)
class Diagnostic:
    """One problem, addressed to a human.

    ``where`` and ``line`` let the GUI make the entry clickable -- jump to the offending
    namelist field, map file or forcing date. ``hint`` is the actionable half: what to do
    about it. Populate it whenever the fix is knowable.
    """

    severity: Severity
    code: str
    message: str
    where: str | None = None
    line: int | None = None
    hint: str | None = None

    def __str__(self) -> str:
        loc = f" [{self.where}{f':{self.line}' if self.line else ''}]" if self.where else ""
        out = f"{self.severity.label}: {self.message}{loc}"
        if self.hint:
            out += f"\n    hint: {self.hint}"
        return out


class DiagnosticList(list):
    """A list of :class:`Diagnostic` with convenience predicates."""

    def add(
        self,
        severity: Severity,
        code: str,
        message: str,
        *,
        where: str | None = None,
        line: int | None = None,
        hint: str | None = None,
    ) -> Diagnostic:
        d = Diagnostic(severity, code, message, where, line, hint)
        self.append(d)
        return d

    def error(self, code: str, message: str, **kw) -> Diagnostic:
        return self.add(Severity.ERROR, code, message, **kw)

    def warning(self, code: str, message: str, **kw) -> Diagnostic:
        return self.add(Severity.WARNING, code, message, **kw)

    def info(self, code: str, message: str, **kw) -> Diagnostic:
        return self.add(Severity.INFO, code, message, **kw)

    @property
    def ok(self) -> bool:
        """True when nothing would block a run (warnings are allowed)."""
        return not any(d.severity >= Severity.ERROR for d in self)

    @property
    def worst(self) -> Severity | None:
        return max((d.severity for d in self), default=None)

    def of(self, severity: Severity) -> list[Diagnostic]:
        return [d for d in self if d.severity == severity]

    def summary(self) -> str:
        n_e = len(self.of(Severity.ERROR))
        n_w = len(self.of(Severity.WARNING))
        if not self:
            return "no problems found"
        return f"{n_e} error(s), {n_w} warning(s)"


# --------------------------------------------------------------------------- exceptions


class CamaError(Exception):
    """Base for every exception raised by the core layer."""


class FileFormatError(CamaError):
    """A file is not in the format its name or configuration implies.

    Raised in particular by the binary readers when a file's size does not match the grid it
    is supposed to be on. That check exists because Fortran ``RECL`` means bytes under
    gfortran but 4-byte *words* under some compilers -- a mismatch silently reads
    plausible-looking garbage instead of failing, so we refuse to read at all.
    """

    def __init__(self, path: Path | str, message: str, *, hint: str | None = None):
        self.path = Path(path)
        self.hint = hint
        super().__init__(f"{self.path.name}: {message}" + (f"\n  hint: {hint}" if hint else ""))


class NamelistError(CamaError):
    """The namelist could not be parsed."""

    def __init__(self, message: str, *, line: int | None = None):
        self.line = line
        super().__init__(f"line {line}: {message}" if line else message)


class ModelRunError(CamaError):
    """The model process failed."""

    def __init__(self, message: str, *, returncode: int | None = None, log_tail: str = ""):
        self.returncode = returncode
        self.log_tail = log_tail
        super().__init__(message)
