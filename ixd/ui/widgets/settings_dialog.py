"""Application settings: transfers, routing, queues, schedules and integration."""

from __future__ import annotations

import subprocess
import time as _time
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ...core.models import (
    DownloadQueue,
    DownloadStatus,
    ProxyEntry,
    ProxyScheme,
    QueueMode,
    Schedule,
    ScheduleAction,
)
from ...core.net import list_interfaces
from ...core.scheduler import WEEKDAY_NAMES, format_days
from ...power import CompletionAction
from ..workers import Worker

if TYPE_CHECKING:  # pragma: no cover
    from ...service import DownloadService


def _size_text(value: int | None) -> str:
    """A byte count for a table cell; blank when the length is not published."""
    amount = float(value or 0)
    if amount <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:,.0f} {unit}" if unit == "B" else f"{amount:,.1f} {unit}"
        amount /= 1024
    return f"{amount:,.1f} TB"


def _kib(value: int) -> int:
    return max(0, int(value)) // 1024


def _from_kib(value: int) -> int:
    return max(0, int(value)) * 1024


class ProxyDialog(QDialog):
    """Add or edit a single proxy entry."""

    def __init__(self, proxy: ProxyEntry | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Proxy server")
        self.setMinimumWidth(430)
        self.proxy = proxy or ProxyEntry()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        form.setSpacing(9)

        self.label_edit = QLineEdit(self.proxy.label)
        self.label_edit.setPlaceholderText("Friendly name")
        form.addRow("Label", self.label_edit)

        self.scheme_combo = QComboBox()
        for scheme in ProxyScheme:
            self.scheme_combo.addItem(scheme.value, scheme)
        self.scheme_combo.setCurrentText(self.proxy.scheme.value)
        form.addRow("Protocol", self.scheme_combo)

        self.host_edit = QLineEdit(self.proxy.host)
        self.host_edit.setPlaceholderText("proxy.example.com")
        form.addRow("Host", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(self.proxy.port or 1080)
        form.addRow("Port", self.port_spin)

        self.user_edit = QLineEdit(self.proxy.username)
        form.addRow("Username", self.user_edit)

        self.password_edit = QLineEdit(self.proxy.password)
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Password", self.password_edit)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(self.proxy.enabled)
        form.addRow("", self.enabled_check)

        layout.addLayout(form)

        paste_row = QHBoxLayout()
        self.paste_edit = QLineEdit()
        self.paste_edit.setPlaceholderText("…or paste socks5://user:pass@host:1080")
        paste_button = QPushButton("Parse")
        paste_button.clicked.connect(self._parse_url)
        paste_row.addWidget(self.paste_edit, 1)
        paste_row.addWidget(paste_button)
        layout.addLayout(paste_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _parse_url(self) -> None:
        from ...core.routing import parse_proxy_url

        try:
            parsed = parse_proxy_url(self.paste_edit.text().strip())
        except ValueError as exc:
            QMessageBox.warning(self, "Proxy", str(exc))
            return
        self.scheme_combo.setCurrentText(parsed.scheme.value)
        self.host_edit.setText(parsed.host)
        self.port_spin.setValue(parsed.port)
        self.user_edit.setText(parsed.username)
        self.password_edit.setText(parsed.password)
        if not self.label_edit.text():
            self.label_edit.setText(parsed.label)

    def result_proxy(self) -> ProxyEntry:
        self.proxy.label = self.label_edit.text().strip() or self.host_edit.text().strip()
        # Back into the enum, deliberately.
        #
        # `ProxyScheme` is a `str` subclass, so Qt marshals the member through
        # `QVariant` as a plain string and `currentData()` hands back a `str`.
        # It looks right, prints right, compares equal — and has no `.value`,
        # so `insert_proxy` raised `AttributeError` on every single add. Qt
        # swallows an exception raised in a slot, so the dialog closed, nothing
        # was written, and the list stayed empty with no error anywhere.
        raw = self.scheme_combo.currentData() or self.scheme_combo.currentText()
        try:
            self.proxy.scheme = ProxyScheme(str(raw))
        except ValueError:
            self.proxy.scheme = ProxyScheme.HTTP
        self.proxy.host = self.host_edit.text().strip()
        self.proxy.port = self.port_spin.value()
        self.proxy.username = self.user_edit.text().strip()
        self.proxy.password = self.password_edit.text()
        self.proxy.enabled = self.enabled_check.isChecked()
        return self.proxy


class ScheduleDownloadsDialog(QDialog):
    """Which downloads a schedule runs, and the order it runs them in.

    A schedule drives a *queue*, and the engine starts a queue's downloads in
    priority order — so "what does this schedule run, and what goes first" is
    queue membership plus ordering. Both were reachable only by adding a
    download to the right queue when it was created, which is no use for the
    ones already sitting there paused.

    Ticking a row puts that download in the schedule's queue; clearing it takes
    it out. The order of the list is the order they start in, written back as
    descending priority. Downloads already finished are not offered: there is
    nothing to schedule about them.

    **A schedule set to "All queues" runs everything that is in a queue**, so
    it belongs here like any other. It used to be refused — "this schedule is
    not attached to a queue, so there is nothing for it to run" — which was the
    dialog reading `queue_id is None` as *unset* when the combo above it says
    *all*. Ticking a row that is already in a queue leaves it where it is; a
    row in no queue at all has to be put in one, and which one is the combo at
    the top of this window.
    """

    def __init__(self, service: "DownloadService", schedule: Schedule,
                 parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.schedule = schedule
        self.queues = [q for q in service.list_queues() if q.id is not None]
        self.queue_names = {q.id: q.name for q in self.queues}
        #: "All queues" — the schedule fires on every one of them.
        self.all_queues = schedule.queue_id is None
        self.setWindowTitle(f"Downloads for “{schedule.name or 'schedule'}”")
        self.setMinimumSize(720, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        blurb = QLabel(
            "Tick the downloads this schedule should run. They start from the "
            "top, so put what matters first."
            + (" This schedule runs every queue, so anything in any queue is "
               "already covered." if self.all_queues else "")
        )
        blurb.setObjectName("Subtle")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self.target_combo = QComboBox()
        if self.all_queues:
            # Only meaningful when the schedule covers every queue: a ticked
            # download that is in no queue has to be put in one, and this says
            # which. One already in a queue keeps it.
            for queue in self.queues:
                self.target_combo.addItem(queue.name, queue.id)
            target_row = QHBoxLayout()
            target_row.addWidget(QLabel("Put newly ticked downloads in"))
            target_row.addWidget(self.target_combo, 1)
            layout.addLayout(target_row)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "File", "Queue", "Size", "State"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 28)
        layout.addWidget(self.table, 1)

        moves = QHBoxLayout()
        for label, delta in (("Move up", -1), ("Move down", 1)):
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, d=delta: self._move(d))
            moves.addWidget(button)
        self.select_all = QPushButton("Select all")
        self.select_all.clicked.connect(lambda: self._set_all(True))
        self.select_none = QPushButton("Select none")
        self.select_none.clicked.connect(lambda: self._set_all(False))
        moves.addWidget(self.select_all)
        moves.addWidget(self.select_none)
        moves.addStretch(1)
        layout.addLayout(moves)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._rows: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    def _covered(self, download) -> bool:
        """Would this schedule already run that download?

        For a schedule on one queue that is membership of it. For one on *all*
        queues it is membership of any queue at all — an unassigned download is
        the only thing nothing runs.
        """
        if self.all_queues:
            return download.queue_id is not None
        return download.queue_id == self.schedule.queue_id

    def _target_queue_id(self) -> int | None:
        """The queue a newly ticked download goes into."""
        if not self.all_queues:
            return self.schedule.queue_id
        return self.target_combo.currentData()

    def _load(self) -> None:
        """Everything schedulable, what this schedule runs first and in order."""
        candidates = [
            download for download in self.service.list_downloads()
            if download.status is not DownloadStatus.COMPLETED
        ]
        # Already run by this schedule, in the order the engine would start
        # them, then everything else — so the list opens showing what it does.
        mine = [d for d in candidates if self._covered(d)]
        mine.sort(key=lambda d: (-int(d.priority or 0), d.id or 0))
        others = [d for d in candidates if not self._covered(d)]
        others.sort(key=lambda d: d.id or 0)

        self._rows = [{"download": d, "checked": True} for d in mine]
        self._rows += [{"download": d, "checked": False} for d in others]
        self._render()

    def _render(self) -> None:
        self.table.setRowCount(len(self._rows))
        for row, entry in enumerate(self._rows):
            download = entry["download"]
            tick = QTableWidgetItem()
            tick.setFlags(tick.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            tick.setCheckState(Qt.CheckState.Checked if entry["checked"]
                               else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, tick)
            self.table.setItem(row, 1, QTableWidgetItem(
                download.filename or download.url))
            self.table.setItem(row, 2, QTableWidgetItem(
                self.queue_names.get(download.queue_id, "—")))
            self.table.setItem(row, 3, QTableWidgetItem(
                _size_text(download.total_size)))
            self.table.setItem(row, 4, QTableWidgetItem(
                download.status.value.title()))

    def _harvest(self) -> None:
        """Read the ticks back off the table before the order is changed."""
        for row, entry in enumerate(self._rows):
            item = self.table.item(row, 0)
            if item is not None:
                entry["checked"] = item.checkState() == Qt.CheckState.Checked

    def _set_all(self, checked: bool) -> None:
        self._harvest()
        for entry in self._rows:
            entry["checked"] = checked
        self._render()

    def _move(self, delta: int) -> None:
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < len(self._rows)):
            return
        self._harvest()
        self._rows[row], self._rows[target] = self._rows[target], self._rows[row]
        self._render()
        self.table.selectRow(target)

    def _save(self) -> None:
        self._harvest()
        target = self._target_queue_id()
        chosen = [entry["download"] for entry in self._rows if entry["checked"]]
        # Descending, so the top of the list is what `_pump` starts first, and
        # spaced so a later insertion between two rows has somewhere to go.
        top = len(chosen) * 10
        for position, download in enumerate(chosen):
            # A download this schedule already runs stays in the queue it is
            # in — on an all-queues schedule that is any queue, and moving them
            # all into one would be a reorganisation nobody asked for.
            queue_id = download.queue_id if self._covered(download) else target
            self.service.db.update_download_fields(
                download.id, queue_id=queue_id, priority=top - position * 10)
        for entry in self._rows:
            if entry["checked"]:
                continue
            download = entry["download"]
            if self._covered(download):
                # Cleared: out of the queue, but still a download.
                self.service.db.update_download_fields(
                    download.id, queue_id=None)
        self.accept()


class ScheduleDialog(QDialog):
    """Add or edit a recurring schedule window."""

    def __init__(self, service: "DownloadService", schedule: Schedule | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Schedule")
        self.setMinimumWidth(470)
        self.schedule = schedule or Schedule()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        form.setSpacing(9)

        self.name_edit = QLineEdit(self.schedule.name)
        self.name_edit.setPlaceholderText("Overnight downloads")
        form.addRow("Name", self.name_edit)

        self.queue_combo = QComboBox()
        self.queue_combo.addItem("All queues", None)
        for queue in service.list_queues():
            self.queue_combo.addItem(queue.name, queue.id)
        if self.schedule.queue_id is not None:
            index = self.queue_combo.findData(self.schedule.queue_id)
            if index >= 0:
                self.queue_combo.setCurrentIndex(index)
        form.addRow("Applies to", self.queue_combo)

        days_row = QHBoxLayout()
        days_row.setSpacing(4)
        self.day_checks: list[QCheckBox] = []
        for index, name in enumerate(WEEKDAY_NAMES):
            check = QCheckBox(name)
            check.setChecked(bool(self.schedule.days_mask & (1 << index)))
            self.day_checks.append(check)
            days_row.addWidget(check)
        form.addRow("Days", days_row)

        time_row = QHBoxLayout()
        self.start_time = QTimeEdit(QTime.fromString(self.schedule.start_time, "HH:mm"))
        self.start_time.setDisplayFormat("HH:mm")
        self.end_time = QTimeEdit(QTime.fromString(self.schedule.end_time, "HH:mm"))
        self.end_time.setDisplayFormat("HH:mm")
        time_row.addWidget(self.start_time)
        time_row.addWidget(QLabel("to"))
        time_row.addWidget(self.end_time)
        time_row.addStretch(1)
        form.addRow("Window", time_row)

        self.start_action = QComboBox()
        self.end_action = QComboBox()
        for combo in (self.start_action, self.end_action):
            for action in ScheduleAction:
                combo.addItem(action.value, action)
        self.start_action.setCurrentText(self.schedule.action_start.value)
        self.end_action.setCurrentText(self.schedule.action_end.value)
        form.addRow("On entering", self.start_action)
        form.addRow("On leaving", self.end_action)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 1024 * 1024)
        self.limit_spin.setSuffix(" KB/s")
        self.limit_spin.setSpecialValueText("unlimited")
        self.limit_spin.setValue(_kib(self.schedule.speed_limit))
        form.addRow("Speed cap in window", self.limit_spin)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(self.schedule.enabled)
        form.addRow("", self.enabled_check)

        layout.addLayout(form)

        hint = QLabel(
            "A window that ends earlier than it starts (for example 22:00 → 04:00) "
            "runs overnight and belongs to the day it starts on."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_schedule(self) -> Schedule:
        mask = 0
        for index, check in enumerate(self.day_checks):
            if check.isChecked():
                mask |= 1 << index
        self.schedule.name = self.name_edit.text().strip() or "Schedule"
        self.schedule.queue_id = self.queue_combo.currentData()
        self.schedule.days_mask = mask
        self.schedule.start_time = self.start_time.time().toString("HH:mm")
        self.schedule.end_time = self.end_time.time().toString("HH:mm")
        # Qt gives back what it stored, and what it stored is a string —
        # `ScheduleAction` is one. Named explicitly so the type is right
        # before it leaves the dialog.
        self.schedule.action_start = ScheduleAction(
            str(self.start_action.currentData() or ScheduleAction.START.value))
        self.schedule.action_end = ScheduleAction(
            str(self.end_action.currentData() or ScheduleAction.NOTHING.value))
        self.schedule.speed_limit = _from_kib(self.limit_spin.value())
        self.schedule.enabled = self.enabled_check.isChecked()
        return self.schedule


class QueueDialog(QDialog):
    """Add or edit a download queue."""

    def __init__(self, queue: DownloadQueue | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Queue")
        self.setMinimumWidth(400)
        self.queue = queue or DownloadQueue(name="New queue")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        form = QFormLayout()
        form.setSpacing(9)

        self.name_edit = QLineEdit(self.queue.name)
        form.addRow("Name", self.name_edit)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Sequential (one at a time)", QueueMode.SEQUENTIAL)
        self.mode_combo.addItem("Concurrent", QueueMode.CONCURRENT)
        self.mode_combo.setCurrentIndex(0 if self.queue.mode is QueueMode.SEQUENTIAL else 1)
        form.addRow("Mode", self.mode_combo)

        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 32)
        self.concurrent_spin.setValue(max(1, self.queue.max_concurrent))
        form.addRow("Max concurrent", self.concurrent_spin)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 1024 * 1024)
        self.limit_spin.setSuffix(" KB/s")
        self.limit_spin.setSpecialValueText("unlimited")
        self.limit_spin.setValue(_kib(self.queue.speed_limit))
        form.addRow("Speed cap", self.limit_spin)

        self.interface_edit = QLineEdit(self.queue.network_interface)
        self.interface_edit.setPlaceholderText("e.g. tun0 — leave empty for the default route")
        form.addRow("Bind to interface", self.interface_edit)

        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(self.queue.enabled)
        form.addRow("", self.enabled_check)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("Primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_queue(self) -> DownloadQueue:
        self.queue.name = self.name_edit.text().strip() or "Queue"
        self.queue.mode = QueueMode(
            str(self.mode_combo.currentData() or QueueMode.SEQUENTIAL.value))
        self.queue.max_concurrent = self.concurrent_spin.value()
        self.queue.speed_limit = _from_kib(self.limit_spin.value())
        self.queue.network_interface = self.interface_edit.text().strip()
        self.queue.enabled = self.enabled_check.isChecked()
        return self.queue


# ----------------------------------------------------------------------
def _scrollable(page: QWidget) -> QScrollArea:
    """Let a page scroll rather than be crushed.

    A Qt layout that cannot fit its minimums does not refuse — it compresses
    past them, and a spin box compressed past its minimum clips the value it
    holds through the middle. The Transfers tab did exactly that at the
    dialog's own minimum height: "1024 KB" with its top and bottom sliced off,
    which is worse than a scroll bar by any measure and happens on every
    screen short enough.
    """
    area = QScrollArea()
    area.setWidget(page)
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.viewport().setAutoFillBackground(False)
    return area


class SettingsDialog(QDialog):
    """The main preferences window."""

    def __init__(self, service: "DownloadService", parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.settings = service.settings
        self.setWindowTitle("Settings")
        self.setMinimumSize(760, 620)
        self._worker: Worker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)

        tabs = QTabWidget()
        tabs.addTab(_scrollable(self._build_general()), "General")
        tabs.addTab(_scrollable(self._build_transfers()), "Transfers")
        tabs.addTab(_scrollable(self._build_proxies()), "Proxies")
        tabs.addTab(_scrollable(self._build_network()), "Network")
        tabs.addTab(_scrollable(self._build_queues()), "Queues")
        tabs.addTab(_scrollable(self._build_schedules()), "Scheduler")
        tabs.addTab(_scrollable(self._build_integration()), "Integration")
        tabs.addTab(_scrollable(self._build_updates()), "Updates")
        layout.addWidget(tabs, 1)
        self._tabs = tabs

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("Primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- Updates --------------------------------------------------------
    def _build_updates(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        from ... import __version__ as running_version
        from ... import updates as update_module

        current = QLabel(f"You are running version {running_version}.")
        current.setObjectName("Title")
        layout.addWidget(current)

        kind = update_module.self_update_kind()
        how = QLabel(
            "This build can install a new version itself."
            if kind else
            "This build was installed from a package, so a new version is "
            "downloaded from its release page and installed the way this one "
            "was. Nothing is replaced behind your back."
        )
        how.setObjectName("Subtle")
        how.setWordWrap(True)
        layout.addWidget(how)

        box = QGroupBox("Checking")
        form = QVBoxLayout(box)
        self.updates_automatic = QCheckBox(
            "Check for a new version automatically (once a day)")
        self.updates_automatic.setChecked(
            self.settings.get_bool("updates_check_automatically", True))
        form.addWidget(self.updates_automatic)

        self.updates_install = QCheckBox(
            "Install it by itself and restart (this build can)"
            if kind else
            "Install it by itself — not possible for a build installed from a "
            "package")
        self.updates_install.setChecked(
            self.settings.get_bool("updates_install_automatically", False))
        self.updates_install.setEnabled(bool(kind))
        self.updates_install.setToolTip(
            "The new version is downloaded in the background. When nothing is "
            "downloading, the application closes, the updater replaces it and "
            "starts it again.")
        form.addWidget(self.updates_install)

        last = self.settings.get("updates_last_check") or 0
        when = ("never" if not last else
                _time.strftime("%Y-%m-%d %H:%M", _time.localtime(float(last))))
        self.updates_when = QLabel(f"Last checked: {when}")
        self.updates_when.setObjectName("Subtle")
        form.addWidget(self.updates_when)

        row = QHBoxLayout()
        self.updates_now = QPushButton("Check now")
        self.updates_now.clicked.connect(self._check_updates_now)
        row.addWidget(self.updates_now)
        row.addStretch(1)
        form.addLayout(row)
        layout.addWidget(box)

        note = QLabel(
            "The check is one HTTPS request to the project's release page. It "
            "sends nothing about you or about what you download, and it goes "
            "through the proxy and interface configured under Network. "
            "Switching it off means checking here when you feel like it."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        layout.addWidget(note)

        extension_note = QLabel(
            "A new version writes the browser extension out again. After "
            "updating, reload it from your browser's extensions page so the "
            "browser runs the new one — the folder it loads from does not "
            "change."
        )
        extension_note.setObjectName("Subtle")
        extension_note.setWordWrap(True)
        layout.addWidget(extension_note)

        layout.addStretch(1)
        return page

    def _check_updates_now(self) -> None:
        """The manual half. Reports whatever it finds, including nothing."""
        from .update_dialog import UpdateDialog

        # Saved first: someone who ticks the box and presses Check expects the
        # tick to have meant something.
        self.settings.set("updates_check_automatically",
                          self.updates_automatic.isChecked())
        self.settings.set("updates_install_automatically",
                          self.updates_install.isChecked())
        UpdateDialog(self.service, {}, self).exec()
        self.updates_install = QCheckBox(
            "Install it by itself and restart (this build can)"
            if kind else
            "Install it by itself — not possible for a build installed from a "
            "package")
        self.updates_install.setChecked(
            self.settings.get_bool("updates_install_automatically", False))
        self.updates_install.setEnabled(bool(kind))
        self.updates_install.setToolTip(
            "The new version is downloaded in the background. When nothing is "
            "downloading, the application closes, the updater replaces it and "
            "starts it again.")
        form.addWidget(self.updates_install)

        last = self.settings.get("updates_last_check") or 0
        if last:
            self.updates_when.setText(
                "Last checked: "
                + _time.strftime("%Y-%m-%d %H:%M", _time.localtime(float(last))))

    def show_tab(self, title: str) -> bool:
        """Open on a named tab. Returns whether there was one."""
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index).lower() == title.lower():
                self._tabs.setCurrentIndex(index)
                return True
        return False

    # -- General --------------------------------------------------------
    def _build_general(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        box = QGroupBox("Files")
        form = QFormLayout(box)
        form.setSpacing(9)

        folder_row = QHBoxLayout()
        self.download_dir = QLineEdit(str(self.settings.get("download_dir")))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_download_dir)
        folder_row.addWidget(self.download_dir, 1)
        folder_row.addWidget(browse)
        form.addRow("Download folder", folder_row)

        self.categorize = QCheckBox("Sort finished files into category sub-folders")
        self.categorize.setChecked(self.settings.get_bool("categorize_into_subfolders", True))
        form.addRow("", self.categorize)
        layout.addWidget(box)

        behaviour = QGroupBox("Behaviour")
        behaviour_form = QFormLayout(behaviour)
        behaviour_form.setSpacing(9)

        self.autostart = QCheckBox("Start downloads as soon as they are added")
        self.autostart.setChecked(self.settings.get_bool("autostart_downloads", True))
        behaviour_form.addRow("", self.autostart)

        # Registered with the session itself — the Run key on Windows, an XDG
        # autostart entry on Linux, a LaunchAgent on macOS. Always minimised:
        # a window that opens by itself at every login is why this gets turned
        # off again.
        self.launch_at_startup = QCheckBox(
            "Launch when I sign in, minimised to the tray"
        )
        self.launch_at_startup.setChecked(
            self.settings.get_bool("launch_at_startup", False)
        )
        self.launch_at_startup.setToolTip(
            "The engine is up and the tray icon is there, with no window "
            "until you ask for one — so a download started from the browser "
            "does not have to wait for the application to be launched."
        )
        behaviour_form.addRow("", self.launch_at_startup)

        self.minimize_tray = QCheckBox("Minimise to the system tray")
        self.minimize_tray.setChecked(self.settings.get_bool("minimize_to_tray", True))
        behaviour_form.addRow("", self.minimize_tray)

        self.close_tray = QCheckBox("Keep running in the tray when the window is closed")
        self.close_tray.setChecked(self.settings.get_bool("close_to_tray", False))
        behaviour_form.addRow("", self.close_tray)

        self.notify = QCheckBox("Show a notification when a download finishes")
        self.notify.setChecked(self.settings.get_bool("notify_on_complete", True))
        behaviour_form.addRow("", self.notify)
        layout.addWidget(behaviour)

        # The log is both the instrument every fault is diagnosed with and a
        # record of what was downloaded and from where. Whether to keep one is
        # therefore the user's decision, and it was not offered anywhere.
        log_box = QGroupBox("Log")
        log_form = QFormLayout(log_box)
        log_form.setSpacing(9)

        self.keep_log = QCheckBox("Keep a log of what happens")
        self.keep_log.setChecked(self.settings.get_bool("keep_log", True))
        self.keep_log.setToolTip(
            "The Log window in the toolbar holds both halves — the engine's "
            "and the browser extension's, in order — and it is what a bug "
            "report needs.\n\nTurning it off writes nothing and clears what "
            "is already there."
        )
        log_form.addRow("", self.keep_log)

        self.clear_log_on_launch = QCheckBox("Start each launch with an empty log")
        self.clear_log_on_launch.setChecked(
            self.settings.get_bool("clear_log_on_launch", True))
        log_form.addRow("", self.clear_log_on_launch)

        log_note = QLabel(
            "With the log off, a report about something not working cannot be "
            "answered — switch it on, reproduce the problem once, then copy it.")
        log_note.setObjectName("Muted")
        log_note.setWordWrap(True)
        log_form.addRow("", log_note)
        self.keep_log.toggled.connect(self.clear_log_on_launch.setEnabled)
        self.clear_log_on_launch.setEnabled(self.keep_log.isChecked())
        layout.addWidget(log_box)

        appearance = QGroupBox("Appearance")
        appearance_form = QFormLayout(appearance)
        self.accent_edit = QLineEdit(self.settings.get("accent", "#5B8CFF"))
        self.accent_edit.setPlaceholderText("#5B8CFF")
        appearance_form.addRow("Accent colour", self.accent_edit)
        note = QLabel("Restart the application to apply a new accent colour.")
        note.setObjectName("Muted")
        appearance_form.addRow("", note)
        layout.addWidget(appearance)

        layout.addStretch(1)
        return page

    def _browse_download_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Default download folder", self.download_dir.text()
        )
        if directory:
            self.download_dir.setText(directory)

    # -- Transfers ------------------------------------------------------
    def _build_transfers(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        box = QGroupBox("Concurrency")
        form = QFormLayout(box)
        form.setSpacing(9)

        self.max_concurrent = QSpinBox()
        self.max_concurrent.setRange(1, 32)
        self.max_concurrent.setValue(self.settings.get_int("max_concurrent_downloads", 4))
        form.addRow("Simultaneous downloads", self.max_concurrent)

        self.connections = QSpinBox()
        self.connections.setRange(1, 64)
        self.connections.setValue(self.settings.get_int("connections_per_download", 8))
        self.connections.setToolTip(
            "How many connections one download uses.\n\n"
            "YouTube's adaptive streams have no byte ranges to divide — the "
            "client asks a streaming endpoint for media rather than for bytes "
            "— so for those this is the number of streaming sessions run at "
            "once, each on its own stretch of the file. Same request, "
            "expressed in the only terms that protocol has."
        )
        form.addRow("Connections per download", self.connections)

        self.dynamic_chunking = QCheckBox(
            "Dynamic chunking — idle connections steal work from the slowest one"
        )
        self.dynamic_chunking.setChecked(self.settings.get_bool("dynamic_chunking", True))
        form.addRow("", self.dynamic_chunking)

        self.min_chunk = QSpinBox()
        self.min_chunk.setRange(64, 1024 * 64)
        self.min_chunk.setSuffix(" KB")
        self.min_chunk.setValue(self.settings.get_int("min_chunk_size", 1 << 20) // 1024)
        form.addRow("Minimum chunk size", self.min_chunk)

        # No separate limit for media segments. Segments and byte ranges are
        # both "pieces this download may fetch at once", and a second control
        # for one of them meant "Connections per download" was quietly
        # overridden on every HLS and DASH site.
        layout.addWidget(box)

        limits = QGroupBox("Bandwidth")
        limits_form = QFormLayout(limits)
        limits_form.setSpacing(9)

        self.global_limit = QSpinBox()
        self.global_limit.setRange(0, 1024 * 1024)
        self.global_limit.setSuffix(" KB/s")
        self.global_limit.setSpecialValueText("unlimited")
        self.global_limit.setValue(_kib(self.settings.get_int("global_speed_limit", 0)))
        limits_form.addRow("Global speed limit", self.global_limit)

        self.per_download_limit = QSpinBox()
        self.per_download_limit.setRange(0, 1024 * 1024)
        self.per_download_limit.setSuffix(" KB/s")
        self.per_download_limit.setSpecialValueText("unlimited")
        self.per_download_limit.setValue(
            _kib(self.settings.get_int("per_download_speed_limit", 0))
        )
        limits_form.addRow("Per-download limit", self.per_download_limit)

        note = QLabel(
            "Scheduled windows override the global limit while they are active."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        limits_form.addRow("", note)
        layout.addWidget(limits)

        reliability = QGroupBox("Reliability")
        reliability_form = QFormLayout(reliability)
        reliability_form.setSpacing(9)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(int(self.settings.get_float("socket_timeout", 30.0)))
        reliability_form.addRow("Socket timeout", self.timeout_spin)

        self.retries_spin = QSpinBox()
        self.retries_spin.setRange(0, 50)
        self.retries_spin.setValue(self.settings.get_int("max_retries", 5))
        reliability_form.addRow("Retries per chunk", self.retries_spin)

        self.auto_verify = QCheckBox(
            "Validate server-advertised digests (Content-MD5 / Digest) automatically"
        )
        self.auto_verify.setChecked(self.settings.get_bool("auto_verify_headers", True))
        reliability_form.addRow("", self.auto_verify)
        layout.addWidget(reliability)

        layout.addStretch(1)
        return page

    # -- Proxies --------------------------------------------------------
    def _build_proxies(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        mode_box = QGroupBox("Routing policy")
        mode_form = QFormLayout(mode_box)
        self.proxy_mode = QComboBox()
        self.proxy_mode.addItem("Direct connection (no proxy)", "none")
        self.proxy_mode.addItem("Use the system proxy", "system")
        self.proxy_mode.addItem("Use one selected proxy", "single")
        self.proxy_mode.addItem("Rotate through the proxy list", "rotate")
        index = self.proxy_mode.findData(self.settings.get("proxy_mode", "none"))
        self.proxy_mode.setCurrentIndex(max(0, index))
        self.proxy_mode.currentIndexChanged.connect(self._on_proxy_mode_changed)
        mode_form.addRow("Mode", self.proxy_mode)

        system_row = QHBoxLayout()
        self.system_proxy_label = QLabel("")
        self.system_proxy_label.setObjectName("Muted")
        self.system_proxy_label.setWordWrap(True)
        redetect = QPushButton("Re-detect")
        redetect.setToolTip("Read the operating system's proxy settings again")
        redetect.clicked.connect(self._refresh_system_proxy)
        system_row.addWidget(self.system_proxy_label, 1)
        system_row.addWidget(redetect)
        mode_form.addRow("Detected", system_row)

        self.rotate_on_error = QCheckBox(
            "Rotate automatically on 403 / 429 / connection failures"
        )
        self.rotate_on_error.setChecked(self.settings.get_bool("proxy_rotate_on_error", True))
        mode_form.addRow("", self.rotate_on_error)

        self.max_failures = QSpinBox()
        self.max_failures.setRange(1, 50)
        self.max_failures.setValue(self.settings.get_int("proxy_max_failures", 3))
        mode_form.addRow("Retire a proxy after", self.max_failures)
        layout.addWidget(mode_box)

        self.proxy_table = QTableWidget(0, 5)
        self.proxy_table.setHorizontalHeaderLabels(
            ["Label", "Protocol", "Address", "Auth", "Failures"]
        )
        self.proxy_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.proxy_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.proxy_table.verticalHeader().setVisible(False)
        self.proxy_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.proxy_table, 1)

        actions = QHBoxLayout()
        for label, handler in (
            ("Add", self._add_proxy), ("Edit", self._edit_proxy),
            ("Remove", self._remove_proxy), ("Test", self._test_proxy),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        self.proxy_status = QLabel("")
        self.proxy_status.setObjectName("Muted")
        actions.addWidget(self.proxy_status)
        layout.addLayout(actions)

        self._reload_proxies()
        self._refresh_system_proxy()
        return page

    def _reload_proxies(self) -> None:
        proxies = self.service.list_proxies()
        self.proxy_table.setRowCount(len(proxies))
        for row, proxy in enumerate(proxies):
            values = [
                proxy.label or proxy.host,
                proxy.scheme.value,
                f"{proxy.host}:{proxy.port}",
                "yes" if proxy.username else "no",
                str(proxy.fail_count),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, proxy.id)
                if not proxy.enabled:
                    item.setForeground(Qt.GlobalColor.gray)
                self.proxy_table.setItem(row, column, item)

    def _selected_proxy(self) -> ProxyEntry | None:
        row = self.proxy_table.currentRow()
        if row < 0:
            return None
        item = self.proxy_table.item(row, 0)
        proxy_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if proxy_id is None:
            return None
        return self.service.db.get_proxy(int(proxy_id))

    def _select_proxy_row(self, proxy_id: int | None) -> None:
        """Put the cursor on a proxy, so the buttons beside it act on it.

        Nothing was selected after an entry was added, and Edit, Remove and
        Test all begin by asking for the selection and returning quietly when
        there is none. So a newly added proxy sat in the table with every
        button that could act on it doing nothing — reported as the proxy not
        being added and not being testable.
        """
        for row in range(self.proxy_table.rowCount()):
            item = self.proxy_table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == proxy_id:
                self.proxy_table.selectRow(row)
                return
        if self.proxy_table.rowCount():
            self.proxy_table.selectRow(self.proxy_table.rowCount() - 1)

    def _require_selection(self) -> ProxyEntry | None:
        """The selected proxy, or a word about why nothing happened."""
        proxy = self._selected_proxy()
        if proxy is None:
            QMessageBox.information(
                self, "Proxy",
                "Select a proxy in the list first — this acts on the one that "
                "is highlighted.")
        return proxy

    def _add_proxy(self) -> None:
        dialog = ProxyDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            proxy = dialog.result_proxy()
            if not proxy.host:
                QMessageBox.warning(self, "Proxy", "A host is required.")
                return
            proxy_id = self.service.save_proxy(proxy)
            self._reload_proxies()
            self._select_proxy_row(proxy_id)
            # Saved and unused is a state worth naming: the routing mode
            # decides whether anything is routed through it at all, and a proxy
            # added while the mode is "none" changes nothing about a download.
            if self.proxy_mode.currentData() == "none":
                self.proxy_status.setText(
                    "Saved. Routing is set to “none”, so downloads still go "
                    "direct — change Mode above to use it.")
            else:
                self.proxy_status.setText("Saved. Select it and press Test to "
                                          "check it answers.")

    def _edit_proxy(self) -> None:
        proxy = self._require_selection()
        if proxy is None:
            return
        dialog = ProxyDialog(proxy, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.save_proxy(dialog.result_proxy())
            self._reload_proxies()
            self._select_proxy_row(proxy.id)

    def _remove_proxy(self) -> None:
        proxy = self._require_selection()
        if proxy is None or proxy.id is None:
            return
        self.service.delete_proxy(proxy.id)
        self._reload_proxies()

    def _test_proxy(self) -> None:
        proxy = self._require_selection()
        if proxy is None:
            return
        self.proxy_status.setText("Testing…")
        worker = Worker(lambda: self.service.test_proxy(proxy), self)
        worker.succeeded.connect(
            lambda result: self.proxy_status.setText(
                ("✓ " if result[0] else "✕ ") + result[1]
            )
        )
        worker.failed.connect(lambda message: self.proxy_status.setText(f"✕ {message}"))
        self._worker = worker
        worker.start()

    def _on_proxy_mode_changed(self) -> None:
        is_system = self.proxy_mode.currentData() == "system"
        self.system_proxy_label.setEnabled(is_system)
        self.rotate_on_error.setEnabled(not is_system)

    def _refresh_system_proxy(self) -> None:
        """Show what the operating system is currently configured to use."""
        from ...core.system_proxy import detect

        try:
            detected = detect()
        except Exception as exc:  # noqa: BLE001 - surfaced, never fatal
            self.system_proxy_label.setText(f"Detection failed: {exc}")
            return

        if detected.configured:
            text = f"<b>{detected.proxy.as_url()}</b> — from {detected.source}"
            bypassed = [b for b in detected.bypass if b not in ("localhost", "127.0.0.1", "::1")]
            if bypassed:
                text += f"<br>Bypassing: {', '.join(bypassed[:8])}"
        else:
            text = f"No proxy configured ({detected.source})."
        if detected.note:
            text += f"<br>{detected.note}"
        self.system_proxy_label.setText(text)
        self._on_proxy_mode_changed()

    # -- Network --------------------------------------------------------
    def _build_network(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        box = QGroupBox("Interface binding")
        form = QFormLayout(box)
        form.setSpacing(9)

        interface_row = QHBoxLayout()
        self.interface_combo = QComboBox()
        self.interface_combo.currentIndexChanged.connect(self._on_interface_changed)
        rescan = QPushButton("Rescan")
        rescan.setToolTip("Look for adapters that appeared since this dialog opened")
        rescan.clicked.connect(lambda: self._reload_interfaces(self._current_interface()))
        interface_row.addWidget(self.interface_combo, 1)
        interface_row.addWidget(rescan)
        form.addRow("Bind all traffic to", interface_row)

        self.interface_custom = QLineEdit()
        self.interface_custom.setPlaceholderText("Adapter name or literal IP address")
        self.interface_custom.setVisible(False)
        form.addRow("", self.interface_custom)

        self._reload_interfaces(self.settings.get("network_interface", "") or "")

        hint = QLabel(
            "Adapters are detected automatically. “Automatic” follows the system's "
            "own routing; choosing an adapter forces every socket out through it, so "
            "picking a VPN interface keeps downloads inside the tunnel regardless of "
            "the default route. On Linux, binding by adapter name needs CAP_NET_RAW; "
            "without it the adapter's own address is used as the source address, "
            "which achieves the same thing for IPv4."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        form.addRow("", hint)
        layout.addWidget(box)

        http_box = QGroupBox("HTTP")
        http_form = QFormLayout(http_box)
        http_form.setSpacing(9)

        self.user_agent = QLineEdit(self.settings.get("user_agent", ""))
        http_form.addRow("User agent", self.user_agent)

        self.verify_tls = QCheckBox("Verify TLS certificates")
        self.verify_tls.setChecked(self.settings.get_bool("verify_tls", True))
        http_form.addRow("", self.verify_tls)

        self.ipv6 = QCheckBox("Allow IPv6")
        self.ipv6.setChecked(self.settings.get_bool("ipv6_enabled", True))
        http_form.addRow("", self.ipv6)
        layout.addWidget(http_box)

        layout.addStretch(1)
        return page

    # -- Queues ---------------------------------------------------------
    def _build_queues(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.queue_table = QTableWidget(0, 5)
        self.queue_table.setHorizontalHeaderLabels(
            ["Name", "Mode", "Concurrent", "Speed cap", "Interface"]
        )
        self.queue_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.queue_table, 1)

        actions = QHBoxLayout()
        for label, handler in (
            ("Add", self._add_queue), ("Edit", self._edit_queue),
            ("Remove", self._remove_queue),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._reload_queues()
        return page

    def _reload_queues(self) -> None:
        queues = self.service.list_queues()
        self.queue_table.setRowCount(len(queues))
        for row, queue in enumerate(queues):
            values = [
                queue.name,
                queue.mode.value,
                str(queue.max_concurrent),
                f"{_kib(queue.speed_limit)} KB/s" if queue.speed_limit else "unlimited",
                queue.network_interface or "default",
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, queue.id)
                self.queue_table.setItem(row, column, item)

    def _selected_queue(self) -> DownloadQueue | None:
        row = self.queue_table.currentRow()
        if row < 0:
            return None
        item = self.queue_table.item(row, 0)
        queue_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        return self.service.db.get_queue(int(queue_id)) if queue_id is not None else None

    def _add_queue(self) -> None:
        dialog = QueueDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.save_queue(dialog.result_queue())
            self._reload_queues()

    def _edit_queue(self) -> None:
        queue = self._selected_queue()
        if queue is None:
            return
        dialog = QueueDialog(queue, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.save_queue(dialog.result_queue())
            self._reload_queues()

    def _remove_queue(self) -> None:
        queue = self._selected_queue()
        if queue is None or queue.id is None:
            return
        confirm = QMessageBox.question(
            self, "Remove queue",
            f"Remove “{queue.name}”? Downloads in it are kept but become unassigned.",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.service.delete_queue(queue.id)
            self._reload_queues()

    # -- what a schedule actually runs ----------------------------------
    def _schedule_downloads(self) -> None:
        """Choose which downloads a schedule runs, and in what order."""
        schedule = self._selected_schedule()
        if schedule is None:
            QMessageBox.information(
                self, "Scheduler",
                "Select a schedule first — this chooses what that schedule "
                "runs.")
            return
        # `queue_id is None` is "All queues", not "no queue" — this used to
        # refuse the most ordinary schedule anybody makes. The one thing that
        # genuinely cannot be scheduled is a database with no queues in it.
        if not [q for q in self.service.list_queues() if q.id is not None]:
            QMessageBox.information(
                self, "Scheduler",
                "There are no queues yet, so there is nothing for a schedule "
                "to run. Add one on the Queues page first.")
            return
        dialog = ScheduleDownloadsDialog(self.service, schedule, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload_schedules()

    # -- Schedules ------------------------------------------------------
    def _build_schedules(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self.schedule_table = QTableWidget(0, 6)
        self.schedule_table.setHorizontalHeaderLabels(
            ["Name", "Days", "Window", "Actions", "Speed cap", "State"]
        )
        self.schedule_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.schedule_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.schedule_table.verticalHeader().setVisible(False)
        self.schedule_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.schedule_table, 1)

        actions = QHBoxLayout()
        for label, handler in (
            ("Add", self._add_schedule), ("Edit", self._edit_schedule),
            ("Remove", self._remove_schedule),
            ("Downloads and order…", self._schedule_downloads),
        ):
            button = QPushButton(label)
            button.clicked.connect(handler)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)

        # -- when it is all over -------------------------------------------
        # The reason to leave a queue running overnight is not having to come
        # back to it, so the end of the schedule belongs on the same page as
        # the start of it.
        done = QGroupBox("When every download has finished")
        done_form = QFormLayout(done)
        done_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.completion_combo = QComboBox()
        for action in CompletionAction:
            self.completion_combo.addItem(action.label, action.value)
        current = self.completion_combo.findData(
            self.settings.get("completion_action", "nothing"))
        self.completion_combo.setCurrentIndex(max(0, current))
        done_form.addRow("Then", self.completion_combo)

        self.completion_grace = QSpinBox()
        self.completion_grace.setRange(0, 3600)
        self.completion_grace.setSuffix(" s")
        self.completion_grace.setValue(
            self.settings.get_int("completion_grace_seconds", 60))
        done_form.addRow("Countdown first", self.completion_grace)

        note = QLabel(
            "It happens once and then switches itself back to “Do nothing”. "
            "A paused or unfinished download counts as work outstanding, so "
            "nothing will happen while one is waiting — and the countdown can "
            "be called off from the window while it runs."
        )
        note.setObjectName("Subtle")
        note.setWordWrap(True)
        done_form.addRow(note)
        layout.addWidget(done)

        self._reload_schedules()
        return page

    def _reload_schedules(self) -> None:
        rows = self.service.scheduler.status()
        self.schedule_table.setRowCount(len(rows))
        schedules = {s.id: s for s in self.service.list_schedules()}
        for row, entry in enumerate(rows):
            schedule = schedules.get(entry["id"])
            actions = (
                f"{schedule.action_start.value} → {schedule.action_end.value}"
                if schedule else "—"
            )
            state = "active now" if entry["active"] else f"next {entry['next_change']}"
            if not entry["enabled"]:
                state = "disabled"
            values = [
                entry["name"] or "Schedule",
                entry["days"],
                entry["window"],
                actions,
                f"{_kib(entry['speed_limit'])} KB/s" if entry["speed_limit"] else "—",
                state,
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, entry["id"])
                self.schedule_table.setItem(row, column, item)

    def _selected_schedule(self) -> Schedule | None:
        row = self.schedule_table.currentRow()
        if row < 0:
            return None
        item = self.schedule_table.item(row, 0)
        schedule_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if schedule_id is None:
            return None
        return next(
            (s for s in self.service.list_schedules() if s.id == int(schedule_id)), None
        )

    def _add_schedule(self) -> None:
        dialog = ScheduleDialog(self.service, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.save_schedule(dialog.result_schedule())
            self._reload_schedules()

    def _edit_schedule(self) -> None:
        schedule = self._selected_schedule()
        if schedule is None:
            return
        dialog = ScheduleDialog(self.service, schedule, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.save_schedule(dialog.result_schedule())
            self._reload_schedules()

    def _remove_schedule(self) -> None:
        schedule = self._selected_schedule()
        if schedule is None or schedule.id is None:
            return
        self.service.delete_schedule(schedule.id)
        self._reload_schedules()

    # -- Integration ----------------------------------------------------
    def _build_integration(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        browser = QGroupBox("Browser integration")
        form = QFormLayout(browser)
        form.setSpacing(9)

        self.browser_integration = QCheckBox("Accept downloads from the browser extension")
        self.browser_integration.setChecked(
            self.settings.get_bool("browser_integration", True)
        )
        form.addRow("", self.browser_integration)

        self.confirm_browser = QCheckBox(
            "Ask before each one — address, folder, and whether to start it now"
        )
        self.confirm_browser.setChecked(
            self.settings.get_bool("confirm_browser_downloads", True)
        )
        self.confirm_browser.setToolTip(
            "A link clicked in the browser opens a window first, with “Start "
            "download”, “Download later” into a queue, and Cancel. Unticked, "
            "it is queued and started without asking."
        )
        form.addRow("", self.confirm_browser)

        self.ipc_port = QSpinBox()
        self.ipc_port.setRange(1024, 65535)
        self.ipc_port.setValue(self.settings.get_int("ipc_port", 47615))
        form.addRow("Control port", self.ipc_port)

        self.integration_status = QLabel("")
        self.integration_status.setWordWrap(True)
        self.integration_status.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("Browsers", self.integration_status)

        install_row = QHBoxLayout()
        setup_button = QPushButton("Set up now")
        setup_button.setObjectName("Primary")
        setup_button.setToolTip(
            "Detect every installed browser and register the messaging host"
        )
        setup_button.clicked.connect(self._setup_integration)
        test_button = QPushButton("Test connection")
        test_button.clicked.connect(self._test_integration)
        folder_button = QPushButton("Open extension folder")
        folder_button.clicked.connect(self._open_extension_folder)
        install_row.addWidget(setup_button)
        install_row.addWidget(test_button)
        install_row.addWidget(folder_button)
        install_row.addStretch(1)
        form.addRow("", install_row)

        self.integration_note = QLabel(
            "Registration is automatic and happens at start-up: browsers are "
            "located wherever they are installed — including snap and flatpak "
            "packages, which keep their profiles somewhere else entirely — and "
            "the extension's identity is fixed in advance, so nothing has to be "
            "copied by hand. The only manual step is loading the extension "
            "itself the first time."
        )
        self.integration_note.setObjectName("Muted")
        self.integration_note.setWordWrap(True)
        form.addRow("", self.integration_note)
        self._reload_integration_status()
        layout.addWidget(browser)

        media = QGroupBox("Media extraction")
        media_form = QFormLayout(media)
        media_form.setSpacing(9)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(
            ["2160p", "1440p", "1080p", "720p", "480p", "360p"]
        )
        self.quality_combo.setCurrentText(
            self.settings.get("preferred_video_quality", "1080p")
        )
        media_form.addRow("Preferred quality", self.quality_combo)

        # At 60fps and above, one resolution is published twice — as WebM
        # (VP9/AV1) and as MP4 (H.264) — and nothing but bitrate separated
        # them, which on a real video meant a 0.3% difference chose a file 46%
        # larger. This is a tie-break only: a resolution offered in one
        # container is still downloaded in that one.
        self.container_combo = QComboBox()
        self._container_values = ["mp4", "webm", "any"]
        self.container_combo.addItems([
            "MP4 / H.264 — plays almost anywhere",
            "WebM / VP9 — smaller at the same quality",
            "Whichever has the higher bitrate",
        ])
        current = self.settings.get("preferred_video_container", "mp4")
        if current not in self._container_values:
            current = "any"
        self.container_combo.setCurrentIndex(self._container_values.index(current))
        media_form.addRow("Preferred container", self.container_combo)

        self.prefer_progressive = QCheckBox(
            "Prefer progressive streams (single file, no muxing needed)"
        )
        self.prefer_progressive.setChecked(
            self.settings.get_bool("prefer_progressive", True)
        )
        media_form.addRow("", self.prefer_progressive)

        self.po_token = QLineEdit(self.settings.get("youtube_po_token", ""))
        self.po_token.setPlaceholderText(
            "Usually unnecessary — the extension supplies this automatically"
        )
        media_form.addRow("YouTube PO token", self.po_token)

        self.visitor_data = QLineEdit(self.settings.get("youtube_visitor_data", ""))
        self.visitor_data.setPlaceholderText("Optional — pairs with the PO token")
        media_form.addRow("YouTube visitor data", self.visitor_data)

        token_note = QLabel(
            "Some sites gate their API behind a proof of origin generated by "
            "their own code in your browser. The extension reads it from the "
            "player's request and sends it along, so these fields are only "
            "needed when downloading without the extension. Note that a token "
            "does not lift a site's playback-rate ceiling — where a video is "
            "streamed to its own player rather than served as a file, it can "
            "only be obtained at the speed it plays."
        )
        token_note.setObjectName("Muted")
        token_note.setWordWrap(True)
        media_form.addRow("", token_note)
        layout.addWidget(media)

        layout.addStretch(1)
        return page

    # -- interface picker ----------------------------------------------
    #: Sentinel stored as the combo's data for the free-text entry.
    _CUSTOM_INTERFACE = "\x00custom"

    def _reload_interfaces(self, keep: str = "") -> None:
        """Rebuild the adapter list, annotated and with the default marked."""
        from ...core.net import default_route_interface, describe_interfaces

        self.interface_combo.blockSignals(True)
        self.interface_combo.clear()
        self.interface_combo.addItem("Automatic — follow system routing", "")

        default_name = default_route_interface()
        for entry in describe_interfaces():
            if entry.loopback:
                continue          # binding downloads to loopback is never useful
            bits = []
            if entry.addresses:
                bits.append(", ".join(entry.addresses[:2]))
            if not entry.up:
                bits.append("down")
            if entry.name == default_name:
                bits.append("default route")
            suffix = f"  ({' · '.join(bits)})" if bits else ""
            self.interface_combo.addItem(f"{entry.name}{suffix}", entry.name)

        self.interface_combo.addItem("Custom…", self._CUSTOM_INTERFACE)

        index = self.interface_combo.findData(keep) if keep else 0
        if index >= 0:
            self.interface_combo.setCurrentIndex(index)
            self.interface_custom.setVisible(False)
        elif keep:
            # A saved adapter that is not present right now (VPN down, say)
            # must not be silently discarded.
            self.interface_combo.setCurrentIndex(
                self.interface_combo.findData(self._CUSTOM_INTERFACE)
            )
            self.interface_custom.setText(keep)
            self.interface_custom.setVisible(True)
        self.interface_combo.blockSignals(False)

    def _on_interface_changed(self) -> None:
        custom = self.interface_combo.currentData() == self._CUSTOM_INTERFACE
        self.interface_custom.setVisible(custom)
        if custom:
            self.interface_custom.setFocus()

    def _current_interface(self) -> str:
        value = self.interface_combo.currentData()
        if value == self._CUSTOM_INTERFACE:
            return self.interface_custom.text().strip()
        return value or ""

    # -- browser integration -------------------------------------------
    def _reload_integration_status(self) -> None:
        from ... import integration

        rows = integration.status()
        if not rows:
            self.integration_status.setText(
                "No browser profiles were found on this machine."
            )
            return

        lines = []
        for row in rows:
            mark = "✔" if row["registered"] else "✕"
            colour = "#4ec9a0" if row["registered"] else "#8d97ad"
            extra = " · sandboxed" if row["sandboxed"] else ""
            lines.append(
                f"<span style='color:{colour}'>{mark}</span> {row['name']}"
                f"<span style='color:#8d97ad'>{extra}</span>"
            )
        identifier = integration.bundled_extension_id()
        lines.append(
            f"<span style='color:#8d97ad'>Extension ID: {identifier or 'unknown'}</span>"
        )
        self.integration_status.setText("<br>".join(lines))

    def _setup_integration(self) -> None:
        from ... import integration

        result = integration.install()
        self._reload_integration_status()

        box = QMessageBox(self)
        box.setWindowTitle("Browser integration")
        box.setIcon(
            QMessageBox.Icon.Information if result.ok else QMessageBox.Icon.Warning
        )
        if result.ok:
            box.setText("The native messaging host is registered.")
            box.setInformativeText(
                "One step is left, and only once: open your browser's extensions "
                "page, turn on Developer mode and choose “Load unpacked”, then "
                "select the folder below. The connection is established "
                "automatically from there — the extension already carries the "
                "matching key.\n\n"
                f"{integration.extension_dir()}"
            )
        else:
            box.setText("The integration could not be completed.")
        box.setDetailedText(result.render())
        box.exec()

    def _open_extension_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from ... import integration

        integration.sync_extension_manifest()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(integration.extension_dir())))

    def _test_integration(self) -> None:
        """Launch the host exactly as a browser would and report the reply."""
        import json
        import struct

        from ... import integration

        launcher = integration.launcher_path()
        if not launcher.exists():
            QMessageBox.warning(
                self, "Browser integration",
                "The host has not been registered yet — use “Set up now” first.",
            )
            return

        request = json.dumps({"id": 1, "command": "ping", "params": {}}).encode("utf-8")
        try:
            completed = subprocess.run(
                [str(launcher)], input=struct.pack("@I", len(request)) + request,
                capture_output=True, timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            QMessageBox.warning(self, "Browser integration",
                                f"The launcher could not be executed:\n{exc}")
            return

        output = completed.stdout
        if len(output) >= 4:
            (length,) = struct.unpack("@I", output[:4])
            try:
                response = json.loads(output[4:4 + length].decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                response = {}
            if response.get("ok") is True:
                result = response.get("result") or {}
                QMessageBox.information(
                    self, "Browser integration",
                    "The browser can reach this application.\n\n"
                    f"Host replied: version {result.get('version')} "
                    f"(process {result.get('pid')}).",
                )
                return

        detail = completed.stderr.decode("utf-8", "replace")[:800]
        QMessageBox.warning(
            self, "Browser integration",
            "The host did not answer.\n\n" + (detail or "No diagnostic output."),
        )

    # ------------------------------------------------------------------
    def _save(self) -> None:
        interface = self._current_interface()

        self.settings.update({
            "download_dir": self.download_dir.text().strip(),
            "categorize_into_subfolders": self.categorize.isChecked(),
            "autostart_downloads": self.autostart.isChecked(),
            "launch_at_startup": self.launch_at_startup.isChecked(),
            "minimize_to_tray": self.minimize_tray.isChecked(),
            "close_to_tray": self.close_tray.isChecked(),
            "notify_on_complete": self.notify.isChecked(),
            "keep_log": self.keep_log.isChecked(),
            "clear_log_on_launch": self.clear_log_on_launch.isChecked(),
            "accent": self.accent_edit.text().strip() or "#5B8CFF",

            "max_concurrent_downloads": self.max_concurrent.value(),
            "connections_per_download": self.connections.value(),
            "dynamic_chunking": self.dynamic_chunking.isChecked(),
            "min_chunk_size": self.min_chunk.value() * 1024,
            "global_speed_limit": _from_kib(self.global_limit.value()),
            "per_download_speed_limit": _from_kib(self.per_download_limit.value()),
            "socket_timeout": float(self.timeout_spin.value()),
            "max_retries": self.retries_spin.value(),
            "auto_verify_headers": self.auto_verify.isChecked(),

            "proxy_mode": self.proxy_mode.currentData(),
            "proxy_rotate_on_error": self.rotate_on_error.isChecked(),
            "proxy_max_failures": self.max_failures.value(),

            "network_interface": interface,
            "user_agent": self.user_agent.text().strip(),
            "verify_tls": self.verify_tls.isChecked(),
            "ipv6_enabled": self.ipv6.isChecked(),

            "browser_integration": self.browser_integration.isChecked(),
            "confirm_browser_downloads": self.confirm_browser.isChecked(),
            "ipc_port": self.ipc_port.value(),
            "preferred_video_quality": self.quality_combo.currentText(),
            "preferred_video_container":
                self._container_values[self.container_combo.currentIndex()],
            "prefer_progressive": self.prefer_progressive.isChecked(),
            "youtube_po_token": self.po_token.text().strip(),
            "youtube_visitor_data": self.visitor_data.text().strip(),
            "updates_check_automatically": self.updates_automatic.isChecked(),
            "updates_install_automatically": self.updates_install.isChecked(),
            "completion_action": self.completion_combo.currentData(),
            "completion_grace_seconds": self.completion_grace.value(),
        })

        self.service.engine.proxies.refresh()
        self.service.engine.global_limiter.set_rate(
            self.settings.get_int("global_speed_limit", 0)
        )
        self._apply_launch_at_startup()
        self.accept()

    def _apply_launch_at_startup(self) -> None:
        """Tell the session, and say so if it refuses.

        A checkbox that stores a preference and never reaches the registry or
        the autostart directory is a setting that appears to work and does
        nothing — so the failure is shown here rather than left in the log.
        """
        wanted = self.launch_at_startup.isChecked()
        try:
            from ...autostart import apply as apply_autostart

            apply_autostart(wanted)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.service.db.log_event(f"Launch at startup: {exc}", level="error")
            QMessageBox.warning(
                self, "Launch at startup",
                "The setting was saved, but this session refused the "
                f"registration:\n\n{exc}",
            )
