"""Threading helpers that keep blocking work off the Qt main thread."""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from ..core.events import EventBus

#: Calls that have not come back yet. A `BackgroundCall` keeps itself alive
#: until its function returns, so a dialog closed while it is in flight is
#: never emitted into — Qt drops a connection whose receiver has been
#: destroyed, and the sender is here rather than owned by the dialog.
_IN_FLIGHT: set["BackgroundCall"] = set()


class BackgroundCall(QObject):
    """One blocking call on a daemon thread, answered on the Qt event loop.

    Deliberately not a `QThread`. A QThread still running when it is destroyed
    **aborts the process** — measured here, SIGABRT, not a warning — and a
    probe is a socket read that nothing can interrupt, so quitting the
    application while one is in flight is an ordinary thing to do rather than a
    corner case. A daemon thread ends with the process and takes nothing down
    with it.

    Connect **bound methods**, not lambdas: Qt drops a connection when the
    receiving object is destroyed, and a bound method of a QObject is what
    tells it who the receiver is. A lambda has no receiver, so it would still
    be called — into a dialog that is gone.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    done = Signal()

    def __init__(self, function: Callable[[], Any]) -> None:
        super().__init__()
        self._function = function
        self.done.connect(self._release)

    def start(self) -> None:
        _IN_FLIGHT.add(self)
        threading.Thread(target=self._run, name="ixd-call", daemon=True).start()

    def _run(self) -> None:
        try:
            result = self._function()
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.succeeded.emit(result)
        finally:
            # Queued: this object lives on the main thread, so the release
            # happens there and never races the delivery of the two above.
            self.done.emit()

    def _release(self) -> None:
        _IN_FLIGHT.discard(self)


class Worker(QThread):
    """Runs one callable on a background thread and reports the outcome.

    Kept for the callers whose work is bounded and whose window is modal.
    Anything that can still be running when the application quits should use
    :class:`BackgroundCall` instead — see the note there about SIGABRT.
    """

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
