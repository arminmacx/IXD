#!/usr/bin/env python3
"""Standalone Native Messaging relay: browser stdio ↔ the control socket.

This file is deliberately self-contained. It imports nothing but the standard
library, never touches the ``ixd`` package, and is copied verbatim into a
directory the browser can actually execute from.

**Why it exists.** A snap-confined browser runs the native messaging host under
its own AppArmor profile, and that profile grants execute permission to almost
nothing: ``~/snap/<browser>/**`` and any path in ``$HOME`` that does not begin
with a dot. The application's own launcher lives under ``~/.local/share``, so
the browser was refused outright — ``apparmor="DENIED" operation="exec"`` — and
reported only "Native host has exited".

Executing the packaged application from inside that sandbox is possible but
fragile: it depends on where the user installed it, and an installation under
``/opt`` or ``/usr`` is invisible from the sandbox entirely. The snap's runtime
does, however, always provide a Python interpreter. So the host that runs
inside the sandbox is reduced to this: a few dozen lines that read frames from
stdin, forward them over a loopback TCP connection, and write the replies back.

The endpoint (host, port and auth token) is passed in through
``IXD_ENDPOINT`` because the application's data directory is hidden and
therefore unreadable from the sandbox as well.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

#: Browsers reject anything larger than 64 MiB; stay well inside that.
MAX_MESSAGE_BYTES = 32 << 20
LAUNCH_TIMEOUT = 25.0
CONNECT_TIMEOUT = 10.0


# ----------------------------------------------------------------------
# native messaging framing
# ----------------------------------------------------------------------
#: The browser's pipes, by descriptor rather than through `sys.stdin`.
#:
#: A windowed build sets `sys.stdout` and `sys.stdin` to `None` — the
#: descriptors are still connected, only the wrappers are gone. See the same
#: note in `native_host.py`; this file is deliberately standalone and cannot
#: import it.
def _stdio():
    reader = getattr(sys.stdin, "buffer", None)
    writer = getattr(sys.stdout, "buffer", None)
    if reader is None:
        reader = os.fdopen(0, "rb", closefd=False)
    if writer is None:
        writer = os.fdopen(1, "wb", closefd=False)
    return reader, writer


_READER, _WRITER = _stdio()


def read_message():
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


#: Replies are written from several threads, so the framing has to be atomic:
#: two interleaved writes would put one message's length in front of another's
#: body, and the browser would never recover the stream.
_WRITE_LOCK = threading.Lock()


def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    with _WRITE_LOCK:
        _WRITER.write(struct.pack("@I", len(body)))
        _WRITER.write(body)
        _WRITER.flush()


# ----------------------------------------------------------------------
# endpoint
# ----------------------------------------------------------------------
def load_endpoint():
    """Read the control-socket details written by the application."""
    path = os.environ.get("IXD_ENDPOINT", "")
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "port" in data else None


def try_launch(endpoint):
    """Best-effort start of the application, if the sandbox can reach it.

    The endpoint file records the command that starts the desktop application.
    Whether it can be executed depends on where it is installed — a path under
    a dot-directory, or outside ``$HOME`` altogether, is not reachable from a
    confined browser. Failure here is normal and is reported as a plain
    "not running", never as a crash.
    """
    command = (endpoint or {}).get("launch")
    if not command or os.environ.get("IXD_NO_LAUNCH"):
        return False
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return False

    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline:
        if probe(endpoint):
            return True
        time.sleep(0.5)
    return False


def probe(endpoint):
    try:
        with socket.create_connection(
            (endpoint.get("host", "127.0.0.1"), int(endpoint["port"])), 2.0
        ):
            return True
    except OSError:
        return False


# ----------------------------------------------------------------------
# transport
# ----------------------------------------------------------------------
class Connection:
    """One authenticated, newline-delimited JSON session."""

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.sock = socket.create_connection(
            (endpoint.get("host", "127.0.0.1"), int(endpoint["port"])),
            CONNECT_TIMEOUT,
        )
        self.sock.settimeout(120.0)
        self.reader = self.sock.makefile("rb")

    def call(self, command, params, request_id):
        payload = {
            "token": self.endpoint.get("token", ""),
            "id": request_id,
            "command": command,
            "params": params,
        }
        self.sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        line = self.reader.readline()
        if not line:
            raise OSError("the application closed the control connection")
        return json.loads(line.decode("utf-8"))

    def close(self):
        try:
            self.reader.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


#: How many requests may be in flight at once. A browser sends a handful — a
#: popup asking for stats and a list, a panel asking for formats — and this is
#: only a guard against something pathological.
_MAX_IN_FLIGHT = 12


class Pool:
    """Connections to the application, one per request in flight.

    The application serves each connection strictly in order, so a slow command
    on a shared connection delays every request behind it. That is what made
    the popup sit on "connecting" for seconds at a time: hovering a video sends
    an ``extract`` that takes several seconds, and the popup's ``stats`` and
    ``list`` queued behind it on the one connection this relay used to keep.
    """

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self._idle = []
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            while self._idle:
                connection = self._idle.pop()
                return connection
        return Connection(self.endpoint)

    def release(self, connection, reusable):
        if not reusable:
            connection.close()
            return
        with self._lock:
            if len(self._idle) >= _MAX_IN_FLIGHT:
                connection.close()
                return
            self._idle.append(connection)

    def close(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()


def main() -> int:
    endpoint = load_endpoint()
    pool = None
    limit = threading.Semaphore(_MAX_IN_FLIGHT)
    live = []

    def serve(message, request_id, command, params, pool):
        """Answer one request, on a connection of its own."""
        connection = None
        try:
            connection = pool.acquire()
            response = connection.call(command, params, request_id)
            pool.release(connection, True)
        except Exception as exc:  # noqa: BLE001 - reported, and the connection
            if connection is not None:  # is dropped rather than reused
                pool.release(connection, False)
            response = {"ok": False, "id": request_id, "error": str(exc)}
        finally:
            limit.release()
        write_message(response)

    try:
        while True:
            message = read_message()
            if message is None:
                return 0

            request_id = message.get("id")
            command = str(message.get("command", ""))
            params = message.get("params") or {}

            if command == "__malformed__":
                write_message({"ok": False, "id": request_id,
                               "error": "malformed message"})
                continue

            if endpoint is None:
                endpoint = load_endpoint()
            if endpoint is None:
                write_message({
                    "ok": False, "id": request_id,
                    "error": "the download manager has not published its control "
                             "socket — start the application once so the browser "
                             "integration can be set up",
                })
                continue

            if pool is None:
                if not probe(endpoint) and not try_launch(endpoint):
                    # Re-read in case the application restarted on a different
                    # port while this host was idle.
                    refreshed = load_endpoint()
                    if refreshed and refreshed != endpoint:
                        endpoint = refreshed
                    if not probe(endpoint):
                        write_message({
                            "ok": False, "id": request_id,
                            "error": "Internet Xtreme Downloader is not running. "
                                     "Start it and try again.",
                        })
                        continue
                pool = Pool(endpoint)

            # Each request is answered on its own thread and its own
            # connection, so one slow command cannot hold up the rest. Replies
            # carry the request id, which is what lets them come back in a
            # different order than they were asked.
            limit.acquire()
            worker = threading.Thread(
                target=serve, args=(message, request_id, command, params, pool),
                daemon=True,
            )
            live = [thread for thread in live if thread.is_alive()]
            live.append(worker)
            worker.start()
    except (KeyboardInterrupt, BrokenPipeError):
        return 0
    finally:
        for thread in live:
            thread.join(timeout=5.0)
        if pool is not None:
            pool.close()
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
