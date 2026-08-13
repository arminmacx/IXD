"""Threading helpers that keep blocking work off the Qt main thread."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from ..core.events import EventBus


class Worker(QThread):
    """Runs one callable on a background thread and reports the outcome."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, function: Callable[[], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._function = function

    def run(self) -> None:  # noqa: D102 - Qt naming
        try:
            result = self._function()
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.failed.emit(str(exc) or exc.__class__.__name__)
            return
        self.succeeded.emit(result)


class EventBridge(QObject):
    """Marshals :class:`EventBus` callbacks onto the Qt event loop.

    Engine events arrive on worker threads; Qt widgets may only be touched from
    the main thread.  Emitting a signal from the subscriber callback performs
    the hand-off, because a signal connected across threads is queued.
    """

    event = Signal(str, dict)

    def __init__(self, bus: EventBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bus = bus
        self._bus.subscribe(self._on_event)

    def _on_event(self, event_type: str, payload: dict) -> None:
        self.event.emit(event_type, payload)

    def detach(self) -> None:
        self._bus.unsubscribe(self._on_event)
