"""Entry point of the frozen CaMa_Floodcast.exe (built by build_exe.bat).

``camafloodcast/__main__.py`` uses a relative import, so it only runs as ``python -m`` --
PyInstaller needs a plain script as the entry point instead. From source, keep using
``python -m camafloodcast``.
"""

import sys


def _hide_child_consoles() -> None:
    """Stop console windows flashing up when a windowed build runs the Fortran tools.

    A windowed .exe has no console, so every console program it starts through
    ``subprocess`` -- the Domain Builder's cut chain, ``downscale_flddph_trib`` (once per day
    in a batch), the model self-test -- would otherwise get a new console window of its own.
    Their output is always captured through pipes, so nothing is lost by hiding it. QProcess
    (the Run Manager) already does exactly this on its own; this gives ``subprocess`` the
    same default. Only applied when there is no console, again matching QProcess.
    """
    import ctypes
    import subprocess

    if ctypes.windll.kernel32.GetConsoleWindow():
        return
    original = subprocess.Popen.__init__

    def __init__(self, *args, **kwargs):
        if len(args) <= 1 and not kwargs.get("creationflags"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__


if __name__ == "__main__":
    if sys.platform == "win32":
        _hide_child_consoles()
    from camafloodcast.ui.app import main
    raise SystemExit(main())
