"""Entry point.

    python -m camafloodcast            (or)    camafloodcast

``--project`` opens a project directly; ``--import`` creates a case from an existing
CaMa-Flood namelist, which is the fastest way for someone who already runs the model by hand
to see their results in the GUI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="camafloodcast",
                                 description="CaMa_Floodcast - CaMa-Flood desktop application")
    ap.add_argument("--project", type=Path, help="open this project directory on startup")
    ap.add_argument("--import", dest="import_nam", type=Path,
                    help="import an existing input_cmf.nam on startup")
    args = ap.parse_args(argv)

    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QApplication

    if sys.platform == "win32":
        # Its own taskbar identity: without one, Windows groups the window under python.exe
        # when run from source and shows Python's icon there instead of the app's own.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CaMa_Floodcast")
        except (AttributeError, OSError):
            pass

    # QtWebEngine (the Leaflet map's backend, see ui.map.leaflet) requires either importing it
    # before any QApplication exists, or this attribute set before one is constructed -- the
    # latter is set here rather than eagerly importing the whole WebEngine module (and its
    # Chromium subprocess) on every startup, including for a user without PyQt6-WebEngine
    # installed at all, who should just fall back to the native renderer, not crash on import.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("CaMa_Floodcast")
    app.setOrganizationName("CaMa_Floodcast")
    # Rendered by packaging/make_icon.py; the same .ico is embedded in the built .exe.
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[1] / "resources"
                                / "camafloodcast.ico")))

    from .main_window import MainWindow
    from ..core.project import Project

    win = MainWindow()

    if args.project:
        try:
            win.set_project(Project.open(args.project))
        except Exception as exc:                              # noqa: BLE001
            print(f"could not open project: {exc}", file=sys.stderr)

    win.show()

    if args.import_nam:
        # Deferred so the window is painted before any dialog appears.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(150, win.import_namelist)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
