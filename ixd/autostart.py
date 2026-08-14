"""Start with the session, in the tray rather than in your face.

A download manager is only useful if it is already running when a download
starts, and every desktop has a different place to say so. None of them agree,
and none of them is Qt's business, so each is written here:

======= ====================================================================
Windows ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` — one
        registry value naming the executable. No scheduled task, no service,
        no elevation: this is per user and needs no administrator.
Linux   ``~/.config/autostart/ixd.desktop`` — the XDG autostart entry every
        desktop environment reads. The same body as the application's own
        entry with ``--hidden`` added.
macOS   ``~/Library/LaunchAgents/com.ixd.downloader.plist`` — a LaunchAgent
        with ``RunAtLoad``, which is what a per-user login item is underneath.
======= ====================================================================

Always ``--hidden``: a window that opens by itself every time the machine
starts is the reason people turn this off again. The tray icon is there and the
window is one click away.

Registration is refreshed on every start, like the browser integration, so a
rebuilt or moved application keeps working instead of pointing the session at a
path that no longer exists.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from . import __appid__
from .desktop import DESKTOP_FILE_NAME, executable_command

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

#: The name the session sees. It is what the user reads in Task Manager's
#: Startup tab or GNOME's Tweaks, so it is the application's name, not "ixd".
ENTRY_NAME = "Internet Xtreme Downloader"

#: Started with the window down. `--hidden` is a normal run that has not shown
#: its window: the tray is there, the engine is up, and a download arriving
#: from the browser can still raise a window of its own.
HIDDEN = "--hidden"


class AutostartError(RuntimeError):
    """The session refused the registration, with the reason it gave."""


# ---------------------------------------------------------------------------
# where each platform keeps it
# ---------------------------------------------------------------------------
def _windows_command() -> str:
    """The command line for the registry value.

    A frozen build is one executable and quotes cleanly.

    A source run cannot simply be ``-m ixd``: a `Run` value inherits whatever
    working directory the session hands it — `C:\\Windows\\system32`, in
    practice — and there is nowhere to set `PYTHONPATH` on the way, so the
    module would not be found and the entry would launch nothing. The checkout
    is put on the path explicitly instead.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {HIDDEN}'
    root = Path(__file__).resolve().parent.parent
    bootstrap = (f"import sys;sys.path.insert(0,r'{root}');"
                 "import runpy;runpy.run_module('ixd',run_name='__main__')")
    return f'"{sys.executable}" -c "{bootstrap}" {HIDDEN}'


def linux_entry_path() -> Path:
    import os

    home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(home) / "autostart" / f"{DESKTOP_FILE_NAME}.desktop"


def macos_entry_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{__appid__}.plist"


def linux_entry() -> str:
    """The XDG autostart entry.

    ``X-GNOME-Autostart-enabled`` is what GNOME writes when the user toggles an
    entry off; it is set explicitly so an entry disabled once and re-enabled
    here comes back on rather than being written and ignored.
    """
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={ENTRY_NAME}\n"
        "Comment=Start the download manager with the session\n"
        f"Exec={executable_command()} {HIDDEN}\n"
        f"Icon={DESKTOP_FILE_NAME}\n"
        "Terminal=false\n"
        "NoDisplay=false\n"
        "Hidden=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def macos_entry() -> str:
    if getattr(sys, "frozen", False):
        arguments = [sys.executable, HIDDEN]
    else:
        arguments = [sys.executable, "-m", "ixd", HIDDEN]
    items = "".join(f"    <string>{argument}</string>\n" for argument in arguments)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{__appid__}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{items}"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <false/>\n"
        "</dict>\n"
        "</plist>\n"
    )


# ---------------------------------------------------------------------------
# the three verbs
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    """Whether the session is currently set to start this application."""
    try:
        if IS_WINDOWS:
            import winreg      # noqa: PLC0415 - Windows only

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                try:
                    value, _ = winreg.QueryValueEx(key, ENTRY_NAME)
                except FileNotFoundError:
                    return False
                return bool(value)
        if IS_MACOS:
            return macos_entry_path().exists()
        return linux_entry_path().exists()
    except OSError:
        return False


def enable() -> None:
    """Register with the session, or raise `AutostartError` saying why not."""
    try:
        if IS_WINDOWS:
            import winreg      # noqa: PLC0415 - Windows only

            with winreg.CreateKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                winreg.SetValueEx(key, ENTRY_NAME, 0, winreg.REG_SZ,
                                  _windows_command())
            return
        path = macos_entry_path() if IS_MACOS else linux_entry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(macos_entry() if IS_MACOS else linux_entry(),
                        encoding="utf-8")
    except OSError as exc:
        raise AutostartError(str(exc)) from exc


def disable() -> None:
    """Remove the registration. Absent is success, not an error."""
    try:
        if IS_WINDOWS:
            import winreg      # noqa: PLC0415 - Windows only

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_SET_VALUE,
            ) as key:
                try:
                    winreg.DeleteValue(key, ENTRY_NAME)
                except FileNotFoundError:
                    pass
            return
        for path in (macos_entry_path(), linux_entry_path()):
            if path.exists():
                path.unlink()
    except OSError as exc:
        raise AutostartError(str(exc)) from exc


def apply(wanted: bool) -> None:
    """Make the session agree with the setting.

    Called on every start as well as when the box is ticked: a rebuilt or moved
    application would otherwise leave the session launching a path that is no
    longer there, which looks exactly like the setting being ignored.
    """
    if wanted:
        enable()
    elif is_enabled():
        disable()


def registered_command() -> str:
    """What the session would run, for the settings page and for tests."""
    if IS_WINDOWS:
        return _windows_command()
    if IS_MACOS:
        return " ".join(shlex.quote(part) for part in (
            [sys.executable, HIDDEN] if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "ixd", HIDDEN]))
    return f"{executable_command()} {HIDDEN}"
