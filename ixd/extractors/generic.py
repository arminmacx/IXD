"""Generic page scraper — the fallback that claims every remaining URL.

Scans a page for embedded media in the places sites actually put it: HTML5
``<video>``/``<source>`` tags, Open Graph and Twitter player metadata, JSON-LD
``contentUrl``, and bare manifest/media URLs inside inline scripts.  A URL that
already points at a media file or manifest is passed straight through.
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from typing import Any

from ..core.errors import ExtractionError
from ..core.http_client import filename_from_url
from ..core.models import MediaFormat, MediaInfo
from .base import Extractor, register
from . import dash, hls

_MEDIA_EXTENSIONS = (
    ".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4v", ".ts",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav",
)
_MANIFEST_EXTENSIONS = (".m3u8", ".mpd")

#: Ceiling on how much of a page is pulled in to scan for embedded media.
#: Real pages are a few hundred kilobytes; anything past this is not a page.
PAGE_READ_LIMIT = 4 << 20

#: Content types worth running the scraper over.
_DOCUMENT_MIMES = (
    "text/html", "application/xhtml", "text/plain", "application/xml", "text/xml",
)


def _is_document(mime: str) -> bool:
    """True when a body is worth scanning for embedded media links."""
    value = (mime or "").split(";", 1)[0].strip().lower()
    if not value:
        # An origin that declines to say is assumed to be a page; the read is
        # capped either way, so guessing wrong is cheap.
        return True
    return value.startswith(_DOCUMENT_MIMES)


def _is_media_mime(mime: str) -> bool:
    value = (mime or "").split(";", 1)[0].strip().lower()
    return value.startswith(("video/", "audio/", "application/vnd.apple.mpegurl",
                             "application/x-mpegurl", "application/dash+xml"))


def looks_like_media_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith(_MEDIA_EXTENSIONS) or path.endswith(_MANIFEST_EXTENSIONS)


def _protocol_for(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "m3u8"
    if path.endswith(".mpd"):
        return "dash"
    return "https"


@register
class GenericExtractor(Extractor):
    """Lowest-priority extractor: matches anything over http(s)."""

    name = "generic"
    priority = -100
    url_patterns = (r"^https?://",)

    def extract(self, url: str) -> MediaInfo:
        # A direct media or manifest link needs no scraping at all — but a
        # *manifest* is not a file, it is a list of qualities, and the panel
        # asks about the manifest the player was seen fetching. Returning one
        # row for it is what made a five-rendition stream look like a single
        # "Video" everywhere outside YouTube and Vimeo.
        if looks_like_media_url(url):
            protocol = _protocol_for(url)
            renditions = self._expand_manifest(url, protocol, "")
            if renditions:
                return MediaInfo(
                    title=filename_from_url(url),
                    formats=renditions,
                    webpage_url=url,
                    extractor=self.name,
                )
            return self._direct(url)

        # Ask what is at the far end before reading it. Without this step a
        # scrape of a plain file link downloads the file itself just to run
        # regular expressions over it — the user watches their disk fill up
        # while the interface claims to be "analysing".
        info = self._probe(url)
        if info is not None and not _is_document(info.mime):
            return self._direct(url, info)

        if info is not None and info.size > PAGE_READ_LIMIT:
            raise ExtractionError(
                f"{url} is a {info.size:,}-byte {info.mime or 'document'}, which is "
                "too large to scan for embedded media"
            )

        try:
            page = self.client.get_text(url, limit=PAGE_READ_LIMIT)
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"could not fetch {url}: {exc}") from exc

        candidates = self._collect(page, url)
        source_url = url
        if not candidates:
            # The page delegates to a player somewhere else. Following that is
            # the difference between working on a site and not: a wrapper page
            # holds no media by construction, and scraping it harder will never
            # find any.
            candidates, source_url = self._follow_embeds(page, url)
        if not candidates:
            raise ExtractionError(f"no embedded media found on {url}")

        formats: list[MediaFormat] = []
        for index, (candidate, note) in enumerate(candidates.items()):
            protocol = _protocol_for(candidate)
            # A manifest is a *list* of qualities, and offering it as one row
            # hands the user "the video" where the site published five of
            # them. Reported as a panel that shows one entry on sites this
            # application is supposed to be good at: the parsers were here and
            # tested, but nothing on the extraction path ever called them, so
            # only YouTube and Vimeo — which build their own format lists —
            # ever produced a choice.
            expanded = self._expand_manifest(candidate, protocol, note)
            if expanded:
                formats.extend(expanded)
                continue
            extension = "mp4"
            if protocol == "m3u8":
                extension = "mp4"
            elif protocol == "dash":
                extension = "mp4"
            else:
                suffix = urllib.parse.urlparse(candidate).path.rsplit(".", 1)
                if len(suffix) == 2 and len(suffix[1]) <= 5:
                    extension = suffix[1].lower()
            formats.append(MediaFormat(
                format_id=f"generic-{index}",
                url=candidate,
                ext=extension,
                protocol=protocol,
                vcodec="h264",
                acodec="aac",
                note=note,
                manifest_url=url if protocol in ("m3u8", "dash") else "",
            ))

        title = self._page_title(page) or filename_from_url(url)
        del source_url        # kept for clarity above; the page URL is the id
        return MediaInfo(
            title=title,
            formats=formats,
            webpage_url=url,
            thumbnail=self._first(
                r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page
            ),
            extractor=self.name,
            http_headers={"Referer": url},
        )

    #: How many delegated players to follow, and how deep. A wrapper pointing
    #: at a wrapper is ordinary; a chain longer than this is a redirect loop or
    #: an advert farm, and following it costs the user their patience.
    _EMBED_LIMIT = 4
    _EMBED_DEPTH = 2

    def _follow_embeds(self, page: str, base_url: str,
                       depth: int = 0) -> tuple[dict[str, str], str]:
        """Scrape the players this page delegates to, and their players."""
        if depth >= self._EMBED_DEPTH:
            return {}, base_url
        for candidate in self._embeds(page, base_url)[:self._EMBED_LIMIT]:
            try:
                probe = self._probe(candidate)
                if probe is not None and not _is_document(probe.mime):
                    # The "player" is the media itself, which some sites do.
                    if _is_media_mime(probe.mime) or looks_like_media_url(candidate):
                        return {candidate: "embed"}, candidate
                    continue
                inner = self.client.get_text(candidate, limit=PAGE_READ_LIMIT)
            except Exception:  # noqa: BLE001 - one dead embed among several
                continue
            found = self._collect(inner, candidate)
            if found:
                return found, candidate
            deeper, origin = self._follow_embeds(inner, candidate, depth + 1)
            if deeper:
                return deeper, origin
        return {}, base_url

    # ------------------------------------------------------------------
    def _probe(self, url: str) -> Any:
        """Cheap HEAD/ranged-GET lookup; ``None`` when the origin refuses it."""
        try:
            return self.client.probe(url)
        except Exception:  # noqa: BLE001 - a failed probe just means "scrape it"
            return None

    def _direct(self, url: str, info: Any = None) -> MediaInfo:
        protocol = _protocol_for(url)
        name = ""
        if info is not None:
            name = info.filename or ""
        name = name or filename_from_url(url, getattr(info, "mime", ""))
        is_media = protocol != "https" or _is_media_mime(getattr(info, "mime", ""))
        return MediaInfo(
            title=name,
            formats=[MediaFormat(
                format_id="direct",
                url=url,
                ext=name.rsplit(".", 1)[-1] if "." in name else "mp4",
                protocol=protocol,
                vcodec="h264" if is_media else "none",
                acodec="aac" if is_media else "none",
                filesize=int(getattr(info, "size", 0) or 0),
                note="direct link",
            )],
            webpage_url=url,
            extractor=self.name,
        )

    def _collect(self, page: str, base_url: str) -> dict[str, str]:
        """Gather candidate media URLs, preserving discovery order."""
        found: dict[str, str] = {}

        def add(raw: str, note: str) -> None:
            if not raw:
                return
            candidate = html.unescape(raw.strip()).replace("\\/", "/")
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            candidate = urllib.parse.urljoin(base_url, candidate)
            if not candidate.lower().startswith(("http://", "https://")):
                return
            if not looks_like_media_url(candidate):
                return
            found.setdefault(candidate, note)

        for pattern, note in (
            (r'<meta[^>]+property="og:video:secure_url"[^>]+content="([^"]+)"', "og:video"),
            (r'<meta[^>]+property="og:video:url"[^>]+content="([^"]+)"', "og:video"),
            (r'<meta[^>]+property="og:video"[^>]+content="([^"]+)"', "og:video"),
            (r'<meta[^>]+name="twitter:player:stream"[^>]+content="([^"]+)"', "twitter"),
            (r'<video[^>]+src="([^"]+)"', "<video>"),
            (r'<source[^>]+src="([^"]+)"', "<source>"),
            (r'<audio[^>]+src="([^"]+)"', "<audio>"),
        ):
            for match in re.finditer(pattern, page, re.I):
                add(match.group(1), note)

        # JSON-LD blocks frequently carry a contentUrl.
        for match in re.finditer(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            page, re.I | re.DOTALL,
        ):
            try:
                data: Any = json.loads(match.group(1).strip())
            except ValueError:
                continue
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict):
                    for key in ("contentUrl", "embedUrl"):
                        value = item.get(key)
                        if isinstance(value, str):
                            add(value, "json-ld")

        # Player configurations. A page rarely writes its media into the markup
        # any more; it hands a configuration object to a player library, and
        # the address lives in a `file:` or `src:` key inside it. JW Player,
        # Video.js, Plyr, Clappr and most of the rest share this shape, which
        # is why matching the *shape* covers players nobody has heard of rather
        # than needing a rule per library.
        for pattern, note in (
            (r'["\']?(?:file|src|source|hls|url|videoUrl|streamUrl|playlistUrl)'
             r'["\']?\s*:\s*["\']([^"\']+)["\']', "player config"),
            (r'\bsetup\s*\(\s*{[^}]{0,4000}?["\']?file["\']?\s*:\s*'
             r'["\']([^"\']+)["\']', "jwplayer setup"),
            (r'data-(?:video|src|file|hls)(?:-url)?=["\']([^"\']+)["\']',
             "data attribute"),
        ):
            for match in re.finditer(pattern, page, re.I):
                add(match.group(1), note)

        # Bare URLs inside inline scripts — the last resort, but very effective.
        for match in re.finditer(
            r'["\'](https?:(?:\\?/){2}[^"\'\s]+?\.(?:m3u8|mpd|mp4|webm|m4a|mp3)(?:\?[^"\'\s]*)?)["\']',
            page, re.I,
        ):
            add(match.group(1), "inline script")

        return found

    @staticmethod
    def _embeds(page: str, base_url: str) -> list[str]:
        """Pages this one delegates its player to.

        A great many sites do not host their player at all: the page is a
        wrapper and the video lives on another host, reached through an
        ``<iframe>``. Scraping only the page the user is on therefore finds
        nothing on exactly the sites that most need it, because the media was
        never on that page to begin with.
        """
        found: list[str] = []
        for pattern in (
            r'<iframe[^>]+src=["\']([^"\']+)["\']',
            r'<embed[^>]+src=["\']([^"\']+)["\']',
            r'<meta[^>]+property="og:video:iframe"[^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+name="twitter:player"[^>]+content=["\']([^"\']+)["\']',
        ):
            for match in re.finditer(pattern, page, re.I):
                candidate = html.unescape(match.group(1).strip())
                if candidate.startswith("//"):
                    candidate = "https:" + candidate
                candidate = urllib.parse.urljoin(base_url, candidate)
                if not candidate.lower().startswith(("http://", "https://")):
                    continue
                # An advert, a comment widget or a social button is an iframe
                # too. Nothing here is certain, so the cost of following one is
                # what has to be bounded rather than the guess made perfect.
                if re.search(r"(doubleclick|googlesyndication|google-analytics"
                             r"|facebook\.com/plugins|disqus|recaptcha"
                             r"|adservice|/ads?[/.])", candidate, re.I):
                    continue
                if candidate not in found:
                    found.append(candidate)
        return found

    #: How much of a manifest is worth reading. A master playlist is a few
    #: kilobytes; anything vastly larger is a *media* playlist listing every
    #: segment of a long film, and that is not what is being looked for here.
    MANIFEST_READ_LIMIT = 512 * 1024

    def _expand_manifest(self, url: str, protocol: str,
                         note: str) -> list[MediaFormat]:
        """The qualities a manifest publishes, or nothing if it publishes none.

        Fetched rather than assumed: an address ending in `.m3u8` may be a
        master playlist listing five renditions or a media playlist listing
        one film's segments, and only its first line says which. A media
        playlist, an unreadable one, or an origin that refuses all fall back
        to offering the manifest itself — one row that downloads is better
        than a menu that is wrong.
        """
        if protocol not in ("m3u8", "dash"):
            return []
        try:
            text = self.client.get_text(url, limit=self.MANIFEST_READ_LIMIT)
        except Exception:  # noqa: BLE001 - the single-row fallback is the point
            return []
        if not text:
            return []

        try:
            if protocol == "m3u8":
                if not hls.is_master_playlist(text):
                    return []
                found = hls.parse_master(text, url)
            else:
                found = dash.parse_mpd(text, url)
        except Exception:  # noqa: BLE001 - a manifest we cannot read is not fatal
            return []

        # One rendition is not a choice, and the row it would replace already
        # carries the note the page gave it.
        if len(found) < 2:
            return []
        if protocol == "m3u8":
            # A master playlist gives each rendition a bandwidth and never a
            # duration, so nothing in it can size a download — every HLS site
            # showed a quality menu with no sizes and a file-info window
            # reading "size not published". One extra request settles it for
            # all of them (context.md §3.85).
            try:
                hls.estimate_sizes(self.client, found)
            except Exception:  # noqa: BLE001 - a size is never worth a failure
                pass
        for fmt in found:
            fmt.manifest_url = fmt.manifest_url or url
            if note and note not in (fmt.note or ""):
                fmt.note = f"{fmt.note} · {note}".strip(" ·")
        return found

    @staticmethod
    def _page_title(page: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.DOTALL)
        if not match:
            return ""
        return html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:150]


@register
class VimeoExtractor(Extractor):
    """Vimeo player configuration endpoint."""

    name = "vimeo"
    priority = 90
    url_patterns = (
        r"^https?://(?:www\.)?vimeo\.com/(?:channels/[\w]+/)?\d+",
        r"^https?://player\.vimeo\.com/video/\d+",
    )

    def extract(self, url: str) -> MediaInfo:
        match = re.search(r"/(\d+)", urllib.parse.urlparse(url).path)
        if not match:
            raise ExtractionError(f"cannot find a Vimeo video id in {url!r}")
        video_id = match.group(1)

        config_url = f"https://player.vimeo.com/video/{video_id}/config"
        try:
            raw = self.client.get_text(config_url, {"Referer": url})
            config = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(f"could not load the Vimeo config: {exc}") from exc

        video = config.get("video") or {}
        request = config.get("request") or {}
        files = request.get("files") or {}
        formats: list[MediaFormat] = []

        for entry in files.get("progressive") or []:
            formats.append(MediaFormat(
                format_id=f"progressive-{entry.get('quality', '')}",
                url=entry.get("url", ""),
                ext="mp4",
                protocol="https",
                width=int(entry.get("width", 0) or 0),
                height=int(entry.get("height", 0) or 0),
                fps=float(entry.get("fps", 0) or 0),
                vcodec="h264",
                acodec="aac",
                quality_label=str(entry.get("quality", "")),
                note="progressive",
            ))

        for protocol_key, protocol in (("hls", "m3u8"), ("dash", "dash")):
            block = files.get(protocol_key) or {}
            cdns = block.get("cdns") or {}
            default = block.get("default_cdn")
            for cdn_name, cdn in cdns.items():
                manifest = cdn.get("url", "")
                if not manifest:
                    continue
                formats.append(MediaFormat(
                    format_id=f"{protocol_key}-{cdn_name}",
                    url=manifest,
                    ext="mp4",
                    protocol=protocol,
                    vcodec="h264",
                    acodec="aac",
                    note=f"{protocol_key} ({cdn_name})"
                         + (" default" if cdn_name == default else ""),
                    manifest_url=manifest,
                ))

        if not formats:
            raise ExtractionError("Vimeo returned no playable streams")

        formats.sort(key=lambda f: (f.height, f.tbr), reverse=True)
        return MediaInfo(
            title=video.get("title", "") or f"vimeo-{video_id}",
            formats=formats,
            webpage_url=url,
            thumbnail=(video.get("thumbs") or {}).get("base", ""),
            duration=float(video.get("duration", 0) or 0),
            extractor=self.name,
            http_headers={"Referer": "https://player.vimeo.com/"},
        )
