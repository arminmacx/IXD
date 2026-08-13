"""Browser integration: register the native messaging host, automatically.

This module is the single implementation behind three callers — the Settings
window, the CLI installer and the first-run bootstrap — so all three agree on
where files go and what "connected" means.

Three problems make naive registration fail silently, and all three are handled
here:

1. **Sandboxed browsers.**  A snap or flatpak browser keeps its profile
   somewhere entirely different from the distribution package, and writing to
   the classic path appears to succeed while doing nothing.  Locations come
   from :mod:`ixd.core.browsers`, which knows all three layouts.

2. **The launcher must be self-contained.**  A sandboxed browser spawns the
   host inside its own mount namespace, where the host system's ``/usr`` is not
   visible.  A shim that execs a virtualenv interpreter therefore cannot run.
   :func:`host_executable` prefers the frozen binary and reports honestly when
   only the source shim is available.

3. **The extension ID has to be known in advance.**  It is: the manifest
   carries a fixed public key, so the ID is derived locally
   (:mod:`ixd.core.chromeid`) and registration happens before the extension is
   ever loaded.  Any additional IDs found in the browsers' own preference files
   are merged in, which covers a hand-modified unpacked copy.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .core.browsers import (
    FIREFOX_EXTENSION_ID,
    HOST_NAME,
    Browser,
    all_browsers,
    installed_browsers,
)
from .core.chromeid import extension_id_from_manifest_key, is_extension_id

DESCRIPTION = "Internet Xtreme Downloader native messaging host"

#: Root of the source tree / installation, used to find the bundled extension.
PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent


@dataclass(slots=True)
class IntegrationResult:
    """What :func:`install` actually managed to do."""

    launcher: Path | None = None
    extension_ids: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.registered) and not self.errors

    def render(self) -> str:
        lines: list[str] = []
        if self.launcher:
            lines.append(f"Launcher: {self.launcher}")
        if self.extension_ids:
            lines.append("Extension ID: " + ", ".join(self.extension_ids))
        if self.registered:
            lines.append("")
            lines.append("Registered for:")
            lines += [f"  • {entry}" for entry in self.registered]
        if self.skipped:
            lines.append("")
            lines.append("Skipped (not installed):")
            lines += [f"  • {entry}" for entry in self.skipped]
        for warning in self.warnings:
            lines.append("")
            lines.append(f"Warning: {warning}")
        for error in self.errors:
            lines.append("")
            lines.append(f"Error: {error}")
        return "\n".join(lines) or "Nothing to do."


# ----------------------------------------------------------------------
# extension identity
# ----------------------------------------------------------------------
def bundled_extension_source() -> Path:
    """The read-only copy of the extension shipped with this build."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(getattr(sys, "_MEIPASS", ".")) / "extension")
        candidates.append(Path(sys.executable).parent / "extension")
    candidates += [SOURCE_ROOT / "extension", PACKAGE_ROOT / "extension"]
    for candidate in candidates:
        if (candidate / "manifest.chrome.json").exists():
            return candidate
    return SOURCE_ROOT / "extension"


def extension_dir() -> Path:
    """The folder the user points "Load unpacked" at.

    A source checkout uses the tree directly. A frozen build cannot: its data
    files live inside the bundle, which is wiped and rebuilt on every launch
    for onefile builds and is not somewhere a user should be browsing. So the
    extension is materialised once into the application's data directory, which
    is writable and has a stable path the browser can keep referring to.
    """
    source = bundled_extension_source()
    if not getattr(sys, "frozen", False):
        return source

    target = config.DATA_DIR / "extension"
    try:
        _mirror_tree(source, target)
    except OSError:
        return source
    return target


def _mirror_tree(source: Path, target: Path) -> None:
    """Copy ``source`` into ``target``, refreshing only what changed."""
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if (destination.exists()
                and destination.stat().st_size == item.stat().st_size
                and destination.stat().st_mtime >= item.stat().st_mtime):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.read_bytes())


def chrome_manifest_path() -> Path:
    """The manifest that defines the extension's identity.

    Read from the shipped copy rather than the materialised one so the ID is
    known even if the data directory has not been populated yet.
    """
    return bundled_extension_source() / "manifest.chrome.json"


def sync_extension_manifest() -> Path | None:
    """Make ``extension/`` directly loadable by Chrome.

    Chrome only recognises a file literally named ``manifest.json``.  Keeping
    the Chrome manifest under its own name lets the Firefox variant live beside
    it, so the loadable copy is written on demand instead of asking the user to
    rename anything.
    """
    source = chrome_manifest_path()
    if not source.exists():
        return None
    target = extension_dir() / "manifest.json"
    try:
        payload = source.read_text(encoding="utf-8")
        if not target.exists() or target.read_text(encoding="utf-8") != payload:
            target.write_text(payload, encoding="utf-8")
    except OSError:
        return None
    return target


def bundled_extension_id() -> str:
    """The ID Chrome will assign to our unpacked extension.

    Derived from the fixed ``"key"`` in the manifest, so it is stable across
    machines and known before the extension has ever been loaded.
    """
    try:
        manifest = json.loads(chrome_manifest_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    key = manifest.get("key", "")
    if not key:
        return ""
    try:
        return extension_id_from_manifest_key(key)
    except ValueError:
        return ""


def discover_extension_ids() -> list[str]:
    """IDs of any installed copy of this extension, read from the browsers.

    Covers the case where someone loads a modified unpacked copy whose ``key``
    was stripped, which would otherwise get an unpredictable ID.
    """
    found: list[str] = []
    for browser in installed_browsers("chromium"):
        for profile in browser.extension_dirs:
            for filename in ("Preferences", "Secure Preferences"):
                path = profile / filename
                if not path.exists():
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError):
                    continue
                settings = (data.get("extensions") or {}).get("settings") or {}
                for identifier, entry in settings.items():
                    if not is_extension_id(identifier) or identifier in found:
                        continue
                    if _entry_is_ours(entry):
                        found.append(identifier)
    return found


def _entry_is_ours(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    manifest = entry.get("manifest")
    if isinstance(manifest, dict):
        name = str(manifest.get("name", ""))
        if "Internet Xtreme Downloader" in name:
            return True
    path = str(entry.get("path", ""))
    return bool(path) and Path(path).name == extension_dir().name and "ixd" in path.lower()


def all_extension_ids(extra: list[str] | None = None) -> list[str]:
    identifiers: list[str] = []
    for value in [bundled_extension_id(), *(extra or []), *discover_extension_ids()]:
        value = (value or "").strip()
        if value and value not in identifiers:
            identifiers.append(value)
    return identifiers


# ----------------------------------------------------------------------
# launcher
# ----------------------------------------------------------------------
def frozen_executable() -> Path | None:
    """Locate a self-contained build of the application, if one exists."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)

    stem = "ixd"
    suffixes = [".exe", ""] if sys.platform.startswith("win") else [""]
    roots = [SOURCE_ROOT / "dist", SOURCE_ROOT]
    for root in roots:
        # PyInstaller "onedir" nests the binary inside a folder of the same
        # name; "onefile" puts it directly in dist/. On Windows the *folder*
        # has no suffix and the *file* does — `dist/ixd/
        # ixd.exe` — and pairing each name with itself never
        # tried that combination, so a Windows build was never found. The
        # launcher then fell back to running the source with an interpreter,
        # which is why the browser could not start the host at all.
        for suffix in suffixes:
            for candidate in (root / stem / f"{stem}{suffix}",
                              root / f"{stem}{suffix}"):
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate
        app = root / "Internet Xtreme Downloader.app" / "Contents" / "MacOS" / "ixd"
        if app.is_file():
            return app
    return None


def launcher_path() -> Path:
    """Where the shim an unconfined browser executes is written."""
    directory = config.DATA_DIR / "native-host"
    if sys.platform.startswith("win"):
        return directory / "ixd-native-host.bat"
    return directory / "ixd-native-host"


#: Directory name used for the per-browser host inside a sandbox.
SANDBOX_DIR_NAME = "ixd"


def sandbox_dir(browser: Browser) -> Path | None:
    """A directory this sandboxed browser can both read and execute from.

    The two sandboxes need opposite treatment:

    * **snap** — AppArmor grants ``mrkix`` (read, map, *execute*) on
      ``~/snap/<instance>/**`` and denies execution of every dotted path in
      ``$HOME``. Nothing outside the snap's own area can be launched, so the
      host has to live inside it.
    * **flatpak** — the browser only sees its own ``~/.var/app/<id>`` tree, but
      the runtime provides ``flatpak-spawn``, which can run a command back on
      the host system. So the file placed here is a one-line trampoline rather
      than the host itself.
    """
    home = Path.home()
    if browser.packaging == "snap":
        try:
            instance = browser.profile_root.relative_to(home / "snap").parts[0]
        except (ValueError, IndexError):
            return None
        return home / "snap" / instance / "common" / SANDBOX_DIR_NAME

    if browser.packaging == "flatpak":
        try:
            app_id = browser.profile_root.relative_to(home / ".var" / "app").parts[0]
        except (ValueError, IndexError):
            return None
        return home / ".var" / "app" / app_id / "data" / SANDBOX_DIR_NAME

    return None


def write_launcher() -> tuple[Path, bool]:
    """Create the executable an unconfined browser spawns.

    Returns ``(path, self_contained)``. ``self_contained`` is False when the
    shim falls back on an interpreter from the host system.
    """
    launcher = launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    binary = frozen_executable()

    if sys.platform.startswith("win"):
        if binary is not None:
            body = f'@echo off\r\n"{binary}" --native-host %*\r\n'
        else:
            # `cd` first, exactly as the POSIX shim does. Without it the browser
            # spawns the host in its own working directory, where `ixd` is not
            # importable — the host exits before reading a byte and the
            # extension reports "Error when communicating with the native
            # messaging host". The POSIX branch had this from the start; the
            # Windows one did not.
            drive = SOURCE_ROOT.drive
            body = (
                "@echo off\r\n"
                + (f"{drive}\r\n" if drive else "")
                + f'cd /d "{SOURCE_ROOT}"\r\n'
                + f'"{sys.executable}" -m ixd --native-host %*\r\n'
            )
        launcher.write_text(body, encoding="utf-8")
        return launcher, binary is not None

    if binary is not None:
        body = f'#!/bin/sh\nexec "{binary}" --native-host "$@"\n'
    else:
        body = (
            "#!/bin/sh\n"
            f'cd "{SOURCE_ROOT}"\n'
            f'exec "{sys.executable}" -m ixd --native-host "$@"\n'
        )
    launcher.write_text(body, encoding="utf-8")
    _make_executable(launcher)
    return launcher, binary is not None


def relay_script() -> Path | None:
    """The relay's source file, which has to be copied out as a file.

    A frozen build keeps only the compiled module, so the ``.py`` is shipped
    as a data file as well; both locations are checked.
    """
    candidates = [PACKAGE_ROOT / "ipc" / "relay.py"]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(getattr(sys, "_MEIPASS", ".")) / "ixd" / "ipc" / "relay.py")
        candidates.append(Path(sys.executable).parent / "ixd" / "ipc" / "relay.py")
    candidates.append(SOURCE_ROOT / "ixd" / "ipc" / "relay.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def write_sandbox_launcher(browser: Browser) -> Path | None:
    """Install a self-contained relay inside a sandboxed browser's own area.

    The relay deliberately does not run this application: from inside the
    sandbox the installed binary may be unreachable (a dotted path, or
    ``/opt``, which does not exist in the snap's mount namespace at all). It
    runs on the interpreter the snap runtime already provides and speaks only
    stdio and TCP, so it works regardless of where the application lives.
    """
    directory = sandbox_dir(browser)
    if directory is None:
        return None

    try:
        directory.mkdir(parents=True, exist_ok=True)
        launcher = directory / "ixd-native-host"

        if browser.packaging == "flatpak":
            # A flatpak can step back out to the host, so run the real launcher
            # there rather than reimplementing anything inside the sandbox.
            host_launcher = launcher_path()
            launcher.write_text(
                "#!/bin/sh\n"
                "# Generated by Internet Xtreme Downloader. A flatpak browser can run\n"
                "# a command on the host system, which is where the application\n"
                "# and its data directory actually live.\n"
                f'exec flatpak-spawn --host "{host_launcher}" "$@"\n',
                encoding="utf-8",
            )
            _make_executable(launcher)
            return launcher

        relay_source = relay_script()
        if relay_source is None:
            return None
        relay = directory / "relay.py"
        relay.write_bytes(relay_source.read_bytes())

        endpoint = directory / "endpoint.json"
        launcher.write_text(
            "#!/bin/sh\n"
            "# Generated by Internet Xtreme Downloader. The browser executes this from\n"
            "# inside its sandbox, where only the snap runtime's own interpreter\n"
            "# and this directory are reachable.\n"
            "#\n"
            "# IXD_NO_LAUNCH is not optional. A process started from here\n"
            "# inherits the sandbox, including its redirected HOME, and would\n"
            "# build a second application instance with its own database and\n"
            "# its own credentials — which then holds the control port and\n"
            "# locks the real instance out.\n"
            f'IXD_ENDPOINT="{endpoint}"\n'
            "IXD_NO_LAUNCH=1\n"
            "export IXD_ENDPOINT IXD_NO_LAUNCH\n"
            f'exec python3 "{relay}" "$@"\n',
            encoding="utf-8",
        )
        _make_executable(launcher)
    except OSError:
        return None

    # Publish the current endpoint immediately so the relay works even if the
    # application is not restarted after registration.
    _mirror_endpoint(directory)
    return launcher


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _mirror_endpoint(directory: Path) -> None:
    """Place the current endpoint beside a sandbox relay, minus the launcher."""
    try:
        source = Path(config.IPC_PORT_FILE)
        if not source.exists():
            return
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        target = directory / "endpoint.json"
        target.write_text(
            json.dumps(sandbox_endpoint_payload(payload)), encoding="utf-8"
        )
        target.chmod(0o600)      # the token is a credential
    except OSError:
        pass


def published_endpoint_dirs() -> list[Path]:
    """Every sandbox directory that needs its own copy of the endpoint.

    Only snap browsers do: a flatpak's trampoline runs the real launcher back
    on the host, where the endpoint file is readable as usual.
    """
    directories: list[Path] = []
    for browser in installed_browsers():
        if browser.packaging != "snap":
            continue
        directory = sandbox_dir(browser)
        if directory is not None and directory.exists() and directory not in directories:
            directories.append(directory)
    return directories


def sandbox_endpoint_payload(payload: dict) -> dict:
    """The endpoint as a confined relay is allowed to see it.

    The start-up command is removed. A relay inside a sandbox that acted on it
    would start the application *within* that sandbox, where ``HOME`` is
    redirected — producing a second instance with a separate database and a
    separate token, which then occupies the control port and locks out the real
    one. Observed happening.
    """
    return {key: value for key, value in payload.items() if key != "launch"}


def publish_endpoint(payload: dict) -> None:
    """Copy the control-socket details where a sandboxed relay can read them."""
    body = json.dumps(sandbox_endpoint_payload(payload))
    for directory in published_endpoint_dirs():
        try:
            target = directory / "endpoint.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(body, encoding="utf-8")
            temporary.replace(target)
            target.chmod(0o600)      # the token is a credential
        except OSError:
            continue


def clear_published_endpoints() -> None:
    for directory in published_endpoint_dirs():
        try:
            (directory / "endpoint.json").unlink()
        except OSError:
            continue


# ----------------------------------------------------------------------
# manifests
# ----------------------------------------------------------------------
#: What this host was registered as before the application was renamed. The
#: extension keeps its signing key and therefore its ID, so the renamed build
#: replaces the old one in the browser and then asks for a host by a name the
#: old manifest does not answer to. Left in place, the old file points at a
#: binary that is being replaced — so it is removed as the new one is written.
_FORMER_HOST_NAMES = ("com.xai.downloadmanager",)


def _drop_former_hosts(directory: Path) -> None:
    for name in _FORMER_HOST_NAMES:
        stale = directory / f"{name}.json"
        try:
            if stale.is_file():
                stale.unlink()
        except OSError:
            pass       # a manifest we cannot remove is not a failed install


def chromium_host_manifest(launcher: Path, extension_ids: list[str]) -> dict:
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{eid}/" for eid in extension_ids],
    }


def firefox_host_manifest(launcher: Path) -> dict:
    return {
        "name": HOST_NAME,
        "description": DESCRIPTION,
        "path": str(launcher),
        "type": "stdio",
        "allowed_extensions": [FIREFOX_EXTENSION_ID],
    }


# ----------------------------------------------------------------------
# install / uninstall / status
# ----------------------------------------------------------------------
def install(extra_ids: list[str] | None = None, *, include_missing: bool = False,
            families: tuple[str, ...] = ("chromium", "firefox"),
            extra_dirs: list[str] | None = None) -> IntegrationResult:
    """Write the native-messaging manifest everywhere it is needed.

    ``extra_dirs`` registers additional manifest directories by hand, for a
    browser installed somewhere no rule anticipates — a portable build, an
    unusual prefix, or a fork that has not been heard of.
    """
    result = IntegrationResult()

    launcher, self_contained = write_launcher()
    result.launcher = launcher
    if not self_contained:
        result.warnings.append(
            "running from a source checkout, so unconfined browsers will start "
            "the host through this interpreter. Building the application "
            "(python packaging/build.py) makes the host self-contained."
        )
    if sync_extension_manifest() is None:
        result.warnings.append(
            f"could not write a loadable manifest.json into {extension_dir()}"
        )

    identifiers = all_extension_ids(extra_ids)
    result.extension_ids = identifiers
    if not identifiers:
        result.errors.append(
            "no Chrome extension ID is known — run "
            "'python packaging/make_extension_key.py' to give the extension a "
            "permanent identity"
        )

    browsers = [b for b in all_browsers() if b.family in families]

    for browser in browsers:
        if not browser.installed and not include_missing:
            result.skipped.append(browser.name)
            continue

        # A sandboxed browser cannot execute the shared launcher — its profile
        # denies every dotted path in $HOME — so it gets its own relay inside
        # the only directory it is allowed to run things from.
        browser_launcher = launcher
        if browser.sandboxed:
            sandboxed = write_sandbox_launcher(browser)
            if sandboxed is None:
                result.warnings.append(
                    f"{browser.name} is sandboxed and no executable location "
                    "could be prepared for it; its integration will not connect."
                )
            else:
                browser_launcher = sandboxed

        manifest = (
            firefox_host_manifest(browser_launcher) if browser.family == "firefox"
            else chromium_host_manifest(browser_launcher, identifiers)
        )
        try:
            browser.host_dir.mkdir(parents=True, exist_ok=True)
            target = browser.host_dir / f"{HOST_NAME}.json"
            target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            _drop_former_hosts(browser.host_dir)
        except OSError as exc:
            result.errors.append(f"{browser.name}: {exc}")
            continue
        result.registered.append(f"{browser.name} → {target}")

    for raw in extra_dirs or []:
        directory = Path(raw).expanduser()
        family = "firefox" if "native-messaging-hosts" in directory.name else "chromium"
        manifest = (
            firefox_host_manifest(launcher) if family == "firefox"
            else chromium_host_manifest(launcher, identifiers)
        )
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{HOST_NAME}.json"
            target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            _drop_former_hosts(directory)
            result.registered.append(f"custom → {target}")
        except OSError as exc:
            result.errors.append(f"{directory}: {exc}")

    if sys.platform.startswith("win"):
        result.registered += _install_windows_registry(launcher, identifiers, result)

    return result


def _install_windows_registry(launcher: Path, extension_ids: list[str],
                              result: IntegrationResult) -> list[str]:
    """Windows resolves the manifest through HKCU, not a profile directory."""
    import winreg      # noqa: PLC0415 - Windows only

    # Point at the executable itself when there is one, rather than at the
    # `.bat` that wraps it. Native messaging is a **binary** protocol —
    # length-prefixed frames — and a batch wrapper puts `cmd.exe` in the middle
    # of that pipe for no gain. The application recognises the argument a
    # browser passes and needs no flag from the shim.
    binary = frozen_executable()
    if binary is not None:
        launcher = binary

    manifest_dir = config.DATA_DIR / "native-host"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    chromium_path = manifest_dir / f"{HOST_NAME}.json"
    firefox_path = manifest_dir / f"{HOST_NAME}.firefox.json"
    chromium_path.write_text(
        json.dumps(chromium_host_manifest(launcher, extension_ids), indent=2),
        encoding="utf-8",
    )
    firefox_path.write_text(
        json.dumps(firefox_host_manifest(launcher), indent=2), encoding="utf-8"
    )

    registered: list[str] = []
    for label, base, path in (
        ("Chrome", r"Software\Google\Chrome\NativeMessagingHosts", chromium_path),
        ("Chromium", r"Software\Chromium\NativeMessagingHosts", chromium_path),
        ("Edge", r"Software\Microsoft\Edge\NativeMessagingHosts", chromium_path),
        ("Brave", r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
         chromium_path),
        ("Firefox", r"Software\Mozilla\NativeMessagingHosts", firefox_path),
    ):
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{base}\\{HOST_NAME}") as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(path))
            registered.append(f"{label} (registry)")
        except OSError as exc:
            result.errors.append(f"{label} registry: {exc}")
    return registered


def uninstall() -> list[str]:
    """Remove every manifest, the launcher shim and any sandbox relay."""
    removed: list[str] = []
    for browser in all_browsers():
        target = browser.host_dir / f"{HOST_NAME}.json"
        if target.exists():
            try:
                target.unlink()
                removed.append(str(target))
            except OSError:
                continue

        directory = sandbox_dir(browser)
        if directory is None or not directory.exists():
            continue
        for name in ("ixd-native-host", "relay.py", "endpoint.json"):
            try:
                (directory / name).unlink()
                removed.append(str(directory / name))
            except OSError:
                continue
        try:
            directory.rmdir()
        except OSError:
            pass

    if sys.platform.startswith("win"):
        import winreg      # noqa: PLC0415 - Windows only

        for base in (
            r"Software\Google\Chrome\NativeMessagingHosts",
            r"Software\Chromium\NativeMessagingHosts",
            r"Software\Microsoft\Edge\NativeMessagingHosts",
            r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
            r"Software\Mozilla\NativeMessagingHosts",
        ):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{base}\\{HOST_NAME}")
                removed.append(f"HKCU\\{base}\\{HOST_NAME}")
            except OSError:
                continue

    launcher = launcher_path()
    if launcher.exists():
        try:
            launcher.unlink()
            removed.append(str(launcher))
        except OSError:
            pass
    return removed


def status() -> list[dict]:
    """Per-browser integration state, for the Settings window."""
    rows: list[dict] = []
    for browser in all_browsers():
        if not browser.installed:
            continue
        rows.append({
            "key": browser.key,
            "name": browser.name,
            "family": browser.family,
            "packaging": browser.packaging,
            "sandboxed": browser.sandboxed,
            "registered": browser.registered,
            "host_dir": str(browser.host_dir),
            "launcher": str(launcher_for(browser)),
        })
    return rows


def launcher_for(browser: Browser) -> Path:
    """The executable this particular browser is configured to spawn."""
    directory = sandbox_dir(browser) if browser.sandboxed else None
    return directory / "ixd-native-host" if directory else launcher_path()


def registered_launchers() -> list[Path]:
    """Every distinct launcher an installed browser would run."""
    seen: list[Path] = []
    for browser in all_browsers():
        if not browser.installed:
            continue
        candidate = launcher_for(browser)
        if candidate not in seen:
            seen.append(candidate)
    return seen


def is_registered() -> bool:
    """True when at least one installed browser has the manifest."""
    return any(row["registered"] for row in status())


def ensure_registered(extra_ids: list[str] | None = None) -> IntegrationResult | None:
    """Register on first run, and re-register when the launcher moved.

    Called at start-up so the user never has to think about it.  A no-op once
    the manifest on disk already points at the current launcher.
    """
    launcher = launcher_path()
    stale = False

    rows = [b for b in all_browsers() if b.installed]
    if not rows:
        return None

    for browser in rows:
        # Sandboxed browsers run their own relay from their own directory, so
        # the path to compare against differs per browser.
        sandboxed = sandbox_dir(browser) if browser.sandboxed else None
        expected = str(sandboxed / "ixd-native-host") if sandboxed else str(launcher)

        target = browser.host_dir / f"{HOST_NAME}.json"
        if not target.exists() or not Path(expected).exists():
            stale = True
            break
        try:
            manifest = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stale = True
            break
        if manifest.get("path") != expected:
            stale = True
            break
        if browser.family == "chromium":
            wanted = {f"chrome-extension://{i}/" for i in all_extension_ids(extra_ids)}
            if not wanted.issubset(set(manifest.get("allowed_origins") or [])):
                stale = True
                break

    if not stale and launcher.exists():
        # Nothing to re-register, but the relay's copy of the control endpoint
        # must still track the port this run is listening on.
        for directory in published_endpoint_dirs():
            _mirror_endpoint(directory)
        return None
    return install(extra_ids)


def browser_extension_targets() -> list[Browser]:
    """Installed Chrome-family browsers the extension can be loaded into."""
    return [b for b in installed_browsers("chromium")]
