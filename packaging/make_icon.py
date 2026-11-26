"""Render the CaMa_Floodcast application icon.

    python packaging/make_icon.py [--preview DIR]

Writes ``camafloodcast/resources/camafloodcast.ico`` (16-256 px) and ``camafloodcast.png``
(256 px). Every size is drawn natively from the same vector description rather than shrunk
from the 256 px image, and the small sizes (<= 32 px) drop the globe's graticule and thicken
the river so they stay legible in a title bar or the taskbar instead of turning to mush.

The motif: a globe (CaMa-Flood is a *global* river model) carrying a branching river network
in the app's own accent blue (``ui/theme.ACCENT``), its lower part going under flood water.

Needs PyQt6 (antialiased vector drawing) and Pillow (to assemble the multi-size .ico).
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "camafloodcast" / "resources"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# ---------------------------------------------------------------- palette
TILE_TOP = "#1b6fd0"
TILE_BOTTOM = "#0a2d5e"
GLOBE_LIGHT = "#ffffff"
GLOBE_EDGE = "#cfe4f8"
GRATICULE = "#9cc2e8"
RIVER = "#0066cc"            # ui/theme.ACCENT
WAVE_BACK = "#2a8fe6"
WAVE_FRONT = "#5ccaf5"
CREST = "#e9f8ff"


def _wave(y0: float, amp: float, periods: float, phase: float, n: int = 96):
    """A closed sine band from ``y0`` down past the bottom edge, in unit coordinates."""
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath

    path = QPainterPath()
    xs = [-0.05 + 1.1 * i / n for i in range(n + 1)]
    pts = [QPointF(x, y0 + amp * math.sin(2 * math.pi * (periods * x + phase))) for x in xs]
    path.moveTo(pts[0])
    for p in pts[1:]:
        path.lineTo(p)
    path.lineTo(1.05, 1.05)
    path.lineTo(-0.05, 1.05)
    path.closeSubpath()
    return path, pts


def _meander(a, b, amp: float, waves: float, w0: float, w1: float, n: int = 72):
    """A river reach from ``a`` to ``b`` (unit coordinates): the straight line between them
    bent into ``waves`` meander bends of amplitude ``amp`` (pinned to zero at both ends),
    as a filled ribbon that widens from ``w0`` upstream to ``w1`` downstream -- a river,
    not a uniform stroke, which is what keeps the network from reading as a tree.

    Returns ``(path, centre)``, ``centre(t)`` being the centreline point at ``t`` in 0..1,
    so a tributary can be aimed exactly at a point on this reach.
    """
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath

    ax, ay = a
    bx, by = b
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length          # along the reach
    nx, ny = -uy, ux                                         # across it

    def centre(t: float) -> tuple[float, float]:
        off = amp * math.sin(2 * math.pi * waves * t) * math.sin(math.pi * t)
        return ax + (bx - ax) * t + nx * off, ay + (by - ay) * t + ny * off

    ts = [i / n for i in range(n + 1)]
    pts = [centre(t) for t in ts]
    left, right = [], []
    for i, t in enumerate(ts):
        x0, y0 = pts[max(i - 1, 0)]
        x1, y1 = pts[min(i + 1, n)]
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy) or 1.0
        px, py = -dy / d, dx / d
        half = 0.5 * (w0 + (w1 - w0) * t)
        x, y = pts[i]
        left.append(QPointF(x + px * half, y + py * half))
        right.append(QPointF(x - px * half, y - py * half))

    path = QPainterPath(left[0])
    for q in left[1:]:
        path.lineTo(q)
    for q in reversed(right):
        path.lineTo(q)
    path.closeSubpath()
    # Round both ends, so reaches overlap cleanly at a confluence. A real union: added as
    # plain subpaths, the default odd-even fill would punch the overlap out as a hole.
    for (x, y), wd in ((pts[0], w0), (pts[-1], w1)):
        cap = QPainterPath()
        cap.addEllipse(QPointF(x, y), wd / 2, wd / 2)
        path = path.united(cap)
    return path, centre


def render(size: int):
    """The icon at ``size`` x ``size`` px, as a QImage (ARGB32, premultiplied)."""
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import (
        QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
        QRadialGradient,
    )

    small = size <= 32
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size, size)                     # draw in unit coordinates from here on

    # Stroke widths are in unit coordinates; floor them at ~1.1 px so thin lines never
    # vanish at small sizes.
    def w(unit: float, min_px: float = 1.1) -> float:
        return max(unit, min_px / size)

    # ------------------------------------------------------------ tile
    inset = 0.0 if small else 0.02
    tile = QPainterPath()
    tile.addRoundedRect(QRectF(inset, inset, 1 - 2 * inset, 1 - 2 * inset), 0.2, 0.2)
    g = QLinearGradient(0, 0, 0, 1)
    g.setColorAt(0.0, QColor(TILE_TOP))
    g.setColorAt(1.0, QColor(TILE_BOTTOM))
    p.fillPath(tile, QBrush(g))
    p.setClipPath(tile)

    # ------------------------------------------------------------ globe
    cx, cy, r = 0.5, 0.45, (0.34 if small else 0.31)
    globe = QPainterPath()
    globe.addEllipse(QPointF(cx, cy), r, r)
    rg = QRadialGradient(QPointF(cx - 0.35 * r, cy - 0.4 * r), 1.4 * r)
    rg.setColorAt(0.0, QColor(GLOBE_LIGHT))
    rg.setColorAt(1.0, QColor(GLOBE_EDGE))
    p.fillPath(globe, QBrush(rg))

    if not small:
        p.save()
        p.setClipPath(globe, Qt.ClipOperation.IntersectClip)
        pen = QPen(QColor(GRATICULE), w(0.009))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k in (0.36, 0.74):                                   # meridians
            p.drawEllipse(QPointF(cx, cy), k * r, r)
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
        for f in (-0.5, 0.0, 0.5):                               # parallels
            y = cy + f * r
            p.drawLine(QPointF(cx - r, y), QPointF(cx + r, y))
        p.restore()

    # ------------------------------------------------------------ river
    # One strongly meandering river crossing the globe diagonally and draining into the flood
    # water, thin upstream and wide downstream, with a single tributary at the larger sizes.
    # Deliberately not a full dendritic network: however it meanders, a stem with several
    # branches reads as a tree at icon size, while a lone S-curved channel reads as a river.
    river = QColor(RIVER)
    p.save()
    p.setClipPath(globe, Qt.ClipOperation.IntersectClip)
    if small:
        main, _ = _meander((0.14, 0.20), (0.78, 0.84), 0.07, 2.0, 0.075, 0.13)
        p.fillPath(main, river)
    else:
        main, at = _meander((0.14, 0.20), (0.78, 0.84), 0.075, 2.0, 0.014, 0.07)
        trib, _ = _meander((0.80, 0.24), at(0.52), 0.025, 1.0, 0.010, 0.03)
        p.fillPath(trib, river)
        p.fillPath(main, river)
    p.restore()

    # ------------------------------------------------------------ flood water
    if small:
        front, crest = _wave(0.70, 0.035, 1.0, 0.1)
        p.fillPath(front, QColor(WAVE_FRONT))
    else:
        back, _ = _wave(0.66, 0.030, 1.35, 0.55)
        back_col = QColor(WAVE_BACK)
        back_col.setAlphaF(0.92)
        p.fillPath(back, back_col)
        front, crest = _wave(0.735, 0.034, 1.1, 0.05)
        fg = QLinearGradient(0, 0.70, 0, 1)
        fg.setColorAt(0.0, QColor(WAVE_FRONT))
        fg.setColorAt(1.0, QColor(WAVE_BACK))
        p.fillPath(front, QBrush(fg))
        crest_path = QPainterPath(crest[0])
        for pt in crest[1:]:
            crest_path.lineTo(pt)
        pen = QPen(QColor(CREST), w(0.016))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.strokePath(crest_path, pen)

    # ------------------------------------------------------------ tile edge
    # A faint light rim keeps the tile's outline readable on a dark taskbar.
    p.setClipping(False)
    rim = QColor("#ffffff")
    rim.setAlphaF(0.18)
    p.strokePath(tile, QPen(rim, w(0.008, 1.0)))
    p.end()
    return img


def _to_pil(img):
    from PIL import Image
    from PyQt6.QtCore import QBuffer, QIODevice

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--preview", type=Path,
                    help="also write each size, and an enlarged contact sheet, into this folder")
    args = ap.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtGui import QGuiApplication
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841

    frames = {s: _to_pil(render(s)) for s in SIZES}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ico = OUT_DIR / "camafloodcast.ico"
    largest = frames[max(SIZES)]
    largest.save(ico, format="ICO", sizes=[(s, s) for s in SIZES],
                 append_images=[frames[s] for s in SIZES if s != max(SIZES)])
    largest.save(OUT_DIR / "camafloodcast.png")
    print(f"wrote {ico}")
    print(f"wrote {OUT_DIR / 'camafloodcast.png'}")

    if args.preview:
        from PIL import Image

        args.preview.mkdir(parents=True, exist_ok=True)
        for s, im in frames.items():
            im.save(args.preview / f"icon_{s}.png")
        # Each size enlarged 4x with nearest-neighbour, so the real pixels are visible, on
        # a light and a dark strip.
        scale, pad = 4, 16
        width = sum(s * scale + pad for s in SIZES) + pad
        height = max(SIZES) * scale + 2 * pad
        sheet = Image.new("RGBA", (width, 2 * height), "#f4f5f7")
        sheet.paste("#1f1f1f", (0, height, width, 2 * height))
        x = pad
        for s in SIZES:
            big = frames[s].resize((s * scale, s * scale), Image.Resampling.NEAREST)
            for row in (0, 1):
                sheet.alpha_composite(big, (x, row * height + pad))
            x += s * scale + pad
        sheet.save(args.preview / "contact_sheet.png")
        print(f"wrote previews to {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
