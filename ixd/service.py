"""Application service layer.

One object owns the database, settings, engine and scheduler, and exposes the
whole feature set as plain Python methods plus a JSON command dispatcher.  The
Qt UI calls the methods directly; the browser extension reaches the very same
methods through :mod:`ixd.ipc.server`.  Keeping a single implementation means
the two front-ends can never drift apart.
"""

from __future__ import annotations

import base64
import os
import threading
import time
import urllib.parse
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import config
from . import __version__
from .config import Settings
from .core.db import Database
from .core.engine import DownloadEngine
from .core.errors import ExtractionError, IXDError
from .core.events import EventBus, EventType
from .core.http_client import (CookieJar, HttpClient, filename_from_url,
                               format_bytes, sanitize_filename)
from .core.models import (
    Download,
    MediaFormat,
    DownloadQueue,
    DownloadStatus,
    MediaInfo,
    ProxyEntry,
    ProxyScheme,
    QueueMode,
    Schedule,
    ScheduleAction,
    TransferMode,
)
from .core.routing import parse_proxy_url
from .core.scheduler import Scheduler
from .power import CompletionAction, parse as parse_completion_action, perform
from . import updates
from .core.muxing import MuxError, combine as combine_tracks
from .extractors import (
    audio_track_rank,
    best_audio,
    best_muxable_audio,
    extract as run_extractor,
    prepare_format,
    quality_shortfall,
    quality_to_height,
    select_format,
    suggested_filename,
)


#: How far down the quality ladder to walk when the streaming server refuses a
#: stream. Each rung costs one request, and the ladder holds one entry per
#: distinct height, so this reaches the bottom of a typical video's range.
_SERVABLE_ATTEMPTS = 6

#: How long an analysis stays usable. The hover panel analyses a page to build
#: its menu and the click that follows asks for the same page again, so without
#: this every download begins by repeating work finished a moment earlier —
#: measured at 4.7 seconds between the click and the row appearing, all of it
#: that repeat. Kept short because the URLs an analysis carries are signed and
#: time-limited; this is only meant to span a person reading a menu.
_ANALYSIS_TTL_SECONDS = 120.0


def cookie_domain(url: str) -> str:
    """The domain a browser-supplied cookie header should be scoped to.

    Dropping only a leading ``www.`` keeps the cookies usable across the site's
    own subdomains without widening them to a public suffix — a naive
    "last two labels" rule would turn ``bbc.co.uk`` into ``co.uk``.
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _referrer_of(params: dict[str, Any]) -> str:
    """The page a browser-side request was made on behalf of.

    Spelled four ways across the wire — the extension sends ``referrer``, the
    HTTP header is ``Referer``, and both arrive snake-cased from the CLI — so
    the reading of it belongs in one place. It matters because a media CDN
    answers **403** to a manifest or segment request that carries none, which
    is the ordinary state of a link captured from a page and handed to a
    downloader.
    """
    for key in ("referrer", "referer", "page_url", "pageUrl"):
        value = params.get(key)
        if value:
            return str(value)
    return ""


#: Headers the engine sets for itself, so a replayed copy would fight it.
_ENGINE_OWNED = frozenset({
    "host", "connection", "content-length", "accept-encoding", "cookie",
    "range", "if-range", "if-none-match", "if-modified-since",
    "transfer-encoding", "keep-alive", "proxy-authorization", "upgrade", "te",
})


#: Progressive itags — a single file carrying both picture and sound, which is
#: therefore complete on its own and carries its own header.
_PROGRESSIVE_ITAGS = ("18", "22", "37", "59", "43")

#: The height each progressive itag carries. A capture has no manifest beside
#: it, so the itag is the only thing that says what it is.
_PROGRESSIVE_HEIGHTS: dict[str, int] = {
    "18": 360, "22": 720, "37": 1080, "59": 480, "43": 360,
}

#: Adaptive video itags and their heights, across the three codec families
#: YouTube serves: H.264 in MP4, VP9 in WebM, AV1 in MP4. A player that used
#: adaptive playback throughout — which is now the ordinary case — leaves one
#: of these behind for each quality it actually rendered.
_CAPTURED_VIDEO_HEIGHTS: dict[str, int] = {
    # H.264 / MP4
    "160": 144, "133": 240, "134": 360, "135": 480, "136": 720, "137": 1080,
    "264": 1440, "266": 2160, "298": 720, "299": 1080,
    # VP9 / WebM
    "278": 144, "242": 240, "243": 360, "244": 480, "247": 720, "248": 1080,
    "271": 1440, "313": 2160, "302": 720, "303": 1080, "308": 1440,
    "315": 2160,
    # AV1 / MP4
    "394": 144, "395": 240, "396": 360, "397": 480, "398": 720, "399": 1080,
    "400": 1440, "401": 2160,
}

#: Adaptive audio itags, ranked by the bitrate each carries. The rank orders
#: companions; it is not a promise about the file.
_CAPTURED_AUDIO_BITRATES: dict[str, int] = {
    "139": 48, "140": 128, "141": 256, "171": 128, "172": 192,
    "249": 50, "250": 70, "251": 160, "256": 192, "258": 384,
    "325": 384, "328": 384, "380": 384, "599": 30, "600": 35,
}


@dataclass(frozen=True, slots=True)
class _Capture:
    """One media address the browser's player was seen fetching.

    The extension observes the request; what matters here is what kind of
    stream it was, because that decides whether it can stand alone. The itag
    is the only identity a bare ``videoplayback`` address carries, so it is
    what everything below is read from — with the declared MIME type as the
    tie-breaker for an itag no table knows.
    """

    url: str
    itag: str = ""
    mime: str = ""
    size: int = 0
    height: int = 0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def progressive(self) -> bool:
        """One file carrying both tracks — complete, and its own header."""
        return self.itag in _PROGRESSIVE_ITAGS

    @property
    def audio_only(self) -> bool:
        if self.mime.startswith("audio/"):
            return True
        return self.itag in _CAPTURED_AUDIO_BITRATES

    @property
    def video_only(self) -> bool:
        return not self.progressive and not self.audio_only and bool(
            self.mime.startswith("video/") or self.itag in _CAPTURED_VIDEO_HEIGHTS
        )

    @property
    def webm(self) -> bool:
        return "webm" in self.mime or self.itag in (
            "43", "242", "243", "244", "247", "248", "271", "278", "302",
            "303", "308", "313", "315", "249", "250", "251", "171", "172",
        )

    @property
    def bitrate(self) -> int:
        return _CAPTURED_AUDIO_BITRATES.get(self.itag, 0)


class BrowserFetchRequired(Exception):
    """The address is refused here and served to the browser.

    Not an error: an instruction. Measured on 2026-08-12 against one address on
    one machine within one second — this application 403, `curl` 403 with and
    without browser headers, and Chrome itself 403 from a youtube.com page. The
    only requests that address answers are the browser's own, so the browser
    makes the request and hands the bytes back.
    """

    def __init__(self, url: str, filename: str, title: str,
                 headers: dict[str, str] | None = None, size: int = 0,
                 referrer: str = "", itag: str = "") -> None:
        super().__init__(f"{url} must be fetched by the browser")
        self.instruction = {
            "url": url, "filename": filename, "title": title,
            "headers": dict(headers or {}), "size": size,
            "referrer": referrer,
            # Which rendition this is. The extension prefers its **own**
            # captured address for the same itag, because the player minted
            # that one and this application's is born refused: YouTube's `n`
            # parameter arrives obfuscated and is transformed by the player's
            # JavaScript before use, which nothing here does. Measured: an
            # address straight from extraction is 403 to this application, to
            # `curl`, and to Chrome itself — while the player's own address for
            # the same stream plays.
            "itag": itag,
        }


def _attested(url: str, po_token: str) -> str:
    """Carry the proof of origin on an ordinary media address.

    A `videoplayback` address takes the token as the `pot` query parameter.
    The captures the browser hands over do not carry one — the player's own
    requests are credentialled by the session that made them, and a replay from
    here is not. Every plain GET in the field log was answered 403 while every
    server-driven transfer completed, and the server-driven route is the one
    that *was* presenting this token.
    """
    if not po_token or not url or "videoplayback" not in url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "pot" for key, _ in query):
        return url
    query.append(("pot", po_token))
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc, parts.path,
        urllib.parse.urlencode(query), parts.fragment,
    ))


def _capture_records(options: dict[str, Any]) -> list[_Capture]:
    """Everything the browser fetched for this page, as records.

    The extension sent bare URL strings for one release and sends full entries
    now — itag, MIME type, size and the headers it actually used. Both shapes
    are read, because a browser that has not been updated in step with the
    application must not lose the route this whole path exists to provide.
    """
    records: list[_Capture] = []
    for candidate in options.get("captured") or []:
        if isinstance(candidate, dict):
            url = str(candidate.get("url") or "")
            if not url:
                continue
            itag = str(candidate.get("itag") or "")
            raw_headers = candidate.get("headers") or {}
            headers = ({str(k): str(v) for k, v in raw_headers.items()
                        if str(k).lower() not in _ENGINE_OWNED and v is not None}
                       if isinstance(raw_headers, dict) else {})
            try:
                size = int(candidate.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            mime = str(candidate.get("mime") or "")
        else:
            url = str(candidate)
            itag, headers, size, mime = "", {}, 0, ""
        if "googlevideo.com" not in url:
            continue
        if not itag or not mime:
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            except ValueError:
                continue
            itag = itag or (query.get("itag") or [""])[0]
            mime = mime or (query.get("mime") or [""])[0]
            if not size:
                try:
                    size = int((query.get("clen") or ["0"])[0])
                except (TypeError, ValueError):
                    size = 0
        height = _CAPTURED_VIDEO_HEIGHTS.get(itag) or _PROGRESSIVE_HEIGHTS.get(
            itag, 0)
        records.append(_Capture(url=url, itag=itag, mime=mime, size=size,
                                height=height, headers=headers))
    return records


@dataclass(frozen=True, slots=True)
class _CapturePlan:
    """A complete file that can be built out of what the browser already has.

    Either one progressive stream, or an adaptive video track with the audio
    track to join to it. Anything that would produce a silent film — a video
    track with no companion — is not a plan and is never returned.
    """

    video: _Capture
    audio: _Capture | None = None

    @property
    def height(self) -> int:
        return self.video.height

    @property
    def webm(self) -> bool:
        return self.video.webm

    def describe(self) -> str:
        quality = f"{self.height}p" if self.height else "the quality"
        if self.audio is None:
            return f"{quality}, picture and sound in one file"
        return f"{quality} video with its audio track"


def _capture_plan(options: dict[str, Any], wanted_height: int = 0, *,
                  allow_shortfall: bool = True) -> _CapturePlan | None:
    """The best file the browser's own fetches can be assembled into.

    This is the route a commercial download manager takes and this one did not:
    it asks the site nothing. The addresses were signed for a session the site
    has already accepted, so they are served when an extraction request from
    the same machine is refused outright.

    ``wanted_height`` is the quality the user asked for. With
    ``allow_shortfall`` false nothing below it is returned, which is what keeps
    the fast path from quietly handing over 360p when 1080p was chosen and the
    site would have served it.
    """
    records = _capture_records(options)
    if not records:
        return None

    def better(candidate: _Capture, best: _Capture) -> bool:
        # Closest to what was asked for, without going under it; then the
        # largest, which on one video is the higher bitrate of two renditions.
        if wanted_height:
            over_a = candidate.height >= wanted_height
            over_b = best.height >= wanted_height
            if over_a != over_b:
                return over_a
            if over_a:
                return candidate.height < best.height
        if candidate.height != best.height:
            return candidate.height > best.height
        return candidate.size > best.size

    videos = [r for r in records if r.progressive or r.video_only]
    audio = [r for r in records if r.audio_only]
    best: _CapturePlan | None = None
    for candidate in videos:
        if candidate.video_only:
            companion = _best_companion(audio, candidate)
            if companion is None:
                # A picture with no sound is not a file anyone asked for.
                continue
            plan = _CapturePlan(candidate, companion)
        else:
            plan = _CapturePlan(candidate)
        if best is None or better(plan.video, best.video):
            best = plan
    if best is None:
        return None
    if not allow_shortfall and wanted_height and best.height < wanted_height:
        return None
    return best


def _best_companion(audio: list[_Capture],
                    video: _Capture) -> _Capture | None:
    """The audio track to join to a captured video track.

    Container first: an MP4 video and an Opus/WebM track cannot become one
    file, so a lower-bitrate AAC track is the better companion. Bitrate only
    decides between candidates that can actually be joined.
    """
    if not audio:
        return None
    matching = [entry for entry in audio if entry.webm == video.webm]
    pool = matching or audio
    return max(pool, key=lambda entry: (entry.bitrate, entry.size))


def _page_key(page_url: str) -> str:
    """Which page an opening was seen on, for the purpose of matching one.

    A player's site is a single-page application: the video changes and the
    query string changes with it, so origin-and-path would call two videos the
    same page. The video id is what identifies it when there is one.
    """
    text = str(page_url or "")
    if not text:
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return text
    video = urllib.parse.parse_qs(parts.query).get("v")
    if video and video[0]:
        return f"{parts.netloc}/v={video[0]}"
    return f"{parts.netloc}{parts.path}"


def _header_unobtainable(media: "MediaFormat | None") -> bool:
    """Whether this stream needs opening bytes it has no way to fetch.

    It never does, and this function now says so. It is kept because the
    reasoning it used to carry was wrong in a way worth recording.

    The claim was that a server-driven session never sends the initialisation
    and index segments, so they had to come from the format's ordinary URL, and
    a stream with `header_end` set and no `header_url` was therefore refused
    before a single request was made. Six transfers in the field log of
    2026-08-12 failed exactly there, each within a second of being queued, and
    each fell back to a progressive address that is **403 to everyone** —
    measured in a real browser, refetching the player's own address from the
    page that minted it.

    The premise is not true of the protocol. A media header carries an
    `is_init_seg` flag (`_HEADER_IS_INIT`, `extractors/sabr.py`), and the server
    sends the initialisation segment at the head of a session that has not
    declared it already holds one. `SabrStream._consume` writes every block at
    the `start_range` its header names, which for that segment is zero — so it
    lands where it belongs with no special handling.

    So the opening bytes are judged on whether they arrived, in the engine,
    after the session has run. Refusing in advance cost the whole download for
    the sake of a prediction about a few kilobytes.
    """
    return False


def companion_probe(info: MediaInfo, chosen: "MediaFormat") -> "MediaFormat | None":
    """The audio that would be fetched beside ``chosen``, if any.

    A silent-film guard is no use when the *audio* is the half that cannot
    produce a header: the download fails just the same, one track later.
    """
    if not chosen.has_video or chosen.has_audio:
        return None
    return best_muxable_audio(info.formats, chosen)


def _captured_progressive(options: dict[str, Any]) -> str:
    """A complete stream the browser already fetched, if it saw one.

    The extension observes every request the player makes. Among them, on
    YouTube, is usually a progressive `videoplayback` URL — one file, both
    tracks, its own header, and signed by a session the site has already
    accepted. It is the route that works when the published formats are
    server-driven and publish no ordinary link at all.

    Its limit is why :func:`_capture_plan` exists: a player that used adaptive
    streams throughout leaves no progressive URL behind at all, and on YouTube
    that is now the ordinary case rather than the exception.
    """
    best: _Capture | None = None
    for record in _capture_records(options):
        if record.progressive and (best is None or record.height > best.height):
            best = record
    return best.url if best is not None else ""


def _site_headers_of(params: dict[str, Any]) -> dict[str, str]:
    """The headers the browser actually sent for this address.

    A media CDN decides by header, and reconstructing what it wants is
    guesswork: `Referer` is a good guess and not always the right one, because a
    player may sign its requests with an `Authorization` or a bespoke `X-…`
    header that nothing could invent. The browser already sent a set that
    worked, so it is replayed. The ones the engine owns are dropped, because a
    replayed `Range` or `Accept-Encoding` would contradict what it is doing.
    """
    raw = params.get("headers") or params.get("site_headers") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()
            if str(key).lower() not in _ENGINE_OWNED and value is not None}


def _named_after_its_page(info: "MediaInfo", url: str, page_title: str) -> "MediaInfo":
    """Give a captured playlist the name of the page it was playing on.

    A `.m3u8` handed to the extractor directly has no title of its own, so the
    address is all there is and the file lands as `master.mp4` — which is the
    name of a routing file, not of anything a person wanted. Every stream on a
    site is called `master.m3u8` or `index-f1-v1-a1.m3u8`, so the names also
    collide with each other.

    The page knows. The extension sends its title along, and it is used when the
    extracted title is absent or is merely the address repeated back.
    """
    if not page_title:
        return info
    basename = url.rsplit("/", 1)[-1].split("?")[0]
    stem = basename.rsplit(".", 1)[0].lower()
    current = (info.title or "").strip().lower()
    if current and current not in (basename.lower(), stem):
        return info
    return replace(info, title=page_title.strip())


class DownloadService:
    """The application's public API surface."""

    def __init__(self, settings: Settings | None = None,
                 db: Database | None = None) -> None:
        config.ensure_dirs()
        self.settings = settings or Settings()
        self.db = db or Database(config.DB_PATH)
        self.events = EventBus()
        self.engine = DownloadEngine(self.db, self.settings, self.events)
        self.scheduler = Scheduler(self.db, self.settings, self.engine, self.events)
        self._started = False
        self._mux_lock = threading.Lock()
        self._muxed: set[str] = set()
        #: The countdown to "shut down when everything is finished", if one is
        #: running. Guarded, because a completion event arrives on whichever
        #: worker thread finished the transfer and two finishing at once must
        #: not start two timers.
        self._completion_lock = threading.Lock()
        self._completion_timer: threading.Timer | None = None
        self._update_timer: threading.Timer | None = None
        self._extract_lock = threading.Lock()
        self._extracts: dict[tuple, tuple[float, MediaInfo]] = {}
        #: Transfers the browser is reading on this application's behalf.
        self._browser_lock = threading.Lock()
        self._browser_streams: dict[int, dict[str, Any]] = {}
        # What the browser knew about an address it handed over. The extension
        # is a relay: it observes and passes on, and the choosing happens here —
        # so the session that made the address work has to be here too, or the
        # dialog that opens repeats the 403 the extension had already solved.
        self._browser_context: dict[str, tuple[float, dict[str, Any]]] = {}
        # Which rendition the streaming server was willing to serve, per video.
        # Asking costs an exchange per rung and the answer is stable for far
        # longer than a person spends choosing.
        self._servable: dict[tuple, tuple[float, tuple[Any, str]]] = {}
        # Openings the page's own player received, by (page, itag). A
        # server-driven session never sends a stream's first bytes and nothing
        # this application can ask gets them; the player receives them, and the
        # page hook hands them over. See `_cmd_browser_media_head`.
        self._openings_lock = threading.Lock()
        self._openings: dict[tuple[str, str], tuple[float, bytes]] = {}
        self.engine.lookup_opening = self._lookup_opening
        self.engine.renew_media_url = self._renew_media_url
        self.engine.renew_sabr_session = self._renew_sabr_session_for

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        # The log is a working instrument, not an archive: it is read by
        # copying the whole of it into a report, and one that has been
        # accumulating across launches is unreadable at exactly the moment it
        # matters — the field reports that arrive are thousands of lines of
        # which the last fifty are the fault. So a launch starts an empty log
        # and it holds this run only. Pruning was the earlier compromise and it
        # is kept for anyone who turns the clearing off.
        # Whether anything is recorded at all is the user's to decide, and it
        # is applied before the first message of the run rather than after.
        self.db.log_enabled = self.settings.get_bool("keep_log", True)
        self.settings.on_change(self._on_log_setting_changed)
        if not self.db.log_enabled:
            # Leaving the previous run's log behind after it has been switched
            # off would be the opposite of what was asked for.
            self.db.clear_events()
        elif self.settings.get_bool("clear_log_on_launch", True):
            self.db.clear_events()
        else:
            self.db.prune_events(self.settings.get_int("log_lines_kept", 2000))
        self.engine.start()
        self.scheduler.start()
        self.events.subscribe(self._on_download_completed, EventType.DOWNLOAD_COMPLETED)
        self.events.subscribe(self._on_download_failed, EventType.DOWNLOAD_FAILED)
        self.db.log_event("Service started")

        # Not on the start-up path: a check that has to answer before the
        # window appears is a check that delays the window when a network is
        # slow. It runs a minute in, on its own thread, and only if it is both
        # allowed and due.
        if self.settings.get_bool("updates_check_automatically", True):
            timer = threading.Timer(60.0, self._background_update_check)
            timer.name = "ixd-update-check"
            timer.daemon = True
            timer.start()
            self._update_timer = timer

    def _background_update_check(self) -> None:
        try:
            self.check_for_updates()
        except Exception:               # noqa: BLE001 - never fatal
            pass

    def _on_log_setting_changed(self, key: str, value: Any) -> None:
        """Take effect when the box is ticked, not at the next launch."""
        if key != "keep_log":
            return
        enabled = bool(value)
        self.db.log_enabled = enabled
        if enabled:
            self.db.log_event("Logging switched on")
        else:
            # It is switched off *and* what was already recorded goes, because
            # "stop keeping a log" plainly includes the one already kept.
            self.db.clear_events()

    # -- combining adaptive pairs --------------------------------------
    def _on_download_completed(self, _event: str, payload: dict[str, Any]) -> None:
        """Join a video/audio pair as soon as its second half arrives."""
        self._consider_completion_action()
        download = self.db.get_download(int(payload.get("download_id") or 0))
        if download is None or not download.mux_group:
            return
        threading.Thread(
            target=self._try_mux, args=(download.mux_group,),
            name="ixd-mux", daemon=True,
        ).start()

    # -- what to do when there is nothing left to do -------------------
    #
    # The point of leaving a queue running overnight is not having to come
    # back to it, which is why IDM's scheduler ends with "shut down when
    # done". This is that, and it fires **once**: after it runs the setting
    # resets itself, because "shut this machine down tonight" is a decision
    # about tonight and not a standing policy.
    #
    # It is armed rather than performed. A countdown the window can offer to
    # call off is the difference between a convenience and a machine that
    # turns itself off while somebody is using it — the timer runs even with
    # no window at all, which is what `--background` needs.
    # -- newer versions -------------------------------------------------
    #
    # Asked for as "tell me there is a new one instead of making me look".
    # Three rules shape it:
    #
    #   * **One request a day, and only if it was allowed.** The setting is a
    #     checkbox and it is honoured here rather than in the window, so the
    #     daemon and the GUI behave the same.
    #   * **It uses the application's own HTTP client**, which means the proxy,
    #     the interface binding and the TLS setting the user chose. An updater
    #     that opens its own socket is a hole in all three.
    #   * **A check never installs anything.** It reports; what happens next is
    #     the user's decision, and for a packaged build there is nothing this
    #     process is allowed to do anyway.
    def check_for_updates(self, force: bool = False) -> updates.Release | None:
        """Ask whether a newer version exists. Returns it, or ``None``."""
        if not force and not self.settings.get_bool("updates_check_automatically", True):
            return None
        if not force:
            last = float(self.settings.get("updates_last_check") or 0)
            if time.time() - last < updates.CHECK_INTERVAL_SECONDS:
                return None

        feed = str(self.settings.get("updates_feed") or updates.DEFAULT_FEED)
        try:
            release = updates.check(self.client(), feed)
        except Exception as error:      # noqa: BLE001 - reported, never fatal
            self.db.log_event(f"Update check failed: {error}", level="warning")
            if force:
                raise
            return None

        self.settings.set("updates_last_check", time.time())
        if not release.newer:
            self.db.log_event(
                f"Update check: {__version__} is the newest published version.")
            return None

        kind = updates.self_update_kind()
        self.db.log_event(
            f"Version {release.version} is available (this build "
            + ("can install it itself)" if kind else "installs from a package)"),
        )
        # Announced once per version. A window that says the same thing at
        # every launch is one people stop reading.
        if self.settings.get("updates_last_seen") != release.version:
            self.settings.set("updates_last_seen", release.version)
        self.events.emit(
            EventType.UPDATE_AVAILABLE,
            version=release.version,
            notes=release.notes,
            page_url=release.page_url,
            self_update=bool(kind),
        )

        if kind and self.settings.get_bool("updates_install_automatically", False):
            self._install_when_idle(release)
        return release

    #: How long an automatic install waits for the downloads to finish before
    #: giving up and leaving it for the next check. Long enough for a transfer
    #: that is nearly done, short enough that it does not sit there for a day.
    IDLE_WAIT_SECONDS = 30 * 60

    def _install_when_idle(self, release: updates.Release) -> None:
        """Install by itself, but never on top of a transfer that is running.

        An update that replaces the application while a download is in flight
        is an update that loses a file. The rule is simple and it is the whole
        policy: if anything is still going, this waits, and if it is still
        going half an hour later the install is left for the next check.
        """
        def wait_then_install() -> None:
            deadline = time.time() + self.IDLE_WAIT_SECONDS
            while time.time() < deadline:
                busy = [d for d in self.db.list_downloads() if d.status.is_active]
                if not busy:
                    break
                time.sleep(10.0)
            else:
                self.db.log_event(
                    f"Version {release.version} was not installed automatically: "
                    "downloads were still running. It will be offered again.",
                )
                return
            started, detail = self.install_update(release)
            if started:
                self.events.emit(EventType.COMPLETION_FIRED, action="exit",
                                 ok=True, detail=f"updating to {detail}")
            else:
                self.db.log_event(
                    f"Automatic update to {release.version} did not start: {detail}",
                    level="warning")

        thread = threading.Thread(target=wait_then_install,
                                  name="ixd-auto-update", daemon=True)
        thread.start()

    def install_update(self, release: updates.Release,
                       progress: Any = None) -> tuple[bool, str]:
        """Fetch the new build, check it, and hand the swap to it.

        Returns ``(started, detail)``. When it returns ``True`` the application
        is about to be replaced and should quit: the staged copy is already
        waiting for this process to end.
        """
        if not updates.self_update_kind():
            return False, "this build installs from a package, not from itself"
        asset = updates.choose_asset(release)
        if asset is None:
            return False, ("the release publishes nothing this build can use: "
                           + ", ".join(str(a.get("name")) for a in release.assets))

        # Next to the application for a portable build, so an update never
        # appears somewhere the user did not put the program.
        staging = updates.staging_root(Path(config.DATA_DIR) / "update")
        try:
            archive = updates.download(self.client(), asset, staging, progress)
            unpacked = updates.stage(archive, staging / "unpacked")
        except Exception as error:      # noqa: BLE001 - reported to the caller
            self.db.log_event(f"Update download failed: {error}", level="warning")
            return False, str(error)

        target = updates.install_root()
        if target is None:
            return False, "an update can only replace a built copy"
        self.db.log_event(
            f"Installing version {release.version} over {target} — the "
            "application will restart. The browser extension is written out "
            "again on the next start; reload it from the extensions page.")
        try:
            updates.relaunch_into(unpacked, target)
        except Exception as error:      # noqa: BLE001
            self.db.log_event(f"Could not start the installer: {error}",
                              level="warning")
            return False, str(error)
        return True, str(release.version)

    def unfinished_work(self) -> list[Download]:
        """Downloads that still have somewhere to go.

        Paused counts. A paused download is work the user has parked, not work
        that is over, and powering the machine down under it would lose the
        session it was going to resume in.
        """
        pending = []
        for download in self.db.list_downloads():
            if download.status.is_active:
                pending.append(download)
            elif download.status in (DownloadStatus.QUEUED,
                                     DownloadStatus.SCHEDULED,
                                     DownloadStatus.PAUSED,
                                     DownloadStatus.NEEDS_LINK):
                pending.append(download)
        return pending

    def _consider_completion_action(self) -> None:
        action = parse_completion_action(self.settings.get("completion_action"))
        if action is CompletionAction.NOTHING:
            return
        with self._completion_lock:
            if self._completion_timer is not None:
                return                      # already counting down
        remaining = self.unfinished_work()
        if remaining:
            return
        self.arm_completion_action(action)

    def arm_completion_action(self, action: CompletionAction | None = None,
                              seconds: int | None = None) -> int:
        """Start the countdown. Returns the seconds the caller has to stop it."""
        action = action or parse_completion_action(
            self.settings.get("completion_action"))
        if action is CompletionAction.NOTHING:
            return 0
        grace = max(0, int(
            self.settings.get_int("completion_grace_seconds", 60)
            if seconds is None else seconds))

        with self._completion_lock:
            if self._completion_timer is not None:
                return grace
            timer = threading.Timer(grace, self._fire_completion_action, (action,))
            timer.name = "ixd-completion"
            timer.daemon = True
            self._completion_timer = timer
            timer.start()

        message = (f"Everything has finished. {action.label} in {grace}s "
                   f"unless it is called off.")
        self.db.log_event(message, level="warning")
        self.events.emit(EventType.COMPLETION_ARMED, action=action.value,
                         seconds=grace, message=message)
        return grace

    def cancel_completion_action(self, reason: str = "cancelled") -> bool:
        """Call off a countdown. Also clears the setting, so it stays off."""
        with self._completion_lock:
            timer = self._completion_timer
            self._completion_timer = None
        if timer is None:
            return False
        timer.cancel()
        self.settings.set("completion_action", CompletionAction.NOTHING.value)
        self.db.log_event(f"Completion action {reason}; nothing will happen.")
        self.events.emit(EventType.COMPLETION_CANCELLED, reason=reason)
        return True

    def _fire_completion_action(self, action: CompletionAction) -> None:
        with self._completion_lock:
            self._completion_timer = None
        # Once. A machine that shuts down after every download is unusable,
        # and the setting is cleared *before* the attempt so that a failure
        # cannot leave it armed for the next download either.
        self.settings.set("completion_action", CompletionAction.NOTHING.value)

        if action is CompletionAction.EXIT:
            self.db.log_event("All downloads finished — quitting.")
            self.events.emit(EventType.COMPLETION_FIRED, action=action.value,
                             ok=True, detail="quitting")
            return

        ok, detail = perform(action)
        # Recorded whichever way it went, and written before the machine has
        # the chance to go: on a system that powers off, this line is the only
        # evidence left behind.
        self.db.log_event(
            f"{action.label} after finishing: {'ok' if ok else 'failed'} — {detail}",
            level="info" if ok else "warning",
        )
        self.events.emit(EventType.COMPLETION_FIRED, action=action.value,
                         ok=ok, detail=detail)

    def _on_download_failed(self, _event: str, payload: dict[str, Any]) -> None:
        """Discard the surviving half of a pair whose partner failed."""
        # A failure can be the last thing that was running, and "when
        # everything has finished" has to include "and the last one did not".
        self._consider_completion_action()
        download = self.db.get_download(int(payload.get("download_id") or 0))
        if download is None or not download.mux_group:
            return
        threading.Thread(
            target=self._discard_orphan, args=(download.mux_group,),
            name="ixd-orphan", daemon=True,
        ).start()

    def _discard_orphan(self, group: str) -> None:
        """Remove a lone audio track left behind by a failed video half.

        The pair exists to become one file. When the video cannot be fetched,
        the audio on its own is not a smaller version of what was asked for —
        it is a stray file in the download folder that nobody wanted, and the
        user is left to work out which of two rows was the real failure. The
        video row is kept, carrying the error, because that is the one whose
        failure needs explaining.
        """
        token = group.rsplit(":", 1)[0]
        members = [
            d for d in self.db.list_downloads()
            if d.mux_group and d.mux_group.rsplit(":", 1)[0] == token
        ]
        video = next((d for d in members if d.mux_group.endswith(":video")), None)
        audio = next((d for d in members if d.mux_group.endswith(":audio")), None)
        if video is None or audio is None:
            return
        if video.status is not DownloadStatus.ERROR:
            return
        # Stop the audio wherever it has got to, so a transfer still running
        # does not keep writing to a file that is about to be removed.
        try:
            self.engine.cancel_download(audio.id)
        except Exception:      # noqa: BLE001 - it may already have finished
            pass
        for path in (audio.filepath, audio.temp_path):
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.db.delete_download(audio.id)
        self.db.log_event(
            "The video track could not be fetched, so the audio track queued "
            "alongside it was discarded rather than left as a soundtrack with "
            "no picture.", video.id, "info",
        )
        self.events.emit(EventType.DOWNLOAD_REMOVED, download_id=audio.id)

    def _try_mux(self, group: str) -> None:
        token = group.rsplit(":", 1)[0]
        members = [
            d for d in self.db.list_downloads()
            if d.mux_group and d.mux_group.rsplit(":", 1)[0] == token
        ]
        video = next((d for d in members if d.mux_group.endswith(":video")), None)
        audio = next((d for d in members if d.mux_group.endswith(":audio")), None)
        if video is None or audio is None:
            return
        if not all(d.status is DownloadStatus.COMPLETED for d in (video, audio)):
            return      # the other half is still running, or failed
        if not (os.path.isfile(video.filepath) and os.path.isfile(audio.filepath)):
            return

        with self._mux_lock:
            if token in self._muxed:
                return
            self._muxed.add(token)

        target = Path(video.filepath)
        combined = target.with_name(f"{target.stem}.muxing{target.suffix}")
        try:
            combine_tracks(video.filepath, audio.filepath, combined)
        except (MuxError, OSError) as exc:
            self.db.log_event(
                f"Could not combine the video and audio tracks ({exc}); both "
                "files have been kept as they are.", video.id, "warning",
            )
            return

        try:
            os.replace(combined, target)
            os.remove(audio.filepath)
        except OSError as exc:
            self.db.log_event(f"Could not tidy up after combining: {exc}",
                              video.id, "warning")
            return

        self.db.delete_download(audio.id)
        size = os.path.getsize(target)
        self.db.update_download_fields(
            video.id, total_size=size, downloaded=size, mux_group="",
        )
        self.db.log_event(
            f"Combined the video and audio tracks into “{target.name}”.",
            video.id, "info",
        )
        self.events.emit(EventType.DOWNLOAD_UPDATED, download_id=video.id,
                         status=DownloadStatus.COMPLETED.value, error="")

    def shutdown(self) -> None:
        if not self._started:
            return
        self._started = False
        # A countdown does not survive the service it belongs to. Cancelled
        # quietly — the setting is left alone, because stopping the
        # application is not the user changing their mind about tonight.
        with self._completion_lock:
            timer, self._completion_timer = self._completion_timer, None
        if timer is not None:
            timer.cancel()
        if self._update_timer is not None:
            self._update_timer.cancel()
            self._update_timer = None
        self.scheduler.stop()
        self.engine.shutdown(wait=True, timeout=8.0)
        self.db.log_event("Service stopped")

    # ------------------------------------------------------------------
    # downloads
    # ------------------------------------------------------------------
    def client(self, download: Download | None = None, *, cookies: str = "",
               user_agent: str = "", cookie_url: str = "",
               referer: str = "",
               site_headers: dict[str, str] | None = None) -> HttpClient:
        """An HTTP client wired to the current proxy/interface policy.

        ``cookies`` and ``user_agent`` come from the browser extension. Passing
        the browser's own session through is what lets sites that gate
        anonymous traffic behave for us exactly as they do for the user — and
        the cookies are scoped to the page's registrable domain so they are
        never leaked to a third-party CDN during the transfer.
        """
        profile = self.engine.proxies.profile_for(download)
        if user_agent:
            profile = replace(profile, user_agent=user_agent)

        jar = CookieJar()
        if cookies:
            jar.load_header(cookies, cookie_domain(cookie_url))
        # ``referer`` is the page the request is being made on behalf of, and
        # ``site_headers`` is the set the browser actually sent for this exact
        # address. A media CDN decides by header: it refuses a manifest or a
        # segment that arrives from nowhere with 403 however good the cookies
        # are, and a player may sign its requests with a header nothing could
        # reconstruct. Replaying what worked beats inventing what might.
        return HttpClient(profile, jar, referer, site_headers,
                          urllib.parse.urlparse(cookie_url).hostname or "")

    #: How long a handed-over browser session stays usable. Long enough for a
    #: person to read a quality menu and choose; short enough that a signed
    #: address is not carried past its own life.
    _BROWSER_CONTEXT_TTL = 900.0

    #: How long a servability answer stays good. Short enough that a site
    #: changing its mind is noticed, long enough to cover a retry.
    _SERVABLE_TTL = 300.0

    def remember_browser_context(self, url: str, context: dict[str, Any]) -> None:
        """Keep what the browser knew about ``url`` for the UI that follows."""
        now = time.time()
        with self._extract_lock:
            for key, (stamp, _) in list(self._browser_context.items()):
                if now - stamp > self._BROWSER_CONTEXT_TTL:
                    self._browser_context.pop(key, None)
            self._browser_context[url] = (now, dict(context))
            host = urllib.parse.urlparse(url).hostname or ""
            if host:
                self._browser_context[f"//{host}"] = (now, dict(context))

    def browser_context(self, url: str) -> dict[str, Any]:
        """What the browser knew about ``url``, or about its host."""
        host = urllib.parse.urlparse(url).hostname or ""
        with self._extract_lock:
            for key in (url, f"//{host}" if host else ""):
                if not key:
                    continue
                found = self._browser_context.get(key)
                if found and time.time() - found[0] <= self._BROWSER_CONTEXT_TTL:
                    return dict(found[1])
        return {}

    def _with_browser_context(self, url: str, cookies: str, user_agent: str,
                              referer: str,
                              site_headers: dict[str, str] | None
                              ) -> tuple[str, str, str, dict[str, str]]:
        """Fill in whatever the caller did not supply from the handed-over session.

        The desktop dialogs call these methods with nothing but a URL, because
        the person opening them is not a browser. Without this the app repeats
        the refusal the extension had already got past.
        """
        if cookies and referer and site_headers:
            return cookies, user_agent, referer, dict(site_headers or {})
        stored = self.browser_context(url)
        if not stored:
            return cookies, user_agent, referer, dict(site_headers or {})
        return (
            cookies or stored.get("cookies", ""),
            user_agent or stored.get("user_agent", ""),
            referer or stored.get("referer", ""),
            dict(site_headers or stored.get("headers") or {}),
        )

    def add_url(self, url: str, **kwargs: Any) -> Download:
        """Queue a plain file download."""
        return self.engine.add_download(url, **kwargs)

    #: Whether a name taken from a URL is a name at all.
    #:
    #: `ixd_1.0.3_amd64.deb` is; `74709710-bf21-4cd4-926a-526ff561a1bb` is not,
    #: and neither is anything else with no extension on the end of it.
    @staticmethod
    def _looks_like_a_filename(name: str) -> bool:
        stem, dot, extension = str(name or "").rpartition(".")
        if not dot or not stem:
            return False
        return 1 <= len(extension) <= 5 and extension.isalnum()

    def _name_before_the_row_appears(self, payload: dict[str, Any]) -> str:
        """Ask the origin what the file is called, when the address will not say.

        The engine renames a guessed filename as soon as it has fetched the
        first response (§3.27), and the row in the window corrects itself. But
        the *reply to this call* is what the browser announces in its "sent to
        IXD" notification, and that happens once, immediately, with whatever
        was known then — which on a GitHub release asset was a UUID.

        So when the address carries nothing that could be a filename, one HEAD
        is spent before answering. It costs a few hundred milliseconds on the
        only downloads where it changes anything, and it is skipped entirely
        for every address that already names its file.
        """
        supplied = sanitize_filename(str(payload.get("filename") or "")) \
            if payload.get("filename") else ""
        # A name the browser supplies is not always a name somebody chose. On
        # Windows the download item already carries one by the time it is
        # intercepted, and for an address ending in an identifier that name is
        # the identifier — which is how `8b192290-d315-431a-8ff6-b03be0d2c027`
        # reached a notification while the window showed the real filename.
        # So it is trusted when it looks like a filename and treated as
        # another guess when it does not.
        if supplied and self._looks_like_a_filename(supplied):
            return supplied
        url = str(payload.get("url") or "")
        if not url or self._looks_like_a_filename(filename_from_url(url)):
            return supplied
        try:
            client = self.client(
                cookies=payload.get("cookies", "") or "",
                user_agent=payload.get("userAgent", "") or "",
                referer=payload.get("referrer", "") or payload.get("referer", "") or "",
                site_headers=_site_headers_of(payload),
            )
            info = client.probe(url)
        except Exception:               # noqa: BLE001 - the guess still stands
            return supplied
        named = getattr(info, "filename", "") or ""
        if named:
            return sanitize_filename(named)
        mime = getattr(info, "mime", "") or ""
        derived = filename_from_url(getattr(info, "url", url) or url, mime)
        return derived if self._looks_like_a_filename(derived) else supplied

    def add_from_browser(self, payload: dict[str, Any]) -> Download:
        """Accept an intercepted download from the browser extension."""
        # The browser's own headers, minus the ones the engine sets itself: a
        # replayed `Range` or `Accept-Encoding` would contradict what it is
        # doing, and a replayed `Cookie` would race the scoped one.
        headers = _site_headers_of(payload)
        return self.engine.add_download(
            payload["url"],
            filename=self._name_before_the_row_appears(payload),
            # Where this one file goes, when somebody chose a folder for it in
            # the window the interception opens. Empty means the setting.
            dest_dir=str(payload.get("dest_dir") or ""),
            headers=headers,
            cookies=payload.get("cookies", "") or "",
            referer=payload.get("referrer", "") or payload.get("referer", "") or "",
            user_agent=payload.get("userAgent", "") or payload.get("user_agent", "") or "",
            queue_id=payload.get("queue_id"),
            start=bool(payload.get("start", True)),
        )

    def queue_pair(self, payload: dict[str, Any], video_url: str,
                   audio_url: str = "", *, title: str = "", stem: str = "",
                   video_ext: str = "mp4", audio_ext: str = "m4a",
                   audio_headers: dict[str, str] | None = None) -> Download:
        """Queue a video track and its audio as one file.

        An adaptive stream is video *or* audio, so the two are tied together by
        a shared mux group and combined on arrival. Without the companion the
        result would be a silent film, which is the complaint this exists to
        answer.

        Both the panel's "Already loaded by the player" list and the quality
        menu's fallback arrive here, because they are the same act: taking what
        the browser already has instead of asking the site for it.
        """
        clean = sanitize_filename(stem or title) if (stem or title) else ""
        video = self.add_from_browser({
            **payload, "url": video_url,
            "filename": payload.get("filename", "") or (
                f"{clean}.{video_ext}" if clean else ""
            ),
        })
        if not audio_url:
            return video

        # Tied to the video's own row id, which is unique by construction. A
        # token minted from the clock is not: two pairs queued in the same
        # millisecond share it, and the join then picks one of four members as
        # "the video" and one as "the audio" — combining halves of different
        # downloads into a file that is exactly as wrong as it sounds.
        group = f"{video.id}-captured"
        self.db.update_download_fields(
            video.id, mux_group=f"{group}:video",
            media_title=title or video.media_title,
        )
        audio = self.add_from_browser({
            **payload, "url": audio_url,
            # The audio's own request headers, not the video's: a media CDN
            # decides by header, and the browser sent a different set for each.
            **({"headers": audio_headers} if audio_headers else {}),
            "filename": f"{clean}.{audio_ext}" if clean else "",
        })
        self.db.update_download_fields(
            audio.id, mux_group=f"{group}:audio",
            media_title=title or audio.media_title,
        )
        self.db.log_event(
            "The audio the player had already loaded was queued alongside the "
            "video; the two will be combined into one file.",
            video.id, "info",
        )
        return self.db.get_download(video.id) or video

    def _queue_capture_plan(self, plan: _CapturePlan, *, url: str, title: str,
                            cookies: str, user_agent: str, referer: str,
                            site_headers: dict[str, str] | None,
                            queue_id: int | None, start: bool,
                            stem: str = "", po_token: str = "") -> Download:
        """Queue what the browser already loaded, as one file.

        The headers are the ones the browser itself sent for that exact
        address, per track, falling back to the page's when a capture predates
        the extension that records them. A media CDN decides by header, so
        replaying the wrong set turns a URL that plays in the tab into a 403.
        """
        payload: dict[str, Any] = {
            "cookies": cookies,
            "referrer": referer or url,
            "userAgent": user_agent,
            "headers": plan.video.headers or dict(site_headers or {}),
            "queue_id": queue_id,
            "start": start,
        }
        return self.queue_pair(
            payload, _attested(plan.video.url, po_token),
            _attested(plan.audio.url, po_token) if plan.audio is not None else "",
            title=title, stem=stem or title,
            video_ext="webm" if plan.webm else "mp4",
            audio_ext="webm" if plan.webm else "m4a",
            audio_headers=(plan.audio.headers if plan.audio is not None
                           else None),
        )

    def probe(self, url: str, *, cookies: str = "", user_agent: str = "",
              referer: str = "", site_headers: dict[str, str] | None = None,
              headers: dict[str, str] | None = None) -> dict[str, Any]:
        """Ask the origin about a file without transferring it.

        The Add dialog uses this to show the real size, name and resume
        capability before anything is queued.
        """
        cookies, user_agent, referer, site_headers = self._with_browser_context(
            url, cookies, user_agent, referer, site_headers)
        client = self.client(cookies=cookies, user_agent=user_agent,
                             cookie_url=url, referer=referer,
                             site_headers=site_headers)
        request_headers = dict(headers or {})
        if cookies:
            request_headers.setdefault("Cookie", cookies)
        info = client.probe(url, request_headers)
        return {
            "url": info.url,
            "size": info.size,
            "size_text": format_bytes(info.size) if info.size > 0 else "unknown",
            "supports_ranges": info.supports_ranges,
            "filename": info.filename or "",
            "mime": info.mime,
            "etag": info.etag,
            "last_modified": info.last_modified,
            "digest": info.digest,
        }

    def _analysed(self, url: str, client: HttpClient,
                  options: dict[str, Any]) -> MediaInfo:
        """Analyse a page, reusing a recent analysis of the same request.

        The hover panel analyses a page to build its menu, and the click that
        follows asks for the very same page again — so a download used to begin
        by repeating, in full, work that had finished seconds earlier. That
        repeat *was* the delay between choosing a quality and seeing the row
        appear.

        The key covers everything that changes what an analysis produces: a
        different proof of origin, identity or session yields different
        streams, so those never share an entry.
        """
        key = (
            url,
            str(options.get("po_token") or ""),
            str(options.get("visitor_data") or ""),
            bool(options.get("cookies")),
            str(options.get("player_request") or "")[:64],
        )
        now = time.monotonic()
        with self._extract_lock:
            cached = self._extracts.get(key)
            if cached is not None and now - cached[0] < _ANALYSIS_TTL_SECONDS:
                return cached[1]

        info = run_extractor(url, client, options)

        with self._extract_lock:
            self._extracts[key] = (now, info)
            # Expired entries are dropped here rather than on a timer: the map
            # only grows when pages are analysed, so that is when it is worth
            # tidying.
            if len(self._extracts) > 32:
                self._extracts = {
                    k: v for k, v in self._extracts.items()
                    if now - v[0] < _ANALYSIS_TTL_SECONDS
                }
        return info

    def _remember_po_token(self, token: str, visitor_data: str = "") -> None:
        """Keep the browser's proof-of-origin token for later downloads.

        The token is minted by the page, so only a request that came from the
        browser carries one. Storing it means a download added by hand later —
        from the Add dialog, or a retry after the tab is gone — is attested
        too, instead of stopping a minute into the stream.

        The visitor identity is stored *with* it, because a proof is issued to
        one identity and means nothing presented under another. Keeping the two
        together is what stops a stored token from being replayed beside a
        freshly minted identity, which fails in the least visible way there
        is: the server simply ignores the proof and the stream stops where it
        would have stopped anyway.
        """
        if not token:
            return
        try:
            if token != self.settings.get("youtube_po_token", ""):
                self.settings.set("youtube_po_token", token)
            # Replace the stored identity whenever a token arrives — including
            # clearing it when one arrives without an identity, so a stale
            # pairing is never carried forward beside a newer token.
            if visitor_data != self.settings.get("youtube_visitor_data", ""):
                self.settings.set("youtube_visitor_data", visitor_data)
        except Exception:  # noqa: BLE001 - a token that cannot be saved is
            pass          # still usable for the request that carried it

    def extract(self, url: str, options: dict[str, Any] | None = None, *,
                cookies: str = "", user_agent: str = "", referer: str = "",
                site_headers: dict[str, str] | None = None,
                po_token: str = "", visitor_data: str = "") -> MediaInfo:
        """Run the extractor chain for a page URL."""
        cookies, user_agent, referer, site_headers = self._with_browser_context(
            url, cookies, user_agent, referer, site_headers)
        merged = dict(options or {})
        if po_token:
            merged["po_token"] = po_token
            # The identity the caller supplied belongs to this token; using our
            # own alongside it would invalidate the proof.
            merged["visitor_data"] = visitor_data
            self._remember_po_token(po_token, visitor_data)
        merged.setdefault("po_token", self.settings.get("youtube_po_token", ""))
        merged.setdefault("visitor_data", self.settings.get("youtube_visitor_data", ""))
        merged.setdefault("cookies", cookies)
        merged.setdefault("referer", referer)
        client = self.client(cookies=cookies, user_agent=user_agent,
                             cookie_url=url, referer=referer,
                             site_headers=site_headers)
        return self._analysed(url, client, merged)

    def add_media(self, url: str, format_id: str = "", *, quality: str = "",
                  queue_id: int | None = None, dest_dir: str = "",
                  cookies: str = "", user_agent: str = "", referer: str = "",
                  site_headers: dict[str, str] | None = None,
                  title: str = "", container: str = "", start: bool = True,
                  po_token: str = "", visitor_data: str = "",
                  options: dict[str, Any] | None = None) -> Download:
        """Extract a page, choose a stream, and queue it for download."""
        # One client for extraction *and* for the transfer that follows. A
        # server-driven endpoint is bound to the session that was handed it, so
        # the cookies picked up while extracting have to travel with the
        # download or the origin refuses it.
        cookies, user_agent, referer, site_headers = self._with_browser_context(
            url, cookies, user_agent, referer, site_headers)
        client = self.client(cookies=cookies, user_agent=user_agent,
                             cookie_url=url, referer=referer,
                             site_headers=site_headers)
        merged = dict(options or {})
        if po_token:
            merged["po_token"] = po_token
            merged["visitor_data"] = visitor_data
            self._remember_po_token(po_token, visitor_data)
        merged.setdefault("po_token", self.settings.get("youtube_po_token", ""))
        merged.setdefault("visitor_data", self.settings.get("youtube_visitor_data", ""))
        merged.setdefault("cookies", cookies)
        merged.setdefault("referer", referer)

        requested = quality or self.settings.get("preferred_video_quality", "1080p")
        wanted = quality_to_height(requested)

        # ── Capture first ───────────────────────────────────────────────────
        #
        # What the browser already fetched is consulted *before* an API that
        # can refuse us, not after. This is the ordering a commercial download
        # manager has always had and this one did not: it has no extractor at
        # all, so there is nothing for a challenge to refuse — it replays what
        # the player loaded, inside a session the site has already accepted.
        #
        # Measured on this machine, both directions: extraction of a YouTube
        # watch page is refused for every client identity ("Sign in to confirm
        # you're not a bot") and takes 8.7 s to say so, while the addresses the
        # player used are served. Asking first cost eight seconds to arrive at
        # a failure, and this path skips it entirely.
        #
        # It only takes the fast route when the captures *meet* the quality
        # that was asked for: a plan below it might be beaten by extraction on
        # a connection the site does answer, and silently handing over 360p
        # when 1080p was chosen is the failure this must not introduce. A
        # format chosen by id belongs to an extraction and is never captured,
        # so that request always goes the long way round.
        if not format_id:
            plan = _capture_plan(merged, wanted, allow_shortfall=False)
            if plan is not None:
                self.db.log_event(
                    f"“{title or url}”: taken from what the browser had already"
                    f" loaded — {plan.describe()} — without asking the site, "
                    "which is both faster and not refusable.", None, "info",
                )
                return self._queue_capture_plan(
                    plan, url=url, title=title, cookies=cookies,
                    user_agent=user_agent, referer=referer,
                    site_headers=site_headers, queue_id=queue_id, start=start,
                    po_token=str(merged.get("po_token") or ""),
                )

        # Timed, because "why does YouTube take a few seconds when a plain link
        # is instant" is a question the log should answer rather than a session
        # arguing about it. Reading a watch page is an InnerTube call, the page
        # itself and the player's JavaScript; when the connection is challenged
        # it is nine seconds to be told no (measured here, 9.05 s).
        reading_started = time.monotonic()

        def reading_time() -> str:
            return f"{time.monotonic() - reading_started:.1f} s"

        try:
            info = self._analysed(url, client, merged)
        except (ExtractionError, IXDError):
            # Extraction refused outright, and the captures do not meet the
            # requested quality — so a smaller file is now the choice against
            # no file at all, and the shortfall is allowed.
            plan = _capture_plan(merged, wanted, allow_shortfall=True)
            if plan is None:
                raise
            # The time is on the refusal too. This is the slow case — being
            # told no takes as long as being answered, and longer than the
            # capture route that then runs — so a log that timed only the
            # successes would time everything except the wait being asked about.
            self.db.log_event(
                "The site would not answer an extraction request "
                f"(it took {reading_time()} to say so), so what the browser had "
                f"already loaded was taken instead: {plan.describe()}.",
                None, "info",
            )
            return self._queue_capture_plan(
                plan, url=url, title=title, cookies=cookies,
                user_agent=user_agent, referer=referer,
                site_headers=site_headers, queue_id=queue_id, start=start,
            )
        if time.monotonic() - reading_started >= 1.0:
            self.db.log_event(
                f"Reading the page for “{title or url}” took {reading_time()}.",
                None, "info")
        info = _named_after_its_page(info, url, title)

        session_cookies = client.cookies.header_for(
            urllib.parse.urlparse(url).hostname or ""
        ) or cookies

        chosen = None
        if format_id:
            chosen = next((f for f in info.formats if f.format_id == format_id), None)
        if chosen is None:
            chosen = select_format(
                info.formats, requested,
                self.settings.get_bool("prefer_progressive", True),
                self.settings.get("preferred_video_container", "mp4"),
            )
        if chosen is None:
            raise ExtractionError("no suitable stream was found")

        # What the user is owed an explanation against: the height they picked
        # when they picked one by name, and the requested quality otherwise.
        # Measuring the shortfall after the fallback has run would compare the
        # result against itself and always report nothing.
        wanted_height = chosen.height or 0 if format_id else 0

        # Which server-driven streams an endpoint will actually hand over is
        # not knowable from the page: the same session that serves 360p in full
        # is refused at 720p, sometimes by name and sometimes by an outright
        # refusal, and it varies by video and by where the request comes from.
        # Asking costs one exchange; not asking costs a queued download that
        # fails after the user has walked away.
        refused = ""
        if chosen.sabr:
            asking_started = time.monotonic()
            chosen, refused = self._servable_format(chosen, info, client, user_agent)
            asking_took = time.monotonic() - asking_started
            if asking_took >= 1.0:
                self.db.log_event(
                    "Asking the streaming server which qualities it will "
                    f"actually serve took {asking_took:.1f} s.", None, "info")
            if chosen is None:
                message = (
                    f"the streaming server would not serve any video track for "
                    f"“{info.title or url}” — it answered: {refused}"
                )
                # Written to the log as well as raised. The refusals of the
                # individual rungs were already there, and the conclusion was
                # not — so the log showed six notes about qualities that were
                # declined and nothing saying the download never happened.
                self.db.log_event(message, None, "error")
                raise ExtractionError(message)

        # Selection deliberately falls back to a stream that arrives whole
        # rather than one the site cuts short, and the servability walk falls
        # back again when the server refuses a rendition outright. Either way
        # the file can be a size down from what was asked for, and saying so is
        # the difference between a considered choice and the quality menu
        # appearing to do nothing.
        if wanted_height:
            shortfall = max(0, wanted_height - (chosen.height or 0))
        else:
            shortfall = quality_shortfall(chosen, requested)

        # A link that only covers part of the file is no longer a dead end:
        # the transfer asks for another one when the grant runs out. It is only
        # hopeless when there is no way to ask again.
        if chosen.restricted and not chosen.refresh and not chosen.sabr:
            raise ExtractionError(
                f"“{info.title or url}” is only being offered as a stream this "
                "site will not serve in full, and it gives no way to request "
                "the rest. Playing the video and using the download panel's "
                "“Already loaded by the player” list is the way through."
            )

        # A server-driven stream whose header cannot be fetched will run to the
        # last byte and then be refused for a few kilobytes at the front: the
        # streaming session never sends the initialisation and index segments,
        # and when the response publishes no ordinary URL either there is
        # nowhere to get them. Measured on a real log — the same machine
        # succeeded on streams that publish an index and failed on those that do
        # not, minutes apart, which is what ruled out the network.
        #
        # The browser has usually already fetched a complete progressive stream
        # for the same video, and that one carries its own header. Taking it is
        # a smaller file than was asked for and it is a file.
        # A server-driven format's `url` is its **streaming endpoint**, which is
        # never empty — so testing it here made this whole rescue unreachable,
        # and the field report came back with the identical failure. What
        # decides it is whether the stream needs opening bytes (`header_end`)
        # and has nowhere to fetch them from (`header_url`).
        if _header_unobtainable(chosen) or _header_unobtainable(companion_probe(
                info, chosen)):
            # The rescue is a whole plan rather than a progressive URL: a
            # player that used adaptive streams throughout leaves no
            # progressive address behind at all, and that is the ordinary case
            # now — which is why this rescue kept finding nothing to use.
            plan = _capture_plan(merged, wanted, allow_shortfall=True)
            if plan is not None:
                # Asked before it is queued, not after six minutes of retries.
                refused = self._plain_url_serves(
                    plan.video.url, client,
                    plan.video.headers or dict(site_headers or {}))
                if refused:
                    self.db.log_event(
                        "The stream the browser loaded is refused when this "
                        f"application asks for it ({refused}), so the browser "
                        "is being asked to fetch it and hand the bytes over.",
                        None, "warning",
                    )
                    raise BrowserFetchRequired(
                        plan.video.url,
                        f"{sanitize_filename(info.title or title)}.mp4"
                        if (info.title or title) else "",
                        info.title or title,
                        plan.video.headers or dict(site_headers or {}),
                        plan.video.size,
                        referer or info.webpage_url or url,
                        plan.video.itag,
                    )
            if plan is not None:
                self.db.log_event(
                    f"“{info.title or url}”: the chosen stream publishes no "
                    "index and no ordinary link, so its opening bytes cannot be "
                    "fetched and the file would not open. What the browser "
                    f"already loaded was taken instead: {plan.describe()}.",
                    None, "info",
                )
                return self._queue_capture_plan(
                    plan, url=url, title=info.title or title,
                    cookies=session_cookies,
                    user_agent=user_agent,
                    referer=referer or info.webpage_url or url,
                    site_headers=site_headers, queue_id=queue_id, start=start,
                    stem=suggested_filename(info, chosen, None).rsplit(".", 1)[0],
                    po_token=str(merged.get("po_token") or ""),
                )

            # Nothing captured — the page was opened and downloaded without the
            # video ever being played, which is the ordinary way to use this.
            #
            # Falling through from here is what a field log caught: the row was
            # queued, the transfer opened, and it failed on the first exchange
            # with the very condition tested one line above. A download that is
            # known to be impossible before it starts must not be started.
            #
            # Extraction itself published other streams, and the ones that are
            # not server-driven carry their own opening bytes — that is the
            # same 360p file the page serves as an ordinary link, and it is the
            # route that was already taken when the endpoint refused every rung
            # outright. It is reached here too now: refused and *stranded* are
            # the same outcome for the person waiting for a file.
            # Said out loud, because "the rescue did not fire" and "the rescue
            # had nothing to fire with" look identical in a log and are
            # different defects. A count here is what distinguishes an
            # extension that is not sending its captures from a page that was
            # never played.
            seen = _capture_records(merged)
            self.db.log_event(
                f"The browser sent {len(seen)} media address(es) for this page"
                + (f" ({', '.join(sorted({r.itag for r in seen if r.itag}))})"
                   if seen else "")
                + ", and none of them forms a complete file on its own.",
                None, "info",
            )
            replacement = select_format(
                # Still a video, if a video was asked for. Every audio track on
                # the page survives a filter written only about headers, and
                # `select_format` will take one — answering a request for a
                # picture with sound alone.
                [media for media in info.formats
                 if (media.has_video or not chosen.has_video)
                 and not _header_unobtainable(media)
                 and not _header_unobtainable(companion_probe(info, media))],
                requested,
                self.settings.get_bool("prefer_progressive", True),
                self.settings.get("preferred_video_container", "mp4"),
            )
            # A lower rendition is only an answer if the origin will serve it.
            # Every plain `videoplayback` address in a field log was refused
            # 403 while the *server-driven* route completed downloads in the
            # same session — so a downgrade onto a plain link is a dead end
            # worth detecting in one request rather than in six minutes.
            if replacement is not None and not replacement.sabr:
                refused = self._plain_url_serves(replacement.url, client, headers=None)
                if refused:
                    self.db.log_event(
                        f"“{info.title or url}”: {replacement.describe()} is "
                        f"refused to this application ({refused}) and served to "
                        "the browser, so the browser is fetching it.",
                        None, "warning",
                    )
                    raise BrowserFetchRequired(
                        replacement.url,
                        suggested_filename(info, replacement, None),
                        info.title or title,
                        dict(site_headers or {}),
                        replacement.filesize,
                        referer or info.webpage_url or url,
                        replacement.format_id,
                    )
            if replacement is None or replacement.format_id == chosen.format_id:
                raise ExtractionError(
                    f"“{info.title or url}” is offered at this quality only as "
                    "a streaming session that does not send the file's opening "
                    "bytes, and publishes no ordinary link to them — so the "
                    "file would not open in any player. The site's ordinary "
                    "links are being refused to this application at the moment "
                    "(the browser is served the same addresses without "
                    "complaint), so there is nothing here to fall back to. A "
                    "quality the site streams in full — one that does publish "
                    "an index — still downloads; this one does not."
                )
            self.db.log_event(
                f"“{info.title or url}”: {chosen.describe()} is served as a "
                "streaming session that never sends the file's opening bytes "
                "and publishes no ordinary link to them, so no player would "
                f"open it. {replacement.describe()} was taken instead — it "
                "carries its own.", None, "info",
            )
            chosen = replacement
            # The shortfall is measured against the request again, because the
            # stream being delivered is no longer the one that was chosen.
            wanted_height = 0
            shortfall = quality_shortfall(chosen, requested)

        headers = dict(info.http_headers)
        headers.update(chosen.http_headers)
        # The browser's own headers travel with the transfer as well: what
        # extraction needed to be allowed through, the segment fetches need too.
        for key, value in (site_headers or {}).items():
            headers.setdefault(key, value)
        mode, target_url, segments = prepare_format(chosen, client, headers)

        # A video-only stream would produce a silent file. When the site offers
        # nothing progressive at this quality — which is now the norm on
        # YouTube — the matching audio track is queued alongside it rather than
        # letting the user discover the omission on playback.
        companion = None
        if chosen.has_video and not chosen.has_audio:
            candidate = best_muxable_audio(info.formats, chosen)
            # "Restricted" on a server-driven track means one session will not
            # cover it — which is no longer a dead end, because a session is
            # continued across as many as the track needs. Only a capped plain
            # link, which has no way to ask for the rest, is unfetchable. The
            # old test dropped every server-driven audio track on any video
            # over a minute and delivered the result as a finished download:
            # the right picture, the right length, and no sound.
            usable = candidate is not None and (
                bool(candidate.sabr) or not candidate.restricted
            )
            companion = candidate if usable else None

            if companion is None and any(
                media.has_audio and not media.has_video
                for media in info.formats
            ):
                # The video has sound and we cannot get it. Handing over the
                # picture alone would be a silent film presented as the
                # quality that was asked for — the one defect a viewer does
                # not discover until they are watching it.
                raise ExtractionError(
                    f"“{info.title or url}” has an audio track, but none that "
                    "can be fetched and joined to this quality, so the file "
                    "would have had no sound. Playing the video once in the "
                    "browser and using the download panel's “Already loaded "
                    "by the player” list fetches the tracks the player itself "
                    "used."
                )
        # The companion travels *inside* this download rather than beside it.
        # Two rows for one chosen quality meant the user watched a video finish
        # and a second transfer start on the same file, and it produced the
        # failures that go with two independent lifetimes: a stray audio file
        # when the video failed, a duplicate output when one half was resumed,
        # and a progress bar that reached the end and then grew. One row fetches
        # both tracks and joins them.
        audio_context: dict[str, Any] = {}
        if companion is not None:
            # A server-driven session tracks one playback position, so the two
            # tracks cannot share one — the companion is given a session of its
            # own, and the engine runs them in turn.
            if companion.sabr:
                companion = self._refresh_sabr_session(
                    url, companion, merged, cookies, user_agent) or companion
            audio_headers = dict(info.http_headers)
            audio_headers.update(companion.http_headers)
            prepare_format(companion, client, audio_headers)
            audio_context = {**companion.sabr, "refresh": dict(companion.refresh)}

        # `container` is how the panel offers a stream twice — as the site
        # serves it and as an MP4. The *filename* carries that choice from here
        # on: the engine rewraps a transport stream only when the name it was
        # asked for says `.mp4`, so nothing else has to be persisted and a
        # resume cannot forget which was wanted.
        chosen_name = suggested_filename(info, chosen, segments)
        if container:
            chosen_name = f"{chosen_name.rsplit('.', 1)[0]}.{container}"

        download = self.engine.add_download(
            target_url,
            filename=chosen_name,
            dest_dir=dest_dir,
            queue_id=queue_id,
            headers={k: v for k, v in headers.items() if k.lower() != "user-agent"},
            user_agent=(headers.get("User-Agent", "") or user_agent
                        or self.settings.get("user_agent")),
            # The page, ahead of the address that was analysed: for a captured
            # manifest those are different things, and a CDN checking where the
            # request came from wants the page.
            referer=(headers.get("Referer", "") or referer
                     or info.webpage_url),
            cookies=session_cookies,
            segments=segments,
            mode=mode,
            media_title=info.title,
            format_id=chosen.format_id,
            sabr_context={
                **chosen.sabr,
                "refresh": dict(chosen.refresh),
                # Where this session came from. A streaming endpoint is signed
                # and expires within hours, so a download paused overnight
                # resumes against a link the origin no longer honours — and
                # the page is where a replacement is obtained.
                "page_url": info.webpage_url or url,
                **({"audio": {**audio_context,
                              "page_url": info.webpage_url or url}}
                   if audio_context else {}),
            },
            start=start,
        )

        if audio_context:
            self.db.log_event(
                f"“{info.title}” is offered only as separate tracks at this "
                f"quality, so the {companion.describe()} audio is being "
                "fetched as part of this download and joined to the video "
                "before the file is saved.",
                download.id, "info",
            )

        if shortfall:
            got = self._quality_label(chosen)
            asked = f"{wanted_height}p" if wanted_height else requested
            # Why a lower quality was taken depends on what the site does, and
            # the two are not interchangeable. "Offered only for about the first
            # minute" describes a server-driven stream; saying it about an
            # ordinary playlist that simply does not publish that resolution is
            # an explanation of something that did not happen.
            # The *chosen* stream, not merely the presence of one somewhere in
            # the list. Every YouTube video publishes server-driven renditions,
            # so `any(...)` was always true — and the explanation about a
            # one-minute ceiling appeared underneath an ordinary progressive
            # file that had been taken precisely because it has no such limit.
            if chosen.sabr:
                because = (
                    "A higher quality here is offered only for about the first "
                    "minute, and a part of a file is worth less than the whole "
                    "of a smaller one."
                )
            else:
                because = (
                    "This stream does not publish that resolution — what it "
                    "does publish was taken."
                )
            self.db.log_event(
                f"“{asked}” is not available for “{info.title or url}” as a "
                f"stream this site will serve in full, so {got} was taken "
                f"instead. {because}",
                download.id, "info",
            )

        return download

    def _plain_url_serves(self, url: str, client: HttpClient,
                          headers: dict[str, str] | None = None) -> str:
        """Whether an ordinary media address answers us. ``""`` when it does.

        One range request for a single byte, before anything is queued.
        Everything this is asked about is a *fallback* — a captured stream, or
        a lower rendition chosen because the wanted one was stranded — and a
        fallback that is refused is worth knowing about in the second it takes
        to ask, not after five probe retries and five session renewals. A field
        log shows that costing six minutes per row, three rows in a run, all
        ending in the same 403 the first request would have shown.

        It is also the measurement this needed and did not have: the log now
        says whether the address itself is refused, rather than leaving "the
        route did not work" to cover four different causes.
        """
        try:
            response = client.open_range(url, 0, 0, dict(headers or {}))
        except Exception as exc:                  # noqa: BLE001 - reported
            return str(exc) or "no answer"
        try:
            status = getattr(response, "status", 0)
        finally:
            response.close()
        return "" if status in (200, 206) else f"HTTP {status}"

    def _servable_format(self, chosen: "MediaFormat", info: MediaInfo,
                         client: HttpClient, user_agent: str = ""
                         ) -> tuple["MediaFormat | None", str]:
        """The best stream at or below ``chosen`` that the server will serve.

        Returns the format and, when it is not the one asked for, the reason
        the original was refused. Only video is walked down: an audio track
        that is refused has no lower rung worth substituting, and a silent file
        is not an improvement on a failed one.
        """
        from .extractors.sabr import stream_from_context   # noqa: PLC0415

        if not chosen.has_video:
            return chosen, ""

        # Asking costs one exchange per rung, and the ladder is walked again on
        # every retry of the same video — six rungs at a second or two each,
        # under a button that says "Sending…". What the server will serve does
        # not change from one minute to the next, so the answer is kept.
        verdict_key = (info.webpage_url or "", chosen.format_id,
                       chosen.audio_track_key)
        now = time.time()
        with self._extract_lock:
            for key, (stamp, _) in list(self._servable.items()):
                if now - stamp > self._SERVABLE_TTL:
                    self._servable.pop(key, None)
            remembered = self._servable.get(verdict_key)
        if remembered is not None:
            found, reason = remembered[1]
            if found is not None:
                return found, reason
            # "Nothing was served" is an answer too, and re-walking six rungs at
            # a second or two each to hear it again is a minute of silence under
            # a button. Measured from a real log: the same ladder declined twice
            # in three minutes for the same video.
            return None, reason

        height = chosen.height or 0
        # Candidates: the choice itself, then one server-driven video track per
        # distinct height below it, tallest first. Keeping every codec at every
        # height would spend the whole attempt budget on two renditions of the
        # same refused resolution and never reach a rung that is actually
        # served — which is precisely how this failed the first time.
        below: dict[int, "MediaFormat"] = {}
        for candidate in info.formats:
            if candidate is chosen or not candidate.sabr or not candidate.has_video:
                continue
            rung = candidate.height or 0
            if not 0 < rung < height:
                continue
            current = below.get(rung)
            if current is None or candidate.tbr > current.tbr:
                below[rung] = candidate
        # The chosen rendition's own height is already the top of the ladder,
        # so keeping another entry at that height spends an attempt on a second
        # copy of a resolution just refused — the log showed "480p" declined
        # twice in a row for exactly that reason.
        below.pop(height, None)
        ladder = [chosen] + [below[k] for k in sorted(below, reverse=True)]

        first_reason = ""
        for candidate in ladder[:_SERVABLE_ATTEMPTS]:
            try:
                stream = stream_from_context(
                    client, candidate.sabr,
                    user_agent or candidate.http_headers.get("User-Agent", ""),
                )
                reason = stream.probe()
            except Exception as exc:             # noqa: BLE001 - treat as refusal
                reason = f"{type(exc).__name__}: {exc}"
            if not reason:
                with self._extract_lock:
                    self._servable[verdict_key] = (
                        time.time(), (candidate, first_reason))
                return candidate, first_reason
            if not first_reason:
                first_reason = reason
            self.db.log_event(
                f"the streaming server would not serve "
                f"{self._quality_label(candidate)} — {reason}",
                None, "info",
            )

        # Every server-driven rung refused. That is not the end of it: a video
        # often also publishes an ordinary fetchable stream — a progressive one
        # the player itself used — and a smaller file that arrives beats a
        # better one that does not exist. Failing outright here handed the user
        # an error for a video they could have had.
        fallback = None
        for candidate in info.formats:
            if candidate.sabr or not candidate.has_video or candidate.restricted:
                continue
            if not candidate.url:
                continue
            if fallback is None or (candidate.height or 0) > (fallback.height or 0):
                fallback = candidate
        if fallback is None:
            with self._extract_lock:
                self._servable[verdict_key] = (time.time(), (None, first_reason))
            return None, first_reason

        # A downgrade is only worth taking if the thing downgraded *to* answers.
        #
        # This walk refused every rung and handed the person a progressive
        # address, and the field log of 2026-08-12 shows what that is worth:
        # five renewals and five probe retries ending in 403, on the same
        # address measured refused to every client there is — including the
        # youtube.com page that minted it (§271). Meanwhile the server-driven
        # stream this walk had just declared unservable had, three runs
        # running, delivered **100% of its media**.
        #
        # So the probe is not authoritative, and trading a stream that has
        # worked for one that never has is the worst of the available moves.
        # It costs one range request to find out which is which.
        refusal = ""
        try:
            refusal = self._plain_url_serves(fallback.url, client,
                                             fallback.http_headers)
        except Exception as exc:  # noqa: BLE001 - an unaskable address is refused
            refusal = f"{type(exc).__name__}: {exc}"
        if refusal:
            self.db.log_event(
                f"no server-driven rendition was served, and the "
                f"{self._quality_label(fallback)} stream this site serves as an "
                f"ordinary file is refused as well ({refusal}) — so the "
                f"{self._quality_label(chosen)} server-driven stream is being "
                "attempted anyway, which is the one that has actually "
                "delivered media here.",
                None, "info",
            )
            with self._extract_lock:
                self._servable[verdict_key] = (
                    time.time(), (chosen, first_reason))
            return chosen, first_reason

        with self._extract_lock:
            self._servable[verdict_key] = (
                time.time(), (fallback, first_reason))
        self.db.log_event(
            f"no server-driven rendition was served, so the "
            f"{self._quality_label(fallback)} stream the site serves as an "
            "ordinary file was taken instead.",
            None, "info",
        )
        return fallback, first_reason

    def _renew_sabr_session_for(self, page_url: str, itag: str,
                                is_audio: bool,
                                xtags: str = "") -> dict[str, Any] | None:
        """Open a new streaming session for a stream already part-fetched.

        Only the session is replaced — the endpoint, its configuration and the
        proof it was opened with. The bytes already on disk describe the same
        stream and are kept, so this costs one extraction and no re-fetching.
        """
        if not page_url or not itag:
            return None
        try:
            client = self.client(cookies="", user_agent="", cookie_url=page_url)
            info = self._analysed(page_url, client, {
                "po_token": self.settings.get("youtube_po_token", ""),
                "visitor_data": self.settings.get("youtube_visitor_data", ""),
            })
        except Exception:  # noqa: BLE001 - the caller reports the original fault
            return None

        # The itag alone does not identify a track — a video's original audio
        # and its machine dubbings all share one, and are told apart only by
        # their tags. Renewing on the itag would open a session for whichever
        # language came back first and continue writing it into a part-file
        # holding another, so the tags carried by the download decide.
        def matches(media: "MediaFormat") -> bool:
            if media.format_id != itag or not media.sabr:
                return False
            if bool(media.sabr.get("is_audio")) != is_audio:
                return False
            return str(media.sabr.get("xtags") or "") == xtags

        match = next((media for media in info.formats if matches(media)), None)
        if match is None and xtags:
            # The stream is no longer published under those tags. A session for
            # a different track would corrupt what is already on disk, so the
            # caller is told there is nothing rather than handed the wrong one.
            return None
        return dict(match.sabr) if match is not None else None

    def _renew_media_url(self, recipe: dict[str, Any], seconds: float) -> str:
        """Ask the site for this stream again, starting from ``seconds``.

        Signed media links expire and are issued covering only part of a long
        file. Rather than treat that as a failure, the transfer asks for
        another one describing the same stream from the position it needs.
        """
        from .extractors.youtube import CLIENTS, YouTubeExtractor  # noqa: PLC0415

        video_id = recipe.get("video_id") or ""
        itag = str(recipe.get("itag") or "")
        if not video_id or not itag:
            return ""

        client = next((c for c in CLIENTS if c.key == recipe.get("client")), None)
        extractor = YouTubeExtractor(self.client(), {})
        for candidate in ([client] if client else []) + list(CLIENTS):
            if candidate is None:
                continue
            try:
                response = extractor._call_player(video_id, candidate, int(seconds))
            except Exception:  # noqa: BLE001 - try the next identity
                continue
            streaming = response.get("streamingData") or {}
            entries = list(streaming.get("formats") or []) + \
                list(streaming.get("adaptiveFormats") or [])
            for entry in entries:
                if str(entry.get("itag")) == itag and entry.get("url"):
                    return entry["url"]
        return ""

    def _refresh_sabr_session(self, url: str, wanted: "MediaFormat",
                              options: dict[str, Any], cookies: str,
                              user_agent: str) -> "MediaFormat | None":
        """Re-extract to give one stream a streaming session of its own."""
        try:
            client = self.client(cookies=cookies, user_agent=user_agent,
                                 cookie_url=url)
            fresh = run_extractor(url, client, dict(options))
        except Exception:  # noqa: BLE001 - the existing session is the fallback
            return None

        # The stream identity is not the itag alone. A video's original audio
        # and its machine dubbings are published under the *same* itag and are
        # told apart only by their track tags — so matching on the itag could
        # hand back a different language's audio, and the substitution is
        # discovered on playback. The tags are part of the identity.
        def same_track(candidate: "MediaFormat") -> bool:
            return (
                candidate.format_id == wanted.format_id
                and bool(candidate.sabr)
                and candidate.audio_language == wanted.audio_language
                and candidate.audio_kind == wanted.audio_kind
                and (candidate.sabr.get("xtags") or "")
                == (wanted.sabr.get("xtags") or "")
            )

        exact = next((f for f in fresh.formats if same_track(f)), None)
        if exact is not None:
            return exact
        # Nothing identical came back. Keeping the session already in hand is
        # better than silently swapping the track it describes.
        return None

    def list_downloads(self) -> list[Download]:
        downloads = self.db.list_downloads()
        # Overlay live speed/ETA from running tasks.
        for download in downloads:
            task = self.engine.task_for(download.id) if download.id else None
            if task is not None and task.running:
                download.speed = task.download.speed
                download.eta = task.download.eta
                download.chunks = task.download.chunks
                download.live_workers = task.download.live_workers
        return downloads

    def list_for_display(self) -> list[Download]:
        """The download list as a person should see it: one row per file.

        A quality chosen on an adaptive site arrives as two transfers, and they
        become one file. Listing both puts a video and an audio row side by
        side for something the user asked for once and will receive once — and
        the audio row vanishes on its own when the two are combined, which
        reads as a download disappearing.

        The pair is therefore shown as the single item it is: the video row,
        carrying the sum of both transfers so the progress describes the file
        being produced rather than half of it.
        """
        rows = self.list_downloads()
        audio_by_group: dict[str, Download] = {}
        for row in rows:
            if row.mux_group and row.mux_group.endswith(":audio"):
                audio_by_group[row.mux_group.rsplit(":", 1)[0]] = row

        shown: list[Download] = []
        for row in rows:
            if row.mux_group and row.mux_group.endswith(":audio"):
                continue
            partner = (audio_by_group.get(row.mux_group.rsplit(":", 1)[0])
                       if row.mux_group else None)
            if partner is not None:
                # A transfer that has not begun reports no size yet, so summing
                # the two would show the video's size alone, reach 100% when
                # the video finished, and then *grow* as the audio started —
                # which reads as a finished download starting over. The size
                # the site declared is known from the outset and is used until
                # the transfer establishes its own.
                row.total_size = self._expected_size(row) + self._expected_size(partner)
                row.downloaded = (row.downloaded or 0) + (partner.downloaded or 0)
                row.speed = (row.speed or 0) + (partner.speed or 0)
                # The pair finishes when the slower half does.
                row.eta = max(row.eta or 0, partner.eta or 0)
                # Nor is it finished while half of it is still arriving.
                if (row.status is DownloadStatus.COMPLETED
                        and partner.status is not DownloadStatus.COMPLETED):
                    row.status = partner.status
            elif not row.total_size:
                # The same fallback for a stream with no partner — an audio
                # track on its own, or a quality published whole. Only the
                # paired branch had it, so those rows sat at "unknown size"
                # until the first response came back, which is the other half
                # of "it doesn't grab the size for YouTube". Display only: the
                # stored row still learns its size from the transfer.
                row.total_size = self._expected_size(row)
            shown.append(row)
        return shown

    @staticmethod
    def _expected_size(download: Download) -> int:
        """The size of a transfer, falling back to what the site declared."""
        if download.total_size:
            return download.total_size
        try:
            return int((download.sabr_context or {}).get("size") or 0)
        except (TypeError, ValueError):
            return 0

    def expected_total(self, download_id: int) -> int:
        """What the finished file will weigh, before a byte has been fetched.

        A transfer that has not started reports `total_size == 0` — it learns
        its own size from the first response — so anything asking a fresh row
        how big it is gets nothing. What the *site* declared is kept in the
        stream's session context from the moment it was extracted, and a paired
        quality is the sum of both halves.

        This is the same arithmetic `list_for_display` does for the row in the
        list. It is here as well because the file-info window asks before the
        row has ever been drawn, and answered "size not published" for every
        YouTube video while the list beside it showed the size.
        """
        return sum(self._expected_size(row)
                   for row in self.mux_companions(download_id))

    def get_download(self, download_id: int) -> Download | None:
        download = self.db.get_download(download_id)
        if download is not None:
            task = self.engine.task_for(download_id)
            if task is not None and task.running:
                download.speed = task.download.speed
                download.eta = task.download.eta
                download.chunks = task.download.chunks
                download.live_workers = task.download.live_workers
            else:
                download.chunks = self.db.load_chunks(download_id)
        return download

    def resume(self, download_id: int) -> bool:
        """Somebody pressed Resume on this one download.

        `by_hand` is what tells the engine this is an instruction about a
        download rather than the queue's own turn coming round — see
        `_started_by_hand`. Without it, Resume inside a paused queue put the
        row straight back to *Queued* and said nothing.
        """
        return self.engine.start_download(download_id, by_hand=True)

    def pause(self, download_id: int) -> None:
        self.engine.pause_download(download_id)

    def cancel(self, download_id: int) -> None:
        self.engine.cancel_download(download_id)

    def remove(self, download_id: int, delete_files: bool = False) -> None:
        self.engine.remove_download(download_id, delete_files)

    def mux_companions(self, download_id: int) -> list["Download"]:
        """Every row that becomes the same file as this one, itself included.

        A chosen quality is **one** file, and above 360p it arrives as two
        streams (§3.3). So anything done to a paired download has to be done to
        its partner or the result is half of a film: starting one and leaving
        the other queues a video that waits for ever for its sound, and
        removing one leaves an orphan nothing will ever combine.
        """
        download = self.db.get_download(download_id)
        if download is None:
            return []
        if not download.mux_group:
            return [download]
        token = download.mux_group.rsplit(":", 1)[0]
        return [row for row in self.db.list_downloads()
                if row.mux_group and row.mux_group.rsplit(":", 1)[0] == token]

    def swap_link(self, download_id: int, new_url: str) -> bool:
        return self.engine.swap_link(download_id, new_url)

    def set_expected_hash(self, download_id: int, value: str, algorithm: str = "") -> None:
        self.engine.set_expected_hash(download_id, value, algorithm)

    def reverify(self, download_id: int) -> None:
        self.engine.reverify(download_id)

    def pause_all(self) -> None:
        for download in self.db.list_downloads():
            if download.status.is_active or download.status is DownloadStatus.QUEUED:
                self.engine.pause_download(download.id)

    def resume_all(self) -> None:
        for download in self.db.list_downloads():
            if download.status in (DownloadStatus.PAUSED, DownloadStatus.ERROR,
                                   DownloadStatus.SCHEDULED):
                # "All" means all, including the ones a paused queue is
                # holding — otherwise pressing this while a schedule has a
                # queue down does nothing and explains nothing, which is the
                # same complaint as the single Resume in a different shape.
                self.engine.allow_by_hand(download.id)
                self.db.update_download_fields(download.id, status=DownloadStatus.QUEUED)
        self.engine._pump_event.set()

    def clear_completed(self) -> None:
        for download in self.db.list_downloads(DownloadStatus.COMPLETED):
            self.db.delete_download(download.id)
        self.events.emit(EventType.DOWNLOAD_REMOVED, download_id=None)

    # ------------------------------------------------------------------
    # queues, schedules, proxies
    # ------------------------------------------------------------------
    def list_queues(self) -> list[DownloadQueue]:
        return self.db.list_queues()

    def save_queue(self, queue: DownloadQueue) -> int:
        if queue.id is None:
            return self.db.insert_queue(queue)
        self.db.update_queue(queue)
        return queue.id

    def delete_queue(self, queue_id: int) -> None:
        self.db.delete_queue(queue_id)

    def list_schedules(self) -> list[Schedule]:
        return self.db.list_schedules()

    def save_schedule(self, schedule: Schedule) -> int:
        if schedule.id is None:
            schedule_id = self.db.insert_schedule(schedule)
        else:
            self.db.update_schedule(schedule)
            schedule_id = schedule.id
        self.scheduler.tick()
        return schedule_id

    def delete_schedule(self, schedule_id: int) -> None:
        self.db.delete_schedule(schedule_id)
        self.scheduler.tick()

    def list_proxies(self) -> list[ProxyEntry]:
        return self.db.list_proxies()

    def save_proxy(self, proxy: ProxyEntry) -> int:
        if proxy.id is None:
            proxy_id = self.db.insert_proxy(proxy)
        else:
            self.db.update_proxy(proxy)
            proxy_id = proxy.id
        self.engine.proxies.refresh()
        return proxy_id

    def add_proxy_url(self, url: str, label: str = "") -> int:
        return self.save_proxy(parse_proxy_url(url, label))

    def delete_proxy(self, proxy_id: int) -> None:
        self.db.delete_proxy(proxy_id)
        self.engine.proxies.refresh()

    def test_proxy(self, proxy: ProxyEntry, url: str = "https://example.com") -> tuple[bool, str]:
        """Make one real request through ``proxy`` and report the outcome."""
        from .core.net import NetworkProfile

        profile = NetworkProfile(
            proxy=proxy,
            interface=self.settings.get("network_interface", ""),
            timeout=15.0,
            verify_tls=self.settings.get_bool("verify_tls", True),
            user_agent=self.settings.get("user_agent"),
        )
        started = time.time()
        try:
            with HttpClient(profile).request("GET", url) as response:
                response.read(1024)
                elapsed = (time.time() - started) * 1000
                return True, f"HTTP {response.status} in {elapsed:.0f} ms"
        except Exception as exc:  # noqa: BLE001 - the message is the result
            return False, str(exc)

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        tasks = self.engine.active_tasks()
        base = self.db.stats()
        base.update({
            "active": len(tasks),
            "speed": sum(t.download.speed for t in tasks),
            "speed_text": format_bytes(sum(t.download.speed for t in tasks)) + "/s",
            "limit": self.engine.global_limiter.rate,
            "proxy": (self.engine.proxies.current().as_url()
                      if self.engine.proxies.current() else "direct"),
            "proxy_pool": self.engine.proxies.pool_size,
        })
        return base

    def set_global_limit(self, bytes_per_second: int) -> None:
        self.engine.set_global_limit(bytes_per_second)

    # ------------------------------------------------------------------
    # JSON command dispatch (used by the IPC / native-messaging bridge)
    # ------------------------------------------------------------------
    def handle_command(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        handler: Callable[[dict[str, Any]], Any] | None = getattr(
            self, f"_cmd_{command}", None
        )
        if handler is None:
            return {"ok": False, "error": f"unknown command: {command}"}
        try:
            result = handler(params)
            return {"ok": True, "result": result}
        except (IXDError, ValueError, KeyError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    # -- command implementations ---------------------------------------
    def _cmd_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        from . import __version__
        return {"pong": True, "version": __version__, "pid": _process_id()}

    def _cmd_add(self, params: dict[str, Any]) -> dict[str, Any]:
        download = self.add_from_browser(params)
        return download.to_public_dict()

    # ------------------------------------------------------------------
    # Bytes fetched by the browser
    #
    # Measured on 2026-08-12, one address, one machine, one second: this
    # application 403, `curl` with browser headers 403, `curl` bare 403, and
    # **Chrome itself, from a youtube.com page, 403**. Four clients, one
    # answer — so no header, credential, TLS profile or HTTP version available
    # here can open that route, and sessions 74–77 were spent proving it the
    # long way.
    #
    # What *is* served is an address the browser's own signed-in player minted,
    # to that player's session. So the transport moves: the extension fetches,
    # and this receives. The application remains the download manager — it
    # names the file, owns the row, writes the bytes and publishes the result —
    # and the browser is reduced to the socket, which is the one part of this
    # it can do and we cannot.
    # ------------------------------------------------------------------
    def _cmd_browser_media_head(self, params: dict[str, Any]) -> dict[str, Any]:
        """Take the head of a response the page hook teed, and keep any opening.

        `content/page_tee.js` wraps `fetch` and `XMLHttpRequest` in the page's
        own world — the thing `idm/document.js` does and this did not — and
        hands over the first part of each media response. What arrives is the
        streaming protocol's framing, which this application already reads.

        Only one thing is wanted out of it: a stream's **initialisation
        segment**, the piece a server-driven session never sends and that no
        request from here can obtain. Measured over 2026-08-12: the address is
        refused on a second fetch even to the page that minted it, the session
        delivers 100% of the media and never the opening, and the endpoint
        answers an ordinary GET with framed protocol. The player receives those
        bytes — it plays the video — so they are taken from where they arrive.

        Everything else in the response is dropped. This is not a second
        transfer path; it is the few kilobytes the transfer path cannot get.
        """
        raw = str(params.get("data") or "")
        if not raw:
            return {"taken": False}
        try:
            payload = base64.b64decode(raw)
        except (ValueError, TypeError):
            return {"taken": False}

        found = self._openings_in(payload)
        if not found:
            return {"taken": False}

        page = _page_key(str(params.get("page_url") or ""))
        kept: list[str] = []
        with self._openings_lock:
            for itag, opening in found.items():
                key = (page, str(itag))
                previous = self._openings.get(key)
                # A longer copy wins: a segment split across replies arrives in
                # pieces, and the first piece on its own would be an opening
                # that describes bytes it does not contain — the exact defect
                # this project has published twice.
                if previous is not None and len(previous[1]) >= len(opening):
                    continue
                self._openings[key] = (time.time(), opening)
                kept.append(f"{itag} ({len(opening):,} bytes)")

        if kept:
            self.db.log_event(
                "[browser] the page's own player received the opening of "
                + ", ".join(f"itag {entry}" for entry in kept)
                + " — kept, because the streaming session never sends it",
                None, "info",
            )
        return {"taken": bool(kept)}

    @staticmethod
    def _openings_in(payload: bytes) -> dict[int, bytes]:
        """Initialisation segments inside a teed reply, by itag.

        The framing is read with the application's own reader. A media header
        names its stream and the byte it starts at; a block starting at zero is
        the opening, whether or not the header troubles to set `is_init_seg` —
        which, measured, it does not always do.
        """
        from .extractors.sabr import (           # noqa: PLC0415
            PART_MEDIA, PART_MEDIA_HEADER, iter_parts,
        )
        from .core.protobuf import parse         # noqa: PLC0415

        openings: dict[int, bytearray] = {}
        headers: dict[int, tuple[int, int]] = {}    # id -> (itag, start)
        try:
            parts = list(iter_parts(payload))
        except Exception:      # noqa: BLE001 - a truncated head is ordinary
            return {}

        for kind, body in parts:
            if kind == PART_MEDIA_HEADER:
                try:
                    fields = parse(body)
                except Exception:  # noqa: BLE001
                    continue
                itag = fields.get(3)
                if not isinstance(itag, int):
                    continue
                start = fields.get(6)
                start = start if isinstance(start, int) else 0
                headers[fields.get(1, 0) or 0] = (itag, start)
            elif kind == PART_MEDIA and body:
                from .extractors.sabr import read_varint   # noqa: PLC0415
                try:
                    header_id, cursor = read_varint(body, 0)
                except Exception:  # noqa: BLE001
                    continue
                named = headers.get(header_id)
                if named is None:
                    continue
                itag, start = named
                if start != 0:
                    continue          # media, not the opening
                openings.setdefault(itag, bytearray()).extend(body[cursor:])

        # An opening announces itself, exactly as every other body in this tree
        # is required to: an ISOBMFF file's first box header names `ftyp`.
        # Anything else is not written anywhere near byte zero of a file.
        return {itag: bytes(data) for itag, data in openings.items()
                if len(data) > 8 and bytes(data[4:8]) in (b"ftyp", b"styp")}

    def _lookup_opening(self, itag: str, page_url: str) -> bytes:
        """An opening the page's player received, for this stream. ``b""`` if none."""
        page = _page_key(page_url)
        with self._openings_lock:
            entry = self._openings.get((page, str(itag)))
            if entry is None:
                # The page a download was started from is not always the page
                # the player ran in — an embed, or a watch page that has since
                # navigated. An itag is specific enough to stand on its own.
                for (_page, stored_itag), value in self._openings.items():
                    if stored_itag == str(itag):
                        entry = value
                        break
        if entry is None:
            return b""
        return entry[1]

    def _cmd_browser_stream_begin(self, params: dict[str, Any]) -> dict[str, Any]:
        """Open a file for bytes the extension is about to send."""
        title = str(params.get("title") or "")
        name = sanitize_filename(str(params.get("filename") or "")) or (
            f"{sanitize_filename(title)}.mp4" if title else "video.mp4")
        dest_dir = str(params.get("dest_dir") or "") or self.settings.get(
            "download_dir", str(config._default_download_dir()))
        try:
            total = int(params.get("size") or 0)
        except (TypeError, ValueError):
            total = 0

        download = Download(
            url=str(params.get("url") or ""),
            filename=name,
            dest_dir=dest_dir,
            total_size=total,
            status=DownloadStatus.DOWNLOADING,
            media_title=title,
            referer=str(params.get("referrer") or ""),
        )
        download.id = self.db.insert_download(download)

        config.ensure_dirs()
        temp = Path(config.TEMP_DIR) / f"browser-{download.id}.part"
        handle = open(temp, "wb")
        with self._browser_lock:
            self._browser_streams[download.id] = {
                "handle": handle, "temp": temp, "written": 0,
                "download": download,
            }
        self.db.update_download_fields(download.id, temp_path=str(temp))
        self.db.log_event(
            f"“{title or name}”: this address is refused to the application "
            "and served to the browser, so the browser is fetching it and "
            "handing the bytes over.", download.id, "info",
        )
        return {"id": download.id, "filename": name}

    def _cmd_browser_stream_chunk(self, params: dict[str, Any]) -> dict[str, Any]:
        """Accept one block of what the browser is reading."""
        import base64

        stream_id = int(params.get("id") or 0)
        with self._browser_lock:
            state = self._browser_streams.get(stream_id)
        if state is None:
            raise IXDError(f"no browser transfer {stream_id} is open")
        blob = base64.b64decode(str(params.get("data") or ""))
        state["handle"].write(blob)
        state["written"] += len(blob)
        # The row moves while it moves, or a person watching sees nothing.
        self.db.update_download_fields(
            stream_id, downloaded=state["written"],
            **({"total_size": state["written"]}
               if not state["download"].total_size else {}))
        return {"received": state["written"]}

    def _cmd_browser_stream_end(self, params: dict[str, Any]) -> dict[str, Any]:
        """Close the file and publish it, or discard a transfer that failed."""
        stream_id = int(params.get("id") or 0)
        ok = bool(params.get("ok", True))
        with self._browser_lock:
            state = self._browser_streams.pop(stream_id, None)
        if state is None:
            raise IXDError(f"no browser transfer {stream_id} is open")
        state["handle"].close()
        temp: Path = state["temp"]
        download: Download = state["download"]

        if not ok or state["written"] <= 0:
            temp.unlink(missing_ok=True)
            self.db.update_download_fields(
                stream_id, status=DownloadStatus.ERROR.value,
                error=str(params.get("error") or "the browser could not read it"))
            return {"id": stream_id, "completed": False}

        # Named and placed by the same rules every other download uses, so a
        # file that arrived this way is indistinguishable from one that did not.
        target = Path(download.dest_dir) / download.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        stem, dot, suffix = target.name.rpartition(".")
        index = 1
        while target.exists():
            target = target.parent / (
                f"{stem or target.name} ({index}){dot}{suffix}")
            index += 1
        # `replace` is atomic and fails across filesystems, which is the
        # ordinary case: the incomplete file lives under the data directory and
        # a person's Downloads folder is frequently another disk entirely.
        try:
            temp.replace(target)
        except OSError:
            import shutil               # noqa: PLC0415 - the rare path only
            shutil.move(str(temp), str(target))

        self.db.update_download_fields(
            stream_id, status=DownloadStatus.COMPLETED.value,
            downloaded=state["written"], total_size=state["written"],
            dest_dir=str(target.parent), filename=target.name, error="")
        self.db.log_event(
            f"Completed: {target}", stream_id, "info")
        self.events.emit(EventType.DOWNLOAD_COMPLETED,
                         id=stream_id, path=str(target))
        return {"id": stream_id, "completed": True, "path": str(target),
                "bytes": state["written"]}

    def _cmd_add_pair(self, params: dict[str, Any]) -> dict[str, Any]:
        """Queue two URLs the browser's player already fetched, as one file.

        This is the route that works where extraction cannot: the URLs come
        from a session the site has already attested, so they are served past
        the point at which our own requests are refused. They are ordinary
        signed media links by then — no extraction, no format selection, and
        nothing for the streaming server to object to.

        An adaptive stream is video *or* audio, so the two are tied together by
        a shared mux group and combined on arrival. Without the companion the
        result would be a silent film, which is the complaint this exists to
        answer.
        """
        # A captured URL is a bare `videoplayback` request, so there is no name
        # to derive a filename from — without the page's title every download
        # would land as "videoplayback".
        return self.queue_pair(
            params, params["url"],
            params.get("audioUrl", "") or params.get("audio_url", ""),
            title=params.get("title", "") or "",
        ).to_public_dict()

    def _cmd_add_media(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._add_media_command(params)
        except BrowserFetchRequired as delegated:
            # Answered to the extension, which is the only party that can make
            # this request. It fetches and streams the bytes back through
            # `browser_stream_*`.
            return {"browser_fetch": delegated.instruction}

    def _add_media_command(self, params: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        download = self._add_media_from_browser(params)
        took = time.monotonic() - started
        # The whole click-to-row time, in one line. The phases inside it log
        # themselves when they are slow, so a log that shows six seconds here
        # and nothing else means the six seconds were not the page or the
        # server — which is worth as much as knowing that they were.
        self.db.log_event(
            f"Queued “{download.get('filename') or 'a stream'}” "
            f"{took:.1f} s after the click.", download.get("id"), "info")
        return download

    def _add_media_from_browser(self, params: dict[str, Any]) -> dict[str, Any]:
        download = self.add_media(
            params["url"],
            params.get("format_id", ""),
            quality=params.get("quality", ""),
            cookies=params.get("cookies", ""),
            user_agent=params.get("userAgent", "") or params.get("user_agent", ""),
            referer=_referrer_of(params),
            site_headers=_site_headers_of(params),
            title=params.get("title", "") or params.get("pageTitle", ""),
            container=str(params.get("container", "") or "").lstrip(".").lower(),
            po_token=params.get("poToken", "") or params.get("po_token", ""),
            visitor_data=(params.get("visitorData", "")
                          or params.get("visitor_data", "")),
            queue_id=params.get("queue_id"),
            # False when a window is going to ask about this one first: the row
            # is created and the streams are resolved, and nothing is fetched
            # until somebody says so.
            start=bool(params.get("start", True)),
            options={
                "player_request": (params.get("playerRequest", "")
                                   or params.get("player_request", "")),
                "player_endpoint": (params.get("playerEndpoint", "")
                                    or params.get("player_endpoint", "")),
                # Everything the browser was seen fetching for this page. Used
                # only as a rescue when the published streams cannot yield a
                # header — see `_captured_progressive`.
                "captured": params.get("captured") or [],
            },
        )
        return download.to_public_dict()

    def _cmd_present(self, params: dict[str, Any]) -> dict[str, Any]:
        """Take a page over from the browser, with the session that made it work.

        The extension is a relay — it watches, it does not decide. This is the
        hand-off: the address it found, the page it was found on, and the
        headers and cookies that got it served. Everything a person then chooses
        happens in the application, where the choosing belongs.

        The window is opened by the GUI, which registers `present` over the top
        of this; when no window is running, remembering the session is still the
        useful half, because the next thing to ask for that address will find it.
        """
        url = params.get("url", "") or ""
        if not url:
            raise ValueError("nothing was handed over")
        context = {
            "cookies": params.get("cookies", "") or "",
            "user_agent": (params.get("userAgent", "")
                           or params.get("user_agent", "")),
            "referer": _referrer_of(params),
            "headers": _site_headers_of(params),
        }
        self.remember_browser_context(url, context)
        for extra in params.get("streams") or []:
            if isinstance(extra, str) and extra:
                self.remember_browser_context(extra, context)
        return {"url": url, "remembered": True}

    def _cmd_log(self, params: dict[str, Any]) -> dict[str, Any]:
        """Record something that happened in the browser, in the app's own log.

        Half of what goes wrong lives on the other side of the native-messaging
        bridge, where the only witness is a service-worker console the user has
        to know how to open. Diagnosing it by correspondence costs a round trip
        per question. One log, holding both halves in order, is what makes a
        real test on a real site produce evidence instead of an impression.
        """
        message = str(params.get("message", ""))[:2000]
        if not message:
            raise ValueError("nothing to log")
        level = str(params.get("level", "info"))
        self.db.log_event(f"[browser] {message}", None,
                          level if level in ("info", "warning", "error") else "info")
        return {"logged": True}

    def _cmd_events(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Recent log lines, newest first."""
        return self.db.recent_events(int(params.get("limit", 300) or 300))

    def _cmd_probe(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.probe(
            params["url"],
            cookies=params.get("cookies", ""),
            user_agent=params.get("userAgent", "") or params.get("user_agent", ""),
            referer=_referrer_of(params),
            site_headers=_site_headers_of(params),
        )

    @staticmethod
    def _quality_label(media: "MediaFormat") -> str:
        """What to call this stream in a menu: its resolution, nothing else.

        Container names, stream identifiers and which client happened to
        supply it are implementation detail. Someone choosing a download wants
        the resolution, the title and the size.
        """
        if media.has_audio and not media.has_video:
            return "Audio only"
        label = media.quality_label or (f"{media.height}p" if media.height else "")
        if not label:
            return "Video"
        if media.fps and media.fps >= 50 and "p" in label and str(int(media.fps)) not in label:
            label = f"{label}{int(media.fps)}"
        return label

    @staticmethod
    def presentable_formats(formats: list["MediaFormat"],
                            preferred_container: str = "") -> list["MediaFormat"]:
        """One entry per quality, which is what a person is choosing between.

        A page commonly offers the same resolution several times over — a
        progressive copy, an adaptive one, another in a different container.
        They are one choice as far as the viewer is concerned, so the best of
        each is kept: one that can be delivered whole beats one that cannot,
        and past that the larger file is the better copy. Audio is collapsed
        the same way, to a single "Audio only" entry.

        The list reads tallest first, which is the order a quality menu is
        expected in. Extraction orders its own results by what can be
        delivered whole — a rule that belongs to *choosing* a stream, not to
        showing the choices, and one that otherwise puts a complete 360p copy
        above the 1080p entry the user came for.

        The single audio entry has to be chosen by *track* and not by size.
        Every language of a dubbed video is the same recording re-voiced, so
        the files land within a kilobyte of each other and "the larger copy"
        picks a language at random — which is how the audio row came to offer
        a German dub of an English video.
        """
        def video_rank(media: "MediaFormat") -> tuple:
            # "The larger copy is the better copy" holds *within* a container
            # and not across one. At 60fps a resolution is published as both
            # WebM and MP4, and the WebM is simply the bigger file — 198 MB
            # against 136 MB for the same 1080p60 — so ranking on size alone
            # made this menu hand out WebM every time, whatever the container
            # preference said. Clicking a row sends its format id, which
            # bypasses selection entirely, so the preference has to be applied
            # here as well or it applies only when nothing is clicked.
            return (
                not media.restricted,
                1 if (preferred_container and media.ext == preferred_container) else 0,
                media.filesize or 0,
            )

        best: dict[tuple, "MediaFormat"] = {}
        for media in formats:
            # One row per resolution — but a great many streams never state a
            # resolution at all (`RESOLUTION` is optional in HLS), and keying
            # on height alone collapsed every quality of such a stream into a
            # single row, which then offered whichever happened to come first.
            # When the height is unknown the bitrate is what a viewer is
            # choosing between, so it takes its place.
            # Video only. Audio must still collapse to a single row chosen by
            # *track*: every language of a dubbed video has its own bitrate,
            # so splitting audio by bitrate puts one row per language back in
            # the menu and lets the loudest win — which is the German-dub
            # defect this project spent three sessions on.
            spread = (round(media.tbr)
                      if media.has_video and not media.height else 0)
            key = (bool(media.has_video), media.height or 0, spread)
            current = best.get(key)
            if current is None:
                best[key] = media
                continue
            if media.has_audio and not media.has_video:
                better = audio_track_rank(media) > audio_track_rank(current)
            else:
                better = video_rank(media) > video_rank(current)
            if better:
                best[key] = media
        # Video before audio; within each, tallest first, with anything whose
        # height the site never published coming last.
        return [best[key] for key in sorted(best, reverse=True)]

    def _describe_choice(self, media: "MediaFormat",
                         formats: list["MediaFormat"]) -> dict[str, Any]:
        """Present a format as the file the user would actually receive.

        A video-only stream is not what anyone is choosing — they are choosing
        a resolution, and the application supplies the audio and combines the
        two. The size shown is therefore the size of the finished file.
        """
        size = media.filesize
        complete = media.is_progressive or not media.has_video

        if media.has_video and not media.has_audio:
            companion = best_muxable_audio(formats, media)
            if companion is not None and not companion.restricted:
                complete = True
                size = (size + companion.filesize) if size and companion.filesize else size

        return {
            **media.to_dict(),
            "description": self._quality_label(media),
            "filesize": size,
            "segments": len(media.segments),
            "complete": complete and not media.restricted,
            "kind": ("audio" if media.has_audio and not media.has_video else "video"),
        }

    def _cmd_extract(self, params: dict[str, Any]) -> dict[str, Any]:
        info = self.extract(
            params["url"],
            cookies=params.get("cookies", ""),
            user_agent=params.get("userAgent", "") or params.get("user_agent", ""),
            referer=_referrer_of(params),
            site_headers=_site_headers_of(params),
            po_token=params.get("poToken", "") or params.get("po_token", ""),
            visitor_data=(params.get("visitorData", "")
                          or params.get("visitor_data", "")),
        )
        return {
            "title": info.title,
            "duration": info.duration,
            "thumbnail": info.thumbnail,
            "extractor": info.extractor,
            "webpage_url": info.webpage_url,
            "formats": [self._describe_choice(f, info.formats)
                        for f in self.presentable_formats(
                            info.formats,
                            self.settings.get("preferred_video_container", "mp4"))],
        }

    def _cmd_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [d.to_public_dict() for d in self.list_for_display()]

    def _cmd_get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        download = self.get_download(int(params["id"]))
        return download.to_public_dict() if download else None

    def _cmd_pause(self, params: dict[str, Any]) -> bool:
        self.pause(int(params["id"]))
        return True

    def _cmd_resume(self, params: dict[str, Any]) -> bool:
        return self.resume(int(params["id"]))

    def _cmd_cancel(self, params: dict[str, Any]) -> bool:
        self.cancel(int(params["id"]))
        return True

    def _cmd_remove(self, params: dict[str, Any]) -> bool:
        self.remove(int(params["id"]), bool(params.get("delete_files")))
        return True

    def _cmd_swap_link(self, params: dict[str, Any]) -> bool:
        return self.swap_link(int(params["id"]), params["url"])

    def _cmd_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.stats()

    def _cmd_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("set"):
            self.settings.update(dict(params["set"]))
        return self.settings.as_dict()

    def _cmd_queues(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"id": q.id, "name": q.name, "mode": q.mode.value,
             "max_concurrent": q.max_concurrent, "enabled": q.enabled}
            for q in self.list_queues()
        ]

    def _cmd_can_handle(self, params: dict[str, Any]) -> dict[str, Any]:
        """Tell the extension whether a URL is worth intercepting as media."""
        from .extractors import find_extractor

        url = params.get("url", "")
        extractor = find_extractor(url)
        return {
            "handled": extractor is not None,
            "extractor": extractor.name if extractor else "",
            "media": bool(extractor and extractor.name != "generic"),
        }


def _process_id() -> int:
    import os
    return os.getpid()
