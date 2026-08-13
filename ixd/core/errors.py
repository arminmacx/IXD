"""Exception hierarchy for the transfer engine."""

from __future__ import annotations


class IXDError(Exception):
    """Base class for every error raised by the download engine."""


class NetworkError(IXDError):
    """Transport-level failure (DNS, TCP, TLS, timeouts)."""


class ProxyError(NetworkError):
    """The proxy itself refused, failed to authenticate, or misbehaved."""

    def __init__(self, message: str, proxy_id: int | None = None) -> None:
        super().__init__(message)
        self.proxy_id = proxy_id


class HttpError(IXDError):
    """Server answered with an unusable status code."""

    def __init__(self, status: int, message: str = "", url: str = "") -> None:
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.url = url


class LinkExpiredError(HttpError):
    """A 403/410 (or token-expiry redirect) on a URL that previously worked.

    The engine converts this into the ``needs_link`` state so the UI can ask
    for a refreshed source and resume the surviving chunks.
    """


class RateLimitedError(HttpError):
    """429 / 503 — a good reason to rotate to the next proxy."""


class RangeCappedError(HttpError):
    """The origin serves only an opening portion of the file.

    Distinct from an expired link: the URL is valid and offset 0 is served
    normally, but every offset beyond a fixed point is refused — and a freshly
    issued URL behaves identically, so retrying or swapping in a new link
    cannot help. Streaming CDNs use this to withhold full-file access from
    clients they have not attested, and the only useful response is to tell the
    user why, rather than retry into the same wall.
    """


class RangeNotSupportedError(IXDError):
    """The origin dropped Range support mid-transfer; restart linearly."""


class ContentChangedError(IXDError):
    """ETag / Last-Modified / length no longer match the resumed state."""


class ExtractionError(IXDError):
    """A media extractor could not produce a playable stream."""


class ChecksumMismatchError(IXDError):
    def __init__(self, algorithm: str, expected: str, actual: str) -> None:
        super().__init__(f"{algorithm} mismatch: expected {expected}, got {actual}")
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual


class CancelledError(IXDError):
    """Raised inside workers when a download is paused or cancelled."""
