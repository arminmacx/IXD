"""Knowing when there is a newer version, and — for a portable build — taking it.

Three questions, kept apart on purpose:

1.  **Is there a newer one?** :func:`check` asks the project's release feed and
    compares version numbers. It reads; it never writes anything and never
    starts anything.
2.  **Can *this* build replace itself?** :func:`self_update_kind` answers from a
    marker the build writes into its own folder. A `.deb` installs into
    `/opt` and its files belong to the package manager, so it must never try —
    it is told to fetch the package instead. A portable folder is the user's
    own, and replacing it is exactly what portable means.
3.  **Do it.** :func:`download` fetches the asset, :func:`stage` unpacks and
    checks it, and :func:`apply` swaps the folders and relaunches.

The swap is done by the *new* build, not the old one: a program cannot replace
the file it is executing on Windows, and on any platform it cannot delete the
directory it is running from and still be running. So the staged copy is
started with ``--apply-update``, it waits for this process to go, moves the old
folder aside, moves itself into place, starts the application again and removes
what it moved aside. If anything fails before the last step, the old folder is
still there and is put back.

Nothing here trusts the network further than it has to: the feed must be
HTTPS on the configured host, the asset must be the size the feed declared,
and the archive must contain the launcher it claims to before anything is
moved anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__

#: Where releases are published. A setting rather than a constant so a fork —
#: or a test — points somewhere else without patching code.
DEFAULT_FEED = "https://api.github.com/repos/arminmacx/IXD/releases/latest"
DEFAULT_PAGE = "https://github.com/arminmacx/IXD/releases/latest"

#: How often an automatic check runs. Once a day is enough for a program
#: released a few times a month, and it keeps the request count per user at a
#: level no rate limit will ever notice.
CHECK_INTERVAL_SECONDS = 24 * 60 * 60

#: The marker a self-updating build writes beside its launcher.
MARKER_NAME = "update-channel.json"


def _trusted_transport(url: str) -> bool:
    """Whether an update may come from here.

    HTTPS, or the loopback address. Everything about an update — what it says
    the newest version is, and the bytes that will replace the application —
    has to arrive over a connection nobody on the network can rewrite; plain
    HTTP to a remote host is exactly the connection they can. Loopback is
    allowed because it never leaves the machine, and because a feed that
    cannot be stood up locally is a feed nothing can test.
    """
    lowered = str(url or "").lower()
    if lowered.startswith("https://"):
        return True
    if not lowered.startswith("http://"):
        return False
    host = lowered.split("://", 1)[1].split("/", 1)[0].split("@")[-1]
    host = host.rsplit(":", 1)[0].strip("[]")
    return host in ("127.0.0.1", "localhost", "::1")


def version_tuple(text: str) -> tuple[int, ...]:
    """``"v1.0.10"`` → ``(1, 0, 10)``, ignoring anything that is not a number.

    Compared as numbers because strings order 1.0.10 before 1.0.9, which is
    how an updater tells someone their newer version is older.
    """
    digits: list[int] = []
    for piece in str(text or "").strip().lstrip("vV").replace("-", ".").split("."):
        run = ""
        for character in piece:
            if character.isdigit():
                run += character
            else:
                break
        if not run:
            break
        digits.append(int(run))
    return tuple(digits) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return version_tuple(candidate) > version_tuple(current)


@dataclass(slots=True)
class Release:
    """What the feed says about the newest published version."""

    version: str = ""
    name: str = ""
    notes: str = ""
    page_url: str = DEFAULT_PAGE
    published_at: str = ""
    assets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def newer(self) -> bool:
        return bool(self.version) and is_newer(self.version)

    def asset(self, *patterns: str) -> dict[str, Any] | None:
        """The first published file whose name contains every given piece."""
        for entry in self.assets:
            name = str(entry.get("name", "")).lower()
            if all(piece.lower() in name for piece in patterns if piece):
                return entry
        return None


# ----------------------------------------------------------------------
# what this build is
# ----------------------------------------------------------------------
def staging_root(fallback: Path) -> Path:
    """Where an update is unpacked before it replaces anything.

    Beside the application, when the application is portable. Somebody who
    unpacks a portable build into a folder of their own does not expect its
    update to appear under `%APPDATA%\\IXD` — reported exactly that way — and
    a portable copy that leaves things elsewhere is not really portable.

    The data directory remains the fallback, for an install whose own folder
    is not writable. That build cannot replace itself either, so nothing is
    unpacked there in practice.
    """
    root = install_root()
    if root is not None:
        beside = root.parent / f"{root.name}-update"
        try:
            beside.mkdir(parents=True, exist_ok=True)
            probe = beside / ".writable"
            probe.write_text("", encoding="ascii")
            probe.unlink()
            return beside
        except OSError:
            pass
    return fallback


def install_root() -> Path | None:
    """The folder this build lives in, or ``None`` when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def marker() -> dict[str, Any]:
    """The build's own description of how it may be updated."""
    root = install_root()
    if root is None:
        return {}
    try:
        return json.loads((root / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def self_update_kind() -> str:
    """``"portable"`` when this build may replace itself, else ``""``.

    A build says so itself, at build time. Guessing from the file layout was
    the alternative and it is not safe: an unpacked `.deb` looks exactly like
    a portable folder, and replacing `/opt/ixd` out from under the package
    manager leaves a machine whose next upgrade fails.
    """
    described = marker()
    if described.get("self_update") is not True:
        return ""
    root = install_root()
    if root is None or not os.access(root, os.W_OK):
        return ""
    return str(described.get("kind") or "portable")


def platform_patterns() -> list[tuple[str, ...]]:
    """How this platform's self-updating archive is named, in order of fit."""
    if sys.platform.startswith("win"):
        return [("windows", "selfupdate", ".zip"), ("windows", ".zip")]
    if sys.platform == "darwin":
        return [("macos", "selfupdate", ".zip"), ("macos", ".zip")]
    return [("linux", "selfupdate", ".tar.gz"), ("linux", ".tar.gz")]


def asset_patterns() -> list[tuple[str, ...]]:
    """Every way of naming the file this build should take, best first.

    A marker may name the archive its build was published as — but **it must
    never contain a version number**, and one release did. The Windows build
    of 1.0.8 recorded `ixd-1.0.8-windows-x64-selfupdate.zip` and then looked
    for that exact string in the 1.0.9 release, which of course publishes
    `ixd-1.0.9-…`. It reported that the release "publishes nothing this build
    can use" while listing the very file it wanted.

    So the marker's name is one candidate among several rather than the only
    one, and the platform's own shape is always tried after it. A stale or
    over-specific marker now costs nothing.
    """
    candidates: list[tuple[str, ...]] = []
    named = str(marker().get("asset") or "").strip()
    if named:
        candidates.append((named,))
    candidates.extend(platform_patterns())
    return candidates


def choose_asset(release: "Release") -> dict[str, Any] | None:
    """The published file this build should take, or ``None``."""
    for pattern in asset_patterns():
        found = release.asset(*pattern)
        if found is not None:
            return found
    return None


# ----------------------------------------------------------------------
# asking
# ----------------------------------------------------------------------
def check(client: Any, feed: str = DEFAULT_FEED, timeout: float = 20.0) -> Release:
    """Ask the feed what the newest release is.

    ``client`` is the application's own HTTP client, so the check follows the
    proxy, the interface binding and the TLS policy the user configured —
    an updater that ignores those is a hole in every one of them.
    """
    if not _trusted_transport(feed):
        raise ValueError("the update feed must be an https address")
    del timeout          # the client owns its own; named for the caller's sake
    body = client.get_text(feed, {"Accept": "application/vnd.github+json"},
                           limit=1 << 20)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("the update feed did not answer with a release")
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    return Release(
        version=str(data.get("tag_name") or "").lstrip("vV"),
        name=str(data.get("name") or ""),
        notes=str(data.get("body") or ""),
        page_url=str(data.get("html_url") or DEFAULT_PAGE),
        published_at=str(data.get("published_at") or ""),
        assets=assets,
    )


# ----------------------------------------------------------------------
# taking it
# ----------------------------------------------------------------------
def download(client: Any, asset: dict[str, Any], into: Path,
             progress: Any = None) -> Path:
    """Fetch a published file, and refuse one that is not the size it claimed."""
    url = str(asset.get("browser_download_url") or "")
    if not _trusted_transport(url):
        raise ValueError("an update may only be fetched over https")
    into.mkdir(parents=True, exist_ok=True)
    target = into / str(asset.get("name") or "update.bin")
    expected = int(asset.get("size") or 0)

    written = 0
    with client.request("GET", url,
                        {"Accept": "application/octet-stream"}) as response:
        with open(target, "wb") as handle:
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if progress is not None:
                    progress(written, expected)

    if expected and written != expected:
        target.unlink(missing_ok=True)
        raise OSError(
            f"the download is {written:,} bytes and the release says "
            f"{expected:,} — refusing it"
        )
    return target


def stage(archive: Path, into: Path, launcher: str = "") -> Path:
    """Unpack an update and return the folder that should replace the current one.

    The archive is checked *before* anything is moved: it has to contain the
    launcher this build runs under. An update that unpacks to something else
    is not this application, and finding that out after the swap means finding
    it out with nothing left to run.
    """
    launcher = launcher or Path(sys.executable).name
    # A staging folder is never reused. The first attempt's updater is still
    # *running from* the last one when a second attempt is made — that is what
    # a retry after a failed update is — and emptying it took its own files
    # away underneath it: "Failed to execute script 'entry' … No such file or
    # directory: …\\update\\unpacked\\ixd\\_internal\\base_library.zip".
    # Each attempt gets its own folder, and the old ones are swept up when
    # nothing is holding them.
    into = into.with_name(f"{into.name}-{int(time.time())}-{os.getpid()}")
    _sweep_old_staging(into.parent, keep=into.name)
    into.mkdir(parents=True, exist_ok=True)

    if archive.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            _refuse_paths(name for name in bundle.namelist())
            bundle.extractall(into)
    else:
        with tarfile.open(archive) as bundle:
            _refuse_paths(member.name for member in bundle.getmembers())
            bundle.extractall(into)

    root = into
    entries = [p for p in into.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        root = entries[0]           # archives usually hold one folder
    if not (root / launcher).exists():
        found = ", ".join(sorted(p.name for p in root.iterdir())[:8])
        shutil.rmtree(into, ignore_errors=True)
        raise OSError(f"the update does not contain {launcher} (found: {found})")
    # The launcher has to be runnable; a zip does not always carry the bit.
    try:
        os.chmod(root / launcher, 0o755)
    except OSError:
        pass
    return root


#: How old a staging folder has to be before it is swept up. An updater from
#: a failed attempt may still be running out of one, and deleting its files
#: underneath it is the exact failure this staging scheme exists to prevent —
#: so age is the only safe signal available, and an hour is far longer than
#: any update takes.
STAGING_KEEP_SECONDS = 60 * 60


def _sweep_old_staging(parent: Path, keep: str = "") -> None:
    """Remove staging folders from *old* attempts, and never a recent one.

    Best-effort throughout: a folder that will not go is one something is
    still holding, and it will be swept next time instead.
    """
    try:
        entries = list(parent.iterdir())
    except OSError:
        return
    cutoff = time.time() - STAGING_KEEP_SECONDS
    for entry in entries:
        if not entry.is_dir() or entry.name == keep:
            continue
        if not entry.name.startswith("unpacked"):
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue              # recent: something may be running in it
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)


def _refuse_paths(names: Any) -> None:
    """An archive may not write outside where it is unpacked."""
    for name in names:
        if name.startswith("/") or ".." in Path(name).parts:
            raise OSError(f"the update contains an unsafe path: {name}")


def relaunch_into(staged: Path, target: Path, launcher: str = "") -> None:
    """Hand the swap to the staged build and leave.

    Called by the *running* application. Everything after this belongs to the
    new build: this process is what it is waiting to see go.
    """
    launcher = launcher or Path(sys.executable).name
    command = [str(staged / launcher), "--apply-update", str(target),
               "--wait-for", str(os.getpid())]
    subprocess.Popen(command, cwd=str(staged), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply(target: Path, wait_for: int = 0, timeout: float = 180.0,
          relaunch: bool = True) -> tuple[bool, str]:
    """Replace ``target`` with the folder this process is running from.

    Runs inside the *new* build. Returns ``(ok, detail)`` and never raises: it
    is the last thing standing between the user and a folder that is neither
    the old version nor the new one.
    """
    staged = install_root()
    if staged is None:
        return False, "an update can only be applied by a built copy"
    # And only by a build that was published as one that may replace a folder.
    # Without this the flag is a copy-this-application-anywhere command on any
    # build, which is not what it is for.
    if not self_update_kind():
        return False, "this build is not a self-updating one"
    target = Path(target).resolve()
    if staged == target:
        return False, "the update is already in place"

    ended = ""
    if wait_for:
        # Politely first. The application has been asked to close and is
        # writing out its database; interrupting that to save ten seconds is
        # not a trade worth making.
        deadline = time.time() + min(timeout, POLITE_WAIT_SECONDS)
        while time.time() < deadline and _alive(wait_for):
            time.sleep(0.2)

        # Then plainly. A process that will not go is what the user saw as
        # "the update did not finish — process 23688 is still running", with
        # the new version downloaded, unpacked and checked, and nothing to
        # show for it. By this point it has been told to quit and given time
        # to do it; the update is what was asked for.
        if _alive(wait_for):
            ended = _end_process(wait_for)
            deadline = time.time() + timeout
            while time.time() < deadline and _alive(wait_for):
                time.sleep(0.2)
        if _alive(wait_for):
            return False, (f"the previous version (process {wait_for}) would "
                           f"not close{ended}")

    ok, detail = _replace_contents(staged, target)
    if not ok:
        return False, detail
    if relaunch:
        launcher = target / Path(sys.executable).name
        try:
            subprocess.Popen([str(launcher)], cwd=str(target),
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            return True, f"updated, but could not start it again: {error}"
    return True, f"updated {target}{ended}"


#: How long the application is given to close on its own before it is closed
#: for it. Long enough for a database flush and a tray icon; short enough that
#: nobody sits watching a progress bar wondering.
POLITE_WAIT_SECONDS = 25.0


def _end_process(pid: int) -> str:
    """Close a process that has been asked and has not gone.

    Returns a note for the Log, because on a machine that is about to restart
    into a different version this line is the only account of what happened.
    """
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes

        PROCESS_TERMINATE = 0x0001
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                             wintypes.DWORD)
            kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, ctypes.c_uint)
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
            if not handle:
                return " (it was already gone)"
            try:
                kernel32.TerminateProcess(handle, 0)
            finally:
                kernel32.CloseHandle(handle)
            return " (it had to be closed)"
        except Exception as error:      # noqa: BLE001
            return f" (it could not be closed: {error})"

    import signal
    for attempt, sig in ((0, signal.SIGTERM), (1, signal.SIGKILL)):
        try:
            os.kill(pid, sig)
        except OSError:
            return " (it was already gone)"
        for _ in range(20):
            if not _alive(pid):
                return " (it had to be closed)" if attempt else " (it was asked again)"
            time.sleep(0.1)
    return " (it could not be closed)"


#: How long a locked file is given before the update gives up on it. Windows
#: releases handles a moment after a process ends, and an anti-virus scanner
#: may hold a freshly written file for a second or two.
LOCK_PATIENCE_SECONDS = 20.0


def _replace_contents(source: Path, target: Path) -> tuple[bool, str]:
    """Put ``source``'s files where ``target``'s are, file by file.

    **Not** a directory rename. Windows refuses to rename or delete a folder
    while any process holds a file inside it, and there is always one: the
    browser keeps a native-messaging host alive from the installed folder
    (§3.14u57), an anti-virus scanner may be reading what was just written,
    and Explorer holds whatever is selected. The report was exactly that —
    `[WinError 32] … being used by another process: 'C:\\Users\\…\\ixd' ->
    'C:\\Users\\…\\ixd.previous'`.

    A *file* is different. Windows will not let a running executable be
    overwritten or deleted, but it will let it be **renamed**, and the new one
    can then be written in its place. So each file is replaced on its own, a
    locked one is moved aside first, and what was moved aside is swept up
    afterwards — or on the next update, if it is still held.
    """
    target.mkdir(parents=True, exist_ok=True)
    moved_aside: list[Path] = []
    deadline = time.time() + LOCK_PATIENCE_SECONDS

    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                shutil.copy2(item, destination)
                break
            except OSError as error:
                aside = destination.with_name(
                    f"{destination.name}.old-{int(time.time())}")
                try:
                    os.replace(destination, aside)
                    moved_aside.append(aside)
                    continue          # the way is clear now
                except OSError:
                    if time.time() >= deadline:
                        return False, (f"{destination.name} is held by another "
                                       f"program and could not be replaced: "
                                       f"{error}")
                    time.sleep(0.5)

    # Anything the new version no longer ships. A file that will not go is not
    # worth failing an otherwise complete update over.
    for item in sorted(target.rglob("*"), reverse=True):
        relative = item.relative_to(target)
        if (source / relative).exists() or ".old-" in item.name:
            continue
        try:
            item.rmdir() if item.is_dir() else item.unlink()
        except OSError:
            pass

    for aside in moved_aside:
        try:
            aside.unlink()
        except OSError:
            pass                      # still running; the next update takes it
    return True, f"replaced {target}"


def _alive(pid: int) -> bool:
    """Is that process still running?

    On POSIX, signal 0 asks without sending anything. **On Windows it does
    not**: `os.kill` maps every signal other than the two console events onto
    `TerminateProcess`, so the obvious cross-platform check would kill the
    application it is waiting for rather than look at it — and the update
    would then be racing a process that was killed mid-write instead of one
    that closed its database and went. Windows is asked through the interface
    that answers this question: open a handle and read the exit code.
    """
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                             wintypes.DWORD)
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = (
                wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        except Exception:               # noqa: BLE001 - unknown means "gone"
            return False
        if not handle:
            return False                # exited, or not ours to look at
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # A child that has exited but has not been waited on still answers signal
    # 0 — it is a zombie, and it is holding nothing. Where the kernel will say
    # so, ask it.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            state = handle.read().rsplit(") ", 1)[1].split(" ", 1)[0]
        if state == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True
