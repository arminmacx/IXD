"""One log, holding both halves of the application.

The browser extension and the engine each know half of what goes wrong, and
until now the browser's half lived only in a service-worker console that a user
has to know how to open. Diagnosing a real site by correspondence then costs a
round trip per question — which is what several sessions of this project were.

The extension reports through the ``log`` command, the engine writes here
already, and both land in one list in order. There is a button that copies the
lot, because the point of it is to be pasted.

Deliberately temporary: this exists while the browser integration is being got
right, and is meant to be removed before release.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from ..theme import own_window

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService


class LogDialog(QDialog):
    """Recent activity from the engine and from the browser, newest last."""

    REFRESH_MS = 900

    def __init__(self, service: "DownloadService", parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Log")
        own_window(self)
        self.resize(940, 560)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(QLabel(
            "Everything the engine and the browser extension have reported. "
            "Paste this when something goes wrong on a real site."
        ), 1)
        layout.addLayout(header)

        controls = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter — a word, a host, an error…")
        self.filter.textChanged.connect(self._render)
        controls.addWidget(self.filter, 1)

        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        controls.addWidget(self.follow)

        self.browser_only = QCheckBox("Browser only")
        self.browser_only.stateChanged.connect(self._render)
        controls.addWidget(self.browser_only)
        layout.addLayout(controls)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setObjectName("LogView")
        layout.addWidget(self.view, 1)

        footer = QHBoxLayout()
        self.count = QLabel("")
        footer.addWidget(self.count, 1)

        copy = QPushButton("Copy all")
        copy.clicked.connect(self._copy)
        footer.addWidget(copy)

        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear)
        footer.addWidget(clear)

        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)

        self._lines: list[str] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._reload)
        self._timer.start(self.REFRESH_MS)
        self._reload()

    # ------------------------------------------------------------------
    def _reload(self) -> None:
        # `recent_events` returns newest first; a log reads oldest first.
        rows = list(reversed(self.service.db.recent_events(600)))
        self._lines = [self._format(row) for row in rows]
        # An empty window and a switched-off log look identical, and one of
        # them is a setting the user chose. Say which this is.
        if not getattr(self.service.db, "log_enabled", True):
            self._lines.append(
                "          ----   Logging is switched off, so nothing is being "
                "recorded. Settings → General → Log → “Keep a log of what "
                "happens” turns it back on; reproduce the problem once "
                "afterwards and this window will hold it."
            )
        self._render()

    @staticmethod
    def _format(row: dict) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(float(row.get("ts") or 0)))
        level = (row.get("level") or "info").upper()[:5].ljust(5)
        download = row.get("download_id")
        target = f" #{download}" if download else ""
        return f"{stamp}  {level}{target}  {row.get('message', '')}"

    def _render(self) -> None:
        needle = self.filter.text().strip().lower()
        lines = self._lines
        if self.browser_only.isChecked():
            lines = [line for line in lines if "[browser]" in line]
        if needle:
            lines = [line for line in lines if needle in line.lower()]

        # Rewriting the document moves the caret, so where the view was looking
        # has to be put back — otherwise reading anything but the tail is
        # impossible while the timer is running.
        scrollbar = self.view.verticalScrollBar()
        at_end = self.follow.isChecked() or scrollbar.value() >= scrollbar.maximum() - 4
        position = scrollbar.value()
        self.view.setPlainText("\n".join(lines))
        scrollbar.setValue(scrollbar.maximum() if at_end else min(position, scrollbar.maximum()))
        self.count.setText(f"{len(lines)} of {len(self._lines)} lines")

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.view.toPlainText())
        self.count.setText("Copied to the clipboard")

    def _clear(self) -> None:
        self.service.db.clear_events()
        self._reload()

    def closeEvent(self, event) -> None:  # noqa: D102, N802 - Qt naming
        self._timer.stop()
        super().closeEvent(event)
