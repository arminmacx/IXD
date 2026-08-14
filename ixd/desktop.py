"""Making the desktop able to name — and therefore draw — this application.

Setting a window icon is not what puts an icon on a Linux desktop. Under
Wayland a window carries an **app id**, not a picture: the compositor looks up
``<app id>.desktop`` among the installed entries and takes the icon named
there. Qt derives that id from :meth:`QGuiApplication.setDesktopFileName`, and
falls back to the executable's own name when nothing is set — which for a
source run is ``python3``, an id no entry claims.

So the icon was never missing. It was unnamed, and nothing about a QIcon can
fix that. The tray is the exception that made it look inconsistent: a tray icon
is handed to the status area as an image, so it showed the real icon while the
window, the dock and the switcher showed a placeholder.

This module supplies the missing half: a desktop entry and the icons it refers
to, installed per-user under ``XDG_DATA_HOME``. A system-wide install — the
``.deb`` — already provides both, and is left alone.

Nothing here runs on Windows or macOS, where the icon travels with the
executable and no entry exists to write.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

#: The base name of the desktop entry, and therefore the Wayland app id.
#: It matches what ``packaging/build.py`` installs, so a packaged install and a
#: source run resolve to the same entry rather than to two competing ones.
DESKTOP_FILE_NAME = "ixd"

#: Icon sizes shipped in ``packaging/icons`` and expected under ``hicolor``.
ICON_SIZES = (16, 32, 64, 128, 256)


def icon_source_dir() -> Path:
    """Where the packaged PNGs live, in a source tree or inside a bundle."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "_internal" / "packaging" / "icons"
    return Path(__file__).resolve().parent.parent / "packaging" / "icons"


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME")
                or Path.home() / ".local" / "share")


def _installed_system_wide() -> bool:
    """Whether a package already provides the entry we would write."""
    roots = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return any(
        (Path(part) / "applications" / f"{DESKTOP_FILE_NAME}.desktop").exists()
        for part in roots.split(":") if part
    )


def executable_command() -> str:
    """How to start this application again, quoted for ``Exec=``.

    The interpreter is taken verbatim and deliberately **not** resolved. A
    virtual environment's ``bin/python`` is a symlink to the system
    interpreter, and resolving it produces a path that exists, runs, and does
    not have PySide6 — an entry that launches nothing, with an error only
    visible to whoever thinks to run it from a terminal.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    # A source run has to name the interpreter as well, or the entry launches
    # nothing. The module form is used so it does not depend on the checkout
    # staying on ``sys.path``.
    root = Path(__file__).resolve().parent.parent
    return f'env PYTHONPATH="{root}" "{sys.executable}" -m ixd'


def desktop_entry() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Internet Xtreme Downloader\n"
        "Comment=Aggressive multi-connection download manager\n"
        f"Exec={executable_command()} %U\n"
        f"Icon={DESKTOP_FILE_NAME}\n"
        "Terminal=false\n"
        "Categories=Network;FileTransfer;\n"
        "StartupNotify=true\n"
        f"StartupWMClass={DESKTOP_FILE_NAME}\n"
        "MimeType=x-scheme-handler/ixd;\n"
    )



#: A directory of PNGs is not an icon theme until it says it is.
#:
#: Measured on this machine, Wayland, with the entry installed and all five
#: sizes written under ``~/.local/share/icons/hicolor``: GTK's own lookup
#: answered ``has_icon("ixd") -> False``, and the window, dock and switcher
#: showed a placeholder. Adding this file — nothing else — turned the same
#: lookup ``True``. The XDG spec requires it; a base directory whose theme has
#: no index is not searched, and the icons sitting in it may as well not exist.
#:
#: The system copy under ``/usr/share/icons/hicolor`` covers the ``.deb``. It
#: does **not** cover the per-user tree, which is its own base directory.
_INDEX_THEME = (
    "[Icon Theme]\n"
    "Name=Hicolor\n"
    "Comment=Fallback icon theme\n"
    "Hidden=true\n"
    "Directories=" + ",".join(f"{size}x{size}/apps" for size in ICON_SIZES) + "\n"
    + "".join(
        f"\n[{size}x{size}/apps]\nSize={size}\nContext=Applications\nType=Threshold\n"
        for size in ICON_SIZES
    )
)


def _ensure_icon_theme_index(icons_root: Path) -> None:
    """Write the theme index when the user's icon tree has none."""
    index = icons_root / "index.theme"
    if index.exists():
        return
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(_INDEX_THEME, encoding="utf-8")


def _refresh_icon_cache(icons_root: Path) -> None:
    """Keep a stale cache from hiding icons that are now there.

    ``icon-theme.cache`` is authoritative when present: GTK reads it instead of
    listing the directory, so an icon added after the cache was built is
    invisible until the cache is rebuilt. Rebuilding is best-effort, and when
    there is no tool to rebuild with, the stale file is removed instead — it is
    a cache, regenerated on demand, and absent is correct where wrong is not.
    """
    cache = icons_root / "icon-theme.cache"
    if not cache.exists():
        return
    tool = shutil.which("gtk-update-icon-cache")
    if tool:
        try:
            subprocess.run([tool, "-q", "-f", "-t", str(icons_root)],
                           timeout=20, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        cache.unlink()
    except OSError:
        pass


def ensure_desktop_entry() -> bool:
    """Install the per-user entry and icons when nothing else provides them.

    Returns whether an entry is available afterwards. Best-effort throughout:
    an application that cannot write an icon still has to start, so every
    filesystem error leaves the desktop looking as it did and nothing else.
    """
    if sys.platform.startswith(("win", "darwin")):
        return False
    if _installed_system_wide():
        return True

    source = icon_source_dir()
    data_home = _data_home()
    icons_root = data_home / "icons" / "hicolor"
    wrote_icon = False
    try:
        for size in ICON_SIZES:
            png = source / f"ixd-{size}.png"
            if not png.is_file():
                continue
            target_dir = (data_home / "icons" / "hicolor"
                          / f"{size}x{size}" / "apps")
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{DESKTOP_FILE_NAME}.png"
            # Rewritten only when it differs, so a launch does not churn the
            # icon cache — some desktops watch these paths.
            if not target.exists() or target.read_bytes() != png.read_bytes():
                shutil.copyfile(png, target)
                wrote_icon = True

        # The icons are useless to the desktop without these two.
        _ensure_icon_theme_index(icons_root)
        if wrote_icon:
            _refresh_icon_cache(icons_root)

        applications = data_home / "applications"
        applications.mkdir(parents=True, exist_ok=True)
        entry = applications / f"{DESKTOP_FILE_NAME}.desktop"
        wanted = desktop_entry()
        if not entry.exists() or entry.read_text(encoding="utf-8") != wanted:
            entry.write_text(wanted, encoding="utf-8")
            entry.chmod(0o755)
    except OSError:
        return False
    return True
