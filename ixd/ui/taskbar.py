"""Download progress on the application's own icon, per platform.

A download manager's icon is the one thing on screen while its window is not,
and every desktop has somewhere to draw progress on it. None of them agree on
how, and Qt exposes none of them: ``QWinTaskbarProgress`` was Qt 5 and did not
survive into Qt 6. So each is spoken to directly, and each degrades to doing
nothing rather than to an error.

======= ====================================================================
Windows ``ITaskbarList3::SetProgressValue`` — the green fill behind the
        taskbar button. COM, reached through ``ctypes``; no pywin32.
Linux   ``com.canonical.Unity.LauncherEntry``, a D-Bus signal. KDE Plasma,
        Unity and GNOME's Dash-to-Dock all draw it; anything else ignores an
        unheard signal, which costs nothing.
macOS   the dock badge, through ``QGuiApplication.setBadgeNumber``. A number
        rather than a bar — the dock has no progress bar to set without an
        Objective-C dock tile — and it is honest about being a percentage.
======= ====================================================================

Nothing here is required for a download to work, so every call is wrapped: a
desktop that refuses is a desktop with no progress on its icon, not a fault.
"""

from __future__ import annotations

import sys

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

#: The application's own D-Bus/desktop identity. The launcher protocol keys
#: everything on it, and a name no `.desktop` file matches is drawn on nothing.
_DESKTOP_ID = "application://ixd.desktop"


class TaskbarProgress:
    """Progress on the taskbar icon, or silently nothing.

    One instance, told a fraction between 0 and 1 and whether to show anything
    at all. It remembers what it last drew: these are cross-process messages,
    and sending an identical one sixty times a minute is waste that a desktop
    can actually feel.
    """

    def __init__(self) -> None:
        self._last: tuple[bool, int, tuple[int, ...]] | None = None
        self._window = None
        self._windows = _WindowsTaskbar() if IS_WINDOWS else None
        self._unity = _UnityLauncher() if IS_LINUX else None

    def set_progress(self, fraction: float, visible: bool = True) -> None:
        """Draw ``fraction`` (0–1), or clear it when ``visible`` is false."""
        percent = max(0, min(100, int(round(fraction * 100))))
        handles = self._handles() if self._windows is not None else ()
        # The set of windows is part of the state, not just the number: a
        # download window that opens mid-transfer is a new taskbar button, and
        # skipping the draw because the percentage had not moved left it blank.
        state = (bool(visible), percent, handles)
        if state == self._last:
            return
        self._last = state

        if self._windows is not None:
            self._windows.set_progress(percent, visible, handles)
        if self._unity is not None:
            self._unity.set_progress(percent, visible)
        if IS_MACOS:
            _set_dock_badge(percent, visible)

    def set_indeterminate(self) -> None:
        """Something is running, but nobody published a length.

        A download whose size the server never states is the ordinary case for
        segmented and server-driven media, and it used to clear the bar
        entirely — indistinguishable from nothing running at all. Windows has a
        state for exactly this; the Linux launcher entry does not, so there it
        stays hidden rather than showing a made-up fraction.
        """
        handles = self._handles() if self._windows is not None else ()
        state = ("indeterminate", handles)
        if state == self._last:
            return
        self._last = state
        if self._windows is not None:
            self._windows.set_indeterminate(handles)
        if self._unity is not None:
            self._unity.set_progress(0, False)

    def clear(self) -> None:
        self.set_progress(0.0, visible=False)

    def attach(self, window) -> None:
        """Remember the main window, as the handle of last resort."""
        self._window = window

    def diagnostic(self) -> str:
        """What the platform backend did or refused to do, for the log.

        Reported on success as well as on failure. Silence was the problem:
        "the bar does not show on Windows" arrived twice with nothing in the
        log either time, because a backend that believed it had succeeded said
        nothing at all. The message carries no percentage, so it changes when
        the situation changes rather than on every update.
        """
        if self._windows is not None:
            return self._windows.diagnostic()
        return ""

    def _handles(self) -> tuple[int, ...]:
        """Every window that currently has a taskbar button of its own.

        Windows draws progress on a *button*, and a window that is not on the
        taskbar has none — so setting it on the main window's handle drew
        nothing whenever that window was hidden, which is exactly the case the
        feature exists for: the browser starts the application hidden, the
        download window opens on its own, and the bar belongs on that. Linux
        never showed this because its launcher signal names the application
        rather than a window.

        Top-level and visible is the test. A parented dialog has no button and
        the call against it is a harmless no-op, so the filter *excludes* the
        few kinds that are never windows in their own right rather than listing
        the kinds that are — a window whose type is not on a list of expected
        ones is a window this would silently refuse to draw on.
        """
        handles: list[int] = []
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QApplication

            never = (Qt.WindowType.Popup, Qt.WindowType.ToolTip,
                     Qt.WindowType.SplashScreen, Qt.WindowType.Desktop)
            for widget in QApplication.topLevelWidgets():
                if not widget.isWindow() or not widget.isVisible():
                    continue
                if widget.windowType() in never:
                    continue
                handle = int(widget.winId())
                if handle and handle not in handles:
                    handles.append(handle)
        except Exception:      # noqa: BLE001 - decoration, never fatal
            pass

        if not handles and self._window is not None:
            try:
                handle = int(self._window.winId())
                if handle:
                    handles.append(handle)
            except Exception:  # noqa: BLE001
                pass
        return tuple(handles)


def _set_dock_badge(percent: int, visible: bool) -> None:
    """macOS: the dock badge, which Qt sets natively.

    Not a bar. The dock draws a progress bar only for a dock tile with an
    NSProgressIndicator in it, which is Objective-C and cannot be reached from
    here without one. A percentage in the badge is what this platform can
    honestly show, and it is visible from the same glance.
    """
    try:
        from PySide6.QtGui import QGuiApplication

        application = QGuiApplication.instance()
        if application is None:
            return
        application.setBadgeNumber(percent if visible else 0)
    except Exception:      # noqa: BLE001 - decoration, never fatal
        pass


class _UnityLauncher:
    """Linux: the launcher-entry signal KDE, Unity and Dash-to-Dock listen for.

    A signal on the session bus, not a method call, so nothing has to be
    listening and nothing fails when nothing is.
    """

    def __init__(self) -> None:
        self._connection = None
        try:
            from PySide6.QtDBus import QDBusConnection

            connection = QDBusConnection.sessionBus()
            if connection.isConnected():
                self._connection = connection
        except Exception:      # noqa: BLE001 - no session bus is not a fault
            self._connection = None

    def set_progress(self, percent: int, visible: bool) -> None:
        if self._connection is None:
            return
        try:
            from PySide6.QtDBus import QDBusMessage

            message = QDBusMessage.createSignal(
                "/com/canonical/Unity/LauncherEntry",
                "com.canonical.Unity.LauncherEntry",
                "Update",
            )
            message.setArguments([
                _DESKTOP_ID,
                {"progress": percent / 100.0, "progress-visible": bool(visible)},
            ])
            self._connection.send(message)
        except Exception:      # noqa: BLE001
            pass


class _WindowsTaskbar:
    """Windows: ``ITaskbarList3``, through ctypes rather than a dependency.

    The interface is obtained once and kept, and the progress is drawn on every
    window handed to `set_progress` — the taskbar draws on a *button*, and which
    of this application's windows owns one depends on what is open.

    Every HRESULT is checked by hand rather than through ``ctypes.oledll``,
    which raises on any failure code and would have turned a first call made a
    moment too early into a permanent, silent nothing. What went wrong is kept
    in `diagnostic` so a field report can say so instead of guessing.
    """

    #: CLSID_TaskbarList and IID_ITaskbarList3, as the registry knows them.
    _CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
    _IID = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

    #: `SetProgressState` flags. NORMAL is the ordinary green fill; NOPROGRESS
    #: removes it; INDETERMINATE is the marquee, for a transfer whose length
    #: nobody published; PAUSED is the yellow one.
    _NOPROGRESS = 0x0
    _INDETERMINATE = 0x1
    _NORMAL = 0x2
    _PAUSED = 0x8

    #: COM apartments. Qt has already initialised this thread as an STA, so
    #: `CoInitializeEx` answers S_FALSE — a success. RPC_E_CHANGED_MODE means
    #: somebody asked for the other kind first, and the apartment that already
    #: exists is still one this interface can be created in.
    _COINIT_APARTMENTTHREADED = 0x2
    _S_FALSE = 1
    _RPC_E_CHANGED_MODE = -2147417850          # 0x80010106

    #: A first attempt can lose to a shell that is not ready yet, so it is
    #: retried rather than written off. Not forever: a machine without a
    #: taskbar service must stop paying for the attempt.
    _MAX_ATTEMPTS = 5

    def __init__(self) -> None:
        self._taskbar = None
        self._attempts = 0
        self._error = ""

    def diagnostic(self) -> str:
        return self._error

    def _ensure(self) -> bool:
        if self._taskbar is not None:
            return True
        if self._attempts >= self._MAX_ATTEMPTS:
            return False
        self._attempts += 1
        try:
            import ctypes
            from ctypes import POINTER, byref, c_void_p
            from ctypes.wintypes import HWND

            # `SetProgressValue` takes two ULONGLONGs, and `ctypes.wintypes`
            # does **not** define ULONGLONG — it has ULARGE_INTEGER and ULONG
            # and nothing named that. Importing it raised ImportError, the
            # blanket `except` here swallowed it, and the whole feature was
            # dead on Windows while every other platform worked. Found from a
            # user's Log, which is the only reason it was findable at all.
            ULONGLONG = ctypes.c_ulonglong

            ole32 = ctypes.windll.ole32

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_uint32),
                            ("Data2", ctypes.c_uint16),
                            ("Data3", ctypes.c_uint16),
                            ("Data4", ctypes.c_ubyte * 8)]

            def guid(text: str) -> GUID:
                value = GUID()
                if ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(value)) < 0:
                    raise OSError(f"CLSIDFromString({text}) failed")
                return value

            initialised = ole32.CoInitializeEx(None, self._COINIT_APARTMENTTHREADED)
            if initialised < 0 and initialised != self._RPC_E_CHANGED_MODE:
                self._error = f"CoInitializeEx failed (0x{initialised & 0xFFFFFFFF:08X})"
                return False

            pointer = c_void_p()
            created = ole32.CoCreateInstance(
                byref(guid(self._CLSID)), None,
                1,                              # CLSCTX_INPROC_SERVER
                byref(guid(self._IID)), byref(pointer),
            )
            if created < 0 or not pointer:
                self._error = f"CoCreateInstance failed (0x{created & 0xFFFFFFFF:08X})"
                return False

            # The vtable, by ordinal: IUnknown occupies 0–2, then
            # HrInit, AddTab, DeleteTab, ActivateTab, SetActiveAlt,
            # MarkFullscreenWindow, SetProgressValue, SetProgressState.
            vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
            self._call = ctypes.WINFUNCTYPE
            self._pointer = pointer
            self._vtable = vtable
            self._HWND = HWND
            self._ULONGLONG = ULONGLONG
            self._c_void_p = c_void_p

            hr_init = self._call(ctypes.c_long, c_void_p)(vtable[3])
            started = hr_init(pointer)
            if started < 0:
                self._error = f"ITaskbarList::HrInit failed (0x{started & 0xFFFFFFFF:08X})"
                return False

            self._taskbar = pointer
            self._error = ""
            return True
        except Exception as exc:      # noqa: BLE001 - no taskbar is not a fault
            self._taskbar = None
            self._error = f"taskbar progress unavailable: {exc}"
            return False

    def set_progress(self, percent: int, visible: bool,
                     handles: tuple[int, ...] = ()) -> None:
        self._draw(handles, self._NORMAL if visible else self._NOPROGRESS,
                   percent if visible else None)

    def set_indeterminate(self, handles: tuple[int, ...] = ()) -> None:
        self._draw(handles, self._INDETERMINATE, None)

    def _draw(self, handles: tuple[int, ...], state: int,
              percent: int | None) -> None:
        """Set one state on every window, and say what happened.

        The result goes in `_error` whether it worked or not. A backend that
        reports only its failures is indistinguishable from one that is not
        being called, which is the position two "the bar does not show"
        reports left this in.
        """
        if not handles:
            self._error = "no window with a taskbar button to draw on"
            return
        if not self._ensure():
            return
        try:
            import ctypes

            set_state = self._call(
                ctypes.c_long, self._c_void_p, self._HWND, ctypes.c_int
            )(self._vtable[10])
            set_value = self._call(
                ctypes.c_long,
                self._c_void_p, self._HWND, self._ULONGLONG, self._ULONGLONG
            )(self._vtable[9])

            results = []
            for handle in handles:
                window = self._HWND(handle)
                hr = set_state(self._pointer, window, state)
                if percent is not None:
                    hr2 = set_value(self._pointer, window,
                                    self._ULONGLONG(percent), self._ULONGLONG(100))
                    hr = hr if hr < 0 else hr2
                results.append(f"0x{handle:X}={'ok' if hr >= 0 else f'0x{hr & 0xFFFFFFFF:08X}'}")
            names = {self._NOPROGRESS: "clear", self._INDETERMINATE: "indeterminate",
                     self._NORMAL: "normal", self._PAUSED: "paused"}
            self._error = (f"ITaskbarList3 {names.get(state, state)} on "
                           f"{len(handles)} window(s): " + ", ".join(results))
        except Exception as exc:      # noqa: BLE001
            self._error = f"taskbar progress failed: {exc}"
