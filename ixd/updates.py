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


def asset_patterns() -> tuple[str, ...]:
    """Which published file this build takes its updates from."""
    described = marker()
    named = described.get("asset")
    if named:
        return (str(named),)
    if sys.platform.startswith("win"):
        return ("windows", ".zip")
    if sys.platform == "darwin":
        return ("macos", ".zip")
    return ("linux", ".tar.gz")


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
    shutil.rmtree(into, ignore_errors=True)
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


def apply(target: Path, wait_for: int = 0, timeout: float = 60.0,
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

    if wait_for:
        deadline = time.time() + timeout
        while time.time() < deadline and _alive(wait_for):
            time.sleep(0.2)
        if _alive(wait_for):
            return False, f"process {wait_for} is still running"

    aside = target.with_name(target.name + ".previous")
    shutil.rmtree(aside, ignore_errors=True)
    try:
        if target.exists():
            os.replace(target, aside)
    except OSError as error:
        return False, f"could not move the old version aside: {error}"

    try:
        shutil.copytree(staged, target, symlinks=True, dirs_exist_ok=True)
    except OSError as error:
        # Put it back. A failed update must leave a working application.
        shutil.rmtree(target, ignore_errors=True)
        if aside.exists():
            try:
                os.replace(aside, target)
            except OSError:
                return False, (f"the update failed ({error}) and the old version "
                               f"is in {aside}")
        return False, f"the update failed and the previous version was restored: {error}"

    shutil.rmtree(aside, ignore_errors=True)
    if relaunch:
        launcher = target / Path(sys.executable).name
        try:
            subprocess.Popen([str(launcher)], cwd=str(target),
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            return True, f"updated, but could not start it again: {error}"
    return True, f"updated {target}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
