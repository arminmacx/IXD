"""Test fixtures: a local HTTP origin that behaves like a real CDN.

``http.server`` ignores ``Range`` entirely, so it cannot exercise the chunking
engine.  This handler implements ranges, ETags, Content-MD5, deliberate
mid-stream disconnects and token expiry so the engine's resume, verification
and link-swap paths can all be tested without touching the network.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import re
import threading
import time
from dataclasses import dataclass, field


@dataclass
class OriginState:
    """Mutable knobs the tests flip to provoke specific engine behaviour."""

    payload: bytes = b""
    etag: str = '"v1"'
    support_ranges: bool = True
    send_content_md5: bool = False
    #: Drop the connection after this many bytes of each response (0 = never).
    cut_after: int = 0
    #: Requests carrying a token not in this set get a 403.
    valid_tokens: set[str] = field(default_factory=set)
    #: Answer this many requests with 429 before serving normally.
    rate_limit_times: int = 0
    #: Refuse any range starting at or beyond this offset with a 403, while
    #: still serving the start of the file. Streaming CDNs do this to withhold
    #: full-file access, and it must not be mistaken for an expired link.
    range_cap: int = 0
    request_count: int = 0
    range_requests: int = 0
    #: Total payload bytes actually written back. Lets a test assert that an
    #: operation which claims to only *inspect* a URL did not transfer it.
    bytes_served: int = 0
    #: Content-Type used for the main payload.
    content_type: str = "application/octet-stream"
    #: Extra routes: path -> (body, content-type). Used for HLS playlists,
    #: encryption keys and individual media segments.
    routes: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: OriginState

    def log_message(self, *args: object) -> None:  # silence test output
        pass

    # ------------------------------------------------------------------
    def _token_ok(self) -> bool:
        if not self.state.valid_tokens:
            return True
        match = re.search(r"[?&]token=([^&]+)", self.path)
        return bool(match and match.group(1) in self.state.valid_tokens)

    def _maybe_reject(self) -> bool:
        with self.state.lock:
            self.state.request_count += 1
            if self.state.rate_limit_times > 0:
                self.state.rate_limit_times -= 1
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
        if not self._token_ok():
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        return False

    def _common_headers(self, length: int) -> None:
        self.send_header("Content-Type", self.state.content_type)
        self.send_header("ETag", self.state.etag)
        self.send_header("Last-Modified", "Wed, 21 Oct 2020 07:28:00 GMT")
        if self.state.support_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if self.state.send_content_md5:
            digest = hashlib.md5(self.state.payload).digest()
            self.send_header("Content-MD5", base64.b64encode(digest).decode("ascii"))
        self.send_header("Content-Length", str(length))

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        if self._maybe_reject():
            return
        route = self.path.split("?", 1)[0]
        if route in self.state.routes:
            # Extra routes must answer HEAD with their own type and length, or
            # a caller that probes before fetching sees the wrong content type.
            body, content_type = self.state.routes[route]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return
        self.send_response(200)
        self._common_headers(len(self.state.payload))
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self._maybe_reject():
            return

        route = self.path.split("?", 1)[0]
        if route in self.state.routes:
            body, content_type = self.state.routes[route]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._write_body(body)
            return

        payload = self.state.payload
        range_header = self.headers.get("Range")

        if range_header and self.state.support_ranges:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match:
                with self.state.lock:
                    self.state.range_requests += 1
                start_text, end_text = match.group(1), match.group(2)
                start = int(start_text) if start_text else 0
                end = int(end_text) if end_text else len(payload) - 1
                end = min(end, len(payload) - 1)
                if self.state.range_cap and start >= self.state.range_cap:
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if start > end or start >= len(payload):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{len(payload)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = payload[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self._common_headers(len(body))
                self.end_headers()
                self._write_body(body)
                return

        self.send_response(200)
        self._common_headers(len(payload))
        self.end_headers()
        self._write_body(payload)

    def _write_body(self, body: bytes) -> None:
        with self.state.lock:
            self.state.bytes_served += min(len(body), self.state.cut_after or len(body))
        cut = self.state.cut_after
        if cut and len(body) > cut:
            # Simulate a mid-transfer disconnect: short write, then close.
            try:
                self.wfile.write(body[:cut])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TestOrigin:
    """Context-manager wrapper around a threaded test HTTP server."""

    def __init__(self, payload: bytes) -> None:
        self.state = OriginState(payload=payload)
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def url(self, path: str = "/file.bin") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def __enter__(self) -> "TestOrigin":
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
