"""Visual language: dark glassmorphic palette and the application stylesheet.

Colours are defined once here and interpolated into the stylesheet so the
accent can be changed at runtime from the settings dialog without touching
individual widgets.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen


@dataclass(frozen=True)
class Palette:
    """Every colour the UI uses."""

    background: str = "#0d0f16"
    surface: str = "#141827"
    surface_alt: str = "#181d2f"
    glass: str = "rgba(255, 255, 255, 0.045)"
    glass_strong: str = "rgba(255, 255, 255, 0.08)"
    border: str = "rgba(150, 170, 230, 0.16)"
    border_strong: str = "rgba(150, 170, 230, 0.30)"
    text: str = "#e7ecff"
    text_dim: str = "#95a0c2"
    text_faint: str = "#6b7597"
    accent: str = "#5b8cff"
    accent_2: str = "#7e60ff"
    good: str = "#43d6a0"
    warn: str = "#ffbf5e"
    bad: str = "#ff6c7a"
    chunk_idle: str = "#232941"
    chunk_active: str = "#5b8cff"
    chunk_done: str = "#43d6a0"

    def with_accent(self, accent: str) -> "Palette":
        return Palette(**{**self.__dict__, "accent": accent})


DARK = Palette()
LIGHT = Palette(
    background="#eef1f8",
    surface="#ffffff",
    surface_alt="#f5f7fc",
    glass="rgba(15, 20, 40, 0.035)",
    glass_strong="rgba(15, 20, 40, 0.07)",
    border="rgba(40, 60, 120, 0.16)",
    border_strong="rgba(40, 60, 120, 0.30)",
    text="#131a2c",
    text_dim="#5a6484",
    text_faint="#8b93ad",
    chunk_idle="#dfe4f2",
)


#: Where the generated chevrons are kept. A stylesheet cannot draw a shape —
#: `image:` takes a URL and nothing else — so the arrows a spin box and a combo
#: box need have to exist as files somewhere. They are tiny, they are derived
#: entirely from the palette, and they are rewritten whenever a colour changes,
#: so they live beside the rest of the application's data rather than in the
#: source tree.
def _arrow_dir() -> Path:
    from .. import config       # noqa: PLC0415 - avoids a UI→config import cycle
    return Path(config.DATA_DIR) / "theme"


def _chevron(direction: str, colour: str, size: int = 11,
             scale: int = 2) -> str:
    """Draw one chevron and return its path, or "" if it cannot be drawn.

    Styling a spin box at all — a background, a border — hands its sub-controls
    to the stylesheet, and a sub-control with no `image` draws *nothing*. That
    is why the up and down buttons were two blank squares and a combo box had
    no arrow whatsoever: not a colour too dim to see, but no glyph at all.

    Returning "" on failure is deliberate. These are decorations; a machine
    that cannot paint an image off-screen should get the plain sub-controls
    back, not a stylesheet that fails to parse.
    """
    key = hashlib.sha1(f"{direction}|{colour}|{size}".encode()).hexdigest()[:10]
    target = _arrow_dir() / f"chevron-{direction}-{key}.png"
    if target.exists():
        return target.as_posix()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(size * scale, size * scale,
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)

        # A chevron rather than a filled triangle: it reads as "more of this"
        # at eleven pixels, where a triangle turns into a smudge.
        span, depth = 6.0, 3.0
        middle = size / 2.0
        if direction == "up":
            points = [QPointF(middle - span / 2, middle + depth / 2),
                      QPointF(middle, middle - depth / 2),
                      QPointF(middle + span / 2, middle + depth / 2)]
        else:
            points = [QPointF(middle - span / 2, middle - depth / 2),
                      QPointF(middle, middle + depth / 2),
                      QPointF(middle + span / 2, middle - depth / 2)]

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(scale, scale)
        pen = QPen(QColor(colour))
        pen.setWidthF(1.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline(points)
        painter.end()

        image.setDevicePixelRatio(scale)
        if not image.save(str(target), "PNG"):
            return ""
    except Exception:      # noqa: BLE001 - decoration, never fatal
        return ""
    return target.as_posix()


def _tick(colour: str, size: int = 16, scale: int = 2) -> str:
    """The check mark inside a ticked checkbox, for the same reason.

    A styled ``::indicator:checked`` was a filled accent square with nothing
    in it: it does distinguish checked from unchecked, but only by colour, and
    only if you have both in front of you.
    """
    key = hashlib.sha1(f"tick|{colour}|{size}".encode()).hexdigest()[:10]
    target = _arrow_dir() / f"tick-{key}.png"
    if target.exists():
        return target.as_posix()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        image = QImage(size * scale, size * scale,
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(scale, scale)
        pen = QPen(QColor(colour))
        pen.setWidthF(2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPolyline([QPointF(4.0, 8.2), QPointF(6.8, 11.0),
                              QPointF(12.0, 5.2)])
        painter.end()
        image.setDevicePixelRatio(scale)
        if not image.save(str(target), "PNG"):
            return ""
    except Exception:      # noqa: BLE001 - decoration, never fatal
        return ""
    return target.as_posix()


def _arrow_rules(p: Palette) -> str:
    """The sub-control rules for every spin box, date/time edit and combo box.

    ``QAbstractSpinBox`` covers QSpinBox, QDoubleSpinBox and QDateTimeEdit —
    a type selector matches subclasses — so the buttons are described once.
    """
    up = _chevron("up", p.text_dim)
    down = _chevron("down", p.text_dim)
    up_lit = _chevron("up", p.text)
    down_lit = _chevron("down", p.text)
    up_off = _chevron("up", p.text_faint)
    down_off = _chevron("down", p.text_faint)
    # White on the accent fill, in both palettes — the fill is the accent
    # colour whatever the theme, so the mark on it does not follow the text.
    tick = _tick("#ffffff")
    if not (up and down):
        # Nothing could be drawn: leave the sub-controls to the base style
        # rather than describing buttons with no glyph in them.
        return ""

    return f"""
    QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 20px;
        height: 13px;
        margin: 3px 4px 0 0;
        border: none;
        border-radius: 5px;
        background: transparent;
    }}
    QAbstractSpinBox::down-button {{
        subcontrol-position: bottom right;
        margin: 0 4px 3px 0;
    }}
    QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
        background: {p.glass_strong};
    }}
    QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{
        background: {p.accent};
    }}
    QAbstractSpinBox::up-arrow {{ image: url({up}); width: 11px; height: 11px; }}
    QAbstractSpinBox::down-arrow {{ image: url({down}); width: 11px; height: 11px; }}
    QAbstractSpinBox::up-arrow:hover {{ image: url({up_lit}); }}
    QAbstractSpinBox::down-arrow:hover {{ image: url({down_lit}); }}
    /* `:off` is Qt's way of saying the value is already at that end. */
    QAbstractSpinBox::up-arrow:off, QAbstractSpinBox::up-arrow:disabled {{
        image: url({up_off});
    }}
    QAbstractSpinBox::down-arrow:off, QAbstractSpinBox::down-arrow:disabled {{
        image: url({down_off});
    }}

    QComboBox::down-arrow {{ image: url({down}); width: 11px; height: 11px; }}
    QComboBox::down-arrow:on {{ image: url({up_lit}); }}
    QComboBox::down-arrow:disabled {{ image: url({down_off}); }}
    """ + (f"""
    QCheckBox::indicator:checked {{ image: url({tick}); }}
    """ if tick else "")


def stylesheet(palette: Palette = DARK) -> str:
    """Build the full application stylesheet for ``palette``."""
    p = palette
    return _arrow_rules(p) + f"""
    QWidget {{
        background: transparent;
        color: {p.text};
        font-family: "Inter", "Segoe UI", "SF Pro Text", "Ubuntu", system-ui, sans-serif;
        font-size: 13px;
    }}

    QMainWindow, QDialog {{
        background: {p.background};
    }}

    /* ---------- containers ---------- */
    #Sidebar {{
        background: {p.surface};
        border-right: 1px solid {p.border};
    }}
    #Content {{
        background: {p.background};
    }}
    #DetailPanel, #Card {{
        background: {p.glass};
        border: 1px solid {p.border};
        border-radius: 14px;
    }}
    #Toolbar {{
        background: {p.surface};
        border-bottom: 1px solid {p.border};
    }}
    #StatusBar {{
        background: {p.surface};
        border-top: 1px solid {p.border};
        color: {p.text_dim};
    }}

    /* ---------- typography ---------- */
    #BrandTitle {{
        font-size: 15px;
        font-weight: 700;
        letter-spacing: 0.3px;
    }}
    #BrandSub, #SectionLabel {{
        color: {p.text_faint};
        font-size: 10.5px;
        font-weight: 600;
        letter-spacing: 0.9px;
        text-transform: uppercase;
    }}
    #StatValue {{ font-size: 17px; font-weight: 700; }}
    #StatLabel {{ color: {p.text_dim}; font-size: 10.5px; letter-spacing: 0.7px; }}
    #DetailTitle {{ font-size: 14px; font-weight: 650; }}
    #Muted {{ color: {p.text_dim}; }}

    /* ---------- buttons ---------- */
    QPushButton {{
        background: {p.glass_strong};
        border: 1px solid {p.border};
        border-radius: 9px;
        padding: 7px 14px;
        color: {p.text};
        font-weight: 500;
    }}
    QPushButton:hover {{
        background: {p.border};
        border-color: {p.border_strong};
    }}
    QPushButton:pressed {{ background: {p.border_strong}; }}
    QPushButton:disabled {{ color: {p.text_faint}; background: {p.glass}; }}

    QPushButton#Primary {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                    stop:0 {p.accent}, stop:1 {p.accent_2});
        border: none;
        color: #ffffff;
        font-weight: 600;
    }}
    QPushButton#Primary:hover {{ background: {p.accent}; }}
    QPushButton#Danger:hover {{ border-color: {p.bad}; color: {p.bad}; }}

    QPushButton#NavItem {{
        background: transparent;
        border: none;
        border-radius: 9px;
        padding: 9px 12px;
        text-align: left;
        color: {p.text_dim};
        font-weight: 500;
    }}
    QPushButton#NavItem:hover {{ background: {p.glass}; color: {p.text}; }}
    QPushButton#NavItem:checked {{
        background: {p.glass_strong};
        color: {p.text};
        font-weight: 600;
        border-left: 3px solid {p.accent};
    }}

    QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: 9px;
        padding: 7px 11px;
        color: {p.text};
    }}
    QToolButton:hover {{ background: {p.glass_strong}; border-color: {p.border}; }}
    QToolButton:pressed {{ background: {p.border}; }}
    QToolButton:disabled {{ color: {p.text_faint}; }}

    /* ---------- inputs ---------- */
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox,
    QComboBox, QTimeEdit {{
        background: {p.glass};
        border: 1px solid {p.border};
        border-radius: 9px;
        padding: 7px 10px;
        selection-background-color: {p.accent};
        color: {p.text};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
    QComboBox:focus, QTimeEdit:focus {{ border-color: {p.accent}; }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {p.text_faint}; }}

    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{
        background: {p.surface_alt};
        border: 1px solid {p.border_strong};
        border-radius: 9px;
        selection-background-color: {p.accent};
        padding: 4px;
    }}

    QCheckBox, QRadioButton {{ spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {p.border_strong};
        border-radius: 5px;
        background: {p.glass};
    }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
    }}

    /* ---------- table ---------- */
    QTableView {{
        background: transparent;
        border: 1px solid {p.border};
        border-radius: 12px;
        gridline-color: transparent;
        selection-background-color: {p.glass_strong};
        selection-color: {p.text};
        alternate-background-color: {p.glass};
        outline: none;
    }}
    QTableView::item {{ padding: 6px 8px; border: none; }}
    QTableView::item:selected {{ background: {p.glass_strong}; }}

    QHeaderView::section {{
        background: {p.surface};
        color: {p.text_faint};
        border: none;
        border-bottom: 1px solid {p.border};
        padding: 9px 8px;
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.5px;
    }}
    QHeaderView::section:hover {{ color: {p.text_dim}; }}

    /* ---------- misc ---------- */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{
        background: {p.border_strong}; border-radius: 5px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.accent}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{
        background: {p.border_strong}; border-radius: 5px; min-width: 30px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QTabWidget::pane {{
        border: 1px solid {p.border};
        border-radius: 12px;
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {p.text_dim};
        padding: 8px 16px;
        border: none;
        border-bottom: 2px solid transparent;
        font-weight: 500;
    }}
    QTabBar::tab:selected {{ color: {p.text}; border-bottom-color: {p.accent}; }}
    QTabBar::tab:hover {{ color: {p.text}; }}

    QProgressBar {{
        background: {p.chunk_idle};
        border: none;
        border-radius: 5px;
        height: 8px;
        text-align: center;
        color: transparent;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                    stop:0 {p.accent}, stop:1 {p.accent_2});
        border-radius: 5px;
    }}

    QMenu {{
        background: {p.surface_alt};
        border: 1px solid {p.border_strong};
        border-radius: 10px;
        padding: 6px;
    }}
    QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 7px; }}
    QMenu::item:selected {{ background: {p.accent}; color: #ffffff; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}

    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:hover {{ background: {p.border}; }}

    QToolTip {{
        background: {p.surface_alt};
        color: {p.text};
        border: 1px solid {p.border_strong};
        border-radius: 7px;
        padding: 6px 9px;
    }}

    QGroupBox {{
        border: 1px solid {p.border};
        border-radius: 12px;
        margin-top: 14px;
        padding-top: 12px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
        color: {p.text_faint};
        font-size: 11px;
        letter-spacing: 0.7px;
    }}

    QListWidget, QTreeWidget {{
        background: transparent;
        border: 1px solid {p.border};
        border-radius: 10px;
        outline: none;
    }}
    QListWidget::item, QTreeWidget::item {{ padding: 7px 9px; border-radius: 7px; }}
    QListWidget::item:selected, QTreeWidget::item:selected {{
        background: {p.glass_strong}; color: {p.text};
    }}
    """


def apply_theme(app, palette: Palette = DARK) -> None:
    """Apply the palette and stylesheet to a ``QApplication``."""
    app.setStyle("Fusion")

    qt_palette = QPalette()
    qt_palette.setColor(QPalette.ColorRole.Window, QColor(palette.background))
    qt_palette.setColor(QPalette.ColorRole.WindowText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Base, QColor(palette.surface))
    qt_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(palette.surface_alt))
    qt_palette.setColor(QPalette.ColorRole.Text, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Button, QColor(palette.surface))
    qt_palette.setColor(QPalette.ColorRole.ButtonText, QColor(palette.text))
    qt_palette.setColor(QPalette.ColorRole.Highlight, QColor(palette.accent))
    qt_palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    qt_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(palette.surface_alt))
    qt_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(palette.text))
    app.setPalette(qt_palette)

    font = QFont()
    font.setPointSizeF(max(9.0, font.pointSizeF()))
    app.setFont(font)

    app.setStyleSheet(stylesheet(palette))


def status_colour(status: str, palette: Palette = DARK) -> str:
    return {
        "downloading": palette.accent,
        "connecting": palette.accent,
        "assembling": palette.accent,
        "verifying": palette.accent,
        "completed": palette.good,
        "paused": palette.warn,
        "scheduled": palette.warn,
        "queued": palette.text_dim,
        "error": palette.bad,
        "needs_link": palette.bad,
        "cancelled": palette.text_faint,
    }.get(status, palette.text_dim)
