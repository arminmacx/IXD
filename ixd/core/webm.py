"""Matroska/WebM parsing and muxing: two adaptive tracks into one file.

The counterpart to :mod:`ixd.core.mp4`, and needed for the same reason. Above
1080p30 — and for every 60fps, VP9 and AV1 rendition — the video is published
in WebM and its audio in Opus, which no MP4 can hold. Handing those two files
to the ISOBMFF muxer produces exactly what it should: *"no moov box — this is
not an MP4"*. Without this module such a quality can only ever be delivered as
two files, which the project's own requirement forbids.

WebM is Matroska, and Matroska is EBML: an element is an id, a length, and
either a payload or more elements. What makes muxing feasible here is that the
media itself is untouched — a ``SimpleBlock`` is copied byte for byte, with
only its track number and its timestamp *relative to the enclosing cluster*
rewritten. No codec is decoded, re-encoded or even inspected.

Three details cost real care and are pinned by tests:

* **A block's timestamp is a signed 16-bit offset from its cluster**, so a
  cluster may not span more than about 32 seconds — and a block may never be
  placed in a cluster that started after it. Both are enforced when planning.
* **Both source files number their only track ``1``.** One of them has to be
  renumbered, in the track header *and* in every one of its blocks.
* **The layout is written before the positions in it are known.** Cues point at
  clusters and the seek head points at the cues, so the file is written with
  fixed-width placeholders and patched afterwards, rather than being assembled
  in memory — these files run to hundreds of megabytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO


class WebmError(Exception):
    """The input is not usable Matroska."""


# ----------------------------------------------------------------------
# element ids
# ----------------------------------------------------------------------
ID_EBML = 0x1A45DFA3
ID_SEGMENT = 0x18538067
ID_SEEK_HEAD = 0x114D9B74
ID_SEEK = 0x4DBB
ID_SEEK_ID = 0x53AB
ID_SEEK_POSITION = 0x53AC
ID_INFO = 0x1549A966
ID_TIMESTAMP_SCALE = 0x2AD7B1
ID_DURATION = 0x4489
ID_MUXING_APP = 0x4D80
ID_WRITING_APP = 0x5741
ID_TRACKS = 0x1654AE6B
ID_TRACK_ENTRY = 0xAE
ID_TRACK_NUMBER = 0xD7
ID_TRACK_UID = 0x73C5
ID_TRACK_TYPE = 0x83
ID_CODEC_ID = 0x86
ID_CLUSTER = 0x1F43B675
ID_TIMESTAMP = 0xE7
ID_SIMPLE_BLOCK = 0xA3
ID_BLOCK_GROUP = 0xA0
ID_BLOCK = 0xA1
ID_CUES = 0x1C53BB6B
ID_CUE_POINT = 0xBB
ID_CUE_TIME = 0xB3
ID_CUE_TRACK_POSITIONS = 0xB7
ID_CUE_TRACK = 0xF7
ID_CUE_CLUSTER_POSITION = 0xF1

TRACK_TYPE_VIDEO = 1
TRACK_TYPE_AUDIO = 2

#: Elements whose payload is more elements.
_MASTER = frozenset({
    ID_EBML, ID_SEGMENT, ID_SEEK_HEAD, ID_SEEK, ID_INFO, ID_TRACKS,
    ID_TRACK_ENTRY, ID_CLUSTER, ID_BLOCK_GROUP, ID_CUES, ID_CUE_POINT,
    ID_CUE_TRACK_POSITIONS,
})


# ----------------------------------------------------------------------
# EBML primitives
# ----------------------------------------------------------------------
def read_vint(data: bytes, pos: int, keep_marker: bool = False) -> tuple[int, int]:
    """Read one variable-length integer, returning ``(value, next position)``.

    Element ids keep their leading marker bit — the id *is* those bytes — while
    lengths drop it. That difference is the whole of ``keep_marker``.
    """
    if pos >= len(data):
        raise WebmError("truncated element")
    first = data[pos]
    if first == 0:
        raise WebmError("invalid variable-length integer")
    length, mask = 1, 0x80
    while not first & mask:
        mask >>= 1
        length += 1
    if pos + length > len(data):
        raise WebmError("truncated element")
    value = first if keep_marker else first & (mask - 1)
    for offset in range(1, length):
        value = (value << 8) | data[pos + offset]
    return value, pos + length


def _is_unknown_size(data: bytes, start: int, end: int) -> bool:
    """Whether a length field means "runs until something else starts"."""
    width = end - start
    return data[start] & (0xFF >> width) == (0xFF >> width) and all(
        data[i] == 0xFF for i in range(start + 1, end)
    )


def encode_id(value: int) -> bytes:
    length = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(length, "big")


def encode_vint(value: int, width: int = 0) -> bytes:
    """Encode a length. ``width`` forces a size, for a field patched later."""
    if width == 0:
        width = 1
        while value >= (1 << (7 * width)) - 1:
            width += 1
    if value >= (1 << (7 * width)) - 1:
        raise WebmError("value does not fit the requested width")
    return (value | (1 << (7 * width))).to_bytes(width, "big")


def encode_uint(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")


def element(element_id: int, payload: bytes) -> bytes:
    return encode_id(element_id) + encode_vint(len(payload)) + payload


def uint_element(element_id: int, value: int) -> bytes:
    return element(element_id, encode_uint(value))


def float_element(element_id: int, value: float) -> bytes:
    return element(element_id, struct.pack(">d", value))


def element_header_size(element_id: int, payload_size: int) -> int:
    return len(encode_id(element_id)) + len(encode_vint(payload_size))


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
@dataclass(slots=True)
class Block:
    """One frame, located in its source file rather than held in memory."""

    timestamp: int          #: absolute, in the source file's timestamp units
    offset: int             #: where the frame bytes start in the source
    size: int               #: how many bytes of frame data
    flags: int              #: the block's own flags byte, copied verbatim

    @property
    def keyframe(self) -> bool:
        return bool(self.flags & 0x80)


@dataclass(slots=True)
class Track:
    """A parsed single-track WebM file."""

    path: Path
    track_type: int = 0
    codec_id: str = ""
    timestamp_scale: int = 1_000_000
    duration: float = 0.0
    #: The source ``TrackEntry`` children, minus its track number, which is
    #: reassigned on output because both inputs number their track ``1``.
    header_children: bytes = b""
    blocks: list[Block] = field(default_factory=list)

    @property
    def is_video(self) -> bool:
        return self.track_type == TRACK_TYPE_VIDEO


def _iter_children(data: bytes, start: int, end: int):
    """Walk the elements directly inside ``[start, end)``."""
    pos = start
    while pos < end:
        try:
            element_id, after_id = read_vint(data, pos, keep_marker=True)
            size, after_size = read_vint(data, after_id)
        except WebmError:
            return
        if _is_unknown_size(data, after_id, after_size):
            # Only a segment or a cluster may do this; it runs to the end of
            # whatever contains it, and its children are parsed regardless.
            body_end = end
            yield element_id, after_size, body_end
            return
        body_end = min(after_size + size, end)
        yield element_id, after_size, body_end
        pos = body_end


def _parse_track_entry(data: bytes, start: int, end: int, track: Track) -> None:
    kept: list[bytes] = []
    for element_id, body, body_end in _iter_children(data, start, end):
        payload = data[body:body_end]
        if element_id == ID_TRACK_NUMBER:
            continue                    # reassigned on output
        if element_id == ID_TRACK_TYPE:
            track.track_type = int.from_bytes(payload, "big")
        elif element_id == ID_CODEC_ID:
            track.codec_id = payload.decode("ascii", "replace").rstrip("\x00")
        kept.append(encode_id(element_id) + encode_vint(len(payload)) + payload)
    track.header_children = b"".join(kept)


def read_track(path: str | Path) -> Track:
    """Parse a single-track WebM file into its header and its block index.

    The frames are deliberately *not* read: a video track runs to hundreds of
    megabytes and is copied straight from this file to the output at write
    time, so only where each frame sits is recorded here.
    """
    path = Path(path)
    data = path.read_bytes()
    if data[:4] != b"\x1a\x45\xdf\xa3":
        raise WebmError("no EBML header — this is not a Matroska/WebM file")

    track = Track(path=path)
    segment_found = False
    for element_id, body, body_end in _iter_children(data, 0, len(data)):
        if element_id != ID_SEGMENT:
            continue
        segment_found = True
        for child_id, child_body, child_end in _iter_children(data, body, body_end):
            if child_id == ID_INFO:
                for info_id, info_body, info_end in _iter_children(
                        data, child_body, child_end):
                    payload = data[info_body:info_end]
                    if info_id == ID_TIMESTAMP_SCALE:
                        track.timestamp_scale = int.from_bytes(payload, "big")
                    elif info_id == ID_DURATION and len(payload) in (4, 8):
                        track.duration = struct.unpack(
                            ">f" if len(payload) == 4 else ">d", payload)[0]
            elif child_id == ID_TRACKS:
                for entry_id, entry_body, entry_end in _iter_children(
                        data, child_body, child_end):
                    if entry_id == ID_TRACK_ENTRY and not track.header_children:
                        _parse_track_entry(data, entry_body, entry_end, track)
            elif child_id == ID_CLUSTER:
                _parse_cluster(data, child_body, child_end, track)
        break

    if not segment_found:
        raise WebmError("no Segment element — this file has no media in it")
    if not track.header_children:
        raise WebmError("no track header found")
    if not track.blocks:
        raise WebmError("no media blocks found")
    return track


def _parse_cluster(data: bytes, start: int, end: int, track: Track) -> None:
    cluster_time = 0
    for element_id, body, body_end in _iter_children(data, start, end):
        if element_id == ID_TIMESTAMP:
            cluster_time = int.from_bytes(data[body:body_end], "big")
        elif element_id in (ID_SIMPLE_BLOCK, ID_BLOCK_GROUP):
            if element_id == ID_BLOCK_GROUP:
                block = next(
                    ((b, e) for i, b, e in _iter_children(data, body, body_end)
                     if i == ID_BLOCK), None)
                if block is None:
                    continue
                body, body_end = block
                # A block inside a group carries no keyframe flag of its own;
                # absence of a reference is what makes it one, and a group
                # without a ReferenceBlock is by definition a keyframe.
                forced_flags = 0x80
            else:
                forced_flags = None
            _, after_track = read_vint(data, body)
            relative = struct.unpack(">h", data[after_track:after_track + 2])[0]
            flags = data[after_track + 2]
            if forced_flags is not None:
                flags = (flags & 0x7F) | forced_flags
            frame_start = after_track + 3
            track.blocks.append(Block(
                timestamp=cluster_time + relative,
                offset=frame_start,
                size=body_end - frame_start,
                flags=flags,
            ))


# ----------------------------------------------------------------------
# muxing
# ----------------------------------------------------------------------
#: A block's timestamp is a signed 16-bit offset from its cluster, so a cluster
#: can never span more than 32,767 units. One second is the ordinary choice and
#: leaves an enormous margin; the ceiling exists so a pathological input cannot
#: produce a file that silently violates the format.
_MAX_CLUSTER_SPAN = 30_000


@dataclass(slots=True)
class _PlannedBlock:
    track_number: int
    source: int             #: 0 = video, 1 = audio
    block: Block
    timestamp: int          #: in output units


@dataclass(slots=True)
class _PlannedCluster:
    timestamp: int
    blocks: list[_PlannedBlock] = field(default_factory=list)
    payload_size: int = 0
    position: int = 0


def _rescale(timestamp: int, source_scale: int, target_scale: int) -> int:
    if source_scale == target_scale:
        return timestamp
    return (timestamp * source_scale) // target_scale


def _plan(video: Track, audio: Track, scale: int,
          cluster_duration: float) -> list[_PlannedCluster]:
    """Group every block of both tracks into clusters, ordered by time."""
    span = max(1, min(int(cluster_duration * 1_000_000_000 / scale),
                      _MAX_CLUSTER_SPAN))

    planned: list[_PlannedBlock] = []
    for source, (track, number) in enumerate(((video, 1), (audio, 2))):
        for block in track.blocks:
            planned.append(_PlannedBlock(
                track_number=number, source=source, block=block,
                timestamp=_rescale(block.timestamp, track.timestamp_scale, scale),
            ))
    # Video before audio at the same instant, so a player has a picture to
    # show the moment it has sound to play. Sorting is stable, so the blocks
    # of one track keep the order they were published in.
    planned.sort(key=lambda item: (item.timestamp, item.source))

    clusters: list[_PlannedCluster] = []
    current: _PlannedCluster | None = None
    for item in planned:
        needs_new = (
            current is None
            or item.timestamp - current.timestamp >= span
            # A block can never precede its own cluster: the offset is signed
            # but a negative one would place it before a cluster that has not
            # started, which players read as a corrupt file.
            or item.timestamp < current.timestamp
            # Starting each cluster on a keyframe is what makes the cues
            # usable: a seek lands on a cluster and must be able to decode
            # from its first video frame.
            or (item.source == 0 and item.block.keyframe
                and item.timestamp > current.timestamp)
        )
        if needs_new:
            current = _PlannedCluster(timestamp=item.timestamp)
            clusters.append(current)
        assert current is not None
        current.blocks.append(item)

    for cluster in clusters:
        size = len(uint_element(ID_TIMESTAMP, cluster.timestamp))
        for item in cluster.blocks:
            payload = (len(encode_vint(item.track_number)) + 3
                       + item.block.size)
            size += element_header_size(ID_SIMPLE_BLOCK, payload) + payload
        cluster.payload_size = size
    return clusters


def _track_entry(track: Track, number: int) -> bytes:
    return element(
        ID_TRACK_ENTRY,
        uint_element(ID_TRACK_NUMBER, number)
        + uint_element(ID_TRACK_UID, number)
        + track.header_children,
    )


#: Positions are patched in after the fact, so every one is written at a fixed
#: width — a narrower number written later would not fit the hole left for it.
_POSITION_WIDTH = 8


def _fixed_uint(element_id: int, value: int) -> bytes:
    return (encode_id(element_id) + encode_vint(_POSITION_WIDTH)
            + value.to_bytes(_POSITION_WIDTH, "big"))


def mux(video_path: str | Path, audio_path: str | Path,
        output_path: str | Path, *, cluster_duration: float = 1.0,
        on_progress: "callable | None" = None) -> Path:
    """Combine a video-only and an audio-only WebM into one playable file.

    The frames are copied verbatim; only the track numbering and the
    cluster-relative timestamps are rewritten. Nothing is re-encoded.
    """
    video = read_track(video_path)
    audio = read_track(audio_path)
    if not video.is_video:
        raise WebmError(f"{Path(video_path).name} holds no video track")
    if audio.track_type != TRACK_TYPE_AUDIO:
        raise WebmError(f"{Path(audio_path).name} holds no audio track")

    scale = video.timestamp_scale or 1_000_000
    clusters = _plan(video, audio, scale, cluster_duration)

    duration = max(
        _rescale(int(video.duration), video.timestamp_scale, scale),
        _rescale(int(audio.duration), audio.timestamp_scale, scale),
        max((cluster.blocks[-1].timestamp for cluster in clusters
             if cluster.blocks), default=0),
    )

    header = element(ID_EBML, b"".join((
        uint_element(0x4286, 1),        # EBMLVersion
        uint_element(0x42F7, 1),        # EBMLReadVersion
        uint_element(0x42F2, 4),        # EBMLMaxIDLength
        uint_element(0x42F3, 8),        # EBMLMaxSizeLength
        element(0x4282, b"webm"),       # DocType
        uint_element(0x4287, 2),        # DocTypeVersion
        uint_element(0x4285, 2),        # DocTypeReadVersion
    )))

    seek_head = element(ID_SEEK_HEAD, b"".join((
        element(ID_SEEK, element(ID_SEEK_ID, encode_id(ID_INFO))
                + _fixed_uint(ID_SEEK_POSITION, 0)),
        element(ID_SEEK, element(ID_SEEK_ID, encode_id(ID_TRACKS))
                + _fixed_uint(ID_SEEK_POSITION, 0)),
        element(ID_SEEK, element(ID_SEEK_ID, encode_id(ID_CUES))
                + _fixed_uint(ID_SEEK_POSITION, 0)),
    )))

    info = element(ID_INFO, b"".join((
        uint_element(ID_TIMESTAMP_SCALE, scale),
        float_element(ID_DURATION, float(duration)),
        element(ID_MUXING_APP, b"Internet Xtreme Downloader"),
        element(ID_WRITING_APP, b"Internet Xtreme Downloader"),
    )))

    tracks = element(ID_TRACKS,
                     _track_entry(video, 1) + _track_entry(audio, 2))

    # Cues are written after the clusters, because a cue names the position of
    # a cluster and those are only known once they have been laid down.
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_blocks = sum(len(cluster.blocks) for cluster in clusters) or 1
    written_blocks = 0

    sources: list[BinaryIO] = []
    try:
        sources = [open(video.path, "rb"), open(audio.path, "rb")]
        with open(output_path, "wb") as out:
            out.write(header)
            out.write(encode_id(ID_SEGMENT))
            segment_size_at = out.tell()
            # Patched once the size is known; written at a fixed width so the
            # real value cannot fail to fit where the placeholder was.
            out.write(encode_vint(0, _POSITION_WIDTH))
            segment_data_at = out.tell()

            seek_head_at = out.tell()
            out.write(seek_head)
            info_at = out.tell()
            out.write(info)
            tracks_at = out.tell()
            out.write(tracks)

            for cluster in clusters:
                cluster.position = out.tell() - segment_data_at
                out.write(encode_id(ID_CLUSTER))
                out.write(encode_vint(cluster.payload_size))
                out.write(uint_element(ID_TIMESTAMP, cluster.timestamp))
                for item in cluster.blocks:
                    relative = item.timestamp - cluster.timestamp
                    payload_size = (len(encode_vint(item.track_number)) + 3
                                    + item.block.size)
                    out.write(encode_id(ID_SIMPLE_BLOCK))
                    out.write(encode_vint(payload_size))
                    out.write(encode_vint(item.track_number))
                    out.write(struct.pack(">h", relative))
                    out.write(bytes((item.block.flags,)))
                    handle = sources[item.source]
                    handle.seek(item.block.offset)
                    remaining = item.block.size
                    while remaining > 0:
                        piece = handle.read(min(remaining, 1 << 20))
                        if not piece:
                            raise WebmError(
                                "a source file ended in the middle of a frame")
                        out.write(piece)
                        remaining -= len(piece)
                    written_blocks += 1
                if on_progress is not None:
                    on_progress(written_blocks / total_blocks)

            cues_at = out.tell()
            cue_points = b"".join(
                element(ID_CUE_POINT,
                        uint_element(ID_CUE_TIME, cluster.timestamp)
                        + element(ID_CUE_TRACK_POSITIONS,
                                  uint_element(ID_CUE_TRACK, 1)
                                  + uint_element(ID_CUE_CLUSTER_POSITION,
                                                 cluster.position)))
                for cluster in clusters if cluster.blocks
            )
            out.write(element(ID_CUES, cue_points))

            end = out.tell()
            out.seek(segment_size_at)
            out.write(encode_vint(end - segment_data_at, _POSITION_WIDTH))

            # The seek head's three positions, in the order they were written.
            positions = (info_at - segment_data_at,
                         tracks_at - segment_data_at,
                         cues_at - segment_data_at)
            holes = _seek_position_offsets(seek_head)
            for hole, position in zip(holes, positions):
                out.seek(seek_head_at + hole)
                out.write(position.to_bytes(_POSITION_WIDTH, "big"))
    finally:
        for handle in sources:
            handle.close()
    return output_path


def _seek_position_offsets(seek_head: bytes) -> list[int]:
    """Where each SeekPosition payload sits inside the seek head."""
    offsets: list[int] = []
    marker = encode_id(ID_SEEK_POSITION) + encode_vint(_POSITION_WIDTH)
    start = 0
    while True:
        found = seek_head.find(marker, start)
        if found < 0:
            return offsets
        offsets.append(found + len(marker))
        start = found + len(marker)


def parse_cues(data: bytes) -> list[tuple[int, int, int]]:
    """Read a Matroska index: ``(byte offset, start ms, duration ms)`` per cue.

    The same answer `mp4.parse_sidx` gives for an ISOBMFF stream, in the form
    the engine already consumes — because the two exist for the same reason.
    A server-driven session is addressed in *time*, so continuing at a byte
    means converting one to the other, and a stream with no index can only be
    estimated at, which is what stops a track being divided across sessions at
    all.

    Without this, every WebM stream reported "publishes no segment index" and
    ran on a single connection however many were configured — `parse_sidx`
    understands ISOBMFF only, and a WebM stream keeps its index in `Cues`.

    Cue positions are relative to the start of the Segment's data, so that
    offset is tracked while walking in. Anything unreadable yields an empty
    list: an index is a bonus here, never a dependency.
    """
    try:
        return _parse_cues(data)
    except Exception:                    # noqa: BLE001 - a bonus, never fatal
        return []


def _parse_cues(data: bytes) -> list[tuple[int, int, int]]:
    scale_ns = 1_000_000                 # Matroska's default: one millisecond
    segment_start = 0
    cues_span: tuple[int, int] | None = None

    for element_id, body, body_end in _iter_children(data, 0, len(data)):
        if element_id != ID_SEGMENT:
            continue
        segment_start = body
        for child_id, child_body, child_end in _iter_children(data, body, body_end):
            if child_id == ID_INFO:
                for info_id, info_body, info_end in _iter_children(
                        data, child_body, child_end):
                    if info_id == ID_TIMESTAMP_SCALE:
                        scale_ns = int.from_bytes(
                            data[info_body:info_end], "big") or scale_ns
            elif child_id == ID_CUES:
                cues_span = (child_body, child_end)
        break

    if cues_span is None:
        return []

    points: list[tuple[int, int]] = []    # (start ms, byte offset)
    for point_id, point_body, point_end in _iter_children(data, *cues_span):
        if point_id != ID_CUE_POINT:
            continue
        time_units: int | None = None
        position: int | None = None
        for field_id, field_body, field_end in _iter_children(
                data, point_body, point_end):
            if field_id == ID_CUE_TIME:
                time_units = int.from_bytes(data[field_body:field_end], "big")
            elif field_id == ID_CUE_TRACK_POSITIONS:
                for track_id, track_body, track_end in _iter_children(
                        data, field_body, field_end):
                    if track_id == ID_CUE_CLUSTER_POSITION:
                        position = int.from_bytes(
                            data[track_body:track_end], "big")
        if time_units is None or position is None:
            continue
        start_ms = int(time_units * scale_ns / 1_000_000)
        points.append((start_ms, segment_start + position))

    points.sort()
    index: list[tuple[int, int, int]] = []
    for at, (start_ms, offset) in enumerate(points):
        # The last piece's length is not published; zero says "unknown", which
        # is what the caller already expects from a final entry.
        duration = (points[at + 1][0] - start_ms) if at + 1 < len(points) else 0
        index.append((offset, start_ms, duration))
    return index
