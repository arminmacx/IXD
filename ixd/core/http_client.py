"""HTTP/1.1 client built on :class:`~ixd.core.net.SocketFactory`.

Standard-library ``http.client`` handles framing (chunked encoding, keep-alive,
header parsing); we override only the connection step so every request inherits
proxy routing and interface binding.  On top of that this module adds redirect
following, a small cookie jar, gzip/deflate decoding for HTML fetches, and the
``probe`` call the engine uses to plan chunking.
"""

from __future__ import annotations

import email.utils
import gzip
import http.client
import io
import re
import socket
import ssl
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterator

from .errors import HttpError, LinkExpiredError, NetworkError, RateLimitedError
from .net import NetworkProfile, SocketFactory
from .models import ProxyScheme

DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)
MAX_REDIRECTS = 10


class CaseInsensitiveDict(dict):
    """Header mapping that ignores case on lookup."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        super().__init__()
        if data:
            for key, value in data.items():
                self[key] = value

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key.lower())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and super().__contains__(key.lower())

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key.lower(), default)


class CookieJar:
    """Deliberately small: enough to survive redirect chains and CDN handoffs."""

    def __init__(self, initial: str = "") -> None:
        self._cookies: dict[str, dict[str, str]] = {}
        if initial:
            self.load_header(initial, "")

    def load_header(self, cookie_header: str, domain: str) -> None:
        jar = self._cookies.setdefault(domain, {})
        for part in cookie_header.split(";"):
            if "=" in part:
                name, _, value = part.partition("=")
                jar[name.strip()] = value.strip()

    def store(self, set_cookie_values: list[str], domain: str) -> None:
        jar = self._cookies.setdefault(domain, {})
        for raw in set_cookie_values:
            pair = raw.split(";", 1)[0]
            if "=" in pair:
                name, _, value = pair.partition("=")
                jar[name.strip()] = value.strip()

    def header_for(self, domain: str) -> str:
        merged: dict[str, str] = {}
        merged.update(self._cookies.get("", {}))
        for stored_domain, cookies in self._cookies.items():
            if stored_domain and (domain == stored_domain or domain.endswith("." + stored_domain)):
                merged.update(cookies)
        return "; ".join(f"{k}={v}" for k, v in merged.items())


class _ProxiedHTTPConnection(http.client.HTTPConnection):
    """``HTTPConnection`` whose socket comes from our factory."""

    def __init__(self, host: str, port: int, factory: SocketFactory, timeout: float,
                 use_tls: bool, direct_to_proxy: bool = False) -> None:
        super().__init__(host, port, timeout=timeout)
        self._factory = factory
        self._use_tls = use_tls
        self._direct_to_proxy = direct_to_proxy

    def connect(self) -> None:  # noqa: D102 - inherited contract
        if self._direct_to_proxy:
            self.sock = self._factory.connect_proxy_endpoint()
        elif self._use_tls:
            self.sock = self._factory.connect_tls(self.host, self.port)
        else:
            self.sock = self._factory.connect(self.host, self.port)


@dataclass(slots=True)
class RemoteFileInfo:
    """What a probe learned about a target before any bytes are transferred."""

    url: str
    size: int = 0
    supports_ranges: bool = False
    filename: str = ""
    mime: str = ""
    etag: str = ""
    last_modified: str = ""
    digest: str = ""
    status: int = 200
    headers: CaseInsensitiveDict = field(default_factory=CaseInsensitiveDict)


class Response:
    """A live response body plus its metadata."""

    def __init__(self, raw: http.client.HTTPResponse, conn: http.client.HTTPConnection,
                 url: str, decode: bool = False) -> None:
        self.raw = raw
        self._conn = conn
        self.url = url
        self.status = raw.status
        self.reason = raw.reason
        self.headers = CaseInsensitiveDict({k: v for k, v in raw.getheaders()})
        self._closed = False
        self._truncated = False
        self._decompressor = None
        if decode:
            encoding = (self.headers.get("content-encoding") or "").lower()
            if encoding == "gzip":
                self._decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif encoding == "deflate":
                self._decompressor = zlib.decompressobj()

    @property
    def content_length(self) -> int:
        try:
            return int(self.headers.get("content-length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def read(self, amount: int = -1) -> bytes:
        """Read from the body, degrading a truncated response to a short read.

        A server that closes mid-body raises ``IncompleteRead``.  The bytes it
        did send are already valid, so we surface them and then report EOF —
        the engine's chunk loop notices the shortfall and re-requests the
        remainder via ``Range`` instead of discarding good data.
        """
        if self._truncated:
            return b""
        try:
            data = self.raw.read(amount) if amount >= 0 else self.raw.read()
        except http.client.IncompleteRead as exc:
            self._truncated = True
            data = exc.partial or b""
        except (ssl.SSLError, socket.error, OSError) as exc:
            raise NetworkError(f"connection lost while reading {self.url}: {exc}") from exc
        if self._decompressor is not None and data:
            return self._decompressor.decompress(data)
        return data

    def iter_content(self, block_size: int = 65536) -> Iterator[bytes]:
        while True:
            chunk = self.read(block_size)
            if not chunk:
                break
            yield chunk

    def read_all(self, limit: int = 64 << 20) -> bytes:
        buffer = io.BytesIO()
        total = 0
        for chunk in self.iter_content():
            total += len(chunk)
            if total > limit:
                raise HttpError(self.status, "response exceeded the read limit", self.url)
            buffer.write(chunk)
        return buffer.getvalue()

    def text(self, limit: int = 64 << 20) -> str:
        data = self.read_all(limit)
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
        if match:
            charset = match.group(1)
        return data.decode(charset, "replace")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.raw.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass

    def abort(self) -> None:
        """Tear the connection down from another thread.

        `close()` is not enough and this is not obvious: closing a socket does
        **not** interrupt a `recv` that is already in progress — the descriptor
        stays valid for the call that is blocked on it, which goes on waiting
        out the timeout. `shutdown` does interrupt it, because it tears the
        connection down underneath the call.

        This is what makes Pause immediate on a stalled origin. Measured: 27
        seconds with `close()`, under one with this.
        """
        try:
            sock = getattr(self._conn, "sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass                       # already gone, or never connected
        self.close()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class HttpClient:
    """Issues requests through a :class:`NetworkProfile`."""

    def __init__(self, profile: NetworkProfile | None = None,
                 cookies: CookieJar | None = None, referer: str = "",
                 site_headers: dict[str, str] | None = None,
                 site_host: str = "") -> None:
        self.profile = profile or NetworkProfile()
        self.factory = SocketFactory(self.profile)
        self.cookies = cookies or CookieJar()
        self.referer = referer
        self.site_headers = dict(site_headers or {})
        """The headers the browser actually sent for this media.

        A CDN decides by header, and reconstructing what it wants is guesswork:
        `Referer` is a good guess and not always the right one, because a player
        may sign its requests with an `Authorization` or a bespoke `X-…` header
        that nothing could invent. The browser already sent a set that worked,
        so it is replayed rather than reconstructed. This is the mechanism a
        commercial download manager uses, and it is why one succeeds on a site
        where a reconstructed request is refused.
        """
        self.site_host = (site_host or "").lower()
        """Where those headers may be sent.

        They can carry credentials, so they go to the host they were captured
        from and its subdomains — never to a third party a manifest happens to
        point at.
        """
        """The page these requests are being made on behalf of.

        Hotlink protection is the rule rather than the exception on a media
        CDN: a manifest or a segment is served to a request that came from the
        site's own page and refused with **403** to one that arrives from
        nowhere. Cookies are not enough and neither is the user agent — the
        header the CDN reads is `Referer`, and until this existed no request
        the extractor made carried one.
        """

    def clone(self) -> "HttpClient":
        """Another client on the same policy, **sharing the cookie jar**.

        For work that runs on several threads at once. A client reuses its
        connections, so two threads issuing requests through one interleave on
        the same socket; the jar is deliberately shared, because a session
        warmed on one thread is the session the others need to present.
        """
        return HttpClient(self.profile, self.cookies, self.referer,
                          self.site_headers, self.site_host)

    # ------------------------------------------------------------------
    def _build_connection(self, parsed: urllib.parse.ParseResult) -> tuple[
            http.client.HTTPConnection, str, dict[str, str]]:
        """Return ``(connection, request_target, extra_headers)``."""
        use_tls = parsed.scheme == "https"
        host = parsed.hostname or ""
        port = parsed.port or (443 if use_tls else 80)
        target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        extra: dict[str, str] = {}

        proxy = self.profile.proxy_for(host)
        forward_plain = (
            proxy is not None
            and not use_tls
            and proxy.scheme in (ProxyScheme.HTTP, ProxyScheme.HTTPS)
        )
        if forward_plain:
            # Absolute-form request straight to the proxy.
            connection = _ProxiedHTTPConnection(
                host, port, self.factory, self.profile.timeout, use_tls=False,
                direct_to_proxy=True,
            )
            target = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
            )
            extra.update(self.factory.proxy_auth_header())
        else:
            connection = _ProxiedHTTPConnection(
                host, port, self.factory, self.profile.timeout, use_tls=use_tls,
            )
        return connection, target, extra

    def _default_headers(self, parsed: urllib.parse.ParseResult,
                         headers: dict[str, str] | None) -> dict[str, str]:
        # Built in order of authority, weakest first, because every layer here
        # is a better answer than the one below it: our defaults are a guess,
        # what the browser actually sent is the truth, and what the caller names
        # is a deliberate choice. `CaseInsensitiveDict.setdefault` is *not*
        # used — `dict.setdefault` bypasses `__setitem__` and `__contains__`, so
        # it would file "Referer" and "referer" as two separate headers.
        merged = CaseInsensitiveDict({
            "User-Agent": self.profile.user_agent,
            "Accept": DEFAULT_ACCEPT,
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close",
        })
        if self.site_headers and self._same_site(parsed.hostname or ""):
            for key, value in self.site_headers.items():
                # The agent is already the browser's, set on the profile from
                # the same source; a second copy can only disagree with it.
                if key.lower() == "user-agent":
                    continue
                merged[key] = value
        for key, value in (headers or {}).items():
            merged[key] = value
        if "cookie" not in merged:
            cookie = self.cookies.header_for(parsed.hostname or "")
            if cookie:
                merged["Cookie"] = cookie
        # Only reached when the browser's own value is not available for this
        # host — a segment served from somewhere the capture did not cover.
        if self.referer and "referer" not in merged:
            referer, origin = self._referer_for(parsed)
            if referer:
                merged["Referer"] = referer
            if origin and "origin" not in merged:
                merged["Origin"] = origin
        return {k: v for k, v in merged.items() if v is not None}

    def _same_site(self, host: str) -> bool:
        """Whether ``host`` is the one the captured headers belong to."""
        host = (host or "").lower()
        if not self.site_host or not host:
            return False
        return host == self.site_host or host.endswith("." + self.site_host)

    def _referer_for(self, parsed: urllib.parse.ParseResult) -> tuple[str, str]:
        """``(Referer, Origin)`` as a browser on ``self.referer`` would send them.

        Emulated rather than invented, because a CDN that checks these compares
        them against what its own player sends. The default policy is
        ``strict-origin-when-cross-origin``: the whole address within one
        origin, the bare origin to another, and nothing at all when stepping
        down from https to http. ``Origin`` accompanies a cross-origin fetch,
        which is what a player's manifest and segment requests are.
        """
        source = urllib.parse.urlparse(self.referer)
        if source.scheme not in ("http", "https") or not source.netloc:
            return "", ""
        if source.scheme == "https" and parsed.scheme == "http":
            return "", ""
        origin = f"{source.scheme}://{source.netloc}"
        if source.scheme == parsed.scheme and source.netloc == parsed.netloc:
            return urllib.parse.urlunsplit(
                (source.scheme, source.netloc, source.path, source.query, "")
            ), ""
        return f"{origin}/", origin

    def request(self, method: str, url: str, headers: dict[str, str] | None = None,
                body: bytes | str | None = None, *, decode: bool = False,
                follow_redirects: bool = True, max_redirects: int = MAX_REDIRECTS,
                had_prior_success: bool = False) -> Response:
        """Perform a request, following redirects, and return an open response."""
        current = url
        seen: set[str] = set()

        for _hop in range(max_redirects + 1):
            parsed = urllib.parse.urlparse(current)
            if parsed.scheme not in ("http", "https"):
                raise NetworkError(f"unsupported URL scheme: {parsed.scheme!r}")

            connection, target, extra = self._build_connection(parsed)
            request_headers = self._default_headers(parsed, headers)
            request_headers.update(extra)
            if decode and "accept-encoding" not in CaseInsensitiveDict(request_headers):
                request_headers["Accept-Encoding"] = "gzip, deflate"
            elif not decode:
                request_headers.setdefault("Accept-Encoding", "identity")

            payload = body.encode("utf-8") if isinstance(body, str) else body
            try:
                connection.request(method, target, body=payload, headers=request_headers)
                raw = connection.getresponse()
            except (http.client.HTTPException, ssl.SSLError, socket.error, OSError) as exc:
                connection.close()
                raise NetworkError(f"{method} {current} failed: {exc}") from exc

            response = Response(raw, connection, current, decode=decode)

            set_cookies = raw.msg.get_all("Set-Cookie") or []
            if set_cookies:
                self.cookies.store(set_cookies, parsed.hostname or "")

            if follow_redirects and response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise HttpError(response.status, "redirect without a Location", current)
                current = urllib.parse.urljoin(current, location)
                if current in seen:
                    raise HttpError(response.status, "redirect loop detected", current)
                seen.add(current)
                if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                continue

            self._raise_for_status(response, had_prior_success)
            return response

        raise HttpError(310, "too many redirects", current)

    @staticmethod
    def _raise_for_status(response: Response, had_prior_success: bool) -> None:
        status = response.status
        if status < 400:
            return
        url = response.url
        response.close()
        if status in (403, 410):
            # A 403 on a link that already served bytes means an expired token,
            # not a permission problem — that distinction drives link swapping.
            if had_prior_success:
                raise LinkExpiredError(status, "source link expired or was revoked", url)
            raise HttpError(status, f"HTTP {status} Forbidden", url)
        if status in (429, 503):
            raise RateLimitedError(status, f"HTTP {status} rate limited", url)
        raise HttpError(status, f"HTTP {status} {response.reason}", url)

    # ------------------------------------------------------------------
    def get(self, url: str, headers: dict[str, str] | None = None, **kwargs: Any) -> Response:
        return self.request("GET", url, headers, **kwargs)

    def post(self, url: str, body: bytes | str, headers: dict[str, str] | None = None,
             **kwargs: Any) -> Response:
        return self.request("POST", url, headers, body=body, **kwargs)

    def get_text(self, url: str, headers: dict[str, str] | None = None,
                 limit: int = 8 << 20, **kwargs: Any) -> str:
        """Fetch a document as text.

        ``limit`` is a hard ceiling on how much is pulled into memory. It
        matters because this is also how page scrapers read a URL, and a page
        scraper pointed at a large binary would otherwise download it.
        """
        with self.request("GET", url, headers, decode=True, **kwargs) as response:
            return response.text(limit)

    def get_bytes(self, url: str, headers: dict[str, str] | None = None, **kwargs: Any) -> bytes:
        with self.request("GET", url, headers, **kwargs) as response:
            return response.read_all()

    def open_range(self, url: str, start: int, end: int | None = None,
                   headers: dict[str, str] | None = None,
                   had_prior_success: bool = False) -> Response:
        """Open a byte-range stream. ``end`` is inclusive; ``None`` = open-ended."""
        merged = dict(headers or {})
        merged["Range"] = f"bytes={start}-{end if end is not None else ''}"
        return self.request("GET", url, merged, had_prior_success=had_prior_success)

    # ------------------------------------------------------------------
    def probe(self, url: str, headers: dict[str, str] | None = None) -> RemoteFileInfo:
        """Discover size, range support and naming metadata for ``url``.

        HEAD is tried first; many CDNs answer it poorly, so a ranged GET is the
        authoritative fallback — a ``206`` proves range support outright.
        """
        info = RemoteFileInfo(url=url)

        try:
            with self.request("HEAD", url, headers) as response:
                info = self._info_from_response(response)
                if info.size > 0 and info.supports_ranges:
                    return info
        except (HttpError, NetworkError):
            pass  # fall through to the ranged GET

        probe_headers = dict(headers or {})
        probe_headers["Range"] = "bytes=0-0"
        with self.request("GET", url, probe_headers) as response:
            ranged = self._info_from_response(response)

        if ranged.status == 206:
            ranged.supports_ranges = True
            content_range = ranged.headers.get("content-range", "")
            match = re.search(r"/\s*(\d+)\s*$", content_range or "")
            if match:
                ranged.size = int(match.group(1))
        elif ranged.status == 200:
            # Server ignored the Range header entirely.
            ranged.supports_ranges = False

        if info.size and not ranged.size:
            ranged.size = info.size
        if info.filename and not ranged.filename:
            ranged.filename = info.filename
        return ranged

    def _info_from_response(self, response: Response) -> RemoteFileInfo:
        headers = response.headers
        size = 0
        content_range = headers.get("content-range", "")
        if content_range:
            match = re.search(r"/\s*(\d+)\s*$", content_range)
            if match:
                size = int(match.group(1))
        if not size and response.status != 206:
            try:
                size = int(headers.get("content-length", 0) or 0)
            except (TypeError, ValueError):
                size = 0

        accept_ranges = (headers.get("accept-ranges", "") or "").lower()
        # Keep the header name attached: the verifier needs to know whether the
        # value is a bare base64 MD5 or an algorithm-tagged RFC 3230 digest.
        digest = ""
        for header_name in ("content-md5", "digest", "repr-digest"):
            value = headers.get(header_name)
            if value:
                digest = f"{header_name}: {value}"
                break

        return RemoteFileInfo(
            url=response.url,
            size=size,
            supports_ranges=(accept_ranges == "bytes" or response.status == 206),
            filename=filename_from_response(response),
            mime=(headers.get("content-type", "") or "").split(";")[0].strip(),
            etag=headers.get("etag", "") or "",
            last_modified=headers.get("last-modified", "") or "",
            digest=digest,
            status=response.status,
            headers=headers,
        )


# ----------------------------------------------------------------------
# naming helpers
# ----------------------------------------------------------------------
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, fallback: str = "download") -> str:
    """Make a filename safe on every supported platform."""
    name = urllib.parse.unquote(name or "").strip().replace("\n", " ")
    name = _UNSAFE_CHARS.sub("_", name)
    name = name.strip(". ")
    if not name:
        name = fallback
    # Windows refuses these names regardless of extension.
    stem = name.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}:
        name = f"_{name}"
    return name[:200]


def parse_content_disposition(value: str) -> str:
    """Extract a filename from a Content-Disposition header (RFC 6266)."""
    if not value:
        return ""
    match = re.search(r"filename\*\s*=\s*([^;]+)", value, re.I)
    if match:
        token = match.group(1).strip().strip('"')
        parts = token.split("'", 2)
        if len(parts) == 3:
            charset, _lang, encoded = parts
            try:
                return urllib.parse.unquote(encoded, encoding=charset or "utf-8")
            except (LookupError, ValueError):
                return urllib.parse.unquote(encoded)
        return urllib.parse.unquote(token)
    match = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if match:
        return match.group(1)
    match = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    if match:
        return match.group(1).strip()
    return ""


def filename_from_url(url: str, mime: str = "") -> str:
    parsed = urllib.parse.urlparse(url)
    candidate = urllib.parse.unquote((parsed.path or "").rstrip("/").split("/")[-1])
    if not candidate or "." not in candidate:
        base = candidate or (parsed.hostname or "download").replace(".", "_")
        extension = _extension_for_mime(mime)
        candidate = f"{base}{extension}"
    return sanitize_filename(candidate)


def filename_from_response(response: Response) -> str:
    disposition = response.headers.get("content-disposition", "")
    name = parse_content_disposition(disposition)
    if name:
        return sanitize_filename(name)
    return ""


_MIME_EXTENSIONS = {
    "video/mp4": ".mp4", "video/webm": ".webm", "video/x-matroska": ".mkv",
    "video/quicktime": ".mov", "video/mp2t": ".ts",
    "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/webm": ".webm",
    "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/wav": ".wav",
    "application/pdf": ".pdf", "application/zip": ".zip",
    "application/x-tar": ".tar", "application/gzip": ".gz",
    "application/x-7z-compressed": ".7z", "application/vnd.rar": ".rar",
    "application/octet-stream": ".bin",
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "image/svg+xml": ".svg",
    "text/plain": ".txt", "text/html": ".html", "application/json": ".json",
}


def _extension_for_mime(mime: str) -> str:
    return _MIME_EXTENSIONS.get((mime or "").lower().split(";")[0].strip(), "")


def http_date_to_timestamp(value: str) -> float:
    """Parse an HTTP date header into a POSIX timestamp (0 on failure)."""
    if not value:
        return 0.0
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed.timestamp() if parsed else 0.0


def format_bytes(count: float) -> str:
    """Human-readable byte size used across the UI and logs."""
    if count < 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    value = float(count)
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.2f} {units[index]}"


def format_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "—"
    return f"{format_bytes(bytes_per_second)}/s"


def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def unique_path(directory: str, filename: str) -> str:
    """Avoid clobbering: ``file.zip`` → ``file (1).zip``."""
    import os

    candidate = os.path.join(directory, filename)
    if not os.path.exists(candidate):
        return candidate
    stem, extension = os.path.splitext(filename)
    index = 1
    while True:
        candidate = os.path.join(directory, f"{stem} ({index}){extension}")
        if not os.path.exists(candidate):
            return candidate
        index += 1
