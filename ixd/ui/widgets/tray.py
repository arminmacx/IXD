"""System tray presence, so the engine can keep working with no window open."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ...core.http_client import format_bytes
from ..theme import DARK, Palette


def application_icon() -> QIcon:
    """Load the packaged icon, falling back to a drawn placeholder."""
    icon = QIcon()
    icon_dir = Path(__file__).resolve().parents[3] / "packaging" / "icons"
    found = False
    for size in (16, 32, 64, 128, 256):
        path = icon_dir / f"ixd-{size}.png"
        if path.exists():
            icon.addFile(str(path))
            found = True
    if found:
        return icon

    pixmap = QPixmap(64, 64)
    pixmap.fill(DARK.accent)
    painter = QPainter(pixmap)
    painter.setPen(DARK.text)
    painter.drawText(pixmap.rect(), 0x0084, "IXD")   # AlignCenter
    painter.end()
    return QIcon(pixmap)


#: The sizes a tray icon is asked for. Rendered at each rather than scaled
#: from one, because a badge scaled down from 256px is a smudge at 16px —
#: which is the size that actually matters here.
BADGE_SIZES = (16, 22, 24, 32, 48, 64, 128, 256)


def badged_icon(base: QIcon, colour: QColor | str,
                sizes: tuple[int, ...] = BADGE_SIZES) -> QIcon:
    """The application icon with a dot on it, for "there is a new version".

    Drawn rather than shipped as a second icon file: the badge has to sit on
    whatever icon the platform hands back, and on Linux that is whichever size
    the theme decided to give us.

    The dot gets a dark ring. A tray sits on a panel of any colour, and an
    accent-coloured dot on a mid-blue panel is invisible — the ring is what
    makes it read on both a light and a dark tray, and it costs one call.
    """
    out = QIcon()
    for size in sizes:
        pixmap = base.pixmap(size, size)
        if pixmap.isNull():
            continue
        pixmap = pixmap.copy()
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        # Proportional, with a floor: below about six pixels a dot stops being
        # a dot and becomes a stray pixel somebody reports as a rendering bug.
        diameter = max(6, round(pixmap.width() * 0.42))
        left = pixmap.width() - diameter
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.drawEllipse(left - 1, 0, diameter + 1, diameter + 1)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(left, 1, diameter - 1, diameter - 1)
        painter.end()
        out.addPixmap(pixmap)
    return out


class TrayIcon(QSystemTrayIcon):
    """Tray icon with a live summary and the common actions."""

    show_requested = Signal()
    quit_requested = Signal()
    pause_all_requested = Signal()
    resume_all_requested = Signal()
    add_requested = Signal()
    update_requested = Signal()

    def __init__(self, palette: Palette = DARK, parent=None) -> None:
        super().__init__(application_icon(), parent)
        self._palette = palette

        menu = QMenu()
        self._status_action = QAction("Idle", menu)
        self._status_action.setEnabled(False)
        menu.addAction(self._status_action)
        menu.addSeparator()

        show_action = QAction("Open Internet Xtreme Downloader", menu)
        show_action.triggered.connect(self.show_requested.emit)
        menu.addAction(show_action)

        add_action = QAction("Add download…", menu)
        add_action.triggered.connect(self.add_requested.emit)
        menu.addAction(add_action)

        # Hidden until there is one. A permanently greyed "no updates" entry
        # is a line everybody reads once and never again.
        self._update_action = QAction("Update available…", menu)
        self._update_action.triggered.connect(self.update_requested.emit)
        self._update_action.setVisible(False)
        menu.addAction(self._update_action)
        menu.addSeparator()

        resume_action = QAction("Resume all", menu)
        resume_action.triggered.connect(self.resume_all_requested.emit)
        menu.addAction(resume_action)

        pause_action = QAction("Pause all", menu)
        pause_action.triggered.connect(self.pause_all_requested.emit)
        menu.addAction(pause_action)
        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self.setToolTip("Internet Xtreme Downloader")
        self.activated.connect(self._on_activated)
        self._update_version = ""
        self._active, self._speed = 0, 0.0

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_requested.emit()

    def set_update_available(self, version: str) -> None:
        """Put a dot on the icon, or take it off again.

        The check runs on its own a minute after start-up whether or not
        anybody asked, and until now the only sign was a strip in a window that
        is usually closed to the tray. Asked for as a mark on the icon itself,
        visible without going to look.
        """
        version = str(version or "")
        if version == self._update_version:
            return
        self._update_version = version
        if version:
            self.setIcon(badged_icon(application_icon(), self._palette.accent))
            self._update_action.setText(f"Version {version} is available…")
            self._update_action.setVisible(True)
        else:
            self.setIcon(application_icon())
            self._update_action.setVisible(False)
        self.update_status(self._active, self._speed)

    def update_status(self, active: int, speed: float) -> None:
        self._active, self._speed = active, speed
        if active:
            text = f"{active} active · {format_bytes(speed)}/s"
        else:
            text = "Idle"
        self._status_action.setText(text)
        if self._update_version:
            text = f"{text} · version {self._update_version} available"
        self.setToolTip(f"Internet Xtreme Downloader — {text}")

    def notify(self, title: str, message: str) -> None:
        if self.supportsMessages():
            self.showMessage(title, message, application_icon(), 4000)
