"""Twitch video-on-demand: past broadcasts, highlights and uploads.

A Twitch VOD page publishes nothing fetchable in its HTML — the player asks
for a playback token and builds the manifest URL itself — so the generic page
scraper found "no embedded media" on every `twitch.tv/videos/…` address
(context.md §3.82). What the extension *did* capture was one of the storage
playlists the player then fetched, and those are **media** playlists: a single
rendition, with no `RESOLUTION` and no `BANDWIDTH` anywhere in them. Handed one
of those, the application had one format to offer, could not say what
resolution it was, and quietly downloaded 360p when 1080p was asked for.

Both entry points are handled here:

* **A page URL** — the token is fetched from Twitch's own GraphQL endpoint with
  the web player's public client id, and exchanged at `usher` for the **master**
  playlist. That is the good route: it names every rendition with its exact
  resolution, frame rate and bandwidth, and `hls.parse_master` already reads it.
* **A storage playlist** the browser captured — the renditions of a VOD are
  sibling folders under one storage path, so they are reachable by swapping one
  path segment, and they need no token at all (measured: `chunked`, `720p60`,
  `480p30`, `360p30` and `160p30` all answer `200` unsigned).

Live streams are deliberately *not* claimed here. A live playlist is a sliding
window of the last few segments, so downloading one yields the last half-minute
rather than the broadcast; following one as it grows is a different feature and
is not built (see the open items in `instruction.md`).
"""

from __future__ import annotations

import json
import re
import urllib.parse

from ..core.errors import ExtractionError
from ..core.models import MediaFormat, MediaInfo
from .base import Extractor, register
from .hls import parse_master

#: The Twitch web player's own client id. It is public — it ships in the
#: page's JavaScript and identifies the player, not a user — and every
#: request made here is one an ordinary signed-out viewer makes.
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

GQL_ENDPOINT = "https://gql.twitch.tv/gql"

#: What each storage folder holds, for the route where no master playlist is
#: available. The folder names are self-describing, which is the only reason
#: this fallback can label anything at all.
_LADDER: dict[str, tuple[int, int, float]] = {
    "chunked": (0, 0, 0.0),          # the source rendition, dimensions unstated
    "1080p60": (1920, 1080, 60.0),
    "1080p30": (1920, 1080, 30.0),
    "720p60": (1280, 720, 60.0),
    "720p30": (1280, 720, 30.0),
    "480p30": (852, 480, 30.0),
    "360p30": (640, 360, 30.0),
    "160p30": (284, 160, 30.0),
}

#: A VOD storage playlist: `…/<storage-path>/<rendition>/<name>.m3u8`.
_STORAGE_PLAYLIST = re.compile(
    r"^(?P<base>https?://[^/]+/(?:.+/)?[^/]+)/(?P<quality>[\w]+)/"
    r"(?P<name>index-dvr|index-muted-\w+|highlight-\d+)\.m3u8",
    re.I,
)


@register
class TwitchExtractor(Extractor):
    """Past broadcasts, highlights and uploads — not live channels."""

    name = "twitch"
    priority = 95
    url_patterns = (
        r"^https?://(?:www\.|m\.)?twitch\.tv/videos/\d+",
        r"^https?://(?:www\.|m\.)?twitch\.tv/[\w.-]+/v(?:ideo)?/\d+",
        # Clips: their own host, and the two forms on the main one.
        r"^https?://clips\.twitch\.tv/[\w-]+",
        r"^https?://(?:www\.|m\.)?twitch\.tv/(?:[\w.-]+/)?clip/[\w-]+",
        # A storage playlist the browser captured, on Twitch's VOD CDNs.
        r"^https?://[\w.-]*(?:cloudfront\.net|jtvnw\.net)/.+/"
        r"(?:index-dvr|index-muted-\w+|highlight-\d+)\.m3u8",
    )

    # ------------------------------------------------------------------
    def extract(self, url: str) -> MediaInfo:
        slug = self.clip_slug(url)
        if slug:
            return self._from_clip(url, slug)

        video_id = self.video_id(url)
        if video_id:
            return self._from_page(url, video_id)

        match = _STORAGE_PLAYLIST.match(url)
        if match:
            # A highlight names its VOD in the filename, so the good route is
            # still open even when what we were handed is a media playlist.
            highlight = re.match(r"highlight-(\d+)", match.group("name"))
            if highlight:
                try:
                    return self._from_page(url, highlight.group(1))
                except ExtractionError:
                    pass    # fall through to sibling probing
            return self._from_storage(match)

        raise ExtractionError(f"not a Twitch video address: {url}")

    @staticmethod
    def clip_slug(url: str) -> str:
        """The clip's slug, or empty for anything that is not a clip.

        `/videos/<id>` must never look like one, so the channel form requires
        the literal `clip` segment rather than treating any trailing word as a
        slug.
        """
        match = re.search(
            r"(?:clips\.twitch\.tv/|twitch\.tv/(?:[\w.-]+/)?clip/)([\w-]+)",
            url, re.I,
        )
        if not match:
            return ""
        slug = match.group(1)
        return "" if slug.lower() in ("edit", "create") else slug

    @staticmethod
    def video_id(url: str) -> str:
        """The VOD id in a page URL, or empty for anything else."""
        match = re.search(
            r"twitch\.tv/(?:videos/|[\w.-]+/v(?:ideo)?/)(\d+)", url, re.I
        )
        return match.group(1) if match else ""

    # -- the master-playlist route --------------------------------------
    def _from_page(self, url: str, video_id: str) -> MediaInfo:
        title, duration, thumbnail, _channel, value, signature = (
            self._video_session(video_id))

        query = urllib.parse.urlencode({
            "allow_source": "true",
            "allow_audio_only": "true",
            "player": "twitchweb",
            "playlist_include_framerate": "true",
            "nauth": value,
            "nauthsig": signature,
            "supported_codecs": "h264",
            "transcode_mode": "cbr_v1",
        })
        master_url = f"https://usher.ttvnw.net/vod/v2/{video_id}.m3u8?{query}"
        try:
            text = self.client.get_text(master_url)
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError(
                f"Twitch would not serve the manifest for video {video_id}: {exc}"
            ) from exc

        formats = parse_master(text, master_url)
        if not formats:
            raise ExtractionError(
                f"Twitch's manifest for video {video_id} lists no renditions"
            )
        for media in formats:
            self._label(media, duration)

        return MediaInfo(
            title=title or f"twitch-{video_id}",
            formats=sorted(formats, key=lambda f: (f.height, f.tbr), reverse=True),
            webpage_url=(url if "twitch.tv" in url
                         else f"https://www.twitch.tv/videos/{video_id}"),
            thumbnail=thumbnail,
            duration=duration,
            extractor=self.name,
            http_headers={"Referer": "https://www.twitch.tv/"},
        )

    # -- clips ------------------------------------------------------------
    def _from_clip(self, url: str, slug: str) -> MediaInfo:
        """A clip is a handful of plain MP4s, not a playlist.

        Which makes it the easy case and it was the one that failed hardest:
        with nothing claiming the address, the panel fell back to the captures
        and listed the same `index.mp4` three times over (§3.85). One query
        returns every quality, its direct address, and the token that signs
        them — no `usher`, no manifest, nothing to parse.
        """
        body = {"query": '{clip(slug:"%s"){title durationSeconds '
                         "broadcaster{displayName} thumbnailURL "
                         "videoQualities{quality frameRate sourceURL} "
                         'playbackAccessToken(params:{platform:"web",'
                         'playerBackend:"mediaplayer",playerType:"site"})'
                         "{value signature}}}" % slug}
        clip = ((self._gql(body).get("data") or {}).get("clip") or {})
        if not clip:
            raise ExtractionError(
                f"Twitch does not know a clip called {slug!r} — it may have "
                "been deleted, or the address may name something else"
            )
        token = clip.get("playbackAccessToken") or {}
        signature, value = token.get("signature", ""), token.get("value", "")

        formats: list[MediaFormat] = []
        for entry in clip.get("videoQualities") or []:
            source = entry.get("sourceURL") or ""
            if not source:
                continue
            # The token signs the address; without it the CDN answers 403.
            signed = source
            if signature and value:
                signed = (f"{source}?sig={signature}"
                          f"&token={urllib.parse.quote(value)}")
            height = 0
            try:
                height = int(str(entry.get("quality") or "0"))
            except ValueError:
                height = 0
            formats.append(MediaFormat(
                format_id=f"twitch-clip-{entry.get('quality') or len(formats)}",
                url=signed,
                ext="mp4",
                # A plain file, so the engine ranges over it and learns its
                # exact length from the response — no estimate anywhere here.
                protocol="https",
                height=height,
                fps=float(entry.get("frameRate") or 0),
                vcodec="h264",
                acodec="aac",
                quality_label=f"{height}p" if height else "Source",
                manifest_url="",
            ))
        if not formats:
            raise ExtractionError(f"Twitch clip {slug!r} publishes no video")

        # A clip is a plain file, so its exact length is a header away — no
        # bandwidth × duration estimate needed, and no "size not published" in
        # the file-info window. All three qualities live on one host and
        # connections are reused (§3.84), so this is one handshake and three
        # round trips. Best-effort: a refused HEAD costs a number, not a
        # download.
        for media in formats:
            try:
                with self.client.request("HEAD", media.url) as answer:
                    media.filesize = answer.content_length
            except Exception:  # noqa: BLE001
                continue

        channel = ((clip.get("broadcaster") or {}).get("displayName") or "").strip()
        title = (clip.get("title") or "").strip() or slug
        return MediaInfo(
            title=f"{title} - {channel}" if channel else title,
            formats=sorted(formats, key=lambda f: f.height, reverse=True),
            webpage_url=url,
            thumbnail=clip.get("thumbnailURL") or "",
            duration=float(clip.get("durationSeconds") or 0),
            extractor=self.name,
            http_headers={"Referer": "https://www.twitch.tv/"},
        )

    # -- the captured-playlist route ------------------------------------
    def _from_storage(self, match: re.Match) -> MediaInfo:
        """Offer every rendition of a VOD whose storage path we already have.

        No token is involved: the renditions sit beside each other under the
        storage path, so each is one path segment away from the one the browser
        happened to fetch. Whatever the captured address was is always offered,
        even when probing turns up nothing else.
        """
        base, captured, name = (match.group("base"), match.group("quality"),
                                match.group("name"))
        duration = 0.0
        formats: list[MediaFormat] = []

        for quality in _LADDER:
            candidate = f"{base}/{quality}/{name}.m3u8"
            if quality == captured:
                text = self._playlist_text(candidate)
            else:
                text = self._playlist_text(candidate, quiet=True)
            if not text:
                continue
            duration = duration or self._total_seconds(text)
            width, height, fps = _LADDER[quality]
            formats.append(MediaFormat(
                format_id=f"twitch-{quality}",
                url=candidate,
                ext="mp4",
                protocol="m3u8",
                width=width,
                height=height,
                fps=fps,
                vcodec="h264",
                acodec="aac",
                quality_label=(f"{height}p" if height else "Source"),
                note="source" if quality == "chunked" else "",
                manifest_url=candidate,
            ))

        if not formats:
            raise ExtractionError(
                f"none of this Twitch VOD's renditions could be read from {base}"
            )
        return MediaInfo(
            title=self._storage_title(base, name),
            formats=sorted(formats, key=lambda f: (f.height, f.tbr), reverse=True),
            webpage_url="",
            duration=duration,
            extractor=self.name,
            http_headers={"Referer": "https://www.twitch.tv/"},
        )

    @staticmethod
    def _storage_title(base: str, name: str) -> str:
        """A name for a VOD known only by its storage path.

        The path's last component is `<hash>_<channel>_<broadcast>_<id>`, so the
        channel is in there and is a far better name than the playlist's
        filename — which is `index-dvr` for every past broadcast ever made.
        """
        folder = base.rstrip("/").rsplit("/", 1)[-1]
        parts = folder.split("_")
        channel = parts[1] if len(parts) >= 3 and parts[1] else ""
        highlight = re.match(r"highlight-(\d+)", name)
        if channel and highlight:
            return f"{channel} - highlight {highlight.group(1)}"
        if channel:
            return f"{channel} - Twitch"
        return f"twitch-{name}"

    def _playlist_text(self, url: str, quiet: bool = False) -> str:
        try:
            text = self.client.get_text(url)
        except Exception:  # noqa: BLE001
            if quiet:
                return ""
            raise
        return text if text.lstrip().startswith("#EXTM3U") else ""

    @staticmethod
    def _total_seconds(text: str) -> float:
        """A VOD playlist states its own length; nothing else has to be added up."""
        match = re.search(r"#EXT-X-TWITCH-TOTAL-SECS:\s*([\d.]+)", text)
        return float(match.group(1)) if match else 0.0

    # -- Twitch's GraphQL endpoint --------------------------------------
    def _gql(self, body: dict) -> dict:
        raw = self.client.post(
            GQL_ENDPOINT,
            json.dumps(body),
            {"Client-ID": CLIENT_ID, "Content-Type": "application/json"},
        )
        try:
            payload = json.loads(raw.text() if hasattr(raw, "text") else raw)
        except (TypeError, ValueError) as exc:
            raise ExtractionError(f"Twitch's API returned no JSON: {exc}") from exc
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        return payload if isinstance(payload, dict) else {}

    def _video_session(self, video_id: str
                       ) -> tuple[str, float, str, str, str, str]:
        """Everything a recording needs, in **one** request.

        `video` and `videoPlaybackAccessToken` are both root fields, so one
        query answers both — and that is not only a round trip saved. Asked
        separately, the metadata call was optional and swallowed its own
        failures: when it hung, the panel showed a quality menu with no sizes
        beside it and no hint why (§3.87). Sharing a request makes the two
        succeed or fail together, which is the honest arrangement — the token
        is not optional, so a failure is now reported instead of half-applied.
        """
        query = (
            '{video(id:"%s"){title lengthSeconds owner{displayName} '
            'previewThumbnailURL} '
            'videoPlaybackAccessToken(id:"%s",params:{platform:"web",'
            'playerBackend:"mediaplayer",playerType:"site"})'
            "{value signature}}" % (video_id, video_id)
        )
        data = (self._gql({"query": query}).get("data") or {})
        token = data.get("videoPlaybackAccessToken") or {}
        value, signature = token.get("value", ""), token.get("signature", "")
        if not (value and signature):
            # The plain query is refused or has changed shape. Fall back to the
            # persisted operation, which is a different code path on Twitch's
            # side and has been known to answer when this one does not.
            value, signature = self._access_token(video_id)

        video = data.get("video") or {}
        thumbnail = (video.get("previewThumbnailURL") or "")
        # The URL is a template; a viewer wants a picture, not `{width}`.
        thumbnail = thumbnail.replace("{width}", "1280").replace("{height}", "720")
        return (
            video.get("title") or "",
            float(video.get("lengthSeconds") or 0),
            thumbnail,
            ((video.get("owner") or {}).get("displayName") or ""),
            value,
            signature,
        )

    def _access_token(self, video_id: str) -> tuple[str, str]:
        """The playback token and its signature, for a signed-out viewer.

        The plain query is asked first on purpose. Twitch's *persisted* queries
        are keyed by a hash of the query text, and those hashes go stale — the
        one for video metadata already answers `PersistedQueryNotFound` — so the
        route with nothing to go stale is the one to depend on. The persisted
        form is kept as a second chance in case the plain one is ever refused.
        """
        plain = {
            "query": (
                '{videoPlaybackAccessToken(id:"%s",params:{platform:"web",'
                'playerBackend:"mediaplayer",playerType:"site"})'
                "{value signature}}" % video_id
            )
        }
        persisted = {
            "operationName": "PlaybackAccessToken",
            "variables": {"isLive": False, "login": "", "isVod": True,
                          "vodID": video_id, "playerType": "site"},
            "extensions": {"persistedQuery": {
                "version": 1,
                "sha256Hash": "0828119ded1c13477966434e15800ff57ddacf13ba1911"
                              "c129dc2200705b0712",
            }},
        }
        for body in (persisted, plain):
            token = ((self._gql(body).get("data") or {})
                     .get("videoPlaybackAccessToken") or {})
            value, signature = token.get("value", ""), token.get("signature", "")
            if value and signature:
                return value, signature
        raise ExtractionError(
            f"Twitch issued no playback token for video {video_id} — it may be "
            "subscriber-only, deleted, or restricted in this region"
        )

    def _metadata(self, video_id: str) -> tuple[str, float, str, str]:
        """Title, length, thumbnail and channel — all optional."""
        body = {"query": '{video(id:"%s"){title lengthSeconds '
                         "owner{displayName} previewThumbnailURL}}" % video_id}
        try:
            video = ((self._gql(body).get("data") or {}).get("video") or {})
        except Exception:  # noqa: BLE001
            # Every field here is a nicety — a name for the file, a length to
            # size the download from. The manifest is what the download needs,
            # and it is fetched from a different host, so a hiccup reaching the
            # API must not decide whether the video can be downloaded at all.
            # (Caught in the wild: one TLS handshake to `gql.twitch.tv` timed
            # out and took the whole extraction down with it.)
            return "", 0.0, "", ""
        thumbnail = (video.get("previewThumbnailURL") or "")
        # The URL is a template; a viewer wants a picture, not `{width}`.
        thumbnail = thumbnail.replace("{width}", "1280").replace("{height}", "720")
        return (
            video.get("title") or "",
            float(video.get("lengthSeconds") or 0),
            thumbnail,
            ((video.get("owner") or {}).get("displayName") or ""),
        )

    # -- shared -----------------------------------------------------------
    @staticmethod
    def _label(media: MediaFormat, duration: float) -> None:
        """Give a rendition its size and a name a menu can show.

        The size is `bandwidth × duration`, which is what the manifest makes
        knowable without fetching the whole thing — an estimate, and named as
        one, but the alternative is the file-info window saying "size not
        published" for a VOD whose length Twitch states outright.
        """
        if media.vcodec == "none":
            # Twitch publishes an `audio_only` rendition alongside the video
            # ones. Left to the generic labelling it reads as "216k", which in
            # a quality menu looks like a very bad picture rather than no
            # picture at all.
            media.quality_label = "Audio only"
            media.ext = "m4a"
            media.note = media.note or "audio only"
        elif media.height >= 1080 and not media.note:
            media.note = "source"
        if duration > 0 and media.tbr > 0 and not media.filesize:
            media.filesize = int(media.tbr * 1000 / 8 * duration)
        if not media.quality_label and media.height:
            media.quality_label = f"{media.height}p"
