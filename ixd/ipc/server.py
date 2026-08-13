"""Local control socket.

A line-delimited JSON server bound to the loopback interface.  It is the single
entry point for anything outside the GUI process:

* the Native Messaging host relays browser requests here,
* a second launch of the application uses it to focus the running instance
  instead of starting a duplicate.

Every connection must authenticate with the token stored in ``settings.json``,
which is generated on first run and readable only by the user account.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import config

if TYPE_CHECKING:  # pragma: no cover
    from ..service import DownloadService

MAX_MESSAGE_BYTES = 8 << 20


class _Handler(socketserver.StreamRequestHandler):
    """One connection: authenticate once, then serve commands until closed."""

    server: "IPCServer"

    def handle(self) -> None:
        authenticated = False
        while True:
            try:
                line = self.rfile.readline(MAX_MESSAGE_BYTES)
            except (OSError, ValueError):
                return
            if not line:
                return

            try:
                message = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._reply({"ok": False, "error": "malformed JSON"})
                continue
            if not isinstance(message, dict):
                self._reply({"ok": False, "error": "expected a JSON object"})
                continue

            if not authenticated:
                if message.get("token") != self.server.token:
                    self._reply({"ok": False, "error": "authentication failed"})
                    return
                authenticated = True

            command = str(message.get("command", ""))
            params = message.get("params") or {}
            if not isinstance(params, dict):
                self._reply({"ok": False, "error": "params must be an object"})
                continue

            response = self.server.dispatch(command, params)
            if message.get("id") is not None:
                response["id"] = message["id"]
            self._reply(response)

    def _reply(self, payload: dict[str, Any]) -> None:
        try:
            self.wfile.write((json.dumps(payload) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (OSError, ValueError):
            pass


class IPCServer(socketserver.ThreadingTCPServer):
    """Threaded loopback JSON server in front of :class:`DownloadService`.

    Binding this port *is* the single-instance lock: one process owns the
    control socket and therefore the engine, and everything else hands its work
    to it. That only holds if a second bind fails.

    ``SO_REUSEADDR`` means two different things. On Unix it permits a bind over
    a socket left in ``TIME_WAIT`` — a dead predecessor — and still refuses a
    live listener. On **Windows it permits two live listeners on the same
    address**, which is Unix's ``SO_REUSEPORT``, and connections go to
    whichever bound last. So on Windows the lock silently did not lock: a
    second instance bound the same port, started its own engine on the same
    database and overwrote the endpoint file.

    Every Windows symptom of 2026-08-13 follows from that one line. A download
    added by the browser went to whichever instance the endpoint file last
    named, so the other one — the one with the window — showed the row with no
    transfer behind it: no download window, and no speed until a pause and
    resume moved the transfer into *its* engine. Pause marked the row paused
    and stopped a task that process did not have, while the other kept
    fetching. Closing the window left the other running.
    """

    #: False on Windows, where sharing the address is exactly what must not
    #: happen. Left true elsewhere so a restart is not blocked by TIME_WAIT.
    allow_reuse_address = not sys.platform.startswith("win")
    daemon_threads = True

    def server_bind(self) -> None:
        # Windows can be asked for the stronger guarantee: refuse this address
        # to anyone else for as long as it is held, whatever they request.
        if sys.platform.startswith("win"):
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                except OSError:
                    pass       # an older Windows: the reuse flag above still holds
        super().server_bind()

    def __init__(self, service: "DownloadService", host: str = "",
                 port: int | None = None) -> None:
        self.service = service
        self.token = service.settings.get("ipc_token", "")
        self._extra_handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        host = host or service.settings.get("ipc_host", "127.0.0.1")
        # ``0`` is a request for an ephemeral port, not an absent value — the
        # distinction matters, because treating it as absent silently binds the
        # configured port instead and collides with a running instance.
        if port is None:
            port = service.settings.get_int("ipc_port", 47615)
        super().__init__((host, port), _Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def register(self, command: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        """Add a command the service itself does not implement (e.g. `focus`)."""
        self._extra_handlers[command] = handler

    def dispatch(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        handler = self._extra_handlers.get(command)
        if handler is not None:
            try:
                return {"ok": True, "result": handler(params)}
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
        return self.service.handle_command(command, params)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.serve_forever, name="ixd-ipc", daemon=True
        )
        self._thread.start()
        write_endpoint(self.port, self.token)

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        clear_endpoint()


# ----------------------------------------------------------------------
# endpoint discovery — how the native host finds a running instance
# ----------------------------------------------------------------------
def write_endpoint(port: int, token: str) -> None:
    config.ensure_dirs()
    payload: dict[str, Any] = {"host": "127.0.0.1", "port": port, "token": token}

    launch = _launch_command()
    if launch:
        payload["launch"] = launch

    path = Path(config.IPC_PORT_FILE)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)
    try:
        path.chmod(0o600)      # the token is a credential
    except OSError:
        pass

    _publish_endpoint(payload)


def _launch_command() -> list[str]:
    """How to start this application again, for a host that finds it stopped."""
    import sys      # noqa: PLC0415 - only needed here

    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    root = Path(__file__).resolve().parents[2]
    if (root / "ixd" / "__main__.py").exists():
        return [sys.executable, "-m", "ixd", "--background"]
    return []


def _publish_endpoint(payload: dict[str, Any]) -> None:
    """Mirror the endpoint anywhere a sandboxed browser can read it.

    The application's data directory is hidden, and a snap-confined browser is
    denied every dotted path in ``$HOME`` — so the copy the relay reads has to
    live inside that browser's own snap area.
    """
    try:
        from .. import integration      # noqa: PLC0415 - avoids an import cycle

        integration.publish_endpoint(payload)
    except Exception:  # noqa: BLE001 - mirroring is never worth a crash
        pass


def read_endpoint() -> dict[str, Any] | None:
    # A relay running inside a browser sandbox is handed its own copy, because
    # the real one is unreadable from there.
    override = os.environ.get("IXD_ENDPOINT", "")
    path = Path(override) if override else Path(config.IPC_PORT_FILE)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "port" not in data:
        return None
    return data


def clear_endpoint() -> None:
    try:
        Path(config.IPC_PORT_FILE).unlink()
    except OSError:
        pass
    try:
        from .. import integration      # noqa: PLC0415 - avoids an import cycle

        integration.clear_published_endpoints()
    except Exception:  # noqa: BLE001
        pass


class IPCClient:
    """Minimal client used by the native host and the single-instance check."""

    def __init__(self, host: str = "", port: int = 0, token: str = "",
                 timeout: float = 15.0) -> None:
        endpoint = read_endpoint() or {}
        self.host = host or endpoint.get("host", "127.0.0.1")
        self.port = port or int(endpoint.get("port", 47615))
        self.token = token or endpoint.get("token", "")
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._file: Any = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        if self._socket is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._socket = sock
        self._file = sock.makefile("rwb")

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
            if self._socket is not None:
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None

    def call(self, command: str, params: dict[str, Any] | None = None,
             request_id: Any = None) -> dict[str, Any]:
        with self._lock:
            self.connect()
            assert self._file is not None
            message = {
                "token": self.token,
                "command": command,
                "params": params or {},
            }
            if request_id is not None:
                message["id"] = request_id
            self._file.write((json.dumps(message) + "\n").encode("utf-8"))
            self._file.flush()
            line = self._file.readline()
        if not line:
            raise ConnectionError("the download manager closed the connection")
        return json.loads(line.decode("utf-8"))

    def __enter__(self) -> "IPCClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def is_running() -> bool:
    """True when another instance is already serving the control socket."""
    try:
        with IPCClient(timeout=2.0) as client:
            response = client.call("ping")
        return bool(response.get("ok"))
    except Exception:  # noqa: BLE001 - any failure means "not running"
        return False
