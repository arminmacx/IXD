"""What is on screen while the application is starting.

Starting is not instant: the database is opened, the engine's pools are built,
the browser manifests are checked and the session registration is refreshed.
On a cold disk that is a second or two of nothing at all — the user clicks and
the screen does not change, which reads as a launch that failed rather than one
in progress.

So there is a splash: the icon, an arc turning around it, and the name of the
step being done. It is not decoration over a fixed delay — each step reports
itself as it is reached, so the thing on screen is what is actually happening.

The animation cannot be driven by a timer alone. Startup runs on this same
thread and blocks it, and a timer that only fires when the loop is idle would
sit frozen for exactly the period the splash exists to cover. `step()` drives
it instead: the angle comes from elapsed time rather than from a tick count, so
it is correct whenever it is drawn, however irregularly that is.
"""

from __future__ import annotations

import time

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QPropertyAnimation,
    QRectF,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QWidget

from ..theme import DARK, Palette
from .tray import application_icon

#: Big enough for the icon and two lines under it, small enough to read as a
#: splash rather than a window.
WIDTH = 380
HEIGHT = 220

#: A full turn of the arc, in milliseconds.
SPIN_MS = 1400

#: The splash is not allowed to flash. A start that takes 80 ms would otherwise
#: paint and vanish, which is worse than never showing anything.
MINIMUM_MS = 850


class SplashScreen(QWidget):
    """A frameless card with a turning arc, shown while the service starts."""

    def __init__(self, palette: Palette = DARK) -> None:
        super().__init__(None, Qt.WindowType.SplashScreen
                         | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self._palette = palette
        self._message = "Starting…"
        self._clock = QElapsedTimer()
        self._clock.start()

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(WIDTH, HEIGHT)
        self._icon = application_icon().pixmap(64, 64)
        self._centre()

    def _centre(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.center().x() - WIDTH // 2, area.center().y() - HEIGHT // 2)

    # ------------------------------------------------------------------
    # driving it
    # ------------------------------------------------------------------
    def step(self, message: str) -> None:
        """Say what is happening now, and let the animation catch up.

        Called between the stages of start-up. `processEvents` is what makes
        the arc move at all: the loop is not running yet, so nothing else will
        deliver a paint event.
        """
        self._message = message
        self.repaint()
        QApplication.processEvents()

    def hold(self) -> None:
        """Spin until the splash has been up long enough to have been seen."""
        while self._clock.elapsed() < MINIMUM_MS:
            self.repaint()
            QApplication.processEvents()
            time.sleep(0.016)

    def finish(self, window=None) -> None:
        """Fade out, then close. The window under it is raised first."""
        self.hold()
        if window is not None:
            window.raise_()
            window.activateWindow()

        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(220)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InCubic)
        fade.finished.connect(self.close)
        fade.start()
        # The animation needs the loop that has not started yet, so it is run
        # here rather than left pending — otherwise the splash would still be
        # on screen when the main window appears, and close only later.
        while fade.state() == QPropertyAnimation.State.Running:
            QApplication.processEvents()
            time.sleep(0.008)
        self.close()

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:      # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p = self._palette

        card = QRectF(0, 0, WIDTH, HEIGHT)
        path = QPainterPath()
        path.addRoundedRect(card, 18, 18)
        painter.fillPath(path, QColor(p.surface))
        painter.setPen(QPen(QColor(p.border_strong), 1))
        painter.drawPath(path)

        icon_size = 64
        icon_x = (WIDTH - icon_size) / 2
        icon_y = 34.0
        centre = QRectF(icon_x - 16, icon_y - 16, icon_size + 32, icon_size + 32)

        # The track, then the arc turning on it.
        painter.setPen(QPen(QColor(p.chunk_idle), 3))
        painter.drawArc(centre, 0, 360 * 16)

        elapsed = self._clock.elapsed()
        start = int(-(elapsed % SPIN_MS) / SPIN_MS * 360 * 16)
        pen = QPen(QColor(p.accent), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(centre, start, 100 * 16)

        painter.drawPixmap(int(icon_x), int(icon_y), self._icon)

        name = QFont(self.font())
        name.setPointSizeF(name.pointSizeF() + 2.5)
        name.setWeight(QFont.Weight.DemiBold)
        painter.setFont(name)
        painter.setPen(QColor(p.text))
        painter.drawText(QRectF(0, 138, WIDTH, 24),
                         Qt.AlignmentFlag.AlignHCenter, "Internet Xtreme Downloader")

        step = QFont(self.font())
        step.setPointSizeF(max(8.0, step.pointSizeF() - 0.5))
        painter.setFont(step)
        painter.setPen(QColor(p.text_faint))
        painter.drawText(QRectF(0, 166, WIDTH, 20),
                         Qt.AlignmentFlag.AlignHCenter, self._message)
        painter.end()
