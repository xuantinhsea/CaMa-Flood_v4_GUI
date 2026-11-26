"""A tab-local echo of ``MainWindow``'s ``interactive`` switch.

``MainWindow`` learned this the hard way (see ``_warn``/``_inform`` there): in an offscreen or
otherwise automated host, a modal ``QMessageBox`` has nothing to dismiss it and blocks the Qt
event loop forever. That fix lived only on ``MainWindow`` itself; a tab that pops its own
dialogs directly -- as the Domain and Forcing tabs do, for build/scan/apply failures the shell
never sees -- needed the same switch, or a background test exercising one of those failure
paths hangs exactly the way the original bug report described. This mixin is that switch, kept
small enough to mix into any tab that has a ``statusMessage`` signal.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QMessageBox


class InteractiveMixin:
    """Mix into a ``QWidget`` subclass that defines a ``statusMessage = pyqtSignal(str)``.

    Defaults to interactive (normal GUI use). Set ``.interactive = False`` to route every
    warning/critical/information through ``statusMessage`` instead of a modal dialog, and make
    every confirmation question answer itself with ``default`` rather than block.
    """

    interactive: bool = True

    def _warn(self, title: str, text: str) -> None:
        if self.interactive:
            QMessageBox.warning(self, title, text)
        else:
            self._status(f"{title}: {text.splitlines()[0]}")

    def _critical(self, title: str, text: str) -> None:
        if self.interactive:
            QMessageBox.critical(self, title, text)
        else:
            self._status(f"{title}: {text.splitlines()[0]}")

    def _inform(self, title: str, text: str) -> None:
        if self.interactive:
            QMessageBox.information(self, title, text)
        else:
            self._status(f"{title}: {text.splitlines()[0]}")

    def _ask(self, title: str, text: str, *, default: bool = False) -> bool:
        """A yes/no confirmation. Non-interactive answers with ``default`` rather than block --
        callers pick whichever default is safe to proceed with unattended (usually False, i.e.
        "don't overwrite")."""
        if not self.interactive:
            return default
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _status(self, text: str) -> None:
        signal = getattr(self, "statusMessage", None)
        if signal is not None:
            signal.emit(text)
