"""The updater's own small window.

Run by the *staged* build, in its own process, while the application it is
replacing shuts down. It exists because the alternative is an application that
vanishes for a few seconds and comes back: on a slow disk that is long enough
to look like a crash, and on Windows — where the build has no console — there
is nowhere for a message to go at all.

Deliberately tiny. No service, no database, no engine: this process must be
able to run while the application's own files are being moved out from under
it, so it touches nothing but the folders it is swapping.

If there is no display — a server, an SSH session, `--background` on a machine
with no session — it does the same work with no window and prints the outcome.
The swap is what matters; the window is only so that somebody watching sees
what is happening.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from . import __version__, updates


def _headless() -> bool:
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def build_window():
    """The window and the labels that change, built once and testable.

    Separated from :func:`run` so a test — and a screenshot — can see the
    thing a user sees without waiting for a process to exit around it.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

    window = QWidget()
    window.setWindowTitle("Internet Xtreme Downloader")
    window.setFixedWidth(420)
    window.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
    # Its own colours, not the application's. This process may be running
    # while the folder holding the theme is being replaced, and a window that
    # depends on what it is deleting is a window that appears as grey text on
    # a grey background at exactly the wrong moment.
    window.setStyleSheet(
        "QWidget { background: #0d0f16; color: #e7ecff;"
        " font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }"
        "QProgressBar { border: 1px solid rgba(150,170,230,.28);"
        " border-radius: 6px; background: #141827; height: 10px; }"
        "QProgressBar::chunk { border-radius: 5px; background: #5b8cff; }"
    )

    layout = QVBoxLayout(window)
    layout.setContentsMargins(24, 22, 24, 20)
    layout.setSpacing(12)

    heading = QLabel(f"Updating to version {__version__}")
    heading.setStyleSheet("font-size: 15px; font-weight: 600; color: #ffffff;")
    layout.addWidget(heading)

    detail = QLabel("Waiting for the application to close…")
    detail.setWordWrap(True)
    detail.setStyleSheet("color: #95a0c2;")
    layout.addWidget(detail)

    bar = QProgressBar()
    bar.setRange(0, 0)                      # this takes as long as it takes
    bar.setTextVisible(False)
    layout.addWidget(bar)

    footer = QLabel("It will start again by itself when this is done.")
    footer.setWordWrap(True)
    footer.setStyleSheet("color: #6b7597; font-size: 12px;")
    layout.addWidget(footer)

    return window, {"heading": heading, "detail": detail, "bar": bar,
                    "footer": footer}


def run(target: Path, wait_for: int = 0) -> int:
    """Swap ``target`` for this folder, showing what is happening if it can."""
    if _headless():
        ok, detail = updates.apply(target, wait_for=wait_for, timeout=180.0)
        print(detail)
        return 0 if ok else 1

    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtWidgets import (
            QApplication, QLabel, QProgressBar, QVBoxLayout, QWidget,
        )
    except Exception:                       # noqa: BLE001 - no Qt, no window
        ok, detail = updates.apply(target, wait_for=wait_for, timeout=180.0)
        print(detail)
        return 0 if ok else 1

    application = QApplication(sys.argv[:1])
    window, parts = build_window()
    heading, detail, bar, footer = (parts["heading"], parts["detail"],
                                    parts["bar"], parts["footer"])

    # A theme, if the built copy carries one. Failing to find it is not worth
    # abandoning an update over.
    try:
        from .ui.theme import DARK, apply_theme

        apply_theme(application, DARK)
    except Exception:                       # noqa: BLE001
        pass

    outcome: dict[str, object] = {}

    def work() -> None:
        ok, message = updates.apply(target, wait_for=wait_for, timeout=180.0)
        outcome["ok"] = ok
        outcome["detail"] = message

    thread = threading.Thread(target=work, name="ixd-apply-update", daemon=True)

    def started() -> None:
        detail.setText("Replacing the old version…")
        thread.start()

    def poll() -> None:
        if thread.is_alive():
            return
        timer.stop()
        if outcome.get("ok"):
            detail.setText("Done. Starting the new version…")
            QTimer.singleShot(900, application.quit)
            return
        bar.setRange(0, 1)
        bar.setValue(1)
        heading.setText("The update did not finish")
        detail.setText(str(outcome.get("detail") or "unknown failure"))
        footer.setText("The previous version was left in place and still works.")
        window.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        window.show()

    window.show()
    QTimer.singleShot(50, started)
    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(200)

    application.exec()
    print(outcome.get("detail") or "")
    return 0 if outcome.get("ok") else 1
