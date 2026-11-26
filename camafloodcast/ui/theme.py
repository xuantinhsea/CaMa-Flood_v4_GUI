"""Light theme (RRI_Floodcast's own palette). One stylesheet, so widget code stays free of
colour literals -- see the module-level constants below, which every other file that needs a
colour imports from here rather than hardcoding its own hex.

RRI_Floodcast itself styles very little explicitly (``#fafafa`` window background, light
borders, a ``#0066CC`` accent, otherwise the platform's native widget look) -- this keeps that
same light palette but, unlike RRI, styles every widget type explicitly, matching how this
app's own dark theme did it, just recoloured rather than restructured.
"""

from __future__ import annotations

# ---------------------------------------------------------------- structural
BG = "#f4f5f7"          #: main window / empty background
PANEL = "#ffffff"       #: group boxes, raised panels, table/list backgrounds
PANEL2 = "#eef1f4"      #: input fields, inactive tab backgrounds, chart/legend fills
LINE = "#d5d9de"        #: borders, separators, chart gridlines
TEXT = "#1c2430"        #: primary text
MUTED = "#5b6472"       #: secondary / hint / caption text
DISABLED = "#a7adb5"    #: disabled text and "not yet" indicators

# ---------------------------------------------------------------- accent
ACCENT = "#0066cc"          #: RRI's own blue -- links, selected-tab underline, running status
ACCENT_HOVER = "#0052a3"
OK = "#2e7d4f"              #: Run button green
OK_HOVER = "#379a60"

# ---------------------------------------------------------------- semantic status
#: Used both as small-text/badge colours and as chart line colours, so each is dark/saturated
#: enough to read on a white background rather than the pale, dark-background-only tones the
#: previous dark theme used for the same roles.
GOOD = "#2e7d32"        #: success / completed
WARN = "#e65100"        #: warning / stale
BAD = "#c62828"         #: error / failed
INFO = "#1565c0"        #: queued / informational (distinct from ACCENT)
PURPLE = "#7b4fa0"      #: extra chart series colour
TAN = "#b5652f"         #: extra chart series colour

#: Map-canvas-only accents (drawn over basemap tiles or a raster layer, not app chrome).
MARKER_BLUE = "#2f6fa8"
MARKER_ORANGE = "#d9622b"
#: A map with no basemap and no layer covering some area (or none at all yet) shows this,
#: distinct from a plain white panel so an empty map doesn't read as "still loading".
MAP_EMPTY = "#e4e7eb"

STYLESHEET = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-size: 12px; }}
QMainWindow::separator {{ background: {LINE}; width: 1px; height: 1px; }}

QGroupBox {{
    border: 1px solid {LINE}; border-radius: 4px;
    margin-top: 10px; padding-top: 8px; background: {PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 8px; padding: 0 4px;
    color: {MUTED}; font-weight: 600;
}}

QTabWidget::pane {{ border: 1px solid {LINE}; background: {PANEL}; }}
QTabBar::tab {{
    background: {PANEL2}; color: {MUTED};
    padding: 6px 14px; border: 1px solid {LINE}; border-bottom: none;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
}}
QTabBar::tab:selected {{ background: {PANEL}; color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:disabled {{ color: {DISABLED}; }}

QPushButton {{
    background: {PANEL2}; border: 1px solid {LINE};
    border-radius: 3px; padding: 5px 12px;
}}
QPushButton:hover {{ background: #e3e7ec; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {DISABLED}; border-color: #e6e9ec; }}
QPushButton#runButton {{ background: {OK}; border-color: {OK}; font-weight: 600; color: white; }}
QPushButton#runButton:hover {{ background: {OK_HOVER}; }}
QPushButton#runButton:disabled {{ background: #cfe3d6; color: #8fa89a; }}

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {PANEL2}; border: 1px solid {LINE}; border-radius: 3px; padding: 3px 6px;
    selection-background-color: {ACCENT}; selection-color: white;
}}
QPlainTextEdit {{ background: #fbfbfc; }}
QComboBox::drop-down {{ border: none; width: 18px; }}

QTableWidget, QTreeWidget, QListWidget {{
    background: {PANEL}; border: 1px solid {LINE};
    alternate-background-color: #f7f8fa; gridline-color: {LINE};
    selection-background-color: #cfe3f7; selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {PANEL2}; color: {MUTED};
    padding: 4px; border: none; border-right: 1px solid {LINE}; border-bottom: 1px solid {LINE};
}}

QProgressBar {{
    background: {PANEL2}; border: 1px solid {LINE}; border-radius: 3px;
    text-align: center; height: 16px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QSlider::groove:horizontal {{ background: {LINE}; height: 4px; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 12px; margin: -5px 0; border-radius: 6px;
}}

QToolBar {{ background: {PANEL}; border-bottom: 1px solid {LINE}; spacing: 4px; padding: 4px; }}
QMenuBar {{ background: {PANEL}; }}
QMenuBar::item:selected {{ background: #e3e7ec; }}
QMenu {{ background: {PANEL}; border: 1px solid {LINE}; }}
QMenu::item:selected {{ background: #cfe3f7; }}
QStatusBar {{ background: {PANEL}; border-top: 1px solid {LINE}; color: {MUTED}; }}
QDockWidget {{ titlebar-close-icon: none; }}
QDockWidget::title {{ background: {PANEL2}; padding: 4px; border-bottom: 1px solid {LINE}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: #c3c9d1; border-radius: 5px; min-height: 24px; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #c3c9d1; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QSplitter::handle {{ background: {LINE}; }}
QToolTip {{ background: #fffbe6; color: {TEXT}; border: 1px solid {ACCENT}; padding: 4px; }}
"""
