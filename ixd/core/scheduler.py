"""Time-based automation: queue windows and time-of-day bandwidth caps.

A :class:`Schedule` describes a recurring window (days-of-week + start/end
time).  Crossing into the window fires ``action_start``; leaving fires
``action_end``.  While inside, the schedule's ``speed_limit`` overrides the
global cap, which is what makes "2 MB/s during work hours, uncapped overnight"
a single declarative rule.

Windows that cross midnight (the common 02:00→06:00 case) are handled by
attributing the window to the day it *starts* on.
"""

from __future__ import annotations

import datetime
import threading
from typing import TYPE_CHECKING

from .events import EventBus, EventType
from .models import Schedule, ScheduleAction

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from .db import Database
    from .engine import DownloadEngine

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ALL_DAYS = 0b1111111
WEEKDAYS = 0b0011111
WEEKENDS = 0b1100000


def parse_time(value: str) -> int:
    """``"02:30"`` → minutes since midnight."""
    try:
        hours, _, minutes = value.strip().partition(":")
        total = int(hours) * 60 + int(minutes or 0)
    except (ValueError, AttributeError):
        return 0
    return max(0, min(24 * 60 - 1, total))


def format_days(mask: int) -> str:
    if mask == ALL_DAYS:
        return "Every day"
    if mask == WEEKDAYS:
        return "Weekdays"
    if mask == WEEKENDS:
        return "Weekends"
    days = [WEEKDAY_NAMES[i] for i in range(7) if mask & (1 << i)]
    return ", ".join(days) if days else "Never"


def window_contains(schedule: Schedule, moment: datetime.datetime) -> bool:
    """Is ``moment`` inside this schedule's recurring window?"""
    start = parse_time(schedule.start_time)
    end = parse_time(schedule.end_time)
    weekday = moment.weekday()
    minutes = moment.hour * 60 + moment.minute

    if start == end:
        return False
    if start < end:
        return schedule.covers_day(weekday) and start <= minutes < end

    # Crosses midnight: the window belongs to the day it started on.
    if schedule.covers_day(weekday) and minutes >= start:
        return True
    previous_day = (weekday - 1) % 7
    return schedule.covers_day(previous_day) and minutes < end


def next_transition(schedule: Schedule, moment: datetime.datetime) -> datetime.datetime | None:
    """When this schedule next changes state — used for UI countdowns."""
    for offset in range(0, 8 * 24 * 60, 1):
        candidate = moment + datetime.timedelta(minutes=offset)
        if window_contains(schedule, candidate) != window_contains(schedule, moment):
            return candidate.replace(second=0, microsecond=0)
    return None


class Scheduler:
    """Evaluates schedules on a timer and drives the engine accordingly."""

    def __init__(self, db: "Database", settings: "Settings", engine: "DownloadEngine",
                 events: EventBus | None = None, tick_seconds: float = 15.0) -> None:
        self.db = db
        self.settings = settings
        self.engine = engine
        self.events = events or engine.events
        self.tick_seconds = tick_seconds

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active: dict[int, bool] = {}      # schedule id -> was inside the window
        self._applied_limit: int | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        # Seed current state so a restart does not re-fire past transitions.
        now = datetime.datetime.now()
        with self._lock:
            self._active = {
                s.id: window_contains(s, now)
                for s in self.db.list_schedules() if s.id is not None
            }
        self._thread = threading.Thread(target=self._loop, name="ixd-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _loop(self) -> None:
        # Apply speed windows immediately rather than waiting a full tick.
        self._apply_speed_windows(datetime.datetime.now())
        while not self._stop.wait(self.tick_seconds):
            try:
                self.tick()
            except Exception:
                import traceback
                traceback.print_exc()

    # ------------------------------------------------------------------
    def tick(self, now: datetime.datetime | None = None) -> None:
        """Evaluate every schedule once. Safe to call manually (and in tests)."""
        moment = now or datetime.datetime.now()
        schedules = [s for s in self.db.list_schedules() if s.enabled and s.id is not None]

        for schedule in schedules:
            inside = window_contains(schedule, moment)
            with self._lock:
                previously = self._active.get(schedule.id)
                self._active[schedule.id] = inside
            if previously is None or previously == inside:
                continue
            action = schedule.action_start if inside else schedule.action_end
            self._fire(schedule, action, inside)

        self._apply_speed_windows(moment)

    def _fire(self, schedule: Schedule, action: ScheduleAction, entering: bool) -> None:
        if action is ScheduleAction.NOTHING:
            return

        targets = (
            [schedule.queue_id] if schedule.queue_id is not None
            else [queue.id for queue in self.db.list_queues()]
        )
        for queue_id in targets:
            if queue_id is None:
                continue
            if action is ScheduleAction.START:
                self.engine.start_queue(queue_id)
            elif action is ScheduleAction.PAUSE:
                self.engine.pause_queue(queue_id)
            elif action is ScheduleAction.STOP:
                self.engine.stop_queue(queue_id)

        label = schedule.name or f"schedule {schedule.id}"
        edge = "started" if entering else "ended"
        message = f"{label} {edge}: {action.value} on {len(targets)} queue(s)"
        self.db.log_event(message)
        self.events.emit(
            EventType.SCHEDULE_FIRED,
            schedule_id=schedule.id,
            action=action.value,
            entering=entering,
            message=message,
        )

    def _apply_speed_windows(self, moment: datetime.datetime) -> None:
        """The tightest active window wins; otherwise fall back to the setting."""
        limits = [
            schedule.speed_limit
            for schedule in self.db.list_schedules()
            if schedule.enabled and schedule.speed_limit > 0
            and window_contains(schedule, moment)
        ]
        target = min(limits) if limits else self.settings.get_int("global_speed_limit", 0)

        if target != self._applied_limit:
            self._applied_limit = target
            self.engine.global_limiter.set_rate(target)
            self.events.emit(
                EventType.ENGINE_STATS, active=-1, speed=0.0, limit=target, proxy=""
            )
            self.db.log_event(
                f"Bandwidth cap now {target} B/s"
                + (" (scheduled window)" if limits else " (default)")
            )

    # ------------------------------------------------------------------
    def status(self, now: datetime.datetime | None = None) -> list[dict]:
        """Snapshot for the settings UI: which windows are live and what's next."""
        moment = now or datetime.datetime.now()
        rows = []
        for schedule in self.db.list_schedules():
            inside = window_contains(schedule, moment)
            upcoming = next_transition(schedule, moment)
            rows.append({
                "id": schedule.id,
                "name": schedule.name,
                "queue_id": schedule.queue_id,
                "days": format_days(schedule.days_mask),
                "window": f"{schedule.start_time}–{schedule.end_time}",
                "active": inside,
                "enabled": schedule.enabled,
                "speed_limit": schedule.speed_limit,
                "next_change": upcoming.strftime("%a %H:%M") if upcoming else "—",
            })
        return rows
