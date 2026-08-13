"""Browser discovery for the native-messaging integration.

Finding where a browser expects a Native Messaging manifest is not the simple
per-vendor lookup it used to be.  On Linux the same browser can be installed
three different ways — a distribution package, a **snap**, or a **flatpak** —
and each of those keeps its profile (and therefore its ``NativeMessagingHosts``
directory) somewhere completely different:

===================  ===================================================
Chromium (deb)       ``~/.config/chromium/NativeMessagingHosts``
Chromium (snap)      ``~/snap/chromium/common/chromium/NativeMessagingHosts``
Firefox (deb)        ``~/.mozilla/native-messaging-hosts``
Firefox (snap)       ``~/snap/firefox/common/.mozilla/native-messaging-hosts``
===================  ===================================================

Writing to the classic path when the browser is a snap silently does nothing,
which is indistinguishable from a broken extension.  This module enumerates
every plausible location, reports which ones actually exist, and — importantly
— flags whether the browser is **sandboxed**.

Sandboxing matters because the browser spawns the native host *inside its own
mount namespace*: a snap-confined Firefox sees the snap runtime's ``/usr``, not
the host's, so a ``#!/bin/sh`` shim that execs ``/path/to/.venv/bin/python``
cannot work — that interpreter does not exist from where the host is launched.
Such a browser needs a fully self-contained (frozen) executable.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MACOS

#: Reverse-DNS name the browser uses to address the native messaging host.
HOST_NAME = "com.ixd.downloader"
#: Fixed add-on id declared by ``manifest.firefox.json``.
FIREFOX_EXTENSION_ID = "ixd@ixd.local"

#: Manifest directory name used by every Chrome-family browser.
CHROMIUM_HOST_DIR = "NativeMessagingHosts"
#: Manifest directory name used by Firefox.
FIREFOX_HOST_DIR = "native-messaging-hosts"


@dataclass(slots=True)
class Browser:
    """One browser installation and where its native-host manifest belongs."""

    key: str
    """Stable identifier, e.g. ``chromium-snap``."""

    name: str
    """Display name, e.g. ``Chromium (snap)``."""

    family: str
    """``chromium`` or ``firefox``."""

    profile_root: Path
    """The directory whose existence proves the browser has been run."""

    host_dir: Path
    """Where the native messaging manifest must be written."""

    packaging: str = "native"
    """``native``, ``snap`` or ``flatpak``."""

    extension_dirs: list[Path] = field(default_factory=list)
    """Profile directories whose ``Preferences`` may name our extension."""

    @property
    def installed(self) -> bool:
        return self.profile_root.exists()

    @property
    def sandboxed(self) -> bool:
        """True when the browser launches the host inside its own namespace."""
        return self.packaging in ("snap", "flatpak")

    @property
    def registered(self) -> bool:
        return (self.host_dir / f"{HOST_NAME}.json").exists()

    def describe(self) -> str:
        state = "installed" if self.installed else "not found"
        if self.installed and self.registered:
            state = "registered"
        return f"{self.name} — {state}"


# ----------------------------------------------------------------------
# per-platform candidate tables
# ----------------------------------------------------------------------
def _linux_candidates(home: Path) -> list[Browser]:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    snap = home / "snap"
    flatpak = home / ".var" / "app"

    browsers: list[Browser] = []

    # -- Chrome family, distribution packages --------------------------
    for key, name, folder in (
        ("chrome", "Google Chrome", "google-chrome"),
        ("chrome-beta", "Google Chrome Beta", "google-chrome-beta"),
        ("chrome-unstable", "Google Chrome Dev", "google-chrome-unstable"),
        ("chromium", "Chromium", "chromium"),
        ("edge", "Microsoft Edge", "microsoft-edge"),
        ("brave", "Brave", "BraveSoftware/Brave-Browser"),
        ("vivaldi", "Vivaldi", "vivaldi"),
        ("opera", "Opera", "opera"),
    ):
        root = config.joinpath(*folder.split("/"))
        browsers.append(Browser(
            key=key, name=name, family="chromium",
            profile_root=root, host_dir=root / CHROMIUM_HOST_DIR,
        ))

    # -- Chrome family, snaps ------------------------------------------
    # A snap keeps the user-data dir under ``common``, not ``current``, so it
    # survives refreshes: ~/snap/chromium/common/chromium/.
    for key, name, snap_name, folder in (
        ("chromium-snap", "Chromium (snap)", "chromium", "chromium"),
        ("brave-snap", "Brave (snap)", "brave", "BraveSoftware/Brave-Browser"),
        ("opera-snap", "Opera (snap)", "opera", "opera"),
    ):
        root = snap.joinpath(snap_name, "common", *folder.split("/"))
        browsers.append(Browser(
            key=key, name=name, family="chromium", packaging="snap",
            profile_root=root, host_dir=root / CHROMIUM_HOST_DIR,
        ))

    # -- Chrome family, flatpaks ---------------------------------------
    for key, name, app_id, folder in (
        ("chrome-flatpak", "Google Chrome (flatpak)", "com.google.Chrome", "google-chrome"),
        ("chromium-flatpak", "Chromium (flatpak)", "org.chromium.Chromium", "chromium"),
        ("brave-flatpak", "Brave (flatpak)", "com.brave.Browser",
         "BraveSoftware/Brave-Browser"),
        ("edge-flatpak", "Microsoft Edge (flatpak)", "com.microsoft.Edge", "microsoft-edge"),
    ):
        root = flatpak.joinpath(app_id, "config", *folder.split("/"))
        browsers.append(Browser(
            key=key, name=name, family="chromium", packaging="flatpak",
            profile_root=root, host_dir=root / CHROMIUM_HOST_DIR,
        ))

    # -- Firefox family -------------------------------------------------
    browsers.append(Browser(
        key="firefox", name="Firefox", family="firefox",
        profile_root=home / ".mozilla",
        host_dir=home / ".mozilla" / FIREFOX_HOST_DIR,
    ))
    browsers.append(Browser(
        key="firefox-snap", name="Firefox (snap)", family="firefox", packaging="snap",
        profile_root=snap / "firefox" / "common" / ".mozilla",
        host_dir=snap / "firefox" / "common" / ".mozilla" / FIREFOX_HOST_DIR,
    ))
    browsers.append(Browser(
        key="firefox-flatpak", name="Firefox (flatpak)", family="firefox",
        packaging="flatpak",
        profile_root=flatpak / "org.mozilla.firefox" / ".mozilla",
        host_dir=flatpak / "org.mozilla.firefox" / ".mozilla" / FIREFOX_HOST_DIR,
    ))
    browsers.append(Browser(
        key="librewolf", name="LibreWolf", family="firefox",
        profile_root=home / ".librewolf",
        host_dir=home / ".librewolf" / FIREFOX_HOST_DIR,
    ))
    return browsers


def _macos_candidates(home: Path) -> list[Browser]:
    support = home / "Library" / "Application Support"
    browsers: list[Browser] = []
    for key, name, folder in (
        ("chrome", "Google Chrome", "Google/Chrome"),
        ("chrome-beta", "Google Chrome Beta", "Google/Chrome Beta"),
        ("chromium", "Chromium", "Chromium"),
        ("edge", "Microsoft Edge", "Microsoft Edge"),
        ("brave", "Brave", "BraveSoftware/Brave-Browser"),
        ("vivaldi", "Vivaldi", "Vivaldi"),
        ("opera", "Opera", "com.operasoftware.Opera"),
    ):
        root = support.joinpath(*folder.split("/"))
        browsers.append(Browser(
            key=key, name=name, family="chromium",
            profile_root=root, host_dir=root / CHROMIUM_HOST_DIR,
        ))

    # Firefox reads one shared directory regardless of the profile in use.
    mozilla = support / "Mozilla"
    browsers.append(Browser(
        key="firefox", name="Firefox", family="firefox",
        profile_root=home / "Library" / "Application Support" / "Firefox",
        host_dir=mozilla / CHROMIUM_HOST_DIR,
    ))
    return browsers


def _windows_candidates(home: Path) -> list[Browser]:
    local = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    browsers: list[Browser] = []
    for key, name, base, folder in (
        ("chrome", "Google Chrome", local, "Google/Chrome/User Data"),
        ("chrome-beta", "Google Chrome Beta", local, "Google/Chrome Beta/User Data"),
        ("chromium", "Chromium", local, "Chromium/User Data"),
        ("edge", "Microsoft Edge", local, "Microsoft/Edge/User Data"),
        ("brave", "Brave", local, "BraveSoftware/Brave-Browser/User Data"),
        ("vivaldi", "Vivaldi", local, "Vivaldi/User Data"),
        ("opera", "Opera", roaming, "Opera Software/Opera Stable"),
    ):
        root = base.joinpath(*folder.split("/"))
        # Windows resolves the manifest through the registry; the directory is
        # only used as an installation marker here.
        browsers.append(Browser(
            key=key, name=name, family="chromium",
            profile_root=root, host_dir=root / CHROMIUM_HOST_DIR,
        ))
    browsers.append(Browser(
        key="firefox", name="Firefox", family="firefox",
        profile_root=roaming / "Mozilla" / "Firefox",
        host_dir=roaming / "Mozilla" / "Firefox" / FIREFOX_HOST_DIR,
    ))
    return browsers


# ----------------------------------------------------------------------
def all_browsers() -> list[Browser]:
    """Every browser location this platform could possibly use."""
    home = Path.home()
    if IS_WINDOWS:
        candidates = _windows_candidates(home)
    elif IS_MACOS:
        candidates = _macos_candidates(home)
    else:
        candidates = _linux_candidates(home)

    candidates += _discovered_browsers(home, {b.profile_root for b in candidates})

    for browser in candidates:
        browser.extension_dirs = _profile_dirs(browser)
    return candidates


def _discovered_browsers(home: Path, known: set[Path]) -> list[Browser]:
    """Find browsers no table lists, by recognising their profile layout.

    Chromium forks are numerous and people install them in ways no fixed list
    anticipates. Rather than fail silently on anything unlisted, the search
    looks for the structure every Chrome-family profile has — a ``Default``
    directory containing ``Preferences`` — and for Firefox's ``profiles.ini``.
    """
    found: list[Browser] = []
    if IS_WINDOWS or IS_MACOS:
        return found

    config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
    roots: list[tuple[Path, str]] = [(config, "native")]

    snap = home / "snap"
    if snap.is_dir():
        try:
            for entry in snap.iterdir():
                if (entry / "common").is_dir():
                    roots.append((entry / "common", "snap"))
        except OSError:
            pass

    flatpak = home / ".var" / "app"
    if flatpak.is_dir():
        try:
            for entry in flatpak.iterdir():
                if (entry / "config").is_dir():
                    roots.append((entry / "config", "flatpak"))
        except OSError:
            pass

    for root, packaging in roots:
        for candidate in _scan_for_profiles(root):
            if candidate in known:
                continue
            known.add(candidate)
            family = "firefox" if (candidate / "profiles.ini").exists() else "chromium"
            host_dir = (FIREFOX_HOST_DIR if family == "firefox" else CHROMIUM_HOST_DIR)
            label = candidate.name
            if packaging != "native":
                label = f"{label} ({packaging})"
            found.append(Browser(
                key=f"discovered-{packaging}-{candidate.name}".lower(),
                name=label, family=family, packaging=packaging,
                profile_root=candidate, host_dir=candidate / host_dir,
            ))
    return found


def _is_chromium_profile(directory: Path) -> bool:
    """Tell a real Chrome-family profile from any other Chromium embedder.

    Electron applications share Chromium's configuration layout — ``Local
    State``, ``Preferences``, a cache tree — so those alone identify nothing.
    Registering a native messaging host with, say, a code editor would be
    meaningless at best. Browsing history and bookmarks are the artefacts only
    something used as a browser actually accumulates, so they are what the test
    looks for.
    """
    default = directory / "Default"
    if not (default / "Preferences").is_file():
        return False
    return any(
        (default / marker).exists()
        for marker in ("History", "Bookmarks", "Web Data", "Login Data")
    )


def _scan_for_profiles(root: Path, depth: int = 2) -> list[Path]:
    """Directories under ``root`` that look like a browser profile root."""
    results: list[Path] = []
    if not root.is_dir():
        return results
    try:
        entries = list(root.iterdir())
    except OSError:
        return results

    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            if _is_chromium_profile(entry) or (entry / "profiles.ini").exists():
                results.append(entry)
                continue
        except OSError:
            continue
        # Vendors nest one level (BraveSoftware/Brave-Browser).
        if depth > 1:
            results.extend(_scan_for_profiles(entry, depth - 1))
    return results


def installed_browsers(family: str = "") -> list[Browser]:
    """Only the browsers that have actually been run at least once."""
    return [
        b for b in all_browsers()
        if b.installed and (not family or b.family == family)
    ]


def _profile_dirs(browser: Browser) -> list[Path]:
    """Chromium profile directories that may list installed extensions."""
    if browser.family != "chromium" or not browser.profile_root.exists():
        return []
    found: list[Path] = []
    for name in ("Default", "Profile 1", "Profile 2", "Profile 3", "Guest Profile"):
        candidate = browser.profile_root / name
        if candidate.is_dir():
            found.append(candidate)
    if not found and (browser.profile_root / "Preferences").exists():
        found.append(browser.profile_root)
    return found


def needs_frozen_host() -> bool:
    """True when any installed browser is sandboxed.

    A sandboxed browser cannot execute an interpreter that lives outside its
    own namespace, so the source-tree shim will not work for it.
    """
    return any(b.sandboxed for b in installed_browsers())


def summarise() -> str:
    """Human-readable status line per installed browser, for the UI and CLI."""
    rows = [b for b in all_browsers() if b.installed]
    if not rows:
        return "No browser profiles were found."
    return "\n".join(f"  • {row.describe()}" for row in rows)
