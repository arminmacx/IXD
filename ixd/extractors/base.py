"""Extractor plugin contract and registry.

Extractors turn a *web page* URL into concrete, downloadable streams.  Adding
support for a new site means dropping a subclass into this package — the
registry picks it up and the engine needs no changes.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

from ..core.errors import ExtractionError
from ..core.models import MediaFormat, MediaInfo

if TYPE_CHECKING:  # pragma: no cover
    from ..core.http_client import HttpClient


class Extractor:
    """Base class for every site plugin."""

    #: Human-readable plugin name.
    name: str = "generic"
    #: Patterns that decide whether this plugin claims a URL.
    url_patterns: tuple[str, ...] = ()
    #: Higher priority wins when several extractors match.
    priority: int = 0

    def __init__(self, client: "HttpClient", options: dict | None = None) -> None:
        self.client = client
        #: Free-form per-site options (tokens, cookies, quality hints).
        self.options = dict(options or {})

    @classmethod
    def matches(cls, url: str) -> bool:
        return any(re.search(pattern, url, re.I) for pattern in cls.url_patterns)

    def extract(self, url: str) -> MediaInfo:
        """Return every stream available at ``url``."""
        raise NotImplementedError

    # -- helpers shared by subclasses ----------------------------------
    def _get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        return self.client.get_text(url, headers)

    @staticmethod
    def _first(pattern: str, text: str, group: int = 1, flags: int = 0) -> str:
        match = re.search(pattern, text, flags)
        return match.group(group) if match else ""


_REGISTRY: list[type[Extractor]] = []


def register(extractor_class: type[Extractor]) -> type[Extractor]:
    """Class decorator that adds an extractor to the registry."""
    if extractor_class not in _REGISTRY:
        _REGISTRY.append(extractor_class)
        _REGISTRY.sort(key=lambda cls: -cls.priority)
    return extractor_class


def registered() -> list[type[Extractor]]:
    return list(_REGISTRY)


def find_extractor(url: str) -> type[Extractor] | None:
    for extractor_class in _REGISTRY:
        try:
            if extractor_class.matches(url):
                return extractor_class
        except re.error:
            continue
    return None


def extract(url: str, client: "HttpClient", options: dict | None = None) -> MediaInfo:
    """Run the best-matching extractor for ``url``."""
    extractor_class = find_extractor(url)
    if extractor_class is None:
        raise ExtractionError(f"no extractor can handle {url}")
    info = extractor_class(client, options).extract(url)
    if not info.formats:
        raise ExtractionError(f"{extractor_class.name} found no downloadable streams")
    return info


# ----------------------------------------------------------------------
# format selection
# ----------------------------------------------------------------------
_QUALITY_ORDER = ("2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p")


def quality_to_height(label: str) -> int:
    match = re.search(r"(\d{3,4})p", label or "")
    return int(match.group(1)) if match else 0


def select_format(formats: Iterable[MediaFormat], preferred_quality: str = "1080p",
                  prefer_progressive: bool = True,
                  preferred_container: str = "") -> MediaFormat | None:
    """Pick the stream that best matches the user's quality preference.

    Progressive (muxed audio+video) streams are preferred by default because
    they need no muxing step to be playable.
    """
    candidates = [f for f in formats if f.url or f.manifest_url]
    if not candidates:
        return None

    target = quality_to_height(preferred_quality) or 1080

    def score(fmt: MediaFormat) -> tuple:
        # Never exceed the requested height; among those, take the tallest.
        height = fmt.height or quality_to_height(fmt.quality_label)
        within = height <= target
        return (
            # A stream the origin will only serve part of cannot produce a
            # complete file, so it loses to any stream that can — however much
            # better it looks on paper, and even when it is the only one at the
            # requested height. Half a file at the right resolution is worth
            # less than a whole one a size down.
            0 if fmt.restricted else 1,
            # Staying inside the requested quality comes next: a taller stream
            # is not what was asked for.
            1 if within else 0,
            # The requested resolution outranks the convenience of a
            # ready-muxed stream. Ranking progressive first meant every
            # request — 480p, 720p, 1080p alike — came back as the 360p
            # progressive copy, because that is the only progressive stream on
            # offer. Pairing audio is this application's job; silently serving
            # a quarter of the requested resolution is not.
            height if within else -height,
            1 if (prefer_progressive and fmt.is_progressive) else 0,
            # Which container, when the same resolution comes in more than one.
            #
            # This sits above bitrate on purpose. At 60fps a rendition is
            # published as both WebM and MP4, and the two were separated by
            # nothing else — measured 0.3% apart on a real video, so the
            # container was effectively chosen at random, and the choice
            # decided both playability and 46% of the file size. A tie-break,
            # never a filter: a resolution offered in one container only is
            # still the best answer for that resolution.
            1 if (preferred_container and fmt.ext == preferred_container) else 0,
            fmt.tbr,
        )

    return max(candidates, key=score)


def quality_shortfall(chosen: MediaFormat, preferred_quality: str) -> int:
    """How far below the requested height ``chosen`` lands, in pixels.

    Zero when the request was met. Selection deliberately falls back to a
    stream that can be delivered whole rather than one the origin will cut
    short, and the caller is expected to say so instead of letting the
    difference pass unremarked.
    """
    target = quality_to_height(preferred_quality)
    if not target:
        return 0
    height = chosen.height or quality_to_height(chosen.quality_label)
    return max(0, target - height)


def audio_track_rank(media: MediaFormat) -> tuple:
    """How good an answer this audio track is to "the audio of this video".

    Language first, and it is not negotiable: a dubbing never outranks the
    original on bitrate, because the language is not a quality setting and a
    viewer who asked for neither finds out on playback.

    Then the plain mix ahead of the site's processed ones. A language is
    published up to three times over — as itself, loudness-compressed
    (``drc``) and volume-boosted (``vb``) — and their bitrates differ by tens
    of bits per second, which is enough for a bitrate tie-break to pick a mix
    the site never plays by default.

    Bitrate only decides between tracks that are otherwise the same track.
    """
    return (
        media.audio_is_default and media.audio_kind != "dubbed",
        media.audio_kind == "original",
        not media.audio_variant,
        media.tbr,
    )


def best_audio(formats: Iterable[MediaFormat]) -> MediaFormat | None:
    """The best audio-only stream: the published track, then bitrate."""
    audio_only = [f for f in formats if f.has_audio and not f.has_video]
    if not audio_only:
        return None
    return max(audio_only, key=audio_track_rank)


def best_muxable_audio(formats: Iterable[MediaFormat],
                       video: MediaFormat) -> MediaFormat | None:
    """The best audio track that can be combined with ``video`` into one file.

    Bitrate is not the only consideration: the two tracks have to end up in the
    same container. Pairing an MP4 video with a WebM/Opus audio track produces
    two files that cannot be joined, so a slightly lower-bitrate MP4 audio
    track is the better answer — a playable file beats a marginally better one
    that has to stay in two pieces.
    """
    audio_only = [f for f in formats if f.has_audio and not f.has_video]
    if not audio_only:
        return None

    compatible = [f for f in audio_only if f.ext in _MUXABLE_WITH.get(video.ext, ())]
    # Falling back to the whole pool used to mean a WebM video could be paired
    # with an MP4 audio track, which no container holds and no muxer can join —
    # the transfer only discovers it after fetching both, which on a 1080p60
    # video is a couple of hundred megabytes spent to reach an error. When the
    # video's container is one we know, an incompatible track is not a
    # fallback; it is a pairing that cannot work.
    pool = compatible or ([] if video.ext in _MUXABLE_WITH else audio_only)
    if not pool:
        return None
    # Container compatibility decides the pool; within it the track the video
    # was published with comes first, ahead of bitrate.
    return max(pool, key=audio_track_rank)


#: Which audio containers can be muxed into which video container.
#:
#: WebM is here because everything at 60fps, and everything above 1080p, is
#: published as VP9 or AV1 with Opus — which an MP4 cannot hold. Pairing those
#: needs `ixd.core.webm`, and before it existed such a quality could only ever
#: be delivered as two files.
_MUXABLE_WITH = {
    "mp4": ("m4a", "mp4"),
    "m4v": ("m4a", "mp4"),
    "webm": ("webm", "weba"),
}
