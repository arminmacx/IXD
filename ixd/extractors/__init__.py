"""Media extraction plugins.

Importing this package registers every built-in extractor.  Adding a site means
adding a module here with an ``@register``-decorated :class:`Extractor`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.errors import ExtractionError
from ..core.models import MediaFormat, MediaInfo, MediaSegment, TransferMode
from .base import (
    Extractor,
    audio_track_rank,
    best_audio,
    best_muxable_audio,
    extract,
    find_extractor,
    quality_shortfall,
    quality_to_height,
    register,
    registered,
    select_format,
)
from . import dash, hls
from . import generic as _generic       # noqa: F401 - registration side effect
from . import youtube as _youtube       # noqa: F401 - registration side effect

if TYPE_CHECKING:  # pragma: no cover
    from ..core.http_client import HttpClient

__all__ = [
    "Extractor", "MediaFormat", "MediaInfo", "MediaSegment",
    "extract", "find_extractor", "register", "registered",
    "select_format", "quality_shortfall", "quality_to_height",
    "best_audio", "best_muxable_audio",
    "audio_track_rank",
    "prepare_format", "output_extension", "dash", "hls",
]


def prepare_format(fmt: MediaFormat, client: "HttpClient",
                   headers: dict[str, str] | None = None
                   ) -> tuple[TransferMode, str, list[MediaSegment]]:
    """Turn a chosen format into what the engine needs to start transferring.

    Returns ``(mode, url, segments)``:

    * ``RANGED`` with a URL — an ordinary file the chunker can range over.
    * ``SEGMENTED`` with a segment list — HLS/DASH, downloaded in parallel and
      concatenated afterwards.
    """
    if fmt.protocol == "sabr":
        # Nothing to resolve: the origin is asked for the media at transfer
        # time, using the session details carried on the format.
        return TransferMode.SABR, fmt.url, []

    if fmt.protocol == "m3u8":
        segments = hls.fetch_segments(client, fmt.url, headers)
        return TransferMode.SEGMENTED, fmt.url, segments

    if fmt.protocol == "dash":
        if fmt.segments:
            return TransferMode.SEGMENTED, fmt.manifest_url or fmt.url, list(fmt.segments)
        formats = dash.fetch_formats(client, fmt.manifest_url or fmt.url, headers)
        match = next((f for f in formats if f.format_id == fmt.format_id), None)
        if match is None or not match.segments:
            raise ExtractionError(
                f"DASH representation {fmt.format_id} has no resolvable segments"
            )
        return TransferMode.SEGMENTED, fmt.manifest_url or fmt.url, list(match.segments)

    if not fmt.url:
        raise ExtractionError(f"format {fmt.format_id} has no URL")
    return TransferMode.RANGED, fmt.url, []


#: Extensions that name a *playlist*, never the media it describes.
_MANIFEST_EXTENSIONS = ("m3u8", "mpd", "ism", "f4m")


def output_extension(fmt: MediaFormat,
                     segments: "list[MediaSegment] | None" = None) -> str:
    """The extension the finished file should carry.

    A manifest extension is never one of them: what lands on disk is the media
    the playlist described, not the playlist. A download taken from a captured
    ``.m3u8`` was being written as ``….m3u8``, which no player opens by name
    however sound its contents.

    The segments say what the container really is — MPEG-TS pieces concatenate
    into a transport stream, fragmented-MP4 pieces into an MP4 — so they are
    consulted when they are known rather than guessed at.
    """
    # The segments outrank the declared extension, and that ordering is the
    # whole of a real defect: a master playlist declares only codecs, so an HLS
    # variant is built with `ext="mp4"` before the playlist behind it has been
    # read at all. Trusting that guess wrote an **MPEG-TS stream into a file
    # called `.mp4`** — bytes that are perfectly correct and that most players
    # refuse by name, which is exactly the shape of "it downloads and will not
    # play". Once the segments are in hand there is nothing left to guess with.
    for segment in segments or []:
        path = segment.url.split("?", 1)[0].lower()
        if path.endswith(".ts"):
            return "ts"
        if path.endswith((".m4s", ".mp4", ".cmfv", ".cmfa", ".fmp4")):
            return "mp4"
        if path.endswith((".aac", ".m4a")):
            return "m4a"
        if path.endswith((".webm", ".cmfw")):
            return "webm"
    if segments:
        # Segments whose addresses say nothing — a CDN that names them by hash,
        # or disguises them as images. A playlist that declares no
        # initialisation segment is MPEG-TS by construction: fragmented MP4
        # *requires* one (`EXT-X-MAP`), so its absence is the answer rather than
        # a guess. Without this the container fell back to the variant's
        # declared `mp4`, which then named a transport stream `.mp4` and
        # rewrapped it even when the original had been asked for.
        if not any(segment.init for segment in segments):
            return "ts"
    extension = (fmt.ext or "").lower().lstrip(".")
    if extension and extension not in _MANIFEST_EXTENSIONS:
        return extension
    return "mp4"


def suggested_filename(info: MediaInfo, fmt: MediaFormat,
                       segments: "list[MediaSegment] | None" = None) -> str:
    """Build a readable output filename from the media title and format."""
    from ..core.http_client import sanitize_filename

    title = info.title or "download"
    extension = output_extension(fmt, segments)
    # A title taken from a URL's last path segment already carries an
    # extension, and appending another produced names like
    # "stream.m3u8.m3u8". The playlist's own suffix is dropped, and a title
    # that already ends in the right one is left alone.
    stem = title
    for candidate in _MANIFEST_EXTENSIONS + (extension,):
        if stem.lower().endswith(f".{candidate}"):
            stem = stem[: -(len(candidate) + 1)]
            break
    label = fmt.quality_label or (f"{fmt.height}p" if fmt.height else "")
    if label:
        stem = f"{stem} [{label}]"
    return sanitize_filename(f"{stem}.{extension}")
