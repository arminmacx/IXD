"""The window that watches one download, in the shape people expect.

A download manager is judged partly on this window: the file, how fast it is
arriving, how long is left, what each connection is doing, and a Pause that is
right there. The properties dialog already held all of it, but it is modal —
it stops the rest of the application while it is open — and modal is exactly
wrong for something you leave on screen while a file arrives.

So this is not modal, one per download, and it remembers where it was put.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.models import DownloadStatus
from ..theme import DARK, Palette
from .chunk_bars import ChunkBars
from .speed_graph import SpeedGraph

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService


def _size(value: float | int | None) -> str:
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:,.0f} {unit}" if unit == "B" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return f"{amount:,.1f} TB"


def _duration(seconds: float | int | None) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return "—"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class DownloadWindow(QDialog):
    """Live progress for a single download: rate, time left, connections."""

    #: One window per download, so activating the same row twice raises the
    #: window that is already open instead of stacking a second copy on it.
    _open: "dict[int, DownloadWindow]" = {}

    @classmethod
    def show_for(cls, service: "DownloadService", download_id: int,
                 palette: Palette = DARK, parent=None) -> "DownloadWindow":
        """Open — or raise — this download's own window.

        ``parent`` is deliberately **not** passed on. A parented window is a
        child of the main window: Windows gives it no taskbar button of its
        own, and if the parent is hidden — which it is when the browser started
        the application, or when it has been closed to the tray — the child
        does not appear at all. Reported exactly that way: the icon appeared on
        the taskbar, no window did, and clicking the icon to raise the main
        window finally brought the download window with it.

        A download's window outlives whatever opened it, so it is top-level and
        carries the application's icon itself rather than inheriting one.
        """
        existing = cls._open.get(download_id)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return existing
        window = cls(service, download_id, palette, parent=None)
        cls._open[download_id] = window
        window.show()
        # Shown, then brought forward: `show()` alone leaves it behind whatever
        # had focus, which on a busy desktop is indistinguishable from it never
        # having opened.
        window.raise_()
        window.activateWindow()
        return window

    def __init__(self, service: "DownloadService", download_id: int,
                 palette: Palette = DARK, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.download_id = download_id
        self._palette = palette
        # Not modal: this is meant to be left open while the file arrives, and
        # a modal window would hold the rest of the application still.
        self.setModal(False)
        # A real top-level window, so the desktop lists it in its own right.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        # Without a parent there is nothing to inherit an icon from, and a
        # window with no icon is the one thing a taskbar cannot label.
        from .tray import application_icon      # noqa: PLC0415 - avoids a cycle
        self.setWindowIcon(application_icon())
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)

        self.name_label = QLabel()
        self.name_label.setObjectName("Heading")
        self.name_label.setWordWrap(True)
        layout.addWidget(self.name_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        facts = QFormLayout()
        facts.setSpacing(6)
        self.status_value = QLabel("—")
        self.speed_value = QLabel("—")
        self.left_value = QLabel("—")
        self.done_value = QLabel("—")
        self.resume_value = QLabel("—")
        facts.addRow("Status", self.status_value)
        facts.addRow("Transfer rate", self.speed_value)
        facts.addRow("Time left", self.left_value)
        facts.addRow("Downloaded", self.done_value)
        facts.addRow("Resume capability", self.resume_value)
        layout.addLayout(facts)

        self.graph = SpeedGraph(palette, parent=self)
        layout.addWidget(self.graph)

        connections = QLabel("Connections")
        connections.setObjectName("Subtle")
        layout.addWidget(connections)
        self.chunk_bars = ChunkBars(palette, parent=self)
        layout.addWidget(self.chunk_bars)

        self.close_when_done = QCheckBox("Close this window when the download "
                                         "finishes")
        self.close_when_done.setChecked(True)
        layout.addWidget(self.close_when_done)

        buttons = QHBoxLayout()
        self.pause_button = QPushButton("Pause")
        self.pause_button.clicked.connect(self._toggle_pause)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._cancel)
        self.folder_button = QPushButton("Open folder")
        self.folder_button.clicked.connect(self._open_folder)
        close_button = QPushButton("Close")
        close_button.setObjectName("Primary")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.folder_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(500)
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        download = self.service.get_download(self.download_id)
        if download is None:
            self.close()
            return

        self.setWindowTitle(download.filename or "Download")
        self.name_label.setText(download.filename or download.url)

        total = int(download.total_size or 0)
        done = int(download.downloaded or 0)
        if total > 0:
            ratio = max(0.0, min(1.0, done / total))
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(ratio * 1000))
            self.progress.setFormat(f"{ratio * 100:.1f}%")
            self.done_value.setText(f"{_size(done)} of {_size(total)}")
        else:
            # A stream whose length nobody published: a bar that cannot say how
            # far along it is should not pretend to.
            self.progress.setRange(0, 0)
            self.done_value.setText(_size(done))

        stage = (download.stage or "").strip()
        self.status_value.setText(stage or download.status.value.title())
        self.speed_value.setText(
            f"{_size(download.speed)}/s" if download.speed else "—")
        self.left_value.setText(_duration(download.eta))
        # `supports_ranges` is about one URL answering a Range request, which a
        # YouTube stream never does and never needs to — it said "No" over a
        # download that resumes perfectly well. `resume_note` answers the
        # question that was actually being asked.
        self.resume_value.setText(download.resume_note)

        self.graph.add_sample(float(download.speed or 0))
        self.chunk_bars.set_chunks([
            {"progress": chunk.progress, "status": chunk.status.value}
            for chunk in (download.display_chunks or [])
        ])

        active = download.status.is_active
        self.pause_button.setText("Resume" if not active else "Pause")
        self.pause_button.setEnabled(
            download.status is not DownloadStatus.COMPLETED)
        self.cancel_button.setEnabled(
            download.status is not DownloadStatus.COMPLETED)

        if download.status is DownloadStatus.COMPLETED:
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000)
            self.progress.setFormat("100%")
            self.status_value.setText("Completed")
            if self.close_when_done.isChecked():
                self.close()

    # ------------------------------------------------------------------
    def _toggle_pause(self) -> None:
        download = self.service.get_download(self.download_id)
        if download is None:
            return
        if download.status.is_active:
            self.service.pause(self.download_id)
        else:
            self.service.resume(self.download_id)
        self.refresh()

    def _cancel(self) -> None:
        # Cancelling ends the download, and this window is the download's.
        # Leaving it open on a cancelled transfer — which is what it used to
        # do, sitting there reading "Cancelled" — asks the user to close the
        # same thing twice. The row stays in the main list, which is where a
        # cancelled download belongs.
        self.service.cancel(self.download_id)
        self.close()

    def _open_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        download = self.service.get_download(self.download_id)
        if download is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(download.dest_dir))

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        self._timer.stop()
        DownloadWindow._open.pop(self.download_id, None)
        super().closeEvent(event)
