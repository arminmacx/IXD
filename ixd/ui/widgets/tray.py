"""System tray presence, so the engine can keep working with no window open."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
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


class TrayIcon(QSystemTrayIcon):
    """Tray icon with a live summary and the common actions."""

    show_requested = Signal()
    quit_requested = Signal()
    pause_all_requested = Signal()
    resume_all_requested = Signal()
    add_requested = Signal()

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

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_requested.emit()

    def update_status(self, active: int, speed: float) -> None:
        if active:
            text = f"{active} active · {format_bytes(speed)}/s"
        else:
            text = "Idle"
        self._status_action.setText(text)
        self.setToolTip(f"Internet Xtreme Downloader — {text}")

    def notify(self, title: str, message: str) -> None:
        if self.supportsMessages():
            self.showMessage(title, message, application_icon(), 4000)
