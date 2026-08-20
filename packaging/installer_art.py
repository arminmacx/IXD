"""The two bitmaps the custom installer window draws itself with.

Windows statics load BMP and nothing else, so these are written as BMP with the
background baked in — there is no alpha to blend against, and the colour behind
each one is known from the layout.

Everything else in that window is a real control with a real font, which is
what keeps it sharp when Windows scales the installer on a high-DPI display.
Only the mark and the tick are pictures, because neither is text.

    python packaging/installer_art.py [output-dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QApplication

PANEL = QColor("#0a0c12")     # behind the mark, and behind the step dots
BG = QColor("#0d0f16")        # behind the tick, and behind the card
SURFACE = QColor("#141827")   # the card itself
ACCENT = QColor("#5b8cff")
ACCENT_2 = QColor("#7e60ff")
GOOD = QColor("#43d6a0")
FAINT = QColor("#6b7597")
TEXT = QColor("#e7ecff")

#: The card's corner, in pixels. NSIS's `CreateRoundRectRgn` takes the
#: *ellipse* through the corner and Qt takes the radius, so the installer's
#: `RADIUS 14` is this doubled. Page one's card measures a radius-7 arc.
CARD_RADIUS = 7


def _image(width: int, height: int, background: QColor) -> tuple[QImage, QPainter]:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(background)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    return image, painter


def _canvas(size: int, background: QColor) -> tuple[QImage, QPainter]:
    return _image(size, size, background)


def mark(size: int = 40) -> QImage:
    """The application's own arc and downward arrow."""
    image, p = _canvas(size, PANEL)
    centre = size / 2
    radius = size * 0.42
    box = QRect(int(centre - radius), int(centre - radius),
                int(radius * 2), int(radius * 2))

    gradient = QLinearGradient(box.left(), box.top(), box.right(), box.bottom())
    gradient.setColorAt(0.0, ACCENT)
    gradient.setColorAt(1.0, ACCENT_2)
    p.setPen(QPen(QBrush(gradient), size * 0.085, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    p.drawArc(box, 200 * 16, 320 * 16)

    p.setPen(QPen(TEXT, size * 0.075, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    top, bottom = centre - size * 0.2, centre + size * 0.18
    p.drawLine(int(centre), int(top), int(centre), int(bottom))
    p.drawLine(int(centre - size * 0.125), int(centre + size * 0.05),
               int(centre), int(bottom))
    p.drawLine(int(centre + size * 0.125), int(centre + size * 0.05),
               int(centre), int(bottom))
    p.end()
    return image


def tick(size: int = 36) -> QImage:
    """The circled check on the final page."""
    image, p = _canvas(size, BG)
    inset = size * 0.08
    p.setPen(QPen(GOOD, size * 0.07))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRect(int(inset), int(inset),
                        int(size - inset * 2), int(size - inset * 2)))
    p.setPen(QPen(GOOD, size * 0.09, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawLine(int(size * 0.28), int(size * 0.52),
               int(size * 0.44), int(size * 0.68))
    p.drawLine(int(size * 0.44), int(size * 0.68),
               int(size * 0.74), int(size * 0.32))
    p.end()
    return image


def card(width: int = 402, height: int = 110) -> QImage:
    """The install page's card, rounded, on the background it sits on.

    **Why this is a picture and not a control.** The `STATIC` class is
    registered `CS_PARENTDC`, so a window region set on one is ignored when it
    paints and the corners come out square — measured on `l1.png`, 402x110
    with every row full width. `WS_CLIPSIBLINGS` is the bit that would make the
    region count, and adding it emptied the page (`l2.png`, context §3.75).

    Baking the corners into pixels asks Windows to round nothing. The colour
    behind it is known from the layout and there is no alpha in a BMP, so the
    background is painted first and the card drawn over it.
    """
    image, p = _image(width, height, BG)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(SURFACE)
    p.drawRoundedRect(QRectF(0, 0, width, height), CARD_RADIUS, CARD_RADIUS)
    p.end()
    return image


def dot(colour: QColor, size: int = 11) -> QImage:
    """One step-list dot: a filled circle on the brand panel.

    The installer asks for `CreateRoundRectRgn(0, 0, 11, 11, 11, 11)` — an
    ellipse as wide and tall as the square, which is a circle. Same reason as
    `card()` for it being a picture.
    """
    image, p = _image(size, size, PANEL)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawEllipse(QRectF(0, 0, size, size))
    p.end()
    return image


def write(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, image in (("mark", mark()), ("tick", tick()),
                        ("card", card()),
                        ("dot-good", dot(GOOD)),
                        ("dot-accent", dot(ACCENT)),
                        ("dot-faint", dot(FAINT))):
        target = directory / f"{name}.bmp"
        if not image.save(str(target), "BMP"):
            raise RuntimeError(f"could not write {target}")
        written.append(target)
    return written


def main(argv: list[str]) -> int:
    output = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "installer-art"
    QApplication(argv[:1])
    for path in write(output):
        print(f"  wrote {path.name} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
