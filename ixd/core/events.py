"""A tiny thread-safe publish/subscribe bus.

The engine runs on worker threads while the UI lives on the Qt main thread, so
the bus is deliberately dumb: it only fans out callbacks.  The Qt layer wraps
its subscription in a queued signal to hop threads safely.
"""

from __future__ import annotations

import threading
import traceback
from collections import defaultdict
from typing import Any, Callable

Listener = Callable[[str, dict[str, Any]], None]


class EventType:
    """Well-known event names (plain strings keep the bus dependency-free)."""

    DOWNLOAD_ADDED = "download.added"
    DOWNLOAD_UPDATED = "download.updated"
    DOWNLOAD_PROGRESS = "download.progress"
    DOWNLOAD_COMPLETED = "download.completed"
    DOWNLOAD_FAILED = "download.failed"
    DOWNLOAD_REMOVED = "download.removed"
    DOWNLOAD_NEEDS_LINK = "download.needs_link"
    DOWNLOAD_VERIFIED = "download.verified"
    CHUNKS_CHANGED = "download.chunks"
    QUEUE_CHANGED = "queue.changed"
    SCHEDULE_FIRED = "schedule.fired"
    #: Everything has finished and a completion action is counting down. The
    #: payload carries the action, the seconds left and why it fired, so the
    #: window can offer to call it off — an action nobody can stop is not one
    #: to give a machine.
    COMPLETION_ARMED = "completion.armed"
    COMPLETION_CANCELLED = "completion.cancelled"
    COMPLETION_FIRED = "completion.fired"
    #: A newer version has been published. Carries the version, the notes and
    #: whether this build is one that can replace itself.
    UPDATE_AVAILABLE = "update.available"
    PROXY_ROTATED = "proxy.rotated"
    ENGINE_STATS = "engine.stats"
    LOG = "engine.log"


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._global: list[Listener] = []

    def subscribe(self, listener: Listener, event_type: str | None = None) -> None:
        with self._lock:
            if event_type is None:
                self._global.append(listener)
            else:
                self._listeners[event_type].append(listener)

    def unsubscribe(self, listener: Listener, event_type: str | None = None) -> None:
        with self._lock:
            if event_type is None:
                if listener in self._global:
                    self._global.remove(listener)
                for listeners in self._listeners.values():
                    if listener in listeners:
                        listeners.remove(listener)
            elif listener in self._listeners.get(event_type, []):
                self._listeners[event_type].remove(listener)

    def emit(self, event_type: str, **payload: Any) -> None:
        with self._lock:
            listeners = list(self._listeners.get(event_type, ())) + list(self._global)
        for listener in listeners:
            try:
                listener(event_type, payload)
            except Exception:
                # A subscriber must never be able to kill a worker thread.
                traceback.print_exc()

    def clear(self) -> None:
        with self._lock:
            self._listeners.clear()
            self._global.clear()
