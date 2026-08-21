"""The transfer engine.

A :class:`DownloadTask` owns one transfer and runs a pool of worker threads
over a *dynamic* chunk map.  Workers that finish early steal the unfetched tail
of whichever chunk still has the most work left, which keeps every connection
busy until the very end of the file instead of leaving one straggler.

The same worker loop drives two unit systems:

* ``RANGED`` / ``SINGLE`` — a chunk is a byte range served by HTTP ``Range``.
* ``SEGMENTED`` — a chunk is a *band of HLS/DASH segments*; the band's
  ``downloaded`` counter is a segment count.

Reusing one loop for both means resume, work stealing, pausing, rate limiting
and proxy rotation are implemented exactly once.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import shutil
import threading
import time
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import config
from ..config import Settings
from .crypto import aes_cbc_decrypt, parse_hex_iv
from .db import Database
from .errors import (
    CancelledError,
    ContentChangedError,
    ExtractionError,
    HttpError,
    LinkExpiredError,
    NetworkError,
    RangeCappedError,
    IXDError,
)
from .events import EventBus, EventType
from .hashing import verify_file
from .http_client import (
    CookieJar,
    HttpClient,
    filename_from_url,
    sanitize_filename,
    unique_path,
)
from .models import (
    Chunk,
    ChunkStatus,
    Download,
    DownloadStatus,
    HashStatus,
    MediaSegment,
    QueueMode,
    TransferMode,
)
from .ratelimit import CompositeLimiter, SpeedMeter, TokenBucket
from .routing import ProxyManager, should_rotate

if TYPE_CHECKING:  # pragma: no cover
    from .net import NetworkProfile

PART_SUFFIX = ".ixddl"

#: Ceiling on how many replacement links one transfer may ask for.
_MAX_LINK_RENEWALS = 60

#: Largest span asked for in a single range request. Some origins refuse a
#: range wider than the grant attached to the link, so a chunk is fetched in
#: bounded windows rather than demanded whole — which also keeps a dropped
#: connection from costing more than one window.
_MAX_RANGE_SPAN = 4 << 20

#: Failures worth another attempt (possibly on a different proxy).
RETRYABLE_ERRORS = (HttpError, NetworkError, OSError, http.client.HTTPException)

#: How often a server-driven transfer records the ranges it holds. Often
#: enough that an interruption costs seconds of re-fetching, rarely enough
#: that it does not write to the database on every block.
_COVERAGE_SAVE_SECONDS = 5.0

#: Whether a server-driven track may be fetched over several sessions at once.
#:
#: On, and only because the guess it used to rest on is gone. Sessions are
#: positioned by *time*; a byte offset used to be converted by estimating from
#: the stream's length, which is right only at constant bitrate and was
#: measured 2.3 seconds of playback out on a real video — so a worker landed
#: megabytes from its stretch and the difference was a hole nothing could seek
#: back into. A 327 MB download stopped at 88%.
#:
#: The stream publishes the answer itself, in the ``sidx`` of the header that
#: is fetched before any media: exact byte offsets and times for every segment.
#: A track that publishes no index is still fetched on one session, because
#: without one nothing has changed.
_SABR_PARALLEL_ENABLED = True

#: How far before a wanted byte to ask, the first time a session overshoots it,
#: and the furthest back it is worth reaching. A position is a time and a byte
#: is only an estimate of one, so an overshoot is ordinary; it is never large.
_SEEK_BIAS_START = 2 << 20
_SEEK_BIAS_LIMIT = 64 << 20

#: Never give a streaming worker less than this to fetch. Opening a session
#: costs a round trip and an allowance; below this the split costs more than
#: the concurrency wins back.
#: Smallest stretch worth opening a streaming session for, when the
#: `min_chunk_size` setting says nothing.
#:
#: Was four megabytes, which capped the sessions at one per 4 MiB regardless of
#: the configured count: a 62 MB stream ran on fifteen sessions and a 480p one
#: on a single session, reported from the field as "only 1080p respects
#: connections per download". It matches `min_chunk_size` now — the floor the
#: ordinary chunker already keeps.
_MIN_SABR_SPAN = 1 << 20


#: The streaming protocol's error part. Its text is how the endpoint says a
#: request has the wrong shape — `sabr.malformed` for an ordinary GET — as
#: opposed to refusing the bytes it asks for.
_PART_SABR_ERROR = 44


def _index_in(header: bytes) -> list[tuple[int, int, int]]:
    """A stream's segment index, whichever container it is written in.

    ISOBMFF keeps it in a `sidx`; Matroska keeps it in `Cues`. Only the first
    was ever read, so every WebM stream reported "publishes no segment index"
    and ran on a single connection however many were configured — the format
    the panel offers under "webm", and the field report that prompted this.

    An index is a bonus in both cases: an unreadable header yields an empty
    list and the transfer proceeds on one session, as it did before.
    """
    if not header:
        return []
    from .mp4 import parse_sidx               # noqa: PLC0415
    try:
        index = parse_sidx(header)
    except Exception:                         # noqa: BLE001
        index = []
    if index:
        return index
    from .webm import parse_cues              # noqa: PLC0415
    return parse_cues(header)


def _with_query(url: str, key: str, value: str) -> str:
    """``url`` with ``key`` set to ``value`` — replaced, never duplicated.

    Appending was the first version, and it is how a request came to be
    refused for its own malformedness: a ``videoplayback`` address names
    ``itag`` in its ``sparams``, so a second copy of a signed parameter can
    invalidate the signature. The 403 that came back then said nothing about
    whether the origin would have served the bytes.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    query = [(name, existing)
             for name, existing in urllib.parse.parse_qsl(
                 parts.query, keep_blank_values=True)
             if name != key]
    query.append((key, value))
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query), parts.fragment,
    ))


def _normalise_ranges(ranges: Any) -> list[tuple[int, int]]:
    """Sort and merge ``[start, end)`` spans, dropping empty ones."""
    spans: list[tuple[int, int]] = []
    for span in ranges or []:
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        if end > start:
            spans.append((start, end))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _missing_ranges(held: list[tuple[int, int]], size: int) -> list[tuple[int, int]]:
    """What of ``0..size`` the merged spans in ``held`` do not cover."""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in _normalise_ranges(held):
        if start > cursor:
            gaps.append((cursor, min(start, size)))
        cursor = max(cursor, end)
        if cursor >= size:
            break
    if cursor < size:
        gaps.append((cursor, size))
    return [(a, b) for a, b in gaps if b > a]


def _split_ranges(ranges: list[tuple[int, int]], parts: int,
                  minimum: int = _MIN_SABR_SPAN) -> list[tuple[int, int]]:
    """Divide ``ranges`` into at most ``parts`` contiguous stretches.

    Split by *bytes* rather than by range, so one worker does not get a
    kilobyte-wide gap while another gets the rest of the file. A stretch is
    never split below ``minimum``, where the overhead of opening a session for
    it would outweigh fetching it in sequence.

    ``minimum`` was fixed at four megabytes, which is why a 62 MB stream ran on
    fifteen sessions and a 480p one on a single session however many were
    configured — reported as "only 1080p respects connections per download",
    and correctly. It follows the `min_chunk_size` setting now, which is what
    the ordinary chunker already uses.
    """
    total = sum(end - start for start, end in ranges)
    if parts <= 1 or total <= 0:
        return list(ranges)
    parts = max(1, min(parts, total // max(1, minimum) or 1))
    if parts <= 1:
        return list(ranges)

    target = total / parts
    spans: list[tuple[int, int]] = []
    carried = 0.0
    for start, end in ranges:
        cursor = start
        while cursor < end:
            room = target - carried
            take = min(end - cursor, max(1, int(room)))
            if len(spans) == parts - 1:
                take = end - cursor
            spans.append((cursor, cursor + take))
            cursor += take
            carried += take
            if carried >= target - 0.5 and len(spans) < parts:
                carried = 0.0
    # Stretches that ended up adjacent belong to one worker, not two.
    merged: list[tuple[int, int]] = []
    for span in spans:
        if merged and merged[-1][1] == span[0] and len(merged) >= parts:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)
    return merged

#: How many streaming sessions one track may take. A session hands over about
#: a minute of media, so a feature-length video legitimately needs well over a
#: hundred — this is a stop against a server conceding a byte at a time, not a
#: budget. A session that gains nothing ends the transfer immediately and never
#: reaches this.
_MAX_SABR_SESSIONS = 240

#: How many streaming-session locks to keep before clearing out the ones
#: nobody holds. Generous — the entries are tiny — but not unbounded.
_MAX_SABR_LOCKS = 256


def _looks_expired(failure: Exception) -> bool:
    """Whether a failure means "this link is no longer valid".

    A streaming session is refused with the same status whether it expired or
    was never allowed, and the POST that carries it cannot use the usual
    "it served bytes once, so this is expiry" test: each request is a separate
    exchange, and a session that opened yesterday served plenty. The status is
    what there is to go on.
    """
    if isinstance(failure, LinkExpiredError):
        return True
    return isinstance(failure, HttpError) and failure.status in (403, 410)


def _decode_po_token(token: str) -> bytes:
    """Decode a proof-of-origin token from its base64url text form."""
    padded = token + "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        return b""


def digest_headers(server_digest: str) -> dict[str, str]:
    """Rebuild the digest header the origin sent, as stored by the probe.

    The probe records the value as ``"<header-name>: <value>"`` because a bare
    base64 blob is ambiguous — ``Content-MD5`` carries an untagged MD5 while
    ``Digest`` carries an algorithm-tagged value.
    """
    if not server_digest:
        return {}
    name, separator, value = server_digest.partition(":")
    if separator and name.strip().lower() in ("content-md5", "digest", "repr-digest"):
        return {name.strip(): value.strip()}
    # Legacy rows stored the raw value; assume the RFC 3230 form.
    return {"Digest": server_digest}


#: How a stream announces itself, when it is allowed to.
_TS_SYNC = 0x47
_TS_PACKET = 188
#: Enough repetitions to be certain: three consecutive sync bytes at the right
#: spacing do not occur by chance in arbitrary data.
_TS_CONFIRMATIONS = 4
#: How far into a piece to look for the real media. A wrapper is a header, not
#: a payload — anything further in is a coincidence, not a stream.
_MAX_WRAPPER = 1 << 16


def _looks_like_transport_stream(data: bytes, start: int) -> bool:
    """Whether an MPEG-TS packet sequence begins at ``start``."""
    for step in range(_TS_CONFIRMATIONS):
        position = start + step * _TS_PACKET
        if position >= len(data) or data[position] != _TS_SYNC:
            return False
    return True


def unwrap_disguised_segment(payload: bytes) -> bytes:
    """Strip a decoy header that a CDN wrapped around a media segment.

    Measured on a real site, from a log the user sent: every segment of a 292
    piece stream began

        89 50 4e 47 0d 0a 1a 0a 00 00 00 0d 49 48 44 52

    — a PNG signature and its `IHDR` chunk. The media follows it. Serving
    segments as images is an ordinary way to get past filters and caches that
    treat video differently, and the site's own player strips the header in
    JavaScript before handing the bytes to the decoder.

    Nothing about this is visible from outside: the transfer succeeds, every
    piece arrives, the sizes are right, and the assembled file plays nothing.
    That was four sessions of "it downloads and will not play".

    The image is skipped and the media returned. Two ways of finding where it
    starts, in order of certainty: after a PNG's `IEND` chunk, which the format
    defines as its end; failing that, the first transport-stream sync byte with
    packets following it at the right spacing. When neither finds anything the
    payload is returned untouched — a piece that is genuinely an image is a
    different fault, and `_require_recognisable` reports it.
    """
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return payload

    end = payload.find(b"IEND")
    if 0 <= end:
        # `IEND` is followed by its four-byte CRC, and the media begins there.
        after = end + 8
        if after < len(payload):
            return payload[after:]

    limit = min(len(payload), _MAX_WRAPPER)
    for start in range(limit):
        if payload[start] == _TS_SYNC and _looks_like_transport_stream(payload, start):
            return payload[start:]
    return payload


class _ReplanRequested(Exception):
    """Stop every worker: the chunk map this transfer was built on is wrong.

    Not an error and never reported as one — it unwinds the workers so
    `_transfer` can lay the file out again and start over.
    """


class DownloadTask:
    """Runs a single download to completion (or to a pause)."""

    def __init__(self, download: Download, engine: "DownloadEngine") -> None:
        self.download = download
        self.engine = engine
        self.db = engine.db
        self.settings = engine.settings
        self.events = engine.events

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()          # set by pause and cancel alike
        #: Set when the transfer has to be re-laid-out and run again — an
        #: origin that advertised byte ranges and then ignored one. Distinct
        #: from `_stop`, which means the user asked for this to end.
        self._replan = threading.Event()
        #: Responses currently being read, so pausing can close them rather
        #: than wait out a socket timeout on a stalled origin.
        self._live: set = set()
        self._live_lock = threading.Lock()
        self._cancelled = False
        self._paused_by_user = False

        self._chunk_lock = threading.RLock()
        #: Diagnostic lines already logged for this transfer, so sixteen
        #: sessions saying the same thing say it once. See `_log_once`.
        self._said_lock = threading.Lock()
        self._said: set[str] = set()
        #: Past the transfer and into work on bytes already on disk. Such a
        #: task holds no connections, so it holds no download slot either.
        self._postprocessing = False
        self._chunks: list[Chunk] = []
        self._next_chunk_index = 0

        self._meter = SpeedMeter()
        self._bytes_done = 0                    # byte counter (authoritative for segmented)
        self._had_success = False               # a byte has arrived on this URL
        #: One entry per live streaming worker, for the connection bars. Empty
        #: except while a parallel server-driven pass is running.
        self._sabr_workers: list[Chunk] = []
        self._refused_from = 0                  # lowest offset the origin refused
        self._renewals = 0                      # fresh links obtained mid-transfer
        self._link_expired = False

        self._limiter = TokenBucket(download.speed_limit or 0)
        self._key_cache: dict[str, bytes] = {}
        self._key_lock = threading.Lock()
        # Scoped to the page they came from, not filed under "" — a jar built
        # from a bare string stores its cookies under the empty domain, and
        # `header_for()` merges that bucket into *every* host. So withholding
        # the header in `_request_headers()` achieved nothing: the client's own
        # fallback (`http_client._default_headers`, "cookie not in merged")
        # put the site's whole session straight back onto the CDN request. Two
        # layers, one decision, and only one of them was fixed.
        self._cookies = CookieJar()
        if download.cookies:
            self._cookies.load_header(
                download.cookies, self._cookie_scope(download.referer))
        self._error: Exception | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def postprocessing(self) -> bool:
        """Past the transfer: assembling, rewrapping or joining a pair.

        Such a task is still alive and still owns its row, but it holds no
        connection — so it must not count against the number of downloads
        allowed to run at once.
        """
        return self._postprocessing

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._cancelled = False
        self._paused_by_user = False
        self._error = None
        self._meter.reset()
        self._thread = threading.Thread(
            target=self._run, name=f"ixd-download-{self.download.id}", daemon=True
        )
        self._thread.start()

    def pause(self) -> None:
        self._paused_by_user = True
        self.download.stage = "Pausing…"
        self._stop.set()
        self._abandon_connections()

    def cancel(self) -> None:
        self._cancelled = True
        self.download.stage = "Stopping…"
        self._stop.set()
        self._abandon_connections()

    def _abandon_connections(self) -> None:
        """Close every response in flight so a blocked read returns at once.

        Setting the flag is not enough. A worker waiting on a stalled origin is
        inside a socket read with the profile's timeout — thirty seconds — and
        notices nothing until it returns, so Pause appeared to do nothing for
        half a minute on exactly the connection a person most wants to stop.

        Closing the socket from here makes that read fail immediately; the
        worker then sees the flag and unwinds through the path it already has
        for a dropped connection. Best-effort by construction: a response that
        has already been closed, or is closing, is not a problem.
        """
        with self._live_lock:
            live = list(self._live)
            self._live.clear()
        for response in live:
            try:
                # `abort`, not `close`: closing a socket does not interrupt a
                # read already blocked on it.
                abandon = getattr(response, "abort", response.close)
                abandon()
            except Exception:  # noqa: BLE001 - closing is never worth raising for
                pass

    def _track(self, response: Any) -> Any:
        """Remember a response for the length of its read."""
        with self._live_lock:
            self._live.add(response)
        # A pause that arrived between opening and tracking would have missed
        # it, so the flag is re-checked here rather than trusted to ordering.
        if self._stop.is_set():
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        return response

    def _untrack(self, response: Any) -> None:
        with self._live_lock:
            self._live.discard(response)

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def set_speed_limit(self, limit: int) -> None:
        self.download.speed_limit = limit
        self._limiter.set_rate(limit)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _profile(self) -> "NetworkProfile":
        return self.engine.proxies.profile_for(self.download)

    def _client(self) -> HttpClient:
        """The client for this download, told where the request comes from.

        The page has to reach the client, not just the header builder: only the
        client sees the *target* of each request, and `Referer`/`Origin` are
        decided per target. Without it `_referer_for()` — which implements the
        browser's own `strict-origin-when-cross-origin` policy — was dead code
        on every engine request, and the media CDN received a `Referer` no
        browser sends and no `Origin` at all.

        The browser's own captured headers travel too, scoped to the host they
        were observed on, because a replayed set beats a reconstructed one.
        """
        return HttpClient(
            self._profile(), self._cookies,
            referer=self.download.referer,
            site_headers=self.download.extra_headers,
            site_host=self._cookie_scope(self.download.referer),
        )

    @staticmethod
    def _cookie_scope(url: str) -> str:
        """The registrable domain a cookie header may be sent to.

        Only a leading ``www.`` is dropped: a naive "last two labels" rule turns
        ``bbc.co.uk`` into ``co.uk`` and would widen every cookie to a public
        suffix.
        """
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host

    def _request_headers(self, url: str = "") -> dict[str, str]:
        """The headers for one request of this download.

        ``url`` decides whether the cookies travel. They belong to the *page*,
        and a media CDN is a different registrable domain — `googlevideo.com`
        against `youtube.com` — so a browser sends it none at all. This sent
        the site's whole session to the CDN on every request, which is a header
        the browser never sends and which the origin answered **403**. Seen in a
        field log as every plain `videoplayback` address failing its probe five
        times over, whether the address came from extraction or from the
        browser's own capture.

        With no page to compare against, the cookies travel as before: an
        ordinary download and its cookies come from the same place.
        """
        # A download is a subresource fetch, never a page navigation. The
        # client's default `Accept` is the one a browser sends for a document —
        # `text/html,…` — and no player asks a media CDN for HTML.
        headers: dict[str, str] = {"Accept": "*/*"}
        if self.download.user_agent:
            headers["User-Agent"] = self.download.user_agent
        # `Referer` is deliberately **not** set here. Setting it suppresses
        # `HttpClient._referer_for()`, which knows the target and applies the
        # browser's own policy: the whole address within one origin, the bare
        # origin across one, and an `Origin` header beside it. Sending the full
        # watch URL to a media CDN is something no browser does, and the
        # `Origin` a player always sends went missing with it — measured as
        # "this request carried: Referer, User-Agent" against a browser's ten.
        if self.download.cookies:
            page = self._cookie_scope(self.download.referer)
            target = self._cookie_scope(url) if url else page
            if not page or not target or target == page \
                    or target.endswith("." + page):
                headers["Cookie"] = self.download.cookies
        # `Referer` and `Origin` are struck out of whatever was inherited,
        # every time, because **only the client knows the target** and these
        # two are decided per target. An extractor stores a page `Referer` in
        # its format's `http_headers` (youtube.py sets one), `add_media` files
        # that as `extra_headers`, and merging it here put `referer` back into
        # the request — which switched off `HttpClient._referer_for()` exactly
        # as setting it directly did, and the `Origin` a player always sends
        # went missing with it. Measured, after the fix that was meant to end
        # this: "this request carried: accept, accept-language, connection,
        # referer, user-agent" — a referer present, no origin.
        #
        # Third time this shape has cost a session: cookies (§3.14u17j),
        # referer (§3.14u17k), and now referer arriving by inheritance. The
        # rule is a rule now rather than a repair.
        for key, value in (self.download.extra_headers or {}).items():
            if key.lower() in ("referer", "origin"):
                continue
            headers[key] = value
        return headers

    def _limiters(self) -> CompositeLimiter:
        return CompositeLimiter(self._limiter, self.engine.global_limiter)

    def _clear_stage(self) -> None:
        self.download.stage = ""

    def _set_status(self, status: DownloadStatus, error: str = "") -> None:
        if status in (DownloadStatus.PAUSED, DownloadStatus.CANCELLED,
                      DownloadStatus.ERROR, DownloadStatus.COMPLETED):
            self.download.stage = ""
        self.download.status = status
        self.download.error = error
        if self.download.id is not None:
            self.db.update_download_fields(self.download.id, status=status, error=error)
        self.events.emit(
            EventType.DOWNLOAD_UPDATED,
            download_id=self.download.id,
            status=status.value,
            error=error,
        )

    def _log(self, message: str, level: str = "info") -> None:
        self.db.log_event(message, self.download.id, level)
        self.events.emit(EventType.LOG, download_id=self.download.id,
                         message=message, level=level)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise CancelledError("stopped")

    @staticmethod
    def _requeue_unfinished(chunks: list[Chunk]) -> None:
        """Return every chunk that is not genuinely finished to the pool.

        ``ACTIVE`` chunks were interrupted and ``FAILED`` ones hit an error; if
        either were left as-is they would never be picked up again and the
        assembled file would silently contain a hole.
        """
        for chunk in chunks:
            finished = (
                chunk.status is ChunkStatus.DONE
                and (chunk.size < 0 or chunk.downloaded >= chunk.size)
            )
            if not finished:
                chunk.status = ChunkStatus.PENDING

    # ------------------------------------------------------------------
    # main body
    # ------------------------------------------------------------------
    def _run(self) -> None:
        flusher: threading.Thread | None = None
        try:
            self._set_status(DownloadStatus.CONNECTING)
            if not self.download.started_at:
                self.download.started_at = time.time()
                self.db.update_download_fields(
                    self.download.id, started_at=self.download.started_at
                )

            self._prepare()
            self._check_stop()

            self._set_status(DownloadStatus.DOWNLOADING)
            flusher = threading.Thread(
                target=self._flush_loop, name=f"ixd-flush-{self.download.id}", daemon=True
            )
            flusher.start()

            self._transfer()

            if self._stop.is_set():
                self._handle_stop()
                return

            self._finalize()

        except CancelledError:
            self._handle_stop()
        except LinkExpiredError as exc:
            self._enter_needs_link(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._error = exc
            self._flush_progress()
            message = str(exc) or exc.__class__.__name__
            self._set_status(DownloadStatus.ERROR, message)
            self._log(f"Failed: {message}", "error")
            self.events.emit(
                EventType.DOWNLOAD_FAILED, download_id=self.download.id, error=message
            )
        finally:
            self._stop.set()
            if flusher is not None:
                flusher.join(timeout=3.0)
            self.engine._task_finished(self)

    def _handle_stop(self) -> None:
        self._flush_progress()
        if self._cancelled:
            self._set_status(DownloadStatus.CANCELLED)
            self._log("Cancelled")
        elif self._link_expired:
            self._enter_needs_link(self.download.error or "source link expired")
        else:
            with self._chunk_lock:
                self._requeue_unfinished(self._chunks)
            self._flush_progress()
            self._set_status(DownloadStatus.PAUSED)

    def _enter_needs_link(self, message: str) -> None:
        self._link_expired = True
        self._flush_progress()
        with self._chunk_lock:
            self._requeue_unfinished(self._chunks)
        self._flush_progress()
        self._set_status(DownloadStatus.NEEDS_LINK, message)
        self._log(f"Link expired — a refreshed URL is required: {message}", "warning")
        self.events.emit(
            EventType.DOWNLOAD_NEEDS_LINK,
            download_id=self.download.id,
            url=self.download.url,
            message=message,
        )

    # ------------------------------------------------------------------
    # preparation
    # ------------------------------------------------------------------
    def _prepare(self) -> None:
        download = self.download
        config.ensure_dirs()

        if download.mode is TransferMode.SEGMENTED:
            self._prepare_segmented()
        elif download.mode is TransferMode.SABR:
            self._prepare_sabr()
        else:
            self._prepare_ranged()

        if not download.dest_dir:
            download.dest_dir = str(
                config.destination_for(self.settings, download.filename, download.mime)
            )
        Path(download.dest_dir).mkdir(parents=True, exist_ok=True)
        download.category = config.category_for(download.filename, download.mime)

        if not download.temp_path:
            download.temp_path = str(
                config.TEMP_DIR / f"{download.id}-{sanitize_filename(download.filename)}{PART_SUFFIX}"
            )
        self.db.update_download(download)

    def _prepare_ranged(self) -> None:
        download = self.download
        existing = self.db.load_chunks(download.id) if download.id else []
        resuming = bool(existing) and any(c.downloaded > 0 for c in existing)

        info = self._probe_with_retries()

        # What the origin calls the file beats what its address looked like.
        #
        # Only a name of ours is replaced: `auto_named` is set exactly when
        # nobody chose one, so a filename typed into the Add dialog or handed
        # over by the browser survives whatever the server says. And only
        # before any bytes are on disk — renaming a resuming download would
        # orphan the part file it is continuing into.
        if not download.filename or (download.auto_named and not resuming):
            better = sanitize_filename(
                info.filename or filename_from_url(info.url, info.mime)
            )
            previous = download.filename
            if better and better != previous:
                if previous:
                    self._log(
                        f"The server calls this “{better}”, not "
                        f"“{previous}” — renamed."
                    )
                download.filename = better
                download.category = config.category_for(better, info.mime)
                # Derived from the old name, so recomputed from the new one by
                # `_prepare`; a stale temp path would write the bytes into a
                # part file named after a guess.
                download.temp_path = ""
                # The folder too — but only if it is the one *this* application
                # chose for the old name. A folder the user picked in the Add
                # dialog is a decision, and a better filename is no reason to
                # move their download somewhere else.
                if previous and download.dest_dir:
                    chosen_for_guess = str(config.destination_for(
                        self.settings, previous, download.mime))
                    if download.dest_dir == chosen_for_guess:
                        download.dest_dir = ""
            # Given by the origin now, or typed: either way it is no longer
            # a guess and nothing further may overrule it.
            download.auto_named = False
        download.mime = download.mime or info.mime
        download.server_digest = info.digest or download.server_digest

        if resuming and self._resume_invalidated(info):
            self._log("Remote file changed since the last attempt — restarting", "warning")
            existing = []
            resuming = False
            self._bytes_done = 0
            try:
                if download.temp_path and os.path.exists(download.temp_path):
                    os.remove(download.temp_path)
            except OSError:
                pass

        download.total_size = info.size or download.total_size
        download.supports_ranges = info.supports_ranges and info.size > 0
        download.etag = info.etag or download.etag
        download.last_modified = info.last_modified or download.last_modified
        download.mode = TransferMode.RANGED if download.supports_ranges else TransferMode.SINGLE

        if not download.temp_path:
            download.temp_path = str(
                config.TEMP_DIR / f"{download.id}-{sanitize_filename(download.filename)}{PART_SUFFIX}"
            )

        if resuming and existing:
            with self._chunk_lock:
                self._chunks = existing
                self._next_chunk_index = max(c.index for c in existing) + 1
                self._requeue_unfinished(self._chunks)
            self._bytes_done = sum(c.downloaded for c in existing)
            self._log(
                f"Resuming at {self._bytes_done:,} / {download.total_size:,} bytes "
                f"across {len(existing)} chunks"
            )
        else:
            self._plan_byte_chunks()

        self._allocate_file(download.total_size)

    def _prepare_sabr(self) -> None:
        """Plan a server-driven transfer.

        There is nothing to probe and no range map to build: the origin decides
        what to send and the size is already known from the extraction. The
        chunk map is a single span so that progress, resume and the
        completeness guard all work exactly as they do for any other transfer.
        """
        download = self.download
        context = download.sabr_context or {}
        if not context.get("endpoint"):
            raise IXDError("this server-driven stream has no endpoint recorded")

        audio = context.get("audio") or {}
        video_size = int(context.get("size") or 0)
        audio_size = int(audio.get("size") or 0)
        # The size of the file that will exist, not of one track of it. The two
        # are fetched by this one download and joined at the end, so a bar that
        # counted only the video would reach the end and then have to grow.
        download.total_size = (video_size + audio_size) or download.total_size or 0
        download.supports_ranges = False
        if not download.filename:
            download.filename = sanitize_filename(
                context.get("filename") or f"stream-{context.get('itag', 0)}.mp4"
            )
        if not download.temp_path:
            download.temp_path = str(
                config.TEMP_DIR
                / f"{download.id}-{sanitize_filename(download.filename)}{PART_SUFFIX}"
            )

        existing = self.db.load_chunks(download.id) if download.id else []
        with self._chunk_lock:
            if existing:
                self._chunks = existing
                self._requeue_unfinished(self._chunks)
            else:
                # One chunk per track. They are separate files until they are
                # joined, so the spans describe each track's own length rather
                # than a position in a shared output.
                self._chunks = [Chunk(
                    index=0, start=0,
                    end=(video_size - 1) if video_size else -1,
                    download_id=download.id,
                )]
                if audio:
                    self._chunks.append(Chunk(
                        index=1, start=0,
                        end=(audio_size - 1) if audio_size else -1,
                        download_id=download.id,
                    ))
            self._next_chunk_index = len(self._chunks)
        # Progress carries over. A server-driven session *is* rebuilt from
        # scratch, which is why this used to reset to zero — but the bytes an
        # earlier attempt wrote are still in the file, and the ranges it held
        # were recorded, so the new session asks only for what is missing.
        # Zeroing here undid all of that: an interrupted transfer showed no
        # progress and began again at the beginning, which is precisely what
        # made a download that stopped at 70% impossible to resume.
        self._bytes_done = max(
            (chunk.downloaded for chunk in self._chunks), default=0
        )
        # The output is preallocated, so the existing file keeps whatever an
        # earlier attempt wrote; allocation only sets the length. Each track
        # gets its own file, since they are joined rather than interleaved.
        self._allocate_file(video_size)
        if audio:
            self._allocate_path(self._audio_temp_path(), audio_size)

    def _transfer_sabr(self) -> None:
        """Run the server-driven exchange, writing each block where it belongs.

        Transfers sharing an endpoint are serialised. The endpoint carries one
        session, and the server tracks a single playback position within it, so
        two downloads driving it at once — a video and its audio track, say —
        each see the other's progress and neither advances.
        """
        from ..extractors.sabr import SabrFormat, SabrStream   # noqa: PLC0415

        download = self.download
        context = download.sabr_context or {}
        audio = context.get("audio") or {}

        workers = self._sabr_worker_count()
        if self._chunks[0].status is not ChunkStatus.DONE:
            if workers > 1:
                # Deliberately outside the session lock: that lock exists to
                # stop two *different* transfers driving one session, and these
                # workers are one transfer opening several on purpose.
                self._run_sabr_parallel(context, self._chunks[0],
                                        download.temp_path, "", workers)
            else:
                with self.engine.sabr_session_lock(
                        context.get("endpoint", ""),
                        str(context.get("config", ""))):
                    self._run_sabr_track(context, self._chunks[0],
                                         download.temp_path)

        if not audio:
            return

        # The companion track. One endpoint carries one session with a single
        # playback position, so the two are fetched one after the other rather
        # than at once — driving both together leaves each seeing the other's
        # progress and neither advancing.
        if self._chunks[1].status is not ChunkStatus.DONE:
            if workers > 1:
                self._run_sabr_parallel(audio, self._chunks[1],
                                        self._audio_temp_path(), "audio",
                                        workers)
            else:
                with self.engine.sabr_session_lock(
                        audio.get("endpoint", ""),
                        str(audio.get("config", ""))):
                    self._run_sabr_track(audio, self._chunks[1],
                                         self._audio_temp_path(), "audio")

    #: Never open more streaming sessions for one track than this, however
    #: many connections are configured. Each is a whole session the origin has
    #: to keep, not a socket, and the cost of one too many is a refusal rather
    #: than a slower transfer — so the connection count is honoured up to a
    #: ceiling instead of literally.
    _MAX_SABR_WORKERS = 16

    def _connection_count(self) -> int:
        """How many workers this download may run, whatever its transfer mode.

        The download's own setting, falling back to the global default and
        bounded by the configured ceiling. One rule for every mode: a separate
        limit for segmented media meant a user who asked for sixteen
        connections silently got eight on every HLS and DASH site, because
        ``hls_max_workers`` quietly won. A setting that is overridden somewhere
        else is not a setting.
        """
        configured = int(self.download.connections or 0) or \
            self.settings.get_int("connections_per_download", 8)
        ceiling = self.settings.get_int("max_connections_per_download", 32)
        return max(1, min(configured, max(1, ceiling)))

    def _sabr_worker_count(self) -> int:
        """How many streaming sessions to run on one track at a time.

        The download's own connection count, because that is what it means.
        A server-driven transfer has no byte ranges to divide — the client asks
        an endpoint for media, not for bytes — so "sixteen connections" becomes
        sixteen sessions, each starting at its own point in the stream.

        This was disabled for two sessions and it is worth saying why, because
        the reason is what makes it safe now. A position in this protocol is a
        *time*, and a byte offset used to be converted by estimating from the
        stream's length — right only at constant bitrate, and real media is
        not. A session asked to continue at byte 95,499,262 delivered from
        103,813,481, eight megabytes past, and the difference was a hole
        nothing could seek back into: the same estimate lands in the same wrong
        place every time. A 327 MB download stopped at 88%.

        The stream publishes the exact answer in the ``sidx`` of its own
        header, which is fetched before any media regardless. With that there
        is no estimate left to be wrong. A track publishing no index is still
        fetched on a single session.
        """
        if not _SABR_PARALLEL_ENABLED:
            return 1
        try:
            configured = int(self.download.connections or 0)
        except (TypeError, ValueError):
            configured = 0
        if configured <= 0:
            configured = self.engine.settings.get_int("connections_per_download", 8)
        return max(1, min(configured, self._MAX_SABR_WORKERS))

    def _run_sabr_track(self, context: dict[str, Any], chunk: Chunk,
                        temp_path: str, track: str = "") -> None:
        """Fetch one track, opening a fresh session whenever one is spent.

        A session hands over a bounded amount of media and then stops, so a
        track longer than that allowance *cannot* be fetched in one — it takes
        as many sessions as it takes, each continuing where the last left off.
        Treating the first stop as the end of the download made the user press
        Resume once per session, and a long video failed as many times as it
        had minutes in it.

        The guard is progress, not patience: a session that gained nothing has
        nothing to continue from, and asking again would only repeat it. That
        is why the ceiling below is generous rather than careful — it exists
        only so a server that concedes a byte at a time cannot loop forever.
        ``max_retries`` is deliberately not used: it is the budget for
        transient network errors, and spending it here would fail a long video
        that was advancing perfectly well, five sessions in.
        """
        last_error: Exception | None = None
        session = 0

        while session < _MAX_SABR_SESSIONS:
            session += 1
            before = chunk.downloaded
            try:
                self._run_sabr(context, chunk, temp_path, track)
                return
            except CancelledError:
                raise                      # a pause or a cancel is not a fault
            except Exception as exc:       # noqa: BLE001 - reported below
                last_error = exc
                # The context is re-read because the attempt that just ended
                # wrote its resume state into it, and the next one continues
                # from exactly that.
                stored = self.download.sabr_context or {}
                context = (stored.get(track) or {}) if track else stored

                # An endpoint that has expired is not something another session
                # will fix, and this is asked before anything else because the
                # alternative was subtler than it looks: fetching the header
                # counts as bytes gained, so the first refusal read as progress
                # and bought a second doomed session before the expiry was
                # noticed. Everything already on disk is kept; only the way in
                # is replaced.
                renewed = self._renew_sabr_session(context, track, exc)
                if renewed is not None:
                    context = renewed
                    self._check_stop()
                    continue
                if chunk.downloaded <= before:
                    break                  # nothing gained; another go is waste
                total = chunk.size if chunk.size >= 0 else 0
                progress = (f" ({chunk.downloaded * 100 // total}%)"
                            if total else "")
                self._log(
                    f"The streaming session ran out at "
                    f"{chunk.downloaded:,} bytes{progress}; opening another "
                    f"and continuing from there (session {session + 1})"
                )
                self._check_stop()

        if last_error is not None:
            raise last_error

    def _run_sabr_parallel(self, context: dict[str, Any], chunk: Chunk,
                           temp_path: str, track: str, workers: int) -> None:
        """Fetch one track over several streaming sessions at once.

        Each worker is given a contiguous stretch of what is still missing and
        sessions of its own, opened by telling each session it already holds
        everything *outside* that stretch — which makes it seek there and
        consider itself finished once the stretch is covered. That reuses the
        machinery resuming already needed (the coverage map, ``missing()`` and
        ``_seek_to_byte``) rather than inventing a second way to track what has
        arrived.

        A worker takes as many sessions as its stretch needs, exactly as the
        serial path does: an allowance running out is the ordinary case, not a
        failure. The union of what the workers write is the track's coverage,
        so an interrupted parallel transfer resumes like a serial one and the
        worker count may change between runs.

        **Workers meet; they do not abut.** This protocol addresses media by
        *time*, and a byte offset is converted to a time by estimating from the
        stream's length — which is only exact at constant bitrate. Asked to
        continue at byte 95,499,262 of a real 1080p60 video, a fresh session
        delivered from 103,813,481: eight megabytes past the point wanted. A
        worker that stopped at its own stretch's end would therefore leave the
        distance between where it was *told* to start and where it actually
        started unfetched — and those holes are what left a 327 MB download
        stuck at 88%, because nothing could seek back into them either.

        So a worker does not stop at a boundary. It stops when the ground from
        its own start to the *next* worker's start is closed, reading into its
        neighbour's territory if it must. Overlap costs a little bandwidth; a
        hole costs the download.
        """
        size = int(context.get("size") or 0)
        held = _normalise_ranges(context.get("covered") or [])
        outstanding = _missing_ranges(held, size) if size > 0 else []
        # The same floor the ordinary chunker uses, for the same reason: a
        # session costs a request, a reply and the server's own fixed
        # allowance, so dividing past that point buys nothing.
        smallest = max(1, self.settings.get_int("min_chunk_size",
                                                _MIN_SABR_SPAN))
        spans = (_split_ranges(outstanding, workers, smallest)
                 if outstanding else [])
        if size <= 0 or len(spans) <= 1:
            # Nothing to divide: a track whose length is unknown, one already
            # finished, or one small enough that splitting it would cost more
            # than it saves.
            #
            # Said aloud, because it was not: a 480p stream ran on one session
            # while a 1080p one ran on fifteen, with nothing in the log to say
            # why, and it read as the setting being ignored.
            if size > 0:
                self._log(
                    f"This stream is {size:,} bytes, which divides into fewer "
                    f"than two stretches of at least {smallest:,} — so it is "
                    f"fetched on a single session rather than {workers}")
            self._run_sabr_track(context, chunk, temp_path, track)
            return

        # The stream's own byte-to-time index, read once from its header and
        # given to every session. Without it a worker can only estimate where
        # to start, and an estimate that is out by a couple of seconds of
        # playback lands megabytes away — which is what left holes nothing
        # could seek back into.
        index = _index_in(self._stream_header_bytes(context))
        if not index:
            # No published index means no exact seek, and a parallel pass
            # without one is what stranded a download at 88%. One session
            # reading forward never needs to seek at all.
            self._log("This stream publishes no segment index, so it is "
                      "fetched on a single session rather than risk gaps")
            self._run_sabr_track(context, chunk, temp_path, track)
            return

        self._log(
            f"Fetching this track over {len(spans)} streaming sessions at once"
            + ("" if len(spans) >= workers else
               f" — fewer than the {workers} configured, because "
               f"{size:,} bytes will not divide further into stretches of at "
               f"least {smallest:,}"))

        covered: list[tuple[int, int]] = list(held)
        covered_lock = threading.Lock()
        failures: list[Exception] = []

        # One bar per session, so the connection display shows what is actually
        # running rather than the single span the chunk map keeps per track.
        with self._chunk_lock:
            self._sabr_workers = [
                Chunk(index=index, start=start, end=end - 1,
                      downloaded=sum(b - a for a, b in
                                     [(max(x, start), min(y, end)) for x, y in held]
                                     if b > a),
                      status=ChunkStatus.ACTIVE)
                for index, (start, end) in enumerate(spans)
            ]


        def claim(start: int, end: int) -> list[tuple[int, int]]:
            """Take the parts of ``start..end`` nobody has written yet.

            A worker keeps whatever it is sent that is still missing, whether
            or not it falls in the stretch that worker asked for. The first
            version clipped each block to the worker's own stretch, and a
            session that answered from a different position than it was asked
            for therefore recorded *nothing* — so the worker saw no progress,
            gave up, and left its whole stretch to the single-session pass at
            the end. Keeping the bytes costs nothing and is never wrong: this
            map is what decides whether a byte is wanted.
            """
            with covered_lock:
                mine = _missing_ranges(_normalise_ranges(covered), end)
                mine = [(max(a, start), min(b, end)) for a, b in mine
                        if min(b, end) > max(a, start)]
                if mine:
                    covered.extend(mine)
                    merged = _normalise_ranges(covered)
                    covered[:] = merged
                    total = sum(b - a for a, b in merged)
                else:
                    total = sum(b - a for a, b in _normalise_ranges(covered))
            if mine:
                with self._chunk_lock:
                    # Bytes actually held, not the highest offset reached. With
                    # workers spread across the file the highest offset is near
                    # the end from the first block, and a bar driven by it
                    # would show a download as nearly finished before it had
                    # started.
                    chunk.downloaded = total
                    self._bytes_done = sum(c.downloaded for c in self._chunks)
                    # Attribute each piece to the bar whose stretch contains
                    # it, so a worker's bar reflects its own stretch and not
                    # whichever thread happened to receive the bytes.
                    for begin, finish in mine:
                        for bar in self._sabr_workers:
                            overlap = min(finish, bar.end + 1) - max(begin, bar.start)
                            if overlap > 0:
                                bar.downloaded += overlap
            return mine

        def covered_total() -> int:
            with covered_lock:
                return sum(b - a for a, b in _normalise_ranges(covered))

        def closed(start: int, end: int) -> bool:
            """Whether every byte of ``start..end`` is held by somebody."""
            if end <= start:
                return True
            with covered_lock:
                merged = _normalise_ranges(covered)
            return not any(
                gap[0] < end and gap[1] > start
                for gap in _missing_ranges(merged, end)
            )

        def held_within(span: tuple[int, int]) -> list[tuple[int, int]]:
            start, end = span
            with covered_lock:
                merged = _normalise_ranges(covered)
            return [(max(a, start), min(b, end)) for a, b in merged
                    if min(b, end) > max(a, start)]

        def persist() -> None:
            with covered_lock:
                snapshot = [list(span) for span in _normalise_ranges(covered)]
            merged = dict(self.download.sabr_context or {})
            state = {"covered": snapshot}
            if track:
                companion = dict(merged.get(track) or {})
                companion.update(state)
                merged[track] = companion
            else:
                merged.update(state)
            self.download.sabr_context = merged
            self.db.update_download_fields(
                self.download.id, sabr_context=json.dumps(merged))

        def run(position: int, span: tuple[int, int]) -> None:
            start, _ = span
            # Where the next worker begins — the point this one has to reach.
            # The last worker's target is the end of the track.
            frontier = spans[position + 1][0] if position + 1 < len(spans) else size

            def first_gap() -> int | None:
                """The first byte of this region nobody holds yet."""
                with covered_lock:
                    merged = _normalise_ranges(covered)
                for gap_start, gap_end in _missing_ranges(merged, frontier):
                    if gap_end > start:
                        return max(gap_start, start)
                return None

            session = 0
            # How far *before* the wanted byte to ask from. A position is a
            # time, and a byte is turned into one by estimating from the
            # stream's length — exact only at constant bitrate. Asked to
            # continue at byte 95,499,262 of a real video, a session delivered
            # from 103,813,481, and the eight megabytes in between could never
            # be reached by asking for them again: the same estimate lands in
            # the same wrong place every time. Asking from progressively
            # earlier makes the session land at or before the gap and read
            # forward *through* it. The bytes it re-sends on the way are
            # already held and are discarded; that is the price of closing a
            # hole this protocol has no other way to reach.
            bias = 0
            while session < _MAX_SABR_SESSIONS:
                session += 1
                if closed(start, frontier):
                    return          # met the next worker; nothing left between
                inside = held_within((start, frontier))
                before = sum(b - a for a, b in inside)
                before_edge = first_gap()
                if before_edge is None:
                    return              # nothing left in this region
                try:
                    self._run_sabr_span(context, temp_path, (start, frontier),
                                        size, claim, inside, first=position == 0,
                                        done=lambda: covered_total() >= size
                                        or closed(start, frontier),
                                        bias=bias, index=index)
                except CancelledError:
                    # A worker stops for two reasons that arrive as the same
                    # exception: the user paused, or this worker's stretch is
                    # finished. Only the first is a cancellation — treating
                    # completion as one killed the thread with a traceback and
                    # would have reported a finished pass as interrupted.
                    if self._stop.is_set():
                        raise
                    return
                except Exception as exc:        # noqa: BLE001 - see below
                    with covered_lock:
                        failures.append(exc)
                if closed(start, frontier):
                    return
                after = sum(b - a for a, b in held_within((start, frontier)))
                edge = first_gap()
                if edge is not None and edge <= before_edge:
                    # The first byte still wanted did not move, so the session
                    # landed past it again — and asking for it once more would
                    # land in the same place, because the same estimate
                    # produces the same answer. Reach further back instead, so
                    # the session opens before the gap and reads through it.
                    bias = max(_SEEK_BIAS_START, bias * 2)
                    if bias > _SEEK_BIAS_LIMIT:
                        return
                    self._check_stop()
                    continue
                bias = 0        # the gap moved; ask normally again
                if after <= before:
                    # A session that gained nothing has nothing to continue
                    # from; another would repeat it. The stretch is left to the
                    # single-session pass at the end.
                    return
                self._check_stop()

        threads = [
            threading.Thread(target=run, args=(position, span),
                             name=f"ixd-sabr-{position}", daemon=True)
            for position, span in enumerate(spans)
        ]
        for thread in threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in threads):
                threads[0].join(timeout=_COVERAGE_SAVE_SECONDS)
                if not threads[0].is_alive():
                    threads.append(threads.pop(0))
                persist()
        finally:
            for thread in threads:
                thread.join()
            persist()
            with self._chunk_lock:
                # The bars belong to this pass. Anything after it — the
                # single-session finish, the other track — is drawn from the
                # ordinary chunk map again.
                self._sabr_workers = []

        with covered_lock:
            remaining = _missing_ranges(_normalise_ranges(covered), size)
        if remaining:
            # Whatever the workers could not finish is finished the way it
            # always was — one session, continuing from what is now on disk. A
            # parallel pass that stops short is not a failure; it is a transfer
            # with less left to do than it started with.
            missing_bytes = sum(b - a for a, b in remaining)
            self._log(
                f"{missing_bytes:,} bytes still missing after the parallel "
                "pass; finishing them on a single session"
            )
            stored = self.download.sabr_context or {}
            self._run_sabr_track(
                dict((stored.get(track) or {}) if track else stored),
                chunk, temp_path, track,
            )
            return

        if failures:
            self._log(f"A streaming session ended early ({failures[0]}), but "
                      "every byte of this track arrived.")
        with self._chunk_lock:
            chunk.downloaded = size
            if chunk.size >= 0 and chunk.downloaded >= chunk.size:
                chunk.status = ChunkStatus.DONE
            self._bytes_done = sum(c.downloaded for c in self._chunks)

    def _run_sabr_span(self, context: dict[str, Any], temp_path: str,
                       span: tuple[int, int], size: int, claim: Any,
                       held: list[tuple[int, int]], first: bool,
                       done: Any = None, bias: int = 0,
                       index: list | None = None) -> None:
        """One session, asked to start at ``span`` and keeping what it is sent.

        ``bias`` asks from that many bytes earlier than the first byte wanted,
        so a session whose position estimate overshoots still lands before the
        gap and reads forward through it.
        """
        from ..extractors.sabr import SabrFormat, SabrStream   # noqa: PLC0415

        start, end = span
        media_format = SabrFormat(
            itag=int(context.get("itag") or 0),
            last_modified=int(context.get("last_modified") or 0),
            size=size,
            is_audio=bool(context.get("is_audio")),
            xtags=context.get("xtags") or "",
        )
        token = context.get("po_token") or ""
        streamer = str(context.get("streamer_context") or "")
        streamer_context = None
        if streamer:
            try:
                streamer_context = base64.b64decode(streamer)
            except (ValueError, TypeError):
                streamer_context = None
        try:
            duration_ms = int(float(context.get("duration") or 0) * 1000)
        except (TypeError, ValueError):
            duration_ms = 0

        limiters = self._limiters()
        # Its own handle, as every ranged worker has: two threads sharing one
        # would interleave a seek with another's write.
        handle = open(temp_path, "r+b")
        try:
            def write(offset: int, data: bytes) -> None:
                self._check_stop()
                # Whatever arrives that nobody has written yet. A session
                # answers from where it pleases, not necessarily where it was
                # asked, so a worker that kept only its own stretch could
                # record nothing at all and conclude it was making no progress.
                # The shared map decides what is wanted; overlap between
                # workers is dropped here rather than raced on disk.
                for begin, finish in claim(offset, offset + len(data)):
                    piece = data[begin - offset:finish - offset]
                    limiters.consume(len(piece))
                    handle.seek(begin)
                    handle.write(piece)
                    # The same accounting the single-session path does. Leaving
                    # these out is why a parallel download showed no speed at
                    # all: the transfer was running, the meter was never told,
                    # and the column had nothing to report.
                    self._meter.record(len(piece))
                    self._had_success = True

            stream = SabrStream(
                self._client(), context["endpoint"],
                base64.b64decode(context.get("config") or ""), media_format,
                user_agent=self.download.user_agent,
                client_id=int(context.get("client_id") or 5),
                po_token=_decode_po_token(token) if token else None,
                streamer_context=streamer_context,
                duration_ms=duration_ms,
                log=self._log_once,
            )
            if index:
                stream.index = list(index)
            # Everything outside this stretch — and everything already held
            # inside it — is declared to the session, so it seeks to the first
            # byte actually wanted and stops once the stretch is covered.
            outside = _missing_ranges([(start, end)], size)
            known = _normalise_ranges(list(outside) + list(held))
            if bias:
                # Present the run just before the first wanted byte as missing
                # too, so the session is asked for an earlier position and
                # overshoots onto the gap rather than past it.
                wanted = _missing_ranges(known, size)
                target = next((a for a, b in wanted if b > start), start)
                floor = max(0, target - bias)
                trimmed: list[tuple[int, int]] = []
                for a, b in known:
                    if b <= floor or a >= target:
                        trimmed.append((a, b))
                        continue
                    # Keep only the parts outside the window being reopened.
                    if a < floor:
                        trimmed.append((a, floor))
                    if b > target:
                        trimmed.append((target, b))
                known = _normalise_ranges(trimmed)
            stream.restore([list(s) for s in known])
            if first:
                self._fetch_stream_header(context, write, stream)
            stream.download(
                write,
                should_stop=lambda: (self._stop.is_set()
                                     or stream.holds(start, end)
                                     or (done is not None and done())),
            )
        finally:
            handle.flush()
            handle.close()

    #: How many times one track may have its streaming session replaced. A
    #: link that expires again immediately is not expiring — it is being
    #: refused — and repeating the extraction would only re-establish that.
    _MAX_SESSION_RENEWALS = 2

    def _renew_sabr_session(self, context: dict[str, Any], track: str,
                            failure: Exception) -> dict[str, Any] | None:
        """Replace an expired streaming session, keeping everything fetched.

        A streaming endpoint is signed and lasts hours, not days, so a transfer
        paused overnight resumes against a link the origin no longer honours.
        That arrives as a plain ``403``, which is indistinguishable from a
        refusal until you notice that the same page will happily issue a new
        session for the same stream. Without this the download stopped dead
        with ``HTTP 403 Forbidden`` and no way forward but deleting it and
        starting the whole file again.
        """
        if not _looks_expired(failure):
            return None
        if self.engine.renew_sabr_session is None:
            return None
        page_url = str(context.get("page_url") or "")
        if not page_url:
            return None

        used = int(context.get("renewals") or 0)
        if used >= self._MAX_SESSION_RENEWALS:
            return None

        self._log("The streaming session for this download has expired; "
                  "asking the page for a new one and continuing from "
                  f"{self.download.downloaded:,} bytes")
        try:
            fresh = self.engine.renew_sabr_session(
                page_url, str(context.get("itag") or ""),
                bool(context.get("is_audio")),
                # The track tags travel with the request. An itag names a
                # rendition, and every audio language of a dubbed video is
                # published under the same one, so without these the renewal
                # can hand back a different language to continue a part-file
                # with — which is a finished download in the wrong language.
                str(context.get("xtags") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - the original failure stands
            self._log(f"Could not renew the streaming session: {exc}", "warning")
            return None
        if not fresh or not fresh.get("endpoint"):
            self._log("The page no longer offers this stream, so the download "
                      "cannot be continued.", "warning")
            return None

        # An itag names a rendition, not a particular encoding of it. If the
        # site has re-encoded or re-uploaded since this download began, the new
        # session describes different bytes at every offset — and the part-file
        # on disk belongs to the old one. Continuing would interleave two
        # encodings into a file that is the right length and unplayable, which
        # is worse than failing. The published length and the modification
        # stamp are what say so.
        for field, name in (("size", "length"), ("last_modified", "version")):
            was, now = context.get(field), fresh.get(field)
            if was and now and int(was) != int(now):
                self._log(
                    f"The site is now serving a different {name} of this "
                    "stream than the one already part-downloaded, so the two "
                    "cannot be joined. Downloading it again will get the "
                    "current version.", "warning")
                return None

        # Only the way in is replaced. What was fetched, where it got to and
        # which segment it reached all belong to the file, not to the session,
        # and carrying them over is the whole point of doing this.
        merged = dict(context)
        merged.update(fresh)
        for kept in ("covered", "player_ms", "sequence", "filename"):
            if kept in context:
                merged[kept] = context[kept]
        merged["renewals"] = used + 1
        merged["page_url"] = page_url

        whole = dict(self.download.sabr_context or {})
        if track:
            whole[track] = merged
        else:
            whole.update(merged)
        self.download.sabr_context = whole
        self.db.update_download_fields(self.download.id,
                                       sabr_context=json.dumps(whole))
        return merged

    def _audio_temp_path(self) -> str:
        """Where the companion track is written before the two are joined."""
        return f"{self.download.temp_path}.audio"

    def _run_sabr(self, context: dict[str, Any], chunk: Chunk | None = None,
                  temp_path: str = "", track: str = "") -> None:
        """Fetch one track into its own file.

        ``track`` names where this track's resume state is kept: the empty
        string for the video, ``"audio"`` for the companion. A quality on an
        adaptive site is two tracks that become one file, and they are fetched
        by one download rather than two — so both belong to the same row and
        both have to record their progress somewhere distinct.
        """
        from ..extractors.sabr import SabrFormat, SabrStream   # noqa: PLC0415

        download = self.download
        limiters = self._limiters()
        chunk = chunk if chunk is not None else self._chunks[0]
        temp_path = temp_path or self.download.temp_path

        media_format = SabrFormat(
            itag=int(context.get("itag") or 0),
            last_modified=int(context.get("last_modified") or 0),
            size=int(context.get("size") or 0),
            is_audio=bool(context.get("is_audio")),
            xtags=context.get("xtags") or "",
        )
        config_blob = base64.b64decode(context.get("config") or "")

        handle = open(temp_path, "r+b")
        stream: Any = None
        last_saved = [time.monotonic()]

        def save_coverage() -> None:
            """Record what is held, so an interrupted transfer can resume.

            Positions in this protocol are times rather than offsets, so a
            resumed session cannot ask for "the rest" unless it is told what it
            already has. Without this a transfer that stopped at 98% began
            again at nothing and spent its whole allowance re-fetching.
            """
            if stream is None:
                return
            merged = dict(self.download.sabr_context or {})
            state = {
                "covered": stream.coverage(),
                # The position reached, kept because it is the only exact way
                # back: converting a byte offset into a time needs the stream's
                # running length, which an older transfer may not have stored.
                "player_ms": int(stream._buffered_ms),
                # The segment index reached, and the part that actually moves
                # the server. What a session already holds is declared only
                # when a sequence is known, so a resume without one asks for
                # the stream while claiming to hold nothing — and is answered
                # from byte zero, which is what made a stopped transfer
                # impossible to continue.
                "sequence": int(stream._sequence),
            }
            if track:
                companion = dict(merged.get(track) or {})
                companion.update(state)
                merged[track] = companion
            else:
                merged.update(state)
            self.download.sabr_context = merged
            self.db.update_download_fields(
                self.download.id, sabr_context=json.dumps(merged))

        try:
            def write(offset: int, data: bytes) -> None:
                self._check_stop()
                limiters.consume(len(data))
                handle.seek(offset)
                handle.write(data)
                with self._chunk_lock:
                    chunk.downloaded = max(chunk.downloaded, offset + len(data))
                    # One row, two tracks: progress is what the finished file
                    # will contain, not what this half of it has, so a bar
                    # never restarts when the second track begins.
                    self._bytes_done = sum(c.downloaded for c in self._chunks)
                self._meter.record(len(data))
                self._had_success = True
                # Saved periodically rather than per block: a write is small
                # and frequent, and the point is only to survive an
                # interruption, not to record every byte as it lands.
                now = time.monotonic()
                if now - last_saved[0] >= _COVERAGE_SAVE_SECONDS:
                    last_saved[0] = now
                    handle.flush()
                    save_coverage()

            token = context.get("po_token") or ""
            # The player's own streamer context, when the browser supplied one.
            # It holds the proof of origin together with the client identity it
            # was issued against, so it is replayed whole rather than rebuilt.
            streamer = str(context.get("streamer_context") or "")
            streamer_context = None
            if streamer:
                try:
                    streamer_context = base64.b64decode(streamer)
                except (ValueError, TypeError):
                    streamer_context = None
            try:
                duration_ms = int(float(context.get("duration") or 0) * 1000)
            except (TypeError, ValueError):
                duration_ms = 0

            stream = SabrStream(
                self._client(), context["endpoint"], config_blob, media_format,
                user_agent=download.user_agent,
                client_id=int(context.get("client_id") or 5),
                po_token=_decode_po_token(token) if token else None,
                streamer_context=streamer_context,
                duration_ms=duration_ms,
                log=self._log,
            )
            # Whatever an earlier attempt managed to fetch is still on disk, so
            # the session is told about it and asks only for the remainder.
            stream.restore(context.get("covered"),
                           int(context.get("player_ms") or 0),
                           int(context.get("sequence") or 0))
            # The header is worth having over the ordinary URL when one exists:
            # it carries the segment index, and reading it up front turns every
            # "continue at byte N" in the session from an estimate into the
            # answer the stream published.
            #
            # Its absence is **not** a reason to refuse the download, and
            # treating it as one is what failed six transfers in the field log
            # of 2026-08-12 without a single request being made. The claim in
            # this comment — that the streaming server never sends the opening
            # bytes — is not true of the protocol: a media header carries an
            # `is_init_seg` flag (`_HEADER_IS_INIT`, sabr.py) and the server
            # sends the initialisation segment at the head of a session that
            # has not said it already holds one. `_consume` writes it at its
            # own `start_range`, which is zero, so it lands exactly where it
            # belongs with no special handling at all.
            #
            # So: ask when there is somewhere to ask, then run the session
            # either way, and judge the opening bytes on whether they actually
            # arrived rather than on where they were expected to come from.
            header_end = int(context.get("header_end") or 0)
            self._fetch_stream_header(context, write, stream)
            try:
                # The session raises for a gap **itself** — "left N bytes
                # unsent" — so anything placed after this call only runs when
                # there was no gap to fix. That is how the endpoint fallback
                # came to be written and never once executed: three field runs,
                # not a line from it in any of them.
                #
                # The refusal is therefore caught. A gap only at the opening is
                # the one this can still do something about; every other gap
                # is re-raised untouched.
                try:
                    stream.download(write, should_stop=self._stop.is_set)
                except ExtractionError as refused:
                    if header_end <= 0 or stream.holds(0, header_end + 1):
                        raise
                    self._take_opening_from_player(context, write, stream)
                    if not stream.holds(0, header_end + 1):
                        self._fetch_opening_from_endpoint(
                            context, write, stream, header_end)
                    if not stream.holds(0, header_end + 1):
                        raise
                    size = int(context.get("size") or 0)
                    if size and stream.missing(size):
                        raise
                    self._log("the opening bytes the session withheld were "
                              "fetched separately, so the transfer stands "
                              f"({refused})".replace(
                                  " It was not kept.", ""))

                if header_end > 0 and not stream.holds(0, header_end + 1):
                    self._fetch_opening_from_endpoint(
                        context, write, stream, header_end)
                if header_end > 0 and not stream.holds(0, header_end + 1):
                    raise ExtractionError(
                        f"the opening {header_end + 1:,} bytes of this stream — "
                        "its index, without which no player will open the "
                        "file — were not served, by the streaming session or "
                        "by an ordinary link. Playing the video once in the "
                        "browser and using the download panel's “Already "
                        "loaded by the player” list fetches the stream the "
                        "player itself used, which does carry them."
                    )
            finally:
                # Saved on the way out however this ended — finished, paused,
                # or failed. A transfer that cannot say where it got to is one
                # that cannot be resumed.
                handle.flush()
                save_coverage()
        finally:
            handle.close()

        with self._chunk_lock:
            if chunk.size < 0:
                chunk.end = chunk.downloaded - 1
                if not track:
                    download.total_size = chunk.downloaded
            if chunk.size >= 0 and chunk.downloaded >= chunk.size:
                chunk.status = ChunkStatus.DONE

    def _fetch_opening_from_endpoint(self, context: dict[str, Any], write: Any,
                                     stream: Any, header_end: int) -> None:
        """Last try for the opening bytes: ask the streaming endpoint plainly.

        The endpoint a server-driven session posts to is itself a
        ``videoplayback`` address, and those answer an ordinary ranged GET. The
        session never sends the initialisation segment — measured, three runs,
        every one beginning at the byte immediately after it, with the server's
        own format description naming the range it is withholding — so this is
        the one source left that costs a single request to try.

        It may not work. It is attempted because the alternative is discarding
        a complete transfer for the sake of a kilobyte, and whatever happens is
        logged with its status so the next run does not need a guess.
        """
        endpoint = str(context.get("endpoint") or "")
        if not endpoint:
            return

        # The session's own description of the range outranks the extractor's
        # arithmetic: the server said which bytes these are.
        wanted_end = header_end
        span = getattr(stream, "init_range", None)
        index = getattr(stream, "index_range", None)
        if index and isinstance(index, tuple):
            wanted_end = max(wanted_end, int(index[1]))
        elif span and isinstance(span, tuple):
            wanted_end = max(wanted_end, int(span[1]))

        itag = str(context.get("itag") or "")

        # Asked three ways, because the first attempt confused the request with
        # the answer. It appended `itag=` to an address that already carries
        # one, and `itag` is named in these addresses' `sparams` — so a
        # duplicate can invalidate the signature, and the 403 that came back
        # may have been this application's own doing rather than the origin's
        # verdict. Each shape reports its own status, so the log distinguishes
        # "the address was malformed" from "the origin refuses this".
        #
        #  1. the endpoint exactly as the session uses it, ranged by header;
        #  2. the same, ranged by the `range=` query these addresses also take;
        #  3. the itag pinned, for an endpoint that names no stream of its own.
        attempts: list[tuple[str, str, bool]] = [
            ("as the session uses it", endpoint, True),
            ("with the range in the address", _with_query(
                endpoint, "range", f"0-{wanted_end}"), False),
        ]
        if itag and "itag=" not in endpoint:
            attempts.append(("with the stream named",
                             _with_query(endpoint, "itag", itag), True))

        for description, url, ranged in attempts:
            self._check_stop()
            try:
                if ranged:
                    response = self._client().open_range(
                        url, 0, wanted_end, self._request_headers(url))
                else:
                    response = self._client().request(
                        "GET", url, self._request_headers(url))
                try:
                    data = response.read_all()
                finally:
                    response.close()
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                self._log(f"the streaming endpoint refused this stream's "
                          f"opening bytes, asked {description} ({exc})",
                          "warning")
                continue

            if not data:
                self._log(f"the streaming endpoint answered with nothing, "
                          f"asked {description}", "warning")
                continue

            # What came back has to *be* an initialisation segment before it
            # is written at byte zero.
            #
            # The first version wrote whatever arrived. The endpoint answered
            # one request with **31 bytes** — it speaks the session protocol,
            # so a plain GET draws a framed reply, not media — and those 31
            # bytes went to the front of the file. The gap moved from "965
            # bytes at 0" to "934 bytes at 31": the opening was not filled, it
            # was poisoned, and a run that had been failing honestly began
            # failing with corruption underneath it.
            #
            # An ISOBMFF file announces itself: the second box-header word of
            # its first box is `ftyp`. Anything else is refused and the bytes
            # go in the log, which is the rule this project already had
            # (§3.14u10) and this code did not follow.
            if data[4:8] not in (b"ftyp", b"styp"):
                opening = " ".join(f"{byte:02x}" for byte in data[:16])
                self._log(f"the streaming endpoint answered {len(data):,} "
                          f"bytes that are not an initialisation segment, "
                          f"asked {description} — first bytes {opening}",
                          "warning")
                if self._describe_ump(data):
                    return          # the protocol, not the address: stop asking
                continue

            write(0, data)
            if stream is not None:
                stream.note_written(0, len(data))
                self._load_stream_index(stream, data)
            self._log(f"the streaming endpoint served this stream's opening "
                      f"{len(data):,} bytes, asked {description} — the "
                      f"session would not send them")
            return

    def _log_once(self, message: str, level: str = "info") -> None:
        """Log a line the first time this transfer produces it.

        Sixteen sessions run at once and each describes what the server told
        it. Those descriptions are identical, so the field log carried the same
        paragraph sixteen times over — around the two lines that say whether
        the download is working. The first copy is the informative one.
        """
        with self._said_lock:
            if message in self._said:
                return
            self._said.add(message)
        self._log(message, level)

    def _take_opening_from_player(self, context: dict[str, Any], write: Any,
                                  stream: Any) -> None:
        """Use the opening the page's own player received, when it has one.

        This is the route the whole of 2026-08-12 narrowed down to. The session
        will not send a stream's first bytes; a second fetch of the address is
        refused even to the page that minted it; the endpoint answers an
        ordinary GET with framed protocol. The player receives those bytes —
        it plays the video — and the page hook copies them as they arrive.

        Written only if it is an initialisation segment, checked here as well
        as at the point it was collected. Byte zero of a file is the one place
        a plausible-looking guess does the most damage.
        """
        lookup = getattr(self.engine, "lookup_opening", None)
        if lookup is None:
            return
        itag = str(context.get("itag") or "")
        if not itag:
            return
        # The page this download came from. `Download` carries `referer`,
        # `original_url` and `url` — and not `webpage_url`, which is
        # `MediaInfo`'s. Naming that one threw `AttributeError` on every
        # attempt, so the route built for exactly this never ran once.
        page = (self.download.referer or self.download.original_url
                or self.download.url)
        try:
            opening = lookup(itag, page)
        except Exception as exc:  # noqa: BLE001 - never fatal to a transfer
            self._log(f"could not consult what the player received ({exc})",
                      "warning")
            return
        if not opening:
            return
        if opening[4:8] not in (b"ftyp", b"styp"):
            self._log("what the player received for this stream does not "
                      "begin like a media file; it was not used", "warning")
            return

        write(0, opening)
        stream.note_written(0, len(opening))
        self._load_stream_index(stream, opening)
        self._log(f"the opening {len(opening):,} bytes came from what this "
                  f"page's own player received — the streaming session does "
                  f"not send them to anyone")

    def _describe_ump(self, data: bytes) -> bool:
        """Say what a framed reply contains. True when it is a protocol error.

        The endpoint speaks the session protocol, so an ordinary GET draws a
        framed answer rather than media — thirty-one bytes of it, whose text
        reads **`sabr.malformed`**. That is not a refusal of the bytes; it is
        the protocol saying the request has the wrong shape, and no rearranging
        of the address will change it. Once it has been said, the remaining
        shapes are not worth asking.
        """
        from ..extractors.sabr import iter_parts    # noqa: PLC0415
        try:
            parts = [(kind, body) for kind, body in iter_parts(data)]
        except Exception:                           # noqa: BLE001 - not framed
            return False
        if not parts:
            return False

        # Part 44 carries the reason, as text among its fields.
        failed = ""
        for kind, body in parts:
            if kind != _PART_SABR_ERROR:
                continue
            readable = "".join(
                chr(byte) if 32 <= byte < 127 else " " for byte in body)
            for word in readable.split():
                if "." in word and len(word) > 4:
                    failed = word
                    break
            failed = failed or readable.strip()

        if failed:
            self._log(f"that answer is the streaming protocol refusing the "
                      f"shape of the request — “{failed}”. This endpoint takes "
                      f"no ordinary request for these bytes.", "warning")
            return True
        self._log("that answer is a streaming-protocol reply, not media: "
                  + ", ".join(f"part {kind} ({len(body):,} bytes)"
                              for kind, body in parts))
        return False

    def _stream_header_bytes(self, context: dict[str, Any]) -> bytes:
        """The stream's opening bytes, fetched once and not written anywhere.

        The parallel path needs them for the segment index rather than for the
        file, and every worker needs the same index — so it is read here once
        instead of by each of them.
        """
        end = int(context.get("header_end") or 0)
        if end <= 0:
            return b""

        url = context.get("header_url") or ""
        if url:
            try:
                response = self._client().open_range(url, 0, end,
                                                     self._request_headers(url))
                try:
                    return response.read_all()
                finally:
                    response.close()
            except Exception:                 # noqa: BLE001 - an index is a
                return b""                    # bonus, never a dependency

        # No ordinary link — the ordinary case on YouTube now. The session
        # itself sends these bytes as its first segment, so a brief one is
        # opened for them alone.
        #
        # This is what "publishes no segment index, so it is fetched on a
        # single session" was really saying: not that the stream has no index,
        # but that this had nowhere to read one from. The index is inside the
        # opening the session sends, and without it every download runs on one
        # connection however many the user asked for.
        return self._opening_from_session(context, end)

    def _opening_from_session(self, context: dict[str, Any], end: int) -> bytes:
        """The stream's opening, from a session opened for that alone.

        Costs one short exchange and buys the segment index, which is what lets
        a track be split across sessions at all.
        """
        from ..extractors.sabr import stream_from_context   # noqa: PLC0415

        want = end + 1
        collected = bytearray()

        def write(offset: int, data: bytes) -> None:
            if offset > want:
                return                        # media, not the opening
            if len(collected) < offset + len(data):
                collected.extend(bytes(offset + len(data) - len(collected)))
            collected[offset:offset + len(data)] = data

        try:
            stream = stream_from_context(self._client(), context,
                                         self.download.user_agent)
            stream.download(write, should_stop=lambda: len(collected) >= want)
        except Exception:                     # noqa: BLE001 - an index is a
            pass                              # bonus, never a dependency
        if len(collected) < want:
            return b""
        return bytes(collected[:want])

    @staticmethod
    def _load_stream_index(stream: Any, header: bytes) -> None:
        """Give the session the exact byte-to-time map from its own header.

        The header is fetched before any media regardless, and it carries the
        stream's ``sidx``. Reading it costs one parse and removes the only
        guess in the protocol's addressing.
        """
        if stream is None or not header:
            return
        index = _index_in(header)
        if index:
            stream.index = index

    def _fetch_stream_header(self, context: dict[str, Any],
                             write: Any, stream: Any = None) -> bool:
        """Retrieve the initialisation and index segments over plain HTTP.

        ``stream`` is told what arrived, because these bytes never travel over
        its session and coverage that does not know about them reports a
        permanent gap at byte zero.

        Returns whether the file's opening is accounted for. A stream that
        names no header needs nothing; one that does and cannot get it is a
        transfer that will run to the last byte and then be refused for a hole
        at byte zero, so the caller stops there instead of spending the whole
        download to arrive at that.
        """
        end = int(context.get("header_end") or 0)
        if end <= 0:
            return True                 # this stream carries its own opening
        if stream is not None and stream.holds(0, end + 1):
            return True                 # an earlier attempt already fetched it

        url = context.get("header_url") or ""
        if not url:
            return False

        # Worth retrying: these are a few kilobytes, and losing them costs the
        # entire download.
        attempts = max(1, self.settings.get_int("max_retries", 5))
        for attempt in range(1, attempts + 1):
            self._check_stop()
            try:
                response = self._client().open_range(url, 0, end,
                                                     self._request_headers(url))
                try:
                    data = response.read_all()
                finally:
                    response.close()
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                self._log(f"Could not fetch the stream header "
                          f"({attempt}/{attempts}): {exc}", "warning")
                continue
            if data:
                write(0, data)
                if stream is not None:
                    stream.note_written(0, len(data))
                    # The same bytes carry the stream's segment index. Reading
                    # it here is what turns "continue at byte N" from an
                    # estimate into the answer the stream itself published.
                    self._load_stream_index(stream, data)
                return True
        return False

    def _probe_with_retries(self) -> Any:
        """Probe the target, retrying transient failures and rotating proxies.

        Rate limits and flaky origins are common on the very first request, so
        the probe gets the same resilience as the chunk transfers themselves.
        """
        headers = self._request_headers(self.download.url)
        max_retries = max(0, self.settings.get_int("max_retries", 5))
        backoff = self.settings.get_float("retry_backoff", 2.0)
        attempt = 0

        while True:
            self._check_stop()
            try:
                return self._client().probe(self.download.url, headers)
            except CancelledError:
                raise
            except LinkExpiredError:
                raise
            except RETRYABLE_ERRORS as exc:
                # A signed media link that is refused before a single byte has
                # moved is stale, not busy: repeating the same request cannot
                # turn a revoked link into a live one, and doing so five times
                # over just spends the retry budget to arrive at the same
                # failure. Ask the site for a fresh link instead.
                if isinstance(exc, HttpError) and exc.status in (403, 410):
                    if self._renew_link(0):
                        self._log("The link was refused before any data moved; "
                                  "a fresh one was obtained.", "warning")
                        attempt = 0
                        continue
                attempt += 1
                # A refusal is not a flaky origin, and repeating it five times
                # tells nobody anything. What the request *was* is the thing
                # that settles it, so it is named once — on the first attempt,
                # by header name only, with no value: a cookie or a token in a
                # log the user pastes into a chat is not something to publish.
                if attempt == 1 and "403" in str(exc):
                    # What actually goes on the wire, not what this method
                    # assembled: the client adds `Referer`, `Origin`, `Accept`
                    # and the cookies from its jar, and a diagnostic that lists
                    # only half of them is how the last two rounds were spent.
                    try:
                        on_the_wire = self._client()._default_headers(
                            urllib.parse.urlparse(self.download.url), headers)
                    except Exception:            # noqa: BLE001 - never fatal
                        on_the_wire = headers
                    self._log(
                        "Refused outright. This request carried: "
                        + (", ".join(sorted(on_the_wire)) or "no headers")
                        + ". A browser fetching the same address sends its own "
                        "set, and the difference is what decides a 403.",
                        "warning",
                    )
                if attempt > max_retries:
                    raise
                rotated = self.engine.handle_transport_error(exc, self.download)
                delay = min(30.0, backoff ** attempt)
                self._log(
                    f"Probe attempt {attempt}/{max_retries} failed ({exc}); "
                    f"retrying in {delay:.0f}s" + (" on a new proxy" if rotated else ""),
                    "warning",
                )
                if self._stop.wait(delay):
                    raise CancelledError("stopped")

    def _resume_invalidated(self, info: Any) -> bool:
        """Detect that the remote object is no longer the one we started."""
        download = self.download
        if download.etag and info.etag and download.etag != info.etag:
            return True
        if download.total_size and info.size and download.total_size != info.size:
            return True
        if (download.last_modified and info.last_modified
                and download.last_modified != info.last_modified):
            return True
        return False

    def _plan_byte_chunks(self) -> None:
        download = self.download
        size = download.total_size

        if not download.supports_ranges or size <= 0:
            download.mode = TransferMode.SINGLE
            with self._chunk_lock:
                self._chunks = [Chunk(index=0, start=0, end=size - 1 if size > 0 else -1)]
                self._next_chunk_index = 1
        else:
            requested = max(1, min(
                download.connections or self.settings.get_int("connections_per_download", 8),
                self.settings.get_int("max_connections_per_download", 32),
            ))
            minimum = max(1, self.settings.get_int("min_chunk_size", 1 << 20))
            count = max(1, min(requested, (size + minimum - 1) // minimum))

            span = size // count
            chunks: list[Chunk] = []
            for index in range(count):
                start = index * span
                end = size - 1 if index == count - 1 else start + span - 1
                chunks.append(Chunk(index=index, start=start, end=end))
            with self._chunk_lock:
                self._chunks = chunks
                self._next_chunk_index = count

        self._bytes_done = 0
        with self._chunk_lock:
            self.db.replace_chunks(download.id, self._chunks)
        self._log(
            f"Planned {len(self._chunks)} chunk(s) for {size:,} bytes "
            f"({'ranged' if download.supports_ranges else 'single stream'})"
        )

    def _prepare_segmented(self) -> None:
        download = self.download
        if not download.segments:
            raise IXDError("segmented download has no segments")
        if not download.filename:
            download.filename = sanitize_filename(
                (download.media_title or "video") + ".ts"
            )

        existing = self.db.load_chunks(download.id) if download.id else []
        if existing:
            with self._chunk_lock:
                self._chunks = existing
                self._next_chunk_index = max(c.index for c in existing) + 1
                self._requeue_unfinished(self._chunks)
        else:
            total = len(download.segments)
            workers = max(1, min(self._connection_count(), total))
            span = max(1, total // workers)
            bands: list[Chunk] = []
            index = 0
            start = 0
            while start < total:
                end = min(total - 1, start + span - 1)
                if index == workers - 1:
                    end = total - 1
                bands.append(Chunk(index=index, start=start, end=end))
                start = end + 1
                index += 1
            with self._chunk_lock:
                self._chunks = bands
                self._next_chunk_index = len(bands)
                self.db.replace_chunks(download.id, bands)
            self._log(f"Planned {len(bands)} segment band(s) over {total} segments")

        parts_dir = Path(self._parts_dir())
        parts_dir.mkdir(parents=True, exist_ok=True)
        self._reconcile_segment_bands()

    def _parts_dir(self) -> str:
        download = self.download
        if not download.temp_path:
            download.temp_path = str(
                config.TEMP_DIR / f"{download.id}-{sanitize_filename(download.filename)}{PART_SUFFIX}"
            )
        return download.temp_path + ".parts"

    def _segment_path(self, index: int) -> str:
        return os.path.join(self._parts_dir(), f"seg_{index:06d}")

    def _reconcile_segment_bands(self) -> None:
        """Trust the filesystem: count the part files that already exist."""
        total_bytes = 0
        with self._chunk_lock:
            for band in self._chunks:
                done = 0
                for index in range(band.start, band.end + 1):
                    path = self._segment_path(index)
                    if os.path.exists(path):
                        done += 1
                        total_bytes += os.path.getsize(path)
                    else:
                        break
                band.downloaded = done
                if band.size > 0 and done >= band.size:
                    band.status = ChunkStatus.DONE
        self._bytes_done = total_bytes

    def _allocate_file(self, size: int) -> None:
        self._allocate_path(self.download.temp_path, size)

    @staticmethod
    def _allocate_path(target: str, size: int) -> None:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "wb") as handle:
                if size > 0:
                    handle.truncate(size)
        elif size > 0 and path.stat().st_size != size:
            with open(path, "r+b") as handle:
                handle.truncate(size)

    # ------------------------------------------------------------------
    # transfer
    # ------------------------------------------------------------------
    def _transfer(self) -> None:
        while True:
            with self._chunk_lock:
                pending = [c for c in self._chunks if c.status is not ChunkStatus.DONE]
            if not pending:
                return

            if self.download.mode is TransferMode.SABR:
                # The origin drives this one; there is no work to divide up.
                self._transfer_sabr()
                return

            if self.download.mode is TransferMode.SINGLE:
                worker_count = 1
            else:
                # Segments and byte ranges are both "pieces this download may
                # fetch at once", so they answer to the same setting.
                worker_count = min(self._connection_count(), len(pending))
            worker_count = max(1, worker_count)

            workers = [
                threading.Thread(
                    target=self._worker_loop, args=(i,),
                    name=f"ixd-worker-{self.download.id}-{i}", daemon=True,
                )
                for i in range(worker_count)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            if self._replan.is_set() and not self._stop.is_set():
                # Every worker has stopped and the chunk map is void. Lay the
                # file out again and run the whole thing once more.
                self._replan.clear()
                self._restart_without_ranges()
                continue

            self._replan.clear()
            if self._error is not None:
                raise self._error
            return

    def _restart_without_ranges(self) -> None:
        """Give up on ranges and pull the file down one connection, from zero.

        Some origins advertise `Accept-Ranges: bytes`, answer the probe
        agreeably, and then send `200` with the entire body for every `Range`
        they are given — gameforge.com's installer host is one (§3.81). There
        is no partial content to be had from such a server, so nothing already
        on disk can be continued and nothing that arrived at an offset can be
        trusted: the file starts over.
        """
        download = self.download
        self._log(
            "The server advertised byte ranges and then ignored one — "
            "restarting on a single connection from the beginning",
            "warning",
        )
        download.supports_ranges = False
        download.mode = TransferMode.SINGLE
        self._error = None
        with self._chunk_lock:
            self._chunks = []
            self._next_chunk_index = 0
        self._bytes_done = 0
        try:
            if download.temp_path and os.path.exists(download.temp_path):
                os.remove(download.temp_path)
        except OSError:
            pass
        self._plan_byte_chunks()
        self._allocate_file(download.total_size)
        self.db.update_download(download)
        self._flush_progress()

    def _worker_loop(self, worker_id: int) -> None:
        try:
            while not self._stop.is_set() and not self._replan.is_set():
                chunk = self._acquire_chunk()
                if chunk is None:
                    return
                try:
                    if self.download.mode is TransferMode.SEGMENTED:
                        self._transfer_segment_band(chunk)
                    else:
                        self._transfer_byte_range(chunk)
                except _ReplanRequested:
                    # The chunk map is about to be thrown away. Nothing to
                    # record and nothing to report.
                    return
                except CancelledError:
                    with self._chunk_lock:
                        if chunk.status is ChunkStatus.ACTIVE:
                            chunk.status = ChunkStatus.PENDING
                    return
                except Exception as exc:  # noqa: BLE001
                    with self._chunk_lock:
                        chunk.status = ChunkStatus.FAILED
                    if self._error is None:
                        self._error = exc
                    if isinstance(exc, LinkExpiredError):
                        self._link_expired = True
                        self.download.error = str(exc)
                    self._stop.set()
                    return
        finally:
            pass

    def _acquire_chunk(self) -> Chunk | None:
        """Take a pending chunk, or steal the tail of the busiest active one."""
        with self._chunk_lock:
            for chunk in self._chunks:
                if chunk.status is ChunkStatus.PENDING:
                    chunk.status = ChunkStatus.ACTIVE
                    return chunk

            if not self.settings.get_bool("dynamic_chunking", True):
                return None
            if self.download.mode is TransferMode.SINGLE:
                return None

            if self.download.mode is TransferMode.SEGMENTED:
                minimum_split = 2      # at least one segment must remain on each side
                threshold = 4
            else:
                minimum_split = max(1, self.settings.get_int("min_chunk_size", 1 << 20))
                threshold = max(
                    minimum_split * 2,
                    self.settings.get_int("chunk_split_threshold", 4 << 20),
                )

            candidate: Chunk | None = None
            for chunk in self._chunks:
                if chunk.status is not ChunkStatus.ACTIVE or chunk.end < 0:
                    continue
                if chunk.remaining < threshold:
                    continue
                if candidate is None or chunk.remaining > candidate.remaining:
                    candidate = chunk

            if candidate is None:
                return None

            # Split the *unfetched* tail in half and hand the second half over.
            cursor = candidate.cursor
            tail = candidate.end - cursor + 1
            if tail < minimum_split * 2:
                return None
            split_at = cursor + tail // 2
            if split_at <= cursor or split_at > candidate.end:
                return None

            old_end = candidate.end
            candidate.end = split_at - 1        # the running worker notices and stops
            stolen = Chunk(
                index=self._next_chunk_index,
                start=split_at,
                end=old_end,
                status=ChunkStatus.ACTIVE,
            )
            self._next_chunk_index += 1
            self._chunks.append(stolen)
            self.events.emit(EventType.CHUNKS_CHANGED, download_id=self.download.id)
            return stolen

    # -- byte-range transfers -------------------------------------------
    def _range_is_capped(self, chunk: Chunk) -> bool:
        """Is this a 403 that applies only past a certain offset?

        Expiry and a range cap both surface as a 403 on a URL that was working,
        so they cannot be told apart from the failure alone — and they need
        opposite responses. Expiry is fixed by a fresh link; a cap is not,
        because a newly issued URL is restricted at the same point, and
        prompting for one wastes the user's time.

        The two are separated by asking the one question that distinguishes
        them: is the *start* of the file still being served? If it is, the URL
        is alive and the refusal is about the offset. If it is not, the URL
        itself has been revoked.

        Deliberately no precondition about what this download has already
        transferred. A cap can sit below the very first chunk boundary, so the
        workers may all fail before any progress is recorded — requiring
        evidence of earlier success would misreport exactly that case, which is
        the common one for a small audio track.
        """
        # The position that was refused is where the transfer has reached, not
        # where its chunk began: with a single connection the chunk starts at
        # zero and the refusal happens much later.
        if chunk.cursor <= 0:
            return False
        try:
            response = self._client().open_range(
                self.download.url, 0, 0, self._request_headers(self.download.url)
            )
        except Exception:  # noqa: BLE001 - any refusal means "not capped"
            return False
        try:
            return response.status in (200, 206)
        finally:
            response.close()

    _MAX_RENEWALS = _MAX_LINK_RENEWALS

    def _renew_link(self, offset: int) -> bool:
        """Obtain a link whose grant covers ``offset``.

        Returns True when the download URL was replaced with one that serves
        this position. Each renewal is checked before being accepted, because
        a link that does not cover the offset is no better than the dead one.
        """
        recipe = (self.download.media_context or {}).get("refresh") or {}
        renew = getattr(self.engine, "renew_media_url", None)
        if not recipe or renew is None:
            return False
        if self._renewals >= self._MAX_RENEWALS:
            return False

        total = self.download.total_size or 1
        duration = float(recipe.get("duration") or 0)
        # Where this byte falls in the running time is what the site is asked
        # for; a small lead makes sure the grant starts before it rather than
        # exactly on the boundary.
        position = (offset / total) * duration if duration else 0.0

        for lead in (0, -5, 5, 15, 30):
            self._renewals += 1
            if self._renewals > self._MAX_RENEWALS:
                return False
            seconds = max(0.0, min(duration - 1 if duration else 0.0, position + lead))
            try:
                fresh = renew(recipe, seconds)
            except Exception as exc:  # noqa: BLE001 - a failed renewal is not fatal
                self._log(f"Renewal at {seconds:.0f}s failed: {exc}")
                continue
            if not fresh:
                self._log(f"Renewal at {seconds:.0f}s returned no link")
                continue
            try:
                probe = self._client().open_range(fresh, offset, offset + 8191,
                                                  self._request_headers(fresh))
            except Exception as exc:  # noqa: BLE001 - this link does not cover it
                self._log(f"Fresh link from {seconds:.0f}s does not cover "
                          f"byte {offset:,}: {exc}")
                continue
            try:
                covered = bool(probe.read(1))
            finally:
                probe.close()
            if not covered:
                continue
            self.download.url = fresh
            if self.download.id is not None:
                self.db.update_download_fields(self.download.id, url=fresh)
            self._log(f"Obtained a fresh link covering byte {offset:,}")
            return True
        return False

    def _capped_error(self, chunk: Chunk) -> RangeCappedError:
        """Describe the limit using only what was actually observed.

        Parallel workers discover the cap at whichever chunk boundary happens
        to sit above it, which is not the cap itself. Quoting that boundary as
        "the server allows N bytes" would understate the limit — sometimes by a
        lot — so the message reports the bracket the evidence supports: the
        highest offset that was served, and the lowest that was refused.
        """
        with self._chunk_lock:
            served = max(
                (other.start + other.downloaded
                 for other in self._chunks if other.downloaded > 0),
                default=0,
            )
            self._refused_from = min(
                self._refused_from or chunk.cursor, chunk.cursor
            )

        total = self.download.total_size
        share = f" — {self._refused_from * 100 // total}% of the file" if total else ""
        # A racing worker can hit the cap before any progress is recorded, in
        # which case there is no lower bound worth quoting.
        lower = (f"; the start of the file is still served, and "
                 f"{served:,} bytes of it arrived" if served else
                 "; the start of the file is still served")
        return RangeCappedError(
            403,
            f"the server refuses any part of this file from byte "
            f"{self._refused_from:,} onward{share}{lower}. The link has not "
            "expired — a freshly issued one is restricted at exactly the same "
            "point — so neither a retry nor a replacement link will help. This "
            "link was issued with an opening-portion-only grant. If it came "
            "from a video site, play the video for a moment and take the "
            "stream from the download panel's “Already loaded by the player” "
            "list instead: those are the ones the site's own player "
            "negotiated, and they carry no such restriction.",
            self.download.url,
        )

    def _transfer_byte_range(self, chunk: Chunk) -> None:
        download = self.download
        limiters = self._limiters()
        # 64 KiB was sixteen reads and sixteen rate-limiter round trips per
        # megabyte, per connection — with sixteen connections that is a great
        # deal of bookkeeping between the socket and the disk. A quarter of a
        # megabyte cuts it fourfold and costs a quarter-megabyte of memory per
        # connection.
        buffer_size = max(8192, self.settings.get_int("read_buffer", 1 << 18))
        max_retries = max(0, self.settings.get_int("max_retries", 5))
        backoff = self.settings.get_float("retry_backoff", 2.0)
        attempt = 0

        while True:
            self._check_stop()
            if self._replan.is_set():
                raise _ReplanRequested()
            with self._chunk_lock:
                if chunk.size >= 0 and chunk.downloaded >= chunk.size:
                    chunk.status = ChunkStatus.DONE
                    return
                if not download.supports_ranges and chunk.downloaded > 0:
                    # No range support and the stream broke: the only way
                    # forward is to pull the whole body again from zero.
                    self._log(
                        "Connection dropped on a non-resumable stream — restarting",
                        "warning",
                    )
                    chunk.downloaded = 0
                start = chunk.cursor
                end = chunk.end
                if end >= 0:
                    end = min(end, start + _MAX_RANGE_SPAN - 1)
                progress_before = chunk.downloaded

            response = None
            try:
                client = self._client()
                headers = self._request_headers(download.url)
                if download.supports_ranges:
                    response = self._track(client.open_range(
                        download.url, start, end if end >= 0 else None, headers,
                        had_prior_success=self._had_success,
                    ))
                    if response.status == 200 and start > 0:
                        # The origin advertised ranges and sent the whole file
                        # anyway. Nothing has been written — the body is not
                        # what this chunk asked for — so stop every worker and
                        # let `_transfer` start again as a single stream.
                        self._replan.set()
                        raise _ReplanRequested()
                else:
                    response = self._track(client.get(download.url, headers))

                self._pump_response(response, chunk, limiters, buffer_size)

                with self._chunk_lock:
                    if chunk.size < 0:
                        # Unknown length: whatever arrived is the whole file.
                        chunk.end = chunk.start + chunk.downloaded - 1
                        download.total_size = chunk.downloaded
                    if chunk.size >= 0 and chunk.downloaded >= chunk.size:
                        chunk.status = ChunkStatus.DONE
                        return
                    advanced = chunk.downloaded > progress_before

                if advanced:
                    # Short read: loop round and ask for the remainder.
                    attempt = 0
                    continue

                # The server accepted the request but sent nothing. Treat a
                # stalled response like any other failure so we cannot spin.
                attempt += 1
                if attempt > max_retries:
                    raise NetworkError(
                        f"chunk {chunk.index} stalled: no data after {attempt} attempts"
                    )
                if self._stop.wait(min(30.0, backoff ** attempt)):
                    raise CancelledError("stopped")
                continue

            except CancelledError:
                raise
            except LinkExpiredError:
                # An expired link and an exhausted grant look identical from
                # here: both answer 403. Either way the cure is the same one —
                # ask the site for a link that covers this position.
                if self._range_is_capped(chunk):
                    if self._renew_link(chunk.cursor):
                        attempt = 0
                        continue
                    raise self._capped_error(chunk) from None
                if self._renew_link(chunk.cursor):
                    attempt = 0
                    continue
                raise
            except RETRYABLE_ERRORS as exc:
                # A 403 confined to the tail of the file means the link's grant
                # has run out. Signed media links are issued covering only part
                # of a long file, so the answer is to obtain another one that
                # covers this position — not to retry the same dead link.
                if (isinstance(exc, HttpError) and exc.status == 403
                        and self._range_is_capped(chunk)):
                    if self._renew_link(chunk.cursor):
                        attempt = 0
                        continue
                    raise self._capped_error(chunk) from None
                attempt += 1
                if attempt > max_retries:
                    raise
                rotated = self.engine.handle_transport_error(exc, download)
                delay = min(30.0, backoff ** attempt)
                self._log(
                    f"Chunk {chunk.index} attempt {attempt}/{max_retries} failed "
                    f"({exc}); retrying in {delay:.0f}s"
                    + (" on a new proxy" if rotated else ""),
                    "warning",
                )
                if self._stop.wait(delay):
                    raise CancelledError("stopped")
            finally:
                if response is not None:
                    self._untrack(response)
                    response.close()

    def _pump_response(self, response: Any, chunk: Chunk, limiters: CompositeLimiter,
                       buffer_size: int) -> None:
        """Stream a response body into the sparse output file."""
        with open(self.download.temp_path, "r+b") as handle:
            with self._chunk_lock:
                handle.seek(chunk.cursor)
            while True:
                self._check_stop()
                if self._replan.is_set():
                    raise _ReplanRequested()
                with self._chunk_lock:
                    remaining = chunk.size - chunk.downloaded if chunk.size >= 0 else -1
                if remaining == 0:
                    return
                want = buffer_size if remaining < 0 else min(buffer_size, remaining)
                want = min(want, limiters.suggested_read_size(buffer_size))

                if not limiters.consume(want, self._stop):
                    raise CancelledError("stopped")

                data = response.read(want)
                if not data:
                    return
                handle.write(data)

                with self._chunk_lock:
                    chunk.downloaded += len(data)
                self._had_success = True
                self._meter.record(len(data))

    # -- segment-band transfers -----------------------------------------
    def _transfer_segment_band(self, band: Chunk) -> None:
        download = self.download
        limiters = self._limiters()
        max_retries = max(0, self.settings.get_int("max_retries", 5))
        backoff = self.settings.get_float("retry_backoff", 2.0)

        while True:
            self._check_stop()
            with self._chunk_lock:
                if band.downloaded >= band.size:
                    band.status = ChunkStatus.DONE
                    return
                index = band.start + band.downloaded
                if index > band.end:
                    band.status = ChunkStatus.DONE
                    return

            segment = download.segments[index]
            target = self._segment_path(index)
            if os.path.exists(target):
                with self._chunk_lock:
                    band.downloaded += 1
                continue

            attempt = 0
            while True:
                self._check_stop()
                try:
                    payload = self._fetch_segment(segment, limiters)
                    temporary = target + ".tmp"
                    with open(temporary, "wb") as handle:
                        handle.write(payload)
                    os.replace(temporary, target)   # rename = "this segment is complete"
                    self._bytes_done += len(payload)
                    self._had_success = True
                    with self._chunk_lock:
                        band.downloaded += 1
                    self._estimate_segmented_total()
                    break
                except CancelledError:
                    raise
                except LinkExpiredError:
                    raise
                except RETRYABLE_ERRORS as exc:
                    attempt += 1
                    if attempt > max_retries:
                        raise
                    rotated = self.engine.handle_transport_error(exc, download)
                    delay = min(30.0, backoff ** attempt)
                    self._log(
                        f"Segment {index} attempt {attempt}/{max_retries} failed "
                        f"({exc}); retrying in {delay:.0f}s"
                        + (" on a new proxy" if rotated else ""),
                        "warning",
                    )
                    if self._stop.wait(delay):
                        raise CancelledError("stopped")

    def _fetch_segment(self, segment: MediaSegment, limiters: CompositeLimiter) -> bytes:
        client = self._client()
        headers = self._request_headers(segment.url)
        if segment.byte_range:
            response = self._track(client.open_range(
                segment.url, segment.byte_range[0], segment.byte_range[1], headers,
                had_prior_success=self._had_success,
            ))
        else:
            response = self._track(client.get(segment.url, headers))

        try:
            buffer = bytearray()
            block = max(8192, self.settings.get_int("read_buffer", 1 << 16))
            while True:
                self._check_stop()
                want = min(block, limiters.suggested_read_size(block))
                if not limiters.consume(want, self._stop):
                    raise CancelledError("stopped")
                data = response.read(want)
                if not data:
                    break
                buffer += data
                self._meter.record(len(data))
        finally:
            self._untrack(response)
            response.close()

        payload = bytes(buffer)
        if segment.key_url:
            key = self._segment_key(segment.key_url)
            iv = parse_hex_iv(segment.key_iv, segment.index)
            payload = aes_cbc_decrypt(key, iv, payload)
        return unwrap_disguised_segment(payload)

    def _segment_key(self, key_url: str) -> bytes:
        with self._key_lock:
            cached = self._key_cache.get(key_url)
        if cached is not None:
            return cached
        key = self._client().get_bytes(key_url, self._request_headers(key_url))
        if len(key) not in (16, 24, 32):
            raise IXDError(f"unexpected HLS key length: {len(key)} bytes")
        with self._key_lock:
            self._key_cache[key_url] = key
        return key

    def _estimate_segmented_total(self) -> None:
        """Live size estimate so the progress bar means something for HLS."""
        done = sum(c.downloaded for c in self._chunks)
        total = len(self.download.segments)
        if done > 0 and total > 0:
            self.download.total_size = int(self._bytes_done / done * total)

    # ------------------------------------------------------------------
    # progress persistence
    # ------------------------------------------------------------------
    def _flush_loop(self) -> None:
        interval = max(0.25, self.settings.get_float("progress_flush_interval", 1.0))
        while not self._stop.wait(interval):
            self._flush_progress()
        self._flush_progress()

    def _flush_progress(self) -> None:
        download = self.download
        if download.id is None:
            return
        with self._chunk_lock:
            snapshot = [
                Chunk(index=c.index, start=c.start, end=c.end,
                      downloaded=c.downloaded, status=c.status)
                for c in self._chunks
            ]
        if download.mode is TransferMode.SEGMENTED:
            download.downloaded = self._bytes_done
        else:
            download.downloaded = sum(c.downloaded for c in snapshot)

        download.speed = self._meter.speed
        download.eta = self._meter.eta(download.remaining)

        try:
            self.db.flush_chunk_progress(download.id, snapshot)
            self.db.update_download_fields(
                download.id, downloaded=download.downloaded, total_size=download.total_size
            )
        except Exception:
            pass  # a transient DB hiccup must not abort a live transfer

        # What the connection bars draw. During a parallel server-driven pass
        # the real chunk map is one span per *track*, which would show a
        # sixteen-session download as a single bar — so the live worker
        # stretches are published instead, which is what the user set and what
        # is actually happening.
        workers = list(self._sabr_workers)
        download.live_workers = len(workers)
        download.chunks = workers or snapshot
        self.events.emit(
            EventType.DOWNLOAD_PROGRESS,
            download_id=download.id,
            downloaded=download.downloaded,
            total=download.total_size,
            speed=download.speed,
            eta=download.eta,
            chunks=[
                {"index": c.index, "progress": c.progress, "status": c.status.value}
                for c in snapshot
            ],
        )

    def _live_chunks(self) -> list[Chunk]:
        with self._chunk_lock:
            return list(self._chunks)

    # ------------------------------------------------------------------
    # completion
    # ------------------------------------------------------------------
    def _verify_completeness(self) -> None:
        """Refuse to publish a file unless every byte is actually accounted for.

        A sparse output file reads back as zeros where nothing was written, so
        an unnoticed missing chunk would produce a plausible-looking file that
        is silently corrupt.  This is the last line of defence against that.
        """
        download = self.download

        if download.mode is TransferMode.SEGMENTED:
            missing = [
                index for index in range(len(download.segments))
                if not os.path.exists(self._segment_path(index))
            ]
            if missing:
                raise IXDError(
                    f"{len(missing)} segment(s) missing (first is #{missing[0]}); "
                    "refusing to assemble a partial file"
                )
            return

        with self._chunk_lock:
            chunks = sorted(self._chunks, key=lambda c: c.start)

        for chunk in chunks:
            complete = (
                chunk.status is ChunkStatus.DONE
                and (chunk.size < 0 or chunk.downloaded >= chunk.size)
            )
            if not complete:
                raise IXDError(
                    f"chunk {chunk.index} is incomplete "
                    f"({chunk.downloaded}/{chunk.size} bytes, {chunk.status.value}); "
                    "refusing to assemble a partial file"
                )

        # A paired quality holds one chunk per track, and the tracks are
        # separate files that are joined afterwards — each starts at byte zero,
        # so they do not tile a single span and contiguity says nothing about
        # them. Their completeness was already established above.
        if (download.mode is TransferMode.SABR
                and (download.sabr_context or {}).get("audio")):
            return

        total = download.total_size
        if total > 0:
            cursor = 0
            for chunk in chunks:
                if chunk.start != cursor:
                    raise IXDError(
                        f"gap in byte coverage at {cursor}–{chunk.start - 1}; "
                        "refusing to assemble a partial file"
                    )
                cursor = chunk.end + 1
            if cursor != total:
                raise IXDError(
                    f"byte coverage ends at {cursor} but the file is {total} bytes"
                )

    def _final_path(self) -> str:
        """Where the finished file belongs.

        A name is only made unique against files this download did not write.
        Passing it through unconditionally meant a transfer that reached this
        point twice — a resume, or a retry after a failure — set its own
        earlier output aside and published a second copy beside it, leaving two
        files where one was asked for and a join that could pick the wrong one.
        """
        download = self.download
        candidate = os.path.join(download.dest_dir, download.filename)
        if download.completed_at and os.path.exists(candidate):
            return candidate
        return unique_path(download.dest_dir, download.filename)

    def _join_tracks(self) -> None:
        """Combine the video and audio tracks this download fetched."""
        audio_temp = self._audio_temp_path()
        if not (self.download.sabr_context or {}).get("audio"):
            return
        if not os.path.isfile(audio_temp):
            return

        from .muxing import MuxError, combine        # noqa: PLC0415

        self.download.stage = "Joining video and audio"
        self._set_status(DownloadStatus.ASSEMBLING)
        combined = f"{self.download.temp_path}.joined"
        try:
            # Which muxer is decided by what the files are, not by the name
            # the site gave the stream: above 1080p30 the pair is VP9 and Opus
            # in WebM, which no MP4 can hold.
            combine(self.download.temp_path, audio_temp, combined)
        except (MuxError, OSError) as exc:
            # Publishing the video alone would hand over a silent film under
            # the name of the quality that was asked for — a file that opens,
            # plays, and is wrong in the one way the user will not check for.
            # Both tracks are on disk and complete, so nothing is lost by
            # stopping here and saying so; the join is what failed, and it can
            # be attempted again without fetching a byte.
            raise IXDError(
                f"both tracks were downloaded but could not be combined into "
                f"one file: {exc}. They are kept at "
                f"{self.download.temp_path} and {audio_temp}."
            ) from exc
        self.download.stage = ""
        os.replace(combined, self.download.temp_path)
        try:
            os.remove(audio_temp)
        except OSError:
            pass

    def _finalize(self) -> None:
        download = self.download
        # Everything from here on is work on bytes already fetched — checking,
        # assembling, rewrapping, joining a pair. It uses no connections, so it
        # must not hold a download slot: a 20-second rewrap kept the next
        # download sitting in the queue with the network idle, which the field
        # log shows exactly (#150 rewrapped 20:28:54–20:29:15, #151 queued at
        # 20:28:58 and not started until 20:29:16).
        self._postprocessing = True
        self.engine.slot_released()
        self._verify_completeness()

        if download.mode is TransferMode.SEGMENTED:
            download.stage = "Assembling segments"
            self._set_status(DownloadStatus.ASSEMBLING)
            self._assemble_segments()
            download.stage = ""

        self._flush_progress()

        # A paired quality is two tracks that become one file. Joining them
        # here — inside the download that fetched both — is what keeps it a
        # single item: one row, one progress bar, one file, and no second
        # transfer appearing to start once the first has finished.
        self._join_tracks()

        Path(download.dest_dir).mkdir(parents=True, exist_ok=True)
        final_path = self._final_path()
        try:
            os.replace(download.temp_path, final_path)
        except OSError:
            # Crossing a filesystem boundary needs a copy.
            shutil.move(download.temp_path, final_path)

        download.filename = os.path.basename(final_path)
        download.total_size = os.path.getsize(final_path)
        download.downloaded = download.total_size
        self.db.update_download(download)

        self._verify(final_path)

        download.completed_at = time.time()
        download.status = DownloadStatus.COMPLETED
        download.speed = 0.0
        self.db.update_download(download)
        self._log(f"Completed: {final_path}")
        self.events.emit(
            EventType.DOWNLOAD_COMPLETED,
            download_id=download.id,
            path=final_path,
            hash_status=download.hash_status.value,
        )

    def _assemble_segments(self) -> None:
        download = self.download
        output = download.temp_path
        total = len(download.segments)
        container = ""
        self._log(f"Assembling {total} segments")
        with open(output, "wb") as sink:
            for index in range(total):
                self._check_stop()
                path = self._segment_path(index)
                if not os.path.exists(path):
                    raise IXDError(f"segment {index} is missing; cannot assemble")
                with open(path, "rb") as source:
                    if index == 0:
                        opening = source.read(1024)
                        self._log(self._describe_opening(opening))
                        self._reject_non_media(opening)
                        self._require_recognisable(opening)
                        self._name_after_its_bytes(opening)
                        source.seek(0)
                        container = self.container_of(opening)
                    shutil.copyfileobj(source, sink, 1 << 20)
        shutil.rmtree(self._parts_dir(), ignore_errors=True)
        # Only when the name that was asked for says so. The panel offers a
        # transport stream twice — as the site serves it, and as an MP4 — and
        # the extension a person chose is the whole of the instruction. Rewrapping
        # unconditionally took the choice away from anyone who wanted the
        # original.
        wanted = self.download.filename.rsplit(".", 1)[-1].lower()
        if container == "ts" and wanted == "mp4":
            self._remux_transport_stream()

    def _remux_transport_stream(self) -> None:
        """Rewrite an assembled transport stream as an MP4.

        HLS delivers MPEG-TS on a great many sites, and a concatenated `.ts` is
        correct, playable, and refused by name by a good half of the world's
        players — it also carries no seek index at all. Every commercial
        download manager converts it, and so does this now: the coded frames are
        copied byte for byte and only the packaging around them changes.

        Failure is not fatal. A transport stream that cannot be described in an
        MP4 — an unusual codec, a shape this does not read — is kept as it is,
        because a `.ts` that plays is worth more than an `.mp4` that does not.
        """
        if not self.settings.get_bool("remux_transport_streams", True):
            return
        import struct                                          # noqa: PLC0415

        from .mp4 import Mp4Error                              # noqa: PLC0415
        from .mpegts import TsError, remux                      # noqa: PLC0415

        source = self.download.temp_path
        target = f"{source}.mp4"
        self.download.stage = "Rewrapping as MP4"
        self._set_status(DownloadStatus.ASSEMBLING)
        # Rewrapping a two-hour film moves a couple of gigabytes, and a window
        # that says nothing for that long is a window that has hung. The bar is
        # driven from the bytes actually written.
        total = max(self.download.total_size, 1)

        def written(count: int) -> None:
            self.download.downloaded = min(count, total)
            self.events.emit(
                EventType.DOWNLOAD_PROGRESS,
                download_id=self.download.id,
                downloaded=self.download.downloaded,
                total=total,
                speed=0.0,
            )

        try:
            remux(source, target, on_progress=written)
        except (TsError, Mp4Error, OSError, struct.error, IndexError) as exc:
            for leftover in (target,):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            self.download.stage = ""
            self._log(
                f"Kept as a transport stream: it could not be rewritten as an "
                f"MP4 ({exc}). The file plays; only its container is the one "
                "the site chose.", "warning",
            )
            return
        self.download.stage = ""
        os.replace(target, source)
        stem = self.download.filename.rsplit(".", 1)[0]
        renamed = f"{stem}.mp4"
        self._log(
            "Rewrapped the transport stream as MP4 — every frame copied, "
            f"nothing re-encoded — and saved as “{renamed}”."
        )
        self.download.filename = renamed
        self.db.update_download_fields(self.download.id, filename=renamed)

    #: What the first bytes of a stream's first piece look like when the piece
    #: is not media at all.
    _NOT_MEDIA_OPENINGS = (
        (b"<!DOCTYPE", "an HTML page"),
        (b"<html", "an HTML page"),
        (b"<HTML", "an HTML page"),
        (b"#EXTM3U", "another playlist"),
        (b"<?xml", "an XML document"),
        (b"{", "a JSON document"),
    )

    #: What a container looks like in its first bytes. A segmented stream is
    #: whatever its pieces are, and the pieces are the only honest witness: a
    #: master playlist declares codecs and not containers, so the extension is a
    #: guess made before the playlist behind it was read.
    _CONTAINER_SIGNATURES = (
        (b"\x47", "ts"),                    # MPEG-TS sync byte
        (b"\x1a\x45\xdf\xa3", "webm"),      # EBML
        (b"OggS", "ogg"),
        (b"fLaC", "flac"),
        (b"ID3", "mp3"),
        (b"RIFF", "wav"),
    )

    @classmethod
    def container_of(cls, opening: bytes) -> str:
        """The container these bytes are, or ``""`` when nothing is recognised.

        ISOBMFF is recognised by any of its opening boxes, not only ``ftyp``: a
        fragmented stream's *media* segments begin ``styp`` or ``moof``, and
        only the initialisation segment carries ``ftyp``. Reading just the one
        box meant every fragmented HLS stream without an ``EXT-X-MAP`` came out
        unrecognised.
        """
        if len(opening) >= 8 and opening[4:8] in (
                b"ftyp", b"styp", b"moof", b"sidx", b"moov", b"mdat", b"free",
                b"skip", b"emsg"):
            return "webm" if opening[8:12] == b"webm" else "mp4"
        # ADTS: twelve set bits, then a two-bit version and a two-bit layer.
        if len(opening) >= 2 and opening[0] == 0xFF and (opening[1] & 0xF0) == 0xF0:
            return "aac"
        for marker, name in cls._CONTAINER_SIGNATURES:
            if opening.startswith(marker):
                return name
        return ""

    def _describe_opening(self, opening: bytes) -> str:
        """What the assembled stream actually turned out to be.

        Written to the log unconditionally. "It downloads and will not play" has
        half a dozen causes that are indistinguishable from outside, and the
        first sixteen bytes separate most of them in one line — so they are
        recorded rather than asked for.
        """
        head = opening[:16]
        readable = " ".join(f"{byte:02x}" for byte in head)
        found = self.container_of(opening)
        return f"first bytes {readable} — {found.upper() if found else 'unrecognised'}"

    def _name_after_its_bytes(self, opening: bytes) -> None:
        """Correct the output's extension when the bytes disagree with it.

        Except where the disagreement is the point: a transport stream asked for
        as `.mp4` is about to become one, and renaming it to `.ts` first would
        undo the choice.

        A file whose contents are right and whose name is wrong is refused by
        most players and reported as a broken download — the bytes are never
        examined, because the name already said what the file was. This is the
        last place the truth is available, so it is where the name is settled.
        """
        actual = self.container_of(opening)
        if not actual:
            return
        current = self.download.filename.rsplit(".", 1)
        if (actual == "ts" and len(current) == 2
                and current[1].lower() == "mp4"
                and self.settings.get_bool("remux_transport_streams", True)):
            return
        if len(current) == 2 and current[1].lower() == actual:
            return
        # `.ts` and `.mp4` are the pair this actually happens between; an audio
        # container inside an `.m4a` name is the same file by another spelling.
        if actual == "mp4" and len(current) == 2 and current[1].lower() in ("m4a", "m4v"):
            return
        stem = current[0] if len(current) == 2 else self.download.filename
        renamed = f"{stem}.{actual}"
        self._log(
            f"The stream turned out to be {actual.upper()} rather than "
            f"{(current[1] if len(current) == 2 else '?')}, so it is saved as "
            f"“{renamed}” — the same bytes under a name a player will open."
        )
        self.download.filename = renamed
        self.db.update_download_fields(self.download.id, filename=renamed)

    def _require_recognisable(self, opening: bytes) -> None:
        """Refuse a stream whose first piece is not any container we know.

        Media always announces itself: a transport stream begins with its sync
        byte, ISOBMFF with a box, Matroska with EBML, AAC with a frame header.
        Bytes that are none of those are not media, and the two ways that
        happens are an encrypted stream decrypted with the wrong key or IV — the
        output is then indistinguishable from noise and every existing guard
        passes it — and a piece that was never media in the first place.

        Publishing it produces a file of the right size and name that plays
        nothing, discovered only by trying to watch it. Better to say so.
        """
        if self.container_of(opening):
            return
        encrypted = any(segment.key_url for segment in self.download.segments)
        cause = (
            "the pieces are encrypted and the key or its IV did not decrypt them"
            if encrypted else
            "the pieces are not media of any kind this can recognise"
        )
        raise IXDError(
            f"this stream cannot be assembled into a playable file: {cause}. "
            f"{self._describe_opening(opening)}. Nothing has been published — a "
            "file of the right size that plays nothing is worse than none."
        )

    def _reject_non_media(self, opening: bytes) -> None:
        """Refuse to publish a stream whose pieces are not media.

        A segment fetch that is answered with an error page, a login wall or a
        further playlist still *succeeds* as far as the transfer is concerned:
        the part file exists, so the completeness guard is satisfied and the
        pieces are concatenated into a file with the right name, the expected
        size and nothing playable in it. That is the worst shape a failure can
        take, because it is only discovered when someone tries to watch it.

        Checked on the first piece only: if the stream is media at all, it is
        media there, and reading further would cost a pass over the whole file.
        """
        head = opening.lstrip()[:16]
        for marker, what in self._NOT_MEDIA_OPENINGS:
            if not head.startswith(marker):
                continue
            raise IXDError(
                f"the pieces of this stream are {what}, not media — the origin "
                "answered the segment requests with something else. The usual "
                "cause is a link that only works from the page it came from: "
                "start the video and use the download panel on the page itself."
            )

    def _verify(self, path: str) -> None:
        download = self.download
        headers = digest_headers(download.server_digest)

        wants_header_check = self.settings.get_bool("auto_verify_headers", True) and headers
        if not download.expected_hash and not wants_header_check:
            download.hash_status = HashStatus.UNKNOWN
            return

        self._set_status(DownloadStatus.VERIFYING)
        self._log("Verifying integrity")
        try:
            result = verify_file(
                path,
                expected_hash=download.expected_hash,
                expected_algorithm=download.expected_hash_algo,
                server_headers=headers if wants_header_check else None,
                extra_algorithms=self.settings.get("hash_algorithms") or ["sha256"],
                chunk_size=self.settings.get_int("hash_chunk_size", 4 << 20),
            )
        except InterruptedError:
            raise CancelledError("verification cancelled")

        download.hash_status = result.status
        download.computed_hash = result.primary_hash
        self.db.update_download_fields(
            download.id,
            hash_status=result.status,
            computed_hash=result.primary_hash,
        )
        self._log(f"Integrity: {result.describe()}",
                  "error" if result.status is HashStatus.CORRUPTED else "info")
        self.events.emit(
            EventType.DOWNLOAD_VERIFIED,
            download_id=download.id,
            status=result.status.value,
            computed=result.computed,
            failures=result.failures,
        )


class DownloadEngine:
    """Owns every task, the concurrency policy and the global bandwidth cap."""

    def __init__(self, db: Database, settings: Settings, events: EventBus | None = None) -> None:
        self.db = db
        self.settings = settings
        self.events = events or EventBus()
        self.proxies = ProxyManager(db, settings, self.events)
        self.global_limiter = TokenBucket(settings.get_int("global_speed_limit", 0))

        self._tasks: dict[int, DownloadTask] = {}
        self._lock = threading.RLock()
        self._pump_event = threading.Event()
        self._shutdown = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._paused_queues: set[int] = set()
        #: Downloads somebody started by hand while their queue was paused.
        #:
        #: A paused queue holds back everything in it, and until this existed
        #: that included a download the user had just pressed Resume on: the
        #: slot check refused it, the status went back to `QUEUED`, and nothing
        #: said why. "It keeps everything in the queue even if you start
        #: manually the one you want" — and it did. An explicit instruction
        #: about one download outranks a policy about its queue, so that
        #: download is exempt until it is paused, cancelled or removed.
        self._started_by_hand: set[int] = set()
        #: One lock per server-driven endpoint; see `sabr_session_lock`.
        self._sabr_locks: dict[str, threading.Lock] = {}
        #: Set by the service: given a refresh recipe and a position in
        #: seconds, return a fresh media URL (or "" when none is available).
        self.renew_media_url: Any = None
        #: Set by the service: given an itag and the page the download came
        #: from, return the stream's initialisation segment as the page's own
        #: player received it (or ``b""``). A server-driven session never sends
        #: those bytes and no request from here obtains them; the page hook
        #: sees what the player receives, which is the only place they exist.
        self.lookup_opening: Any = None
        #: Set by the service: given a page URL, an itag, whether the track is
        #: audio and its track tags, return a fresh server-driven session for
        #: that same stream (or ``None``). A streaming endpoint is signed and
        #: expires within hours, so a transfer paused overnight resumes against
        #: a link the origin no longer honours — and everything needed to open
        #: a new session is obtainable from the page it came from.
        #:
        #: All four arguments are the stream's identity: the itag alone names a
        #: rendition, which every audio language of a dubbed video shares.
        self.renew_sabr_session: Any = None

        settings.on_change(self._on_setting_changed)

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._supervisor is not None:
            return
        recovered = self.db.recover_interrupted()
        if recovered:
            self.db.log_event(f"Recovered {recovered} interrupted download(s) after restart")
        # And the ones that never got as far as running. Reported even when it
        # is zero: a launch that parked nothing and a launch where this never
        # ran look identical in a log otherwise, and that is how the defect
        # survived — the pump started them a second later and the log only
        # ever said "Started".
        parked = self.db.park_queued()
        self.db.log_event(
            f"Parked {parked} download(s) left queued by a previous session"
            if parked else "Nothing was left queued by a previous session")
        self._shutdown.clear()
        self._supervisor = threading.Thread(
            target=self._supervise, name="ixd-supervisor", daemon=True
        )
        self._supervisor.start()

    def shutdown(self, wait: bool = True, timeout: float = 10.0) -> None:
        self._shutdown.set()
        self._pump_event.set()
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            task.pause()
        if wait:
            deadline = time.time() + timeout
            for task in tasks:
                task.join(max(0.1, deadline - time.time()))
        if self._supervisor is not None:
            self._supervisor.join(timeout=3.0)
            self._supervisor = None

    def sabr_session_lock(self, endpoint: str,
                          session_key: str = "") -> threading.Lock:
        """A lock shared by every transfer driving the same streaming session.

        The server keeps one playback position per session. Two transfers
        driving the same one concurrently each advance that position past what
        the other still needs, and both stall — observed as an audio track
        receiving zero bytes while its video ran.

        What identifies a session is its configuration, not the machine serving
        it. Keying on the endpoint's host and path put every video coming from
        one CDN node behind a single lock: two unrelated downloads that happened
        to be handed the same node ran one after the other, for no reason, and
        the second looked stalled while the first finished. The configuration
        blob is issued per video and per session, which is exactly the grain
        the server's playback position is kept at.
        """
        key = session_key or endpoint.split("?", 1)[0]
        with self._lock:
            lock = self._sabr_locks.get(key)
            if lock is None:
                # Sessions are transient and a long-running application meets
                # a great many of them, so entries nobody is holding are
                # cleared out rather than accumulating for the process's life.
                if len(self._sabr_locks) >= _MAX_SABR_LOCKS:
                    self._sabr_locks = {
                        held: value for held, value in self._sabr_locks.items()
                        if value.locked()
                    }
                lock = threading.Lock()
                self._sabr_locks[key] = lock
            return lock

    def _on_setting_changed(self, key: str, value: Any) -> None:
        if key == "global_speed_limit":
            self.global_limiter.set_rate(int(value or 0))
        elif key in ("proxy_mode", "active_proxy_id", "proxy_max_failures"):
            self.proxies.refresh()
        elif key == "max_concurrent_downloads":
            self._pump_event.set()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def add_download(self, url: str, *, filename: str = "", dest_dir: str = "",
                     queue_id: int | None = None, headers: dict[str, str] | None = None,
                     cookies: str = "", referer: str = "", user_agent: str = "",
                     expected_hash: str = "", expected_hash_algo: str = "sha256",
                     connections: int = 0, segments: list[MediaSegment] | None = None,
                     mode: TransferMode | None = None, media_title: str = "",
                     sabr_context: dict[str, Any] | None = None,
                     mux_group: str = "",
                     format_id: str = "", proxy_id: int | None = None,
                     network_interface: str = "", speed_limit: int = 0,
                     priority: int = 0, start: bool = True) -> Download:
        """Register a new transfer and (optionally) begin it immediately."""
        if not url and not segments:
            raise ValueError("a download needs a URL or a segment list")

        parsed = urllib.parse.urlparse(url)
        if url and parsed.scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme: {parsed.scheme!r}")

        if queue_id is None:
            queues = self.db.list_queues()
            queue_id = queues[0].id if queues else None

        download = Download(
            url=url,
            original_url=url,
            filename=sanitize_filename(filename) if filename else "",
            dest_dir=dest_dir,
            queue_id=queue_id,
            # Zero means "follow the setting", and that is what an unspecified
            # count now stores. Stamping the setting's value here froze it: a
            # download added while the setting said eight kept eight for ever,
            # so raising the setting changed nothing for anything already in
            # the list — and the setting is the one place a person expects to
            # change it. An explicit per-download count still wins, because it
            # was asked for.
            connections=int(connections or 0),
            referer=referer,
            user_agent=user_agent or self.settings.get("user_agent"),
            cookies=cookies,
            extra_headers=dict(headers or {}),
            expected_hash=expected_hash,
            expected_hash_algo=expected_hash_algo,
            segments=list(segments or []),
            mode=mode or (TransferMode.SEGMENTED if segments else TransferMode.RANGED),
            media_title=media_title,
            sabr_context=dict(sabr_context or {}),
            mux_group=mux_group,
            format_id=format_id,
            proxy_id=proxy_id,
            network_interface=network_interface,
            speed_limit=speed_limit or self.settings.get_int("per_download_speed_limit", 0),
            priority=priority,
            status=DownloadStatus.QUEUED,
        )
        if not download.filename:
            # A name is needed *now* — the row appears the moment this returns,
            # and a row with no name in it is not a download manager. But the
            # URL is only a guess, and on plenty of sites a bad one: GitHub's
            # release assets redirect to a path ending in a UUID, so the guess
            # was `74709710-bf21-4cd4-926a-526ff561a1bb` with no extension
            # while the response was saying `filename=ixd_1.0.3_amd64.deb` the
            # whole time. Flagged as a guess so the probe can overrule it.
            download.auto_named = True
            download.filename = filename_from_url(url) if url else sanitize_filename(
                media_title or "download"
            )
        download.category = config.category_for(download.filename)

        self.db.insert_download(download)
        self.db.log_event(f"Added {download.filename}", download.id)

        # Settled *before* the event goes out. A subscriber is told a download
        # was added and is entitled to act on what it is told: the window opens
        # a progress view for one that is starting and leaves a deferred one
        # alone, and it could not tell them apart while every announcement said
        # "queued" and the pause landed a line later.
        running = start and self.settings.get_bool("autostart_downloads", True)
        if not running:
            self.db.update_download_fields(download.id, status=DownloadStatus.PAUSED)
            download.status = DownloadStatus.PAUSED

        self.events.emit(
            EventType.DOWNLOAD_ADDED,
            download_id=download.id,
            download=download.to_public_dict(),
        )
        if running:
            self._pump_event.set()
        return download

    def start_download(self, download_id: int, force: bool = False,
                       by_hand: bool = False) -> bool:
        """Begin or resume a download, subject to concurrency limits.

        `by_hand` is somebody pressing Resume on this one download. It exempts
        it from its queue's pause — not from the concurrency limits, which are
        about the machine rather than about policy — and the exemption lasts
        until the download is paused, cancelled or removed, so the supervisor
        picks it up too when a slot frees.
        """
        with self._lock:
            task = self._tasks.get(download_id)
            if task is not None and task.running:
                return False
            if by_hand:
                self._started_by_hand.add(download_id)

        download = self.db.get_download(download_id)
        if download is None:
            return False
        if download.status is DownloadStatus.COMPLETED and not force:
            return False

        if by_hand and download.queue_id is not None and self.is_queue_paused(
                download.queue_id):
            queue = self.db.get_queue(download.queue_id)
            self.db.log_event(
                f"Started {download.filename} by hand — its queue "
                f"({queue.name if queue else download.queue_id}) is paused, "
                "and the rest of it stays that way.", download_id)

        if not force and not self._has_free_slot(download):
            self.db.update_download_fields(download_id, status=DownloadStatus.QUEUED)
            self.events.emit(EventType.DOWNLOAD_UPDATED, download_id=download_id,
                             status=DownloadStatus.QUEUED.value)
            return False

        task = DownloadTask(download, self)
        with self._lock:
            self._tasks[download_id] = task
        task.start()
        return True

    def allow_by_hand(self, download_id: int) -> None:
        """Exempt one download from its queue's pause, without starting it.

        For "Resume all", which hands its downloads to the supervisor in
        priority order rather than starting them itself — the ordering inside a
        queue is the point of having one, and calling `start_download` in list
        order would throw it away.
        """
        with self._lock:
            self._started_by_hand.add(download_id)

    def pause_download(self, download_id: int) -> None:
        with self._lock:
            task = self._tasks.get(download_id)
            # Pausing withdraws the exemption: the next thing to start this is
            # the queue, on the queue's terms.
            self._started_by_hand.discard(download_id)
        if task is not None and task.running:
            task.pause()
        else:
            self.db.update_download_fields(download_id, status=DownloadStatus.PAUSED)
            self.events.emit(EventType.DOWNLOAD_UPDATED, download_id=download_id,
                             status=DownloadStatus.PAUSED.value)

    def cancel_download(self, download_id: int) -> None:
        with self._lock:
            task = self._tasks.get(download_id)
            self._started_by_hand.discard(download_id)
        if task is not None and task.running:
            task.cancel()
        else:
            self.db.update_download_fields(download_id, status=DownloadStatus.CANCELLED)
            self.events.emit(EventType.DOWNLOAD_UPDATED, download_id=download_id,
                             status=DownloadStatus.CANCELLED.value)

    def remove_download(self, download_id: int, delete_files: bool = False) -> None:
        # Read the row *before* cancelling: cancelling rewrites the status, and
        # whether this download published a file is exactly what decides which
        # files may be deleted below.
        download = self.db.get_download(download_id)
        self.cancel_download(download_id)
        current = self.db.get_download(download_id)
        if download is not None and current is not None:
            # Keep whatever cancelling changed about the paths, and the status
            # from before it ran.
            current.status = download.status
            current.completed_at = download.completed_at
            download = current
        with self._lock:
            task = self._tasks.pop(download_id, None)
        if task is not None:
            task.join(timeout=5.0)
        if download is not None and delete_files:
            # Only ever this download's own files.
            #
            # `filepath` is `dest_dir/filename`, and until a download finishes
            # its filename is the *requested* one — the unique "(1)" suffix is
            # decided at publication. So a second download of something already
            # in the folder pointed at the **first one's finished file**, and
            # cancelling it deleted that: a completed download destroyed by
            # removing an unrelated one. Reported, and it is data loss.
            #
            # A download owns its temporary file always, and owns the published
            # file only once it has published one.
            published = (download.status is DownloadStatus.COMPLETED
                         or bool(download.completed_at))
            removable = [download.temp_path]
            if published:
                removable.append(download.filepath)
            for path in removable:
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
            for extra in (".parts", ".audio", ".joined"):
                candidate = (download.temp_path or "") + extra
                if extra == ".parts":
                    shutil.rmtree(candidate, ignore_errors=True)
                else:
                    try:
                        if os.path.isfile(candidate):
                            os.remove(candidate)
                    except OSError:
                        pass
        self.db.delete_download(download_id)
        self.events.emit(EventType.DOWNLOAD_REMOVED, download_id=download_id)
        self._pump_event.set()

    def swap_link(self, download_id: int, new_url: str) -> bool:
        """Point an existing download at a refreshed source and resume it.

        Chunk cursors are preserved, so only the bytes that were still missing
        are fetched from the new URL.
        """
        download = self.db.get_download(download_id)
        if download is None:
            return False
        parsed = urllib.parse.urlparse(new_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("the refreshed link must be an http(s) URL")
        if download.status is DownloadStatus.COMPLETED:
            # Re-pointing a finished download would re-fetch it into a second
            # file; the caller wants a new download, not a swap.
            raise ValueError("this download has already completed")

        with self._lock:
            task = self._tasks.get(download_id)
        if task is not None and task.running:
            task.pause()
            task.join(timeout=10.0)

        download.url = new_url
        # The old validators belong to the old URL; drop them so the resume
        # check compares against the new source instead of failing outright.
        download.etag = ""
        download.last_modified = ""
        download.status = DownloadStatus.QUEUED
        download.error = ""
        self.db.update_download(download)
        self.db.log_event(f"Link swapped to {new_url}", download_id)
        self.events.emit(EventType.DOWNLOAD_UPDATED, download_id=download_id,
                         status=DownloadStatus.QUEUED.value)
        self._pump_event.set()
        return True

    def set_expected_hash(self, download_id: int, value: str, algorithm: str = "") -> None:
        from .hashing import algorithm_for_hex, normalize_hash

        normalized = normalize_hash(value)
        chosen = (algorithm or algorithm_for_hex(normalized) or "sha256").lower()
        self.db.update_download_fields(
            download_id,
            expected_hash=normalized,
            expected_hash_algo=chosen,
            hash_status=HashStatus.UNKNOWN,
        )
        self.events.emit(EventType.DOWNLOAD_UPDATED, download_id=download_id)

    def reverify(self, download_id: int) -> None:
        """Re-run integrity checks on an already-completed file."""
        download = self.db.get_download(download_id)
        if download is None or not os.path.isfile(download.filepath):
            return

        def worker() -> None:
            headers = digest_headers(download.server_digest)
            result = verify_file(
                download.filepath,
                expected_hash=download.expected_hash,
                expected_algorithm=download.expected_hash_algo,
                server_headers=headers or None,
                extra_algorithms=self.settings.get("hash_algorithms") or ["sha256"],
                chunk_size=self.settings.get_int("hash_chunk_size", 4 << 20),
            )
            self.db.update_download_fields(
                download_id, hash_status=result.status, computed_hash=result.primary_hash
            )
            self.events.emit(
                EventType.DOWNLOAD_VERIFIED, download_id=download_id,
                status=result.status.value, computed=result.computed,
                failures=result.failures,
            )

        threading.Thread(target=worker, name=f"ixd-verify-{download_id}", daemon=True).start()

    # ------------------------------------------------------------------
    # queue control
    # ------------------------------------------------------------------
    def pause_queue(self, queue_id: int) -> None:
        with self._lock:
            self._paused_queues.add(queue_id)
        for download in self.db.downloads_in_queue(queue_id):
            if download.status.is_active or download.status is DownloadStatus.QUEUED:
                self.pause_download(download.id)
        self.events.emit(EventType.QUEUE_CHANGED, queue_id=queue_id, paused=True)

    def resume_queue(self, queue_id: int) -> None:
        with self._lock:
            self._paused_queues.discard(queue_id)
        for download in self.db.downloads_in_queue(queue_id):
            if download.status in (DownloadStatus.PAUSED, DownloadStatus.SCHEDULED):
                self.db.update_download_fields(download.id, status=DownloadStatus.QUEUED)
        self._pump_event.set()
        self.events.emit(EventType.QUEUE_CHANGED, queue_id=queue_id, paused=False)

    def stop_queue(self, queue_id: int) -> None:
        self.pause_queue(queue_id)

    def start_queue(self, queue_id: int) -> None:
        self.resume_queue(queue_id)

    def is_queue_paused(self, queue_id: int) -> bool:
        with self._lock:
            return queue_id in self._paused_queues

    # ------------------------------------------------------------------
    # error policy
    # ------------------------------------------------------------------
    def handle_transport_error(self, error: BaseException, download: Download | None) -> bool:
        """Record a failure and rotate the proxy when that might help.

        Returns ``True`` when the route changed, so the caller knows the retry
        is meaningfully different from the attempt that just failed.
        """
        if not self.settings.get_bool("proxy_rotate_on_error", True):
            return False
        if self.settings.get("proxy_mode", "none") == "none":
            return False
        if not should_rotate(error):
            return False

        current = self.proxies.resolve_for(download)
        self.proxies.report_failure(current, str(error))
        rotated = self.proxies.rotate(reason=str(error)[:120])
        return rotated is not None and (current is None or rotated.id != current.id)

    # ------------------------------------------------------------------
    # scheduling / concurrency
    # ------------------------------------------------------------------
    def _has_free_slot(self, download: Download) -> bool:
        with self._lock:
            active = [
                task for task in self._tasks.values()
                if task.running and task.download.id != download.id
                and not task.postprocessing
            ]
        if len(active) >= max(1, self.settings.get_int("max_concurrent_downloads", 4)):
            return False

        queue_id = download.queue_id
        if queue_id is None:
            return True

        # An explicit "start this one" outranks its queue being held — but not
        # the machine's own limits, which is why this sits below the
        # concurrency check and above the policy ones.
        with self._lock:
            by_hand = download.id in self._started_by_hand
        if not by_hand and self.is_queue_paused(queue_id):
            return False

        queue = self.db.get_queue(queue_id)
        if queue is None:
            return True
        if not by_hand and not queue.enabled:
            return False

        limit = 1 if queue.mode is QueueMode.SEQUENTIAL else max(1, queue.max_concurrent)
        in_queue = sum(1 for task in active if task.download.queue_id == queue_id)
        return in_queue < limit

    def _supervise(self) -> None:
        """Start eligible downloads and publish aggregate stats once a second."""
        while not self._shutdown.is_set():
            try:
                self._pump()
                self._emit_stats()
            except Exception:
                import traceback
                traceback.print_exc()
            self._pump_event.wait(1.0)
            self._pump_event.clear()

    def _pump(self) -> None:
        candidates = [
            d for d in self.db.list_downloads()
            if d.status in (DownloadStatus.QUEUED, DownloadStatus.SCHEDULED)
        ]
        if not candidates:
            return
        candidates.sort(key=lambda d: (-d.priority, d.id or 0))
        for download in candidates:
            if self._shutdown.is_set():
                return
            with self._lock:
                task = self._tasks.get(download.id)
                if task is not None and task.running:
                    continue
            if self._has_free_slot(download):
                self.start_download(download.id)

    def slot_released(self) -> None:
        """A running task has stopped needing a download slot.

        Called when a transfer passes into assembling, rewrapping or joining:
        those touch only bytes already on disk. Waking the scheduler here is
        what turns "the next download starts when this one finishes" into "the
        next download starts when this one stops using the network".
        """
        self._pump_event.set()

    def _task_finished(self, task: DownloadTask) -> None:
        with self._lock:
            if self._tasks.get(task.download.id) is task:
                self._tasks.pop(task.download.id, None)
        self._pump_event.set()

    def _emit_stats(self) -> None:
        with self._lock:
            tasks = [t for t in self._tasks.values() if t.running]
        total_speed = sum(t.download.speed for t in tasks)
        self.events.emit(
            EventType.ENGINE_STATS,
            active=len(tasks),
            speed=total_speed,
            limit=self.global_limiter.rate,
            proxy=(self.proxies.current().as_url() if self.proxies.current() else "direct"),
        )

    # ------------------------------------------------------------------
    def active_tasks(self) -> list[DownloadTask]:
        with self._lock:
            return [t for t in self._tasks.values() if t.running]

    def task_for(self, download_id: int) -> DownloadTask | None:
        with self._lock:
            return self._tasks.get(download_id)

    def set_global_limit(self, bytes_per_second: int) -> None:
        self.global_limiter.set_rate(max(0, bytes_per_second))
        self.settings.set("global_speed_limit", max(0, bytes_per_second))
