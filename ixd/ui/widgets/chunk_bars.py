"""Per-chunk progress visualisation.

Each active connection gets its own bar, so the user can see the dynamic
chunking at work: bars appear as workers steal ranges, and fill independently.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..theme import DARK, Palette


class ChunkBars(QWidget):
    """Draws one rounded bar per chunk with its own fill ratio."""

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._chunks: list[dict] = []
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setToolTip("Per-connection progress")

    def set_chunks(self, chunks: list[dict]) -> None:
        """``chunks`` items need ``progress`` (0..1) and ``status``."""
        self._chunks = list(chunks or [])
        self.updateGeometry()
        self.update()

    def sizeHint(self):  # noqa: D102 - Qt naming
        from PySide6.QtCore import QSize
        rows = max(1, (len(self._chunks) + 7) // 8)
        return QSize(320, min(140, 22 + rows * 20))

    def paintEvent(self, event) -> None:  # noqa: D102, N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        palette = self._palette
        if not self._chunks:
            painter.setPen(QColor(palette.text_faint))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "No active connections"
            )
            return

        count = len(self._chunks)
        columns = min(8, count)
        rows = (count + columns - 1) // columns
        spacing = 6.0
        width = self.width()
        height = self.height()

        cell_width = (width - spacing * (columns - 1)) / columns
        cell_height = min(16.0, max(8.0, (height - spacing * (rows - 1)) / rows))

        for index, chunk in enumerate(self._chunks):
            row = index // columns
            column = index % columns
            x = column * (cell_width + spacing)
            y = row * (cell_height + spacing)
            rect = QRectF(x, y, cell_width, cell_height)

            track = QPainterPath()
            track.addRoundedRect(rect, cell_height / 2, cell_height / 2)
            painter.fillPath(track, QColor(palette.chunk_idle))

            ratio = max(0.0, min(1.0, float(chunk.get("progress", 0.0))))
            if ratio <= 0:
                continue

            fill_rect = QRectF(rect)
            fill_rect.setWidth(max(cell_height, rect.width() * ratio))
            fill = QPainterPath()
            fill.addRoundedRect(fill_rect, cell_height / 2, cell_height / 2)

            status = str(chunk.get("status", "active"))
            if status == "done":
                brush = QBrush(QColor(palette.chunk_done))
            elif status == "failed":
                brush = QBrush(QColor(palette.bad))
            else:
                gradient = QLinearGradient(
                    fill_rect.left(), 0.0, fill_rect.right(), 0.0
                )
                gradient.setColorAt(0.0, QColor(palette.accent))
                gradient.setColorAt(1.0, QColor(palette.accent_2))
                brush = QBrush(gradient)
            painter.fillPath(fill, brush)

        painter.end()


class SegmentedProgress(QWidget):
    """A single slim progress bar with a subtle glass track."""

    def __init__(self, palette: Palette = DARK, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._value = 0.0
        self._status = "queued"
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value: float, status: str = "") -> None:
        self._value = max(0.0, min(1.0, value))
        if status:
            self._status = status
        self.update()

    def paintEvent(self, event) -> None:  # noqa: D102, N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette = self._palette
        radius = self.height() / 2

        track = QPainterPath()
        track.addRoundedRect(QRectF(self.rect()), radius, radius)
        painter.fillPath(track, QColor(palette.chunk_idle))

        if self._value > 0:
            fill_rect = QRectF(self.rect())
            fill_rect.setWidth(max(self.height(), self.width() * self._value))
            fill = QPainterPath()
            fill.addRoundedRect(fill_rect, radius, radius)

            if self._status == "completed":
                painter.fillPath(fill, QColor(palette.good))
            elif self._status in ("error", "needs_link"):
                painter.fillPath(fill, QColor(palette.bad))
            elif self._status == "paused":
                painter.fillPath(fill, QColor(palette.warn))
            else:
                gradient = QLinearGradient(0.0, 0.0, float(self.width()), 0.0)
                gradient.setColorAt(0.0, QColor(palette.accent))
                gradient.setColorAt(1.0, QColor(palette.accent_2))
                painter.fillPath(fill, QBrush(gradient))
        painter.end()
