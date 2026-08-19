"""Add-download dialog with inline media analysis."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..theme import own_window

from ...core.models import MediaInfo
from ..workers import BackgroundCall

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService


class AddDownloadDialog(QDialog):
    """Collects a URL (plus optional media format) and queues the download."""

    def __init__(self, service: "DownloadService", parent=None,
                 initial_url: str = "") -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Add download")
        own_window(self)
        self.setMinimumWidth(600)

        self._media: MediaInfo | None = None
        self._probe_info: dict | None = None
        self._probe_summary: str = ""
        #: The address the call in flight is about. Kept as state rather than
        #: captured in a lambda: a connection made through a lambda has no
        #: receiving object, so Qt cannot drop it when this window closes.
        self._pending_url: str = ""
        self._worker: BackgroundCall | None = None
        self.created_ids: list[int] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        url_row = QHBoxLayout()
        self.url_edit = QLineEdit(initial_url)
        self.url_edit.setPlaceholderText("https://example.com/file.zip or a video page URL")
        self.analyze_button = QPushButton("Analyse")
        self.analyze_button.setToolTip(
            "Inspect the page for downloadable video and audio streams"
        )
        self.analyze_button.clicked.connect(self._analyze)
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.analyze_button)
        form.addRow("URL", url_row)

        self.format_combo = QComboBox()
        self.format_combo.setEnabled(False)
        self.format_combo.addItem("Direct file download (no analysis)", "")
        form.addRow("Stream", self.format_combo)

        self.filename_edit = QLineEdit()
        self.filename_edit.setPlaceholderText("Detected automatically")
        form.addRow("Save as", self.filename_edit)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(str(self.service.settings.get("download_dir")))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("Folder", folder_row)

        self.queue_combo = QComboBox()
        for queue in self.service.list_queues():
            self.queue_combo.addItem(queue.name, queue.id)
        form.addRow("Queue", self.queue_combo)

        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, self.service.settings.get_int(
            "max_connections_per_download", 32))
        self.connections_spin.setValue(
            self.service.settings.get_int("connections_per_download", 8)
        )
        form.addRow("Connections", self.connections_spin)

        hash_row = QHBoxLayout()
        self.hash_edit = QLineEdit()
        self.hash_edit.setPlaceholderText("Optional: paste a checksum from the release page")
        self.hash_algo = QComboBox()
        self.hash_algo.addItems(["auto", "md5", "sha1", "sha256", "sha512"])
        hash_row.addWidget(self.hash_edit, 1)
        hash_row.addWidget(self.hash_algo)
        form.addRow("Verify hash", hash_row)

        layout.addLayout(form)

        self.start_check = QCheckBox("Start immediately")
        self.start_check.setChecked(True)
        layout.addWidget(self.start_check)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("Add")
        ok_button.setObjectName("Primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.url_edit.setFocus()

    # ------------------------------------------------------------------
    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a destination folder", self.folder_edit.text()
        )
        if directory:
            self.folder_edit.setText(directory)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.analyze_button.setEnabled(not busy)
        self.analyze_button.setText("Analysing…" if busy else "Analyse")
        self.status_label.setText(message)

    def _analyze(self) -> None:
        """Inspect the URL: what the file is, then what streams it offers.

        The probe runs first and on its own. It is a HEAD (or a one-byte
        ranged GET) and answers the question the user actually asked — how big
        is it, what is it called, can it be resumed — without transferring
        anything. Stream enumeration is a separate, slower step that only some
        URLs need, so its result is reported when it arrives instead of holding
        the size hostage until it does.
        """
        url = self.url_edit.text().strip()
        if not url:
            self.status_label.setText("Enter a URL first.")
            return

        self._probe_info = None
        self._media = None
        self._pending_url = url
        self._set_busy(True, "Contacting the server…")
        worker = BackgroundCall(lambda: self.service.probe(url))
        worker.succeeded.connect(self._probe_answered)
        worker.failed.connect(self._probe_refused)
        self._worker = worker
        worker.start()

    def _probe_answered(self, info: dict) -> None:
        self._on_probed(info, self._pending_url)

    def _probe_refused(self, message: str) -> None:
        self._on_probe_failed(message, self._pending_url)

    def _on_probed(self, info: dict, url: str) -> None:
        self._probe_info = info

        size = int(info.get("size") or 0)
        parts = [info.get("size_text") or "unknown size"]
        if info.get("mime"):
            parts.append(str(info["mime"]).split(";")[0])
        parts.append("resumable" if info.get("supports_ranges")
                     else "no resume support (single connection)")
        self._probe_summary = " · ".join(parts)

        if info.get("filename") and not self.filename_edit.text():
            self.filename_edit.setPlaceholderText(str(info["filename"]))

        # An origin that cannot resume gains nothing from extra connections.
        if not info.get("supports_ranges"):
            self.connections_spin.setValue(1)
            self.connections_spin.setEnabled(False)
        else:
            self.connections_spin.setEnabled(True)

        self._set_busy(True, f"{self._probe_summary}\nLooking for media streams…")
        self._start_extraction(url, size)

    def _on_probe_failed(self, message: str, url: str) -> None:
        self._probe_summary = f"The server did not answer a size query: {message}"
        self._set_busy(True, f"{self._probe_summary}\nLooking for media streams…")
        self._start_extraction(url, 0)

    def _start_extraction(self, url: str, size: int) -> None:
        worker = BackgroundCall(lambda: self.service.extract(url))
        worker.succeeded.connect(self._on_analyzed)
        worker.failed.connect(self._on_analyze_failed)
        self._worker = worker
        worker.start()

    def _on_analyzed(self, info: MediaInfo) -> None:
        # A "direct link" result is the generic extractor telling us this is a
        # plain file, not a media page. Treat it as such: no stream picker.
        single_direct = (
            len(info.formats) == 1
            and info.formats[0].format_id == "direct"
            and info.extractor == "generic"
        )
        if single_direct:
            self._media = None
            self.format_combo.setEnabled(False)
            self._set_busy(False, self._probe_summary or "Ready to download.")
            return

        self._media = info
        self.format_combo.clear()
        self.format_combo.setEnabled(True)
        self.format_combo.addItem("Best available", "")
        for media_format in self.service.presentable_formats(
                info.formats,
                self.service.settings.get("preferred_video_container", "mp4")):
            # What a person picks here is a quality, so that is what the entry
            # says: the resolution, the container, and how big the finished
            # file will be. Which stream or client it came from is the
            # application's business, not the viewer's.
            choice = self.service._describe_choice(media_format, info.formats)
            label = str(choice.get("description") or "")
            if media_format.has_video and media_format.ext:
                label += f" · {media_format.ext}"
            size = int(choice.get("filesize") or 0)
            if size:
                label += f" · {size / 1048576:.1f} MB"
            if not choice.get("complete"):
                label += "  (first minute only)"
            self.format_combo.addItem(label, media_format.format_id)

        if info.title and not self.filename_edit.text():
            self.filename_edit.setPlaceholderText(info.title)
        self._set_busy(
            False,
            f"Found {len(info.formats)} stream(s) via the “{info.extractor}” extractor.",
        )

    def _on_analyze_failed(self, message: str) -> None:
        self._media = None
        self.format_combo.setEnabled(False)
        if self._probe_info:
            # The probe already told us everything a direct download needs.
            self._set_busy(False, f"{self._probe_summary}\nNo media streams here — "
                                  "it will be added as a direct file.")
        else:
            self._set_busy(
                False,
                f"Analysis failed: {message}\nThe URL can still be added as a direct file.",
            )

    # ------------------------------------------------------------------
    def _chosen_connections(self) -> int:
        """The count to store: zero when it was simply left at the default.

        The spinner opens on the current setting, so passing its value always
        stamped a number onto the download and froze it — raising "connections
        per download" afterwards then changed nothing for anything already in
        the list, which is exactly where a person expects to change it.

        Zero means "follow the setting". A value the user actually moved to is
        kept, because they asked for it; one that is still sitting on the
        default is not a choice, it is the default.
        """
        chosen = self.connections_spin.value()
        default = self.service.settings.get_int("connections_per_download", 8)
        if chosen == default and self.connections_spin.isEnabled():
            return 0
        return chosen

    def _accept(self) -> None:
        url = self.url_edit.text().strip()
        if not url:
            self.status_label.setText("Enter a URL first.")
            return

        folder = self.folder_edit.text().strip()
        if folder:
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.status_label.setText(f"Cannot use that folder: {exc}")
                return

        algorithm = self.hash_algo.currentText()
        expected_hash = self.hash_edit.text().strip()

        try:
            if self._media is not None:
                download = self.service.add_media(
                    url,
                    self.format_combo.currentData() or "",
                    queue_id=self.queue_combo.currentData(),
                    dest_dir=folder,
                    start=self.start_check.isChecked(),
                )
            else:
                download = self.service.add_url(
                    url,
                    filename=self.filename_edit.text().strip(),
                    dest_dir=folder,
                    queue_id=self.queue_combo.currentData(),
                    connections=self._chosen_connections(),
                    expected_hash=expected_hash,
                    expected_hash_algo="" if algorithm == "auto" else algorithm,
                    start=self.start_check.isChecked(),
                )

            if expected_hash and self._media is not None:
                self.service.set_expected_hash(
                    download.id, expected_hash,
                    "" if algorithm == "auto" else algorithm,
                )
            self.created_ids.append(download.id)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.status_label.setText(f"Could not add the download: {exc}")
            return

        self.accept()
