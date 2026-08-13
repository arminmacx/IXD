"""Choosing the muxer by what the files actually are.

Two containers reach this project. MP4 carries H.264 with AAC, and everything
at 60fps or above 1080p — VP9, AV1, Opus — arrives as WebM. Each needs its own
muxer, and picking the wrong one produces a message about the *format* rather
than about the mistake: handing two WebM files to the ISOBMFF muxer reports
"no moov box — this is not an MP4", which is true, unhelpful, and cost a user
a 208 MB download.

The container is read from the file rather than from its name. These are
part-files with names like ``62-Something [1080p60].webm.ixddl``, so the
extension describes what the site called the stream and not necessarily what
the bytes are; the first few bytes are unambiguous and cost nothing.
"""

from __future__ import annotations

from pathlib import Path

from . import mp4, webm
from .mp4 import Mp4Error


class MuxError(Exception):
    """The two tracks cannot be combined."""


#: What each container looks like at the front of the file.
MATROSKA = "matroska"
ISOBMFF = "mp4"


def container_of(path: str | Path) -> str:
    """Identify a media container from its opening bytes.

    Returns :data:`MATROSKA`, :data:`ISOBMFF`, or ``""`` when it is neither.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return ""
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return MATROSKA
    if len(head) < 8:
        return ""
    # An ISOBMFF file opens with a sized box. ``ftyp`` is the usual first one
    # and ``styp`` opens a segment, but a file assembled from adaptive media
    # can legitimately begin with any of several, so the shape of the header is
    # what is tested rather than a list of names: a plausible length followed
    # by a four-character printable type.
    if head[4:8] in (b"ftyp", b"styp", b"moov", b"moof", b"free", b"skip",
                     b"mdat", b"sidx"):
        return ISOBMFF
    size = int.from_bytes(head[:4], "big")
    if (size == 1 or size >= 8) and all(
            0x20 <= byte < 0x7F for byte in head[4:8]):
        return ISOBMFF
    return ""


def _describe(container: str) -> str:
    return {MATROSKA: "WebM/Matroska", ISOBMFF: "MP4"}.get(
        container, "an unrecognised container")


def combine(video_path: str | Path, audio_path: str | Path,
            output_path: str | Path, **kwargs) -> Path:
    """Join a video-only and an audio-only file into one, whatever they are.

    Raises :class:`MuxError` when the pair cannot be joined, including when the
    two are different containers — which is a fault in how the pair was chosen,
    not in the files, and is worth saying in those terms.
    """
    video_container = container_of(video_path)
    audio_container = container_of(audio_path)

    if not video_container or not audio_container:
        unknown = video_path if not video_container else audio_path
        raise MuxError(
            f"{Path(unknown).name} is not a container this can read "
            "(expected MP4 or WebM)"
        )
    if video_container != audio_container:
        raise MuxError(
            f"the video track is {_describe(video_container)} and the audio "
            f"track is {_describe(audio_container)}; the two cannot share one "
            "file, so an audio track in the video's own container is needed"
        )

    # Called through the module rather than through a name bound at import.
    # A captured reference is a hazard here: whatever ``mux`` happened to be
    # when this module was first imported would be used forever after, which
    # is invisible until something replaces it and is ignored.
    try:
        if video_container == MATROSKA:
            return webm.mux(video_path, audio_path, output_path, **kwargs)
        return mp4.mux(video_path, audio_path, output_path, **kwargs)
    except (webm.WebmError, Mp4Error) as exc:
        raise MuxError(str(exc)) from exc
