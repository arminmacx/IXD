"""Download properties: live chunk view, integrity verification and link swap."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.http_client import format_bytes, format_eta, format_speed
from ...core.models import DownloadStatus, HashStatus
from ..theme import DARK, Palette, own_window, status_colour
from .chunk_bars import ChunkBars, SegmentedProgress

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService


class PropertiesDialog(QDialog):
    """Everything about one download, refreshed live while it runs."""

    def __init__(self, service: "DownloadService", download_id: int,
                 palette: Palette = DARK, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.download_id = download_id
        self._palette = palette

        self.setWindowTitle("Download properties")
        own_window(self)
        self.setMinimumSize(660, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        self.title_label = QLabel("—")
        self.title_label.setObjectName("DetailTitle")
        self.title_label.setWordWrap(True)
        layout.addWidget(self.title_label)

        self.progress = SegmentedProgress(palette)
        layout.addWidget(self.progress)

        self.summary_label = QLabel("—")
        self.summary_label.setObjectName("Muted")
        layout.addWidget(self.summary_label)

        tabs = QTabWidget()
        tabs.addTab(self._build_general_tab(), "General")
        tabs.addTab(self._build_integrity_tab(), "Integrity")
        tabs.addTab(self._build_source_tab(), "Source")
        tabs.addTab(self._build_log_tab(), "Log")
        layout.addWidget(tabs, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.clicked.connect(self._open_folder)
        close_button = QPushButton("Close")
        close_button.setObjectName("Primary")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(self.open_folder_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(700)
        self.refresh()

    # ------------------------------------------------------------------
    def _build_general_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(8)
        self.value_labels: dict[str, QLabel] = {}
        for key, label in (
            ("status", "Status"), ("size", "Size"), ("downloaded", "Downloaded"),
            ("speed", "Speed"), ("eta", "Time left"), ("mode", "Transfer mode"),
            # Two different facts that were being read as one. Range support is
            # about this URL; resume is about whether the bytes already here
            # survive a pause, which for a segmented or server-driven stream is
            # true whatever the URL does with a Range header.
            ("ranges", "Range support"), ("resume", "Resume"),
            ("path", "Saved to"), ("queue", "Queue"),
        ):
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.value_labels[key] = value
            form.addRow(label, value)
        layout.addLayout(form)

        chunk_box = QGroupBox("Connections")
        chunk_layout = QVBoxLayout(chunk_box)
        self.chunk_bars = ChunkBars(self._palette)
        chunk_layout.addWidget(self.chunk_bars)
        layout.addWidget(chunk_box)
        layout.addStretch(1)
        return page

    def _build_integrity_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        self.integrity_banner = QLabel("Not verified")
        self.integrity_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.integrity_banner.setMinimumHeight(52)
        layout.addWidget(self.integrity_banner)

        form = QFormLayout()
        form.setSpacing(8)

        expected_row = QHBoxLayout()
        self.expected_edit = QLineEdit()
        self.expected_edit.setPlaceholderText(
            "Paste the checksum published by the developer"
        )
        self.algo_combo = QComboBox()
        self.algo_combo.addItems(["auto", "md5", "sha1", "sha256", "sha512"])
        expected_row.addWidget(self.expected_edit, 1)
        expected_row.addWidget(self.algo_combo)
        form.addRow("Expected", expected_row)

        self.computed_label = QLabel("—")
        self.computed_label.setWordWrap(True)
        self.computed_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow("Computed", self.computed_label)

        self.server_digest_label = QLabel("—")
        self.server_digest_label.setWordWrap(True)
        form.addRow("Server digest", self.server_digest_label)
        layout.addLayout(form)

        actions = QHBoxLayout()
        save_button = QPushButton("Save expected hash")
        save_button.clicked.connect(self._save_hash)
        verify_button = QPushButton("Verify now")
        verify_button.setObjectName("Primary")
        verify_button.clicked.connect(self._verify_now)
        actions.addWidget(save_button)
        actions.addWidget(verify_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.integrity_note = QLabel("")
        self.integrity_note.setObjectName("Muted")
        self.integrity_note.setWordWrap(True)
        layout.addWidget(self.integrity_note)
        layout.addStretch(1)
        return page

    def _build_source_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(8)
        self.url_label = QLabel("—")
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Current URL", self.url_label)

        self.original_label = QLabel("—")
        self.original_label.setWordWrap(True)
        form.addRow("Original URL", self.original_label)
        layout.addLayout(form)

        swap_box = QGroupBox("Refresh an expired link")
        swap_layout = QVBoxLayout(swap_box)
        explanation = QLabel(
            "When a source link expires the download pauses instead of failing. "
            "Paste a freshly generated link below — the completed byte ranges are "
            "kept and only the missing parts are fetched from the new source."
        )
        explanation.setObjectName("Muted")
        explanation.setWordWrap(True)
        swap_layout.addWidget(explanation)

        swap_row = QHBoxLayout()
        self.swap_edit = QLineEdit()
        self.swap_edit.setPlaceholderText("https://… refreshed source link")
        swap_button = QPushButton("Swap and resume")
        swap_button.setObjectName("Primary")
        swap_button.clicked.connect(self._swap_link)
        swap_row.addWidget(self.swap_edit, 1)
        swap_row.addWidget(swap_button)
        swap_layout.addLayout(swap_row)

        self.swap_note = QLabel("")
        self.swap_note.setObjectName("Muted")
        self.swap_note.setWordWrap(True)
        swap_layout.addWidget(self.swap_note)
        layout.addWidget(swap_box)
        layout.addStretch(1)
        return page

    def _build_log_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        return page

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        download = self.service.get_download(self.download_id)
        if download is None:
            self._timer.stop()
            return

        self.title_label.setText(download.filename or download.url)
        # A complete but corrupted file should not read as a green success.
        bar_status = (
            "error" if download.hash_status is HashStatus.CORRUPTED
            else download.status.value
        )
        self.progress.set_value(download.progress, bar_status)

        colour = status_colour(download.status.value, self._palette)
        self.summary_label.setText(
            f"{format_bytes(download.downloaded)} of "
            f"{format_bytes(download.total_size) if download.total_size else 'unknown'}"
            f"  ·  {download.progress * 100:.1f}%"
        )

        labels = self.value_labels
        labels["status"].setText(download.status.value.replace("_", " ").title())
        labels["status"].setStyleSheet(f"color: {colour};")
        labels["size"].setText(
            format_bytes(download.total_size) if download.total_size else "unknown"
        )
        labels["downloaded"].setText(format_bytes(download.downloaded))
        labels["speed"].setText(
            format_speed(download.speed) if download.status.is_active else "—"
        )
        labels["eta"].setText(
            format_eta(download.eta) if download.status.is_active else "—"
        )
        labels["mode"].setText(download.mode.value)
        labels["ranges"].setText("yes" if download.supports_ranges else "no")
        labels["resume"].setText(download.resume_note)
        labels["path"].setText(download.filepath or "—")
        queue = self.service.db.get_queue(download.queue_id) if download.queue_id else None
        labels["queue"].setText(queue.name if queue else "—")

        self.chunk_bars.set_chunks([
            {"progress": chunk.progress, "status": chunk.status.value}
            for chunk in (download.display_chunks or [])
        ])

        if not self.expected_edit.hasFocus():
            self.expected_edit.setText(download.expected_hash)
        self.computed_label.setText(download.computed_hash or "—")
        self.server_digest_label.setText(download.server_digest or "none advertised")
        self._render_integrity(download.hash_status)

        self.url_label.setText(download.url)
        self.original_label.setText(download.original_url or download.url)
        if download.status is DownloadStatus.NEEDS_LINK:
            self.swap_note.setText(
                f"This download is waiting for a refreshed link. {download.error}"
            )
        elif download.error:
            self.swap_note.setText(download.error)

        events = self.service.db.recent_events(120, self.download_id)
        text = "\n".join(
            f"[{event['level']}] {event['message']}" for event in reversed(events)
        )
        if text != self.log_view.toPlainText():
            scrollbar = self.log_view.verticalScrollBar()
            at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
            self.log_view.setPlainText(text)
            if at_bottom:
                scrollbar.setValue(scrollbar.maximum())

        self.open_folder_button.setEnabled(bool(download.dest_dir))

    def _render_integrity(self, status: HashStatus) -> None:
        palette = self._palette
        text, colour, background = {
            HashStatus.VERIFIED: (
                "✓  Verified — the file matches the expected checksum",
                palette.good, "rgba(67, 214, 160, 0.14)",
            ),
            HashStatus.CORRUPTED: (
                "✕  Corrupted — the checksum does not match",
                palette.bad, "rgba(255, 108, 122, 0.14)",
            ),
            HashStatus.NO_REFERENCE: (
                "•  Hashed, but there was nothing to compare against",
                palette.text_dim, "rgba(255, 255, 255, 0.05)",
            ),
            HashStatus.PENDING: (
                "…  Verification in progress",
                palette.warn, "rgba(255, 191, 94, 0.14)",
            ),
        }.get(status, (
            "•  Not verified",
            palette.text_dim, "rgba(255, 255, 255, 0.05)",
        ))

        self.integrity_banner.setText(text)
        self.integrity_banner.setStyleSheet(
            f"color: {colour}; background: {background};"
            f" border: 1px solid {colour}; border-radius: 12px;"
            " font-size: 14px; font-weight: 600; padding: 12px;"
        )

    # ------------------------------------------------------------------
    def _save_hash(self) -> None:
        algorithm = self.algo_combo.currentText()
        self.service.set_expected_hash(
            self.download_id,
            self.expected_edit.text().strip(),
            "" if algorithm == "auto" else algorithm,
        )
        self.integrity_note.setText(
            "Expected hash saved. Use “Verify now” to check the file on disk."
        )
        self.refresh()

    def _verify_now(self) -> None:
        download = self.service.get_download(self.download_id)
        if download is None or not os.path.isfile(download.filepath):
            self.integrity_note.setText(
                "The finished file is not on disk yet — verification runs automatically "
                "once the download completes."
            )
            return
        self._save_hash()
        self.service.reverify(self.download_id)
        self.integrity_note.setText("Hashing the file…")

    def _swap_link(self) -> None:
        url = self.swap_edit.text().strip()
        if not url:
            self.swap_note.setText("Paste the refreshed link first.")
            return
        try:
            self.service.swap_link(self.download_id, url)
        except Exception as exc:  # noqa: BLE001 - shown inline
            self.swap_note.setText(f"Could not swap the link: {exc}")
            return
        self.swap_edit.clear()
        self.swap_note.setText("Link swapped — resuming from the existing progress.")
        self.refresh()

    def _open_folder(self) -> None:
        download = self.service.get_download(self.download_id)
        if download is None or not download.dest_dir:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(download.dest_dir))

    def closeEvent(self, event) -> None:  # noqa: D102, N802 - Qt naming
        self._timer.stop()
        super().closeEvent(event)
