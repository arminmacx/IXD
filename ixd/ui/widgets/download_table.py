"""The download list: table model, progress delegate and the view itself."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QRectF,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QTableView,
)

from ...core.http_client import format_bytes, format_eta, format_speed
from ...core.models import Download, DownloadStatus, HashStatus
from ..theme import DARK, Palette, status_colour

COLUMNS = ("Name", "Size", "Progress", "Speed", "ETA", "Status", "Integrity")
COL_NAME, COL_SIZE, COL_PROGRESS, COL_SPEED, COL_ETA, COL_STATUS, COL_INTEGRITY = range(7)

#: Custom roles the delegate reads.
ROLE_PROGRESS = int(Qt.ItemDataRole.UserRole) + 1
ROLE_STATUS = int(Qt.ItemDataRole.UserRole) + 2
ROLE_DOWNLOAD_ID = int(Qt.ItemDataRole.UserRole) + 3
ROLE_PROGRESS_TEXT = int(Qt.ItemDataRole.UserRole) + 4
ROLE_DETERMINATE = int(Qt.ItemDataRole.UserRole) + 5

_STATUS_LABELS = {
    DownloadStatus.QUEUED: "Queued",
    DownloadStatus.SCHEDULED: "Scheduled",
    DownloadStatus.CONNECTING: "Connecting",
    DownloadStatus.DOWNLOADING: "Downloading",
    DownloadStatus.PAUSED: "Paused",
    DownloadStatus.ASSEMBLING: "Assembling",
    DownloadStatus.VERIFYING: "Verifying",
    DownloadStatus.COMPLETED: "Completed",
    DownloadStatus.ERROR: "Error",
    DownloadStatus.NEEDS_LINK: "Link expired",
    DownloadStatus.CANCELLED: "Cancelled",
}

_INTEGRITY_LABELS = {
    HashStatus.VERIFIED: "Verified",
    HashStatus.CORRUPTED: "Corrupted",
    HashStatus.NO_REFERENCE: "Hashed",
    HashStatus.PENDING: "Checking…",
    HashStatus.UNKNOWN: "—",
}


class DownloadTableModel(QAbstractTableModel):
    """Table over a snapshot list of :class:`Download` objects."""

    def __init__(self, palette: Palette = DARK, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._rows: list[Download] = []
        self._index_by_id: dict[int, int] = {}

    # -- Qt plumbing ----------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return section + 1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        download = self._rows[index.row()]
        column = index.column()

        if role == ROLE_DOWNLOAD_ID:
            return download.id
        if role == ROLE_PROGRESS:
            return download.progress
        if role == ROLE_STATUS:
            return download.status.value
        if role == ROLE_DETERMINATE:
            return bool(download.total_size)
        if role == ROLE_PROGRESS_TEXT:
            return self._progress_text(download)

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(download, column)

        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [download.filepath or download.filename, download.url]
            if download.error:
                parts.append(f"Error: {download.error}")
            if download.computed_hash:
                parts.append(f"{download.expected_hash_algo}: {download.computed_hash}")
            return "\n".join(p for p in parts if p)

        if role == Qt.ItemDataRole.ForegroundRole:
            if column == COL_STATUS:
                return QBrush(QColor(status_colour(download.status.value, self._palette)))
            if column == COL_INTEGRITY:
                if download.hash_status is HashStatus.VERIFIED:
                    return QBrush(QColor(self._palette.good))
                if download.hash_status is HashStatus.CORRUPTED:
                    return QBrush(QColor(self._palette.bad))
            if column in (COL_SIZE, COL_SPEED, COL_ETA):
                return QBrush(QColor(self._palette.text_dim))

        if role == Qt.ItemDataRole.TextAlignmentRole and column in (
            COL_SIZE, COL_SPEED, COL_ETA
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        return None

    @staticmethod
    def _progress_text(download: Download) -> str:
        """What to print in the progress column.

        An origin that sends no ``Content-Length`` leaves the total unknown, so
        a percentage would be a fabrication — the byte count is reported
        instead, which is the honest answer and still shows movement.
        """
        if download.total_size:
            return f"{download.progress * 100:.0f}%"
        if download.downloaded:
            return format_bytes(download.downloaded)
        return "—"

    def _display(self, download: Download, column: int) -> str:
        if column == COL_NAME:
            return download.filename or download.url
        if column == COL_SIZE:
            if download.total_size:
                return format_bytes(download.total_size)
            # Size unknown: show what has arrived so far rather than nothing.
            return f"{format_bytes(download.downloaded)}+" if download.downloaded else "—"
        if column == COL_PROGRESS:
            return self._progress_text(download)
        if column == COL_SPEED:
            return format_speed(download.speed) if download.status.is_active else "—"
        if column == COL_ETA:
            return format_eta(download.eta) if download.status.is_active else "—"
        if column == COL_STATUS:
            return (download.stage
             or _STATUS_LABELS.get(download.status, download.status.value))
        if column == COL_INTEGRITY:
            return _INTEGRITY_LABELS.get(download.hash_status, "—")
        return ""

    # -- updates --------------------------------------------------------
    def set_downloads(self, downloads: list[Download]) -> None:
        """Replace the snapshot, emitting a lightweight update when possible."""
        new_ids = [d.id for d in downloads]
        old_ids = [d.id for d in self._rows]

        if new_ids == old_ids:
            # Same rows: refresh values in place so selection and scroll stay put.
            self._rows = downloads
            if downloads:
                self.dataChanged.emit(
                    self.index(0, 0),
                    self.index(len(downloads) - 1, len(COLUMNS) - 1),
                )
            return

        self.beginResetModel()
        self._rows = downloads
        self._index_by_id = {d.id: i for i, d in enumerate(downloads) if d.id is not None}
        self.endResetModel()

    def download_at(self, row: int) -> Download | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def row_for_id(self, download_id: int) -> int:
        return self._index_by_id.get(download_id, -1)

    @property
    def downloads(self) -> list[Download]:
        return self._rows


class ProgressDelegate(QStyledItemDelegate):
    """Paints the progress column as a rounded gradient bar with a label."""

    def __init__(self, palette: Palette = DARK, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        if index.column() != COL_PROGRESS:
            super().paint(painter, option, index)
            return

        progress = float(index.data(ROLE_PROGRESS) or 0.0)
        status = str(index.data(ROLE_STATUS) or "queued")
        determinate = index.data(ROLE_DETERMINATE)
        determinate = True if determinate is None else bool(determinate)
        text = str(index.data(ROLE_PROGRESS_TEXT) or f"{progress * 100:.0f}%")

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        font = painter.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1.5))
        painter.setFont(font)

        rect = QRectF(option.rect).adjusted(8, 0, -8, 0)
        height = 9.0
        # Reserve a gutter for the label so it never sits on the fill, where it
        # would be unreadable at 100%. The width follows the text because a
        # byte count ("24.8 MB") needs far more room than a percentage.
        label_width = min(rect.width() * 0.55,
                          painter.fontMetrics().horizontalAdvance(text) + 12.0)
        bar_width = max(24.0, rect.width() - label_width)
        bar = QRectF(rect.left(), rect.center().y() - height / 2, bar_width, height)
        radius = height / 2

        track = QPainterPath()
        track.addRoundedRect(bar, radius, radius)
        painter.fillPath(track, QColor(self._palette.chunk_idle))

        if determinate and progress > 0:
            fill_rect = QRectF(bar)
            fill_rect.setWidth(max(height, bar.width() * min(1.0, progress)))
            fill = QPainterPath()
            fill.addRoundedRect(fill_rect, radius, radius)

            if status == "completed":
                painter.fillPath(fill, QColor(self._palette.good))
            elif status in ("error", "needs_link"):
                painter.fillPath(fill, QColor(self._palette.bad))
            elif status == "paused":
                painter.fillPath(fill, QColor(self._palette.warn))
            else:
                gradient = QLinearGradient(bar.left(), 0.0, bar.right(), 0.0)
                gradient.setColorAt(0.0, QColor(self._palette.accent))
                gradient.setColorAt(1.0, QColor(self._palette.accent_2))
                painter.fillPath(fill, QBrush(gradient))
        elif not determinate and status in ("downloading", "connecting", "assembling"):
            # No Content-Length: a percentage would be invented, so the whole
            # track is tinted to show activity without claiming a position.
            gradient = QLinearGradient(bar.left(), 0.0, bar.right(), 0.0)
            gradient.setColorAt(0.0, QColor(self._palette.accent))
            gradient.setColorAt(0.5, QColor(self._palette.accent_2))
            gradient.setColorAt(1.0, QColor(self._palette.accent))
            painter.setOpacity(0.45)
            painter.fillPath(track, QBrush(gradient))
            painter.setOpacity(1.0)

        painter.setPen(QColor(self._palette.text_dim))
        painter.drawText(
            QRectF(bar.right(), rect.top(), label_width, rect.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            text,
        )
        painter.restore()


class DownloadFilterProxy(QSortFilterProxyModel):
    """Filters by sidebar category, status group and a text query."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.category = "All"
        self.query = ""
        self.queue_id: int | None = None

    def set_category(self, category: str) -> None:
        self.category = category
        self.invalidateFilter()

    def set_query(self, query: str) -> None:
        self.query = (query or "").strip().lower()
        self.invalidateFilter()

    def set_queue(self, queue_id: int | None) -> None:
        self.queue_id = queue_id
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, DownloadTableModel):
            return True
        download = model.download_at(source_row)
        if download is None:
            return False

        if self.queue_id is not None and download.queue_id != self.queue_id:
            return False

        category = self.category
        if category == "Active":
            if not (download.status.is_active or download.status is DownloadStatus.QUEUED):
                return False
        elif category == "Completed":
            if download.status is not DownloadStatus.COMPLETED:
                return False
        elif category == "Unfinished":
            if download.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
                return False
        elif category not in ("All", ""):
            if download.category != category:
                return False

        if self.query:
            haystack = f"{download.filename} {download.url} {download.media_title}".lower()
            if self.query not in haystack:
                return False
        return True


class DownloadTable(QTableView):
    """The configured view. Emits ``activated_download`` on double-click."""

    activated_download = Signal(int)
    context_requested = Signal(int, object)

    def __init__(self, palette: Palette = DARK, parent=None) -> None:
        super().__init__(parent)
        self._palette = palette

        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setSortingEnabled(False)
        self.setWordWrap(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(38)
        self.horizontalHeader().setHighlightSections(False)

        self.setItemDelegate(ProgressDelegate(palette, self))

        self.doubleClicked.connect(self._on_double_click)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def setModel(self, model) -> None:  # noqa: N802 - Qt naming
        """Attach the model, then size the columns.

        Header configuration only takes effect once sections exist, which is
        after a model is attached — doing it in ``__init__`` silently does
        nothing and every column stays at the default width.
        """
        super().setModel(model)
        if model is not None:
            self._configure_header()

    def _configure_header(self) -> None:
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in (
            (COL_SIZE, 95), (COL_PROGRESS, 170), (COL_SPEED, 100),
            (COL_ETA, 90), (COL_STATUS, 115), (COL_INTEGRITY, 95),
        ):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(column, width)
        # Name absorbs whatever is left, so long filenames stay readable.
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)

    def _on_double_click(self, index: QModelIndex) -> None:
        download_id = index.data(ROLE_DOWNLOAD_ID)
        if download_id is not None:
            self.activated_download.emit(int(download_id))

    def _on_context_menu(self, position) -> None:
        index = self.indexAt(position)
        download_id = index.data(ROLE_DOWNLOAD_ID) if index.isValid() else None
        self.context_requested.emit(
            int(download_id) if download_id is not None else -1,
            self.viewport().mapToGlobal(position),
        )

    def selected_ids(self) -> list[int]:
        ids: list[int] = []
        for index in self.selectionModel().selectedRows():
            value = index.data(ROLE_DOWNLOAD_ID)
            if value is not None:
                ids.append(int(value))
        return ids
