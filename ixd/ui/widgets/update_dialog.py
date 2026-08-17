"""What a newer version is, and what happens if you take it.

Deliberately not a wizard. It says which version, what changed, and offers the
one action this build is actually able to perform — install it, or open the
page it is published on. A build installed from a package must not pretend it
can replace itself; that is decided by :func:`ixd.updates.self_update_kind`
and shown here rather than discovered halfway through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ... import __version__
from ... import updates
from ..workers import Worker

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService


class UpdateDialog(QDialog):
    """Shows a published release and, where possible, installs it."""

    def __init__(self, service: "DownloadService", payload: dict | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.payload = dict(payload or {})
        self._worker: Worker | None = None
        self._release: updates.Release | None = None

        self.setWindowTitle("Update")
        self.setMinimumSize(560, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        self.heading = QLabel()
        self.heading.setObjectName("Title")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.subheading = QLabel()
        self.subheading.setObjectName("Subtle")
        self.subheading.setWordWrap(True)
        layout.addWidget(self.subheading)

        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        layout.addWidget(self.notes, 1)

        # The half people forget: the extension in the browser is a *copy*, and
        # a new application does not reload it.
        self.extension_note = QLabel(
            "The browser extension is written out again when the new version "
            "starts. Reload it from your browser's extensions page afterwards "
            "so the browser runs the new one."
        )
        self.extension_note.setObjectName("Subtle")
        self.extension_note.setWordWrap(True)
        layout.addWidget(self.extension_note)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        row = QHBoxLayout()
        self.check_button = QPushButton("Check again")
        self.check_button.clicked.connect(lambda: self.refresh(force=True))
        row.addWidget(self.check_button)
        row.addStretch(1)

        self.page_button = QPushButton("Open the download page")
        self.page_button.clicked.connect(self._open_page)
        row.addWidget(self.page_button)

        self.install_button = QPushButton("Download and install")
        self.install_button.setObjectName("Primary")
        self.install_button.clicked.connect(self._install)
        row.addWidget(self.install_button)
        layout.addLayout(row)

        closing = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        closing.rejected.connect(self.reject)
        layout.addWidget(closing)

        if self.payload.get("version"):
            self._show_release(self.payload)
        else:
            self.refresh(force=True)

    # ------------------------------------------------------------------
    def _show_release(self, payload: dict) -> None:
        version = str(payload.get("version") or "")
        self.heading.setText(
            f"Version {version} is available" if version
            else f"Version {__version__} is the newest published version"
        )
        kind = updates.self_update_kind()
        self.subheading.setText(
            f"You are running {__version__}."
            + ("" if not version else
               "  This build was installed, so the new installer is downloaded "
               "and run — it upgrades in place and keeps your shortcuts."
               if kind == "installer" else
               "  This build can install it for you."
               if kind else
               "  This build was installed from a package, so the new version "
               "is downloaded from its release page.")
        )
        notes = str(payload.get("notes") or "").strip()
        self.notes.setMarkdown(notes or "No release notes were published.")
        self.install_button.setEnabled(bool(version) and bool(kind))
        self.install_button.setVisible(bool(kind))
        self.page_button.setEnabled(True)
        self.extension_note.setVisible(bool(version))

    def refresh(self, force: bool = False) -> None:
        self.check_button.setEnabled(False)
        self.heading.setText("Checking…")
        self.subheading.setText("")

        def work() -> dict:
            release = self.service.check_for_updates(force=force)
            if release is None:
                return {}
            self._release = release
            return {
                "version": release.version,
                "notes": release.notes,
                "page_url": release.page_url,
            }

        self._worker = Worker(work)
        self._worker.succeeded.connect(self._checked)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _checked(self, payload: dict) -> None:
        self.check_button.setEnabled(True)
        self.payload = dict(payload or {})
        self._show_release(self.payload)

    def _failed(self, message: str) -> None:
        self.check_button.setEnabled(True)
        self.heading.setText("The check did not finish")
        self.subheading.setText(str(message))
        self.notes.setMarkdown(
            "The release feed could not be read. This is usually a network or "
            "proxy problem rather than anything about the update itself."
        )
        self.install_button.setEnabled(False)

    # ------------------------------------------------------------------
    def _open_page(self) -> None:
        QDesktopServices.openUrl(QUrl(
            str(self.payload.get("page_url") or updates.DEFAULT_PAGE)))

    def _install(self) -> None:
        release = self._release
        if release is None:
            # Reached from the status-bar notice, where only the summary
            # travelled with the event: ask the feed again for the assets.
            try:
                release = self.service.check_for_updates(force=True)
            except Exception as error:      # noqa: BLE001
                QMessageBox.warning(self, "Update", str(error))
                return
        if release is None:
            QMessageBox.information(self, "Update", "There is nothing newer.")
            return

        by_installer = updates.self_update_kind() == "installer"
        confirmed = QMessageBox.question(
            self, "Install the update",
            (f"Version {release.version} will be downloaded and its installer "
             "will open — click through it as you did the first time.\n\n"
             if by_installer else
             f"Version {release.version} will be downloaded and this "
             "application will restart into it.\n\n")
            + "Downloads in progress are "
            "paused and resume afterwards. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return

        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.install_button.setEnabled(False)
        self.check_button.setEnabled(False)

        def report(done: int, total: int) -> None:
            if total:
                self.progress.setValue(int(done * 100 / total))

        def work() -> tuple[bool, str]:
            return self.service.install_update(release, report)

        self._worker = Worker(work)
        self._worker.succeeded.connect(self._installed)
        self._worker.failed.connect(self._failed)
        self._worker.start()

    def _installed(self, outcome: tuple) -> None:
        started, detail = outcome
        self.progress.setVisible(False)
        if not started:
            self.install_button.setEnabled(True)
            self.check_button.setEnabled(True)
            QMessageBox.warning(self, "Update", detail)
            return

        # No box to dismiss. The updater is already running and waiting for
        # this process to end; asking somebody to click OK so that it can is
        # a step that exists only to be in the way. What is shown instead is
        # what is about to happen, for the second or two it takes.
        self.heading.setText(f"Version {detail} is ready")
        self.subheading.setText(
            "Closing now — the installer is opening, and it needs this copy "
            "closed before it can replace the files. Remember to reload the "
            "extension in your browser afterwards."
            if updates.self_update_kind() == "installer" else
            "Closing now — the updater replaces this version and starts it "
            "again by itself. Remember to reload the extension in your "
            "browser afterwards."
        )
        self.notes.setVisible(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.install_button.setVisible(False)
        self.page_button.setVisible(False)
        self.check_button.setVisible(False)

        # Out of the way first, then gone. The updater's own window is the
        # only thing that should be on screen while the folders are swapped,
        # and it cannot start until this process has ended — so leaving is
        # not optional here, which is what `force_after` guarantees.
        window = self.parent()
        QTimer.singleShot(900, self.accept)
        if window is not None and hasattr(window, "quit_application"):
            QTimer.singleShot(1000, lambda: window.hide())
            QTimer.singleShot(1200, lambda: window.quit_application(force_after=4.0))
