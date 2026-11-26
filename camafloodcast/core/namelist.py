"""Fortran namelist parsing and writing, with comments and layout preserved.

Why comment-preserving matters (risk R7): users hand-edit ``input_cmf.nam`` and keep notes in
it. A structured editor that re-emits the file from a dict destroys that on first save and
loses their trust immediately. So this module keeps the original lines and performs *surgical*
edits -- only the value text of a changed key is rewritten, everything else is byte-identical.

Risk R12 is the other half: never edit model input by text substitution. Parse to a typed
structure, modify the structure, serialise. This module is that structure.

Grammar handled (CaMa-Flood style, one assignment per line):

    &GROUPNAME
    KEY = VALUE            ! optional trailing comment
    KEY = "quoted string"
    /

Values: ``.TRUE.``/``.FALSE.`` (and ``.T.``/``.F.``), integers, reals including Fortran
``D``/``E`` exponents (``0.03D0``, ``1.D-5``, ``1.E20``), single- or double-quoted strings, and
comma-separated lists of any of these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .errors import NamelistError

# --------------------------------------------------------------------------- value scanning

_LOGICAL_TRUE = {".true.", ".t."}
_LOGICAL_FALSE = {".false.", ".f."}

# Fortran real: optional sign, digits with optional '.', optional D/E exponent.
_RE_REAL = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[dDeE][+-]?\d+)?$")
_RE_INT = re.compile(r"^[+-]?\d+$")
_RE_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*(.*)$")


def parse_value(text: str) -> Any:
    """Convert one Fortran literal to a Python value.

    A bare integer stays an ``int``; that matters because CaMa writes ``DT = 3600`` and we want
    to round-trip it as ``3600`` rather than ``3600.0``.
    """
    t = text.strip()
    if not t:
        return None

    low = t.lower()
    if low in _LOGICAL_TRUE:
        return True
    if low in _LOGICAL_FALSE:
        return False

    if (len(t) >= 2) and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]

    if _RE_INT.match(t):
        return int(t)

    if _RE_REAL.match(t):
        return float(t.replace("D", "E").replace("d", "e"))

    # Unquoted bare word -- Fortran allows it for strings in some compilers. Keep as text.
    return t


def format_value(value: Any) -> str:
    """Render a Python value as a Fortran literal."""
    if isinstance(value, bool):
        return ".TRUE." if value else ".FALSE."
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 'D0' keeps it a double literal, matching the package's own style (PMANRIV=0.03D0).
        s = repr(value)
        if "e" in s or "E" in s:
            mant, _, exp = s.lower().partition("e")
            return f"{mant}D{int(exp)}"
        return f"{s}D0"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (list, tuple)):
        return ", ".join(format_value(v) for v in value)
    if value is None:
        return ""
    return str(value)


def _split_comment(line: str) -> tuple[str, str]:
    """Split a line into (code, comment) honouring quotes, so ``"a!b"`` is not truncated."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "!":
            return line[:i], line[i:]
    return line, ""


def _split_values(text: str) -> list[str]:
    """Split a value list on commas that are not inside quotes."""
    out, buf, quote = [], [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return [s for s in (x.strip() for x in out) if s]


# --------------------------------------------------------------------------- model


@dataclass
class Entry:
    """One ``KEY = VALUE`` assignment, remembering exactly where it came from."""

    group: str
    key: str
    value: Any
    line_no: int          # 0-based index into Namelist.lines
    comment: str = ""     # includes the leading '!'
    _dirty: bool = False

    @property
    def name(self) -> str:
        return f"{self.group}/{self.key}"


class Namelist:
    """A parsed Fortran namelist that can be edited and written back losslessly.

    Access is case-insensitive on both group and key, because Fortran is::

        nml["NRUNVER"]["LADPSTP"]        # -> True
        nml.get("NDIMTIME", "DT")        # -> 3600
        nml.set("NDIMTIME", "DT", 1800)  # marks the line dirty
        nml.save(path)                   # rewrites only dirty lines
    """

    def __init__(self, lines: list[str], entries: list[Entry], path: Path | None = None):
        self.lines = lines
        self.entries = entries
        self.path = path
        self._index: dict[tuple[str, str], Entry] = {
            (e.group.upper(), e.key.upper()): e for e in entries
        }
        # Scanned from the lines themselves, not just from entries: CaMa-Flood reads several
        # groups only conditionally (NBOUND when LSEALEV, NLEVEE when LLEVEE, NDAMOUT when
        # LDAMOUT -- see cmf_drv_control_mod.F90), so a real namelist commonly has an empty
        # "&GROUP\n/\n" placeholder, or omits the group altogether, for a feature that is off.
        # Deriving groups from entries alone would miss an empty placeholder and treat it as
        # absent, which matters below (see _insert).
        self._groups: list[str] = []
        for raw in lines:
            code, _ = _split_comment(raw)
            s = code.strip()
            if s.startswith("&") and len(s) > 1:
                name = s[1:].strip().split()[0].upper()
                if name and name not in self._groups:
                    self._groups.append(name)

    # ---------------------------------------------------------------- construction

    @classmethod
    def parse(cls, text: str, path: Path | None = None) -> "Namelist":
        lines = text.splitlines()
        entries: list[Entry] = []
        group: str | None = None

        for i, raw in enumerate(lines):
            code, comment = _split_comment(raw)
            s = code.strip()
            if not s:
                continue

            if s.startswith("&"):
                if group is not None:
                    raise NamelistError(f"group &{s[1:]} opened inside &{group}", line=i + 1)
                group = s[1:].strip().split()[0] if len(s) > 1 else ""
                if not group:
                    raise NamelistError("'&' with no group name", line=i + 1)
                continue

            if s in ("/", "&end", "$end") or s.startswith("/"):
                group = None
                continue

            m = _RE_ASSIGN.match(code)
            if not m:
                # Continuation of a previous value, or something we do not model. CaMa's own
                # namelists are one-assignment-per-line, so treat anything else as a comment.
                continue

            if group is None:
                raise NamelistError(f"assignment outside any group: {s!r}", line=i + 1)

            key, value_text = m.group(1), m.group(2).strip().rstrip(",")
            parts = _split_values(value_text)
            if len(parts) > 1:
                value: Any = [parse_value(p) for p in parts]
            else:
                value = parse_value(value_text)

            entries.append(Entry(group, key, value, i, comment))

        if group is not None:
            raise NamelistError(f"group &{group} is never closed with '/'")

        return cls(lines, entries, path)

    @classmethod
    def load(cls, path: Path | str) -> "Namelist":
        p = Path(path)
        return cls.parse(p.read_text(encoding="utf-8", errors="replace"), p)

    # ---------------------------------------------------------------- access

    @property
    def groups(self) -> list[str]:
        return list(self._groups)

    def has(self, group: str, key: str) -> bool:
        return (group.upper(), key.upper()) in self._index

    def get(self, group: str, key: str, default: Any = None) -> Any:
        e = self._index.get((group.upper(), key.upper()))
        return default if e is None else e.value

    def entry(self, group: str, key: str) -> Entry | None:
        return self._index.get((group.upper(), key.upper()))

    def group_items(self, group: str) -> dict[str, Any]:
        g = group.upper()
        return {e.key: e.value for e in self.entries if e.group.upper() == g}

    def __getitem__(self, group: str) -> dict[str, Any]:
        return self.group_items(group)

    def __contains__(self, group: str) -> bool:
        return group.upper() in self._groups

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.entries)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {g: self.group_items(g) for g in self._groups}

    # ---------------------------------------------------------------- mutation

    def set(self, group: str, key: str, value: Any) -> Entry:
        """Change a value, or add the key to an existing group if it is absent."""
        e = self._index.get((group.upper(), key.upper()))
        if e is not None:
            if e.value != value:
                e.value = value
                e._dirty = True
            return e
        return self._insert(group, key, value)

    def _group_header_line(self, group: str) -> int | None:
        """Index of the ``&GROUP`` line, or None if the group is not present."""
        want = group.upper()
        for i, raw in enumerate(self.lines):
            code, _ = _split_comment(raw)
            s = code.strip()
            if s.startswith("&") and s[1:].strip().split()[:1] == [want]:
                return i
        return None

    def _append_group(self, group: str) -> None:
        """Add a new, empty ``&GROUP / `` block at the end of the file.

        CaMa-Flood reads several groups only conditionally -- NBOUND when LSEALEV, NLEVEE when
        LLEVEE, NDAMOUT when LDAMOUT (cmf_drv_control_mod.F90) -- so a real namelist for a run
        with that feature off legitimately has no such group at all. Turning the feature on and
        setting its first field must not fail just because the group was never there to begin
        with; it must not silently reinterpret the file either, so this only ever appends.
        """
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.append(f"&{group}")
        self.lines.append("/")
        self._groups.append(group)

    def _insert(self, group: str, key: str, value: Any) -> Entry:
        g = group.upper()
        if g not in self._groups:
            self._append_group(g)

        existing = [e.line_no for e in self.entries if e.group.upper() == g]
        if existing:
            start = max(existing) + 1
        else:
            # An empty group (as produced by new_namelist): insert straight after the header.
            header = self._group_header_line(g)
            if header is None:
                raise NamelistError(f"group &{group} is registered but has no '&{group}' line")
            start = header + 1

        # Walk forward to the group's terminating '/'.
        insert_at = start
        while insert_at < len(self.lines):
            code, _ = _split_comment(self.lines[insert_at])
            if code.strip().startswith("/"):
                break
            insert_at += 1

        width = max((len(e.key) for e in self.entries if e.group.upper() == g), default=8)
        self.lines.insert(insert_at, f"{key.ljust(width)} = {format_value(value)}")

        for e in self.entries:
            if e.line_no >= insert_at:
                e.line_no += 1

        e = Entry(group, key, value, insert_at, "")
        self.entries.append(e)
        self.entries.sort(key=lambda x: x.line_no)
        self._index[(g, key.upper())] = e
        return e

    def update(self, group: str, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.set(group, k, v)

    # ---------------------------------------------------------------- output

    def render(self) -> str:
        """Rebuild the file text, rewriting only the lines whose value changed."""
        out = list(self.lines)
        for e in self.entries:
            if not e._dirty:
                continue
            original = out[e.line_no]
            code, comment = _split_comment(original)
            m = _RE_ASSIGN.match(code)
            if not m:  # pragma: no cover - would mean the index is stale
                raise NamelistError(f"cannot rewrite line {e.line_no + 1}: {original!r}")
            lhs = code[: m.start(2)]          # keeps the author's alignment and spacing
            out[e.line_no] = f"{lhs}{format_value(e.value)}" + (f"  {comment}" if comment else "")
        return "\n".join(out) + "\n"

    def save(self, path: Path | str | None = None, *, backup: bool = True) -> Path:
        """Write the namelist, taking a timestamped backup of the previous file first.

        The backup is not optional paranoia: this file is the user's experiment definition and
        may contain hand-written notes. See risks R7 and R12.
        """
        from datetime import datetime

        target = Path(path) if path is not None else self.path
        if target is None:
            raise NamelistError("no path given and this namelist was not loaded from a file")

        if backup and target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target.with_suffix(target.suffix + f".{stamp}.bak").write_bytes(target.read_bytes())

        target.write_text(self.render(), encoding="utf-8", newline="\n")
        for e in self.entries:
            e._dirty = False
        self.path = target
        return target

    def diff(self, other: "Namelist") -> list[tuple[str, Any, Any]]:
        """(name, self_value, other_value) for every key that differs or is missing."""
        out: list[tuple[str, Any, Any]] = []
        keys = set(self._index) | set(other._index)
        for g, k in sorted(keys):
            a = self.get(g, k, ...)
            b = other.get(g, k, ...)
            if a != b:
                out.append((f"{g}/{k}", None if a is ... else a, None if b is ... else b))
        return out

    def __repr__(self) -> str:
        return f"<Namelist {self.path.name if self.path else '(memory)'}: " \
               f"{len(self._groups)} groups, {len(self.entries)} keys>"


def new_namelist(groups: list[str]) -> Namelist:
    """Create an empty namelist with the given groups, ready for ``set()``."""
    lines: list[str] = []
    for g in groups:
        lines.append(f"&{g}")
        lines.append("/")
    entries: list[Entry] = []
    nml = Namelist(lines, entries)
    nml._groups = [g.upper() for g in groups]
    return nml
