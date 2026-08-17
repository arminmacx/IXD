"""The window a click on a link in the browser opens.

IDM answers a intercepted download with one small window before anything is
transferred: the address it is about to fetch, where the file is going to land,
and three ways out — start it, start it later in a queue, or drop it. That is
what this is.

Two things it is deliberately not:

* It is **not modal to the application.** The browser hands downloads over one
  at a time and a person may click three links before looking at the screen, so
  each gets its own window rather than a second one queueing behind a first.
* It does **not open the main window.** The application is commonly in the tray
  when this arrives; raising the whole thing to ask one question is not what
  was asked for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QVBoxLayout,
)

from ...core.scheduler import format_days
from ..workers import BackgroundCall

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService


def queue_schedule_hint(service: "DownloadService", queue_id: int) -> str:
    """When, if ever, a schedule will start this queue.

    "Download later" is only an answer if something is going to come along and
    start it, so the menu says which schedule that is rather than leaving the
    download in a queue nothing runs.
    """
    windows: list[str] = []
    for schedule in service.list_schedules():
        if not schedule.enabled:
            continue
        if schedule.queue_id not in (None, queue_id):
            continue
        if schedule.action_start.value != "start":
            continue
        windows.append(
            f"{format_days(schedule.days_mask).lower()} at {schedule.start_time}")
    if not windows:
        return "no schedule starts it yet"
    return "starts " + ", ".join(windows[:2])


class BrowserDownloadDialog(QDialog):
    """Confirm, redirect or defer a download the browser handed over."""

    queued = Signal(int)

    def __init__(self, service: "DownloadService", parent, payload: dict[str, Any],
                 *, media: bool = False) -> None:
        super().__init__(parent)
        self.service = service
        self.payload = dict(payload)
        self.download_id: int | None = None
        #: A stream chosen in the page's panel, rather than a file the browser
        #: was about to fetch. The difference is that the engine has to read
        #: the page before there is anything to name or size — so this window
        #: opens on what is known, and fills in when that returns. It is opened
        #: at the moment of the click precisely *because* that read is slow:
        #: on a challenged connection it is twelve seconds (§3.51), and twelve
        #: seconds of nothing is what "it didn't work" looks like.
        self.media = media
        self._resolved = False

        self.setWindowTitle("Download file info")
        # A top-level window rather than a panel over the main one: the main
        # window is usually hidden when this arrives, and a dialog nobody can
        # find on the taskbar is a dialog that looks like nothing happened.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setMinimumWidth(720)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        columns = QHBoxLayout()
        columns.setSpacing(16)
        root.addLayout(columns, 1)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # The address is shown and never edited: it carries the session the
        # browser established — cookies, referrer, the headers the CDN decided
        # on — and a typed-over URL would keep all of that while pointing
        # somewhere it was never valid for.
        self.url_edit = QLineEdit(str(self.payload.get("url") or ""))
        self.url_edit.setReadOnly(True)
        self.url_edit.setCursorPosition(0)
        self.url_edit.setToolTip(self.url_edit.text())
        form.addRow("Address", self.url_edit)

        self.filename_edit = QLineEdit(self._initial_name())
        self.filename_edit.setPlaceholderText(
            "The stream names itself" if self.media else "Named by the server")
        form.addRow("File name", self.filename_edit)

        if self.media:
            self.quality_label = QLabel(self._quality_text())
            form.addRow("Quality", self.quality_label)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        self.folder_edit = QLineEdit(str(self.service.settings.get("download_dir")))
        self.folder_edit.setToolTip(
            "Where this one file goes. The default is the folder in Settings.")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("Save in", folder_row)

        self.info_label = QLabel(
            "Reading the stream…" if self.media
            else "Asking the server about this file…")
        self.info_label.setObjectName("Muted")
        self.info_label.setWordWrap(True)
        form.addRow("", self.info_label)

        form_holder = QVBoxLayout()
        form_holder.addLayout(form)
        form_holder.addStretch(1)
        columns.addLayout(form_holder, 1)

        buttons = QVBoxLayout()
        buttons.setSpacing(8)
        self.start_button = QPushButton("Start download")
        self.start_button.setObjectName("Primary")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self._start_now)

        self.later_button = QPushButton("Download later")
        self.later_button.setObjectName("WithMenu")
        self.later_button.setToolTip(
            "Put it in a queue, paused, and let the scheduler start it")
        self.later_menu = QMenu(self)
        self._fill_queue_menu()
        self.later_button.setMenu(self.later_menu)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        # Cancelling a stream has something to undo: the engine has already
        # made the row (paused) while this window was on screen, and leaving it
        # behind means a download nobody asked for sitting in the list.
        self.rejected.connect(self._discard_media)

        for button in (self.start_button, self.later_button, self.cancel_button):
            button.setMinimumWidth(160)
            buttons.addWidget(button)
        buttons.addStretch(1)
        columns.addLayout(buttons)

        self.ask_check = QCheckBox("Ask before every download the browser hands over")
        self.ask_check.setChecked(True)
        self.ask_check.setToolTip(
            "Unticked, downloads from the browser are queued straight away. "
            "The same switch is in Settings → Integration.")
        root.addWidget(self.ask_check)

        # Not the address: it is read-only, and opening with the caret in a
        # field nobody can type in is the window pointing at the wrong thing.
        self.start_button.setFocus()
        if self.media:
            # There is nothing to start until the engine has resolved the
            # stream, and nothing to probe: the engine is already talking to
            # the site. Both buttons come alive in `media_ready`.
            self.start_button.setEnabled(False)
            self.later_button.setEnabled(False)
            self.filename_edit.setEnabled(False)
        else:
            self._probe()

    # ------------------------------------------------------------------
    def _initial_name(self) -> str:
        """What the browser called it, or what the address suggests."""
        from ...core.http_client import filename_from_url, sanitize_filename

        supplied = str(self.payload.get("filename") or "").strip()
        if supplied:
            return sanitize_filename(supplied)
        if self.media:
            # A stream is named after its media, never its address (§3.3), and
            # the engine is the one that knows. The page's title is the best
            # thing to show in the meantime, and it is usually the answer.
            return str(self.payload.get("title") or "").strip()
        url = str(self.payload.get("url") or "")
        return filename_from_url(url) if url else ""

    def _quality_text(self) -> str:
        quality = str(self.payload.get("quality") or "").strip()
        container = str(self.payload.get("container") or "").strip()
        if self.payload.get("format_id") and not quality:
            quality = "the quality chosen in the page"
        parts = [quality or "best available"]
        if container:
            parts.append(container.lstrip("."))
        return " · ".join(parts)

    def _fill_queue_menu(self) -> None:
        queues = self.service.list_queues()
        if not queues:
            action = self.later_menu.addAction(
                "No queues — make one in Settings → Queues")
            action.setEnabled(False)
            return
        for queue in queues:
            if queue.id is None:
                continue
            action = self.later_menu.addAction(
                f"{queue.name} — {queue_schedule_hint(self.service, queue.id)}")
            action.triggered.connect(
                lambda _=False, queue_id=queue.id: self._download_later(queue_id))

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a destination folder", self.folder_edit.text())
        if directory:
            self.folder_edit.setText(directory)

    # -- what the server says about it ----------------------------------
    def _probe(self) -> None:
        url = str(self.payload.get("url") or "")
        if not url:
            self.info_label.setText("This download carries no address.")
            self.start_button.setEnabled(False)
            self.later_button.setEnabled(False)
            return
        call = BackgroundCall(lambda: self.service.probe(
            url,
            cookies=str(self.payload.get("cookies") or ""),
            user_agent=str(self.payload.get("userAgent")
                           or self.payload.get("user_agent") or ""),
            referer=str(self.payload.get("referrer")
                        or self.payload.get("referer") or ""),
            site_headers=dict(self.payload.get("headers") or {}),
        ))
        # Bound methods, so closing the window before the origin answers simply
        # drops the connection.
        call.succeeded.connect(self._on_probed)
        call.failed.connect(self._on_probe_failed)
        call.start()

    def _on_probed(self, info: dict) -> None:
        parts = [str(info.get("size_text") or "unknown size")]
        if info.get("mime"):
            parts.append(str(info["mime"]).split(";")[0])
        parts.append("resumable" if info.get("supports_ranges")
                     else "no resume support (single connection)")
        self.info_label.setText(" · ".join(parts))
        # The server names the file better than the address does — a release
        # asset behind a redirect is a UUID until `Content-Disposition` is read.
        name = str(info.get("filename") or "")
        if name and not self.filename_edit.isModified():
            self.filename_edit.setText(name)

    def _on_probe_failed(self, message: str) -> None:
        self.info_label.setText(
            f"The server did not answer a size query: {message}. "
            "It can still be downloaded.")

    # -- what the engine came back with, for a stream -------------------
    def media_ready(self, download: dict[str, Any]) -> None:
        """The engine resolved the stream; the row exists and is paused."""
        self._resolved = True
        self.download_id = int(download.get("id") or 0) or None
        name = str(download.get("filename") or "")
        if name:
            self.filename_edit.setText(name)
        self.filename_edit.setEnabled(True)
        size = int(download.get("total_size") or 0)
        parts = [f"{size / 1048576:.1f} MB" if size else "size not published"]
        companions = (self.service.mux_companions(self.download_id)
                      if self.download_id else [])
        if len(companions) > 1:
            # Said plainly, because two rows appear in the list for one file
            # and that has been reported as a bug more than once.
            parts.append("video and audio, combined into one file when both "
                         "have arrived")
        self.info_label.setText(" · ".join(parts))
        self.start_button.setEnabled(True)
        self.later_button.setEnabled(True)
        self.start_button.setFocus()

    def media_failed(self, message: str) -> None:
        """The engine could not resolve the stream at all."""
        self._resolved = True
        self.info_label.setText(message)
        self.start_button.setEnabled(False)
        self.later_button.setEnabled(False)
        self.cancel_button.setText("Close")

    def media_delegated(self) -> None:
        """The browser is fetching this one; there is nothing here to start.

        The address is refused to this application and served to the page that
        minted it (§271), so the extension reads the bytes and hands them over.
        That transfer is already under way by the time this arrives — there is
        no paused row to offer, and a window with buttons that do nothing is
        worse than one that says so.
        """
        self._resolved = True
        self.info_label.setText(
            "This stream is only served to the browser, so the browser is "
            "fetching it and handing the bytes over. It is already running — "
            "watch it in the main window.")
        self.start_button.setEnabled(False)
        self.later_button.setEnabled(False)
        self.cancel_button.setText("Close")

    def _discard_media(self) -> None:
        """Cancelled: take the paused row, and its pair, back out again."""
        if not self.media or self.download_id is None:
            return
        for row in self.service.mux_companions(self.download_id) or []:
            try:
                self.service.remove(row.id, delete_files=True)
            except Exception:  # noqa: BLE001 - nothing to escalate to here
                pass
        self.service.db.log_event("A stream was cancelled before it started.")
        self.download_id = None
        self.queued.emit(0)

    # -- the three ways out ---------------------------------------------
    def _start_now(self) -> None:
        if self.media:
            self._release_media(queue_id=None, start=True)
            return
        self._queue_it(queue_id=None, start=True)

    def _download_later(self, queue_id: int) -> None:
        if self.media:
            self._release_media(queue_id=queue_id, start=False)
            return
        self._queue_it(queue_id=queue_id, start=False)

    def _chosen_folder(self) -> str | None:
        folder = self.folder_edit.text().strip()
        if not folder:
            return ""
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.info_label.setText(f"Cannot use that folder: {exc}")
            return None
        return folder

    def _release_media(self, *, queue_id: int | None, start: bool) -> None:
        """Apply the answers to the row the engine already made, and let it go.

        Every field is written to **all** the rows that become this one file,
        because a paired quality is two of them and moving one of a pair is how
        a video ends up in one folder and its sound in another.
        """
        if self.download_id is None:
            return
        folder = self._chosen_folder()
        if folder is None:
            return

        companions = self.service.mux_companions(self.download_id) or []
        chosen_name = self.filename_edit.text().strip()
        for row in companions:
            fields: dict[str, Any] = {}
            if folder:
                fields["dest_dir"] = folder
            if queue_id is not None:
                fields["queue_id"] = queue_id
            # The name is the finished file's, so it belongs to the video half
            # of a pair — the audio keeps its own, and the muxer names the
            # output from the video. Renaming both would collide them.
            primary = (len(companions) == 1
                       or str(row.mux_group or "").endswith(":video"))
            if chosen_name and primary:
                fields["filename"] = chosen_name
            if fields:
                self.service.db.update_download_fields(row.id, **fields)

        self.service.settings.set(
            "confirm_browser_downloads", self.ask_check.isChecked())
        if start:
            for row in companions:
                self.service.resume(row.id)
        else:
            queue = self.service.db.get_queue(queue_id) if queue_id else None
            where = f"“{queue.name}”" if queue else "its queue"
            self.service.db.log_event(
                f"{chosen_name or 'The stream'} is waiting in {where} — "
                f"{queue_schedule_hint(self.service, queue_id)}",
                self.download_id)
        self.queued.emit(int(self.download_id))
        self.accept()

    def _queue_it(self, *, queue_id: int | None, start: bool) -> None:
        folder = self.folder_edit.text().strip()
        if folder:
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.info_label.setText(f"Cannot use that folder: {exc}")
                return

        payload = dict(self.payload)
        payload["filename"] = self.filename_edit.text().strip()
        payload["dest_dir"] = folder
        payload["queue_id"] = queue_id
        payload["start"] = start

        try:
            download = self.service.add_from_browser(payload)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.info_label.setText(f"Could not add the download: {exc}")
            return

        self.download_id = download.id
        self.service.settings.set(
            "confirm_browser_downloads", self.ask_check.isChecked())
        if not start:
            queue = self.service.db.get_queue(queue_id) if queue_id else None
            where = f"“{queue.name}”" if queue else "its queue"
            self.service.db.log_event(
                f"{download.filename} is waiting in {where} — "
                f"{queue_schedule_hint(self.service, queue_id)}",
                download.id)
        self.queued.emit(int(download.id))
        self.accept()
