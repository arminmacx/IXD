"""Chrome/Firefox Native Messaging host.

The browser launches this process and speaks the standard framing protocol on
stdio: a 32-bit native-endian length prefix followed by a UTF-8 JSON payload.
Each message is relayed to the running application over the local control
socket and the reply is written back.

If the desktop application is not running, the host starts it (detached) and
waits briefly for the control socket to appear, so clicking a download in the
browser works even from a cold start.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Allow execution both as a module and as a bare script registered with the browser.
if __package__ in (None, ""):  # pragma: no cover - script invocation
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ixd.ipc.server import IPCClient, is_running, read_endpoint

#: Browsers reject anything larger than 64 MiB; stay well inside that.
MAX_MESSAGE_BYTES = 32 << 20
LAUNCH_TIMEOUT = 25.0


#: The pipes the browser handed us, taken from the file descriptors rather than
#: from `sys.stdin`/`sys.stdout`.
#:
#: A PyInstaller **windowed** build — which is what the application is on
#: Windows, because a download manager with a console window behind it is not
#: something anyone wants — sets `sys.stdout` and `sys.stdin` to `None`. The
#: descriptors are still there and still connected to the browser's pipes; only
#: the convenience wrappers are missing. Reading through `sys.stdin.buffer`
#: therefore raised `AttributeError` on the first message and the browser saw
#: the host exit immediately.
def _stdio() -> tuple[Any, Any]:
    reader = getattr(sys.stdin, "buffer", None)
    writer = getattr(sys.stdout, "buffer", None)
    if reader is None:
        reader = os.fdopen(0, "rb", closefd=False)
    if writer is None:
        writer = os.fdopen(1, "wb", closefd=False)
    return reader, writer


_READER, _WRITER = _stdio()


def read_message() -> dict[str, Any] | None:
    """Read one length-prefixed message from stdin."""
    header = _READER.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("@I", header)
    if length == 0 or length > MAX_MESSAGE_BYTES:
        return None
    body = _READER.read(length)
    if len(body) < length:
        return None
    try:
        message = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"command": "__malformed__", "params": {}}
    return message if isinstance(message, dict) else {"command": "__malformed__"}


def write_message(payload: dict[str, Any]) -> None:
    """Write one length-prefixed message to stdout."""
    body = json.dumps(payload).encode("utf-8")
    _WRITER.write(struct.pack("@I", len(body)))
    _WRITER.write(body)
    _WRITER.flush()


#: The commands worth starting the application for: ones where the user has
#: asked for something to happen. Everything else — status polls, the badge,
#: a page announcing what it found — is answered "not running" and the browser
#: carries on without it.
#:
#: A quit application that comes back on its own is not a running application,
#: it is one that cannot be quit. The extension polled `stats` every 1.5
#: seconds, so ending the process merely postponed it.
STARTS_THE_APPLICATION = frozenset({
    "add", "add_media", "add_pair", "add_many", "present", "download",
    "queue_quality", "focus", "swap_link", "resume", "resume_all",
    "pause", "remove", "browser_stream_begin", "browser_stream_chunk",
    "browser_stream_end",
})

#: `extract` is deliberately **not** in that set. The panel prefetches a page's
#: qualities speculatively when it loads, so listing it would start the
#: application on every video page merely opened. A request carrying
#: `user_initiated` starts it instead, which is what the panel sends when
#: somebody actually clicks.


def launch_application() -> bool:
    """Start the desktop app detached and wait for its control socket."""
    command = _application_command()
    if not command:
        return False

    creation_flags = 0
    start_info = None
    if sys.platform.startswith("win"):
        creation_flags = 0x00000008 | 0x08000000   # DETACHED_PROCESS | NO_WINDOW
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=not sys.platform.startswith("win"),
            creationflags=creation_flags,
            startupinfo=start_info,
        )
    except (OSError, ValueError):
        return False

    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(0.4)
    return False


#: Started hidden, not headless.
#:
#: `--background` runs with no Qt at all, so the instance that owns the engine
#: has no window and no tray — and once one instance properly owns the control
#: socket, a later launch of the application could only be told "already
#: running" and have nowhere to show itself. The browser starting a download
#: would have locked the user out of their own interface.
#:
#: `--hidden` is a normal run with the window not shown: the tray is there, the
#: window opens on request, and a new download can raise its own window. A
#: machine with no display falls back to headless on its own, because run_gui
#: raises ImportError and main catches it.
_START_HIDDEN = "--hidden"


def _application_command() -> list[str]:
    """Locate the installed application binary, or fall back to this source tree."""
    override = os.environ.get("IXD_EXECUTABLE")
    if override and Path(override).exists():
        return [override, _START_HIDDEN]

    root = Path(__file__).resolve().parents[2]
    for candidate in (
        root / "dist" / "ixd",
        root / "dist" / "ixd.exe",
        root / "dist" / "Internet Xtreme Downloader.app" / "Contents" / "MacOS" / "ixd",
    ):
        if candidate.exists():
            return [str(candidate), _START_HIDDEN]

    if (root / "ixd" / "__main__.py").exists():
        return [sys.executable, "-m", "ixd", _START_HIDDEN]
    return []


def main() -> int:
    client: IPCClient | None = None
    try:
        while True:
            message = read_message()
            if message is None:
                return 0

            command = str(message.get("command", ""))
            params = message.get("params") or {}
            request_id = message.get("id")

            if command == "__malformed__":
                write_message({"ok": False, "error": "malformed message"})
                continue

            try:
                if client is None:
                    if not is_running():
                        # Only work the user asked for starts the application.
                        # The extension polls for status on a timer, and any
                        # command starting it meant a quit application came
                        # back within seconds — "even end task does not work,
                        # it runs again", which is exactly what happened.
                        wanted = (command in STARTS_THE_APPLICATION
                                  or bool(params.get("user_initiated")))
                        if not wanted:
                            write_message({
                                "ok": False, "id": request_id,
                                "error": "not running",
                                "not_running": True,
                            })
                            continue
                        if not launch_application():
                            write_message({
                                "ok": False, "id": request_id,
                                "error": "Internet Xtreme Downloader is not running "
                                         "and could not be started",
                            })
                            continue
                    endpoint = read_endpoint() or {}
                    client = IPCClient(
                        host=endpoint.get("host", "127.0.0.1"),
                        port=int(endpoint.get("port", 47615)),
                        token=endpoint.get("token", ""),
                    )
                response = client.call(command, params, request_id)
            except Exception as exc:  # noqa: BLE001 - report and retry next message
                if client is not None:
                    client.close()
                client = None
                response = {"ok": False, "id": request_id, "error": str(exc)}

            write_message(response)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
