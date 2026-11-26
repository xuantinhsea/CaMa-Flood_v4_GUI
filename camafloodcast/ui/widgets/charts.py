"""Matplotlib-backed charts: the live water-balance monitor and gauge hydrographs.

matplotlib rather than pyqtgraph because it is already installed here and the regional
development targets (decision D1) are small enough that redraw cost does not matter. If the
water-balance monitor ever feels sluggish on a global run, swapping this one module for
pyqtgraph is the fix -- which is why the charts live behind small, specific classes rather than
being inlined into the views.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from .. import theme

BG = theme.PANEL
FG = theme.TEXT
GRID = theme.LINE
ACCENT = theme.ACCENT
WARN = theme.WARN
BAD = theme.BAD
GOOD = theme.GOOD


def _style(ax) -> None:
    ax.set_facecolor(BG)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=FG, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6, alpha=0.7)
    ax.xaxis.label.set_color(FG)
    ax.yaxis.label.set_color(FG)
    ax.title.set_color(FG)


def _style_twin(ax) -> None:
    """Like :func:`_style` but for a ``twinx()`` overlay axis: no opaque facecolor (it sits on
    top of the primary axis and would hide everything plotted there) and no second grid."""
    ax.set_facecolor("none")
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.tick_params(colors=FG, labelsize=8)
    ax.grid(False)
    ax.yaxis.label.set_color(FG)


class Canvas(FigureCanvasQTAgg):
    def __init__(self, nrows: int = 1, height: float = 3.0):
        fig = Figure(figsize=(6, height), facecolor=BG, layout="constrained")
        super().__init__(fig)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.axes = fig.subplots(nrows, 1, sharex=(nrows > 1))
        if nrows == 1:
            self.axes = [self.axes]
        for ax in self.axes:
            _style(ax)

    def save(self, path) -> None:
        self.figure.savefig(path, dpi=160, facecolor=BG)


class WaterBalanceChart(QWidget):
    """Live global storage and closure error during a run.

    The distinctive diagnostic: CaMa-Flood prints a global budget line on *every* adaptive
    substep, so if closure starts drifting the user sees it at minute three of an eight-hour
    run rather than afterwards. A healthy run sits near 1e-14.
    """

    LIMIT = 1e-9

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.canvas = Canvas(nrows=2, height=3.4)
        lay.addWidget(self.canvas)

        self.ax_sto, self.ax_err = self.canvas.axes
        self.ax_sto.set_ylabel("storage [1e9 m3]", fontsize=8)
        self.ax_err.set_ylabel("closure error", fontsize=8)
        self.ax_err.set_yscale("log")
        self._drawn = False

    def update_from(self, progress) -> None:
        bal = progress.balances
        if not bal:
            return

        t = np.arange(len(bal))
        storage = np.array([b.sto_new2 for b in bal])
        riv = np.array([b.riv_storage for b in bal])
        err = np.array([max(b.relative_flux_error, b.relative_stage_error, 1e-18) for b in bal])

        self.ax_sto.clear()
        self.ax_err.clear()
        _style(self.ax_sto)
        _style(self.ax_err)

        self.ax_sto.plot(t, storage, color=ACCENT, lw=1.3, label="total")
        self.ax_sto.plot(t, riv, color=GOOD, lw=1.0, alpha=0.8, label="channel")
        self.ax_sto.set_ylabel("storage [1e9 m3]", fontsize=8)
        self.ax_sto.legend(fontsize=7, facecolor=BG, edgecolor=GRID, labelcolor=FG, loc="upper left")

        worst = float(err.max())
        colour = GOOD if worst < self.LIMIT else BAD
        self.ax_err.plot(t, err, color=colour, lw=1.1)
        self.ax_err.axhline(self.LIMIT, color=WARN, lw=0.9, ls="--")
        self.ax_err.set_yscale("log")
        self.ax_err.set_ylabel("closure error", fontsize=8)
        self.ax_err.set_xlabel("substep", fontsize=8)
        self.ax_err.set_title(
            f"worst {worst:.2e}  ({'within tolerance' if worst < self.LIMIT else 'ABOVE LIMIT'})",
            fontsize=8)

        self.canvas.draw_idle()
        self._drawn = True

    def clear(self) -> None:
        for ax in (self.ax_sto, self.ax_err):
            ax.clear()
            _style(ax)
        self.canvas.draw_idle()


class HydrographChart(QWidget):
    """Named series against a shared axis -- record index by default (a time series at one or
    more gauges), or an explicit ``x`` for anything else with the same shape, such as distance
    along a traced river path (see AnalysisView's longitudinal profile)."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.canvas = Canvas(nrows=1, height=3.2)
        lay.addWidget(self.canvas)
        self.ax = self.canvas.axes[0]
        self._series: list[tuple[str, np.ndarray]] = []
        self._series2: list[tuple[str, np.ndarray]] = []
        self._x: np.ndarray | None = None
        self._xlabel = "record"
        self._ax2 = None

    def plot(self, series: list[tuple[str, np.ndarray]], *, unit: str = "",
             dates: list[dt.datetime] | None = None, spinup: int = 0,
             observed: dict[str, np.ndarray] | None = None,
             x: np.ndarray | None = None, xlabel: str = "record",
             series2: list[tuple[str, np.ndarray]] | None = None, unit2: str = "") -> None:
        """``series2``/``unit2`` plot on a second y-axis (``ax.twinx()``) sharing the same x --
        for two quantities with genuinely different units and magnitudes (e.g. inundated area
        in km2 alongside inundated volume in Mm3), where one shared axis would flatten one of
        them. Dashed lines distinguish it from the primary axis at a glance; both axes' legend
        entries are merged into one box so there is still only one legend to read."""
        self.ax.clear()
        _style(self.ax)
        if self._ax2 is not None:
            self._ax2.remove()
            self._ax2 = None
        self._series = series
        self._series2 = series2 or []
        self._x = np.asarray(x) if x is not None else None
        self._xlabel = xlabel

        palette = [ACCENT, GOOD, WARN, theme.PURPLE, theme.TAN, theme.INFO]

        def xs_for(values) -> np.ndarray:
            return self._x if self._x is not None else np.arange(len(values))

        for i, (name, values) in enumerate(series):
            self.ax.plot(xs_for(values), values,
                         color=palette[i % len(palette)], lw=1.3, label=name)
            if observed and name in observed:
                obs = observed[name]
                self.ax.plot(xs_for(obs), obs, color=palette[i % len(palette)],
                             lw=1.0, ls="--", alpha=0.7, label=f"{name} (obs)")

        if spinup > 0 and self._x is None and series:
            self.ax.axvspan(0, spinup, color=WARN, alpha=0.12)
            self.ax.text(spinup / 2, self.ax.get_ylim()[1] * 0.95, "spin-up",
                         color=WARN, fontsize=7, ha="center", va="top")

        self.ax.set_xlabel(xlabel, fontsize=8)
        self.ax.set_ylabel(unit or "value", fontsize=8)

        if self._series2:
            self._ax2 = self.ax.twinx()
            _style_twin(self._ax2)
            for i, (name, values) in enumerate(self._series2):
                self._ax2.plot(xs_for(values), values,
                               color=palette[(i + len(series)) % len(palette)],
                               lw=1.3, ls="--", label=name)
            self._ax2.set_ylabel(unit2 or "value", fontsize=8)

        if series or self._series2:
            lines1, labels1 = self.ax.get_legend_handles_labels()
            lines2, labels2 = (self._ax2.get_legend_handles_labels()
                               if self._ax2 is not None else ([], []))
            self.ax.legend(lines1 + lines2, labels1 + labels2,
                          fontsize=7, facecolor=BG, edgecolor=GRID, labelcolor=FG)
        self.canvas.draw_idle()

    def clear(self) -> None:
        self.ax.clear()
        _style(self.ax)
        if self._ax2 is not None:
            self._ax2.remove()
            self._ax2 = None
        self._series = []
        self._series2 = []
        self._x = None
        self.canvas.draw_idle()

    def export_csv(self, path) -> int:
        import csv

        if not self._series and not self._series2:
            return 0
        all_series = self._series + self._series2
        n = max(len(v) for _, v in all_series)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([self._xlabel] + [name for name, _ in all_series])
            for i in range(n):
                x_val = self._x[i] if self._x is not None and i < len(self._x) else i
                w.writerow([f"{x_val:.6g}" if isinstance(x_val, (int, float, np.floating))
                           else x_val]
                          + [(f"{v[i]:.6g}" if i < len(v) else "") for _, v in all_series])
        return n

    def export_png(self, path) -> None:
        self.canvas.save(path)
