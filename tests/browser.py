"""Drive a real browser over the DevTools protocol.

Everything else in this suite tests logic that has been lifted out of its
surroundings, and four consecutive sessions shipped a content script that
passed all of it and did nothing at all in a browser — a function that called
itself, a listener registered in the wrong phase, an inherited CSS property.
None of those is visible anywhere except in a page.

So this launches the browser with the extension loaded, opens a real page and
clicks the panel. No dependency is added: the WebSocket framing is RFC 6455 and
is written here, the same way every other protocol in this project is.
"""

from __future__ import annotations

import base64
import json
import secrets
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def find_browser() -> str:
    """A Chrome-family browser to drive, or ``""`` when none is installed."""
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome", "brave-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


class WebSocket:
    """The client half of RFC 6455, in the small: text frames, no extensions."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        rest = url.split("://", 1)[1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("the browser closed the debugging socket")
            buffer += chunk
        if b" 101 " not in buffer.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"debugging socket refused: {buffer[:120]!r}")
        self._rest = buffer.split(b"\r\n\r\n", 1)[1]

    def _recv(self, amount: int) -> bytes:
        while len(self._rest) < amount:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("the browser closed the debugging socket")
            self._rest += chunk
        head, self._rest = self._rest[:amount], self._rest[amount:]
        return head

    def send(self, payload: str) -> None:
        data = payload.encode()
        header = bytearray([0x81])
        mask = secrets.token_bytes(4)
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def receive(self) -> str:
        while True:
            first, second = self._recv(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv(8))[0]
            body = self._recv(length) if length else b""
            if opcode == 0x8:
                raise RuntimeError("the browser closed the debugging socket")
            if opcode == 0x9:            # ping → pong, or it hangs up on us
                self.sock.sendall(b"\x8a\x80" + secrets.token_bytes(4))
                continue
            if opcode in (0x1, 0x2):
                return body.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Browser:
    """A headless browser with the extension loaded, driven over CDP."""

    def __init__(self, extension: Path | None = None, timeout: float = 40.0) -> None:
        self.binary = find_browser()
        if not self.binary:
            raise RuntimeError("no Chrome-family browser is installed")
        # Not /tmp. A snap-confined browser — which is what `/snap/bin/chromium`
        # is, and what this machine's `find_browser()` picks — runs with a
        # private `/tmp` of its own and cannot see the host's, so a profile put
        # there is invisible to it along with the messaging manifest inside it.
        # `$HOME` is what the snap's `home` interface grants, and only
        # non-hidden paths within it, so the directory is named plainly and
        # removed on close.
        self.profile = Path(tempfile.mkdtemp(prefix="ixd-browser-",
                                             dir=str(Path.home())))
        self.timeout = timeout
        self._id = 0
        port = self._free_port()
        arguments = [
            self.binary,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={port}",
        ]
        if extension is not None:
            arguments += [
                f"--load-extension={extension}",
                f"--disable-extensions-except={extension}",
            ]
        if extension is not None:
            self._register_messaging_host()
        arguments.append("about:blank")
        self.process = subprocess.Popen(
            arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.endpoint = f"http://127.0.0.1:{port}"
        self._wait_for_devtools()
        self.socket = WebSocket(self._browser_socket(), timeout)

    def _register_messaging_host(self) -> None:
        """Give this throwaway profile the native-messaging host.

        Chromium looks for messaging manifests **inside the user data
        directory**, and the suite runs on a fresh one — so the bridge simply
        was not there, `connectNative` failed, and the panel was blamed for
        waiting on an answer that could never come. The suite exists to test the
        real thing; the real thing includes the bridge.
        """
        import json as _json
        import sys as _sys

        _sys.path.insert(0, str(REPO))
        from ixd import integration                       # noqa: PLC0415

        launcher = self._host_launcher(integration)
        if not launcher:
            return
        identity = (REPO / "extension" / "chrome-extension-id.txt")
        extension_id = identity.read_text().strip() if identity.exists() else ""
        if not extension_id:
            return
        self.bridge_note = f"host: {launcher}"
        hosts = self.profile / "NativeMessagingHosts"
        hosts.mkdir(parents=True, exist_ok=True)
        (hosts / "com.ixd.downloader.json").write_text(_json.dumps({
            "name": "com.ixd.downloader",
            "description": "Internet Xtreme Downloader",
            "path": str(launcher),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{extension_id}/"],
        }, indent=2))

    def _host_launcher(self, integration) -> str:
        """The messaging host this particular browser is able to execute.

        A snap browser may not run anything from a dotted path in `$HOME`
        (context.md §3.12), so the application installs a relay inside the
        snap's own area and registers *that*. A test that registers the plain
        launcher instead hands the browser a path its confinement refuses, and
        the failure — "Specified native messaging host not found" — looks
        exactly like a panel that is broken.
        """
        if "/snap/" in str(self.binary):
            for name in ("chromium", "chrome", "google-chrome"):
                relay = Path.home() / "snap" / name / "common" / "ixd" / "ixd-native-host"
                if relay.exists():
                    return str(relay)
        launcher = integration.launcher_path()
        if launcher and Path(launcher).exists():
            return str(launcher)
        return ""

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def _wait_for_devtools(self) -> None:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.endpoint}/json/version", timeout=2):
                    return
            except Exception:  # noqa: BLE001 - it is simply not up yet
                time.sleep(0.25)
        raise RuntimeError("the browser never opened its debugging port")

    def _browser_socket(self) -> str:
        with urllib.request.urlopen(f"{self.endpoint}/json/version", timeout=5) as reply:
            return json.load(reply)["webSocketDebuggerUrl"]

    # -- protocol ------------------------------------------------------
    def call(self, method: str, params: dict | None = None,
             session: str = "") -> dict:
        self._id += 1
        message: dict = {"id": self._id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.socket.send(json.dumps(message))
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            reply = json.loads(self.socket.receive())
            if reply.get("id") != self._id:
                continue          # an event, or another session's answer
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message')}")
            return reply.get("result", {})
        raise RuntimeError(f"{method}: the browser did not answer")

    def open(self, url: str) -> str:
        """Open ``url`` in a new tab and return its session id."""
        target = self.call("Target.createTarget", {"url": url})["targetId"]
        session = self.call(
            "Target.attachToTarget", {"targetId": target, "flatten": True}
        )["sessionId"]
        self.call("Page.enable", {}, session)
        self.call("Runtime.enable", {}, session)
        return session

    def evaluate(self, session: str, expression: str, awaits: bool = True):
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": awaits,
            "returnByValue": True,
        }, session)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise RuntimeError(detail.get("exception", {}).get("description")
                               or detail.get("text", "evaluation failed"))
        return result.get("result", {}).get("value")

    def wait_for(self, session: str, expression: str, seconds: float = 12.0):
        """Poll ``expression`` until it is truthy, and return the last value."""
        deadline = time.time() + seconds
        last = None
        while time.time() < deadline:
            try:
                last = self.evaluate(session, expression)
            except RuntimeError:
                last = None
            if last:
                return last
            time.sleep(0.25)
        return last

    def close(self) -> None:
        """Ask the browser to quit before resorting to a signal.

        A snap browser cannot be killed by an unconfined process: AppArmor
        refuses the signal, and `terminate()` raises PermissionError rather
        than doing nothing quietly. Every run then left its whole process tree
        and a profile directory in `$HOME` behind, and the suite reported a
        failure that said nothing about the extension.

        `Browser.close` over the DevTools protocol is not a signal — it is the
        browser shutting itself down on request — so confinement has no opinion
        about it. The signals stay as the fallback for a browser that has
        stopped answering, and neither path is allowed to take the profile
        directory down with it.
        """
        try:
            self.call("Browser.close")
        except Exception:  # noqa: BLE001 - it may already be gone
            pass
        try:
            self.socket.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.process.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.process.kill()
                except Exception:  # noqa: BLE001
                    pass
        shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self) -> "Browser":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
