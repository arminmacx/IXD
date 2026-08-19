"""The illustrated guide shown once, the first time the application runs.

Five pages, each with a drawing rather than a screenshot. Drawings, because a
screenshot of a browser goes stale the moment that browser changes its
settings page, and because what somebody needs here is *where to look*, not a
photograph of one version of one window.

Every picture is painted from the application's own palette, so the guide and
the thing it is describing are visibly the same object.

The page that matters is the extension one: loading it is the single step the
installer cannot do, and the exact folder is printed on it — read from
`integration`, never typed out, because a wrong path in a guide is worse than
no guide.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygon)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout, QWidget)

from ..theme import DARK, Palette, own_window

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService

ART_W, ART_H = 620, 300


# ---------------------------------------------------------------------------
# painting helpers
# ---------------------------------------------------------------------------
def _font(size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setPixelSize(size)
    font.setWeight(weight)
    return font


def _alpha(colour: str, value: int) -> QColor:
    """A palette colour at partial opacity.

    Not by appending two hex digits: Qt reads a nine-character `#AARRGGBB`, so
    `"#5b8cff" + "55"` is a *green* with an alpha of 0x5b rather than a
    translucent blue. It drew a green border round the folder path on the very
    first render.
    """
    result = QColor(colour)
    result.setAlpha(value)
    return result


#: The palette's `glass`/`border` entries are CSS `rgba(...)` strings for the
#: stylesheet, which `QColor` cannot parse. Painting uses these instead.
_OUTLINE = QColor(150, 170, 230, 60)
_CHROME = QColor(60, 70, 100)


def _card(p: QPainter, rect: QRect, fill: QColor, border: QColor | None = None,
          radius: int = 10) -> None:
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)
    p.fillPath(path, QBrush(fill))
    if border is not None:
        p.setPen(QPen(border, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


def _text(p: QPainter, rect: QRect, text: str, colour: QColor, font: QFont,
          align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
          wrap: bool = False) -> None:
    p.setPen(colour)
    p.setFont(font)
    flags = align | (Qt.TextFlag.TextWordWrap if wrap else Qt.TextFlag(0))
    p.drawText(rect, flags, text)


def _numbered(p: QPainter, x: int, y: int, number: int, accent: QColor) -> None:
    """A step bubble, the thing the eye follows through a picture."""
    _card(p, QRect(x, y, 22, 22), accent, radius=11)
    _text(p, QRect(x, y, 22, 22), str(number), QColor("#ffffff"),
          _font(12, QFont.Weight.DemiBold), Qt.AlignmentFlag.AlignCenter)


def _cursor(p: QPainter, x: int, y: int) -> None:
    """A pointer, so "click here" needs no caption."""
    arrow = QPolygon([QPoint(x, y), QPoint(x, y + 17), QPoint(x + 4, y + 13),
                      QPoint(x + 7, y + 19), QPoint(x + 10, y + 18),
                      QPoint(x + 7, y + 12), QPoint(x + 12, y + 12)])
    p.setPen(QPen(QColor("#0d0f16"), 2.4, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(QBrush(QColor("#ffffff")))
    p.drawPolygon(arrow)


def _browser_frame(p: QPainter, rect: QRect, palette: Palette,
                   address: str) -> QRect:
    """A browser window, reduced to the parts somebody has to recognise."""
    _card(p, rect, QColor(palette.surface_alt), _CHROME, 12)
    bar = QRect(rect.left(), rect.top(), rect.width(), 34)
    path = QPainterPath()
    path.addRoundedRect(QRectF(bar), 12, 12)
    path.addRect(QRectF(bar.left(), bar.bottom() - 12, bar.width(), 12))
    p.fillPath(path, QBrush(QColor(palette.background)))

    for index, colour in enumerate(("#ff6c7a", "#ffbf5e", "#43d6a0")):
        p.setBrush(QBrush(QColor(colour)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRect(rect.left() + 14 + index * 16, rect.top() + 13, 8, 8))

    field = QRect(rect.left() + 70, rect.top() + 8, rect.width() - 90, 19)
    _card(p, field, QColor(palette.surface), None, 9)
    _text(p, field.adjusted(10, 0, -6, 0), address, QColor(palette.text_dim),
          _font(11))
    return QRect(rect.left(), bar.bottom(), rect.width(),
                 rect.height() - bar.height())


# ---------------------------------------------------------------------------
# the five drawings
# ---------------------------------------------------------------------------
def draw_welcome(p: QPainter, palette: Palette, paths: dict) -> None:
    accent = QColor(palette.accent)
    body = QRect(40, 30, ART_W - 80, ART_H - 70)
    _card(p, body, QColor(palette.surface), _alpha(palette.accent, 0x44), 14)

    _text(p, QRect(70, 58, 480, 26), "Internet Xtreme Downloader",
          QColor(palette.text), _font(19, QFont.Weight.DemiBold))
    _text(p, QRect(70, 88, 480, 20),
          "A download manager, and a browser that hands it everything.",
          QColor(palette.text_dim), _font(13))

    rows = [
        ("Faster", "One file, many connections at once."),
        ("Video", "YouTube, HLS, DASH — quality chosen by you."),
        ("Nothing bolted on", "No ffmpeg, no yt-dlp, no telemetry."),
    ]
    y = 130
    for index, (title, note) in enumerate(rows):
        _numbered(p, 70, y, index + 1, accent)
        _text(p, QRect(104, y, 200, 22), title, QColor(palette.text),
              _font(13, QFont.Weight.DemiBold))
        _text(p, QRect(250, y, 300, 22), note, QColor(palette.text_dim), _font(12))
        y += 40


def draw_extension(p: QPainter, palette: Palette, paths: dict) -> None:
    accent = QColor(palette.accent)
    inner = _browser_frame(p, QRect(30, 20, ART_W - 60, 200), palette,
                           "chrome://extensions")

    _numbered(p, 46, inner.top() + 16, 1, accent)
    _text(p, QRect(78, inner.top() + 16, 400, 22),
          "Type chrome://extensions in the address bar",
          QColor(palette.text), _font(12, QFont.Weight.DemiBold))

    toggle = QRect(ART_W - 122, inner.top() + 54, 42, 22)
    _card(p, toggle, accent, None, 11)
    p.setBrush(QBrush(QColor("#ffffff")))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QRect(toggle.right() - 19, toggle.top() + 3, 16, 16))
    _numbered(p, 46, inner.top() + 52, 2, accent)
    _text(p, QRect(78, inner.top() + 52, 400, 22), "Turn on Developer mode",
          QColor(palette.text), _font(12, QFont.Weight.DemiBold))

    button = QRect(78, inner.top() + 94, 132, 30)
    _card(p, button, QColor(palette.surface), QColor(palette.accent), 8)
    _text(p, button, "Load unpacked", QColor(palette.text),
          _font(12, QFont.Weight.DemiBold), Qt.AlignmentFlag.AlignCenter)
    _numbered(p, 46, inner.top() + 96, 3, accent)
    _cursor(p, button.right() - 34, button.bottom() - 12)

    _text(p, QRect(46, inner.bottom() - 34, ART_W - 92, 20),
          "…and choose this folder:", QColor(palette.text_dim), _font(12))

    _folder_strip(p, palette, paths, "chrome")


def draw_firefox(p: QPainter, palette: Palette, paths: dict) -> None:
    accent = QColor(palette.accent)
    inner = _browser_frame(p, QRect(30, 20, ART_W - 60, 200), palette,
                           "about:debugging#/runtime/this-firefox")

    steps = [
        "Type about:debugging in the address bar",
        "Choose “This Firefox” on the left",
        "Click “Load Temporary Add-on…” and pick manifest.json",
    ]
    # Debugging is the route until the extension is signed, and saying which
    # step is temporary is better than a note that reads like an apology.
    y = inner.top() + 16
    for index, step in enumerate(steps):
        _numbered(p, 46, y, index + 1, accent)
        _text(p, QRect(78, y, ART_W - 130, 22), step, QColor(palette.text),
              _font(12, QFont.Weight.DemiBold))
        y += 36

    # Kept inside the frame: the note ran past its bottom edge at the first
    # spacing, which reads as a drawing that does not fit its own box.
    note = QRect(46, y + 2, ART_W - 92, 34)
    _text(p, note, "This is the debugging route, and it lasts until Firefox "
                   "closes — its rule for an add-on that is not signed yet. "
                   "Load it again the same way, or use Chrome or Edge.",
          QColor(palette.text_dim), _font(12), wrap=True)

    _folder_strip(p, palette, paths, "firefox")


def _folder_strip(p: QPainter, palette: Palette, paths: dict, key: str) -> None:
    """The folder, exactly as it is on this machine — and why, when it moved.

    This is the one line in the guide that has to be *true* rather than
    helpful, so it is the resolved path and never a description of one. When an
    all-users install has pushed the folders out of the installation directory,
    the reason goes underneath: a path nobody chose, with no explanation, reads
    as the guide being wrong.
    """
    note = str(paths.get("note") or "")
    strip = QRect(30, 232 if note else 236, ART_W - 60, 40)
    _card(p, strip, QColor(palette.background), _alpha(palette.accent, 0x66), 10)
    _text(p, strip.adjusted(16, 0, -16, 0), paths.get(key, ""),
          QColor(palette.accent), _font(12, QFont.Weight.DemiBold))
    if note:
        _text(p, QRect(34, strip.bottom() + 2, ART_W - 68, 26), note,
              QColor(palette.text_faint), _font(11), wrap=True)


def draw_panel(p: QPainter, palette: Palette, paths: dict) -> None:
    accent = QColor(palette.accent)
    inner = _browser_frame(p, QRect(30, 14, ART_W - 60, 232), palette,
                           "a page with a video on it")

    stage = QRect(inner.left() + 22, inner.top() + 18, 300, 170)
    _card(p, stage, QColor("#05070c"), _CHROME, 8)
    p.setBrush(QBrush(QColor(palette.text_faint)))
    p.setPen(Qt.PenStyle.NoPen)
    play = QPolygon([QPoint(stage.center().x() - 12, stage.center().y() - 16),
                     QPoint(stage.center().x() - 12, stage.center().y() + 16),
                     QPoint(stage.center().x() + 18, stage.center().y())])
    p.drawPolygon(play)

    chip = QRect(stage.right() - 128, stage.top() + 12, 116, 30)
    grad = QLinearGradient(chip.left(), chip.top(), chip.right(), chip.bottom())
    grad.setColorAt(0.0, accent)
    grad.setColorAt(1.0, QColor(palette.accent_2))
    path = QPainterPath()
    path.addRoundedRect(QRectF(chip), 8, 8)
    p.fillPath(path, QBrush(grad))
    _text(p, chip, "⤓  Download", QColor("#ffffff"),
          _font(12, QFont.Weight.DemiBold), Qt.AlignmentFlag.AlignCenter)
    _cursor(p, chip.center().x(), chip.bottom() - 2)

    menu = QRect(stage.right() + 24, stage.top() + 26, 210, 150)
    _card(p, menu, QColor(palette.surface), _alpha(palette.accent, 0x66), 10)
    _text(p, QRect(menu.left() + 14, menu.top() + 8, 180, 18), "QUALITY",
          QColor(palette.text_faint), _font(10, QFont.Weight.DemiBold))
    rows = [("1080p · mp4", "184 MB"), ("720p · mp4", "96 MB"),
            ("480p · mp4", "48 MB"), ("Audio only · m4a", "9 MB")]
    y = menu.top() + 32
    for index, (label, size) in enumerate(rows):
        if index == 0:
            _card(p, QRect(menu.left() + 8, y, menu.width() - 16, 26),
                  QColor(palette.surface_alt), None, 7)
        _text(p, QRect(menu.left() + 18, y, 130, 26), label,
              QColor(palette.text) if index == 0 else QColor(palette.text_dim),
              _font(12, QFont.Weight.DemiBold if index == 0 else QFont.Weight.Normal))
        _text(p, QRect(menu.right() - 76, y, 60, 26), size,
              QColor(palette.text_faint), _font(11),
              Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        y += 28

    # The bubbles sit on the things they are about, not in the margin.
    _numbered(p, chip.left() - 28, chip.top() + 4, 1, accent)
    _numbered(p, menu.left() - 26, menu.top() + 6, 2, accent)
    _text(p, QRect(30, 254, ART_W - 60, 20),
          "The panel finds the player by itself. Drag it anywhere; the × puts it "
          "away until the page reloads.",
          QColor(palette.text_dim), _font(12), Qt.AlignmentFlag.AlignCenter)


def draw_window(p: QPainter, palette: Palette, paths: dict) -> None:
    """A small replica of the window a hand-over actually opens."""
    accent = QColor(palette.accent)
    frame = QRect(30, 16, ART_W - 60, 200)
    _card(p, frame, QColor(palette.background), _CHROME, 12)

    label_x = frame.left() + 44
    field_x = frame.left() + 122
    field_w = 216
    rows = [
        ("Address", "https://site.example/video.mp4", True),
        ("File name", "video.mp4", False),
        ("Save in", "C:\\Users\\you\\Downloads", False),
    ]
    y = frame.top() + 24
    for label, value, locked in rows:
        _text(p, QRect(label_x, y, 70, 30), label, QColor(palette.text_dim),
              _font(11), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        field = QRect(field_x, y, field_w, 30)
        _card(p, field, QColor(palette.surface), _OUTLINE, 8)
        _text(p, field.adjusted(10, 0, -8, 0), value,
              QColor(palette.text_faint) if locked else QColor(palette.text),
              _font(11))
        y += 42

    _numbered(p, frame.left() + 14, frame.top() + 28, 1, accent)
    _numbered(p, frame.left() + 14, frame.top() + 112, 2, accent)

    _text(p, QRect(label_x, y + 2, 300, 20), "184.1 MB · video and audio",
          QColor(palette.text_faint), _font(11))

    for index, (label, colour) in enumerate((
            ("Start download", accent),
            ("Download later", QColor(palette.surface)),
            ("Cancel", QColor(palette.surface)))):
        rect = QRect(frame.right() - 158, frame.top() + 24 + index * 44, 140, 34)
        _card(p, rect, colour, None if index == 0 else _OUTLINE, 8)
        _text(p, rect, label,
              QColor("#ffffff") if index == 0 else QColor(palette.text),
              _font(12, QFont.Weight.DemiBold), Qt.AlignmentFlag.AlignCenter)
    _numbered(p, frame.right() - 188, frame.top() + 72, 3, accent)

    _text(p, QRect(30, 226, ART_W - 60, 60),
          "1  the address is shown, never edited      "
          "2  change where it goes\n"
          "3  “Download later” parks it in a queue until the scheduler starts it",
          QColor(palette.text_dim), _font(12),
          Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, wrap=True)


PAGES: list[tuple[str, str, Callable]] = [
    ("Welcome", "What this is, in three lines.", draw_welcome),
    ("The extension — Chrome & Edge",
     "One step the installer cannot do for you.", draw_extension),
    ("The extension — Firefox", "Same idea, different door.", draw_firefox),
    ("The panel on a video", "Hover, choose a quality, done.", draw_panel),
    ("When a download arrives", "Where it goes, and when.", draw_window),
]


# ---------------------------------------------------------------------------
class GuideArt(QWidget):
    """One drawing, repainted when the page changes."""

    def __init__(self, palette: Palette, paths: dict, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._paths = paths
        self._index = 0
        self.setFixedSize(ART_W, ART_H)

    def show_page(self, index: int) -> None:
        self._index = index
        # Re-asked, not remembered. The folder follows the installation, and a
        # guide reopened after a reinstall somewhere else would otherwise be
        # pointing at where the last one was.
        self._paths = extension_paths()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(self._palette.background))
        PAGES[self._index][2](p, self._palette, self._paths)
        p.end()

    def as_pixmap(self) -> QPixmap:
        return self.grab()


class GuideDialog(QDialog):
    """The whole guide: a drawing, a caption, and a way through."""

    def __init__(self, service: "DownloadService", palette: Palette = DARK,
                 parent=None, *, first_run: bool = False) -> None:
        super().__init__(parent)
        self.service = service
        self._palette = palette
        self._index = 0
        #: Shown by start-up rather than asked for. Only then does closing it
        #: write the preference — a person who opened it from the toolbar has
        #: not said anything about whether they want it again.
        self._first_run = first_run

        self.setWindowTitle("Getting started")
        own_window(self)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setFixedWidth(ART_W + 56)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        self.title = QLabel()
        self.title.setObjectName("GuideTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("Muted")
        self.subtitle.setWordWrap(True)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)

        self.art = GuideArt(palette, extension_paths(), self)
        layout.addWidget(self.art, 0, Qt.AlignmentFlag.AlignHCenter)

        self.dots = QLabel()
        self.dots.setObjectName("Muted")
        self.dots.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.dots)

        row = QHBoxLayout()
        self.again = QCheckBox("Show this next time too")
        self.again.setVisible(first_run)
        row.addWidget(self.again)
        row.addStretch(1)
        self.copy_button = QPushButton("Copy the folder")
        self.copy_button.clicked.connect(self._copy_path)
        self.back_button = QPushButton("Back")
        self.back_button.clicked.connect(lambda: self._go(-1))
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("Primary")
        self.next_button.clicked.connect(lambda: self._go(1))
        for button in (self.copy_button, self.back_button, self.next_button):
            button.setMinimumWidth(120)
            row.addWidget(button)
        layout.addLayout(row)

        # Written when it closes, however it closes. Doing it on "Finish" alone
        # meant the window dismissed with the × came back on every launch for
        # ever, which is the behaviour people uninstall over.
        if first_run:
            self.finished.connect(self._remember)
        self._render()

    def _remember(self, _result: int = 0) -> None:
        self.service.settings.set("show_guide", self.again.isChecked())

    # ------------------------------------------------------------------
    def _render(self) -> None:
        title, subtitle, _ = PAGES[self._index]
        self.title.setText(f"<b style='font-size:17px'>{title}</b>")
        self.subtitle.setText(subtitle)
        self.art.show_page(self._index)
        self.dots.setText("   ".join(
            "●" if index == self._index else "○" for index in range(len(PAGES))))
        self.back_button.setEnabled(self._index > 0)
        self.next_button.setText(
            "Finish" if self._index == len(PAGES) - 1 else "Next")
        # Only the two pages that name a folder have one worth copying.
        self.copy_button.setVisible(self._index in (1, 2))

    def _go(self, delta: int) -> None:
        target = self._index + delta
        if target < 0:
            return
        if target >= len(PAGES):
            self.accept()      # `finished` carries the preference; see above
            return
        self._index = target
        self._render()

    def _copy_path(self) -> None:
        paths = extension_paths()
        key = "chrome" if self._index == 1 else "firefox"
        QApplication.clipboard().setText(paths.get(key, ""))
        self.copy_button.setText("Copied")


def extension_paths() -> dict[str, str]:
    """Where the two folders actually are on this machine.

    Asked rather than described, and asked **every time the guide is opened**
    rather than baked in. The user's instruction: *"the address where the
    extension installed need to be where user selected during the
    installation"* — and it is, because `integration` resolves it from where
    the application is actually running rather than from a default.

    The one case where it is somewhere else is worth saying out loud instead of
    silently printing a path nobody chose: an all-users install goes to
    `Program Files`, the account running the application cannot write there,
    and the folders fall back to the data directory (context.md §3.45).
    """
    from ... import integration

    try:
        found = integration.extension_locations()
    except Exception:  # noqa: BLE001 - a guide never blocks a launch
        return {"chrome": "(check the Log for the extension folder)",
                "firefox": "(check the Log for the extension folder)",
                "note": ""}

    note = ""
    if not found["beside_the_application"] and getattr(sys, "frozen", False):
        note = ("Installed for everyone, so the folders are here rather than in "
                f"{found['installation']} — that folder is read-only to you.")
    return {
        "chrome": str(found["chrome"]),
        "firefox": str(found["firefox"]),
        "note": note,
    }
