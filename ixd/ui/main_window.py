"""The main application window."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..config import CATEGORY_ORDER
from ..core.events import EventType
from ..core.http_client import format_bytes, format_eta, format_speed
from ..core.models import DownloadStatus, TransferMode
from .taskbar import TaskbarProgress
from .theme import DARK, Palette, status_colour
from .widgets.add_dialog import AddDownloadDialog
from .widgets.chunk_bars import ChunkBars
from .widgets.download_table import (
    DownloadFilterProxy,
    DownloadTable,
    DownloadTableModel,
    ROLE_DOWNLOAD_ID,
)
from .widgets.download_window import DownloadWindow
from .widgets.properties_dialog import PropertiesDialog
from .widgets.settings_dialog import SettingsDialog
from .widgets.speed_graph import SpeedGraph
from .widgets.tray import TrayIcon, application_icon
from .workers import EventBridge

if TYPE_CHECKING:  # pragma: no cover
    from ..service import DownloadService

SIDEBAR_SECTIONS = (
    ("All", "All downloads"),
    ("Active", "In progress"),
    ("Unfinished", "Unfinished"),
    ("Completed", "Completed"),
)


class MainWindow(QMainWindow):
    """Sidebar + download table + detail panel."""

    def __init__(self, service: "DownloadService", palette: Palette = DARK) -> None:
        super().__init__()
        self.service = service
        self._palette = palette
        self._quitting = False
        self._selected_id: int | None = None
        #: The file-info windows the browser has opened and nobody has answered
        #: yet. Held because they are shown, not executed — a dialog with no
        #: reference to it is collected the moment the method that made it ends.
        self._browser_dialogs: set = set()

        self.setWindowTitle(f"Internet Xtreme Downloader {__version__}")
        self.setWindowIcon(application_icon())
        self.resize(1240, 760)
        self.setMinimumSize(940, 560)

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._build_tray()

        self.bridge = EventBridge(service.events, self)
        self.bridge.event.connect(self._on_engine_event)

        # Progress on the taskbar button, the dock badge or the launcher —
        # whichever this desktop has. It needs a window handle on Windows, so
        # it is given one as soon as there is one.
        self.taskbar = TaskbarProgress()
        self.taskbar.attach(self)
        self._taskbar_note = ""
        #: Whether the taskbar backend has already been seen working. One line
        #: proves it is live; the rest is noise in a log kept for defects.
        self._taskbar_confirmed = False

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(700)
        self.refresh()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Main")
        toolbar.setObjectName("Toolbar")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self.action_add = QAction("＋  Add", self)
        self.action_add.setShortcut(QKeySequence.StandardKey.New)
        self.action_add.triggered.connect(self.open_add_dialog)
        toolbar.addAction(self.action_add)

        toolbar.addSeparator()

        self.action_resume = QAction("▶  Resume", self)
        self.action_resume.triggered.connect(self.resume_selected)
        toolbar.addAction(self.action_resume)

        self.action_pause = QAction("❚❚  Pause", self)
        self.action_pause.triggered.connect(self.pause_selected)
        toolbar.addAction(self.action_pause)

        self.action_remove = QAction("✕  Remove from list", self)
        self.action_remove.setShortcut(QKeySequence.StandardKey.Delete)
        # `triggered(bool)` hands its checked flag to any slot that will take an
        # argument, and this one takes `ids` — so a direct connection called
        # `remove_selected(False)` and fell out of the "nothing to remove"
        # guard. The button and the Delete key did nothing at all.
        self.action_remove.triggered.connect(lambda _=False: self.remove_selected())
        toolbar.addAction(self.action_remove)

        toolbar.addSeparator()

        self.action_resume_all = QAction("Resume all", self)
        self.action_resume_all.triggered.connect(self.service.resume_all)
        toolbar.addAction(self.action_resume_all)

        self.action_pause_all = QAction("Pause all", self)
        self.action_pause_all.triggered.connect(self.service.pause_all)
        toolbar.addAction(self.action_pause_all)

        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        toolbar.addWidget(spacer)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search downloads…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(220)
        self.search_edit.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search_edit)

        # A place to read what actually happened, in both halves at once. The
        # browser side reports through the `log` command, so a real test on a
        # real site leaves evidence here instead of in a service-worker console
        # nobody has open. Temporary by intent — it exists while the browser
        # integration is being got right.
        self.action_log = QAction("🗒  Log", self)
        self.action_log.triggered.connect(self.open_log)
        toolbar.addAction(self.action_log)

        # The scheduler earns its own button. Everything it does — which
        # downloads a queue runs, the order they run in, when it starts and
        # stops, and what happens to the machine afterwards — was reachable
        # only by opening Settings and finding the right tab, which is not
        # where anybody looks for "start this at 2am".
        self.action_schedule = QAction("🕑  Scheduler", self)
        self.action_schedule.triggered.connect(self.open_scheduler)
        toolbar.addAction(self.action_schedule)

        self.action_settings = QAction("⚙  Settings", self)
        self.action_settings.triggered.connect(self.open_settings)
        toolbar.addAction(self.action_settings)

    def _build_body(self) -> None:
        central = QWidget()
        central.setObjectName("Content")
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(14, 14, 14, 8)

        self.banner = QFrame()
        self.banner.setObjectName("Card")
        banner_layout = QHBoxLayout(self.banner)
        banner_layout.setContentsMargins(14, 10, 12, 10)
        self.banner_label = QLabel("")
        self.banner_label.setWordWrap(True)
        banner_button = QPushButton("Provide a new link…")
        banner_button.setObjectName("Primary")
        banner_button.clicked.connect(self._resolve_expired_link)
        banner_dismiss = QPushButton("Dismiss")
        banner_dismiss.clicked.connect(lambda: self.banner.setVisible(False))
        banner_layout.addWidget(self.banner_label, 1)
        banner_layout.addWidget(banner_button)
        banner_layout.addWidget(banner_dismiss)
        self.banner.setVisible(False)
        table_layout.addWidget(self.banner)

        self.model = DownloadTableModel(self._palette, self)
        self.proxy_model = DownloadFilterProxy(self)
        self.proxy_model.setSourceModel(self.model)

        self.table = DownloadTable(self._palette, self)
        self.table.setModel(self.proxy_model)
        # Opening a row shows the download's own window — the live one, with
        # the rate, the time left, what each connection is doing and a Pause.
        # Properties stays on the context menu, where a form belongs.
        self.table.activated_download.connect(self.open_download_window)
        self.table.context_requested.connect(self._show_context_menu)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        table_layout.addWidget(self.table, 1)
        splitter.addWidget(table_container)

        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 240])

        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(214)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(4)

        brand = QLabel("IXD")
        brand.setObjectName("BrandTitle")
        layout.addWidget(brand)
        subtitle = QLabel("Internet Xtreme Downloader")
        subtitle.setObjectName("BrandSub")
        # The sidebar is a fixed 214px and the name is longer than it: without
        # this the label is silently cut off mid-word.
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(14)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)

        def add_section(title: str) -> None:
            label = QLabel(title)
            label.setObjectName("SectionLabel")
            layout.addSpacing(10)
            layout.addWidget(label)
            layout.addSpacing(2)

        add_section("Library")
        for key, label in SIDEBAR_SECTIONS:
            layout.addWidget(self._nav_button(key, label))

        add_section("Categories")
        for category in CATEGORY_ORDER:
            layout.addWidget(self._nav_button(category, category))

        layout.addStretch(1)

        self.stat_speed = QLabel("—")
        self.stat_speed.setObjectName("StatValue")
        speed_label = QLabel("TOTAL SPEED")
        speed_label.setObjectName("StatLabel")
        layout.addWidget(self.stat_speed)
        layout.addWidget(speed_label)

        self.nav_group.buttons()[0].setChecked(True)
        return sidebar

    def _nav_button(self, key: str, label: str) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName("NavItem")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _checked, k=key: self._on_category(k))
        self.nav_group.addButton(button)
        return button

    def _build_detail_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("DetailPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(16)

        left = QVBoxLayout()
        left.setSpacing(8)
        self.detail_title = QLabel("Select a download")
        self.detail_title.setObjectName("DetailTitle")
        self.detail_title.setWordWrap(True)
        left.addWidget(self.detail_title)

        self.detail_meta = QLabel("")
        self.detail_meta.setObjectName("Muted")
        self.detail_meta.setWordWrap(True)
        left.addWidget(self.detail_meta)

        self.connections_label = QLabel("CONNECTIONS")
        self.connections_label.setObjectName("SectionLabel")
        left.addWidget(self.connections_label)

        self.chunk_bars = ChunkBars(self._palette)
        left.addWidget(self.chunk_bars, 1)

        buttons = QHBoxLayout()
        self.detail_properties = QPushButton("Properties")
        self.detail_properties.clicked.connect(
            lambda: self._selected_id and self.open_properties(self._selected_id)
        )
        self.detail_open = QPushButton("Open folder")
        self.detail_open.clicked.connect(self._open_selected_folder)
        buttons.addWidget(self.detail_properties)
        buttons.addWidget(self.detail_open)
        buttons.addStretch(1)
        left.addLayout(buttons)
        layout.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(8)
        throughput_label = QLabel("THROUGHPUT")
        throughput_label.setObjectName("SectionLabel")
        right.addWidget(throughput_label)
        self.speed_graph = SpeedGraph(self._palette)
        right.addWidget(self.speed_graph, 1)
        layout.addLayout(right, 2)

        return panel

    def _build_statusbar(self) -> None:
        bar = self.statusBar()
        bar.setObjectName("StatusBar")
        self.status_left = QLabel("Ready")
        self.status_right = QLabel("")
        # Where a newer version announces itself: a line at the bottom of the
        # window, in the accent colour, that is a link rather than a dialog.
        # A modal box on top of somebody's downloads to tell them about a
        # version number is how an update prompt becomes something people
        # learn to dismiss without reading.
        self.update_notice = QLabel("")
        self.update_notice.setObjectName("UpdateNotice")
        self.update_notice.setStyleSheet(
            f"color: {self._palette.accent}; font-weight: 600;")
        self.update_notice.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_notice.setVisible(False)
        self.update_notice.mousePressEvent = (
            lambda _event: self.open_update_dialog())
        bar.addWidget(self.status_left, 1)
        bar.addWidget(self.update_notice)
        bar.addPermanentWidget(self.status_right)

    def _build_tray(self) -> None:
        self.tray = TrayIcon(self._palette, self)
        self.tray.show_requested.connect(self._restore_window)
        self.tray.quit_requested.connect(self.quit_application)
        self.tray.add_requested.connect(self.open_add_dialog)
        self.tray.pause_all_requested.connect(self.service.pause_all)
        self.tray.resume_all_requested.connect(self.service.resume_all)
        self.tray.show()

    # ------------------------------------------------------------------
    # data refresh
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        downloads = self.service.list_for_display()
        self.model.set_downloads(downloads)

        stats = self.service.stats()
        speed = float(stats.get("speed", 0.0))
        self.speed_graph.add_sample(speed)
        self.speed_graph.set_limit(int(stats.get("limit", 0)))
        self.stat_speed.setText(f"{format_bytes(speed)}/s")
        self.tray.update_status(int(stats.get("active", 0)), speed)

        limit = int(stats.get("limit", 0))
        limit_text = f"limit {format_bytes(limit)}/s" if limit else "no limit"
        self.status_left.setText(
            f"{stats.get('active', 0)} active · {len(downloads)} total · {limit_text}"
        )
        self.status_right.setText(
            f"route: {stats.get('proxy', 'direct')}"
            + (f" · {stats.get('proxy_pool', 0)} proxies" if stats.get("proxy_pool") else "")
        )

        self._update_taskbar(downloads)
        self._update_detail()
        self._update_banner(downloads)

    def _update_taskbar(self, downloads) -> None:
        """Overall progress on the application's icon.

        The bytes of everything running, against what those downloads will
        weigh — not the count of them, and not the average of their
        percentages, either of which jumps about as downloads start and finish.
        A download whose length nobody published is left out of the sum rather
        than counted as zero, which would drag the bar down for the whole
        transfer and then snap it up at the end.

        Nothing running means nothing drawn: an icon that keeps a full bar
        after the last download finished is worse than one that shows none.
        """
        done = total = 0
        active = False
        for download in downloads:
            if not download.status.is_active:
                continue
            active = True
            if download.total_size > 0:
                done += max(0, min(download.downloaded, download.total_size))
                total += download.total_size
        if not active:
            self.taskbar.clear()
        elif total <= 0:
            # Running, but nobody published a length: segmented and
            # server-driven media routinely have none, and clearing the bar for
            # those looked exactly like nothing running.
            self.taskbar.set_indeterminate()
        else:
            self.taskbar.set_progress(done / total, visible=True)
        self._log_taskbar_state()

    def _log_taskbar_state(self) -> None:
        """Put what the platform did in the log — every refusal, one success.

        Progress on the icon never fails loudly: it is decoration, and no
        transfer depends on it. Silence about it made "the bar does not show
        on Windows" a report with nothing behind it twice over, which is why
        success is recorded at all (§3.22, §3.14u58).

        But it is confirmed working on both platforms now, and a line every
        time the state changes — normal, clear, indeterminate, and once per
        window — is a log full of the one thing that is *not* going wrong.
        The first success says the backend is live and names the windows it
        drew on; after that only refusals are written, and a refusal after a
        success is itself a change worth seeing.
        """
        message = self.taskbar.diagnostic()
        if not message or message == self._taskbar_note:
            return
        self._taskbar_note = message
        drew = message.startswith("ITaskbarList3")
        if drew:
            if self._taskbar_confirmed:
                return
            self._taskbar_confirmed = True
            self.service.db.log_event(
                f"Taskbar progress: {message} — working; further updates are "
                "not logged."
            )
            return
        # A refusal is always written, and the next success after one is
        # written too, so a backend that recovers says so.
        self._taskbar_confirmed = False
        self.service.db.log_event(f"Taskbar progress: {message}", level="warning")

    def _update_detail(self) -> None:
        if self._selected_id is None:
            self.detail_title.setText("Select a download")
            self.detail_meta.setText("")
            self.chunk_bars.set_chunks([])
            return

        download = self.service.get_download(self._selected_id)
        if download is None:
            self._selected_id = None
            return

        self.detail_title.setText(download.filename or download.url)
        colour = status_colour(download.status.value, self._palette)
        pieces = [
            f"<span style='color:{colour}'>{download.status.value.replace('_', ' ')}</span>",
            f"{format_bytes(download.downloaded)} / "
            f"{format_bytes(download.total_size) if download.total_size else '?'}",
        ]
        if download.status.is_active:
            pieces.append(format_speed(download.speed))
            pieces.append(format_eta(download.eta))
        if download.error:
            pieces.append(f"<span style='color:{self._palette.bad}'>{download.error}</span>")
        self.detail_meta.setText("  ·  ".join(pieces))

        bars = download.display_chunks or []
        # Say *why* there is one bar. A server-driven stream has no byte ranges
        # to divide — the client asks an endpoint for media rather than for
        # bytes — so the connection count cannot apply to it, and a lone bar
        # next to a setting that says sixteen looks like the setting is being
        # ignored rather than inapplicable.
        wanted = download.connections or self.service.settings.get_int(
            "connections_per_download", 8)
        # A server-driven transfer is now split across sessions when the stream
        # publishes a segment index, so "1" is no longer the answer for the
        # mode — it is the answer only when a stream carries no index and there
        # is nothing exact to seek with. Saying "1" while sixteen bars are
        # drawn was worse than saying nothing.
        if download.mode is TransferMode.SABR and len(bars) <= 1:
            self.connections_label.setText(
                "CONNECTIONS — 1 (this stream publishes no segment index, so "
                "it cannot be split)")
        elif download.mode is TransferMode.SABR and bars:
            self.connections_label.setText(
                f"CONNECTIONS — {len(bars)} streaming sessions")
        elif download.status.is_active and len(bars) < wanted and bars:
            self.connections_label.setText(
                f"CONNECTIONS — {len(bars)} of {wanted}")
        else:
            self.connections_label.setText("CONNECTIONS")

        self.chunk_bars.set_chunks([
            {"progress": chunk.progress, "status": chunk.status.value}
            for chunk in bars
        ])

    def _update_banner(self, downloads) -> None:
        expired = [d for d in downloads if d.status is DownloadStatus.NEEDS_LINK]
        if not expired:
            self.banner.setVisible(False)
            return
        self._expired_id = expired[0].id
        names = ", ".join(d.filename for d in expired[:2])
        extra = f" and {len(expired) - 2} more" if len(expired) > 2 else ""
        self.banner_label.setText(
            f"<b>Source link expired</b> — {names}{extra}. "
            "Provide a refreshed link to resume from the existing progress."
        )
        self.banner.setVisible(True)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------
    def open_add_dialog(self, url: str = "") -> None:
        dialog = AddDownloadDialog(self.service, self, url if isinstance(url, str) else "")
        dialog.exec()
        self.refresh()

    def confirm_browser_download(self, payload: dict) -> None:
        """Ask about a download the browser handed over, IDM-style.

        Shown, never executed: the browser can hand over three downloads before
        anyone looks at the screen, and a modal window would make the second
        and third wait behind the first. The main window is deliberately left
        as it is — hidden in the tray is the normal state when this arrives.
        """
        from .widgets.browser_dialog import BrowserDownloadDialog

        # Logged because "I clicked download and nothing happened" is otherwise
        # unanswerable: the extension has already cancelled the browser's own
        # download and gone quiet, so this line is the only evidence that the
        # hand-over arrived and reached a window (§423).
        self.service.db.log_event(
            "Asking where to put "
            f"{payload.get('filename') or payload.get('url') or 'a download'}")

        dialog = BrowserDownloadDialog(self.service, self, payload)
        self._browser_dialogs.add(dialog)
        dialog.finished.connect(
            lambda _result, d=dialog: self._browser_dialogs.discard(d))
        dialog.queued.connect(lambda _id: self.refresh())
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_log(self) -> None:
        from .widgets.log_dialog import LogDialog

        LogDialog(self.service, self).exec()

    def open_settings(self, tab: str = "") -> None:
        dialog = SettingsDialog(self.service, self)
        if tab:
            dialog.show_tab(tab)
        dialog.exec()
        self.refresh()

    # ------------------------------------------------------------------
    # a newer version
    # ------------------------------------------------------------------
    def _offer_update(self, payload: dict) -> None:
        version = str(payload.get("version") or "")
        if not version:
            return
        self._update_payload = dict(payload)
        self.update_notice.setText(f"  ●  Version {version} is available  ")
        self.update_notice.setToolTip(
            "A newer version has been published. Click to see what changed.")
        self.update_notice.setVisible(True)
        self.tray.notify("Update available", f"Version {version} is ready.")

    def open_update_dialog(self) -> None:
        from .widgets.update_dialog import UpdateDialog

        UpdateDialog(self.service, getattr(self, "_update_payload", {}),
                     self).exec()
        self.refresh()

    def open_scheduler(self) -> None:
        """Settings, opened where the schedules are."""
        self.open_settings("Scheduler")

    # ------------------------------------------------------------------
    # "shut down when it is done", and the way out of it
    #
    # The countdown is the whole safety of the feature. The service arms it
    # and would carry it out with no window at all — which is what
    # `--background` needs — so this window's only job is to say what is about
    # to happen and offer to stop it. It is modeless on purpose: a modal box
    # would block the rest of the application while the machine is still
    # perfectly usable.
    # ------------------------------------------------------------------
    def _show_completion_countdown(self, payload: dict) -> None:
        from ..power import parse as parse_completion_action

        action = parse_completion_action(payload.get("action"))
        remaining = int(payload.get("seconds") or 0)
        self._close_completion_countdown()

        box = QMessageBox(self)
        box.setWindowTitle("Everything has finished")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Cancel).setText("Don't")
        box.setModal(False)

        def render(seconds: int) -> None:
            box.setText(f"<b>{action.label} in {seconds}s</b>")
            box.setInformativeText(
                "Every download has finished and nothing is paused or waiting."
                + ("\n\nThis affects the whole machine."
                   if action.touches_the_machine else "")
            )

        render(remaining)
        self._completion_box = box
        self._completion_left = remaining

        def tick() -> None:
            self._completion_left -= 1
            if self._completion_left <= 0:
                self._close_completion_countdown()
                return
            render(self._completion_left)

        timer = QTimer(self)
        timer.setInterval(1000)
        timer.timeout.connect(tick)
        timer.start()
        self._completion_ticker = timer

        # Dismissing the warning any way at all calls the action off. The two
        # failure modes are not equal: a machine that does not power down
        # because a box was closed costs nothing, and one that powers down
        # while somebody was reading the warning costs whatever they were
        # doing. The guard is against re-entry — the box is torn down by the
        # cancelled *and* the fired event, and neither is a user decision.
        def dismissed(_result: int = 0) -> None:
            if getattr(self, "_completion_box", None) is not box:
                return
            self.service.cancel_completion_action("stopped from the window")
            self._close_completion_countdown()

        box.buttonClicked.connect(lambda _button: dismissed())
        box.finished.connect(dismissed)
        box.show()
        box.raise_()
        self.tray.notify("Everything has finished", f"{action.label} in {remaining}s.")

    def _close_completion_countdown(self) -> None:
        ticker = getattr(self, "_completion_ticker", None)
        if ticker is not None:
            ticker.stop()
            self._completion_ticker = None
        box = getattr(self, "_completion_box", None)
        if box is not None:
            self._completion_box = None
            box.hide()
            box.deleteLater()

    def open_download_window(self, download_id: int) -> None:
        """The download's own live window, one per download and not modal."""
        DownloadWindow.show_for(self.service, download_id, self._palette, self)

    def open_properties(self, download_id: int) -> None:
        dialog = PropertiesDialog(self.service, download_id, self._palette, self)
        dialog.exec()
        self.refresh()

    def resume_selected(self) -> None:
        for download_id in self._target_ids():
            self.service.resume(download_id)
        self.refresh()

    def pause_selected(self) -> None:
        for download_id in self._target_ids():
            self.service.pause(download_id)
        self.refresh()

    def remove_selected(self, ids: list[int] | None = None) -> None:
        """Remove downloads, always asking what to do with the files.

        Every route to removal comes through here — the toolbar button, the
        Delete key and the right-click menu. The menu used to call
        ``service.remove()`` directly, so the same command silently kept the
        files from one place and asked from another; whether a file survived
        depended on which way the user reached for it.

        Anything that is not a list of ids means "whatever is selected". A
        signal's own argument arrives here otherwise — `triggered(bool)` sent
        ``False``, which is not ``None`` and is not a list either, and removal
        silently did nothing.
        """
        if not isinstance(ids, list):
            ids = self._target_ids()
        if not ids:
            return

        if len(ids) == 1:
            download = self.service.get_download(ids[0])
            subject = f"“{download.filename}”" if download else "this download"
        else:
            subject = f"these {len(ids)} downloads"

        noun = "file" if len(ids) == 1 else "files"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Remove from list")
        box.setText(f"Remove {subject} from the list?")
        box.setInformativeText(
            f"The downloaded {noun} can be kept on disk or deleted along with it."
            if len(ids) == 1 else
            "The downloaded files can be kept on disk or deleted along with them."
        )
        # Both buttons name the same act and differ only in what happens to the
        # file, so neither can be read as "remove" meaning one thing here and
        # another there.
        keep = box.addButton(f"Remove from list, keep {noun}",
                             QMessageBox.ButtonRole.AcceptRole)
        delete = box.addButton(f"Remove from list and delete {noun}",
                               QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        # Keeping the file is the safe answer, so it is the one a stray Return
        # or Escape lands on rather than the destructive one.
        box.setDefaultButton(keep)
        box.setEscapeButton(cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked not in (keep, delete):
            return
        for download_id in ids:
            self.service.remove(download_id, delete_files=(clicked is delete))
        self._selected_id = None
        self.refresh()

    def _target_ids(self) -> list[int]:
        ids = self.table.selected_ids()
        if not ids and self._selected_id is not None:
            return [self._selected_id]
        return ids

    def _resolve_expired_link(self) -> None:
        download_id = getattr(self, "_expired_id", None)
        if download_id is None:
            return
        download = self.service.get_download(download_id)
        if download is None:
            return
        url, accepted = QInputDialog.getText(
            self, "Refresh the source link",
            f"Paste a fresh link for:\n{download.filename}\n\n"
            "Completed byte ranges are preserved — only the missing parts are fetched.",
            QLineEdit.EchoMode.Normal, download.url,
        )
        if not accepted or not url.strip():
            return
        try:
            self.service.swap_link(download_id, url.strip())
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            QMessageBox.warning(self, "Link swap", str(exc))
            return
        self.banner.setVisible(False)
        self.refresh()

    def _open_selected_folder(self) -> None:
        if self._selected_id is None:
            return
        download = self.service.get_download(self._selected_id)
        if download is None or not download.dest_dir:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(download.dest_dir))

    def _open_file(self, download_id: int) -> None:
        download = self.service.get_download(download_id)
        if download is None or not os.path.isfile(download.filepath):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(download.filepath))

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    def _on_category(self, key: str) -> None:
        self.proxy_model.set_category(key)

    def _on_search(self, text: str) -> None:
        self.proxy_model.set_query(text)

    def _on_selection_changed(self, *_args) -> None:
        ids = self.table.selected_ids()
        self._selected_id = ids[0] if ids else None
        self._update_detail()

    def _show_context_menu(self, download_id: int, position) -> None:
        if download_id < 0:
            return
        download = self.service.get_download(download_id)
        if download is None:
            return

        # A right-click inside an existing selection keeps that selection, and
        # what the menu then does has to be what the list shows as chosen — the
        # menu used to act on the clicked row alone, so selecting ten and
        # removing them removed one and left nine looking selected.
        selection = self.table.selected_ids()
        targets = selection if download_id in selection else [download_id]
        several = len(targets) > 1
        suffix = f" ({len(targets)})" if several else ""

        menu = QMenu(self)
        if download.status.is_active:
            menu.addAction(f"Pause{suffix}",
                           lambda: self._for_each(targets, self.service.pause))
        elif download.status.is_startable:
            menu.addAction(f"Resume{suffix}",
                           lambda: self._for_each(targets, self.service.resume))

        if download.status is DownloadStatus.NEEDS_LINK:
            menu.addAction("Provide a refreshed link…", self._resolve_expired_link)

        menu.addSeparator()
        menu.addAction("Properties…", lambda: self.open_properties(download_id))
        if download.status is DownloadStatus.COMPLETED:
            menu.addAction("Open file", lambda: self._open_file(download_id))
            menu.addAction("Re-verify integrity",
                           lambda: (self.service.reverify(download_id), self.refresh()))
        menu.addAction("Open containing folder", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(download.dest_dir)
        ))
        menu.addSeparator()
        menu.addAction("Copy source URL", lambda: self._copy(download.url))
        menu.addSeparator()
        # Through the same prompt as the toolbar and the Delete key: removing
        # from the list and deleting the file are different acts, and the menu
        # used to pick one of them on the user's behalf without saying so.
        menu.addAction(f"Remove from list…{suffix}",
                       lambda: self.remove_selected(list(targets)))
        menu.exec(position)

    def _for_each(self, ids: list[int], command) -> None:
        for download_id in ids:
            command(download_id)
        self.refresh()

    def _copy(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)

    def _on_engine_event(self, event_type: str, payload: dict) -> None:
        if event_type == EventType.DOWNLOAD_ADDED:
            # A new download shows its own window, as a download manager does.
            # Closing it leaves the transfer running in the main window, and
            # opening the row brings it back — the window watches a download,
            # it does not own it.
            #
            # Unless it was added paused: "download later" means exactly that,
            # and a progress window over a transfer that will not move until
            # tonight is the opposite of what was asked for.
            deferred = str(
                (payload.get("download") or {}).get("status") or "") == "paused"
            if not deferred and self.service.settings.get_bool(
                    "show_download_window", True):
                download_id = payload.get("download_id")
                if download_id is not None:
                    self.open_download_window(int(download_id))
        elif event_type == EventType.DOWNLOAD_COMPLETED:
            if self.service.settings.get_bool("notify_on_complete", True):
                download = self.service.get_download(payload.get("download_id", -1))
                name = download.filename if download else "Download"
                status = payload.get("hash_status", "")
                suffix = " (verified)" if status == "verified" else ""
                self.tray.notify("Download complete", f"{name}{suffix}")
        elif event_type == EventType.DOWNLOAD_NEEDS_LINK:
            self._expired_id = payload.get("download_id")
            self.tray.notify(
                "Source link expired",
                "A download is waiting for a refreshed link.",
            )
        elif event_type == EventType.UPDATE_AVAILABLE:
            self._offer_update(payload)
        elif event_type == EventType.COMPLETION_ARMED:
            self._show_completion_countdown(payload)
        elif event_type == EventType.COMPLETION_CANCELLED:
            self._close_completion_countdown()
        elif event_type == EventType.COMPLETION_FIRED:
            self._close_completion_countdown()
            if payload.get("action") == "exit":
                self.quit_application()
            elif not payload.get("ok", False):
                self.tray.notify(
                    "Nothing happened",
                    "The machine refused the completion action; the Log says why.",
                )
        elif event_type == EventType.DOWNLOAD_FAILED:
            self.status_left.setText(f"Error: {payload.get('error', '')}")
        elif event_type == EventType.PROXY_ROTATED:
            self.status_right.setText(f"route: {payload.get('proxy', 'direct')}")

    # ------------------------------------------------------------------
    # window lifecycle
    # ------------------------------------------------------------------
    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_application(self, force_after: float = 0.0) -> None:
        """Shut the whole application down, not just the window.

        ``force_after`` is for the one case where lingering is worse than
        leaving something unfinished: an update is staged and the new version
        is waiting for this process to end before it can replace it. Qt is
        asked to stop as usual, and if this process is somehow still here a
        few seconds later it stops anyway.

        The application deliberately keeps running when its window is closed —
        that is what lets downloads continue from the tray — which means
        closing the window cannot end the event loop. Quitting therefore has to
        say so explicitly, or the process stays alive with no window and has to
        be killed by hand.
        """
        if self._quitting:
            return
        self._quitting = True

        self._refresh_timer.stop()
        self.bridge.detach()
        self.tray.hide()
        self.close()

        if force_after > 0:
            def leave() -> None:
                # The service first, so the database closes cleanly, and then
                # out — whatever else is still holding the event loop. The
                # updater is watching this process id and can do nothing until
                # it is gone.
                try:
                    self.service.shutdown()
                except Exception:       # noqa: BLE001 - going anyway
                    pass
                os._exit(0)

            QTimer.singleShot(int(force_after * 1000), leave)

        from PySide6.QtWidgets import QApplication
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: D102, N802 - Qt naming
        # Closing the window quits, unless the user has *asked* for it to keep
        # running in the tray.
        #
        # This defaulted to on, and `tray.isVisible()` only reports that `show()`
        # was called — not that any desktop is drawing it. A GNOME session with
        # no tray extension therefore swallowed the whole application: the
        # window closed, nothing appeared anywhere, and the process had to be
        # killed by hand. Reported exactly that way. Availability is asked of
        # the platform now, and the choice is opt-in.
        from PySide6.QtWidgets import QSystemTrayIcon

        if not self._quitting \
                and self.service.settings.get_bool("close_to_tray", False) \
                and QSystemTrayIcon.isSystemTrayAvailable() \
                and self.tray.isVisible():
            event.ignore()
            self.hide()
            # Once. The first close has to explain where the application went,
            # because a window that disappears with no trace is one people
            # think they have quit — but saying it at every close is a
            # notification for something the user now knows and did on
            # purpose. Remembered in the settings, so it is once per
            # installation and not once per launch.
            if not self.service.settings.get_bool("close_to_tray_notice_shown", False):
                self.service.settings.set("close_to_tray_notice_shown", True)
                self.tray.notify(
                    "Still running",
                    "Downloads continue in the background. Use the tray icon "
                    "to reopen — this is the only time this will be said.",
                )
            return

        # Closing the window with the tray disabled is also a request to quit;
        # otherwise the process would linger with nothing left to interact with.
        self._refresh_timer.stop()
        self.bridge.detach()
        self.tray.hide()
        event.accept()

        if not self._quitting:
            self._quitting = True
            from PySide6.QtWidgets import QApplication
            QApplication.quit()
