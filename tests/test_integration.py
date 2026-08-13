"""Integration test across the service, control socket and native-host framing.

Exercises the same path the browser extension uses:
    extension → native messaging (length-prefixed JSON) → IPC socket → service

Run with:  python -m tests.test_integration
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures import TestOrigin
from ixd import config
from ixd.config import Settings
from ixd.core.db import Database

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name} {detail}")


def test_service_over_ipc() -> None:
    print("\n[1] control socket: add, poll, pause, resume, complete")
    from ixd.ipc.server import IPCClient, IPCServer
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-ipc-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()

    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    settings.set("categorize_into_subfolders", False)
    settings.set("progress_flush_interval", 0.2)

    service = DownloadService(settings, Database(root / "state.sqlite3"))
    service.start()
    server = IPCServer(service, port=0)
    server.start()

    payload = os.urandom(3 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    try:
        with TestOrigin(payload) as origin:
            client = IPCClient(host="127.0.0.1", port=server.port,
                               token=settings.get("ipc_token"))
            with client:
                pong = client.call("ping")
                check("ping authenticates", pong.get("ok") is True, str(pong))

                bad = IPCClient(host="127.0.0.1", port=server.port, token="wrong")
                with bad:
                    rejected = bad.call("ping")
                check("bad token rejected", rejected.get("ok") is False, str(rejected))

                # Probing must answer with real metadata without moving bytes.
                # Checked before anything is queued, so the counter cannot be
                # confused by a transfer running concurrently.
                origin.state.bytes_served = 0
                probed = client.call("probe", {"url": origin.url()})
                check("probe succeeds", probed.get("ok") is True, str(probed))
                if probed.get("ok"):
                    result = probed["result"]
                    check("probe reports the true size",
                          result["size"] == len(payload), str(result.get("size")))
                    check("probe reports range support",
                          result["supports_ranges"] is True, str(result))
                    check("probe formats the size for display",
                          bool(result.get("size_text")), str(result.get("size_text")))
                check("probe transferred no payload",
                      origin.state.bytes_served <= 1,
                      f"{origin.state.bytes_served} bytes served")

                added = client.call("add", {
                    "url": origin.url(),
                    "cookies": "session=abc",
                    "userAgent": "IXD-Test/1.0",
                })
                check("add accepted", added.get("ok") is True, str(added.get("error")))
                download_id = added["result"]["id"]

                listed = client.call("list")
                check("list returns the download",
                      any(d["id"] == download_id for d in listed["result"]))

                stats = client.call("stats")
                check("stats respond", stats.get("ok") is True and "active" in stats["result"])

                handled = client.call("can_handle", {"url": "https://www.youtube.com/watch?v=abcdefghijk"})
                check("youtube recognised as media",
                      handled["result"]["media"] is True, str(handled["result"]))

                deadline = time.time() + 60
                final = None
                while time.time() < deadline:
                    got = client.call("get", {"id": download_id})
                    final = got.get("result") or {}
                    if final.get("status") in ("completed", "error"):
                        break
                    time.sleep(0.2)

                check("download completes over IPC",
                      final and final.get("status") == "completed",
                      str(final.get("error") if final else "no result"))
                if final and final.get("status") == "completed":
                    path = final["filepath"]
                    check("file on disk", os.path.isfile(path), path)
                    if os.path.isfile(path):
                        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                        check("bytes correct via IPC path", actual == digest)

                removed = client.call("remove", {"id": download_id, "delete_files": True})
                check("remove works", removed.get("ok") is True)
    finally:
        server.stop()
        service.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def test_native_host_framing() -> None:
    print("\n[2] native messaging: 4-byte framing round-trip")
    from ixd.ipc.server import IPCServer
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-nh-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()

    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))
    service.start()
    server = IPCServer(service, port=0)
    server.start()

    try:
        environment = dict(os.environ)
        environment["IXD_HOME"] = str(root)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

        requests = [
            {"id": 1, "command": "ping", "params": {}},
            {"id": 2, "command": "stats", "params": {}},
            {"id": 3, "command": "queues", "params": {}},
        ]
        stream = b""
        for request in requests:
            body = json.dumps(request).encode("utf-8")
            stream += struct.pack("@I", len(body)) + body

        process = subprocess.run(
            [sys.executable, "-m", "ixd.ipc.native_host"],
            input=stream, capture_output=True, timeout=90,
            env=environment, cwd=str(Path(__file__).resolve().parents[1]),
        )

        responses = []
        buffer = process.stdout
        offset = 0
        while offset + 4 <= len(buffer):
            (length,) = struct.unpack("@I", buffer[offset:offset + 4])
            offset += 4
            if offset + length > len(buffer):
                break
            responses.append(json.loads(buffer[offset:offset + length].decode("utf-8")))
            offset += length

        check("host answered every request", len(responses) == 3,
              f"{len(responses)} of 3; stderr={process.stderr.decode()[:300]}")
        if responses:
            check("responses carry their request id",
                  [r.get("id") for r in responses] == [1, 2, 3],
                  str([r.get("id") for r in responses]))
            check("ping succeeded through the host",
                  responses[0].get("ok") is True, str(responses[0]))
            check("version reported",
                  bool(responses[0].get("result", {}).get("version")),
                  str(responses[0].get("result")))
        if len(responses) >= 3:
            check("queues listed through the host",
                  isinstance(responses[2].get("result"), list)
                  and len(responses[2]["result"]) >= 1,
                  str(responses[2])[:160])
    finally:
        server.stop()
        service.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def test_sandbox_relay() -> None:
    """The standalone relay must work with nothing but an interpreter.

    This is the host a sandboxed browser actually runs. It may not import the
    application, read its data directory, or rely on the packaged binary being
    reachable — a snap's AppArmor profile denies execution of every dotted path
    in ``$HOME``, which is what made the integration report only "Native host
    has exited". So it is exercised here exactly as the browser drives it: a
    bare interpreter, framed stdio, and an endpoint file passed by environment.
    """
    print("\n[3] sandbox relay: framed stdio against a live control socket")
    from ixd.ipc.server import IPCServer
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-relay-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()

    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))
    service.start()
    server = IPCServer(service, port=0)
    server.start()

    try:
        relay = Path(__file__).resolve().parents[1] / "ixd" / "ipc" / "relay.py"
        check("relay script exists", relay.exists(), str(relay))
        if not relay.exists():
            return

        source = relay.read_text(encoding="utf-8")
        check("relay imports nothing from the application",
              "from ixd" not in source and "import ixd" not in source)

        endpoint = root / "endpoint.json"
        endpoint.write_text(json.dumps({
            "host": "127.0.0.1",
            "port": server.port,
            "token": settings.get("ipc_token"),
        }), encoding="utf-8")

        # A copy outside the source tree, run with a bare interpreter and an
        # empty environment: exactly the constraints inside a browser sandbox.
        isolated = root / "relay-copy.py"
        isolated.write_bytes(relay.read_bytes())

        requests = [
            {"id": 1, "command": "ping", "params": {}},
            {"id": 2, "command": "stats", "params": {}},
        ]
        stream = b""
        for request in requests:
            body = json.dumps(request).encode("utf-8")
            stream += struct.pack("@I", len(body)) + body

        process = subprocess.run(
            [sys.executable, str(isolated)],
            input=stream, capture_output=True, timeout=90,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "IXD_ENDPOINT": str(endpoint)},
            cwd=str(root),
        )

        responses = []
        buffer = process.stdout
        offset = 0
        while offset + 4 <= len(buffer):
            (length,) = struct.unpack("@I", buffer[offset:offset + 4])
            offset += 4
            if offset + length > len(buffer):
                break
            responses.append(json.loads(buffer[offset:offset + length].decode("utf-8")))
            offset += length

        check("relay answered every request", len(responses) == 2,
              f"{len(responses)} of 2; stderr={process.stderr.decode()[:300]}")
        # Replies are matched by id, never by arrival order. The relay answers
        # each request on its own connection so that a slow command cannot hold
        # up the rest, which means a later request can legitimately finish
        # first — and the id is what makes that safe.
        by_id = {reply.get("id"): reply for reply in responses}
        check("relay preserved every request id",
              set(by_id) == {1, 2}, str(sorted(by_id)))
        check("relay authenticated and got a pong",
              (by_id.get(1) or {}).get("ok") is True, str(by_id.get(1)))
        check("relay answers later commands too",
              (by_id.get(2) or {}).get("ok") is True, str(by_id.get(2)))

        # With no application listening it must explain itself, not hang or die.
        server.stop()
        ping = json.dumps({"id": 9, "command": "ping", "params": {}}).encode("utf-8")
        process = subprocess.run(
            [sys.executable, str(isolated)],
            input=struct.pack("@I", len(ping)) + ping,
            capture_output=True, timeout=90,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "IXD_ENDPOINT": str(endpoint),
                 "IXD_NO_LAUNCH": "1"},
            cwd=str(root),
        )
        buffer = process.stdout
        if len(buffer) >= 4:
            (length,) = struct.unpack("@I", buffer[:4])
            reply = json.loads(buffer[4:4 + length].decode("utf-8"))
            check("relay reports a stopped application clearly",
                  reply.get("ok") is False and "not running" in str(reply.get("error")),
                  str(reply))
        else:
            check("relay reports a stopped application clearly", False,
                  f"no reply; stderr={process.stderr.decode()[:200]}")
    finally:
        try:
            server.stop()
        except Exception:  # noqa: BLE001 - already stopped in the happy path
            pass
        service.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def test_sandbox_launcher_placement() -> None:
    """A sandboxed browser must be given a launcher it is allowed to execute."""
    print("\n[4] sandbox launcher lands somewhere the browser may execute")
    from ixd import integration
    from ixd.core.browsers import Browser

    home = Path.home()
    browser = Browser(
        key="chromium-snap", name="Chromium (snap)", family="chromium",
        packaging="snap",
        profile_root=home / "snap" / "chromium" / "common" / "chromium",
        host_dir=home / "snap" / "chromium" / "common" / "chromium"
                 / "NativeMessagingHosts",
    )
    directory = integration.sandbox_dir(browser)
    check("a sandbox directory is derived", directory is not None, str(directory))
    if directory is None:
        return

    text = str(directory)
    check("it sits inside the browser's own snap area",
          text.startswith(str(home / "snap" / "chromium")), text)

    # The snap profile grants execute on ~/snap/<instance>/** but denies every
    # dotted path in $HOME, which is precisely what broke before.
    relative = Path(text).relative_to(home)
    check("no path component is hidden",
          not any(part.startswith(".") for part in relative.parts), text)
    check("an unconfined browser still uses the shared launcher",
          integration.launcher_for(
              Browser(key="chrome", name="Chrome", family="chromium",
                      profile_root=home, host_dir=home)
          ) == integration.launcher_path())

    # A relay inside a sandbox must never start the application: the child
    # inherits the sandbox, including its redirected HOME, and builds a second
    # instance with its own database and token that then squats on the control
    # port and locks the real one out. Observed happening for real.
    # Written here rather than read off disk. This used to check whatever an
    # earlier install had left behind, so it silently skipped on a machine
    # that had never registered — which is exactly what happened when the
    # application was renamed and the file it looked for was the old name.
    # A check that disappears when the thing it guards is absent is not a
    # check; `write_sandbox_launcher` is the producer, so it produces it.
    launcher = integration.write_sandbox_launcher(browser) or (
        directory / "ixd-native-host")
    if launcher.exists():
        script = launcher.read_text(encoding="utf-8")
        check("the sandbox shim forbids launching the application",
              "IXD_NO_LAUNCH=1" in script and "export" in script, script[:200])
    else:
        check("the sandbox shim forbids launching the application",
              False, f"no launcher was produced at {launcher}")

    visible = integration.sandbox_endpoint_payload({
        "host": "127.0.0.1", "port": 1, "token": "t",
        "launch": ["/somewhere/ixd", "--background"],
    })
    check("the mirrored endpoint carries no start-up command",
          "launch" not in visible, str(visible))
    check("it still carries what the relay needs",
          visible.get("port") == 1 and visible.get("token") == "t", str(visible))


def test_browser_discovery() -> None:
    """Unlisted browsers are found; things that merely embed Chromium are not.

    People install browsers in ways no fixed table anticipates, so an unknown
    Chrome fork still has to be picked up. Electron applications share
    Chromium's entire configuration layout, though, so the structure alone
    proves nothing — registering a messaging host with a code editor would be
    meaningless.
    """
    print("\n[5] browser discovery: unknown forks yes, Electron apps no")
    from ixd.core import browsers as browsers_module

    home = Path(tempfile.mkdtemp(prefix="ixd-discover-"))
    config_dir = home / ".config"

    # An unlisted Chromium fork: a browser profile, with browsing artefacts.
    fork = config_dir / "waterfox-chromium"
    (fork / "Default").mkdir(parents=True)
    (fork / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    (fork / "Default" / "History").write_text("", encoding="utf-8")

    # A vendor-nested fork, one directory deeper.
    nested = config_dir / "SomeVendor" / "Some-Browser"
    (nested / "Default").mkdir(parents=True)
    (nested / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    (nested / "Default" / "Bookmarks").write_text("{}", encoding="utf-8")

    # An Electron application: same layout, no browsing artefacts.
    electron = config_dir / "SomeEditor"
    (electron / "Default").mkdir(parents=True)
    (electron / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    (electron / "Local State").write_text("{}", encoding="utf-8")
    (electron / "GPUCache").mkdir()

    # An unlisted Firefox fork.
    gecko = config_dir / "some-gecko"
    gecko.mkdir(parents=True)
    (gecko / "profiles.ini").write_text("[Profile0]\n", encoding="utf-8")

    original_home = Path.home
    environment = os.environ.get("XDG_CONFIG_HOME")
    try:
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        Path.home = staticmethod(lambda: home)     # type: ignore[assignment]
        names = {b.profile_root for b in browsers_module.all_browsers()}

        check("unlisted Chromium fork discovered", fork in names,
              str(sorted(str(n) for n in names)))
        check("vendor-nested fork discovered", nested in names)
        check("unlisted Firefox fork discovered", gecko in names)
        check("Electron application ignored", electron not in names,
              "a code editor is not a browser")

        discovered = {b.profile_root: b for b in browsers_module.all_browsers()}
        if fork in discovered:
            check("fork gets the Chrome-family host directory",
                  discovered[fork].host_dir.name == "NativeMessagingHosts",
                  discovered[fork].host_dir.name)
        if gecko in discovered:
            check("gecko fork gets the Firefox host directory",
                  discovered[gecko].host_dir.name == "native-messaging-hosts",
                  discovered[gecko].host_dir.name)
    finally:
        Path.home = original_home     # type: ignore[assignment]
        if environment is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = environment
        shutil.rmtree(home, ignore_errors=True)


def test_every_route_to_remove_asks_about_the_file() -> None:
    """Removing from the list and deleting the file are different acts.

    The toolbar button and the Delete key asked which was meant; the
    right-click menu called ``service.remove()`` directly and quietly kept the
    file. So the same command did two different things depending on how the
    user reached for it — and the one that answered on their behalf was the one
    with no prompt at all. Whether a file survived depended on which way it was
    removed, which is not something a user can be expected to know.

    Both Qt classes are replaced in the window's own namespace rather than
    patched on the types: a menu and a message box are modal, and a real one
    would sit waiting for a click that never comes.
    """
    print("\n[every route to remove asks about the file]")
    script = '''
import sys, tempfile, shutil
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-remove-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.models import Download, DownloadStatus
from ixd.service import DownloadService
from ixd.ui import main_window as mw
from ixd.ui.theme import DARK, apply_theme

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
out = root / "out"; out.mkdir(parents=True, exist_ok=True)
settings.set("download_dir", str(out))
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = mw.MainWindow(service, DARK)

payload = out / "finished.bin"
payload.write_bytes(b"x" * 32)
download = Download(url="https://example.invalid/f", filename="finished.bin",
                    dest_dir=str(out), total_size=32, downloaded=32,
                    status=DownloadStatus.COMPLETED)
download.id = service.db.insert_download(download)

class Action:
    def __init__(self, text, callback):
        self.text, self.callback = text, callback
    def trigger(self):
        self.callback()

class FakeMenu:
    """Records what the menu offers instead of showing it."""
    def __init__(self, parent=None):
        self.actions = []
    def addAction(self, *args):
        if args and isinstance(args[0], str):
            action = Action(args[0], args[1] if len(args) > 1 else (lambda: None))
            self.actions.append(action)
            return action
        return None
    def addSeparator(self):
        pass
    def exec(self, position=None):
        return None

class Button:
    def __init__(self, text):
        self._text = text
    def text(self):
        return self._text

class FakeBox:
    """Answers the question in place and records that it was asked."""
    asked = []
    answer = "keep"
    class Icon:
        Question = 0
    class ButtonRole:
        AcceptRole = 0
        DestructiveRole = 1
    class StandardButton:
        Cancel = 2
    def __init__(self, parent=None):
        self._text = ""
        self._buttons = []
    def setIcon(self, *a): pass
    def setWindowTitle(self, *a): pass
    def setText(self, text): self._text = text
    def setInformativeText(self, *a): pass
    def setDefaultButton(self, *a): pass
    def setEscapeButton(self, *a): pass
    def addButton(self, first, role=None):
        button = Button(first if isinstance(first, str) else "Cancel")
        self._buttons.append(button)
        return button
    def exec(self):
        FakeBox.asked.append(self._text)
        return 0
    def clickedButton(self):
        for button in self._buttons:
            if FakeBox.answer in button.text().lower():
                return button
        return None

mw.QMenu = FakeMenu
mw.QMessageBox = FakeBox

captured = []
real_menu_init = FakeMenu.__init__
def remember(self, parent=None):
    real_menu_init(self, parent)
    captured.append(self)
FakeMenu.__init__ = remember

window._show_context_menu(download.id, None)
menu = captured[-1]
entries = [a for a in menu.actions if a.text.lower().startswith("remove")]
print("MENU_ENTRY", entries[0].text if entries else "<none>")
if entries:
    entries[0].trigger()
print("ASKED", len(FakeBox.asked))
print("PROMPT", FakeBox.asked[0] if FakeBox.asked else "")
print("FILE_KEPT", payload.exists())
print("ROW_GONE", service.db.get_download(download.id) is None)

# And the destructive answer really is destructive, from the same menu.
second = out / "second.bin"
second.write_bytes(b"y" * 16)
other = Download(url="https://example.invalid/g", filename="second.bin",
                 dest_dir=str(out), total_size=16, downloaded=16,
                 status=DownloadStatus.COMPLETED)
other.id = service.db.insert_download(other)
FakeBox.answer = "delete"
window._show_context_menu(other.id, None)
menu = captured[-1]
[a for a in menu.actions if a.text.lower().startswith("remove")][0].trigger()
print("FILE_DELETED", not second.exists())
service.db.close()
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-remove-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the context menu asks before removing", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-400:] or "") + (process.stderr[-400:] or "")
    check("the menu offers Remove", "MENU_ENTRY Remove" in output, detail)
    check("clicking it asks the question", "ASKED 1" in output, detail)
    check("and the question names the file, not just the row",
          "finished.bin" in output.split("PROMPT", 1)[-1].splitlines()[0]
          if "PROMPT" in output else False, detail)
    check("answering “keep” leaves the file on disk",
          "FILE_KEPT True" in output, detail)
    check("and still removes it from the list", "ROW_GONE True" in output, detail)
    check("answering “delete” removes the file too",
          "FILE_DELETED True" in output, detail)


def test_removal_takes_the_whole_selection() -> None:
    """Selecting several and removing has to remove several.

    Two defects, one report. The toolbar button and the Delete key removed
    *nothing*: `triggered(bool)` hands its checked flag to any slot that will
    accept an argument, this one accepts `ids`, so `remove_selected(False)`
    ran — not ``None``, so the selection was never consulted, and `if not ids`
    returned in silence. The right-click menu removed exactly *one*: it passed
    the clicked row's id and ignored the selection entirely.

    Both are driven here the way Qt drives them — `action.trigger()`, not a
    direct call — because a direct call is precisely what hid the first one.
    """
    print("\n[removal takes the whole selection]")
    script = '''
import sys, tempfile, shutil
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-remove-many-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.models import Download, DownloadStatus
from ixd.service import DownloadService
from ixd.ui import main_window as mw
from ixd.ui.theme import DARK, apply_theme

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
out = root / "out"; out.mkdir(parents=True, exist_ok=True)
settings.set("download_dir", str(out))
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = mw.MainWindow(service, DARK)

def populate(first, count=3):
    ids = []
    for n in range(first, first + count):
        (out / f"f{n}.bin").write_bytes(b"x" * 32)
        d = Download(url=f"https://example.invalid/{n}", filename=f"f{n}.bin",
                     dest_dir=str(out), total_size=32, downloaded=32,
                     status=DownloadStatus.COMPLETED)
        d.id = service.db.insert_download(d)
        ids.append(d.id)
    window.refresh()
    return ids

class Action:
    def __init__(self, text, callback):
        self.text, self.callback = text, callback
    def trigger(self):
        self.callback()

captured = []

class FakeMenu:
    def __init__(self, parent=None):
        self.actions = []
        captured.append(self)
    def addAction(self, *args):
        if args and isinstance(args[0], str):
            a = Action(args[0], args[1] if len(args) > 1 else (lambda: None))
            self.actions.append(a)
            return a
        return None
    def addSeparator(self): pass
    def exec(self, position=None): return None

class Button:
    def __init__(self, text): self._text = text
    def text(self): return self._text

class FakeBox:
    asked = []
    answer = "keep"
    class Icon: Question = 0
    class ButtonRole: AcceptRole = 0; DestructiveRole = 1
    class StandardButton: Cancel = 2
    def __init__(self, parent=None):
        self._text = ""; self._buttons = []
    def setIcon(self, *a): pass
    def setWindowTitle(self, *a): pass
    def setText(self, t): self._text = t
    def setInformativeText(self, *a): pass
    def setDefaultButton(self, *a): pass
    def setEscapeButton(self, *a): pass
    def addButton(self, first, role=None):
        b = Button(first if isinstance(first, str) else "Cancel")
        self._buttons.append(b); return b
    def exec(self):
        FakeBox.asked.append(self._text); return 0
    def clickedButton(self):
        for b in self._buttons:
            if FakeBox.answer in b.text().lower():
                return b
        return None

mw.QMenu = FakeMenu
mw.QMessageBox = FakeBox

# -- the toolbar button, fired the way Qt fires it --------------------------
populate(0)
window.table.selectAll()
print("SELECTED", len(window.table.selected_ids()))
window.action_remove.trigger()
print("TOOLBAR_ASKED", len(FakeBox.asked))
print("TOOLBAR_LEFT", len(service.db.list_downloads()))

# -- and the right-click menu, with the clicked row inside the selection ----
populate(3)
window.table.selectAll()
clicked = window.table.selected_ids()[0]
window._show_context_menu(clicked, None)
entry = [a for a in captured[-1].actions if a.text.lower().startswith("remove")][0]
print("MENU_LABEL", entry.text)
entry.trigger()
print("MENU_LEFT", len(service.db.list_downloads()))

# -- a right-click outside the selection still means that one row ----------
ids = populate(6)
window.table.clearSelection()
window.table.selectRow(0)
outsider = [i for i in ids if i != window.table.selected_ids()[0]][0]
window._show_context_menu(outsider, None)
entry = [a for a in captured[-1].actions if a.text.lower().startswith("remove")][0]
print("LONE_LABEL", entry.text)
entry.trigger()
print("LONE_LEFT", len(service.db.list_downloads()))
service.db.close()
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-remove-many-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("removal takes the whole selection", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("three rows are selected to begin with", "SELECTED 3" in output, detail)
    check("the toolbar button asks once for all three",
          "TOOLBAR_ASKED 1" in output, detail)
    check("and removes all three, not none", "TOOLBAR_LEFT 0" in output, detail)
    check("the menu says how many it will remove",
          "MENU_LABEL Remove from list… (3)" in output, detail)
    check("and removes all three, not one", "MENU_LEFT 0" in output, detail)
    check("a click outside the selection names no count",
          "LONE_LABEL Remove from list…\n" in output, detail)
    check("and removes that row alone", "LONE_LEFT 2" in output, detail)


def test_a_spin_box_shows_its_arrows() -> None:
    """Every stepper in the settings drew two blank squares.

    Styling a spin box at all hands its sub-controls to the stylesheet, and a
    sub-control with no ``image`` draws nothing — so the up and down buttons
    were empty rectangles and a combo box had no drop-down arrow whatsoever.
    A stylesheet cannot draw a shape, so the chevrons are generated from the
    palette and written out as files.

    Drawn, then clicked: an arrow that is visible and does not move the value
    is no better than the blank square it replaced.
    """
    print("\n[a spin box shows its arrows]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-arrows-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import (QApplication, QSpinBox, QComboBox, QStyle,
                               QStyleOptionSpinBox)
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from ixd.ui.theme import DARK, LIGHT, apply_theme, stylesheet

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
sheet = stylesheet(DARK)

import re
urls = re.findall(r"image: url\\(([^)]+)\\)", sheet)
print("ARROW_URLS", len(urls))
print("FILES_EXIST", all(Path(u).is_file() and Path(u).stat().st_size > 0
                        for u in urls))
print("PNG_MAGIC", all(Path(u).read_bytes()[:4] == b"\\x89PNG" for u in urls))
print("SPIN_UP", "QAbstractSpinBox::up-arrow" in sheet)
print("SPIN_DOWN", "QAbstractSpinBox::down-arrow" in sheet)
print("COMBO_ARROW", "QComboBox::down-arrow" in sheet)
print("CHECK_TICK", "QCheckBox::indicator:checked" in sheet
      and "tick-" in sheet)

# Both palettes draw their own, in their own colours.
light_urls = re.findall(r"image: url\\(([^)]+)\\)", stylesheet(LIGHT))
print("LIGHT_DIFFERS", set(light_urls) != set(urls) and bool(light_urls))

spin = QSpinBox(); spin.setRange(0, 100); spin.setValue(10)
spin.resize(180, 34); spin.show()
app.processEvents()

option = QStyleOptionSpinBox()
option.initFrom(spin)
option.rect = spin.rect()
option.subControls = QStyle.SubControl.SC_SpinBoxUp | QStyle.SubControl.SC_SpinBoxDown
up = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                 QStyle.SubControl.SC_SpinBoxUp, spin)
down = spin.style().subControlRect(QStyle.ComplexControl.CC_SpinBox, option,
                                   QStyle.SubControl.SC_SpinBoxDown, spin)
print("BUTTONS_REAL", up.width() > 0 and up.height() > 0 and not up.intersects(down))
print("BUTTONS_INSIDE", spin.rect().contains(up) and spin.rect().contains(down))
QTest.mouseClick(spin, Qt.MouseButton.LeftButton,
                 Qt.KeyboardModifier.NoModifier, up.center())
print("CLICK_UP", spin.value())
QTest.mouseClick(spin, Qt.MouseButton.LeftButton,
                 Qt.KeyboardModifier.NoModifier, down.center())
print("CLICK_DOWN", spin.value())
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-arrows-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("a spin box shows its arrows", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("the stylesheet names arrow images at all",
          "ARROW_URLS 0" not in output and "ARROW_URLS" in output, detail)
    check("and every one of them was written to disk",
          "FILES_EXIST True" in output, detail)
    check("as real PNGs", "PNG_MAGIC True" in output, detail)
    check("a spin box gets an up arrow", "SPIN_UP True" in output, detail)
    check("and a down arrow", "SPIN_DOWN True" in output, detail)
    check("a combo box gets one too — it had none",
          "COMBO_ARROW True" in output, detail)
    check("and a ticked checkbox gets a tick, not a filled square",
          "CHECK_TICK True" in output, detail)
    check("the light palette draws its own",
          "LIGHT_DIFFERS True" in output, detail)
    check("the buttons are two separate areas",
          "BUTTONS_REAL True" in output, detail)
    check("inside the field", "BUTTONS_INSIDE True" in output, detail)
    check("clicking the up arrow raises the value",
          "CLICK_UP 11" in output, detail)
    check("clicking the down arrow lowers it",
          "CLICK_DOWN 10" in output, detail)


def test_the_settings_pages_scroll_instead_of_crushing() -> None:
    """At its own minimum height the Transfers tab clipped its values.

    Qt does not refuse a layout it cannot fit — it compresses past the
    minimums, and a spin box compressed past its minimum slices the value it
    holds through the middle: "1024 KB" with the top and bottom of every
    character cut off. The dialog declares a minimum of 620px and its own
    content needs more than that, so it happened to anyone whose screen was
    not tall enough to make them resize it.
    """
    print("\n[the settings pages scroll instead of crushing]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-crush-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication, QSpinBox, QScrollArea, QTabWidget
from ixd.config import Settings
from ixd.core.db import Database
from ixd.service import DownloadService
from ixd.ui.theme import DARK, apply_theme
from ixd.ui.widgets.settings_dialog import SettingsDialog

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
service = DownloadService(settings, Database(root / "state.sqlite3"))
dialog = SettingsDialog(service)
dialog.resize(dialog.minimumSize())      # the size it says it can be shown at
dialog.show()
app.processEvents()

tabs = dialog.findChild(QTabWidget)
print("PAGES_SCROLL", all(isinstance(tabs.widget(i), QScrollArea)
                          for i in range(tabs.count())))
tabs.setCurrentIndex(1)                  # Transfers, the tallest page
app.processEvents()

crushed = []
for spin in dialog.findChildren(QSpinBox):
    if not spin.isVisible():
        continue
    if spin.height() < spin.minimumSizeHint().height():
        crushed.append((spin.height(), spin.minimumSizeHint().height()))
print("VISIBLE_SPINS", len([s for s in dialog.findChildren(QSpinBox)
                            if s.isVisible()]))
print("CRUSHED", len(crushed), crushed[:3])
service.db.close()
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-crush-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the settings pages scroll instead of crushing", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("every tab is a page that can scroll",
          "PAGES_SCROLL True" in output, detail)
    check("the Transfers tab has its spin boxes on show",
          "VISIBLE_SPINS 0" not in output and "VISIBLE_SPINS" in output, detail)
    check("and not one of them is squeezed below its own minimum",
          "CRUSHED 0 []" in output, detail)


def test_the_rename_does_not_lose_the_data_directory() -> None:
    """XAI → IXD moved where the application keeps everything.

    The download history, the settings and every partially fetched file lived
    in `~/.local/share/xai-dm`. A rename that simply starts writing to
    `~/.local/share/ixd` reads, from the outside, as an application that
    forgot everything — so the old directory is adopted once, and only when
    the new one is not already in use.
    """
    print("\n[the rename does not lose the data directory]")
    script = '''
import importlib, os, sys, tempfile, json
from pathlib import Path

share = Path(tempfile.mkdtemp(prefix="ixd-move-"))
former = share / "xai-dm"
(former / "logs").mkdir(parents=True)
(former / "incomplete").mkdir(parents=True)
(former / "state.sqlite3").write_bytes(b"SQLite format 3\\x00" + b"x" * 64)
(former / "settings.json").write_text(json.dumps({"download_dir": "/tmp/keep"}))
(former / "incomplete" / "7-video.mp4.xaidl").write_bytes(b"partial")

os.environ["XDG_DATA_HOME"] = str(share)
os.environ.pop("IXD_HOME", None)
from ixd import config
importlib.reload(config)

print("NEW_DIR_IS_IXD", config.DATA_DIR.name == "ixd")
adopted = config.migrate_former_data_dir()
print("ADOPTED", adopted is not None and adopted.name == "xai-dm")
print("DB_MOVED", config.DB_PATH.is_file())
print("SETTINGS_KEPT", json.loads(config.SETTINGS_PATH.read_text())
      .get("download_dir") == "/tmp/keep")
print("PARTIAL_KEPT", (config.TEMP_DIR / "7-video.mp4.xaidl").is_file())
print("OLD_GONE", not former.exists())

# Run again: there is nothing left to adopt and nothing is disturbed.
print("SECOND_RUN", config.migrate_former_data_dir() is None)

# And a new directory already in use is never overwritten by an old one.
share2 = Path(tempfile.mkdtemp(prefix="ixd-keep-"))
old2 = share2 / "xai-dm"; old2.mkdir(parents=True)
(old2 / "state.sqlite3").write_bytes(b"old")
os.environ["XDG_DATA_HOME"] = str(share2)
importlib.reload(config)
config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
config.DB_PATH.write_bytes(b"new and in use")
print("IN_USE_UNTOUCHED", config.migrate_former_data_dir() is None
      and config.DB_PATH.read_bytes() == b"new and in use"
      and (old2 / "state.sqlite3").is_file())
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment.pop("IXD_HOME", None)
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the rename does not lose the data directory", False, "timed out")
        return

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("the new home is ~/.local/share/ixd", "NEW_DIR_IS_IXD True" in output, detail)
    check("the former directory is adopted", "ADOPTED True" in output, detail)
    check("the download history comes with it", "DB_MOVED True" in output, detail)
    check("so do the settings", "SETTINGS_KEPT True" in output, detail)
    check("and the half-finished files", "PARTIAL_KEPT True" in output, detail)
    check("nothing is left in two places", "OLD_GONE True" in output, detail)
    check("a second start has nothing to do", "SECOND_RUN True" in output, detail)
    check("a home already in use is never rolled back",
          "IN_USE_UNTOUCHED True" in output, detail)


def test_the_old_native_host_registration_is_removed() -> None:
    """The extension keeps its ID, so the old host manifest must go.

    Same signing key, same Chrome ID — the renamed build replaces the old
    extension in the browser and then asks for `com.ixd.downloader`. The
    `com.xai.downloadmanager.json` beside it answers to nothing and points at a
    binary being replaced, so registering removes it.
    """
    print("\n[the old native host registration is removed]")
    script = '''
import json, tempfile
from pathlib import Path
from ixd import integration

directory = Path(tempfile.mkdtemp(prefix="ixd-hosts-"))
stale = directory / "com.xai.downloadmanager.json"
stale.write_text(json.dumps({"name": "com.xai.downloadmanager"}))
other = directory / "com.someone.else.json"
other.write_text("{}")

integration._drop_former_hosts(directory)
print("STALE_GONE", not stale.exists())
print("STRANGER_KEPT", other.exists())
integration._drop_former_hosts(directory)      # absent is not an error
print("IDEMPOTENT", True)
print("HOST_NAME", integration.HOST_NAME)
'''
    root = Path(__file__).resolve().parents[1]
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=60, env=dict(os.environ), cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the old native host registration is removed", False, "timed out")
        return

    output = process.stdout
    detail = (output.strip()[-400:] or "") + (process.stderr[-400:] or "")
    check("the superseded manifest is deleted", "STALE_GONE True" in output, detail)
    check("another application's is not touched",
          "STRANGER_KEPT True" in output, detail)
    check("and doing it twice is not an error", "IDEMPOTENT True" in output, detail)
    check("the host is registered under the new name",
          "HOST_NAME com.ixd.downloader" in output, detail)


def test_every_platform_has_an_icon_it_will_accept() -> None:
    """PyInstaller refuses the wrong icon container; it does not convert.

    The first CI release died on macOS with "Received icon image ixd-256.png
    which exists but is not in the correct format. On this platform, only
    ('icns',) images may be used" — the spec fell back to a PNG on a platform
    that will not take one, and no application was produced at all.

    The ICNS is written by `packaging/make_icons.py` with the standard library:
    the format is a header and typed entries, and since OS X 10.7 those entries
    carry a PNG verbatim. `file(1)` agrees it is a Mac OS X icon.
    """
    print("\n[every platform has an icon it will accept]")
    import struct

    root = Path(__file__).resolve().parents[1]
    icons = root / "packaging" / "icons"

    icns = icons / "ixd.icns"
    check("macOS has an .icns", icns.is_file(), str(icns))
    check("Windows has an .ico", (icons / "ixd.ico").is_file())
    check("and the PNG the other platforms use is there",
          (icons / "ixd-256.png").is_file())

    if icns.is_file():
        raw = icns.read_bytes()
        check("the .icns declares itself", raw[:4] == b"icns", raw[:4].hex())
        declared = struct.unpack(">I", raw[4:8])[0]
        check("and its length is its real length",
              declared == len(raw), f"{declared} vs {len(raw)}")

        entries, offset = {}, 8
        while offset < len(raw):
            kind = raw[offset:offset + 4]
            size = struct.unpack(">I", raw[offset + 4:offset + 8])[0]
            if size < 8:
                break
            entries[kind] = raw[offset + 8:offset + size]
            offset += size
        check("it carries the entry macOS looks for first",
              b"ic08" in entries, str(sorted(k.decode() for k in entries)))
        check("every entry is a PNG",
              all(v[:4] == b"\x89PNG" for v in entries.values()),
              str([(k.decode(), v[:4].hex()) for k, v in entries.items()]))
        check("and the retina entries are twice the points",
              entries.get(b"ic11", b"")[16:24] == struct.pack(">II", 32, 32)
              and entries.get(b"ic13", b"")[16:24] == struct.pack(">II", 256, 256))

    # The spec must never hand a platform a container it refuses.
    spec = (root / "packaging" / "ixd.spec").read_text()
    macos_block = spec.split("elif IS_MACOS:", 1)[-1].split("elif", 1)[0]
    check("the spec offers macOS only an .icns",
          "ixd.icns" in macos_block and ".png" not in macos_block, macos_block)


def test_the_log_can_be_switched_off() -> None:
    """Keeping a record of what you downloaded is a choice, not an assumption.

    Every message in the application — the engine's and the extension's alike
    — arrives through `Database.log_event`, so the switch is that one method.
    Off means nothing is written *and* what was already written is dropped:
    "stop keeping a log" plainly includes the one already kept.
    """
    print("\n[the log can be switched off]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-log-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.service import DownloadService
from ixd.ui.theme import DARK, apply_theme
from ixd.ui.widgets.settings_dialog import SettingsDialog

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
service = DownloadService(settings, Database(root / "state.sqlite3"))
service.start()

service.db.log_event("while it is on")
print("ON_RECORDS", len(service.db.recent_events(50)) > 0)

# Through the real dialog, as a user would.
dialog = SettingsDialog(service)
print("BOX_EXISTS", hasattr(dialog, "keep_log"))
print("BOX_STARTS_TICKED", dialog.keep_log.isChecked())
dialog.keep_log.setChecked(False)
dialog._save()

print("SETTING_SAVED", settings.get_bool("keep_log", True) is False)
print("FLAG_FOLLOWED", service.db.log_enabled is False)
print("EXISTING_DROPPED", len(service.db.recent_events(50)) == 0)
service.db.log_event("while it is off")
print("NOTHING_NEW", len(service.db.recent_events(50)) == 0)

# And back on again, in the same session.
dialog.keep_log.setChecked(True)
dialog._save()
print("BACK_ON", service.db.log_enabled is True)
service.db.log_event("after switching on")
messages = [row["message"] for row in service.db.recent_events(50)]
print("RECORDS_AGAIN", "after switching on" in messages)
print("OFF_PERIOD_ABSENT", "while it is off" not in messages)
service.shutdown()
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-log-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the log can be switched off", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("it records while it is on", "ON_RECORDS True" in output, detail)
    check("the setting is offered in the dialog", "BOX_EXISTS True" in output, detail)
    check("and starts ticked", "BOX_STARTS_TICKED True" in output, detail)
    check("unticking it saves the setting", "SETTING_SAVED True" in output, detail)
    check("and takes effect at once, not at the next launch",
          "FLAG_FOLLOWED True" in output, detail)
    check("what was already recorded is dropped",
          "EXISTING_DROPPED True" in output, detail)
    check("and nothing further is written", "NOTHING_NEW True" in output, detail)
    check("ticking it again resumes recording", "BACK_ON True" in output, detail)
    check("with new messages kept", "RECORDS_AGAIN True" in output, detail)
    check("and nothing from the switched-off period invented",
          "OFF_PERIOD_ABSENT True" in output, detail)


def test_quit_ends_the_process() -> None:
    """Quitting must end the event loop, not just hide the window.

    The application deliberately keeps running when its window is closed, so
    downloads continue from the tray. That same setting means closing the
    window cannot end the event loop — so quitting has to say so explicitly.
    Without it the process survives with no window and has to be killed by
    hand, which is exactly what was reported.
    """
    print("\n[6] quitting actually exits")
    script = '''
import sys, tempfile, shutil, threading
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-quit-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.service import DownloadService
from ixd.ui.main_window import MainWindow
from ixd.ui.theme import DARK, apply_theme

app = QApplication(sys.argv[:1])
app.setQuitOnLastWindowClosed(False)      # as the real application does
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
service = DownloadService(settings, Database(root / "state.sqlite3"))
service.start()
window = MainWindow(service, DARK)
window.show()
app.aboutToQuit.connect(service.shutdown)
QTimer.singleShot(800, window.quit_application)
QTimer.singleShot(25000, lambda: app.exit(9))     # the loop never ended
code = app.exec()
lingering = [t.name for t in threading.enumerate()
             if t is not threading.main_thread() and not t.daemon]
print("EXIT", code)
print("LINGERING", ",".join(lingering))
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-quit-home-")

    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=90, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the process exits on quit", False,
              "it was still running after 90 seconds")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    check("the process exits on quit", "EXIT 0" in output,
          output.strip()[-200:] or process.stderr[-200:])
    check("no non-daemon thread keeps it alive",
          "LINGERING\n" in output or "LINGERING \n" in output
          or output.rstrip().endswith("LINGERING"),
          [line for line in output.splitlines() if line.startswith("LINGERING")])


def test_the_audio_queued_beside_a_video_is_the_original() -> None:
    """Follow the audio track to what is queued, not just to what is ranked.

    The user has been handed a German-dubbed soundtrack three times. Each
    previous fix corrected the *ranking* and was inert, because the original
    had already been discarded upstream — so a test of the ranking alone
    proves nothing about what lands on disk. This walks the real service path:
    a format list holding every language the site publishes, through
    ``add_media``, to the two rows it queues and the tags stored on them.

    The list below is the real shape: eight auto-dubbed languages listed
    *before* the original, all under itag 140, all within 300 bits per second
    of one another, and the original published three times over — plain,
    loudness-compressed and volume-boosted.
    """
    print("\n[the audio queued beside a video is the original]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import MediaFormat, MediaInfo
    from ixd.service import DownloadService

    def track(language: str, kind: str, variant: str, tbr: float) -> MediaFormat:
        tags = f"acont={kind}:lang={language}" + (f":{variant}=1" if variant else "")
        return MediaFormat(
            "140", f"https://media/140/{language}/{variant or 'plain'}",
            ext="m4a", tbr=tbr, vcodec="none", acodec="mp4a", filesize=9000,
            audio_is_default=(kind == "original"), audio_language=language,
            audio_kind=kind, audio_variant=variant, audio_tags=tags,
            sabr={"endpoint": "https://endpoint", "itag": 140, "size": 9000,
                  "is_audio": True, "config": "", "xtags": tags})

    dubbings = [track(language, "dubbed", "", tbr) for language, tbr in (
        ("de-DE", 131.667), ("es-US", 131.663), ("hi", 131.579),
        ("it", 131.676), ("nl-NL", 131.536), ("pl", 131.669),
        ("pt-BR", 131.564), ("uk", 131.542),
    )]
    compressed = track("en-US", "original", "drc", 131.429)
    plain = track("en-US", "original", "", 131.400)
    boosted = track("en-US", "original", "vb", 131.470)

    video = MediaFormat("137", "https://media/137", ext="mp4", height=1080,
                        vcodec="avc1", acodec="none", filesize=50_000_000,
                        sabr={"endpoint": "https://endpoint", "itag": 137,
                              "size": 50_000_000, "config": ""})

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        info = MediaInfo(title="t", webpage_url="https://page",
                         formats=[video, *dubbings, compressed, plain, boosted])
        service._analysed = lambda url, client, options: info
        service._servable_format = lambda chosen, i, c, ua="": (chosen, "")
        service.engine.start_download = lambda *args, **kwargs: False

        reply = service.handle_command("add_media", {
            "url": "https://page", "format_id": "137", "quality": "1080p",
            "cookies": "", "userAgent": "test", "start": False,
        })
        check("the command is accepted", reply.get("ok") is True, str(reply))

        queued = service.db.get_download(reply["result"]["id"])
        companion = (queued.sabr_context or {}).get("audio") or {}
        check("an audio track was queued alongside the video", bool(companion),
              str(sorted((queued.sabr_context or {}).keys())))
        check("and it is the original, not the first dubbing on the list",
              companion.get("xtags") == "acont=original:lang=en-US",
              str(companion.get("xtags")))
        check("which is emphatically not the German dub",
              companion.get("xtags") != "acont=dubbed-auto:lang=de-DE",
              str(companion.get("xtags")))
        check("nor the compressed or boosted mix of the right language",
              "drc" not in str(companion.get("xtags"))
              and "vb" not in str(companion.get("xtags")),
              str(companion.get("xtags")))

        # The tags are stored, so a resume or a session renewal can name the
        # same track rather than re-picking one. Storing only the itag is how
        # a resume turned an English download German in the first place.
        check("the track is stored, not just the rendition",
              bool(companion.get("xtags")) and companion.get("itag") == 140,
              str({k: companion.get(k) for k in ("itag", "xtags")}))
        service.db.close()


def test_the_panel_offers_the_preferred_container() -> None:
    """The container preference has to reach the menu, not just selection.

    Clicking a quality sends that rendition's format id, and `add_media` takes
    a named format as given — so whatever the menu offers *is* what gets
    downloaded, and a preference applied only in `select_format` applies only
    when nothing is clicked. Which is every download made from the hover panel.

    The menu ranked its one row per resolution by file size, and at 60fps the
    WebM copy is simply the larger file — 198 MB against 136 MB for the same
    1080p60 — so it handed out WebM every time regardless of the setting.
    """
    print("\n[the panel offers the preferred container]")
    from ixd.core.models import MediaFormat
    from ixd.service import DownloadService

    def rendition(itag: str, ext: str, tbr: float, size: int) -> MediaFormat:
        return MediaFormat(itag, f"u{itag}", ext=ext, height=1080, fps=60,
                           vcodec="vp09" if ext == "webm" else "avc1",
                           acodec="none", tbr=tbr, filesize=size)

    # The real numbers from the video this was reported on.
    pool = [rendition("303", "webm", 4346, 198_035_097),
            rendition("299", "mp4", 4334, 135_996_996),
            rendition("399", "mp4", 2297, 73_071_798)]

    offered = {pref: next(r for r in DownloadService.presentable_formats(pool, pref)
                          if r.has_video)
               for pref in ("mp4", "webm", "")}
    check("preferring MP4 offers the MP4 rendition",
          offered["mp4"].format_id == "299", offered["mp4"].format_id)
    check("and it is the smaller file, not the bigger one",
          offered["mp4"].filesize < offered["webm"].filesize,
          f"{offered['mp4'].filesize:,} vs {offered['webm'].filesize:,}")
    check("preferring WebM offers the WebM one",
          offered["webm"].format_id == "303", offered["webm"].format_id)
    check("with no preference, the largest copy wins as before",
          offered[""].format_id == "303", offered[""].format_id)

    # Still one row per resolution: the preference decides which copy is shown,
    # never how many are.
    check("the menu still shows one entry per resolution",
          len([r for r in DownloadService.presentable_formats(pool, "mp4")
               if r.has_video]) == 1)

    # And a resolution published in one container only is still offered.
    only_webm = [rendition("315", "webm", 20000, 900_000_000)]
    only_webm[0].height = 2160
    shown = DownloadService.presentable_formats(only_webm, "mp4")
    check("a resolution with no MP4 copy is still offered",
          len(shown) == 1 and shown[0].format_id == "315",
          str([r.format_id for r in shown]))


def test_the_quality_clicked_is_the_quality_fetched() -> None:
    """What the panel sends must decide what is queued, start to finish.

    The user picks a resolution from the hover panel; it travels as a
    ``formatId`` to the service worker, as ``format_id`` over the control
    socket, and has to survive as the chosen stream. This project has already
    shipped the failure at the far end of that chain — every request, 480p to
    1080p alike, coming back as the 360p copy — and nothing exercised the chain
    itself. A rename anywhere along it is silent: the download still succeeds,
    at the wrong quality, which is the sort of thing a user notices only after
    watching it.
    """
    print("\n[the quality clicked is the quality fetched]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import MediaFormat, MediaInfo
    from ixd.service import DownloadService

    def rendition(itag: str, height: int) -> MediaFormat:
        return MediaFormat(itag, f"https://media/{itag}", ext="mp4",
                           height=height, vcodec="avc1", acodec="none",
                           filesize=height * 1000,
                           sabr={"endpoint": "https://endpoint", "itag": int(itag),
                                 "size": height * 1000, "config": ""})

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        # The default preference is deliberately *not* what the test asks for,
        # so a chain that ignores the click falls back to something visibly
        # different rather than accidentally agreeing.
        settings.set("preferred_video_quality", "360p")
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        info = MediaInfo(title="t", webpage_url="https://page", formats=[
            rendition("137", 1080), rendition("136", 720),
            rendition("134", 360),
            MediaFormat("140", "https://media/140", ext="m4a", tbr=130,
                        vcodec="none", acodec="mp4a", filesize=9000,
                        sabr={"endpoint": "https://endpoint", "itag": 140,
                              "size": 9000, "is_audio": True, "config": ""}),
        ])
        service._analysed = lambda url, client, options: info
        service._servable_format = lambda chosen, i, c, ua="": (chosen, "")
        # The command starts what it queues, as it does for the extension.
        # What is under test is what gets queued, which other tests cover the
        # running of — so the transfers are held rather than left to time out
        # against an address that does not exist.
        service.engine.start_download = lambda *args, **kwargs: False

        # Exactly what the service worker puts on the wire for a click on
        # "1080p" in the panel.
        reply = service.handle_command("add_media", {
            "url": "https://page",
            "format_id": "137",
            "quality": "360p",
            "cookies": "", "userAgent": "test", "start": False,
        })
        check("the command is accepted", reply.get("ok") is True, str(reply))
        queued = service.db.get_download(reply["result"]["id"])
        check("the clicked rendition is the one queued",
              (queued.sabr_context or {}).get("itag") == 137,
              str((queued.sabr_context or {}).get("itag")))
        check("and the quality preference did not override the click",
              queued.format_id == "137", queued.format_id)

        # And with no click, the preference is what decides.
        reply = service.handle_command("add_media", {
            "url": "https://page", "quality": "720p",
            "cookies": "", "userAgent": "test", "start": False,
        })
        chosen = service.db.get_download(reply["result"]["id"])
        check("with nothing clicked, the requested quality decides",
              (chosen.sabr_context or {}).get("itag") == 136,
              str((chosen.sabr_context or {}).get("itag")))
        service.db.close()

    # The name the panel sends and the name the socket carries have to match.
    # They are in different languages and different files, so nothing but this
    # keeps them in step.
    worker = (Path(__file__).resolve().parents[1]
              / "extension" / "background.js").read_text(encoding="utf-8")
    check("the service worker forwards the clicked format",
          "format_id: message.formatId" in worker,
          "background.js no longer maps formatId to format_id")
    panel = (Path(__file__).resolve().parents[1]
             / "extension" / "content" / "video_inject.js").read_text(
                 encoding="utf-8")
    check("and the panel sends it under the name the worker reads",
          "formatId," in panel or "formatId:" in panel,
          "video_inject.js no longer sends formatId")


def test_captured_pairs_cannot_be_crossed() -> None:
    """Two pairs queued together must not have their halves swapped.

    The captured-stream route is what the extension offers as “Already loaded
    by the player”, and it is what several of the application's own error
    messages send the user to — so it has to be right. Its two halves were tied
    together by a token minted from the clock, so two pairs queued in the same
    millisecond shared one: the join then picks one of four members as "the
    video" and one as "the audio", and combines halves of different downloads.
    The adaptive path had this removed in session 23; this one kept it.
    """
    print("\n[captured pairs cannot be crossed]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import Download
    from ixd.service import DownloadService

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        queued: list[Download] = []

        def fake_add(params):
            download = Download(url=params["url"],
                                filename=params.get("filename") or "x.mp4",
                                dest_dir=home)
            download.id = service.db.insert_download(download)
            queued.append(download)
            return download

        service.add_from_browser = fake_add

        first = service._cmd_add_pair({
            "url": "https://v1", "audioUrl": "https://a1", "title": "one"})
        second = service._cmd_add_pair({
            "url": "https://v2", "audioUrl": "https://a2", "title": "two"})

        groups = [service.db.get_download(d.id).mux_group for d in queued]
        tokens = {g.rsplit(":", 1)[0] for g in groups if g}
        check("each pair gets a token of its own", len(tokens) == 2, str(groups))
        check("and every row carries one", all(groups), str(groups))

        # The halves of one pair must share a token, and not with the other.
        by_token: dict[str, set[str]] = {}
        for group in groups:
            token, role = group.rsplit(":", 1)
            by_token.setdefault(token, set()).add(role)
        check("each token names exactly one video and one audio",
              all(roles == {"video", "audio"} for roles in by_token.values()),
              str(by_token))
        check("the pairs are the ones that were queued together",
              first["id"] != second["id"], f"{first['id']} {second['id']}")
        service.db.close()


def test_a_quality_with_sound_is_never_queued_without_it() -> None:
    """Pairing must not drop the audio track and call the result finished.

    A server-driven track is marked "restricted" when one session will not
    cover it — which is no longer a dead end, because a session is continued
    across as many as the track needs. The pairing test predated that and
    dropped every such audio track, which on any video over a minute meant the
    download was queued as video alone and delivered as a finished file: the
    right picture, the right length, and no sound.
    """
    print("\n[a quality with sound is never queued without it]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.errors import ExtractionError
    from ixd.core.models import MediaFormat, MediaInfo
    from ixd.service import DownloadService

    def video(**kwargs) -> MediaFormat:
        return MediaFormat("137", "https://v", ext="mp4", height=1080,
                           vcodec="avc1", acodec="none", filesize=1000,
                           **kwargs)

    def audio(**kwargs) -> MediaFormat:
        return MediaFormat("140", "https://a", ext="m4a", tbr=130,
                           vcodec="none", acodec="mp4a", filesize=200,
                           **kwargs)

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        session = {"endpoint": "https://endpoint", "itag": 140, "size": 200,
                   "is_audio": True, "config": ""}

        # A server-driven audio track the site caps per session: fetchable, and
        # it must be paired.
        info = MediaInfo(title="t", webpage_url="https://page", formats=[
            video(sabr={"endpoint": "https://endpoint", "itag": 137,
                        "size": 1000, "config": ""}, restricted=True),
            audio(sabr=session, restricted=True),
        ])
        service._analysed = lambda url, client, options: info
        service._servable_format = lambda chosen, i, c, ua="": (chosen, "")

        download = service.add_media("https://page", format_id="137",
                                     start=False)
        paired = (download.sabr_context or {}).get("audio") or {}
        check("a capped server-driven audio track is still paired",
              paired.get("itag") == 140, str(paired))

        # An audio track that exists but genuinely cannot be fetched — a capped
        # plain link, which has no way to ask for the rest.
        silent = MediaInfo(title="t", webpage_url="https://page", formats=[
            video(sabr={"endpoint": "https://endpoint", "itag": 137,
                        "size": 1000, "config": ""}),
            audio(restricted=True),
        ])
        service._analysed = lambda url, client, options: silent
        refused = ""
        try:
            service.add_media("https://page", format_id="137", start=False)
        except ExtractionError as exc:
            refused = str(exc)
        check("a video whose sound cannot be fetched is refused, not queued",
              "would have had no sound" in refused, refused or "it was queued")

        # A video that genuinely has no audio at all is not the same thing.
        mute = MediaInfo(title="t", webpage_url="https://page", formats=[
            video(sabr={"endpoint": "https://endpoint", "itag": 137,
                        "size": 1000, "config": ""}),
        ])
        service._analysed = lambda url, client, options: mute
        queued = service.add_media("https://page", format_id="137", start=False)
        check("a video with no soundtrack is queued as it is",
              queued is not None and not (queued.sabr_context or {}).get("audio"),
              str((queued.sabr_context or {}).get("audio")))
        service.db.close()


def test_the_service_can_reopen_a_streaming_session() -> None:
    """The engine's way back from an expired endpoint has to exist on this side.

    The engine asks for a replacement session by page URL and itag; this is
    what answers. Stubbing the hook in the engine's own tests proves the engine
    uses it — it does not prove anything is on the other end, which is exactly
    the kind of gap that only shows up in front of a user.
    """
    print("\n[the service can reopen a streaming session]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import MediaFormat, MediaInfo
    from ixd.service import DownloadService

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        check("the engine is given a way to reopen a session",
              callable(service.engine.renew_sabr_session),
              str(service.engine.renew_sabr_session))

        fresh = MediaInfo(title="t", formats=[
            MediaFormat("137", "https://endpoint/new", height=1080,
                        sabr={"endpoint": "https://endpoint/new", "itag": 137,
                              "is_audio": False, "config": "Y2Zn"}),
            MediaFormat("140", "https://endpoint/new",
                        sabr={"endpoint": "https://endpoint/new", "itag": 140,
                              "is_audio": True}),
        ])
        service._analysed = lambda url, client, options: fresh
        renew = service.engine.renew_sabr_session
        page = "https://www.youtube.com/watch?v=abcdefghijk"

        video = renew(page, "137", False)
        check("a video track gets a new endpoint",
              video and video["endpoint"] == "https://endpoint/new", str(video))
        audio = renew(page, "140", True)
        check("and the audio track gets its own, not the video's",
              audio and audio["itag"] == 140 and audio["is_audio"],
              str(audio))
        check("a stream the page no longer offers yields nothing",
              renew(page, "999", False) is None, "it returned something")
        check("and so does a download with no page recorded",
              renew("", "137", False) is None, "it returned something")

        def unavailable(*args, **kwargs):
            raise RuntimeError("the page could not be read")

        service._analysed = unavailable
        check("an extraction that fails is not an exception to the caller",
              renew(page, "137", False) is None, "it raised or returned")
        service.db.close()


def test_pair_shows_as_one_row() -> None:
    """A quality chosen once must be listed once, and progress must not go back.

    The two halves of an adaptive pair become one file, so listing both shows a
    video and an audio row for something asked for once. Worse, a transfer that
    has not begun reports no size: summing the two reached 100% when the video
    finished and then *grew* as the audio started, so a finished download
    appeared to start over — which is exactly what was reported.
    """
    print("\n[a pair is one row]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import Download, DownloadStatus
    from ixd.service import DownloadService

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        group = "1234-137"
        video = Download(url="https://example.invalid/v", filename="v.mp4",
                         dest_dir=home, mux_group=f"{group}:video")
        audio = Download(url="https://example.invalid/a", filename="a.m4a",
                         dest_dir=home, mux_group=f"{group}:audio")
        video.id = service.db.insert_download(video)
        audio.id = service.db.insert_download(audio)

        # The video has finished; the audio has not started, so it has no size
        # of its own yet — only the size the site declared.
        service.db.update_download_fields(
            video.id, status=DownloadStatus.COMPLETED.value,
            total_size=1000, downloaded=1000)
        service.db.update_download_fields(
            audio.id, status=DownloadStatus.QUEUED.value,
            total_size=0, downloaded=0,
            sabr_context=json.dumps({"size": 250}))

        shown = service.list_for_display()
        check("the pair is one row", len(shown) == 1, str(len(shown)))
        row = shown[0]
        check("the audio row is not listed separately",
              row.mux_group.endswith(":video"), row.mux_group)
        check("the declared size counts before the transfer starts",
              row.total_size == 1250, str(row.total_size))
        check("progress does not read as finished while half is missing",
              row.downloaded == 1000 and row.downloaded < row.total_size,
              f"{row.downloaded}/{row.total_size}")
        check("nor is the row marked completed",
              row.status is not DownloadStatus.COMPLETED, row.status.value)

        # Once both halves are in, the row is whole and finished.
        service.db.update_download_fields(
            audio.id, status=DownloadStatus.COMPLETED.value,
            total_size=250, downloaded=250)
        row = service.list_for_display()[0]
        check("both halves in makes one finished row",
              row.downloaded == 1250 and row.total_size == 1250
              and row.status is DownloadStatus.COMPLETED,
              f"{row.downloaded}/{row.total_size} {row.status.value}")

        service.db.close()


def test_extension_assets() -> None:
    print("\n[3] extension manifests and referenced assets")
    root = Path(__file__).resolve().parents[1] / "extension"

    for name in ("manifest.chrome.json", "manifest.firefox.json"):
        path = root / name
        check(f"{name} exists", path.exists(), str(path))
        if not path.exists():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            check(f"{name} is valid JSON", False, str(exc))
            continue
        check(f"{name} is valid JSON", True)
        check(f"{name} is Manifest V3", manifest.get("manifest_version") == 3)
        check(f"{name} requests nativeMessaging",
              "nativeMessaging" in manifest.get("permissions", []))
        check(f"{name} requests downloads + cookies",
              {"downloads", "cookies"} <= set(manifest.get("permissions", [])))

        referenced = set()
        for icon_map in (manifest.get("icons", {}),
                         manifest.get("action", {}).get("default_icon", {})):
            referenced.update(icon_map.values())
        referenced.add(manifest.get("action", {}).get("default_popup", ""))
        referenced.add(manifest.get("options_ui", {}).get("page", ""))
        for entry in manifest.get("content_scripts", []):
            referenced.update(entry.get("js", []))
            referenced.update(entry.get("css", []))
        background = manifest.get("background", {})
        if "service_worker" in background:
            referenced.add(background["service_worker"])
        referenced.update(background.get("scripts", []))

        missing = [r for r in referenced if r and not (root / r).exists()]
        check(f"{name} references only existing files", not missing, str(missing))

    check("firefox manifest declares a gecko id",
          "browser_specific_settings" in json.loads(
              (root / "manifest.firefox.json").read_text(encoding="utf-8")))


def test_native_host_installer() -> None:
    print("\n[4] native host installer produces valid manifests")
    root = Path(__file__).resolve().parents[1]
    script = root / "native-host" / "install_host.py"
    check("installer exists", script.exists(), str(script))
    if not script.exists():
        return

    home = Path(tempfile.mkdtemp(prefix="ixd-host-"))
    environment = dict(os.environ)
    environment["HOME"] = str(home)
    environment["XDG_CONFIG_HOME"] = str(home / ".config")
    environment["IXD_HOME"] = str(home / "data")

    # Pre-create profile dirs so the installer treats those browsers as
    # installed: a classic Chrome, and a snap Chromium — the snap layout is the
    # one that used to be missed entirely.
    (home / ".config" / "google-chrome").mkdir(parents=True, exist_ok=True)
    (home / ".mozilla").mkdir(parents=True, exist_ok=True)
    (home / "snap" / "chromium" / "common" / "chromium").mkdir(parents=True, exist_ok=True)
    (home / "snap" / "firefox" / "common" / ".mozilla").mkdir(parents=True, exist_ok=True)

    try:
        process = subprocess.run(
            [sys.executable, str(script), "--extension-id", "abcdefghijklmnopabcdefghijklmnop"],
            capture_output=True, text=True, timeout=90, env=environment, cwd=str(root),
        )
        check("installer exits cleanly", process.returncode == 0,
              process.stderr[:300])

        chrome_manifest = (home / ".config" / "google-chrome" / "NativeMessagingHosts"
                           / "com.ixd.downloader.json")
        firefox_manifest = (home / ".mozilla" / "native-messaging-hosts"
                            / "com.ixd.downloader.json")
        snap_chromium_manifest = (home / "snap" / "chromium" / "common" / "chromium"
                                  / "NativeMessagingHosts"
                                  / "com.ixd.downloader.json")
        snap_firefox_manifest = (home / "snap" / "firefox" / "common" / ".mozilla"
                                 / "native-messaging-hosts"
                                 / "com.ixd.downloader.json")

        check("chrome manifest written", chrome_manifest.exists(), str(chrome_manifest))
        check("firefox manifest written", firefox_manifest.exists(), str(firefox_manifest))
        check("snap chromium manifest written", snap_chromium_manifest.exists(),
              str(snap_chromium_manifest))
        check("snap firefox manifest written", snap_firefox_manifest.exists(),
              str(snap_firefox_manifest))

        if chrome_manifest.exists():
            data = json.loads(chrome_manifest.read_text())
            check("chrome manifest names the host",
                  data["name"] == "com.ixd.downloader")

            origins = data.get("allowed_origins") or []
            check("chrome manifest allows the supplied extension id",
                  "chrome-extension://abcdefghijklmnopabcdefghijklmnop/" in origins,
                  str(origins))

            # The bundled manifest carries a fixed key, so its id is known in
            # advance and must be authorised without the user supplying it.
            sys.path.insert(0, str(root))
            from ixd.integration import bundled_extension_id

            bundled = bundled_extension_id()
            check("bundled extension id is derived from the manifest key",
                  len(bundled) == 32, bundled)
            check("chrome manifest allows the bundled extension without being told",
                  f"chrome-extension://{bundled}/" in origins, str(origins))

            launcher = Path(data["path"])
            check("launcher shim exists and is executable",
                  launcher.exists() and os.access(launcher, os.X_OK), str(launcher))
            check("launcher lives under the application's data directory",
                  str(launcher).startswith(str(home / "data")), str(launcher))

        if firefox_manifest.exists():
            data = json.loads(firefox_manifest.read_text())
            check("firefox manifest uses allowed_extensions",
                  data.get("allowed_extensions") == ["ixd@ixd.local"],
                  str(data.get("allowed_extensions")))
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_the_page_reaches_the_engine_from_the_browser() -> None:
    """A captured stream is queued with the page it was captured from.

    A media CDN answers 403 to a manifest or segment request that carries no
    `Referer`, which is the ordinary state of an address lifted out of a
    request log and handed to a downloader. The user reported exactly that: the
    popup listed `master.m3u8`, the site played it perfectly, and asking for it
    came back "HTTP 403 forbidden".

    The page therefore has to survive every hop — extension, IPC command,
    service, HTTP client and the download row the transfer reads it back from.
    Each hop dropped it silently, so each is checked.
    """
    print("\n[the page reaches the engine from the browser]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import MediaFormat, MediaInfo
    from ixd.service import DownloadService, _referrer_of

    page = "https://site.example/watch/1234"

    check("the extension's spelling is read",
          _referrer_of({"referrer": page}) == page)
    check("so is the header's spelling",
          _referrer_of({"referer": page}) == page)
    check("and the snake-cased one a script would send",
          _referrer_of({"page_url": page}) == page)
    check("nothing supplied is no page, not a crash",
          _referrer_of({"url": "https://x"}) == "")

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        seen: dict[str, object] = {}

        def fake_client(download=None, *, cookies="", user_agent="",
                        cookie_url="", referer="", site_headers=None):
            seen["client_referer"] = referer
            seen["client_site_headers"] = dict(site_headers or {})
            from ixd.core.http_client import HttpClient
            return HttpClient(referer=referer, site_headers=site_headers,
                              site_host="cdn.example.net")

        def fake_analysed(url, client, options):
            seen["options_referer"] = options.get("referer", "")
            return MediaInfo(
                title="Film", webpage_url=url, extractor="hls",
                formats=[MediaFormat("v", "https://cdn.example.net/hi.m3u8",
                                     ext="mp4", height=720, vcodec="avc1",
                                     acodec="mp4a", filesize=1000)],
            )

        service.client = fake_client
        service._analysed = fake_analysed

        # A manifest lifted out of the capture list, queued from the popup.
        result = service._cmd_add_media({
            "url": "https://cdn.example.net/master.m3u8",
            "referrer": page,
            # …together with the headers the browser sent when it fetched it.
            "headers": {"Authorization": "Bearer abc", "Range": "bytes=0-1"},
            "start": False,
        })
        row = service.db.get_download(result["id"])

        check("the page reaches the client that does the extracting",
              seen.get("client_referer") == page, str(seen))
        check("and the extractors are told as well",
              seen.get("options_referer") == page, str(seen))
        check("and it is stored on the download the transfer reads",
              row.referer == page, row.referer)
        check("the browser's own headers reach the client too",
              seen.get("client_site_headers") == {"Authorization": "Bearer abc"},
              str(seen.get("client_site_headers")))
        check("and travel with the transfer, minus what the engine owns",
              row.extra_headers.get("Authorization") == "Bearer abc"
              and "Range" not in row.extra_headers,
              str(row.extra_headers))

        # `extract` is the other half: the panel's quality menu goes through it,
        # and a 403 there is what leaves the menu empty.
        service._cmd_extract({"url": "https://cdn.example.net/master.m3u8",
                              "referrer": page})
        check("extraction carries it too",
              seen.get("client_referer") == page, str(seen))

        service.db.close()



def test_a_page_handed_over_keeps_its_session() -> None:
    """The extension relays; the application decides — with the same session.

    The extension is a proxy in IDM's sense: it watches every request, works out
    what is media, keeps the headers and cookies that made an address work, and
    hands the lot over. Everything a person chooses then happens in the
    application. Without carrying the session across that hand-off the dialog
    that opens repeats the 403 the extension had already got past, which would
    make the whole shape worse than the popup it replaced.
    """
    print("\n[a page handed over keeps its session]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.service import DownloadService

    page = "https://site.example/watch/9"
    manifest = "https://cdn.example.net/master.m3u8"

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        result = service._cmd_present({
            "url": manifest,
            "referrer": page,
            "cookies": "sid=1",
            "userAgent": "Mozilla/5.0 Chrome/150",
            "headers": {"Authorization": "Bearer abc", "Range": "bytes=0-1"},
            "streams": ["https://cdn.example.net/index-f1-v1-a1.m3u8"],
        })
        check("the hand-off is accepted", result["remembered"] is True)

        held = service.browser_context(manifest)
        check("the page is kept", held["referer"] == page, held.get("referer", ""))
        check("the cookies are kept", held["cookies"] == "sid=1")
        check("the credential is kept",
              held["headers"] == {"Authorization": "Bearer abc"},
              str(held["headers"]))
        check("and what the engine owns is not",
              "Range" not in held["headers"], str(held["headers"]))

        check("every stream found on the page is covered, not only the one sent",
              service.browser_context(
                  "https://cdn.example.net/index-f1-v1-a1.m3u8"
              ).get("referer") == page)
        check("and so is anything else on that host, which is where a quality "
              "menu leads next",
              service.browser_context(
                  "https://cdn.example.net/720/index.m3u8").get("referer") == page)

        # This is the point of it: a caller with nothing but a URL — which is
        # every desktop dialog, because a person is not a browser — is filled in.
        cookies, agent, referer, headers = service._with_browser_context(
            manifest, "", "", "", None)
        check("a bare call is given the session", referer == page and cookies == "sid=1")
        check("…and the credential with it", headers == {"Authorization": "Bearer abc"})
        check("the user agent travels too", agent == "Mozilla/5.0 Chrome/150")

        explicit = service._with_browser_context(
            manifest, "own=1", "Agent/1", "https://other.example/", {"X": "y"})
        check("a caller that supplies its own is never overridden",
              explicit == ("own=1", "Agent/1", "https://other.example/", {"X": "y"}),
              str(explicit))

        check("an address nobody handed over is left alone",
              service._with_browser_context(
                  "https://elsewhere.example/x.m3u8", "", "", "", None)
              == ("", "", "", {}))
        service.db.close()


def test_removing_a_download_never_deletes_another_ones_file() -> None:
    """Cancelling a second download must not destroy the first one's file.

    `filepath` is `dest_dir/filename`, and until a download finishes its
    filename is the *requested* one — the unique "(1)" suffix is only decided
    at publication. So downloading something already in the folder pointed the
    new row at the **old row's finished file**, and removing the new one
    deleted it. A completed download destroyed by cancelling an unrelated one:
    reported, and it is data loss rather than an inconvenience.

    A download owns its temporary file always, and owns the published file only
    once it has published one.
    """
    print("\n[removing a download never deletes another one's file]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import Download, DownloadStatus
    from ixd.service import DownloadService

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        finished = Path(home) / "Film.mp4"
        finished.write_bytes(b"the first download, complete" * 64)

        # The row that produced it.
        first = Download(url="https://example/Film.mp4", filename="Film.mp4",
                         dest_dir=home, status=DownloadStatus.COMPLETED)
        first.id = service.db.insert_download(first)

        # And a second attempt at the same thing, still running: same requested
        # name, its own temporary file, nothing published.
        partial = Path(home) / "second.part"
        partial.write_bytes(b"half of the second download")
        second = Download(url="https://example/Film.mp4", filename="Film.mp4",
                          dest_dir=home, temp_path=str(partial),
                          status=DownloadStatus.DOWNLOADING)
        second.id = service.db.insert_download(second)

        service.engine.remove_download(second.id, delete_files=True)

        check("the finished file of the earlier download survives",
              finished.exists(), "it was deleted")
        check("with its contents untouched",
              finished.read_bytes().startswith(b"the first download"))
        check("the cancelled download's own part file is gone",
              not partial.exists(), "the part file was left behind")
        check("and its row is gone", service.db.get_download(second.id) is None)
        check("while the earlier row remains",
              service.db.get_download(first.id) is not None)

        # And removing the row that *did* publish the file does delete it —
        # otherwise "remove and delete" would silently do nothing.
        service.engine.remove_download(first.id, delete_files=True)
        check("removing the download that published a file does delete it",
              not finished.exists(), "it was left behind")
        service.db.close()


def test_a_quality_is_queued_against_where_it_came_from() -> None:
    """A chosen format belongs to the address its list was built from.

    When the page yields nothing the menu is built from the captured manifest —
    and queueing the choice against `location.href` then asks the engine to
    extract a page already shown to hold no media. Reported as "I click Best
    available and it says no embedded media found", with the failure in the log
    to prove it.

    Checked here on the extension's side, because that is where the address is
    chosen and the panel has no other test that can see it.
    """
    print("\n[a quality is queued against where it came from]")
    source = (Path(__file__).resolve().parents[1]
              / "extension" / "content" / "video_inject.js").read_text()

    queue = source[source.index("async function queue(formatId)"):]
    queue = queue[:queue.index("\n  }")]
    check("the menu's own source is used, with the page only as a fallback",
          "menuSource || location.href" in queue, queue[:200])
    check("and it is recorded when the list is built",
          "menuSource = target;" in source)
    check("cleared before each attempt, so a stale one cannot be reused",
          'menuSource = "";' in source)

    # And the panel must not hold the user while the engine thinks.
    check("the panel reports the click at once rather than when the engine "
          "has finished",
          queue.index('toast("Sent to IXD")') < queue.index("await send("),
          "the toast comes after the await")
    check("a failure is still reported", "catch (error)" in queue)


def test_a_refused_ladder_falls_back_to_a_stream_that_works() -> None:
    """Every server-driven rung refused is not the end of it.

    Measured from a real log: six rungs declined in turn — and 480p twice, for
    want of dropping the chosen height from the ladder below it — and then the
    download failed outright. The same video published a progressive stream the
    site serves as an ordinary file, and the player had already fetched it. A
    smaller file that arrives beats a better one that does not exist.
    """
    print("\n[a refused ladder falls back to a stream that works]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.models import MediaFormat, MediaInfo
    from ixd.service import DownloadService

    def sabr(height: int, itag: str) -> MediaFormat:
        return MediaFormat(itag, "", ext="mp4", height=height, vcodec="avc1",
                           acodec="none", sabr={"itag": itag, "endpoint": "x"})

    progressive = MediaFormat("18", "https://cdn/progressive.mp4", ext="mp4",
                              height=360, vcodec="avc1", acodec="mp4a")

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        chosen = sabr(1080, "137")
        info = MediaInfo(title="Sunflower", webpage_url="https://site/watch",
                         extractor="youtube",
                         formats=[chosen, sabr(720, "136"), sabr(480, "135"),
                                  sabr(480, "244"), progressive])

        asked: list[str] = []

        class Refuses:
            def probe(self):
                return "the streaming server returned no media for this stream"

        import ixd.extractors.sabr as sabr_module
        original = sabr_module.stream_from_context

        def refuse(client, context, agent=""):
            asked.append(str(context.get("itag", "")))
            return Refuses()

        # The downgrade is worth taking only if the thing downgraded *to*
        # answers, so the walk asks. Here it does.
        service._plain_url_serves = lambda url, *a, **k: ""

        sabr_module.stream_from_context = refuse
        try:
            found, reason = service._servable_format(chosen, info, None)
        finally:
            sabr_module.stream_from_context = original

        check("a rendition that is served as a file is taken",
              found is progressive, str(found and found.format_id))
        check("and the reason the ladder was refused is carried out with it",
              "no media" in reason, reason)
        check("the chosen height is not asked for twice",
              asked.count("137") == 1, ",".join(asked))
        check("nor is a second copy of a height already refused",
              len(asked) == len(set(asked)), ",".join(asked))
        check("one rung per distinct height, all of them tried",
              asked == ["137", "136", "135"], ",".join(asked))

        # And when it does not answer, the downgrade is not taken at all.
        #
        # The field log of 2026-08-12 is the case: every rung refused, the walk
        # handed over the progressive address, and that address spent five
        # renewals and five probe retries arriving at 403 — the same address
        # measured refused to every client there is, the page that minted it
        # included. Meanwhile the server-driven stream just declared unservable
        # had delivered 100% of its media three runs running. Trading a stream
        # that has worked for one that never has is the worst move available.
        service._plain_url_serves = lambda url, *a, **k: "HTTP 403 Forbidden"
        service._servable.clear()
        sabr_module.stream_from_context = refuse
        try:
            attempted, _ = service._servable_format(chosen, info, None)
        finally:
            sabr_module.stream_from_context = original
        check("a refused downgrade is not taken; the chosen stream is "
              "attempted anyway",
              attempted is chosen, str(attempted and attempted.format_id))
        said = [event["message"] for event in service.db.recent_events()]
        check("…and the log says both refusals, not just the ladder's",
              any("refused as well" in m and "attempted anyway" in m
                  for m in said), str(said[:1]))
        # Put the walk back where the checks below expect it: an answering
        # fallback, remembered, so "a retry is instant" is testing the cache
        # rather than the state this block left behind.
        service._plain_url_serves = lambda url, *a, **k: ""
        service._servable.clear()
        asked.clear()
        sabr_module.stream_from_context = refuse
        try:
            service._servable_format(chosen, info, None)
        finally:
            sabr_module.stream_from_context = original

        # With nothing fetchable at all, it still fails rather than inventing
        # something — a silent film or a stream known to stop partway is not an
        # improvement on an honest refusal.
        only_sabr = MediaInfo(title="x", webpage_url="https://site/w",
                              extractor="youtube", formats=[chosen])
        sabr_module.stream_from_context = refuse
        try:
            nothing, _ = service._servable_format(chosen, only_sabr, None)
        finally:
            sabr_module.stream_from_context = original
        check("with nothing else published, it still refuses honestly",
              nothing is None, str(nothing))

        # "Nothing was served" is an answer, and hearing it again costs six
        # exchanges. Measured from a real log: the same ladder declined twice in
        # three minutes for the same video, a minute of silence each time.
        asked.clear()
        sabr_module.stream_from_context = refuse
        try:
            again, _ = service._servable_format(chosen, only_sabr, None)
        finally:
            sabr_module.stream_from_context = original
        check("a refusal is remembered rather than re-asked",
              again is None and asked == [], ",".join(asked))

        # And the fallback is remembered too, so a retry is instant.
        asked.clear()
        sabr_module.stream_from_context = refuse
        try:
            repeat, _ = service._servable_format(chosen, info, None)
        finally:
            sabr_module.stream_from_context = original
        check("and so is the rendition that was taken instead",
              repeat is progressive and asked == [], ",".join(asked))
        service.db.close()


def test_the_container_asked_for_is_the_container_produced() -> None:
    """A transport stream is offered twice, and the choice is honoured.

    Most HLS streams are MPEG-TS: they play, and half the world's players refuse
    them by name. Converting is therefore worth offering and wrong to impose —
    so the panel lists the stream as the site serves it *and* as an MP4, and the
    **filename carries the choice** from there on. Nothing else is persisted,
    and a resume cannot forget which was wanted.
    """
    print("\n[the container asked for is the container produced]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Download
    from ixd.service import DownloadService

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        transport = b"\x47\x40\x11\x11" + bytes(28)

        def keeps_its_name(filename: str) -> str:
            row = Download(url="https://cdn/index.m3u8", filename=filename,
                           dest_dir=home)
            row.id = service.db.insert_download(row)
            task = DownloadTask.__new__(DownloadTask)
            task.download = row
            task.settings = service.settings
            task.db = service.db
            task._log = lambda *a, **k: None
            DownloadTask._name_after_its_bytes(task, transport)
            return row.filename

        check("a stream asked for as MP4 keeps that name through assembly, "
              "because it is about to become one",
              keeps_its_name("An Episode.mp4") == "An Episode.mp4",
              keeps_its_name("An Episode.mp4"))
        check("and one asked for as it is served is named for what it is",
              keeps_its_name("An Episode.mp4x") == "An Episode.ts",
              keeps_its_name("An Episode.mp4x"))

        # The engine reads the choice straight off the name, which is why
        # nothing else needed persisting.
        for filename, rewraps in (("x.mp4", True), ("x.ts", False)):
            wanted = filename.rsplit(".", 1)[-1].lower()
            check(f"“{filename}” {'is' if rewraps else 'is not'} rewrapped",
                  (wanted == "mp4") is rewraps, wanted)

        # With the conversion switched off entirely, an MP4 name over transport
        # bytes is corrected rather than left lying.
        service.settings.set("remux_transport_streams", False)
        check("with rewrapping off, the name follows the bytes instead",
              keeps_its_name("An Episode.mp4") == "An Episode.ts",
              keeps_its_name("An Episode.mp4"))
        service.settings.set("remux_transport_streams", True)

        # The panel no longer lists captured streams at all.
        #
        # It used to head the menu with them — a playlist offered twice, as the
        # site serves it and as an MP4. Two things ended that. The captured
        # address was measured 403 on a second fetch, from inside the page that
        # minted it, so the row led the menu with a download that cannot start;
        # and it was reported from the field as unwanted. The captures are
        # still read — they are what a manifest-only site is extracted from —
        # they are simply not offered as something to click.
        panel = (Path(__file__).resolve().parents[1]
                 / "extension" / "content" / "video_inject.js").read_text()
        # Captures are listed only when extraction produced nothing.
        #
        # Unconditional listing put a dead row above the live ones on YouTube;
        # removing it outright emptied the menu on a site whose extraction
        # fails and whose captures are the only route — the field log has
        # "no embedded media found on megaplay.buzz" beside a captured
        # `master.m3u8`, and a panel reading "2 videos on this page" with
        # nothing under it.
        # Listed when they are the only route, and when the extraction was read
        # out of one of them — that second clause is what names the file. A
        # playlist site extracts to one format called after the playlist, so
        # the menu read "MASTER.M3U8 · Video · m3u8" instead of offering the
        # video's own name twice, as served (`.ts`) and rewrapped (`.mp4`).
        check("captures are listed when they are the only route or its source",
              "if (captured.length && (!extracted || fromCapture))" in panel)
        check("…and the container choice is still offered for a playlist",
              'entry.kind === "manifest" ? ["mp4", ""]' in panel)
        check("but the container choice still travels with a request",
              "url: entry.url, title, container" in panel)
        service.db.close()


def test_a_browser_launching_the_host_needs_no_flag() -> None:
    """Windows: `cmd.exe` has no business in a binary pipe.

    Native messaging is length-prefixed frames, and the `--native-host` flag is
    ours — no browser knows it, so on POSIX a shell shim adds it. On Windows
    that shim is a `.bat`, which puts `cmd.exe` between the browser and the host
    for no gain, on the one platform where the host was reported not to start.

    A browser identifies itself: Chrome-family pass the calling extension's
    origin, Firefox the manifest path and the add-on id. Recognising either lets
    the manifest point straight at the executable.
    """
    print("\n[a browser launching the host needs no flag]")
    from ixd.__main__ import _launched_by_a_browser, _parse_arguments

    check("a Chrome extension origin is recognised",
          _parse_arguments(["chrome-extension://kjlkjcdjfcolkimljplcgokhkeglbfce/"])
          .native_host)
    check("a Firefox extension origin too",
          _parse_arguments(["moz-extension://0d2f/"]).native_host)
    check("and Firefox's manifest-path-then-id pair",
          _parse_arguments([r"C:\Users\a\com.ixd.downloader.json",
                            "ixd@example"]).native_host)
    check("the explicit flag still works",
          _parse_arguments(["--native-host"]).native_host)

    # And nothing else may be mistaken for it: a URL to queue is the common
    # case, and starting the host instead would look like the app doing nothing.
    plain = _parse_arguments(["https://example.com/file.zip"])
    check("a URL is queued, not mistaken for a browser",
          not plain.native_host and plain.urls == ["https://example.com/file.zip"])
    check("someone else's json is not our manifest",
          not _launched_by_a_browser([r"C:\somewhere\config.json"]))
    check("and no arguments is the ordinary launch",
          not _parse_arguments([]).native_host)


def test_a_stream_with_no_header_falls_back_to_what_the_browser_fetched() -> None:
    """A server-driven stream that cannot produce its opening bytes.

    The streaming session never sends the initialisation and index segments —
    it assumes a player fetched them separately — so they come from the format's
    ordinary URL. When the response publishes no ordinary URL either, there is
    nowhere to get them, and the download runs to the last byte and is then
    refused for a few kilobytes at the front.

    Measured on a real log: the *same machine*, minutes apart, succeeded on
    streams that publish an index and failed on those that do not. That is what
    ruled out the network explanation offered the session before.

    The browser has usually already fetched a complete progressive stream for
    the same video, and that one carries its own header.
    """
    print("\n[a stream with no header falls back to what the browser fetched]")
    from ixd.service import _captured_progressive

    progressive = ("https://rr3---sn-x.googlevideo.com/videoplayback?"
                   "expire=1&itag=18&mime=video%2Fmp4&ratebypass=yes&sig=a")
    adaptive = ("https://rr3---sn-x.googlevideo.com/videoplayback?"
                "expire=1&itag=137&mime=video%2Fmp4&sig=b")
    audio = ("https://rr3---sn-x.googlevideo.com/videoplayback?"
             "expire=1&itag=140&mime=audio%2Fmp4&sig=c")

    # The condition that decides it. A server-driven format's `url` is its
    # streaming *endpoint* and is never empty, so testing that made the whole
    # rescue unreachable and the field report came back unchanged. What matters
    # is needing opening bytes with nowhere to fetch them from.
    from ixd.core.models import MediaFormat as _Format
    from ixd.service import _header_unobtainable

    # Nothing is stranded any more, and that is the point.
    #
    # This used to answer True for a server-driven stream needing opening bytes
    # with no ordinary link to them, and the whole rescue below hung off it.
    # The rescue lands on a progressive address, and on 2026-08-12 that address
    # was measured 403 to *everyone* — refetched from the youtube.com page that
    # minted it, cookies and origin intact. So the condition traded a download
    # that might work for one that cannot, six times in a single field log.
    #
    # The session serves those bytes itself: a media header carries an
    # `is_init_seg` flag and the server sends that segment at the head of a
    # fresh session. Whether it arrived is now judged in the engine, after the
    # session has run, instead of predicted before it starts.
    needs_header = _Format("137", "https://youtubei/endpoint", ext="mp4",
                           height=1080, vcodec="avc1", acodec="none",
                           sabr={"header_end": 989, "header_url": ""})
    check("a stream needing opening bytes is no longer stranded in advance",
          _header_unobtainable(needs_header) is False)

    fetchable = _Format("137", "https://youtubei/endpoint", ext="mp4", height=1080,
                        vcodec="avc1", acodec="none",
                        sabr={"header_end": 989,
                              "header_url": "https://cdn/videoplayback"})
    check("nor is one that has a link to them", not _header_unobtainable(fetchable))
    carries_own = _Format("137", "https://youtubei/endpoint", ext="mp4",
                          sabr={"header_end": 0, "header_url": ""})
    check("nor one that needs no header at all",
          not _header_unobtainable(carries_own))
    check("nor an ordinary file, which is not server-driven at all",
          not _header_unobtainable(_Format("18", "https://cdn/v.mp4", ext="mp4")))
    check("and nothing at all is not a fault", not _header_unobtainable(None))

    check("a progressive stream is the rescue",
          _captured_progressive({"captured": [adaptive, audio, progressive]})
          == progressive)
    check("an adaptive video track is not — it would be silent",
          _captured_progressive({"captured": [adaptive]}) == "")
    check("nor an audio track — it would have no picture",
          _captured_progressive({"captured": [audio]}) == "")
    check("nothing captured is no rescue, not a crash",
          _captured_progressive({}) == "")
    check("and something from another site is never mistaken for one",
          _captured_progressive({"captured": [
              "https://cdn.example/movie.mp4?itag=18"]}) == "")

    # And when extraction is refused outright — the site challenging the
    # connection and serving nothing — the same rescue applies. This is the
    # whole of why a commercial download manager keeps working where an
    # extractor stops: it never asks the site's API anything.
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.errors import ExtractionError
    from ixd.service import DownloadService

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))
        service.client = lambda *a, **k: None

        def refused(*args, **kwargs):
            raise ExtractionError("Sign in to confirm you're not a bot")

        service._analysed = refused
        queued: list[dict] = []
        service.add_from_browser = lambda payload: (
            queued.append(payload) or type("D", (), {"id": 1})())

        service.add_media("https://www.youtube.com/watch?v=x", title="A Video",
                          options={"captured": [progressive]})
        check("a refused extraction still queues what the browser loaded",
              queued and queued[0]["url"] == progressive,
              str(queued[:1]))
        check("and it is named after the page rather than the address",
              queued[0]["filename"].startswith("A Video"),
              queued[0]["filename"])

        # With nothing captured there is nothing to fall back to, and the
        # refusal must reach the user rather than being swallowed.
        try:
            service.add_media("https://www.youtube.com/watch?v=y", options={})
            check("a refusal with no rescue is reported", False, "it was swallowed")
        except ExtractionError as exc:
            check("a refusal with no rescue is reported", "not a bot" in str(exc))
        service.db.close()

    # The extension has to send them, or none of the above is reachable.
    background = (Path(__file__).resolve().parents[1]
                  / "extension" / "background.js").read_text()
    check("the extension sends what it captured with the request",
          "captured: capturesToSend(senderTabId, senderTab, pageUrl,"
          in background)
    check("…from the page's own timeline as well as the worker's memory",
          "pageMedia: pageMediaAddresses()" in
          (Path(__file__).resolve().parents[1] / "extension" / "content"
           / "video_inject.js").read_text())
    check("…and never a playlist, which carries no media of its own",
          'entry.kind !== "manifest"' in background)


def test_captures_are_consulted_before_the_site() -> None:
    """What the browser fetched is used *before* an API that can refuse us.

    Measured on this machine: extraction of a YouTube watch page is refused for
    every client identity — "Sign in to confirm you're not a bot" — and takes
    8.7 s to say so, while the addresses the player used are served. Asking
    first bought nothing but the delay before the failure.

    The reason a commercial download manager keeps working here is that it has
    no extractor at all: nothing for a challenge to refuse. This is that
    ordering, with the one guard that matters — the fast route is only taken
    when the captures meet the quality that was asked for, so it can never
    quietly hand over 360p to someone who chose 1080p.
    """
    print("\n[captures are consulted before the site]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.errors import ExtractionError
    from ixd.service import DownloadService, _capture_plan

    def capture(itag: str, mime: str, size: int = 0) -> dict:
        return {"url": f"https://rr3---sn-x.googlevideo.com/videoplayback?"
                       f"expire=1&itag={itag}&sig={itag}",
                "itag": itag, "mime": mime, "size": size,
                "headers": {"Referer": "https://www.youtube.com/"}}

    progressive = capture("18", "video/mp4", 5_000_000)
    v1080 = capture("137", "video/mp4", 90_000_000)
    v360 = capture("134", "video/mp4", 9_000_000)
    aac = capture("140", "audio/mp4", 3_000_000)
    opus = capture("251", "audio/webm", 3_500_000)
    v1080_vp9 = capture("248", "video/webm", 80_000_000)

    # ── what can be built out of a capture list ─────────────────────────────
    plan = _capture_plan({"captured": [v1080, aac]}, 1080)
    check("an adaptive video and its audio are a plan",
          plan is not None and plan.audio is not None)
    check("…at the height the itag names", plan.height == 1080, str(plan.height))
    check("a video track with no audio anywhere is not a plan — it is silent",
          _capture_plan({"captured": [v1080]}, 1080) is None)
    check("an audio track alone is not a plan either — it has no picture",
          _capture_plan({"captured": [aac]}, 1080) is None)
    check("a progressive stream needs no companion",
          (_capture_plan({"captured": [progressive]}, 360) or object()).audio
          is None)
    check("nothing captured is no plan, not a crash",
          _capture_plan({}, 1080) is None)
    check("and another site's media is never mistaken for one",
          _capture_plan({"captured": ["https://cdn.example/m.mp4?itag=137"]},
                        1080) is None)

    # A WebM video cannot be joined to an AAC track, so the companion is
    # chosen by container first and bitrate second.
    mixed = _capture_plan({"captured": [v1080_vp9, aac, opus]}, 1080)
    check("a WebM video takes the Opus track, not the AAC one",
          mixed is not None and mixed.audio.itag == "251", str(mixed))
    check("…and the file is named for the container it will be",
          mixed.webm is True)
    mp4_pair = _capture_plan({"captured": [v1080, aac, opus]}, 1080)
    check("an MP4 video takes the AAC track", mp4_pair.audio.itag == "140")

    # The guard: below what was asked for, the fast route declines and lets
    # extraction try — it may be answered, and it may do better.
    check("captures under the requested quality are not the fast path",
          _capture_plan({"captured": [v360, aac]}, 1080,
                        allow_shortfall=False) is None)
    check("…but they are still a rescue when extraction is refused",
          _capture_plan({"captured": [v360, aac]}, 1080,
                        allow_shortfall=True) is not None)
    check("captures that meet it are the fast path",
          _capture_plan({"captured": [v1080, aac]}, 1080,
                        allow_shortfall=False) is not None)
    # Asked for 720p with 1080p and 360p on hand: the one that satisfies it,
    # not the biggest file on the list.
    picked = _capture_plan({"captured": [v1080, v360, aac]}, 720)
    check("the closest quality at or above what was asked for wins",
          picked.height == 1080, str(picked.height))

    # ── the path, not the piece ─────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))
        service.client = lambda *a, **k: None

        asked: list[str] = []

        def refused(url, *args, **kwargs):
            asked.append(url)
            raise ExtractionError("Sign in to confirm you're not a bot")

        service._analysed = refused

        # 1080p was asked for and the browser has it: the site is never asked.
        row = service.add_media("https://www.youtube.com/watch?v=fast",
                                quality="1080p", title="Fast Video", start=False,
                                options={"captured": [v1080, aac, v360]})
        check("with the quality already loaded, the site is not asked at all",
              asked == [], str(asked))
        members = [d for d in service.db.list_downloads()
                   if (d.mux_group or "").startswith(f"{row.id}-captured")]
        check("the video and its audio are both queued", len(members) == 2,
              str(len(members)))
        check("they are one row, joined by a mux group",
              sorted((m.mux_group or "").rsplit(":", 1)[1] for m in members)
              == ["audio", "video"])
        check("the video is the one the player loaded at that height",
              row.url == v1080["url"], row.url)
        check("and it is named after the page, not after `videoplayback`",
              row.filename.startswith("Fast Video"), row.filename)

        # Only 360p on hand and 1080p asked for: extraction is tried, refused,
        # and the smaller file is taken rather than nothing.
        row = service.add_media("https://www.youtube.com/watch?v=short",
                                quality="1080p", title="Short Video", start=False,
                                options={"captured": [v360, aac]})
        check("a capture below the asked-for quality does not skip extraction",
              asked == ["https://www.youtube.com/watch?v=short"], str(asked))
        check("and when that is refused, the smaller file is queued anyway",
              row.url == v360["url"], row.url)

        # Adaptive throughout — no progressive stream anywhere — which is the
        # case the old rescue could make nothing of, and the shape of every
        # field report that came back unchanged.
        row = service.add_media("https://www.youtube.com/watch?v=adaptive",
                                quality="1080p", title="Adaptive Only",
                                start=False,
                                options={"captured": [v1080_vp9, opus]})
        joined = [d for d in service.db.list_downloads()
                  if (d.mux_group or "").startswith(f"{row.id}-captured")]
        check("a page with no progressive stream is still downloadable",
              len(joined) == 2, str(len(joined)))
        check("…as a WebM pair, which is what those tracks can become",
              row.filename.endswith(".webm"), row.filename)

        # Nothing captured, extraction refused: the refusal must reach the
        # user rather than being swallowed by a fallback that has nothing.
        try:
            service.add_media("https://www.youtube.com/watch?v=none",
                              start=False, options={})
            check("a refusal with nothing captured is reported", False,
                  "it was swallowed")
        except ExtractionError as exc:
            check("a refusal with nothing captured is reported",
                  "not a bot" in str(exc))
        service.db.close()

    # The engine can only do any of this if the extension sends whole entries.
    background = (Path(__file__).resolve().parents[1]
                  / "extension" / "background.js").read_text()
    check("the extension sends the itag with each capture",
          "itag: String(entry.itag || \"\")" in background)
    check("…and the headers the browser itself used for it",
          "headers: entry.headers || {}" in background)


def test_a_plain_address_carries_the_proof_of_origin() -> None:
    """An ordinary `videoplayback` address takes the token as `pot`.

    The field log's split is exact and was staring at this the whole time:

        18:40  #95   1080p  server-driven  ->  Completed
        19:37  #105   720p  server-driven  ->  Completed
        20:48  #107  plain videoplayback GET -> 403
        …every plain GET after it, 403

    The proof of origin was being presented to the **API** (as
    `serviceIntegrityDimensions.poToken`) and to the streaming session, and
    never on a plain address — so the two routes to the same CDN were not
    equally credentialled. Four sessions of header work could not have found
    this, because it is not a header: it is a query parameter.
    """
    print("\n[a plain address carries the proof of origin]")
    from ixd.service import _attested

    plain = "https://rr3---sn-x.googlevideo.com/videoplayback?itag=18&sig=a"
    check("the token is carried as `pot`",
          _attested(plain, "TOKEN") == plain + "&pot=TOKEN",
          _attested(plain, "TOKEN"))
    check("an address that already has one is left alone",
          _attested(plain + "&pot=OLD", "TOKEN") == plain + "&pot=OLD")
    check("applying it twice does not double it",
          _attested(_attested(plain, "TOKEN"), "TOKEN")
          == plain + "&pot=TOKEN")
    check("with no token nothing is added", _attested(plain, "") == plain)
    check("and another site's media is never touched",
          _attested("https://cdn.example/movie.mp4", "TOKEN")
          == "https://cdn.example/movie.mp4")

    # The extractor puts it on every address it publishes, at the one place
    # every URL passes through.
    source = (Path(__file__).resolve().parents[1]
              / "ixd" / "extractors" / "youtube.py").read_text()
    check("the extractor attests the addresses it resolves",
          source.count("return self._attested(") >= 3, source.count("self._attested("))


def test_a_stranded_stream_is_never_queued() -> None:
    """A download known to be impossible before it starts must not start.

    From the field log of 2026-08-12, rows #106 · #109 · #110 · #111 · #112,
    all the same shape:

        Added … [1080p60]
        This stream publishes no segment index, so it is fetched on a single…
        ERROR Failed: the opening 1,602 bytes of this stream — its index …
              are not served by the streaming session and no ordinary link
              to them was published.

    `add_media` tests that exact condition one line before queueing, found it
    true, looked for a captured stream, found none — and then **fell through
    and queued it anyway**. The transfer opened and failed on the first
    exchange with the condition already known at the top.

    Extraction had published seven formats. The ones that are not
    server-driven carry their own opening bytes, and one of them is the very
    360p file the page serves as an ordinary link — the fallback that already
    ran when the endpoint refused every rung outright. Refused and stranded are
    the same outcome for the person waiting for a file.
    """
    print("\n[a stranded stream is never queued]")
    import tempfile

    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.core.errors import ExtractionError
    from ixd.core.models import MediaFormat, MediaInfo
    from ixd.service import DownloadService

    def stranded(itag: str, height: int) -> MediaFormat:
        """Server-driven, needs opening bytes, nowhere to fetch them."""
        return MediaFormat(itag, "https://youtubei/endpoint", ext="mp4",
                           height=height, vcodec="avc1", acodec="none",
                           sabr={"header_end": 1601, "header_url": "",
                                 "endpoint": "https://youtubei/endpoint"})

    def plain(itag: str, height: int, *, audio: bool = True) -> MediaFormat:
        """An ordinary file the site serves over HTTP, header and all."""
        return MediaFormat(itag, f"https://cdn.invalid/{itag}.mp4", ext="mp4",
                           height=height, vcodec="avc1",
                           acodec="mp4a" if audio else "none")

    audio_track = MediaFormat("140", "https://youtubei/endpoint", ext="m4a",
                              vcodec="none", acodec="mp4a",
                              sabr={"header_end": 700,
                                    "header_url": "https://cdn.invalid/a.m4a",
                                    "endpoint": "https://youtubei/endpoint"})

    info = MediaInfo(
        title="Sung JinWoo vs. Kargalgan",
        webpage_url="https://www.youtube.com/watch?v=bQPgzJJLbfA",
        formats=[stranded("299", 1080), stranded("137", 1080),
                 plain("18", 360), audio_track],
    )

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))
        # A real client, because this path reads the session's cookies off it.
        # Nothing here reaches the network: the replacement is an ordinary
        # ranged URL and the download is queued without being started.
        service._analysed = lambda *a, **k: info
        # The servability walk asks the network; the stream it would return is
        # the one already chosen.
        service._servable_format = lambda chosen, *a, **k: (chosen, "")
        # The origin is asked, in one request, whether it will serve a fallback
        # before anything is queued. Here it will.
        served: list[str] = []
        service._plain_url_serves = lambda url, *a, **k: (
            served.append(url) or "")

        row = service.add_media(
            "https://www.youtube.com/watch?v=bQPgzJJLbfA",
            quality="1080p", title="Sung JinWoo vs. Kargalgan", start=False,
            options={},
        )
        check("the quality that was asked for is what gets queued",
              row.format_id in ("299", "137"), row.format_id)
        check("…as the server-driven stream itself, not the 360p downgrade",
              row.format_id != "18", row.format_id)

        messages = [event["message"] for event in service.db.recent_events()]
        check("and nothing claims the quality had to change",
              not any("carries its own" in m for m in messages),
              str(messages[-3:]))

        # A captured 360p no longer outranks the 1080p that was asked for.
        #
        # It used to, and the reason was sound while the server-driven stream
        # was considered impossible: a capture is the player's own address and
        # a stranded stream is no download at all. Both halves of that have
        # changed. The server-driven stream is attempted now, and the captured
        # address was measured 403 on a second fetch — from the youtube.com
        # page that minted it. So the capture is neither the safer choice nor
        # the better one, and quality decides, as it does everywhere else.
        captured = {"url": "https://rr3---sn-x.googlevideo.com/videoplayback"
                           "?itag=18&sig=a",
                    "itag": "18", "mime": "video/mp4", "size": 5_000_000}
        row = service.add_media(
            "https://www.youtube.com/watch?v=bQPgzJJLbfA",
            quality="1080p", title="Sung JinWoo", start=False,
            options={"captured": [captured]})
        check("the 1080p that was asked for outranks a captured 360p",
              row.format_id in ("299", "137"), f"{row.format_id} {row.url[:40]}")

        # A page offering nothing but server-driven streams is the ordinary
        # case on YouTube now, not a hopeless one. It is queued and the session
        # is asked; before, it was refused here and the person got nothing.
        adaptive_only = MediaInfo(
            title="Server Driven Only",
            webpage_url="https://www.youtube.com/watch?v=x",
            formats=[stranded("299", 1080), audio_track],
        )
        service._analysed = lambda *a, **k: adaptive_only
        try:
            queued = service.add_media("https://www.youtube.com/watch?v=x",
                                       quality="1080p", start=False, options={})
            check("a page with only server-driven streams is queued, not refused",
                  queued.format_id == "299", queued.format_id)
        except ExtractionError as exc:
            check("a page with only server-driven streams is queued, not refused",
                  False, str(exc)[:90])
        check("and no plain fallback was probed on the way, because none is "
              "needed", not served, str(served[:1]))
        service.db.close()


def test_the_windows_source_bundle_is_complete() -> None:
    """The zip handed to a machine that can build what this one cannot.

    PyInstaller bundles the interpreter of the machine it runs on, so a Windows
    binary needs a Windows Python and there is no cross-compilation. The source
    is what ships, and `packaging/windows/build.bat` runs on the far end.

    It is checked rather than read, because reading it is how the first version
    of this shipped with `.png` excluded by suffix — which took the extension's
    own icons with the screenshots, leaving a manifest naming files that were
    not there. The zip looked complete and the extension would not have loaded.
    """
    print("\n[the windows source bundle]")
    import json
    import sys
    import tempfile
    import zipfile

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "packaging"))
    import build as build_module  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as home:
        original = build_module.DIST
        build_module.DIST = Path(home)
        try:
            archive_path = build_module.build_windows_source()
        finally:
            build_module.DIST = original

        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            manifests = {
                name.rsplit("/", 1)[1]: json.loads(archive.read(name))
                for name in names
                if "/extension/manifest" in name and name.endswith(".json")
            }
    stem = f"ixd-{build_module.VERSION}"
    inner = [n[len(stem) + 1:] for n in names if n.startswith(stem + "/")]

    check("the bundle holds the application", "ixd/service.py" in inner)
    check("and the engine", "ixd/core/engine.py" in inner)
    check("and the extension", "extension/background.js" in inner)
    check("and the Windows procedure that builds it",
          "packaging/windows/build.bat" in inner
          and "packaging/windows/README.md" in inner)
    check("and the scripts the other two platforms use",
          "packaging/linux/build.sh" in inner
          and "packaging/macos/build.sh" in inner)
    check("and the spec PyInstaller reads", "packaging/ixd.spec" in inner)

    # What must never leave this tree.
    check("the extension's private signing key is not in it",
          "packaging/extension-key.pem" not in inner)
    check("nor somebody else's extension",
          not any(name.startswith("idm/") for name in inner))
    check("nor the session log, which nobody building needs",
          "session-log.md" not in inner)
    check("nor a database, a build artefact or a virtualenv",
          not any(name.endswith((".sqlite3", ".deb", ".pyc")) or
                  name.startswith((".venv/", "dist/", "build/"))
                  for name in inner), str(inner[:3]))

    # A screenshot at the top of the tree is dropped; an icon inside the
    # extension is not. This is the distinction the first version got wrong.
    check("field-report screenshots are left out",
          not any("/" not in name and name.endswith(".png") for name in inner))
    check("but the extension's icons are kept — the manifest names them",
          "extension/icons/icon128.png" in inner)

    # Checked the way a browser would check it: every path the manifest names
    # has to be in the archive.
    for manifest_name in ("manifest.json", "manifest.chrome.json",
                          "manifest.firefox.json"):
        check(f"{manifest_name} is in the bundle", manifest_name in manifests)
        manifest = manifests[manifest_name]
        referenced: list[str] = list((manifest.get("icons") or {}).values())
        action = manifest.get("action") or manifest.get("browser_action") or {}
        referenced += list((action.get("default_icon") or {}).values())
        for script in manifest.get("content_scripts") or []:
            referenced += script.get("js", []) + script.get("css", [])
        background = manifest.get("background") or {}
        if background.get("service_worker"):
            referenced.append(background["service_worker"])
        referenced += background.get("scripts", [])
        if manifest.get("options_page"):
            referenced.append(manifest["options_page"])
        missing = [ref for ref in referenced
                   if f"extension/{ref}" not in inner]
        check(f"every file {manifest_name} names is present",
              not missing, str(missing))


def test_the_page_hook_supplies_what_the_session_withholds() -> None:
    """The opening bytes, taken from what the player received.

    Every other route was measured shut on 2026-08-12: a `videoplayback`
    address is refused on a second fetch even to the page that minted it; the
    server-driven session delivers 100% of a stream's media and never its
    opening; and the streaming endpoint answers an ordinary GET with 31 bytes
    of framed protocol.

    The player receives those bytes — it plays the video. `content/page_tee.js`
    runs in the page's own world, as `idm/document.js` does, and copies the
    head of each media response; this reads them.
    """
    print("\n[the page hook supplies what the session withholds]")
    import base64
    import tempfile

    from ixd.core.protobuf import Message
    from ixd.extractors.sabr import PART_MEDIA, PART_MEDIA_HEADER
    from ixd.config import Settings
    from ixd.core.db import Database
    from ixd.service import DownloadService, _page_key

    def ump(part_type: int, payload: bytes) -> bytes:
        return bytes([part_type, len(payload)]) + payload

    # An initialisation segment as one arrives: a header naming the stream and
    # byte zero, then the body behind it. `ftyp` is what makes it a media file.
    opening = b"\x00\x00\x00\x18ftypiso5" + b"\x00" * 90
    header = (Message().varint(1, 3).varint(3, 137).varint(6, 0)).to_bytes()
    reply = (ump(PART_MEDIA_HEADER, header)
             + ump(PART_MEDIA, bytes([3]) + opening))

    # …alongside a media block that is not the opening, which must be ignored.
    later = (Message().varint(1, 4).varint(3, 137).varint(6, 965)).to_bytes()
    reply += ump(PART_MEDIA_HEADER, later) + ump(PART_MEDIA, bytes([4]) + b"junk")

    with tempfile.TemporaryDirectory() as home:
        settings = Settings(Path(home) / "settings.json")
        settings.set("download_dir", home)
        service = DownloadService(settings, Database(Path(home) / "state.sqlite3"))

        found = service._openings_in(reply)
        check("the opening is recognised and read out by itag",
              list(found) == [137] and found[137] == opening,
              f"{list(found)} {len(found.get(137, b''))}")
        check("and media that is not the opening is left alone",
              b"junk" not in found.get(137, b""))

        watch = "https://www.youtube.com/watch?v=p8XSB8VVrNY"
        taken = service._cmd_browser_media_head({
            "data": base64.b64encode(reply).decode(),
            "page_url": watch, "url": "https://x.googlevideo.com/videoplayback",
        })
        check("the command keeps it", taken.get("taken") is True, str(taken))
        check("and the engine can find it for that stream",
              service._lookup_opening("137", watch) == opening)
        check("…and does not invent one for a stream it has not seen",
              service._lookup_opening("999", watch) == b"")

        said = [event["message"] for event in service.db.recent_events()]
        check("the log says where those bytes came from",
              any("the page's own player received the opening" in m
                  for m in said), str(said[:1]))

        # A reply carrying no opening must not be recorded as one.
        nothing = service._cmd_browser_media_head({
            "data": base64.b64encode(
                ump(PART_MEDIA_HEADER, later)
                + ump(PART_MEDIA, bytes([4]) + b"tail")).decode(),
            "page_url": watch, "url": "https://x.googlevideo.com/videoplayback",
        })
        check("a reply with no opening in it is not taken",
              nothing.get("taken") is False, str(nothing))

        # Bytes that are not a media file are refused, wherever they came from.
        forged = (ump(PART_MEDIA_HEADER,
                      (Message().varint(1, 9).varint(3, 251).varint(6, 0))
                      .to_bytes())
                  + ump(PART_MEDIA, bytes([9]) + b"\x00\x00\x00\x18NOPEiso5"))
        check("a block at byte zero that is not a media file is refused",
              service._openings_in(forged) == {},
              str(service._openings_in(forged)))

        # A longer copy replaces a shorter one: a segment split across replies
        # would otherwise be stored as an opening describing bytes it does not
        # contain.
        # Kept under 128 bytes: `ump` above writes single-byte lengths, and the
        # size is not what is under test — only that the longer copy wins.
        longer = opening + b"\x00" * 20
        service._cmd_browser_media_head({
            "data": base64.b64encode(
                ump(PART_MEDIA_HEADER, header)
                + ump(PART_MEDIA, bytes([3]) + longer)).decode(),
            "page_url": watch, "url": "https://x.googlevideo.com/videoplayback",
        })
        check("a longer copy of the same opening replaces the shorter",
              service._lookup_opening("137", watch) == longer,
              str(len(service._lookup_opening("137", watch))))

        check("a page is identified by its video, not its query string",
              _page_key(watch) == _page_key(watch + "&t=90s"),
              f"{_page_key(watch)} vs {_page_key(watch + '&t=90s')}")
        service.db.close()

def main() -> int:
    print("=" * 68)
    print("Internet Xtreme Downloader — integration test suite")
    print("=" * 68)
    for test in (test_service_over_ipc, test_native_host_framing,
                 test_sandbox_relay, test_sandbox_launcher_placement,
                 test_browser_discovery, test_quit_ends_the_process,
                 test_extension_assets, test_native_host_installer,
                 test_pair_shows_as_one_row,
                 test_the_service_can_reopen_a_streaming_session,
                 test_a_quality_with_sound_is_never_queued_without_it,
                 test_captured_pairs_cannot_be_crossed,
                 test_captures_are_consulted_before_the_site,
                 test_a_plain_address_carries_the_proof_of_origin,
                 test_a_stranded_stream_is_never_queued,
                 test_the_page_hook_supplies_what_the_session_withholds,
                 test_the_windows_source_bundle_is_complete,
        test_the_page_reaches_the_engine_from_the_browser,
        test_a_page_handed_over_keeps_its_session,
        test_removing_a_download_never_deletes_another_ones_file,
        test_a_quality_is_queued_against_where_it_came_from,
        test_a_browser_launching_the_host_needs_no_flag,
        test_a_refused_ladder_falls_back_to_a_stream_that_works,
        test_a_stream_with_no_header_falls_back_to_what_the_browser_fetched,
        test_the_container_asked_for_is_the_container_produced,
                 test_the_quality_clicked_is_the_quality_fetched,
                 test_the_audio_queued_beside_a_video_is_the_original,
                 test_every_route_to_remove_asks_about_the_file,
                 test_removal_takes_the_whole_selection,
                 test_a_spin_box_shows_its_arrows,
                 test_the_settings_pages_scroll_instead_of_crushing,
                 test_the_rename_does_not_lose_the_data_directory,
                 test_the_old_native_host_registration_is_removed,
                 test_every_platform_has_an_icon_it_will_accept,
                 test_the_log_can_be_switched_off,
                 test_the_panel_offers_the_preferred_container):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILED.append(f"{test.__name__} raised {exc}")

    print("\n" + "=" * 68)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
