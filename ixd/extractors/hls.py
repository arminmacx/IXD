"""HLS (RFC 8216) master and media playlist parsing.

Produces either a list of selectable variants (from a master playlist) or the
concrete :class:`MediaSegment` list the engine's segmented transfer path
consumes, including AES-128 key/IV plumbing and ``EXT-X-BYTERANGE`` support.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import TYPE_CHECKING

from ..core.errors import ExtractionError
from ..core.models import MediaFormat, MediaSegment

if TYPE_CHECKING:  # pragma: no cover
    from ..core.http_client import HttpClient


def parse_attributes(line: str) -> dict[str, str]:
    """Parse an HLS attribute list, honouring quoted values containing commas."""
    attributes: dict[str, str] = {}
    for match in re.finditer(
        r'([A-Za-z0-9\-]+)\s*=\s*("[^"]*"|[^,]*)', line
    ):
        key = match.group(1).strip().upper()
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attributes[key] = value
    return attributes


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


#: How a codec string names each kind of track. ``CODECS`` lists them in no
#: guaranteed order, so taking the first as the video codec describes an
#: audio-only variant as a video one — and an audio-only stream is a perfectly
#: ordinary thing to publish, and to want.
_VIDEO_CODECS = ("avc1", "avc3", "hvc1", "hev1", "hvc", "vp8", "vp9", "vp09",
                 "av01", "mp4v", "dvh1", "dvhe", "theora")
_AUDIO_CODECS = ("mp4a", "ac-3", "ec-3", "opus", "vorbis", "alac", "flac",
                 "mp3", "dts")


def split_codecs(codecs: str) -> tuple[str, str]:
    """``(video codec, audio codec)`` from a ``CODECS`` attribute.

    Either may come back ``"none"``: a variant carrying only audio is what a
    radio stream or a music service publishes, and calling its codec a video
    one makes it look like a soundless film.
    """
    video = audio = ""
    for entry in (part.strip() for part in (codecs or "").split(",")):
        if not entry:
            continue
        lowered = entry.lower()
        if not video and lowered.startswith(_VIDEO_CODECS):
            video = entry
        elif not audio and lowered.startswith(_AUDIO_CODECS):
            audio = entry
    return video or "none", audio or "none"


def parse_master(text: str, base_url: str) -> list[MediaFormat]:
    """Extract every variant (and audio rendition) from a master playlist."""
    formats: list[MediaFormat] = []
    lines = [line.strip() for line in text.splitlines()]

    # Audio-only renditions declared with EXT-X-MEDIA.
    for line in lines:
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attributes = parse_attributes(line[len("#EXT-X-MEDIA:"):])
        if attributes.get("TYPE") != "AUDIO" or not attributes.get("URI"):
            continue
        formats.append(MediaFormat(
            format_id=f"hls-audio-{attributes.get('GROUP-ID', 'default')}-"
                      f"{attributes.get('NAME', 'audio')}",
            url=urllib.parse.urljoin(base_url, attributes["URI"]),
            ext="m4a",
            protocol="m3u8",
            vcodec="none",
            acodec="aac",
            note=attributes.get("NAME", "audio"),
            manifest_url=base_url,
        ))

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = parse_attributes(line[len("#EXT-X-STREAM-INF:"):])
            uri = ""
            for lookahead in range(index + 1, len(lines)):
                if lines[lookahead] and not lines[lookahead].startswith("#"):
                    uri = lines[lookahead]
                    index = lookahead
                    break
            if uri:
                width = height = 0
                resolution = attributes.get("RESOLUTION", "")
                if "x" in resolution:
                    try:
                        width, height = (int(v) for v in resolution.lower().split("x", 1))
                    except ValueError:
                        width = height = 0
                bandwidth = attributes.get("BANDWIDTH") or attributes.get("AVERAGE-BANDWIDTH") or "0"
                try:
                    tbr = int(bandwidth) / 1000.0
                except ValueError:
                    tbr = 0.0
                codecs = attributes.get("CODECS", "")
                vcodec, acodec = split_codecs(codecs)
                if not codecs:
                    # Nothing declared: assume the ordinary pairing rather than
                    # assume there is no picture.
                    vcodec, acodec = "h264", "aac"
                formats.append(MediaFormat(
                    format_id=f"hls-{height or int(tbr)}",
                    url=urllib.parse.urljoin(base_url, uri),
                    ext="mp4" if vcodec != "none" else "m4a",
                    protocol="m3u8",
                    width=width,
                    height=height,
                    fps=float(attributes.get("FRAME-RATE", 0) or 0),
                    tbr=tbr,
                    vcodec=vcodec,
                    acodec=acodec,
                    # `RESOLUTION` is optional, and without a label a menu has
                    # nothing to tell one variant from another. The bandwidth
                    # is what the playlist does declare, and it is what a
                    # viewer is choosing between when the height is unstated.
                    quality_label=(f"{height}p" if height
                                   else (f"{int(tbr)}k" if tbr else "")),
                    manifest_url=base_url,
                ))
        index += 1
    return formats


def parse_media_playlist(text: str, base_url: str) -> list[MediaSegment]:
    """Turn a media playlist into the engine's segment list."""
    segments: list[MediaSegment] = []
    current_key_url: str | None = None
    current_key_iv: str | None = None
    duration = 0.0
    byte_range: tuple[int, int] | None = None
    next_offset = 0
    media_sequence = 0
    index = 0
    #: Counts *media* segments only. `index` counts everything appended to the
    #: list, and an `EXT-X-MAP` takes one of those numbers — see the IV below.
    media_index = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_sequence = int(line.split(":", 1)[1])
            except ValueError:
                media_sequence = 0

        elif line.startswith("#EXT-X-KEY:"):
            attributes = parse_attributes(line[len("#EXT-X-KEY:"):])
            method = attributes.get("METHOD", "NONE").upper()
            if method == "NONE":
                current_key_url = None
                current_key_iv = None
            elif method == "AES-128":
                uri = attributes.get("URI", "")
                current_key_url = urllib.parse.urljoin(base_url, uri) if uri else None
                current_key_iv = attributes.get("IV")
            else:
                raise ExtractionError(
                    f"unsupported HLS encryption method: {method} "
                    "(only AES-128 can be decrypted natively)"
                )

        elif line.startswith("#EXT-X-MAP:"):
            attributes = parse_attributes(line[len("#EXT-X-MAP:"):])
            uri = attributes.get("URI", "")
            if uri:
                map_range = None
                if attributes.get("BYTERANGE"):
                    map_range = _parse_byterange(attributes["BYTERANGE"], 0)
                segments.append(MediaSegment(
                    index=index,
                    url=urllib.parse.urljoin(base_url, uri),
                    byte_range=map_range,
                    key_url=current_key_url,
                    key_iv=current_key_iv,
                    init=True,
                ))
                index += 1

        elif line.startswith("#EXTINF:"):
            value = line[len("#EXTINF:"):].split(",", 1)[0].strip()
            try:
                duration = float(value)
            except ValueError:
                duration = 0.0

        elif line.startswith("#EXT-X-BYTERANGE:"):
            byte_range = _parse_byterange(line[len("#EXT-X-BYTERANGE:"):], next_offset)
            if byte_range:
                next_offset = byte_range[1] + 1

        elif not line.startswith("#"):
            segments.append(MediaSegment(
                index=index,
                url=urllib.parse.urljoin(base_url, line),
                duration=duration,
                byte_range=byte_range,
                key_url=current_key_url,
                # An absent IV defaults to the media sequence number
                # (RFC 8216 §5.2) — the *media* sequence, which counts only
                # media segments. `index` counts everything appended, and an
                # `EXT-X-MAP` takes one of those numbers, so on an encrypted
                # fragmented stream every IV after it was off by one and the
                # whole file decrypted to noise: correct-looking bytes, right
                # size, unplayable.
                key_iv=current_key_iv or (
                    None if current_key_url is None
                    else f"{media_sequence + media_index:032x}"
                ),
            ))
            index += 1
            media_index += 1
            duration = 0.0
            byte_range = None

    return segments


def _parse_byterange(value: str, default_offset: int) -> tuple[int, int] | None:
    """``<length>[@<offset>]`` → inclusive ``(start, end)``."""
    text = value.strip()
    if not text:
        return None
    if "@" in text:
        length_text, _, offset_text = text.partition("@")
        try:
            length = int(length_text)
            offset = int(offset_text)
        except ValueError:
            return None
    else:
        try:
            length = int(text)
        except ValueError:
            return None
        offset = default_offset
    if length <= 0:
        return None
    return (offset, offset + length - 1)


def fetch_segments(client: "HttpClient", playlist_url: str,
                   headers: dict[str, str] | None = None) -> list[MediaSegment]:
    """Resolve ``playlist_url`` to a segment list, descending into a master."""
    text = client.get_text(playlist_url, headers)
    if not text.lstrip().startswith("#EXTM3U"):
        raise ExtractionError(f"not an HLS playlist: {playlist_url}")

    if is_master_playlist(text):
        variants = parse_master(text, playlist_url)
        if not variants:
            raise ExtractionError("master playlist contains no playable variants")
        # A variant is playable when it carries pictures, and `RESOLUTION` is
        # *optional* in HLS — plenty of real playlists declare only a bandwidth
        # and a codec list. Choosing on the resolution alone therefore rejected
        # every variant of a perfectly ordinary stream and refused the whole
        # download. What decides it is the codec.
        #
        # And when nothing carries video, the best variant is still the answer:
        # a stream with only sound is a radio station or an album, not a fault.
        with_video = [v for v in variants if v.has_video]
        best = max(with_video or variants,
                   key=lambda v: (v.height or 0, v.tbr))
        text = client.get_text(best.url, headers)
        playlist_url = best.url

    segments = parse_media_playlist(text, playlist_url)
    if not segments:
        raise ExtractionError(f"HLS playlist has no segments: {playlist_url}")
    return segments


def total_duration(segments: list[MediaSegment]) -> float:
    return sum(segment.duration for segment in segments)
