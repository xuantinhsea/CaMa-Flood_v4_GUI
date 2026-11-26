"""The model binary registry: what it is, what it can do, and can it actually start.

CaMa-Flood is not one executable, and a given ``MAIN_cmf`` is not interchangeable with another:
preprocessor flags change its behaviour (``-DSinglePrec_CMF``) and its capabilities
(``-DUseCDF_CMF``). So the application tracks a **build signature**, not just a path, and
pre-flight cross-checks the configuration against it.

The other job here is risk R4, which is not theoretical -- it broke the integration test. A
MinGW-built ``MAIN_cmf.exe`` needs ``libgfortran`` / ``libgomp`` / ``libwinpthread`` at run
time. If they are not reachable the process dies with ``STATUS_ENTRYPOINT_NOT_FOUND``
(``0xC0000139``): non-zero exit, **no output at all**, nothing in the log. Resolving the
runtime directory and *prepending* it to PATH is what makes the binary usable.

Prepend, never replace: replacing PATH breaks Python and everything else in the process tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

WIN_ENTRYPOINT_NOT_FOUND = -1073741511      # 0xC0000139
WIN_DLL_NOT_FOUND = -1073741515             # 0xC0000135

#: DLLs a MinGW gfortran build needs beside it at run time.
MINGW_RUNTIME = ("libgfortran", "libgomp", "libwinpthread", "libquadmath", "libgcc_s")


@dataclass
class BuildSignature:
    """What a binary was compiled with, and therefore what it can do."""

    model_version: str = "unknown"
    compiler: str = "unknown"
    flags: str = ""
    capabilities: dict[str, bool] = field(default_factory=dict)
    built: str = ""
    source: str = "declared"          # 'manifest' | 'declared' | 'unknown'

    @property
    def caps(self) -> set[str]:
        return {k for k, v in self.capabilities.items() if v}

    def supports(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability, False))

    def describe(self) -> str:
        on = ", ".join(sorted(self.caps)) or "none"
        return f"{self.model_version} ({self.compiler}) -- capabilities: {on}"


@dataclass
class SelfTestResult:
    ok: bool
    returncode: int | None
    message: str
    hint: str = ""
    runtime_path: Path | None = None


def read_manifest(exe: Path) -> BuildSignature | None:
    """Read ``manifest.json`` beside the executable, if the build wrote one."""
    exe = Path(exe)
    # Path("") collapses to Path("."), which *exists* but has no name -- with_name() then
    # raises. An unset binary path is an ordinary state, not an error, so handle it here.
    if not exe.name or not exe.is_file():
        return None
    p = exe.with_name("manifest.json")
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return BuildSignature(
        model_version=d.get("model_version", "unknown"),
        compiler=d.get("compiler", "unknown"),
        flags=d.get("flags", ""),
        capabilities=dict(d.get("capabilities", {})),
        built=d.get("built", ""),
        source="manifest",
    )


def _dir_has_runtime(d: Path) -> bool:
    try:
        names = {f.name.lower() for f in d.iterdir() if f.suffix.lower() == ".dll"}
    except OSError:
        return False
    return any(any(n.startswith(stem) for n in names) for stem in MINGW_RUNTIME)


def resolve_runtime_path(exe: Path, *, compiler_hint: str = "") -> Path | None:
    """Directory that must be on PATH for this binary to start.

    Tried in order: beside the executable; the compiler named in the manifest; ``gfortran``
    on PATH; then a few conventional MSYS2 locations. Returns None on non-Windows, where
    ``rpath`` normally handles this.
    """
    exe = Path(exe)
    if sys.platform != "win32" or not exe.name:
        return None

    if exe.parent.exists() and _dir_has_runtime(exe.parent):
        return exe.parent

    # The manifest records the compiler string; if it points at a real path, use its directory.
    for token in compiler_hint.replace("\\", "/").split():
        p = Path(token)
        if p.suffix.lower() == ".exe" and p.exists() and _dir_has_runtime(p.parent):
            return p.parent

    import shutil

    found = shutil.which("gfortran")
    if found:
        d = Path(found).parent
        if _dir_has_runtime(d):
            return d

    for cand in (Path(r"C:\msys64\ucrt64\bin"), Path(r"C:\msys64\mingw64\bin")):
        if cand.exists() and _dir_has_runtime(cand):
            return cand
    return None


def environment_for(exe: Path, *, threads: int | None = None,
                    runtime_path: Path | None = None) -> dict[str, str]:
    """Environment for launching the model: threads, plus the runtime directory PREPENDED."""
    env = dict(os.environ)
    if threads:
        env["OMP_NUM_THREADS"] = str(threads)
    rp = runtime_path or resolve_runtime_path(exe)
    if rp:
        env["PATH"] = str(rp) + os.pathsep + env.get("PATH", "")
    return env


def explain_launch_failure(returncode: int | None) -> tuple[str, str]:
    """(message, hint) for a process that never really started."""
    if returncode == WIN_ENTRYPOINT_NOT_FOUND:
        return (
            "The model could not start: a required runtime library is missing or the wrong "
            "version (STATUS_ENTRYPOINT_NOT_FOUND, 0xC0000139).",
            "A MinGW-built MAIN_cmf.exe needs libgfortran / libgomp / libwinpthread at run "
            "time, and another toolchain earlier on PATH can shadow them. Set the compiler's "
            "bin directory in Simulation -> Model binary. Windows gives no message for this, "
            "which is why the exit code is the only clue.",
        )
    if returncode == WIN_DLL_NOT_FOUND:
        return ("The model could not start: a required DLL was not found (0xC0000135).",
                "Same cause as above -- point the application at the compiler's bin directory.")
    return (f"The model exited with code {returncode}.", "")


def self_test(exe: Path, *, runtime_path: Path | None = None,
              timeout_s: float = 20.0) -> SelfTestResult:
    """Can this binary actually start?

    Runs it in an empty directory with no namelist. A working CaMa-Flood exits **2** with
    ``Cannot open file 'input_cmf.nam'`` -- that is success for our purposes: the process
    started, loaded its libraries and reached its own input handling. Anything else means it
    could not start at all.
    """
    import tempfile

    exe = Path(exe)
    if not exe.exists():
        return SelfTestResult(False, None, f"No executable at {exe}.",
                              "Set the path in Simulation -> Model binary.")

    rp = runtime_path or resolve_runtime_path(exe, compiler_hint=(
        (read_manifest(exe) or BuildSignature()).compiler))

    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run(
                [str(exe)], cwd=tmp, env=environment_for(exe, runtime_path=rp),
                capture_output=True, text=True, errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return SelfTestResult(False, None,
                                  "The model did not exit within the self-test timeout.",
                                  "It may be waiting on input. Check the executable.",
                                  runtime_path=rp)
        except OSError as exc:
            return SelfTestResult(False, None, f"Could not launch the executable: {exc}",
                                  runtime_path=rp)

    out = (proc.stdout or "") + (proc.stderr or "")
    if "input_cmf.nam" in out or proc.returncode == 2:
        return SelfTestResult(
            True, proc.returncode,
            "The model starts correctly and reached its own input handling.",
            runtime_path=rp)

    msg, hint = explain_launch_failure(proc.returncode)
    return SelfTestResult(False, proc.returncode, msg, hint, runtime_path=rp)


@dataclass
class BinaryInfo:
    """Everything the application knows about one model binary."""

    path: Path
    signature: BuildSignature
    runtime_path: Path | None
    tested: SelfTestResult | None = None

    @property
    def usable(self) -> bool:
        return self.tested is None or self.tested.ok

    def missing_capabilities(self, required: set[str]) -> set[str]:
        return {c for c in required if not self.signature.supports(c)}


def inspect(exe: Path, *, run_self_test: bool = True) -> BinaryInfo:
    """Full inspection: manifest, runtime path, and optionally a launch test."""
    exe = Path(exe)
    sig = read_manifest(exe) or BuildSignature(source="unknown")
    rp = resolve_runtime_path(exe, compiler_hint=sig.compiler)
    tested = self_test(exe, runtime_path=rp) if run_self_test else None
    return BinaryInfo(exe, sig, rp, tested)
