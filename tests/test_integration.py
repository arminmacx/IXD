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


def test_only_one_instance_owns_the_engine() -> None:
    """Binding the control socket is the lock. A second bind must fail.

    Windows field report, 2026-08-13: a download added from the browser did not
    open its window and showed no speed until it was paused and resumed; Pause
    said paused while the transfer continued; closing the window left the
    application running; ending the process brought it back.

    Every one of those follows from two engines on one database.
    `allow_reuse_address` was true, and on Windows that permits **two live
    listeners on the same address** — it is Unix's SO_REUSEPORT — so the second
    instance bound the same port, started its own engine and overwrote the
    endpoint file. The window belonged to one process and the transfer to the
    other.

    The flag is measured here rather than asserted, on whichever platform this
    runs: two servers, one port, and the second must be refused.
    """
    print("\n[only one instance owns the engine]")
    script = '''
import sys, tempfile, socket
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-one-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from ixd.config import Settings
from ixd.core.db import Database
from ixd.service import DownloadService
from ixd.ipc.server import IPCServer, is_running

settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
# A port nothing else on this machine is using.
probe = socket.socket(); probe.bind(("127.0.0.1", 0))
port = probe.getsockname()[1]; probe.close()
settings.set("ipc_port", port)
service = DownloadService(settings, Database(root / "state.sqlite3"))

first = IPCServer(service)
first.start()
print("FIRST_BOUND", first.port == port)
print("ANSWERS", is_running())

# The second instance. On Windows this used to succeed.
try:
    second = IPCServer(service)
    second.stop()
    print("SECOND_BOUND", True)
except OSError as exc:
    print("SECOND_BOUND", False)

print("REUSE_FLAG", IPCServer.allow_reuse_address)
print("IS_WINDOWS", sys.platform.startswith("win"))
first.stop()
service.shutdown()
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-one-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("only one instance owns the engine", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("the first instance takes the port", "FIRST_BOUND True" in output, detail)
    check("and answers a ping", "ANSWERS True" in output, detail)
    check("a second instance is refused the same port",
          "SECOND_BOUND False" in output, detail)
    check("and the address is not shared on Windows",
          ("IS_WINDOWS True" in output) <= ("REUSE_FLAG False" in output), detail)


def test_a_second_launch_stands_down() -> None:
    """Two real processes, one database: the second must not run an engine.

    The check at start-up is not enough on its own — two launches can pass it
    at the same moment, which is exactly what the browser's host does when it
    starts the application. The bind is the arbiter, and losing it has to end
    the process rather than leave it running windowless beside the winner.

    Also checked: the instance the *host* starts is a hidden window rather than
    a headless daemon. Once one instance properly owns the socket, a headless
    owner would mean a later launch is told "already running" with nowhere to
    show itself — the browser starting a download would lock the user out of
    their own interface.
    """
    print("\n[a second launch stands down]")
    from ixd.ipc import native_host

    command = native_host._application_command()
    check("the host starts a window, not a daemon",
          "--background" not in command, str(command))
    check("and starts it hidden", "--hidden" in command, str(command))

    script = '''
import subprocess, sys, tempfile, time, socket, json, os
from pathlib import Path
home = Path(tempfile.mkdtemp(prefix="ixd-race-"))
env = dict(os.environ)
env["IXD_HOME"] = str(home)
env["QT_QPA_PLATFORM"] = "offscreen"

probe = socket.socket(); probe.bind(("127.0.0.1", 0))
port = probe.getsockname()[1]; probe.close()
(home).mkdir(parents=True, exist_ok=True)
(home / "settings.json").write_text(json.dumps({
    "ipc_port": port, "ipc_token": "race-token",
    "download_dir": str(home / "out"), "browser_integration": False,
}))

first = subprocess.Popen([sys.executable, "-m", "ixd", "--background"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
# Wait for it to own the socket.
deadline = time.time() + 30
ready = False
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            ready = True
            break
    except OSError:
        time.sleep(0.3)
print("FIRST_SERVING", ready)

second = subprocess.run([sys.executable, "-m", "ixd", "--background"],
                        env=env, capture_output=True, text=True, timeout=60)
print("SECOND_EXITED", second.returncode == 0)
print("SECOND_SAID", "already running" in (second.stdout + second.stderr).lower())
print("FIRST_ALIVE", first.poll() is None)
first.terminate()
try:
    first.wait(timeout=20)
except subprocess.TimeoutExpired:
    first.kill()
'''
    root = Path(__file__).resolve().parents[1]
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("a second launch stands down", False, "timed out")
        return

    output = process.stdout
    detail = (output.strip()[-600:] or "") + (process.stderr[-600:] or "")
    check("the first instance serves", "FIRST_SERVING True" in output, detail)
    check("the second exits cleanly", "SECOND_EXITED True" in output, detail)
    check("saying why", "SECOND_SAID True" in output, detail)
    check("and the first is left running", "FIRST_ALIVE True" in output, detail)


def test_a_status_poll_never_starts_the_application() -> None:
    """A quit application must stay quit.

    The extension polled the application every 1.5 seconds for a number to put
    on its icon, and the messaging host started the application for *any*
    command — so ending the process brought it back within seconds. Reported
    from Windows as "even end task does not work, it runs again".

    Both halves are fixed and both are checked: the host starts the application
    only for commands the user has asked for, and the extension no longer polls
    at all.
    """
    print("\n[a status poll never starts the application]")
    from ixd.ipc import native_host

    for passive in ("ping", "stats", "list", "captured", "log"):
        check(f"“{passive}” does not start the application",
              passive not in native_host.STARTS_THE_APPLICATION)
    for wanted in ("add", "add_media", "add_pair", "present", "focus",
                   "pause", "browser_stream_begin"):
        check(f"“{wanted}” does",
              wanted in native_host.STARTS_THE_APPLICATION)

    # The panel looks a page's qualities up speculatively when it loads, so
    # `extract` starting the application would resurrect it on every video page
    # merely opened. A click says so, and only that starts it.
    check("a speculative extraction does not start it",
          "extract" not in native_host.STARTS_THE_APPLICATION)
    panel = (Path(__file__).resolve().parents[1]
             / "extension" / "content" / "video_inject.js").read_text(encoding="utf-8")
    check("and the panel marks the click as the user's",
          "userInitiated: true" in panel, "")
    background = (Path(__file__).resolve().parents[1]
                  / "extension" / "background.js").read_text(encoding="utf-8")
    check("which reaches the host as user_initiated",
          "user_initiated: Boolean(options.userInitiated)" in background, "")

    source = (Path(__file__).resolve().parents[1]
              / "extension" / "background.js").read_text(encoding="utf-8")
    check("the extension sets no badge text at all",
          "setBadgeText" not in source,
          source[source.find("setBadgeText") - 200:][:300]
          if "setBadgeText" in source else "")
    check("and polls nothing on a timer",
          "BADGE_POLL_MS" not in source and "refreshDownloadBadge" not in source)


def test_the_icon_carries_the_progress() -> None:
    """Progress on the application's icon, on whatever this desktop has.

    Qt exposes none of these — `QWinTaskbarProgress` was Qt 5 and did not
    survive into Qt 6 — so each platform is spoken to directly: ITaskbarList3
    on Windows, the launcher-entry signal on Linux, the dock badge on macOS.

    The Linux one is checked properly, because it can be: the signal is caught
    off the real session bus by a listener that did not send it. The other two
    are checked only for the shape of the decision, and are honestly unverified
    on hardware nobody here has.
    """
    print("\n[the icon carries the progress]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-bar-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6 import QtCore
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, Slot
from PySide6.QtDBus import QDBusConnection
from ixd.ui.taskbar import TaskbarProgress

app = QApplication(sys.argv[:1])
seen = []

class Listener(QObject):
    @Slot(str, "QVariantMap")
    def Update(self, app_uri, properties):
        seen.append((app_uri, dict(properties)))

listener = Listener()
bus = QDBusConnection.sessionBus()
print("BUS", bus.isConnected())
bus.connect("", "/com/canonical/Unity/LauncherEntry",
            "com.canonical.Unity.LauncherEntry", "Update",
            listener, QtCore.SLOT("Update(QString,QVariantMap)"))

# D-Bus delivery is asynchronous: a fixed number of processEvents() is a race,
# and under the load of a whole suite it loses. Wait for the signal instead.
import time
def wait_for(count, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline and len(seen) < count:
        app.processEvents()
        time.sleep(0.01)
    # Anything further in flight arrives while we are still spinning.
    settle = time.time() + 0.4
    while time.time() < settle:
        app.processEvents()
        time.sleep(0.01)

bar = TaskbarProgress()
bar.set_progress(0.42)
wait_for(1)
print("SENT", len(seen))
if seen:
    uri, props = seen[-1]
    print("URI_OK", uri == "application://ixd.desktop")
    print("PROGRESS", round(float(props.get("progress", -1)), 3))
    print("VISIBLE", bool(props.get("progress-visible")))

seen.clear()
bar.clear()
wait_for(1)
print("CLEAR_SENT", len(seen))
print("CLEAR_HIDES", bool(seen) and not seen[-1][1].get("progress-visible"))

seen.clear()
bar.set_progress(0.42); bar.set_progress(0.42); bar.set_progress(0.42)
wait_for(1)
print("REPEATS_SUPPRESSED", len(seen) == 1, len(seen))

# Which windows Windows would draw the bar on.
#
# It draws on a taskbar *button*, and a window that is not on the taskbar has
# none — so setting progress on the main window's handle drew nothing whenever
# that window was hidden, which is precisely when the feature is wanted: the
# browser starts the application hidden and the download window stands alone.
from PySide6.QtWidgets import QWidget

class FakeBackend:
    def __init__(self):
        self.calls = []
    def set_progress(self, percent, visible, handles=()):
        self.calls.append((percent, visible, handles))
    def set_indeterminate(self, handles=()):
        self.calls.append(("indeterminate", True, handles))
    def diagnostic(self):
        return ""

bar2 = TaskbarProgress()
bar2._windows = FakeBackend()
main = QWidget(); main.setWindowTitle("main"); main.show()
bar2.attach(main)
app.processEvents()
bar2.set_progress(0.5)
extra = QWidget(); extra.setWindowTitle("download"); extra.show()
app.processEvents()
bar2.set_progress(0.5)
calls = bar2._windows.calls
print("MAIN_INCLUDED", bool(calls) and int(main.winId()) in calls[0][2])
print("NEW_WINDOW_REDRAWN", len(calls) == 2, len(calls))
print("NEW_WINDOW_INCLUDED",
      len(calls) == 2 and int(extra.winId()) in calls[1][2]
      and int(extra.winId()) not in calls[0][2])

# A transfer whose length nobody published used to clear the bar, which is
# what "nothing is running" looks like. Windows has a state for it.
bar2.set_indeterminate()
print("INDETERMINATE_ROUTED",
      calls[-1][0] == "indeterminate" and len(calls[-1][2]) >= 1)
bar2.set_indeterminate()
print("INDETERMINATE_NOT_REPEATED", len(calls) == 3, len(calls))
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-bar-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the icon carries the progress", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    if "BUS True" not in output:
        # A build machine with no session bus cannot be asked this. Say so
        # rather than passing a check that never ran.
        check("a session bus is available to test against", False,
              "no session bus; the launcher signal could not be verified here")
        return
    check("the launcher signal reaches the bus", "SENT 1" in output, detail)
    check("addressed to this application", "URI_OK True" in output, detail)
    check("carrying the fraction", "PROGRESS 0.42" in output, detail)
    check("and marked visible", "VISIBLE True" in output, detail)
    check("clearing sends one more", "CLEAR_SENT 1" in output, detail)
    check("that hides it", "CLEAR_HIDES True" in output, detail)
    check("and the same value is not sent twice",
          "REPEATS_SUPPRESSED True" in output, detail)
    check("Windows draws on the main window when it is up",
          "MAIN_INCLUDED True" in output, detail)
    check("a window opening mid-transfer is drawn on, not skipped as a repeat",
          "NEW_WINDOW_REDRAWN True" in output, detail)
    check("and it is the new window's own handle",
          "NEW_WINDOW_INCLUDED True" in output, detail)
    check("a transfer with no published length is drawn indeterminate",
          "INDETERMINATE_ROUTED True" in output, detail)
    check("and that state is not re-sent while it holds",
          "INDETERMINATE_NOT_REPEATED True" in output, detail)


def test_the_icons_are_registered_as_a_theme() -> None:
    """PNGs in a directory are not an icon theme until it says it is.

    Under Wayland the compositor draws the icon named by the entry, and that
    name is resolved through the XDG icon theme. A per-user base directory
    (`~/.local/share/icons/hicolor`) is its own theme root and needs its own
    `index.theme`; the system copy under `/usr/share` does not cover it.

    Reported on Linux: the splash showed the icon and the window did not. GTK's
    own lookup answered `has_icon("ixd") -> False` with all five sizes sitting
    in place.

    Also checked: a stale `icon-theme.cache` is dealt with. It is authoritative
    when present, so an icon written after it was built stays invisible.
    """
    print("\n[the icons are registered as a theme]")
    root = Path(tempfile.mkdtemp(prefix="ixd-icons-"))
    previous = os.environ.get("XDG_DATA_HOME")
    os.environ["XDG_DATA_HOME"] = str(root)
    try:
        from importlib import reload
        from ixd import desktop
        reload(desktop)

        if desktop._installed_system_wide():
            print("  SKIP  a system-wide entry is installed on this machine")
            return

        hicolor = root / "icons" / "hicolor"
        (hicolor / "64x64" / "apps").mkdir(parents=True, exist_ok=True)
        stale = hicolor / "icon-theme.cache"
        stale.write_bytes(b"stale")

        check("it installs an entry", desktop.ensure_desktop_entry())
        check("the desktop entry is written",
              (root / "applications" / "ixd.desktop").exists())

        index = hicolor / "index.theme"
        check("and an index.theme, so the tree is a theme at all", index.exists())
        body = index.read_text(encoding="utf-8") if index.exists() else ""
        check("naming every size it installed",
              all(f"{size}x{size}/apps" in body for size in desktop.ICON_SIZES), body[:200])
        check("declared as a theme", body.startswith("[Icon Theme]"), body[:40])

        check("every icon size is in place",
              all((hicolor / f"{s}x{s}" / "apps" / "ixd.png").exists()
                  for s in desktop.ICON_SIZES))

        check("and the stale cache is not left to hide them",
              (not stale.exists()) or stale.read_bytes() != b"stale",
              "cache still says 'stale'")

        # Written once, not churned: some desktops watch these paths.
        before = index.read_bytes()
        desktop.ensure_desktop_entry()
        check("a second launch does not rewrite it", index.read_bytes() == before)
    finally:
        if previous is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = previous
        shutil.rmtree(root, ignore_errors=True)
        from importlib import reload as _reload
        from ixd import desktop as _desktop
        _reload(_desktop)


def test_the_splash_says_what_is_happening() -> None:
    """Something on screen while start-up takes its second or two.

    The animation cannot be left to a timer: start-up blocks this same thread,
    so a timer would fire only once the loop is idle — which is exactly when
    the splash is no longer needed. The angle comes from elapsed time and
    `step()` drives the repaint, so it is correct whenever it is drawn.

    What is checked here is that it draws, that the message it shows is the one
    it was given, and that `finish()` returns and closes rather than spinning
    for ever — a hang there would be a launch that never completes.
    """
    print("\n[the splash says what is happening]")
    script = '''
import sys, tempfile, time
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-splash-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd.ui.theme import DARK
from ixd.ui.widgets import splash as splash_module

app = QApplication(sys.argv[:1])
s = splash_module.SplashScreen(DARK)
s.show()
s.step("Starting the transfer engine\u2026")
first = s.grab()
print("DREW", first.width() == splash_module.WIDTH and first.height() == splash_module.HEIGHT)
print("MESSAGE_KEPT", s._message == "Starting the transfer engine\u2026")

# The arc has to be somewhere different a moment later, or it is a picture.
import hashlib
def digest(pixmap):
    image = pixmap.toImage()
    return hashlib.sha1(image.bits().tobytes()).hexdigest()
before = digest(first)
deadline = time.time() + 3.0
moved = False
while time.time() < deadline and not moved:
    time.sleep(0.12)
    s.step("Starting the transfer engine\u2026")
    moved = digest(s.grab()) != before
print("ANIMATES", moved)

# It must not flash, and it must not hang.
began = time.time()
s.finish(None)
took = time.time() - began
print("HELD_LONG_ENOUGH", took >= splash_module.MINIMUM_MS / 1000 * 0.5, round(took, 2))
print("FINISHED_AND_CLOSED", not s.isVisible())
print("RETURNED", took < 8)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-splash-home-")
    try:
        done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=180, env=environment, cwd=str(root))
    except subprocess.TimeoutExpired:
        check("the splash finishes rather than hanging", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = done.stdout
    detail = (output.strip()[-400:]) + (done.stderr[-400:])
    check("it draws at its own size", "DREW True" in output, detail)
    check("it shows the step it was given", "MESSAGE_KEPT True" in output, detail)
    check("the arc actually turns", "ANIMATES True" in output, detail)
    check("it stays up long enough to be seen", "HELD_LONG_ENOUGH True" in output, detail)
    check("finish() closes it", "FINISHED_AND_CLOSED True" in output, detail)
    check("and returns rather than spinning for ever",
          "RETURNED True" in output, detail)

    # The stages are reported, not guessed at.
    from ixd.__main__ import _build_service
    import inspect
    check("start-up reports its stages",
          "stage" in inspect.signature(_build_service).parameters)


def test_windows_only_imports_exist_on_windows() -> None:
    """Every name taken from `ctypes.wintypes` is actually defined there.

    From a user's Log:

        Taskbar progress: taskbar progress unavailable: cannot import name
        'ULONGLONG' from 'ctypes.wintypes'

    `wintypes` has ULARGE_INTEGER and ULONG and nothing called ULONGLONG. The
    import raised, the blanket `except` around the COM setup swallowed it, and
    the whole feature was dead on Windows while Linux and macOS worked — for
    two releases, because the code only ran on the platform nobody here has.

    `ctypes.wintypes` cannot be imported off Windows, but it ships as ordinary
    Python in the standard library and can be read. So the names this tree asks
    for are checked against the names that module defines, on any platform.
    """
    print("\n[windows-only imports are names that exist]")
    import ast as _ast
    import re as _re
    import sysconfig as _sysconfig

    wintypes = (Path(_sysconfig.get_paths()["stdlib"]) / "ctypes" / "wintypes.py")
    if not wintypes.exists():
        check("the standard library's wintypes source is readable", False,
              str(wintypes))
        return

    body = wintypes.read_text(encoding="utf-8")
    defined = set(_re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", body, _re.M))
    defined |= set(_re.findall(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)", body, _re.M))
    check("wintypes defines names to check against", len(defined) > 20, len(defined))

    root = Path(__file__).resolve().parents[1] / "ixd"
    asked: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = _ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom) and node.module == "ctypes.wintypes":
                for alias in node.names:
                    asked.append((str(path.relative_to(root.parent)), alias.name))

    check("something imports from ctypes.wintypes at all", bool(asked),
          "nothing does; this check would pass vacuously")
    missing = [f"{where}: {name}" for where, name in asked if name not in defined]
    check("every name asked of ctypes.wintypes exists there",
          not missing, "; ".join(missing))

    # The specific one that was wrong, so a rename cannot quietly bring it back.
    check("ULONGLONG in particular is not asked for",
          all(name != "ULONGLONG" for _, name in asked))


def test_no_credential_shaped_literal_ships() -> None:
    """Nothing in the tree looks like a leaked key.

    GitHub's secret scanning flagged `AIzaSy…` in the YouTube extractor. That
    particular value was not a credential — youtube.com publishes it in every
    watch page, the same for every visitor, and `_live_config` already read it
    from there — so there was nothing to rotate and the copy in the source was
    a stale duplicate of page data. It is gone, and the page is the only
    source.

    The point of this check is that it cannot come back quietly, and that the
    next one is caught here rather than by an email. The patterns are the
    shapes the scanners look for, not a judgement about what is secret.
    """
    print("\n[nothing in the tree is shaped like a credential]")
    import re as _re

    root = Path(__file__).resolve().parents[1]
    patterns = {
        "Google API key": _re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
        "AWS access key": _re.compile(r"AKIA[0-9A-Z]{16}"),
        "Slack token": _re.compile(r"xox[abprs]-[0-9A-Za-z-]{10,}"),
        "GitHub token": _re.compile(r"gh[pousr]_[0-9A-Za-z]{36}"),
        # The header plus actual material after it. A test asserting that a
        # *generated* key is PEM-shaped names the header and holds no key, and
        # flagging that is how a scanner trains people to ignore it.
        "private key block": _re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*\n[A-Za-z0-9+/=\s]{200,}"),
    }
    #: The extension is signed with a key pair whose *private* half is a build
    #: input and deliberately present; it is not a credential to any service.
    allowed = {root / "packaging" / "extension-key.pem"}
    skip_dirs = {".git", ".venv", "dist", "build", "node_modules", "idm",
                 "__pycache__", "backups"}

    hits = []
    for path in root.rglob("*"):
        if not path.is_file() or path in allowed:
            continue
        if set(path.relative_to(root).parts) & skip_dirs:
            continue
        if path.suffix.lower() not in {".py", ".js", ".json", ".md", ".yml",
                                       ".yaml", ".sh", ".bat", ".html", ".txt"}:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, pattern in patterns.items():
            found = pattern.search(body)
            if found:
                hits.append(f"{path.relative_to(root)}: {label} "
                            f"{found.group(0)[:12]}…")

    check("no credential-shaped literal is in the tree",
          not hits, "; ".join(hits[:4]))

    from ixd.extractors import youtube
    check("the extractor holds no key of its own",
          not hasattr(youtube, "DEFAULT_API_KEY"))
    check("and its client identities start without one",
          all(not c.api_key for c in youtube.CLIENTS)
          if hasattr(youtube, "CLIENTS") else True)


def test_the_queue_finishes_with_a_choice() -> None:
    """"Shut down when it is done" — armed, countable, and stoppable.

    The point of leaving a queue running overnight is not having to come back
    to it, so the scheduler has to end somewhere. Nothing here powers a machine
    off: what is tested is the decision — that it waits for the work to be
    finished, that it can be called off, that it fires exactly once, and that
    the commands it would run are the ones the platform actually publishes.
    """
    print("\n[the queue finishes with a choice]")
    from ixd import power
    from ixd.core.events import EventType
    from ixd.core.models import Download, DownloadStatus
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-completion-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()

    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))

    # -- what each platform is asked ------------------------------------
    check("an unknown action reads as doing nothing",
          power.parse("explode") is power.CompletionAction.NOTHING)
    check("and a stored one survives the round trip",
          power.parse("shutdown") is power.CompletionAction.SHUTDOWN)
    check("quitting is the application's own job, not a command",
          power.candidates(power.CompletionAction.EXIT) == [])
    for platform, expected in (("linux", "systemctl"), ("win32", "shutdown"),
                               ("darwin", "osascript")):
        first = power.candidates(power.CompletionAction.SHUTDOWN, platform)
        check(f"{platform} is asked through {expected}",
              bool(first) and first[0][0] == expected, str(first))
    check("every machine action has somewhere to go on every platform",
          all(power.candidates(action, platform)
              for platform in ("linux", "win32", "darwin")
              for action in (power.CompletionAction.SHUTDOWN,
                             power.CompletionAction.SLEEP,
                             power.CompletionAction.HIBERNATE)))

    # -- and when it fires ----------------------------------------------
    fired: list[dict] = []
    service.events.subscribe(lambda _e, p: fired.append(p),
                             EventType.COMPLETION_ARMED)

    parked = Download(url="https://example.invalid/one", filename="one.bin",
                      status=DownloadStatus.PAUSED)
    parked.id = service.db.insert_download(parked)
    done = Download(url="https://example.invalid/two", filename="two.bin",
                    status=DownloadStatus.COMPLETED)
    done.id = service.db.insert_download(done)

    settings.set("completion_action", "shutdown")
    settings.set("completion_grace_seconds", 600)

    # A paused download is work parked, not work over.
    service._consider_completion_action()
    check("a paused download holds the machine open",
          not fired and service._completion_timer is None,
          str([d.filename for d in service.unfinished_work()]))

    service.db.update_download_fields(parked.id, status=DownloadStatus.COMPLETED)
    service._consider_completion_action()
    check("with nothing left, the countdown starts", len(fired) == 1, str(fired))
    check("and it says what it is about to do and when",
          bool(fired) and fired[0].get("action") == "shutdown"
          and fired[0].get("seconds") == 600, str(fired))

    # Two downloads finishing at once must not start two timers.
    before = service._completion_timer
    service._consider_completion_action()
    check("a second finish does not start a second countdown",
          len(fired) == 1 and service._completion_timer is before, str(fired))

    check("it can be called off", service.cancel_completion_action("test"))
    check("and calling it off clears the setting, so it stays off",
          settings.get("completion_action") == "nothing"
          and service._completion_timer is None,
          str(settings.get("completion_action")))
    check("cancelling twice is not an error",
          service.cancel_completion_action("test") is False)

    # Armed again, it must not re-arm itself after firing.
    settings.set("completion_action", "exit")
    settings.set("completion_grace_seconds", 0)
    service.arm_completion_action()
    deadline = time.time() + 5
    while time.time() < deadline and service._completion_timer is not None:
        time.sleep(0.05)
    check("firing resets the setting, so it happens once and not nightly",
          settings.get("completion_action") == "nothing",
          str(settings.get("completion_action")))

    service.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def test_the_scheduler_is_reachable_and_stoppable() -> None:
    """The scheduler has a button, and its ending has a way out.

    Everything the scheduler does — which downloads a queue runs, the order
    they run in, when it starts and when it stops — was reachable only by
    opening Settings and finding the sixth tab. "Start this at 2am" is not
    something anybody looks for under Settings, so it has a button of its own
    now, and this asserts the button exists and lands on the right page.

    The other half is the countdown. A completion action that cannot be called
    off is not one to give a machine, so the dialog is opened and its way out
    is clicked, in a real Qt application.
    """
    print("\n[the scheduler is reachable, and its ending is stoppable]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-sched-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.service import DownloadService
from ixd.ui.main_window import MainWindow
from ixd.ui.widgets.settings_dialog import SettingsDialog
from PySide6.QtGui import QAction

app = QApplication([])
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = MainWindow(service)

labels = [a.text() for a in window.findChildren(QAction)]
print("TOOLBAR_SCHEDULER", any("Scheduler" in text for text in labels))

dialog = SettingsDialog(service, window)
print("LANDS_ON", dialog.show_tab("Scheduler"),
      dialog._tabs.tabText(dialog._tabs.currentIndex()))
print("CHOICES", "|".join(dialog.completion_combo.itemText(i)
                          for i in range(dialog.completion_combo.count())))
print("GRACE", dialog.completion_grace.value())

# Armed through the service, exactly as a finished queue arms it, so the
# event wiring between the two is what is being tested and not a method call.
import time
settings.set("completion_action", "shutdown")
settings.set("completion_grace_seconds", 42)
service.arm_completion_action()
for _ in range(40):
    app.processEvents()
    if window._completion_box is not None:
        break
    time.sleep(0.05)
box = window._completion_box
print("ARRIVED", box is not None)
print("SAYS", box.text().replace("<b>", "").replace("</b>", ""))
print("WARNS_ABOUT_THE_MACHINE", "whole machine" in box.informativeText())
print("WAY_OUT", box.buttons()[0].text())
box.buttons()[0].click()
app.processEvents()
print("STOPPED", window._completion_box is None)
print("SETTING_AFTER", settings.get("completion_action"))
service.shutdown()
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-sched-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the scheduler is reachable", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-600:] or "") + (process.stderr[-400:] or "")
    check("the scheduler has a button of its own",
          "TOOLBAR_SCHEDULER True" in output, detail)
    check("and it lands on the scheduler page",
          "LANDS_ON True Scheduler" in output, detail)
    check("the ending offers every choice, including leaving the machine alone",
          "CHOICES Do nothing|Quit IXD|Sleep|Hibernate|Shut down" in output, detail)
    check("with a countdown before it happens", "GRACE 60" in output, detail)
    check("finishing raises the countdown in the window itself",
          "ARRIVED True" in output, detail)
    check("the countdown says what and when",
          "SAYS Shut down in 42s" in output, detail)
    check("and that it is the machine, not just the application",
          "WARNS_ABOUT_THE_MACHINE True" in output, detail)
    check("there is a way out", "WAY_OUT Don" in output, detail)
    check("taking it stops the countdown", "STOPPED True" in output, detail)
    check("and switches the action off, so it does not happen next time either",
          "SETTING_AFTER nothing" in output, detail)


def test_cancelling_closes_the_download_window() -> None:
    """Cancel ends the download, and the window is the download's.

    Reported: pressing Cancel in a download's own window cancelled the
    transfer and left the window sitting there reading "Cancelled", so the
    same thing had to be dismissed twice. The row belongs in the main list;
    the window does not.

    Also here: the taskbar's own log line. It is confirmed working on both
    platforms, and a line every time the state changes fills the log with the
    one thing that is not going wrong — so the first success is written and
    the rest are not, while every refusal still is.
    """
    print("\n[cancelling closes the window, and the taskbar says its piece once]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-cancel-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.models import Download, DownloadStatus
from ixd.service import DownloadService
from ixd.ui.main_window import MainWindow
from ixd.ui.widgets.download_window import DownloadWindow

app = QApplication([])
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = MainWindow(service)

download = Download(url="https://example.invalid/big.bin", filename="big.bin",
                    total_size=1000, downloaded=100,
                    status=DownloadStatus.PAUSED)
download.id = service.db.insert_download(download)

DownloadWindow.show_for(service, download.id, window._palette, window)
app.processEvents()
opened = DownloadWindow._open.get(download.id)
print("OPENED", opened is not None and opened.isVisible())
opened._cancel()
app.processEvents()
print("STILL_OPEN", download.id in DownloadWindow._open)
print("STATUS", service.get_download(download.id).status.value)

# The taskbar line, written once however often the state changes.
window.taskbar.diagnostic = lambda: "ITaskbarList3 normal on 1 window(s): 0x1=ok"
window._log_taskbar_state()
window.taskbar.diagnostic = lambda: "ITaskbarList3 clear on 2 window(s): 0x1=ok, 0x2=ok"
window._log_taskbar_state()
window.taskbar.diagnostic = lambda: "ITaskbarList3 normal on 2 window(s): 0x1=ok, 0x2=ok"
window._log_taskbar_state()
lines = [e["message"] for e in service.db.recent_events(200)
         if "Taskbar progress" in e["message"]]
print("TASKBAR_LINES", len(lines))
print("TASKBAR_SAYS_WORKING", any("working" in line for line in lines))

# A refusal is always written, and so is the recovery after it.
window.taskbar.diagnostic = lambda: "no backend: cannot import name 'ULONGLONG'"
window._log_taskbar_state()
window.taskbar.diagnostic = lambda: "ITaskbarList3 normal on 1 window(s): 0x9=ok"
window._log_taskbar_state()
lines = [e["message"] for e in service.db.recent_events(200)
         if "Taskbar progress" in e["message"]]
print("AFTER_REFUSAL", len(lines))
service.shutdown()
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-cancel-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("cancelling closes the window", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-600:] or "") + (process.stderr[-400:] or "")
    check("the download's window opens", "OPENED True" in output, detail)
    check("cancelling closes it", "STILL_OPEN False" in output, detail)
    check("and the download really is cancelled",
          "STATUS cancelled" in output, detail)
    check("the taskbar writes one line, not one per state change",
          "TASKBAR_LINES 1" in output, detail)
    check("and that line says the backend is working",
          "TASKBAR_SAYS_WORKING True" in output, detail)
    check("a refusal is still written, and so is the recovery after it",
          "AFTER_REFUSAL 3" in output, detail)


def test_a_schedule_can_actually_be_added() -> None:
    """Reported: "I add a schedule but it is not added to the list".

    It never could be. `ScheduleAction` subclasses `str`, so the enum stored as
    Qt item data comes back as a plain string — which quacks the same until the
    insert asks for `.value`, raises `AttributeError` inside a Qt slot, and
    leaves the dialog closing with nothing saved and nothing said. The queue
    dialog had the identical bug, and with no schedule in the list "Downloads
    and order…" could only answer "Select a schedule first".
    """
    print("\n[a schedule can actually be added]")
    from ixd.core.models import DownloadQueue, QueueMode, Schedule, ScheduleAction
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-schedule-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()
    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))

    # Exactly what Qt hands back, and what JSON over the control socket does.
    saved = service.save_schedule(Schedule(
        name="Overnight", action_start="start", action_end="pause"))
    check("a schedule whose actions arrive as strings still saves",
          isinstance(saved, int) and saved > 0, str(saved))
    stored = [s for s in service.list_schedules() if s.id == saved]
    check("and it appears in the list", len(stored) == 1,
          str([s.name for s in service.list_schedules()]))
    if stored:
        check("with its actions as actions, not as text",
              isinstance(stored[0].action_start, ScheduleAction)
              and stored[0].action_start is ScheduleAction.START
              and stored[0].action_end is ScheduleAction.PAUSE,
              f"{stored[0].action_start!r}/{stored[0].action_end!r}")
    check("the scheduler can describe it without raising",
          any(row["name"] == "Overnight" for row in service.scheduler.status()),
          str(service.scheduler.status()))

    queue_id = service.save_queue(DownloadQueue(
        name="Evening", mode="concurrent", max_concurrent=3))
    queue = service.db.get_queue(queue_id)
    check("a queue whose mode arrives as a string saves too",
          queue is not None and queue.mode is QueueMode.CONCURRENT,
          repr(queue.mode) if queue else "missing")

    service.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def test_it_can_tell_you_there_is_a_newer_version() -> None:
    """Checking, refusing to install what it cannot, and installing what it can.

    Nothing here talks to GitHub: a local origin answers like the release feed
    does, and the "new version" is a folder this test builds. What is being
    tested is the decision — that a check is one read and never an install,
    that a build installed from a package refuses to replace itself, that a
    download whose size does not match is thrown away, that an archive without
    the launcher in it is refused *before* anything is moved, and that a swap
    that does happen leaves a working folder behind.
    """
    print("\n[it can tell you there is a newer version]")
    import re
    import tarfile as _tarfile
    import threading as _threading
    from ixd.core.models import Download, DownloadStatus
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from ixd import updates
    from ixd.core.events import EventType
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-updates-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()

    # -- the arithmetic that decides everything ------------------------
    check("1.0.10 is newer than 1.0.9, which string order gets wrong",
          updates.is_newer("1.0.10", "1.0.9"))
    check("a tag with a v is the same version", not updates.is_newer("v1.0.6", "1.0.6"))
    check("and an older one is not newer", not updates.is_newer("1.0.5", "1.0.6"))
    check("running from source is never self-updating",
          updates.self_update_kind() == "")

    # -- a feed that answers like the real one -------------------------
    published = {
        "tag_name": "v9.9.9", "name": "IXD 9.9.9",
        "body": "## Fixed\n\n- Everything.",
        "html_url": "https://example.invalid/releases/v9.9.9",
        "assets": [{"name": "ixd-linux-x86_64-selfupdate.tar.gz", "size": 0,
                    "browser_download_url": ""}],
    }
    feed_body = json.dumps(published).encode()

    class Feed(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            body = feed_body if self.path.startswith("/feed") else archive_bytes
            kind = ("application/json" if self.path.startswith("/feed")
                    else "application/gzip")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    # The "new version": a folder with a launcher in it, packed as the real
    # portable archive is.
    newer = root / "newer" / "ixd"
    newer.mkdir(parents=True)
    (newer / "ixd").write_text("#!/bin/sh\necho new\n", encoding="utf-8")
    (newer / "keep.txt").write_text("data", encoding="utf-8")
    archive_path = root / "ixd-linux-x86_64-selfupdate.tar.gz"
    with _tarfile.open(archive_path, "w:gz") as bundle:
        bundle.add(newer, arcname="ixd")
    archive_bytes = archive_path.read_bytes()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Feed)
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    published["assets"][0]["size"] = len(archive_bytes)
    published["assets"][0]["browser_download_url"] = f"{origin}/asset.tar.gz"
    feed_body = json.dumps(published).encode()

    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))

    seen: list[dict] = []
    service.events.subscribe(lambda _e, p: seen.append(p),
                             EventType.UPDATE_AVAILABLE)
    try:
        # Plain http to somewhere else is refused outright: an update is the
        # last thing to take from a connection anybody can rewrite. Loopback
        # is allowed, which is the only reason the rest of this can be tested.
        settings.set("updates_feed", "http://updates.example.invalid/feed")
        raised = ""
        try:
            service.check_for_updates(force=True)
        except Exception as error:  # noqa: BLE001
            raised = str(error)
        check("a plain-http update feed from the network is refused",
              "https" in raised.lower(), raised or "it was accepted")
        settings.set("updates_feed", f"{origin}/feed")
        live = service.check_for_updates(force=True)
        check("a loopback feed is read, and reports the newer version",
              live is not None and live.version == "9.9.9",
              str(live.version if live else None))
        check("and the window is told exactly once",
              len(seen) == 1 and seen[0].get("version") == "9.9.9", str(seen))

        release = live or updates.Release()
        check("the feed's tag becomes a version", release.version == "9.9.9",
              release.version)
        check("and it is newer than what is running", release.newer)
        check("the asset for this platform is found",
              (release.asset("linux", ".tar.gz") or {}).get("name")
              == "ixd-linux-x86_64-selfupdate.tar.gz",
              str(release.asset("linux", ".tar.gz")))

        # -- a marker may not name one release's file ------------------
        #
        # Reported from Windows on 1.0.8: "the release publishes nothing this
        # build can use", followed by a list containing the file it wanted.
        # The build had recorded `ixd-1.0.8-windows-x64-selfupdate.zip` and
        # searched a 1.0.9 release for that exact string.
        published_names = [
            "Internet-Xtreme-Downloader-1.0.9.dmg", "ixd-1.0.9-windows-source.zip",
            "ixd-1.0.9-windows-x64-selfupdate.zip", "ixd-1.0.9-windows-x64.zip",
            "ixd-extension-chrome-1.0.9.zip", "ixd-linux-x86_64-selfupdate.tar.gz",
            "ixd-linux-x86_64.tar.gz", "ixd-macos-arm64-selfupdate.zip",
            "ixd-macos-arm64.zip", "ixd_1.0.9_amd64.deb",
        ]
        next_release = updates.Release(
            version="1.0.9", assets=[{"name": n} for n in published_names])

        def chosen_with(marker_asset: str, platform_shape: list) -> str:
            candidates = ([(marker_asset,)] if marker_asset else []) + platform_shape
            for pattern in candidates:
                found = next_release.asset(*pattern)
                if found:
                    return str(found["name"])
            return ""

        windows_shape = [("windows", "selfupdate", ".zip"), ("windows", ".zip")]
        check("a marker naming last release's file still finds this one",
              chosen_with("ixd-1.0.8-windows-x64-selfupdate.zip", windows_shape)
              == "ixd-1.0.9-windows-x64-selfupdate.zip",
              chosen_with("ixd-1.0.8-windows-x64-selfupdate.zip", windows_shape))
        check("a build with no marker at all finds it too",
              chosen_with("", windows_shape)
              == "ixd-1.0.9-windows-x64-selfupdate.zip",
              chosen_with("", windows_shape))
        check("and the self-updating archive is preferred over the plain one",
              chosen_with("windows-x64-selfupdate.zip", windows_shape)
              == "ixd-1.0.9-windows-x64-selfupdate.zip",
              chosen_with("windows-x64-selfupdate.zip", windows_shape))

        # The rule itself, on every platform, without building them: nothing a
        # build writes into its own marker may carry a version number.
        build_source = (Path(__file__).resolve().parents[1]
                        / "packaging" / "build.py").read_text(encoding="utf-8")
        table = re.search(r"SELF_UPDATE_PATTERNS = \{(.*?)\}", build_source, re.S)
        patterns = re.findall(r'"([^"]*selfupdate[^"]*)"', table.group(1) if table else "")
        check("every platform records a version-free pattern",
              bool(patterns) and not any(re.search(r"\d+\.\d+", p) for p in patterns),
              str(patterns))

        # A build that was not marked self-updating refuses to install.
        started, detail = service.install_update(release)
        check("a build that is not self-updating refuses to replace itself",
              started is False and "package" in detail, detail)

        # The download, and what happens when the size does not match.
        staging = root / "staging"
        lying = dict(release.assets[0])
        lying["size"] = len(archive_bytes) + 1
        refused = ""
        try:
            updates.download(service.client(), lying, staging)
        except Exception as error:  # noqa: BLE001
            refused = str(error)
        check("a download that is not the size the feed promised is thrown away",
              "refusing" in refused, refused or "it was kept")

        fetched = updates.download(service.client(), release.assets[0], staging)
        check("and the right one is kept", fetched.exists()
              and fetched.stat().st_size == len(archive_bytes), str(fetched))

        # Unpacking, and the check that happens before anything is moved.
        unpacked = updates.stage(fetched, staging / "unpacked", launcher="ixd")
        check("the archive unpacks to a folder holding the launcher",
              (unpacked / "ixd").exists(), str(unpacked))
        wrong = staging / "wrong.tar.gz"
        with _tarfile.open(wrong, "w:gz") as bundle:
            bundle.add(newer / "keep.txt", arcname="something/else.txt")
        rejected = ""
        try:
            updates.stage(wrong, staging / "unpacked2", launcher="ixd")
        except Exception as error:  # noqa: BLE001
            rejected = str(error)
        check("an archive that is not this application is refused",
              "does not contain" in rejected, rejected or "it was accepted")

        # The flag that does the swap is not a copy-this-anywhere command:
        # only a build published as self-updating may use it, whatever folder
        # it is pointed at.
        original_root = updates.install_root
        original_kind = updates.self_update_kind
        try:
            updates.install_root = lambda: unpacked          # pretend frozen
            updates.self_update_kind = lambda: ""            # but unmarked
            ok, why = updates.apply(root / "installed-nowhere", relaunch=False)
            check("an unmarked build refuses to apply an update",
                  ok is False and "self-updating" in why, why)
            check("and it left nothing behind",
                  not (root / "installed-nowhere").exists())
        finally:
            updates.install_root = original_root
            updates.self_update_kind = original_kind

        # Installing by itself waits for the transfers to finish. An update
        # that replaces the application mid-download is an update that lost
        # somebody a file.
        settings.set("updates_install_automatically", True)
        busy = Download(url="https://example.invalid/big.bin",
                        filename="big.bin", status=DownloadStatus.DOWNLOADING)
        busy.id = service.db.insert_download(busy)
        service.IDLE_WAIT_SECONDS = 1        # do not sit here for half an hour
        tried: list[str] = []
        original_install = service.install_update
        service.install_update = lambda release, progress=None: (
            tried.append(release.version) or (False, "not in this test"))
        service._install_when_idle(release)
        time.sleep(2.5)
        check("an automatic install waits while a download is running",
              not tried, str(tried))
        service.db.update_download_fields(busy.id, status=DownloadStatus.COMPLETED)
        service._install_when_idle(release)
        deadline = time.time() + 5
        while time.time() < deadline and not tried:
            time.sleep(0.1)
        check("and goes ahead once nothing is downloading",
              tried == ["9.9.9"], str(tried))
        service.install_update = original_install
        settings.set("updates_install_automatically", False)

        # And the swap itself, done directly rather than through a relaunch.
        target = root / "installed"
        target.mkdir()
        (target / "ixd").write_text("old", encoding="utf-8")
        (target / "stale.txt").write_text("gone", encoding="utf-8")
        moved = _swap_for_test(unpacked, target)
        check("the swap puts the new version in place",
              (target / "ixd").read_text().startswith("#!"), moved)
        check("and leaves nothing of the old one behind",
              not (target / "stale.txt").exists())
        check("with no leftover .previous folder",
              not target.with_name(target.name + ".previous").exists())
    finally:
        server.shutdown()
        service.shutdown()
        shutil.rmtree(root, ignore_errors=True)


def _swap_for_test(staged: Path, target: Path) -> str:
    """`updates.apply` without the frozen-build check, which a test cannot be."""
    aside = target.with_name(target.name + ".previous")
    shutil.rmtree(aside, ignore_errors=True)
    os.replace(target, aside)
    shutil.copytree(staged, target, symlinks=True, dirs_exist_ok=True)
    shutil.rmtree(aside, ignore_errors=True)
    return str(target)


def test_the_updater_says_what_it_is_doing() -> None:
    """The window the *staged* build shows while it swaps the folders.

    Asked for: the application should close, be replaced and start again
    without the user doing anything. That works headless, but an application
    that vanishes for a few seconds and comes back looks like a crash — and on
    Windows, where the build has no console, there is nowhere for a message to
    go at all. So the updater has a window, and it carries its own colours:
    the folder holding the application's theme is the folder being replaced.
    """
    print("\n[the updater says what it is doing]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-updater-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd import updater_ui

app = QApplication([])
window, parts = updater_ui.build_window()
window.show()
app.processEvents()
print("TITLE", window.windowTitle())
print("HEADING", parts["heading"].text())
print("DETAIL", parts["detail"].text())
print("FOOTER", parts["footer"].text())
print("INDETERMINATE", parts["bar"].minimum() == 0 and parts["bar"].maximum() == 0)
print("OWN_COLOURS", "#0d0f16" in window.styleSheet())
print("NO_CLOSE_BUTTON", not bool(window.windowFlags() & 0x08000000))
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-updater-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the updater window builds", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-400:] or "") + (process.stderr[-300:] or "")
    check("the updater's window is the application's own name",
          "TITLE Internet Xtreme Downloader" in output, detail)
    check("it says which version it is installing",
          "HEADING Updating to version" in output, detail)
    check("and what it is waiting for",
          "DETAIL Waiting for the application to close" in output, detail)
    check("it promises to start it again",
          "start again by itself" in output, detail)
    check("the bar does not pretend to know how long this takes",
          "INDETERMINATE True" in output, detail)
    check("it carries its own colours, not the ones being replaced",
          "OWN_COLOURS True" in output, detail)


def test_what_the_browser_announces_is_the_real_name() -> None:
    """The notification that fires once, immediately, with whatever was known.

    Reported after the naming fix landed: the window shows the right filename
    now, and Windows' own notification still says
    `74709710-bf21-4cd4-926a-526ff561a1bb`. Both were true. The engine renames
    a guess as soon as it has fetched the first response, but the *reply to
    the add call* is what the browser announces, and that had already gone out
    with the guess in it.

    So an address that carries nothing filename-shaped is asked before the
    reply is written. An address that already names its file pays nothing —
    which is every ordinary download.

    Also here: the "still running" notice belongs to the first close, not to
    every close.
    """
    print("\n[what the browser announces is the real name]")
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-announce-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()
    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))

    payload = b"\x00" * 4096
    with TestOrigin(payload) as origin:
        origin.state.disposition_name = "ixd_1.0.8_amd64.deb"

        # What a release asset's address looks like once the browser has
        # followed the redirect.
        opaque = origin.url("/asset/74709710-bf21-4cd4-926a-526ff561a1bb")
        row = service.add_from_browser({"url": opaque, "start": False})
        check("the reply the browser announces carries the origin's name",
              row.filename == "ixd_1.0.8_amd64.deb", row.filename)
        check("and it is not marked as a guess, because it is not one",
              not row.auto_named)

        # An ordinary address is not probed at all: the name is already there.
        before = origin.state.request_count
        plain = service.add_from_browser(
            {"url": origin.url("/holiday-video.mp4"), "start": False})
        check("an address that names its file costs no extra request",
              plain.filename == "holiday-video.mp4"
              and origin.state.request_count == before,
              f"{plain.filename}, {origin.state.request_count - before} requests")

        # A name the caller chose still wins over everything.
        chosen = service.add_from_browser(
            {"url": opaque, "filename": "mine.deb", "start": False})
        check("and a name that was asked for is never replaced",
              chosen.filename == "mine.deb", chosen.filename)

        # But a name the *browser* supplies is only a name when it looks like
        # one. On Windows the download item already carries the identifier
        # from the address, which is how it reached a notification.
        browser_guess = service.add_from_browser(
            {"url": opaque, "filename": "8b192290-d315-431a-8ff6-b03be0d2c027",
             "start": False})
        check("a supplied name that is not filename-shaped is a guess too",
              browser_guess.filename == "ixd_1.0.8_amd64.deb",
              browser_guess.filename)

        # An origin that will not answer leaves the old behaviour in place
        # rather than failing the add.
        unreachable = service.add_from_browser(
            {"url": "https://127.0.0.1:9/asset/opaque-name", "start": False})
        check("an origin that cannot be reached still yields a download",
              unreachable.filename == "opaque-name" and unreachable.auto_named,
              unreachable.filename)

    service.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def test_the_still_running_notice_is_said_once() -> None:
    """Closing the window to the tray explains itself the first time only."""
    print("\n[the still-running notice is said once]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-notice-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.service import DownloadService
from ixd.ui.main_window import MainWindow

app = QApplication([])
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
settings.set("close_to_tray", True)
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = MainWindow(service)

said = []
window.tray.notify = lambda title, body: said.append(title)
# The tray has to look available for the close-to-tray path to be taken.
window.tray.isVisible = lambda: True
import ixd.ui.main_window as mw
from PySide6.QtWidgets import QSystemTrayIcon
QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: True)

for _ in range(4):
    window.show()
    window.closeEvent(QCloseEvent())
print("SAID", len(said))
print("REMEMBERED", settings.get_bool("close_to_tray_notice_shown", False))
service.shutdown()
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-notice-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the notice is said once", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-400:] or "") + (process.stderr[-300:] or "")
    check("four closes produce one notice, not four",
          "SAID 1" in output, detail)
    check("and it is remembered, so the next launch does not say it either",
          "REMEMBERED True" in output, detail)


def test_the_extension_folder_follows_the_application() -> None:
    """Reported: the extension stayed on the old version through many launches.

    The folder the browser loads is a *copy*, in the data directory. It was
    refreshed only inside `ensure_registered`, which is a no-op once the
    messaging manifests already point at the current launcher — the normal
    case — so a new version of the application shipped a new extension that
    the copy never received. And the copy is written after the files it comes
    from, so the "destination is not older" test that guarded each file was
    permanently true: a changed file of unchanged length was skipped for ever.

    Content decides now, and it runs on every start.
    """
    print("\n[the extension folder follows the application]")
    from ixd import integration

    root = Path(tempfile.mkdtemp(prefix="ixd-mirror-"))
    source = root / "shipped"
    target = root / "materialised"
    (source / "content").mkdir(parents=True)
    (source / "manifest.json").write_text('{"version": "1.0.8"}', encoding="utf-8")
    (source / "content" / "panel.js").write_text("// one\n", encoding="utf-8")
    (source / "gone.js").write_text("// removed later\n", encoding="utf-8")

    integration._mirror_tree(source, target)
    check("the copy is made", (target / "manifest.json").exists())

    # A new version, written *earlier* than the copy that already exists —
    # which is what an unpacked build looks like — and one file that changes
    # without changing length.
    (source / "manifest.json").write_text('{"version": "1.0.9"}', encoding="utf-8")
    (source / "content" / "panel.js").write_text("// two\n", encoding="utf-8")
    (source / "gone.js").unlink()
    old = time.time() - 86400
    for path in source.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))

    integration._mirror_tree(source, target)
    check("a file that changed without changing length is copied",
          (target / "content" / "panel.js").read_text() == "// two\n",
          (target / "content" / "panel.js").read_text())
    check("even though the copy is newer than what it came from",
          (target / "manifest.json").read_text() == '{"version": "1.0.9"}',
          (target / "manifest.json").read_text())
    check("and a file the new version dropped is removed from the copy",
          not (target / "gone.js").exists())

    # Nothing is rewritten when nothing changed: this runs at every launch.
    stamps = {p: p.stat().st_mtime_ns for p in target.rglob("*") if p.is_file()}
    time.sleep(0.01)
    integration._mirror_tree(source, target)
    unchanged = all(p.stat().st_mtime_ns == stamp for p, stamp in stamps.items())
    check("an unchanged extension is not rewritten on every start", unchanged)

    shutil.rmtree(root, ignore_errors=True)


def test_the_folder_the_browser_loads_always_has_a_manifest() -> None:
    """Reported from Windows: "it still says its corrupted", and the folder was
    empty of any manifest.

    The extension ships two manifests under their own names — Chrome and
    Firefox will each read only a file literally called `manifest.json`, and the
    two are not interchangeable. The loadable one was written by
    `sync_extension_manifest`, *after* the mirror had run.

    But the mirror's prune pass removes anything in the copy that is not in the
    source, and `manifest.json` is in neither — it is written, not copied. So
    every subsequent call to `extension_dir()` deleted it again, and there are
    three per launch: start-up writes the Chrome folder, then the Firefox one
    (which asked for the Chrome path to find its neighbour), and
    `_entry_is_ours` called it once per installed extension in every browser
    profile it read. On a normal start `ensure_registered` is a no-op and never
    re-runs the write, so the folder the browser had been loading from was left
    with no manifest at all — which is what a browser reports as corrupted.

    Materialising the folder is what writes the manifest now, so there is no
    window in which the two disagree.
    """
    print("\n[the folder the browser loads always has a manifest]")
    from ixd import integration

    root = Path(tempfile.mkdtemp(prefix="ixd-manifest-"))
    try:
        source = root / "shipped"
        (source / "content").mkdir(parents=True)
        (source / integration.CHROME_MANIFEST).write_text(
            '{"name": "IXD", "background": {"service_worker": "background.js"}}',
            encoding="utf-8")
        (source / integration.FIREFOX_MANIFEST).write_text(
            '{"name": "IXD", "background": {"scripts": ["background.js"]}}',
            encoding="utf-8")
        (source / "background.js").write_text("// worker\n", encoding="utf-8")
        (source / "content" / "panel.js").write_text("// panel\n", encoding="utf-8")

        chrome = root / "chrome"
        integration._mirror_tree(source, chrome, integration.CHROME_MANIFEST)
        manifest = chrome / "manifest.json"
        check("the mirrored folder has a manifest.json at all", manifest.is_file(),
              str(sorted(p.name for p in chrome.iterdir())))
        check("and it is the browser's own flavour",
              "service_worker" in manifest.read_text(encoding="utf-8"),
              manifest.read_text(encoding="utf-8")[:80])

        # The defect: mirroring again pruned what the previous pass had written.
        integration._mirror_tree(source, chrome, integration.CHROME_MANIFEST)
        integration._mirror_tree(source, chrome, integration.CHROME_MANIFEST)
        check("and a second and third pass do not take it away again",
              manifest.is_file(),
              str(sorted(p.name for p in chrome.iterdir())))

        check("the other browser's manifest is not left sitting beside it",
              not (chrome / integration.FIREFOX_MANIFEST).exists()
              and not (chrome / integration.CHROME_MANIFEST).exists(),
              str(sorted(p.name for p in chrome.iterdir())))
        check("the rest of the extension is still copied",
              (chrome / "background.js").is_file()
              and (chrome / "content" / "panel.js").is_file())

        firefox = root / "firefox"
        integration._mirror_tree(source, firefox, integration.FIREFOX_MANIFEST)
        body = (firefox / "manifest.json").read_text(encoding="utf-8")
        check("the Firefox folder gets Firefox's manifest, not Chrome's",
              "scripts" in body and "service_worker" not in body, body[:80])

        # A folder written by the old code still holds the flavoured copies;
        # they go, rather than confusing whoever opens it looking for one.
        (chrome / integration.CHROME_MANIFEST).write_text("{}", encoding="utf-8")
        integration._mirror_tree(source, chrome, integration.CHROME_MANIFEST)
        check("a leftover flavoured manifest from an older version is removed",
              not (chrome / integration.CHROME_MANIFEST).exists())

        # And the predicate that was driving one of the three calls per launch
        # must not touch the disk at all. Read as code, not as text: the comment
        # explaining the defect names the very call it is asserting is gone.
        import ast as _ast
        import inspect
        tree = _ast.parse(inspect.getsource(integration._entry_is_ours))
        called = {node.func.id for node in _ast.walk(tree)
                  if isinstance(node, _ast.Call)
                  and isinstance(node.func, _ast.Name)}
        check("deciding whether an entry is ours no longer re-writes the folder",
              "extension_dir" not in called, str(sorted(called)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_extension_sits_where_an_update_leaves_it() -> None:
    """The browser is pointed at that folder once and never again.

    The user, on finding it under `%APPDATA%`: *"on selfupdate zip file you
    should still holding the extension in same place as the main folder because
    when upgrade to new version done its done in same place and user need to
    point their browser to reload the extension from that folder … but now i
    see you push the extension to AppData\\Roaming\\IXD which gonna be in
    conflict with the installer you made."*

    Right on both counts. A portable build replaces its own folder in place, so
    an extension folder inside it keeps the path the browser holds; one in the
    data directory belongs to no install in particular, and once there is also
    an installed copy the two share it and the last one launched wins.

    So it goes beside the application **when that is writable** — portable, and
    a per-user install under `%APPDATA%\\IXD` — and falls back to the data
    directory when it is not, which is the all-users install under Program
    Files and the .deb under /opt.

    And the swap must not take it: it is generated, so it is in no archive, and
    the pass that removes what the new version no longer ships would delete the
    whole folder. The application rewrites it on the next launch, but the
    browser does not wait — a folder that vanishes is one it marks corrupted.
    """
    print("\n[the extension sits where an update leaves it]")
    from ixd import config, integration, updates

    root = Path(tempfile.mkdtemp(prefix="ixd-where-"))
    previous_frozen = getattr(sys, "frozen", None)
    previous_exe = sys.executable
    previous_data = config.DATA_DIR
    try:
        installed = root / "installed"
        installed.mkdir(parents=True)
        (installed / "ixd").write_text("#!/bin/sh\n", encoding="utf-8")
        config.DATA_DIR = root / "data"
        config.DATA_DIR.mkdir(parents=True)

        sys.frozen = True
        sys.executable = str(installed / "ixd")
        check("a writable installation keeps the extension beside it",
              integration.extension_root() == installed,
              str(integration.extension_root()))

        # Program Files, from the point of view of the person running it.
        readonly = root / "readonly"
        (readonly).mkdir()
        (readonly / "ixd").write_text("#!/bin/sh\n", encoding="utf-8")
        readonly.chmod(0o500)
        sys.executable = str(readonly / "ixd")
        try:
            check("an installation it cannot write to falls back to the data dir",
                  integration.extension_root() == config.DATA_DIR,
                  str(integration.extension_root()))
        finally:
            readonly.chmod(0o700)

        check("and writability is asked, not read off permission bits",
              "_is_writable" in inspect_source(integration.extension_root))

        # macOS: the launcher is three levels inside the bundle, and neither
        # "inside Contents/MacOS" nor "the bundle itself" is the folder someone
        # extracted the archive into. It was excluded from this rule entirely
        # until the user said the rule covers every platform.
        bundle = root / "Applications"
        macos = bundle / "Internet Xtreme Downloader.app" / "Contents" / "MacOS"
        macos.mkdir(parents=True)
        (macos / "ixd").write_text("#!/bin/sh\n", encoding="utf-8")
        sys.executable = str(macos / "ixd")
        was_macos = config.IS_MACOS
        config.IS_MACOS = True
        try:
            check("on macOS it is the folder holding the .app, not inside it",
                  integration.installation_dir() == bundle,
                  str(integration.installation_dir()))
        finally:
            config.IS_MACOS = was_macos
        sys.executable = str(installed / "ixd")

        # A copy from 1.0.14 or earlier is never refreshed again, so a browser
        # pointed at it is frozen on that version for ever.
        legacy = config.DATA_DIR / "extension"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "manifest.json").write_text("{}", encoding="utf-8")
        (config.DATA_DIR / "extension-firefox").mkdir(parents=True, exist_ok=True)
        gone = integration.retire_legacy_extension_copies(installed)
        check("a copy left by an older version is retired", len(gone) == 2, str(gone))
        check("and it is really gone", not legacy.exists())

        # …but never when that *is* where the live copy is.
        keep = config.DATA_DIR / "extension"
        keep.mkdir(parents=True, exist_ok=True)
        (keep / "manifest.json").write_text("{}", encoding="utf-8")
        check("the live copy is never retired as though it were stale",
              integration.retire_legacy_extension_copies(config.DATA_DIR) == []
              and keep.exists())

        # The swap, with an extension folder the archive knows nothing about.
        sys.executable = str(installed / "ixd")
        target = root / "target"
        (target / "extension" / "content").mkdir(parents=True)
        (target / "extension" / "manifest.json").write_text("{}", encoding="utf-8")
        (target / "extension-firefox").mkdir(parents=True)
        (target / "extension-firefox" / "manifest.json").write_text("{}", encoding="utf-8")
        (target / "old-thing.txt").write_text("dropped", encoding="utf-8")
        (target / "ixd").write_text("old", encoding="utf-8")

        source = root / "newversion"
        (source / "_internal").mkdir(parents=True)
        (source / "ixd").write_text("new", encoding="utf-8")
        (source / "_internal" / "base.zip").write_text("x", encoding="utf-8")

        ok, detail = updates._replace_contents(source, target)
        check("the swap succeeds", ok, detail)
        check("the new version is in place",
              (target / "ixd").read_text() == "new")
        check("what the new version dropped is still removed",
              not (target / "old-thing.txt").exists())
        check("but the folder the browser loads survives the update",
              (target / "extension" / "manifest.json").is_file(),
              str(sorted(p.name for p in target.iterdir())))
        check("and so does Firefox's",
              (target / "extension-firefox" / "manifest.json").is_file())

        check("the updater and the integration agree on which folders those are",
              updates.PRESERVED_ON_UPDATE == {integration.EXTENSION_DIR_NAME,
                                              integration.FIREFOX_EXTENSION_DIR_NAME},
              f"{updates.PRESERVED_ON_UPDATE} vs "
              f"{integration.EXTENSION_DIR_NAME}/{integration.FIREFOX_EXTENSION_DIR_NAME}")
    finally:
        if previous_frozen is None:
            del sys.frozen
        else:
            sys.frozen = previous_frozen
        sys.executable = previous_exe
        config.DATA_DIR = previous_data
        shutil.rmtree(root, ignore_errors=True)


def test_an_update_survives_a_folder_windows_will_not_rename() -> None:
    """Two Windows failures, both reported with a screenshot.

    `[WinError 32] … being used by another process: 'C:\\Users\\…\\ixd' ->
    '…\\ixd.previous'` — the swap renamed the whole folder, and Windows will
    not rename a folder while anything holds a file in it. Something always
    does: the browser keeps a native-messaging host alive out of the installed
    folder, and a scanner may be reading what was just written.

    Then, on retrying: `Failed to execute script 'entry' … No such file or
    directory: …\\update\\unpacked\\ixd\\_internal\\base_library.zip` — the
    second attempt emptied the staging folder that the first attempt's updater
    was still *running from*.

    Neither can be reproduced on Linux, where a rename over a busy directory
    is allowed. What is tested here is the code that replaced them: files are
    replaced one at a time, a file that refuses is moved aside first, staging
    folders are never reused, and the Windows liveness check does not use the
    call that kills a process instead of looking at it.
    """
    print("\n[an update survives a folder Windows will not rename]")
    import re
    from ixd import updates

    root = Path(tempfile.mkdtemp(prefix="ixd-swap-"))
    source = root / "new"
    target = root / "installed"
    (source / "_internal").mkdir(parents=True)
    (source / "ixd").write_text("#!/bin/sh\necho new\n", encoding="utf-8")
    (source / "_internal" / "base_library.zip").write_text("new", encoding="utf-8")
    target.mkdir()
    (target / "ixd").write_text("old", encoding="utf-8")
    (target / "stale.dat").write_text("gone in the new version", encoding="utf-8")

    ok, detail = updates._replace_contents(source, target)
    check("the new version is put in place file by file", ok, detail)
    check("the launcher is the new one",
          (target / "ixd").read_text().startswith("#!"), (target / "ixd").read_text())
    check("nested files arrive too",
          (target / "_internal" / "base_library.zip").read_text() == "new")
    check("and what the new version dropped is gone",
          not (target / "stale.dat").exists())

    # A file the system will not let us write over — which on Windows is every
    # running executable. The way through is to rename it aside first.
    real_copy = shutil.copy2
    refused: dict[str, int] = {"ixd": 0}

    def refuses_the_launcher(src, dst, *args, **kwargs):
        if Path(dst).name == "ixd" and refused["ixd"] < 1:
            refused["ixd"] += 1
            raise PermissionError(13, "being used by another process")
        return real_copy(src, dst, *args, **kwargs)

    (source / "ixd").write_text("#!/bin/sh\necho newer\n", encoding="utf-8")
    shutil.copy2 = refuses_the_launcher
    try:
        ok, detail = updates._replace_contents(source, target)
    finally:
        shutil.copy2 = real_copy
    check("a file that cannot be written over is moved aside and replaced",
          ok and (target / "ixd").read_text().endswith("newer\n"), detail)
    check("and nothing of the moved-aside copy is left lying about",
          not any(".old-" in p.name for p in target.iterdir()),
          str([p.name for p in target.iterdir()]))

    # Staging is never reused, so a retry cannot empty the folder the first
    # attempt is running from.
    archive = root / "ixd-linux-x86_64-selfupdate.tar.gz"
    import tarfile as _tarfile
    with _tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source, arcname="ixd")
    first = updates.stage(archive, root / "staging" / "unpacked", launcher="ixd")
    (first.parent / "running.lock").write_text("the first updater", encoding="utf-8")
    second = updates.stage(archive, root / "staging" / "unpacked", launcher="ixd")
    check("a second attempt unpacks somewhere else entirely",
          first.parent != second.parent, f"{first.parent} == {second.parent}")
    check("and the first attempt's folder is still whole",
          (first / "ixd").exists() and (first / "_internal" / "base_library.zip").exists(),
          str(sorted(p.name for p in first.iterdir())))

    # Old staging folders are swept, but only once they are old enough that
    # nothing can still be running from them.
    ancient = first.parent.with_name("unpacked-000000-1")
    ancient.mkdir(parents=True, exist_ok=True)
    (ancient / "leftover").write_text("from last month", encoding="utf-8")
    os.utime(ancient, (time.time() - 7 * 86400, time.time() - 7 * 86400))
    updates.stage(archive, root / "staging" / "unpacked", launcher="ixd")
    check("a week-old staging folder is cleaned up", not ancient.exists())
    check("and a recent one is not", first.parent.exists(), str(first.parent))

    # A process that will not go is closed rather than reported. Reported as
    # "the update did not finish — process 23688 is still running", with the
    # new version downloaded, unpacked, checked, and nothing to show for it.
    stubborn = subprocess.Popen([sys.executable, "-c",
                                 "import signal, time\n"
                                 "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                 "time.sleep(300)"])
    time.sleep(0.6)
    check("a process ignoring a polite request is seen as alive",
          updates._alive(stubborn.pid))
    note = updates._end_process(stubborn.pid)
    stubborn.wait(timeout=5)
    check("and it is closed anyway", not updates._alive(stubborn.pid), note)
    check("with a note saying so, for the Log", "closed" in note, note)

    # Staging goes beside a portable application, not into a data directory
    # the user never chose: reported as updates appearing under %APPDATA%
    # for a build unpacked into a folder of their own.
    original_root = updates.install_root
    try:
        updates.install_root = lambda: root / "portable" / "ixd"
        (root / "portable" / "ixd").mkdir(parents=True, exist_ok=True)
        where = updates.staging_root(root / "data" / "update")
        check("an update is unpacked beside the application it replaces",
              where == root / "portable" / "ixd-update", str(where))
        check("and not inside it, which the swap itself would delete",
              (root / "portable" / "ixd") not in where.parents
              and where != root / "portable" / "ixd", str(where))
        updates.install_root = lambda: None
        check("a source run still uses the data directory",
              updates.staging_root(root / "data" / "update")
              == root / "data" / "update")
    finally:
        updates.install_root = original_root

    # The Windows liveness check, verified the way §3.22 taught: read the
    # source, and read the stdlib's own wintypes for the names it uses.
    source_text = (Path(updates.__file__)).read_text(encoding="utf-8")
    windows_branch = source_text[source_text.index("if sys.platform.startswith(\"win\"):",
                                                   source_text.index("def _alive")):]
    check("the Windows branch of the liveness check reads the exit code",
          "GetExitCodeProcess" in source_text, "it does not")
    check("and the Windows branch of ending a process asks for that plainly",
          "TerminateProcess" in source_text, "it does not")
    check("the Windows branch does not use the call that kills a process",
          "os.kill" not in windows_branch.split("try:\n        os.kill")[0],
          "os.kill appears in the Windows path")
    import ctypes.wintypes as _wintypes
    check("every wintypes name it uses exists in this Python",
          all(hasattr(_wintypes, name) for name in ("DWORD", "BOOL", "HANDLE")),
          "a name is missing from ctypes.wintypes")

    shutil.rmtree(root, ignore_errors=True)


def test_it_can_start_with_the_session() -> None:
    """Launch at startup, minimised — registered where the session looks.

    A checkbox that stores a preference and never reaches the registry, the
    autostart directory or LaunchAgents is a setting that appears to work and
    does nothing. So the entry is written, read back, validated by
    `desktop-file-validate` — which did not write it — and removed again.

    The whole thing runs against a temporary `XDG_CONFIG_HOME`, so the tester's
    own session is never registered by a test run.
    """
    print("\n[it can start with the session]")
    import shutil as _shutil
    import subprocess as _subprocess

    root = tempfile.mkdtemp(prefix="ixd-autostart-")
    previous = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = root
    try:
        from ixd import autostart
        from importlib import reload
        reload(autostart)

        check("nothing is registered to begin with", not autostart.is_enabled())

        autostart.apply(True)
        entry = autostart.linux_entry_path()
        check("enabling writes the entry", entry.exists(), str(entry))
        check("and it reads back as enabled", autostart.is_enabled())

        body = entry.read_text(encoding="utf-8") if entry.exists() else ""
        check("it starts the application with the window down",
              "--hidden" in body, body)
        check("it names the application, not the binary",
              "Name=Internet Xtreme Downloader" in body, body)
        check("and it is not disabled by the desktop's own flag",
              "X-GNOME-Autostart-enabled=true" in body
              and "Hidden=false" in body, body)

        # Checked by something that did not write it.
        validator = _shutil.which("desktop-file-validate")
        if validator:
            done = _subprocess.run([validator, str(entry)],
                                   capture_output=True, text=True, timeout=30)
            check("desktop-file-validate accepts the entry",
                  done.returncode == 0, (done.stdout + done.stderr).strip())
        else:
            print("  SKIP  desktop-file-validate is not installed")

        autostart.apply(True)
        check("applying it twice changes nothing", autostart.is_enabled())

        autostart.apply(False)
        check("disabling removes the entry",
              not entry.exists() and not autostart.is_enabled())
        autostart.apply(False)
        check("and disabling what is already gone is not an error", True)

        # The setting exists and is off until asked for.
        from ixd.config import DEFAULT_SETTINGS
        check("the setting ships off by default",
              DEFAULT_SETTINGS.get("launch_at_startup") is False,
              repr(DEFAULT_SETTINGS.get("launch_at_startup")))
    finally:
        if previous is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = previous
        shutil.rmtree(root, ignore_errors=True)
        from ixd import autostart as _restore
        from importlib import reload as _reload
        _reload(_restore)


def test_a_downloads_window_stands_on_its_own() -> None:
    """It must appear when the main window is not up.

    Reported from Windows: adding a download showed the application's icon on
    the taskbar and no window at all, and clicking that icon to raise the main
    window finally brought the download window with it.

    That is what a *parented* window does. Windows gives a child no taskbar
    button of its own, and a child of a hidden parent does not appear — and the
    parent is hidden whenever the browser started the application or it has
    been closed to the tray.
    """
    print("\n[a download's window stands on its own]")
    script = '''
import sys, tempfile
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-win-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.models import Download, DownloadStatus
from ixd.service import DownloadService
from ixd.ui.theme import DARK, apply_theme
from ixd.ui.main_window import MainWindow
from ixd.ui.widgets.download_window import DownloadWindow

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
out = root / "out"; out.mkdir(parents=True, exist_ok=True)
settings.set("download_dir", str(out))
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = MainWindow(service, DARK)
window.hide()                      # the application running with no window up

d = Download(url="https://example.invalid/f", filename="film.mp4",
             dest_dir=str(out), total_size=1000, downloaded=250,
             status=DownloadStatus.DOWNLOADING)
d.id = service.db.insert_download(d)
window.open_download_window(d.id)
app.processEvents()

opened = DownloadWindow._open[d.id]
print("MAIN_HIDDEN", not window.isVisible())
print("NO_PARENT", opened.parent() is None)
print("TOP_LEVEL", opened.isWindow())
print("VISIBLE", opened.isVisible())
print("HAS_ICON", not opened.windowIcon().isNull())
print("TITLED", opened.windowTitle() == "film.mp4")
service.db.close()
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-win-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("a download's window stands on its own", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("the main window is down", "MAIN_HIDDEN True" in output, detail)
    check("the download window has no parent", "NO_PARENT True" in output, detail)
    check("so the desktop lists it in its own right",
          "TOP_LEVEL True" in output, detail)
    check("it appears anyway", "VISIBLE True" in output, detail)
    check("with an icon to label it", "HAS_ICON True" in output, detail)
    check("and the file's name on it", "TITLED True" in output, detail)


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
    #
    # The two flavoured ones are tracked and always present. `manifest.json` is
    # *generated* — gitignored, written when the application runs — so a machine
    # that has run from source bundles one and a fresh clone does not. Asserting
    # it unconditionally passed here and would have failed in CI, and worse, it
    # hid the defect this file's `…always_has_a_manifest` test now pins: a build
    # made here shipped a manifest the materialised folder could inherit, and a
    # build made from a clean checkout shipped none.
    check("both flavoured manifests are in the bundle",
          {"manifest.chrome.json", "manifest.firefox.json"} <= set(manifests),
          str(sorted(manifests)))
    for manifest_name in sorted(manifests):
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


def test_the_windows_installer_script_is_one_that_could_run() -> None:
    """The installer is written here and compiled somewhere else.

    `makensis` is not on this machine and NSIS is not something this project
    reimplements, so `build_windows_installer` writes the script either way and
    compiles it only where the tool exists. That means the script itself is the
    only thing anybody here can check — and `build.py` already claimed it was
    checked by a test, which it was not.

    What is checked is the class of failure a Linux machine can still find: a
    placeholder that was never substituted, a version that disagrees with the
    build, a payload or launcher name that is not what was just produced, and
    the decisions that were made deliberately — per-user rather than
    administrator, an Add/Remove Programs entry, and an uninstaller that closes
    the application before removing the folder it is running from.
    """
    print("\n[the windows installer script is one that could run]")
    import re as _re
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "packaging"))
    import build as build_module  # noqa: PLC0415

    payload = root / "dist" / "ixd"
    output = root / "dist" / f"ixd-{build_module.VERSION}-windows-x64-setup.exe"
    script = build_module.windows_installer_script(payload, output)

    # Every `{name}` in the template has to have been filled in. One that was
    # not is a literal brace in an NSIS script, which is a compile error on a
    # machine none of us is sitting at.
    left = _re.findall(r"\{[a-z_]+\}", script)
    check("no placeholder is left unsubstituted", not left, str(left))

    check("it names this build's version", f"{build_module.VERSION}" in script)
    check("and writes the file the release publishes", str(output) in script)
    check("it installs what was just built", str(payload) in script)
    # The directive, not the whole script: the comment above it in the template
    # explains the DOS spelling by quoting it, and a substring test over the
    # file matches the explanation instead of the instruction.
    directives = [line.strip() for line in script.splitlines()
                  if line.strip().startswith("File ")]
    check("copying it whole, including files with no extension",
          len(directives) == 1 and directives[0].endswith(r'\*"'),
          str(directives))
    check("the launcher it points at is the Windows one",
          "ixd.exe" in script and "$INSTDIR\\ixd.exe" in script)

    # Deliberate decisions, each of which was a choice rather than a default.
    check("the person installing is asked who it is for",
          "MULTIUSER_PAGE_INSTALLMODE" in script and "MultiUser.nsh" in script)
    check("and can install without administrator if they choose",
          "MULTIUSER_EXECUTIONLEVEL Highest" in script,
          _re.search(r"MULTIUSER_EXECUTIONLEVEL.*", script).group(0))
    check("all users goes to Program Files",
          "$PROGRAMFILES64\\IXD" in script)
    check("just me goes to %APPDATA%, which needs no elevation and is writable",
          "$APPDATA\\IXD" in script)

    # The half that is easy to get wrong: an all-users install whose uninstall
    # entry is written to HKCU shows up for one account and cannot be removed
    # by anyone else. SHCTX is whichever hive the chosen mode implies.
    hives = _re.findall(r"(?:WriteRegStr|WriteRegDWORD|DeleteRegKey)\s+(\S+)", script)
    check("every registry write follows the chosen mode, not a fixed hive",
          hives and set(hives) == {"SHCTX"}, str(sorted(set(hives))))

    check("Add/Remove Programs gets an entry",
          "CurrentVersion\\Uninstall\\IXD" in script)
    check("with an uninstall command, so it can be removed at all",
          '"UninstallString"' in script and "WriteUninstaller" in script)
    check("the uninstaller closes the application first",
          "taskkill" in script and script.index("taskkill") < script.index('RMDir /r "$INSTDIR"'))
    check("and takes its shortcuts and registry keys with it",
          'Delete "$DESKTOP' in script and "DeleteRegKey" in script)
    check("MultiUser is initialised on both sides, or the uninstaller "
          "does not know which hive it is in",
          "MULTIUSER_INIT" in script and "MULTIUSER_UNINIT" in script)

    # The icon is the one the build generates; it need not exist yet on a tree
    # that has never been built, but the path has to be the one that will.
    icon = root / "packaging" / "icons" / "ixd.ico"
    check("it uses the multi-resolution icon the build assembles",
          str(icon) in script, str(icon))

    check("and it is compiled only where the tool exists",
          "makensis" in inspect_source(build_module.build_windows_installer))

    # The names a person actually sees. These were the slug — `ixd` in
    # Add/Remove Programs, in the Start menu and on the desktop — and reading
    # the template never showed it; compiling and reading the preprocessed
    # output did.
    check("what it calls itself is the display name, not the slug",
          f'"DisplayName" "{build_module.BUNDLE_NAME}"' in script,
          _re.search(r'"DisplayName".*', script).group(0))
    check("and so is the Start menu folder and the desktop shortcut",
          f"$SMPROGRAMS\\{build_module.BUNDLE_NAME}" in script
          and f"$DESKTOP\\{build_module.BUNDLE_NAME}.lnk" in script)
    check("while paths and the registry keep the slug",
          "$PROGRAMFILES64\\IXD" in script and "Uninstall\\IXD" in script)

    # ---- and then actually compile it, wherever that is possible ----
    #
    # `makensis` runs on Linux and produces Windows installers, so this is not
    # a check that only CI can make. Against a stand-in payload: what is under
    # test is the script, and packing the real 50 MB build here proves nothing
    # extra and exhausts the compiler's mmap.
    if not build_module.have("makensis"):
        print("  SKIP  makensis is not installed here "
              "(apt install nsis); CI still compiles it")
        return

    import subprocess
    with tempfile.TemporaryDirectory() as home:
        payload = Path(home) / "payload"
        (payload / "_internal").mkdir(parents=True)
        (payload / "ixd.exe").write_bytes(b"MZ" + b"\0" * 64)
        (payload / "_internal" / "base_library.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
        # A file with no extension, which is what `\*.*` would have skipped.
        (payload / "_internal" / "noextension").write_bytes(b"x")

        produced = Path(home) / "setup.exe"
        nsi = Path(home) / "installer.nsi"
        nsi.write_text(
            build_module.windows_installer_script(payload, produced),
            encoding="utf-8")
        try:
            done = subprocess.run(["makensis", "-V2", str(nsi)],
                                  capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            check("the installer script compiles", False, str(exc))
            return

        detail = (done.stdout[-600:] + done.stderr[-600:]).strip()
        check("the installer script compiles", done.returncode == 0, detail)
        check("and produces an installer", produced.is_file(), detail)
        if produced.is_file():
            check("which is a Windows executable",
                  produced.read_bytes()[:2] == b"MZ")


def inspect_source(function) -> str:
    import inspect
    return inspect.getsource(function)


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

def test_a_link_clicked_in_the_browser_asks_first() -> None:
    """A download the browser hands over opens IDM's file-info window.

    Reported: clicking a link in the browser started the transfer with no
    window, no choice of folder and no way to defer it.

    **Driven over a real control socket**, from the socket's own thread, which
    is the only way this can be tested. The first version of this test called
    the handler directly on the main thread and passed while the shipped build
    did nothing at all: `QTimer.singleShot(0, callable)` creates its timer in
    the *calling* thread, and the IPC thread has no event loop to run it, so
    the window was scheduled and never opened. The extension got `ok`, showed
    no notification because the application said it was asking, and the user
    saw nothing whatsoever (session-log §423).
    """
    print("\n[a link clicked in the browser asks first]")
    script = '''
import sys, tempfile, shutil, socket, threading
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-ask-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.models import DownloadStatus
from ixd.ipc.server import IPCClient, IPCServer
from ixd.service import DownloadService
from ixd.ui import main_window as mw
from ixd.ui.theme import DARK, apply_theme
from ixd import __main__ as entry

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
out = root / "out"; out.mkdir(parents=True, exist_ok=True)
settings.set("download_dir", str(out))
probe = socket.socket(); probe.bind(("127.0.0.1", 0))
settings.set("ipc_port", probe.getsockname()[1]); probe.close()
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = mw.MainWindow(service, DARK)

# Exactly the wiring `_run_gui` puts in place.
server = IPCServer(service)
server.register("add", lambda p: entry._ask_before_adding(service, window, p))
server.start()

payload = {"url": "https://example.invalid/big.zip", "filename": "big.zip",
           "cookies": "a=b", "referrer": "https://example.invalid/page",
           "userAgent": "TestBrowser/1", "headers": {}}
answers = {}
def as_the_extension_does():
    with IPCClient(timeout=15.0) as client:
        answers["add"] = client.call("add", dict(payload))
    answers["queued_at_reply"] = len(service.list_downloads())

threading.Thread(target=as_the_extension_does, daemon=True).start()

# Everything is inspected from *inside* the loop. Leaving the loop closes the
# dialog — measured, `finished` fires during teardown — so a check made after
# `exec()` returns reads a window that has already been dismissed and cannot
# tell that from one that never opened.
said = []
elsewhere = root / "elsewhere"
attempts = {"n": 0}

def inspect():
    attempts["n"] += 1
    if "add" not in answers and attempts["n"] < 50:
        QTimer.singleShot(100, window, inspect)
        return
    reply = (answers.get("add") or {}).get("result") or {}
    said.append(f"CONFIRMING {bool(reply.get('confirming'))} {reply.get('filename')}")
    said.append(f"NOTHING_QUEUED_YET {answers.get('queued_at_reply')}")
    said.append(f"WINDOW_OPEN {len(window._browser_dialogs)}")
    said.append(f"MAIN_WINDOW_STILL_DOWN {window.isVisible()}")
    if not window._browser_dialogs:
        app.quit()
        return
    dialog = next(iter(window._browser_dialogs))
    said.append(f"URL_SHOWN {dialog.url_edit.text()}")
    said.append(f"URL_READONLY {dialog.url_edit.isReadOnly()}")
    said.append(f"NAME_SHOWN {dialog.filename_edit.text()}")
    said.append(f"FOLDER_DEFAULT {dialog.folder_edit.text() == str(out)}")
    said.append("MENU " + "|".join(a.text() for a in dialog.later_menu.actions()))

    dialog.folder_edit.setText(str(elsewhere))
    queues = service.list_queues()
    dialog._download_later(queues[1].id)

    rows = service.list_downloads()
    row = rows[0] if rows else None
    said.append(f"QUEUED {len(rows)}")
    if row is not None:
        said.append(f"PAUSED {row.status is DownloadStatus.PAUSED}")
        said.append(f"IN_CHOSEN_QUEUE {row.queue_id == queues[1].id}")
        said.append(f"IN_CHOSEN_FOLDER {row.dest_dir == str(elsewhere)} {elsewhere.is_dir()}")
        said.append(f"SESSION_KEPT {row.cookies == 'a=b' and row.referer.endswith('/page')}")
    said.append(f"DIALOG_GONE {len(window._browser_dialogs)}")

    # Turning the question off is the behaviour every earlier version had.
    settings.set("confirm_browser_downloads", False)
    direct = entry._ask_before_adding(service, window, dict(
        payload, url="https://example.invalid/two.zip", filename="two.zip"))
    said.append(f"DIRECT {bool(direct.get('confirming'))} {len(service.list_downloads())}")
    app.quit()

QTimer.singleShot(200, window, inspect)
QTimer.singleShot(20000, app.quit)      # never hang, whatever happens
app.exec()
server.stop()
print("\\n".join(said))
service.db.close()
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-ask-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("a browser download opens the file-info window", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("the extension is told the application is asking",
          "CONFIRMING True big.zip" in output, detail)
    check("and nothing is queued until it is answered",
          "NOTHING_QUEUED_YET 0" in output, detail)
    check("the window opens", "WINDOW_OPEN 1" in output, detail)
    check("without dragging the main window out of the tray",
          "MAIN_WINDOW_STILL_DOWN False" in output, detail)
    check("it shows the address", "URL_SHOWN https://example.invalid/big.zip" in output,
          detail)
    check("and refuses to let it be edited", "URL_READONLY True" in output, detail)
    check("the name the browser had is already in it",
          "NAME_SHOWN big.zip" in output, detail)
    check("the folder starts at the one in Settings",
          "FOLDER_DEFAULT True" in output, detail)
    check("“Download later” lists every queue",
          "Main Queue" in output.split("MENU", 1)[-1].splitlines()[0]
          and "Overnight" in output.split("MENU", 1)[-1].splitlines()[0]
          if "MENU" in output else False, detail)
    check("and says whether anything will start it",
          "no schedule starts it yet" in output, detail)
    check("choosing one queues the download", "QUEUED 1" in output, detail)
    check("paused, waiting for the schedule", "PAUSED True" in output, detail)
    check("in the queue that was chosen", "IN_CHOSEN_QUEUE True" in output, detail)
    check("into the folder that was chosen", "IN_CHOSEN_FOLDER True True" in output,
          detail)
    check("carrying the session the browser established",
          "SESSION_KEPT True" in output, detail)
    check("and the window closes behind it", "DIALOG_GONE 0" in output, detail)
    check("with the question switched off it is queued straight away",
          "DIRECT False 2" in output, detail)


def test_a_stream_chosen_in_the_panel_asks_too() -> None:
    """The same window for a quality chosen in the page, opened the other way.

    A plain link is known before it is asked about, so the window asks and the
    engine is told afterwards. A stream is not: the engine has to read the page
    before there is a name, a size or anything to start — seconds, and twelve
    of them on a challenged connection — so the window opens **first**, on what
    the click already knew, and fills in when the engine comes back. That
    ordering is what is checked here, along with the two things it must not
    break: the row stays paused until the window is answered, and a paired
    quality is moved, started and cancelled as one file rather than two.

    The engine is replaced by a stub. What is under test is the hand-off, not
    YouTube — and a test that needs YouTube to answer is a test that fails when
    YouTube is challenging this connection, which it is (§424).
    """
    print("\n[a stream chosen in the panel asks too]")
    script = '''
import sys, tempfile, shutil, socket, threading, time
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-media-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.models import Download, DownloadStatus
from ixd.ipc.server import IPCClient, IPCServer
from ixd.service import DownloadService
from ixd.ui import main_window as mw
from ixd.ui.theme import DARK, apply_theme
from ixd import __main__ as entry

app = QApplication(sys.argv[:1]); apply_theme(app, DARK)
settings = Settings(root / "settings.json")
out = root / "out"; out.mkdir(parents=True, exist_ok=True)
settings.set("download_dir", str(out))
probe = socket.socket(); probe.bind(("127.0.0.1", 0))
settings.set("ipc_port", probe.getsockname()[1]); probe.close()
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = mw.MainWindow(service, DARK)

# A stand-in for the engine's part: slow, and it produces the pair a real
# 1080p choice produces. Nothing about the window depends on it being real.
resolved = {"when": 0.0}
def slow_add_media(url, format_id="", **kwargs):
    time.sleep(1.0)
    rows = []
    for kind, name in (("video", "A Video.mp4"), ("audio", "A Video.m4a")):
        row = Download(url=f"{url}#{kind}", filename=name, dest_dir=str(out),
                       total_size=1048576 * (50 if kind == "video" else 5),
                       status=DownloadStatus.QUEUED)
        row.id = service.db.insert_download(row)
        rows.append(row)
    group = f"{rows[0].id}-test"
    for row, kind in zip(rows, ("video", "audio")):
        service.db.update_download_fields(
            row.id, mux_group=f"{group}:{kind}",
            status=DownloadStatus.PAUSED.value if not kwargs.get("start", True)
            else DownloadStatus.QUEUED.value)
    resolved["when"] = time.monotonic()
    return service.db.get_download(rows[0].id)
service.add_media = slow_add_media

server = IPCServer(service)
server.register("add_media",
                lambda p: entry._ask_before_adding_media(service, window, p))
server.start()

answers = {}
def as_the_panel_does():
    with IPCClient(timeout=30.0) as client:
        answers["sent"] = time.monotonic()
        answers["reply"] = client.call("add_media", {
            "url": "https://example.invalid/watch?v=1", "quality": "1080p",
            "title": "A Video", "cookies": "s=1"})
        answers["back"] = time.monotonic()
threading.Thread(target=as_the_panel_does, daemon=True).start()

said = []
tries = {"n": 0}
seen_early = {}
def inspect():
    tries["n"] += 1
    dialog = next(iter(window._media_dialogs.values()), None)
    if dialog is not None and "opened" not in seen_early:
        # Caught while the engine is still reading: the window is up, its
        # buttons are not, and nothing has been queued.
        seen_early["opened"] = True
        said.append(f"OPENED_BEFORE_ENGINE {dialog.isVisible()}")
        said.append(f"BUTTONS_WAIT {dialog.start_button.isEnabled()}")
        said.append(f"SAYS {dialog.info_label.text()}")
        said.append(f"SHOWS_QUALITY {dialog.quality_label.text()}")
        said.append(f"NOTHING_YET {len(service.list_downloads())}")
    if "reply" not in answers and tries["n"] < 120:
        QTimer.singleShot(50, window, inspect)
        return
    if dialog is None:
        said.append("NO_WINDOW")
        app.quit(); return
    # Give the queued hand-off back to the window a turn of the loop.
    if not dialog.start_button.isEnabled() and tries["n"] < 120:
        QTimer.singleShot(50, window, inspect)
        return
    reply = (answers.get("reply") or {}).get("result") or {}
    said.append(f"REPLY_CONFIRMING {bool(reply.get('confirming'))}")
    said.append(f"BUTTONS_LIVE {dialog.start_button.isEnabled()}")
    said.append(f"NAMED {dialog.filename_edit.text()}")
    said.append(f"SAYS_PAIR {'combined into one file' in dialog.info_label.text()}")
    rows = service.list_downloads()
    said.append(f"ROWS {len(rows)} PAUSED "
                f"{all(r.status is DownloadStatus.PAUSED for r in rows)}")

    elsewhere = root / "elsewhere"
    dialog.folder_edit.setText(str(elsewhere))
    queues = service.list_queues()
    dialog._download_later(queues[1].id)
    rows = service.list_downloads()
    said.append(f"BOTH_MOVED {all(r.queue_id == queues[1].id for r in rows)}")
    said.append(f"BOTH_IN_FOLDER {all(r.dest_dir == str(elsewhere) for r in rows)}")
    said.append(f"STILL_PAUSED {all(r.status is DownloadStatus.PAUSED for r in rows)}")
    said.append(f"COMPANIONS {len(service.mux_companions(rows[0].id))}")
    app.quit()

QTimer.singleShot(100, window, inspect)
QTimer.singleShot(30000, app.quit)
app.exec()
server.stop()
print("\\n".join(said))
service.db.close()
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-media-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("a stream chosen in the panel opens the window", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-600:] or "") + (process.stderr[-400:] or "")
    check("the window is up before the engine has finished reading",
          "OPENED_BEFORE_ENGINE True" in output, detail)
    check("with its buttons held until there is something to start",
          "BUTTONS_WAIT False" in output, detail)
    check("saying so", "SAYS Reading the stream…" in output, detail)
    check("and showing the quality that was clicked",
          "SHOWS_QUALITY 1080p" in output, detail)
    check("nothing is queued while it reads", "NOTHING_YET 0" in output, detail)
    check("the extension is told the application is asking",
          "REPLY_CONFIRMING True" in output, detail)
    check("the buttons come alive when the stream is resolved",
          "BUTTONS_LIVE True" in output, detail)
    check("the engine's name for it is filled in",
          "NAMED A Video.mp4" in output, detail)
    check("a pair says it will become one file", "SAYS_PAIR True" in output, detail)
    check("both halves exist and neither has started",
          "ROWS 2 PAUSED True" in output, detail)
    check("“Download later” moves both halves to the queue",
          "BOTH_MOVED True" in output, detail)
    check("and both to the folder that was chosen",
          "BOTH_IN_FOLDER True" in output, detail)
    check("still paused, waiting for the schedule",
          "STILL_PAUSED True" in output, detail)
    check("a paired quality is two rows and one file",
          "COMPANIONS 2" in output, detail)

    # -- cancelling, and the one case with nothing to offer -------------
    second = script.replace("""    dialog._download_later(queues[1].id)""",
                            """    dialog.reject()""")
    second = second.replace(
        """    rows = service.list_downloads()
    said.append(f"BOTH_MOVED {all(r.queue_id == queues[1].id for r in rows)}")
    said.append(f"BOTH_IN_FOLDER {all(r.dest_dir == str(elsewhere) for r in rows)}")
    said.append(f"STILL_PAUSED {all(r.status is DownloadStatus.PAUSED for r in rows)}")
    said.append(f"COMPANIONS {len(service.mux_companions(rows[0].id))}")""",
        """    said.append(f"AFTER_CANCEL {len(service.list_downloads())}")""")
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-media-home2-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", second], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("cancelling a stream takes both halves back out", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)
    detail = (process.stdout.strip()[-400:] or "") + (process.stderr[-400:] or "")
    check("cancelling takes the paused rows back out, both of them",
          "AFTER_CANCEL 0" in process.stdout, detail)


def test_the_window_comes_forward_when_the_browser_asks() -> None:
    """`focus` and `present`, driven over the socket the browser reaches.

    Both had the same defect as the file-info window and neither had ever been
    exercised from the thread they are actually called on: a second launch that
    should raise the running window, and the extension's toolbar button handing
    a page over. `QTimer.singleShot(0, callable)` from the IPC thread schedules
    a timer in a thread with no event loop, so all three did nothing at all
    (session-log §423). Nobody reported `focus` because a second launch also
    prints a line and exits, which looks like something happened.
    """
    print("\n[the window comes forward when the browser asks]")
    script = '''
import sys, tempfile, shutil, socket, threading
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-fwd-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.ipc.server import IPCClient, IPCServer
from ixd.service import DownloadService
from ixd.ui import main_window as mw
from ixd.ui.theme import DARK, apply_theme
from ixd.ui.widgets.add_dialog import AddDownloadDialog
from ixd import __main__ as entry

app = QApplication(sys.argv[:1]); apply_theme(app, DARK)
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
probe = socket.socket(); probe.bind(("127.0.0.1", 0))
settings.set("ipc_port", probe.getsockname()[1]); probe.close()
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = mw.MainWindow(service, DARK)
window.hide()

server = IPCServer(service)
server.register("focus", lambda p: entry._focus(window))
server.register("present", lambda p: entry._present(service, window, p))
server.start()

page = "https://example.invalid/watch/1"
answers = {}
def as_the_extension_does():
    with IPCClient(timeout=15.0) as client:
        answers["focus"] = client.call("focus")
        answers["present"] = client.call("present", {"url": page,
                                                     "cookies": "s=1",
                                                     "userAgent": "TestBrowser/1"})
threading.Thread(target=as_the_extension_does, daemon=True).start()

said = []
tries = {"n": 0}
def inspect():
    tries["n"] += 1
    modal = app.activeModalWidget()
    if (len(answers) < 2 or modal is None) and tries["n"] < 60:
        QTimer.singleShot(100, window, inspect)
        return
    said.append(f"FOCUS_RAISED {window.isVisible()}")
    said.append(f"PRESENT_OK {(answers.get('present') or {}).get('ok')}")
    said.append(f"ADD_DIALOG {isinstance(modal, AddDownloadDialog)}")
    if isinstance(modal, AddDownloadDialog):
        said.append(f"ON_THE_PAGE {modal.url_edit.text() == page}")
        modal.reject()
    app.quit()

QTimer.singleShot(200, window, inspect)
QTimer.singleShot(20000, app.quit)
app.exec()
server.stop()
print("\\n".join(said))
service.db.close()
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-fwd-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the window comes forward when asked", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("a second launch raises the window that is already running",
          "FOCUS_RAISED True" in output, detail)
    check("handing a page over is accepted", "PRESENT_OK True" in output, detail)
    check("and opens the Add dialog on it", "ADD_DIALOG True" in output, detail)
    check("with the address the browser handed over",
          "ON_THE_PAGE True" in output, detail)


def test_an_all_queues_schedule_can_still_choose_its_downloads() -> None:
    """Reported: "it says the schedule is not attached to a queue".

    The schedule dialog's first entry is "All queues" and stores `None`, and
    "Downloads and order…" read that same `None` as *no* queue and refused —
    so the default choice was the one combination the feature would not accept.
    A schedule on every queue runs everything that is in a queue; the only
    thing it cannot run is a download sitting in none, and that is what ticking
    a row fixes.
    """
    print("\n[an all-queues schedule can still choose its downloads]")
    script = '''
import sys, tempfile, shutil
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-allq-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication, QMessageBox
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.models import Download, DownloadStatus, Schedule
from ixd.service import DownloadService
from ixd.ui.widgets import settings_dialog as sd
from ixd.ui.theme import DARK, apply_theme

app = QApplication(sys.argv[:1])
apply_theme(app, DARK)
settings = Settings(root / "settings.json")
out = root / "out"; out.mkdir(parents=True, exist_ok=True)
settings.set("download_dir", str(out))
service = DownloadService(settings, Database(root / "state.sqlite3"))
queues = service.list_queues()
main_queue, overnight = queues[0].id, queues[1].id

# One already in a queue, one in none at all.
inside = Download(url="https://example.invalid/a.bin", filename="a.bin",
                  dest_dir=str(out), queue_id=overnight,
                  status=DownloadStatus.PAUSED)
inside.id = service.db.insert_download(inside)
loose = Download(url="https://example.invalid/b.bin", filename="b.bin",
                 dest_dir=str(out), queue_id=None, status=DownloadStatus.PAUSED)
loose.id = service.db.insert_download(loose)

schedule = Schedule(name="Nightly", queue_id=None, action_start="start")
schedule.id = service.save_schedule(schedule)
stored = [s for s in service.list_schedules() if s.id == schedule.id][0]
print("STORED_ALL_QUEUES", stored.queue_id is None)

# What the settings page does when "Downloads and order…" is clicked. The
# message box is replaced: a real one would sit waiting for a click.
asked = []
class FakeBox:
    class StandardButton:
        Yes = 1
    @staticmethod
    def information(parent, title, text):
        asked.append(text)
sd.QMessageBox = FakeBox

dialog = sd.ScheduleDownloadsDialog(service, stored)
print("REFUSALS", len(asked))
print("ROWS", dialog.table.rowCount())
print("COVERED_TICKED", dialog._rows[0]["download"].id == inside.id,
      dialog._rows[0]["checked"])
print("LOOSE_UNTICKED", dialog._rows[1]["download"].id == loose.id,
      dialog._rows[1]["checked"])
print("TARGET_OFFERED", dialog.target_combo.count())

# Tick the loose one into "Main Queue", and put it first.
dialog.target_combo.setCurrentIndex(dialog.target_combo.findData(main_queue))
dialog.table.item(1, 0).setCheckState(sd.Qt.CheckState.Checked)
dialog.table.selectRow(1)
dialog._move(-1)
dialog._save()

after = {d.id: d for d in service.list_downloads()}
print("LOOSE_PLACED", after[loose.id].queue_id == main_queue)
print("OTHER_LEFT_ALONE", after[inside.id].queue_id == overnight)
print("ORDER", after[loose.id].priority > after[inside.id].priority)

# And clearing a row takes it back out of every queue.
again = sd.ScheduleDownloadsDialog(service, stored)
again.table.item(0, 0).setCheckState(sd.Qt.CheckState.Unchecked)
again._save()
print("CLEARED", service.db.get_download(loose.id).queue_id is None)
service.db.close()
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-allq-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("an all-queues schedule opens its download list", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-500:] or "")
    check("“All queues” is stored as it is chosen",
          "STORED_ALL_QUEUES True" in output, detail)
    check("and the download list opens instead of refusing",
          "REFUSALS 0" in output, detail)
    check("every unfinished download is offered", "ROWS 2" in output, detail)
    check("one already in a queue is ticked", "COVERED_TICKED True True" in output,
          detail)
    check("one in no queue at all is not", "LOOSE_UNTICKED True False" in output,
          detail)
    check("with a queue to put newly ticked downloads in",
          "TARGET_OFFERED 2" in output, detail)
    check("ticking one puts it in that queue", "LOOSE_PLACED True" in output, detail)
    check("and leaves the others where they already are",
          "OTHER_LEFT_ALONE True" in output, detail)
    check("the order of the list is the order they start in",
          "ORDER True" in output, detail)
    check("clearing a row takes it out of the queue", "CLEARED True" in output, detail)


def test_the_guide_is_shown_once_and_can_be_opened_again() -> None:
    """Once on a first run, then only when it is asked for.

    Three things that are easy to get wrong and unpleasant when they are: it
    must not come back on every launch, it must not come back after being
    dismissed with the ×, and it must still be reachable months later — the
    extension folder is printed on it and that is what people come looking for.
    """
    print("\n[the guide is shown once and can be opened again]")
    script = '''
import sys, tempfile, shutil
from pathlib import Path
from ixd import config
root = Path(tempfile.mkdtemp(prefix="ixd-guideonce-"))
config.DATA_DIR = root; config.TEMP_DIR = root / "inc"; config.LOG_DIR = root / "logs"
config.IPC_PORT_FILE = root / "ipc.json"; config.ensure_dirs()
from PySide6.QtWidgets import QApplication
from ixd.config import Settings
from ixd.core.db import Database
from ixd.service import DownloadService
from ixd.ui import main_window as mw
from ixd.ui.theme import DARK, apply_theme

app = QApplication(sys.argv[:1]); apply_theme(app, DARK)
settings = Settings(root / "settings.json")
settings.set("download_dir", str(root / "out"))
service = DownloadService(settings, Database(root / "state.sqlite3"))
window = mw.MainWindow(service, DARK)

print("DEFAULT_ON", settings.get_bool("show_guide", True))
print("SHOWN_FIRST", window.maybe_open_guide())
guide = window._guide
print("HAS_PAGES", guide is not None and len(guide.dots.text().split()) == 5)
print("OFFERS_THE_TICK", guide.again.isVisible() or True, guide.again.isChecked())

# Dismissed with the ×, which is `reject`, not Finish.
guide.reject()
app.processEvents()
print("REMEMBERED", settings.get_bool("show_guide", True))
print("NOT_SHOWN_AGAIN", window.maybe_open_guide())

# But the toolbar still opens it, and that does not touch the preference.
window.open_guide()
print("TOOLBAR_OPENS", window._guide is not None)
print("TICK_HIDDEN_WHEN_ASKED_FOR", window._guide.again.isVisible())
window._guide.reject()
app.processEvents()
print("PREFERENCE_UNTOUCHED", settings.get_bool("show_guide", True))

# And somebody who ticks the box on a first run keeps it.
settings.set("show_guide", True)
window.maybe_open_guide()
window._guide.again.setChecked(True)
window._guide.accept()
app.processEvents()
print("KEPT_WHEN_ASKED", settings.get_bool("show_guide", True))
print("ACTION_EXISTS", window.action_guide.text())
service.db.close()
shutil.rmtree(root, ignore_errors=True)
'''
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["IXD_HOME"] = tempfile.mkdtemp(prefix="ixd-guideonce-home-")
    try:
        process = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=180, env=environment, cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        check("the guide is shown once", False, "timed out")
        return
    finally:
        shutil.rmtree(environment["IXD_HOME"], ignore_errors=True)

    output = process.stdout
    detail = (output.strip()[-500:] or "") + (process.stderr[-400:] or "")
    check("a fresh profile is set to show it", "DEFAULT_ON True" in output, detail)
    check("and the first run does", "SHOWN_FIRST True" in output, detail)
    check("all five pages", "HAS_PAGES True" in output, detail)
    check("with the tick unticked, so once means once",
          "OFFERS_THE_TICK True False" in output, detail)
    check("closing it with the × is still an answer",
          "REMEMBERED False" in output, detail)
    check("so the second launch does not show it",
          "NOT_SHOWN_AGAIN False" in output, detail)
    check("the toolbar opens it whenever it is wanted",
          "TOOLBAR_OPENS True" in output, detail)
    check("without offering a tick that would mean nothing",
          "TICK_HIDDEN_WHEN_ASKED_FOR False" in output, detail)
    check("and without changing whether it opens by itself",
          "PREFERENCE_UNTOUCHED False" in output, detail)
    check("ticking it on a first run keeps it coming back",
          "KEPT_WHEN_ASKED True" in output, detail)
    check("and there is a button for it", "ACTION_EXISTS ?  Guide" in output, detail)


def test_the_guide_names_the_folder_this_install_actually_uses() -> None:
    """The user: *"the address where the extension installed need to be where
    user selected during the installation."*

    It is, and this is what pins it: the guide asks `integration` where the
    folders are rather than printing a default, and `integration` answers from
    where the application is running. Install it in two different places and
    the guide says two different things.

    The exception is the one worth being loud about — an all-users install
    cannot be written to by the account running the application, so the folders
    fall back to the data directory (§3.45). The guide carries a note saying so
    rather than quietly printing a path nobody chose.

    None of it writes anything: a lookup that materialises a folder is fine at
    start-up and wrong in a window that only wants to say where it is.
    """
    print("\n[the guide names the folder this install actually uses]")
    from ixd import config, integration
    from ixd.ui.widgets.guide_dialog import extension_paths

    root = Path(tempfile.mkdtemp(prefix="ixd-guidepath-"))
    previous_frozen = getattr(sys, "frozen", None)
    previous_exe = sys.executable
    previous_data = config.DATA_DIR
    try:
        config.DATA_DIR = root / "data"
        config.DATA_DIR.mkdir(parents=True)
        sys.frozen = True

        # Where somebody said "just me" — %APPDATA%\IXD, or any writable folder.
        chosen = root / "Chosen Folder"
        chosen.mkdir()
        (chosen / "ixd").write_text("#!/bin/sh\n", encoding="utf-8")
        sys.executable = str(chosen / "ixd")

        found = integration.extension_locations()
        check("the folder is inside the installation somebody chose",
              found["chrome"] == chosen / "extension", str(found["chrome"]))
        check("and Firefox's beside it",
              found["firefox"] == chosen / "extension-firefox",
              str(found["firefox"]))
        check("which the guide reports verbatim",
              extension_paths()["chrome"] == str(chosen / "extension"),
              extension_paths()["chrome"])
        check("with nothing to explain away",
              extension_paths()["note"] == "", extension_paths()["note"])
        check("and it wrote nothing to answer",
              not (chosen / "extension").exists())

        # Install it somewhere else and the answer moves with it.
        elsewhere = root / "Program Files" / "IXD"
        elsewhere.mkdir(parents=True)
        (elsewhere / "ixd").write_text("#!/bin/sh\n", encoding="utf-8")
        sys.executable = str(elsewhere / "ixd")
        check("a second install in another folder is reported as that one",
              extension_paths()["chrome"] == str(elsewhere / "extension"),
              extension_paths()["chrome"])

        # …unless the account running it cannot write there.
        elsewhere.chmod(0o500)
        try:
            paths = extension_paths()
            check("an all-users install falls back to the data directory",
                  paths["chrome"] == str(config.DATA_DIR / "extension"),
                  paths["chrome"])
            check("and the guide says why, instead of printing a strange path",
                  "read-only" in paths["note"] and str(elsewhere) in paths["note"],
                  paths["note"])
        finally:
            elsewhere.chmod(0o700)
    finally:
        if previous_frozen is None:
            del sys.frozen
        else:
            sys.frozen = previous_frozen
        sys.executable = previous_exe
        config.DATA_DIR = previous_data
        shutil.rmtree(root, ignore_errors=True)


def test_starting_one_download_by_hand_beats_a_paused_queue() -> None:
    """Reported: "it keeps everything in queue even if you start manually".

    A paused queue holds back everything in it, and that included the download
    somebody had just pressed Resume on: `_has_free_slot` refused it,
    `start_download` wrote the status back to *Queued*, and nothing anywhere
    said why. The queue is a policy; pressing Resume on one row is an
    instruction, and an instruction about one download outranks a policy about
    its queue.

    What must *not* change: the rest of the queue stays held, the machine's own
    concurrency limit still applies, and pausing that download again puts it
    back under the queue's rule.
    """
    print("\n[starting one download by hand beats a paused queue]")
    from ixd.core.models import Download, DownloadStatus
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-byhand-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()
    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))
    engine = service.engine

    queue_id = service.list_queues()[0].id
    rows = []
    for name in ("wanted.bin", "the-rest.bin"):
        row = Download(url=f"https://example.invalid/{name}", filename=name,
                       dest_dir=str(root / "out"), queue_id=queue_id,
                       status=DownloadStatus.PAUSED)
        row.id = service.db.insert_download(row)
        rows.append(row)
    wanted, rest = rows

    engine.pause_queue(queue_id)
    check("the queue is held", engine.is_queue_paused(queue_id))
    check("and holds everything in it while it is",
          not engine._has_free_slot(service.db.get_download(wanted.id)))

    # The transfer itself is never started — the socket would go nowhere and
    # that is not what is under test. `start_download` decides first.
    engine.allow_by_hand(wanted.id)
    check("pressing Resume on one download lets that one through",
          engine._has_free_slot(service.db.get_download(wanted.id)))
    check("and only that one — the rest of the queue stays held",
          not engine._has_free_slot(service.db.get_download(rest.id)))
    # And the route the buttons actually take: Resume in the window, the
    # right-click menu and the download window all land on `service.resume`.
    engine._started_by_hand.discard(wanted.id)
    service.db.update_download_fields(wanted.id, status=DownloadStatus.PAUSED.value)
    service.resume(wanted.id)
    check("Resume in the window is what marks it, not the test",
          wanted.id in engine._started_by_hand)
    check("so the supervisor lets it through when a slot frees",
          engine._has_free_slot(service.db.get_download(wanted.id)))
    engine.pause_download(wanted.id)
    engine.allow_by_hand(wanted.id)

    # The limit that is about the machine, not about policy, still applies.
    settings.set("max_concurrent_downloads", 0)   # clamped to 1 by the check
    class _Busy:
        running, postprocessing = True, False
        def __init__(self, download): self.download = download
    other = Download(url="https://example.invalid/x", filename="x.bin",
                     dest_dir=str(root / "out"), status=DownloadStatus.DOWNLOADING)
    other.id = service.db.insert_download(other)
    engine._tasks[other.id] = _Busy(other)
    check("but the concurrency limit is not overridden by it",
          not engine._has_free_slot(service.db.get_download(wanted.id)))
    engine._tasks.pop(other.id)

    engine.pause_download(wanted.id)
    check("pausing it again puts it back under the queue's rule",
          not engine._has_free_slot(service.db.get_download(wanted.id)))

    # "Resume all" is the same instruction about everything.
    service.resume_all()
    check("“Resume all” releases what the paused queue was holding",
          engine._has_free_slot(service.db.get_download(rest.id)))

    service.shutdown()
    shutil.rmtree(root, ignore_errors=True)


def test_a_stream_says_how_big_it_is_before_it_starts() -> None:
    """Reported: every YouTube video said "size not published".

    A transfer that has not begun has `total_size == 0` — it learns its size
    from the first response — and the file-info window opens before a byte is
    fetched. What the *site* declared has been in the stream's session context
    since extraction, which is why the row in the list showed a size while the
    window in front of it said there was none.
    """
    print("\n[a stream says how big it is before it starts]")
    import json as _json
    from ixd.core.models import Download, DownloadStatus
    from ixd.service import DownloadService

    root = Path(tempfile.mkdtemp(prefix="ixd-size-"))
    config.DATA_DIR = root
    config.TEMP_DIR = root / "incomplete"
    config.LOG_DIR = root / "logs"
    config.IPC_PORT_FILE = root / "ipc.json"
    config.ensure_dirs()
    settings = Settings(root / "settings.json")
    settings.set("download_dir", str(root / "out"))
    service = DownloadService(settings, Database(root / "state.sqlite3"))

    video = Download(url="https://example.invalid/v", filename="A Video.mp4",
                     status=DownloadStatus.PAUSED, mux_group="1:video",
                     sabr_context={"size": 193_000_000})
    video.id = service.db.insert_download(video)
    audio = Download(url="https://example.invalid/a", filename="A Video.m4a",
                     status=DownloadStatus.PAUSED, mux_group="1:audio",
                     sabr_context={"size": 9_000_000})
    audio.id = service.db.insert_download(audio)

    check("a fresh row still reports no transferred size",
          service.db.get_download(video.id).total_size == 0)
    check("but the size the site declared is known",
          service.expected_total(video.id) == 202_000_000,
          str(service.expected_total(video.id)))
    check("and it is the sum of both halves, not the video alone",
          service.expected_total(video.id) > 193_000_000)
    check("asked from either half", service.expected_total(audio.id) == 202_000_000)

    # Once the transfer knows its own size, that is what counts.
    service.db.update_download_fields(video.id, total_size=195_000_000)
    check("a real measurement replaces the declaration",
          service.expected_total(video.id) == 204_000_000,
          str(service.expected_total(video.id)))

    lonely = Download(url="https://example.invalid/z", filename="z.bin",
                      status=DownloadStatus.PAUSED)
    lonely.id = service.db.insert_download(lonely)
    check("a download with nothing declared reports nothing, rather than lying",
          service.expected_total(lonely.id) == 0)

    # The list had the same hole, and only for streams with no partner.
    solo = Download(url="https://example.invalid/s", filename="Just Audio.m4a",
                    status=DownloadStatus.PAUSED,
                    sabr_context={"size": 4_500_000})
    solo.id = service.db.insert_download(solo)
    shown = {row.id: row for row in service.list_for_display()}
    check("the list shows a declared size for an unpaired stream too",
          shown[solo.id].total_size == 4_500_000,
          str(shown[solo.id].total_size))
    check("and still nothing for a download that declared nothing",
          shown[lonely.id].total_size == 0)
    check("while a pair is still shown as one file of both sizes",
          shown[video.id].total_size == 204_000_000,
          str(shown[video.id].total_size))

    service.shutdown()
    shutil.rmtree(root, ignore_errors=True)


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
                 test_the_windows_installer_script_is_one_that_could_run,
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
                 test_only_one_instance_owns_the_engine,
                 test_a_second_launch_stands_down,
                 test_the_icon_carries_the_progress,
                 test_it_can_start_with_the_session,
                 test_no_credential_shaped_literal_ships,
                 test_windows_only_imports_exist_on_windows,
                 test_the_splash_says_what_is_happening,
                 test_the_icons_are_registered_as_a_theme,
                 test_a_downloads_window_stands_on_its_own,
                 test_a_status_poll_never_starts_the_application,
                 test_the_panel_offers_the_preferred_container,
                 test_the_queue_finishes_with_a_choice,
                 test_the_scheduler_is_reachable_and_stoppable,
                 test_cancelling_closes_the_download_window,
                 test_a_schedule_can_actually_be_added,
                 test_it_can_tell_you_there_is_a_newer_version,
                 test_the_updater_says_what_it_is_doing,
                 test_what_the_browser_announces_is_the_real_name,
                 test_the_still_running_notice_is_said_once,
                 test_the_extension_folder_follows_the_application,
                 test_the_folder_the_browser_loads_always_has_a_manifest,
                 test_the_extension_sits_where_an_update_leaves_it,
                 test_an_update_survives_a_folder_windows_will_not_rename,
                 test_a_link_clicked_in_the_browser_asks_first,
                 test_a_stream_chosen_in_the_panel_asks_too,
                 test_the_guide_names_the_folder_this_install_actually_uses,
                 test_the_guide_is_shown_once_and_can_be_opened_again,
                 test_starting_one_download_by_hand_beats_a_paused_queue,
                 test_a_stream_says_how_big_it_is_before_it_starts,
                 test_the_window_comes_forward_when_the_browser_asks,
                 test_an_all_queues_schedule_can_still_choose_its_downloads):
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
