"""Live throughput graph.

A custom-painted sparkline rather than a charting dependency: it needs to
update several times a second at low cost and match the surrounding glass
aesthetic exactly.
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...core.http_client import format_bytes
from ..theme import DARK, Palette


class SpeedGraph(QWidget):
    """Scrolling area chart of recent transfer speed."""

    def __init__(self, palette: Palette = DARK, capacity: int = 120,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._samples: deque[float] = deque([0.0] * capacity, maxlen=capacity)
        self._limit = 0
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------
    def add_sample(self, bytes_per_second: float) -> None:
        self._samples.append(max(0.0, float(bytes_per_second)))
        self.update()

    def set_limit(self, bytes_per_second: int) -> None:
        self._limit = max(0, int(bytes_per_second))
        self.update()

    def clear(self) -> None:
        self._samples = deque([0.0] * self._samples.maxlen, maxlen=self._samples.maxlen)
        self.update()

    @property
    def current(self) -> float:
        return self._samples[-1] if self._samples else 0.0

    @property
    def peak(self) -> float:
        return max(self._samples) if self._samples else 0.0

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: D102, N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        palette = self._palette

        width = float(self.width())
        height = float(self.height())
        padding_top = 18.0
        padding_bottom = 6.0
        plot_height = max(1.0, height - padding_top - padding_bottom)

        peak = self.peak
        scale_max = peak * 1.25 if peak > 0 else 1.0

        # Horizontal guides.
        guide_pen = QPen(QColor(palette.border))
        guide_pen.setWidthF(1.0)
        painter.setPen(guide_pen)
        for fraction in (0.0, 0.5, 1.0):
            y = padding_top + plot_height * fraction
            painter.drawLine(QPointF(0.0, y), QPointF(width, y))

        # The configured cap, when one is active and within range.
        if self._limit and self._limit <= scale_max:
            limit_y = padding_top + plot_height * (1.0 - self._limit / scale_max)
            limit_pen = QPen(QColor(palette.warn))
            limit_pen.setStyle(Qt.PenStyle.DashLine)
            limit_pen.setWidthF(1.2)
            painter.setPen(limit_pen)
            painter.drawLine(QPointF(0.0, limit_y), QPointF(width, limit_y))

        samples = list(self._samples)
        if len(samples) >= 2:
            step = width / (len(samples) - 1)
            points = [
                QPointF(
                    index * step,
                    padding_top + plot_height * (1.0 - value / scale_max),
                )
                for index, value in enumerate(samples)
            ]

            area = QPainterPath()
            area.moveTo(QPointF(0.0, height))
            for point in points:
                area.lineTo(point)
            area.lineTo(QPointF(width, height))
            area.closeSubpath()

            gradient = QLinearGradient(0.0, padding_top, 0.0, height)
            top_colour = QColor(palette.accent)
            top_colour.setAlpha(120)
            bottom_colour = QColor(palette.accent)
            bottom_colour.setAlpha(8)
            gradient.setColorAt(0.0, top_colour)
            gradient.setColorAt(1.0, bottom_colour)
            painter.fillPath(area, QBrush(gradient))

            line = QPainterPath()
            line.moveTo(points[0])
            for point in points[1:]:
                line.lineTo(point)
            line_pen = QPen(QColor(palette.accent))
            line_pen.setWidthF(1.8)
            line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(line_pen)
            painter.drawPath(line)

        # Readout.
        font = QFont(self.font())
        font.setPointSizeF(max(8.5, font.pointSizeF() - 0.5))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(palette.text))
        painter.drawText(
            QRectF(2.0, 0.0, width - 4.0, padding_top),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{format_bytes(self.current)}/s",
        )
        painter.setPen(QColor(palette.text_faint))
        painter.drawText(
            QRectF(2.0, 0.0, width - 4.0, padding_top),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"peak {format_bytes(peak)}/s",
        )
        painter.end()
