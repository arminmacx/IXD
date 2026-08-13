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
        self._last: tuple[bool, int] | None = None
        self._windows = _WindowsTaskbar() if IS_WINDOWS else None
        self._unity = _UnityLauncher() if IS_LINUX else None

    def set_progress(self, fraction: float, visible: bool = True) -> None:
        """Draw ``fraction`` (0–1), or clear it when ``visible`` is false."""
        percent = max(0, min(100, int(round(fraction * 100))))
        state = (bool(visible), percent)
        if state == self._last:
            return
        self._last = state

        if self._windows is not None:
            self._windows.set_progress(percent, visible)
        if self._unity is not None:
            self._unity.set_progress(percent, visible)
        if IS_MACOS:
            _set_dock_badge(percent, visible)

    def clear(self) -> None:
        self.set_progress(0.0, visible=False)

    def attach(self, window) -> None:
        """Give the Windows backend a window handle to draw on."""
        if self._windows is not None:
            self._windows.attach(window)


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

    The interface is obtained once and kept. ``SetProgressValue`` needs the
    window handle, so the taskbar cannot be drawn on until a window exists —
    which is why `attach` is separate from construction.
    """

    #: CLSID_TaskbarList and IID_ITaskbarList3, as the registry knows them.
    _CLSID = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
    _IID = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

    #: `SetProgressState` flags. NORMAL is the ordinary green fill; NOPROGRESS
    #: removes it; PAUSED is the yellow one.
    _NOPROGRESS = 0x0
    _NORMAL = 0x2
    _PAUSED = 0x8

    def __init__(self) -> None:
        self._taskbar = None
        self._hwnd = 0
        self._ready = False

    def attach(self, window) -> None:
        try:
            handle = int(window.winId())
        except Exception:      # noqa: BLE001
            return
        if handle:
            self._hwnd = handle

    def _ensure(self) -> bool:
        if self._ready:
            return self._taskbar is not None
        self._ready = True
        try:
            import ctypes
            from ctypes import POINTER, byref, c_void_p
            from ctypes.wintypes import HWND, ULONGLONG

            ole32 = ctypes.oledll.ole32
            ole32.CoInitialize(None)

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_uint32),
                            ("Data2", ctypes.c_uint16),
                            ("Data3", ctypes.c_uint16),
                            ("Data4", ctypes.c_ubyte * 8)]

            def guid(text: str) -> GUID:
                value = GUID()
                ole32.CLSIDFromString(ctypes.c_wchar_p(text), byref(value))
                return value

            pointer = c_void_p()
            ole32.CoCreateInstance(
                byref(guid(self._CLSID)), None,
                1,                              # CLSCTX_INPROC_SERVER
                byref(guid(self._IID)), byref(pointer),
            )
            if not pointer:
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
            hr_init(pointer)
            self._taskbar = pointer
            return True
        except Exception:      # noqa: BLE001 - no taskbar is not a fault
            self._taskbar = None
            return False

    def set_progress(self, percent: int, visible: bool) -> None:
        if not self._ensure() or not self._hwnd:
            return
        try:
            import ctypes

            set_state = self._call(
                ctypes.c_long, self._c_void_p, self._HWND, ctypes.c_int
            )(self._vtable[10])
            if not visible:
                set_state(self._pointer, self._HWND(self._hwnd), self._NOPROGRESS)
                return
            set_value = self._call(
                ctypes.c_long,
                self._c_void_p, self._HWND, self._ULONGLONG, self._ULONGLONG
            )(self._vtable[9])
            set_state(self._pointer, self._HWND(self._hwnd), self._NORMAL)
            set_value(self._pointer, self._HWND(self._hwnd),
                      self._ULONGLONG(percent), self._ULONGLONG(100))
        except Exception:      # noqa: BLE001
            pass
