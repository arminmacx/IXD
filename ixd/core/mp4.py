"""ISOBMFF (MP4) parsing and muxing, without any external tool.

Adaptive streaming publishes video and audio as separate files. Downloading a
chosen quality therefore yields two files that most players will not play
together, which is not what anyone asked for. This module combines them into a
single MP4 — parsing both source files, merging their tracks into one ``moov``
and writing one interleaved ``mdat``.

Both shapes adaptive streaming produces are read: a plain MP4 whose ``moov``
carries the sample tables, and a **fragmented** one where the tables live in
``moof`` boxes and the ``moov`` is only a template. The output is always plain,
because that is what plays everywhere.

Everything is written against the box layout in ISO/IEC 14496-12: sizes are
32-bit with a 64-bit escape, versions decide between 32- and 64-bit times, and
the sample tables (``stts``/``stsc``/``stsz``/``stco``) are rebuilt rather than
copied because chunk offsets necessarily change when two files are combined.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

#: Boxes that contain other boxes rather than a payload.
_CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf", b"edts", b"udta",
    # Fragmented files put their sample tables inside these.
    b"moof", b"traf", b"mvex",
}


class Mp4Error(Exception):
    """The input is not something this muxer can safely combine."""


@dataclass(slots=True)
class Box:
    """One ISOBMFF box: its type, where its payload lives, and its children."""

    kind: bytes
    offset: int          # absolute offset of the box header
    size: int            # total size including the header
    header: int          # header length (8, or 16 for a 64-bit size)
    children: list["Box"] = field(default_factory=list)

    @property
    def payload_offset(self) -> int:
        return self.offset + self.header

    @property
    def payload_size(self) -> int:
        return self.size - self.header

    def find(self, *path: bytes) -> "Box | None":
        """Walk a chain of child box types, e.g. ``find(b"mdia", b"minf")``."""
        node: "Box | None" = self
        for kind in path:
            if node is None:
                return None
            node = next((c for c in node.children if c.kind == kind), None)
        return node

    def find_all(self, kind: bytes) -> list["Box"]:
        return [c for c in self.children if c.kind == kind]


def parse_boxes(handle: BinaryIO, start: int, end: int) -> list[Box]:
    """Read the box tree between two absolute offsets."""
    boxes: list[Box] = []
    position = start
    while position + 8 <= end:
        handle.seek(position)
        header = handle.read(8)
        if len(header) < 8:
            break
        size, kind = struct.unpack(">I4s", header)
        header_length = 8

        if size == 1:
            extended = handle.read(8)
            if len(extended) < 8:
                break
            size = struct.unpack(">Q", extended)[0]
            header_length = 16
        elif size == 0:
            size = end - position          # "to the end of the file"

        if size < header_length or position + size > end:
            break

        box = Box(kind=kind, offset=position, size=size, header=header_length)
        if kind in _CONTAINERS:
            box.children = parse_boxes(handle, box.payload_offset, position + size)
        boxes.append(box)
        position += size
    return boxes


# ----------------------------------------------------------------------
# sample tables
# ----------------------------------------------------------------------
@dataclass(slots=True)
class Sample:
    """One coded sample: where it is, how big, and when it is shown."""

    offset: int
    size: int
    duration: int
    composition_offset: int = 0
    sync: bool = True


@dataclass(slots=True)
class Track:
    """A single media track, flattened into a list of samples."""

    handle: BinaryIO
    kind: bytes                 # b"vide" or b"soun"
    timescale: int
    duration: int
    sample_description: bytes   # the raw stsd payload, copied verbatim
    samples: list[Sample] = field(default_factory=list)
    language: int = 0x55C4      # "und"
    width: int = 0
    height: int = 0

    @property
    def has_composition_offsets(self) -> bool:
        return any(s.composition_offset for s in self.samples)

    @property
    def all_sync(self) -> bool:
        return all(s.sync for s in self.samples)


def _read(handle: BinaryIO, box: Box) -> bytes:
    handle.seek(box.payload_offset)
    return handle.read(box.payload_size)


def _full_box(data: bytes) -> tuple[int, bytes]:
    """Split a full box's version/flags word from the rest."""
    if len(data) < 4:
        raise Mp4Error("truncated full box")
    return data[0], data[4:]


def read_track(handle: BinaryIO, trak: Box) -> Track:
    """Flatten one ``trak`` into an explicit list of samples.

    The sample tables are run-length encoded in four different ways, so this
    expands all of them. Doing it once up front means the writer only has to
    think about a flat list.
    """
    mdia = trak.find(b"mdia")
    if mdia is None:
        raise Mp4Error("track has no media box")

    mdhd = mdia.find(b"mdhd")
    hdlr = mdia.find(b"hdlr")
    stbl = mdia.find(b"minf", b"stbl")
    if mdhd is None or hdlr is None or stbl is None:
        raise Mp4Error("track is missing its media header or sample table")

    version, body = _full_box(_read(handle, mdhd))
    if version == 1:
        timescale, duration = struct.unpack(">IQ", body[16:28])
        language = struct.unpack(">H", body[28:30])[0]
    else:
        timescale, duration = struct.unpack(">II", body[8:16])
        language = struct.unpack(">H", body[16:18])[0]

    handler = _read(handle, hdlr)[8:12]

    stsd = stbl.find(b"stsd")
    if stsd is None:
        raise Mp4Error("track has no sample description")
    description = _read(handle, stsd)

    samples = _expand_sample_table(handle, stbl)

    width = height = 0
    tkhd = trak.find(b"tkhd")
    if tkhd is not None and handler == b"vide":
        tk_version, tk_body = _full_box(_read(handle, tkhd))
        tail = tk_body[-8:]
        if len(tail) == 8:
            width = struct.unpack(">I", tail[0:4])[0] >> 16
            height = struct.unpack(">I", tail[4:8])[0] >> 16

    return Track(handle=handle, kind=handler, timescale=timescale or 1000,
                 duration=duration, sample_description=description,
                 samples=samples, language=language, width=width, height=height)


def _expand_sample_table(handle: BinaryIO, stbl: Box) -> list[Sample]:
    stts, stsc, stsz, stco, co64, ctts, stss = (
        stbl.find(b"stts"), stbl.find(b"stsc"), stbl.find(b"stsz"),
        stbl.find(b"stco"), stbl.find(b"co64"), stbl.find(b"ctts"),
        stbl.find(b"stss"),
    )
    if stts is None or stsc is None or stsz is None or (stco is None and co64 is None):
        raise Mp4Error("sample table is incomplete")

    # Sizes.
    _, body = _full_box(_read(handle, stsz))
    uniform, count = struct.unpack(">II", body[:8])
    if uniform:
        sizes = [uniform] * count
    else:
        sizes = list(struct.unpack(f">{count}I", body[8:8 + 4 * count]))

    # Durations, run-length encoded.
    _, body = _full_box(_read(handle, stts))
    entries = struct.unpack(">I", body[:4])[0]
    durations: list[int] = []
    for index in range(entries):
        run, delta = struct.unpack(">II", body[4 + index * 8:12 + index * 8])
        durations.extend([delta] * run)
    durations.extend([durations[-1] if durations else 0] * (count - len(durations)))

    # Composition offsets, also run-length encoded and possibly signed.
    offsets = [0] * count
    if ctts is not None:
        ctts_version, body = _full_box(_read(handle, ctts))
        entries = struct.unpack(">I", body[:4])[0]
        cursor = 0
        for index in range(entries):
            run, value = struct.unpack(
                f">I{'i' if ctts_version == 1 else 'I'}",
                body[4 + index * 8:12 + index * 8],
            )
            for _ in range(run):
                if cursor < count:
                    offsets[cursor] = value
                    cursor += 1

    # Chunk offsets.
    if stco is not None:
        _, body = _full_box(_read(handle, stco))
        chunk_count = struct.unpack(">I", body[:4])[0]
        chunk_offsets = list(struct.unpack(f">{chunk_count}I", body[4:4 + 4 * chunk_count]))
    else:
        _, body = _full_box(_read(handle, co64))
        chunk_count = struct.unpack(">I", body[:4])[0]
        chunk_offsets = list(struct.unpack(f">{chunk_count}Q", body[4:4 + 8 * chunk_count]))

    # Samples-per-chunk, run-length encoded by first-chunk index.
    _, body = _full_box(_read(handle, stsc))
    entries = struct.unpack(">I", body[:4])[0]
    runs = [struct.unpack(">III", body[4 + i * 12:16 + i * 12]) for i in range(entries)]

    per_chunk: list[int] = []
    for index, (first_chunk, samples_per_chunk, _description) in enumerate(runs):
        last_chunk = runs[index + 1][0] - 1 if index + 1 < len(runs) else chunk_count
        per_chunk.extend([samples_per_chunk] * max(0, last_chunk - first_chunk + 1))
    per_chunk.extend([per_chunk[-1] if per_chunk else 1] * (chunk_count - len(per_chunk)))

    # Sync samples; absent means every sample is a sync sample.
    sync: set[int] | None = None
    if stss is not None:
        _, body = _full_box(_read(handle, stss))
        sync_count = struct.unpack(">I", body[:4])[0]
        sync = set(struct.unpack(f">{sync_count}I", body[4:4 + 4 * sync_count]))

    samples: list[Sample] = []
    index = 0
    for chunk, chunk_offset in enumerate(chunk_offsets):
        position = chunk_offset
        for _ in range(per_chunk[chunk] if chunk < len(per_chunk) else 0):
            if index >= count:
                break
            samples.append(Sample(
                offset=position,
                size=sizes[index],
                duration=durations[index],
                composition_offset=offsets[index],
                sync=(sync is None or (index + 1) in sync),
            ))
            position += sizes[index]
            index += 1
    if len(samples) != count:
        raise Mp4Error(
            f"sample table describes {count} samples but the chunk map yields "
            f"{len(samples)}"
        )
    return samples


def read_fragmented_track(handle: BinaryIO, moov: Box, boxes: list[Box]) -> Track:
    """Flatten a fragmented file, where the sample tables live in the fragments.

    Adaptive streaming delivers this shape: an initialisation segment carrying
    a ``moov`` whose sample tables are *empty*, followed by ``moof``/``mdat``
    pairs that each describe their own samples. The track is reconstructed by
    walking every fragment and appending what it declares, which yields the
    same flat list a plain file produces — so the writer needs no special case.
    """
    trak = next((t for t in moov.find_all(b"trak") if t.find(b"mdia")), None)
    if trak is None:
        raise Mp4Error("no track in the initialisation segment")

    mdia = trak.find(b"mdia")
    mdhd, hdlr = mdia.find(b"mdhd"), mdia.find(b"hdlr")
    stsd = mdia.find(b"minf", b"stbl", b"stsd")
    if mdhd is None or hdlr is None or stsd is None:
        raise Mp4Error("the initialisation segment is incomplete")

    version, body = _full_box(_read(handle, mdhd))
    if version == 1:
        timescale = struct.unpack(">I", body[16:20])[0]
        language = struct.unpack(">H", body[28:30])[0]
    else:
        timescale = struct.unpack(">I", body[8:12])[0]
        language = struct.unpack(">H", body[16:18])[0]

    handler = _read(handle, hdlr)[8:12]
    width = height = 0
    tkhd = trak.find(b"tkhd")
    if tkhd is not None and handler == b"vide":
        _, tk_body = _full_box(_read(handle, tkhd))
        tail = tk_body[-8:]
        if len(tail) == 8:
            width = struct.unpack(">I", tail[0:4])[0] >> 16
            height = struct.unpack(">I", tail[4:8])[0] >> 16

    samples: list[Sample] = []
    for box in boxes:
        if box.kind != b"moof":
            continue
        samples.extend(_fragment_samples(handle, box))

    if not samples:
        raise Mp4Error("the fragments describe no samples")

    return Track(handle=handle, kind=handler, timescale=timescale or 1000,
                 duration=sum(s.duration for s in samples),
                 sample_description=_read(handle, stsd), samples=samples,
                 language=language, width=width, height=height)


def _fragment_samples(handle: BinaryIO, moof: Box) -> list[Sample]:
    """Samples declared by one ``moof``, with absolute file offsets."""
    samples: list[Sample] = []
    for traf in moof.find_all(b"traf"):
        tfhd = traf.find(b"tfhd")
        if tfhd is None:
            continue

        raw = _read(handle, tfhd)
        flags = int.from_bytes(raw[1:4], "big")
        cursor = 8                              # version/flags + track_ID
        base_offset = moof.offset               # "default base is moof"
        if flags & 0x01:
            base_offset = struct.unpack(">Q", raw[cursor:cursor + 8])[0]
            cursor += 8
        if flags & 0x02:
            cursor += 4                         # sample description index
        default_duration = default_size = default_flags = 0
        if flags & 0x08:
            default_duration = struct.unpack(">I", raw[cursor:cursor + 4])[0]
            cursor += 4
        if flags & 0x10:
            default_size = struct.unpack(">I", raw[cursor:cursor + 4])[0]
            cursor += 4
        if flags & 0x20:
            default_flags = struct.unpack(">I", raw[cursor:cursor + 4])[0]

        for trun in traf.find_all(b"trun"):
            samples.extend(_trun_samples(
                handle, trun, base_offset, default_duration, default_size,
                default_flags,
            ))
    return samples


def _trun_samples(handle: BinaryIO, trun: Box, base_offset: int,
                  default_duration: int, default_size: int,
                  default_flags: int) -> list[Sample]:
    raw = _read(handle, trun)
    version = raw[0]
    flags = int.from_bytes(raw[1:4], "big")
    count = struct.unpack(">I", raw[4:8])[0]
    cursor = 8

    offset = base_offset
    if flags & 0x0001:
        offset = base_offset + struct.unpack(">i", raw[cursor:cursor + 4])[0]
        cursor += 4
    first_flags = default_flags
    if flags & 0x0004:
        first_flags = struct.unpack(">I", raw[cursor:cursor + 4])[0]
        cursor += 4

    samples: list[Sample] = []
    position = offset
    for index in range(count):
        duration, size = default_duration, default_size
        sample_flags = first_flags if index == 0 else default_flags
        composition = 0
        if flags & 0x0100:
            duration = struct.unpack(">I", raw[cursor:cursor + 4])[0]
            cursor += 4
        if flags & 0x0200:
            size = struct.unpack(">I", raw[cursor:cursor + 4])[0]
            cursor += 4
        if flags & 0x0400:
            sample_flags = struct.unpack(">I", raw[cursor:cursor + 4])[0]
            cursor += 4
        if flags & 0x0800:
            fmt = ">i" if version else ">I"
            composition = struct.unpack(fmt, raw[cursor:cursor + 4])[0]
            cursor += 4

        samples.append(Sample(
            offset=position, size=size, duration=duration,
            composition_offset=composition,
            # Bit 16 of the sample flags marks a sample that is *not* a sync
            # point; without any flags every sample is treated as one.
            sync=not (sample_flags & 0x00010000),
        ))
        position += size
    return samples


def parse_sidx(data: bytes) -> list[tuple[int, int, int]]:
    """Read a segment index: ``(byte offset, start ms, duration ms)`` per piece.

    This is the exact answer to a question the streaming protocol otherwise
    forces a client to guess at. Positions there are *times*, so continuing a
    transfer at a byte means converting one to the other — and estimating that
    from the stream's length is only right at constant bitrate. Measured on a
    real video, the estimate was out by 2.3 seconds of playback, which on a
    long file is megabytes: a session asked to resume at a byte was answered
    from somewhere else entirely, and the difference was a hole nothing could
    reach.

    The index is published in the stream's own header, which is already
    fetched before any media, so this costs nothing extra to know.

    Returns an empty list when there is no ``sidx``; a stream without one is
    left to the estimate, which is what it always had.
    """
    position = 0
    box_end = 0
    while position + 8 <= len(data):
        size = int.from_bytes(data[position:position + 4], "big")
        if size < 8:
            return []
        if data[position + 4:position + 8] == b"sidx":
            box_end = position + size
            break
        position += size
    else:
        return []

    cursor = position + 8
    try:
        version = data[cursor]
        cursor += 4                                   # version + flags
        cursor += 4                                   # reference_ID
        timescale = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        width = 4 if version == 0 else 8
        earliest = int.from_bytes(data[cursor:cursor + width], "big")
        cursor += width
        first_offset = int.from_bytes(data[cursor:cursor + width], "big")
        cursor += width
        cursor += 2                                   # reserved
        count = int.from_bytes(data[cursor:cursor + 2], "big")
        cursor += 2
    except (IndexError, ValueError):
        return []
    if not timescale or not count:
        return []

    # The first subsegment begins immediately after this box, plus whatever
    # offset it declares.
    offset = box_end + first_offset
    time = earliest
    entries: list[tuple[int, int, int]] = []
    for _ in range(count):
        if cursor + 12 > len(data):
            break
        word = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 4
        referenced_size = word & 0x7FFFFFFF
        duration = int.from_bytes(data[cursor:cursor + 4], "big")
        cursor += 8                                   # duration + SAP word
        entries.append((offset,
                        int(time * 1000 // timescale),
                        int(duration * 1000 // timescale)))
        offset += referenced_size
        time += duration
    return entries


def open_track(path: str | Path, kind: bytes = b"") -> tuple[BinaryIO, Track]:
    """Open a plain MP4 and flatten one media track.

    ``kind`` selects ``b"vide"`` or ``b"soun"``; without it the first track in
    the file is used. Choosing by kind matters because an adaptive audio file
    is not guaranteed to put its audio track first.
    """
    handle = open(path, "rb")
    try:
        size = handle.seek(0, 2)
        boxes = parse_boxes(handle, 0, size)
        moov = next((b for b in boxes if b.kind == b"moov"), None)
        if moov is None:
            raise Mp4Error("no moov box — this is not an MP4")

        if any(box.kind == b"moof" for box in boxes):
            # Fragmented: the sample tables are in the fragments, not the moov.
            track = read_fragmented_track(handle, moov, boxes)
            if kind and track.kind != kind:
                raise Mp4Error(
                    f"{path} has no {kind.decode()} track "
                    f"(found {track.kind.decode()})"
                )
            return handle, track
        traks = moov.find_all(b"trak")
        if not traks:
            raise Mp4Error("no track in the file")

        tracks = []
        for trak in traks:
            try:
                tracks.append(read_track(handle, trak))
            except Mp4Error:
                continue      # a text or hint track we do not need
        if not tracks:
            raise Mp4Error("no readable track in the file")

        if kind:
            chosen = next((t for t in tracks if t.kind == kind), None)
            if chosen is None:
                raise Mp4Error(
                    f"{path} has no {kind.decode()} track "
                    f"(found {', '.join(t.kind.decode() for t in tracks)})"
                )
            return handle, chosen
        return handle, tracks[0]
    except Exception:
        handle.close()
        raise


# ----------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------
def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def _full(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return _box(kind, struct.pack(">B3s", version, flags.to_bytes(3, "big")) + payload)


def _run_length(values: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for value in values:
        if runs and runs[-1][1] == value:
            runs[-1] = (runs[-1][0] + 1, value)
        else:
            runs.append((1, value))
    return runs


_MATRIX = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def _sample_table(track: Track, chunk_offsets: list[int],
                  samples_per_chunk: list[int]) -> bytes:
    durations = _run_length([s.duration for s in track.samples])
    stts = _full(b"stts", 0, 0, struct.pack(">I", len(durations)) + b"".join(
        struct.pack(">II", run, value) for run, value in durations))

    sizes = [s.size for s in track.samples]
    stsz = _full(b"stsz", 0, 0,
                 struct.pack(">II", 0, len(sizes)) + struct.pack(f">{len(sizes)}I", *sizes))

    runs = _run_length(samples_per_chunk)
    stsc_entries = []
    first = 1
    for run, value in runs:
        stsc_entries.append(struct.pack(">III", first, value, 1))
        first += run
    stsc = _full(b"stsc", 0, 0,
                 struct.pack(">I", len(stsc_entries)) + b"".join(stsc_entries))

    # 64-bit offsets whenever the output could exceed 4 GiB.
    if chunk_offsets and chunk_offsets[-1] > 0xFFFFFFFF:
        stco = _full(b"co64", 0, 0, struct.pack(">I", len(chunk_offsets))
                     + struct.pack(f">{len(chunk_offsets)}Q", *chunk_offsets))
    else:
        stco = _full(b"stco", 0, 0, struct.pack(">I", len(chunk_offsets))
                     + struct.pack(f">{len(chunk_offsets)}I", *chunk_offsets))

    parts = [stts, track.sample_description and _box(b"stsd", track.sample_description),
             stsc, stsz, stco]

    if track.has_composition_offsets:
        offsets = _run_length([s.composition_offset for s in track.samples])
        parts.append(_full(b"ctts", 1, 0, struct.pack(">I", len(offsets)) + b"".join(
            struct.pack(">Ii", run, value) for run, value in offsets)))

    if not track.all_sync:
        keys = [i + 1 for i, s in enumerate(track.samples) if s.sync]
        parts.append(_full(b"stss", 0, 0,
                           struct.pack(">I", len(keys)) + struct.pack(f">{len(keys)}I", *keys)))

    return _box(b"stbl", b"".join(p for p in parts if p))


def _track_box(track: Track, track_id: int, movie_timescale: int,
               chunk_offsets: list[int], samples_per_chunk: list[int]) -> bytes:
    total = sum(s.duration for s in track.samples)
    movie_duration = int(total * movie_timescale / (track.timescale or 1))

    flags = 0x000007          # enabled | in movie | in preview
    # tkhd v0: creation, modification, track id, reserved, duration, then
    # *eight* reserved bytes before layer/alternate group/volume. Omitting
    # those eight shifts the matrix and the dimensions, and a real demuxer
    # rejects the file outright — the structure still round-trips through a
    # lenient reader, which is why this is checked against one that is not.
    volume = 0x0100 if track.kind == b"soun" else 0
    tkhd = _full(b"tkhd", 0, flags,
                 struct.pack(">IIIII", 0, 0, track_id, 0, movie_duration)
                 + b"\0" * 8
                 + struct.pack(">hhhh", 0, 0, volume, 0)
                 + _MATRIX
                 + struct.pack(">II", track.width << 16, track.height << 16))

    mdhd = _full(b"mdhd", 0, 0, struct.pack(
        ">IIIIHH", 0, 0, track.timescale, total, track.language, 0))

    name = b"VideoHandler\0" if track.kind == b"vide" else b"SoundHandler\0"
    hdlr = _full(b"hdlr", 0, 0, struct.pack(">I4s", 0, track.kind) + b"\0" * 12 + name)

    if track.kind == b"vide":
        media_header = _full(b"vmhd", 0, 1, struct.pack(">HHHH", 0, 0, 0, 0))
    else:
        media_header = _full(b"smhd", 0, 0, struct.pack(">hH", 0, 0))

    dref = _full(b"dref", 0, 0, struct.pack(">I", 1) + _full(b"url ", 0, 1, b""))
    dinf = _box(b"dinf", dref)

    minf = _box(b"minf", media_header + dinf
                + _sample_table(track, chunk_offsets, samples_per_chunk))
    mdia = _box(b"mdia", mdhd + hdlr + minf)
    return _box(b"trak", tkhd + mdia)


def mux(video_path: str | Path, audio_path: str | Path,
        output_path: str | Path, *, chunk_duration: float = 1.0,
        on_progress: "callable | None" = None) -> Path:
    """Combine a video-only and an audio-only MP4 into one playable file.

    Samples are interleaved in roughly one-second groups so the result streams
    and seeks like a normal file rather than requiring the whole video to be
    read before any audio is found.
    """
    video_handle, video = open_track(video_path, b"vide")
    audio_handle, audio = open_track(audio_path, b"soun")
    try:
        return write_mp4(video, audio, output_path,
                         chunk_duration=chunk_duration, on_progress=on_progress)
    finally:
        video_handle.close()
        audio_handle.close()


def write_mp4(video: Track, audio: "Track | None", output_path: str | Path, *,
              chunk_duration: float = 1.0,
              on_progress: "callable | None" = None) -> Path:
    """Write two prepared tracks out as one interleaved MP4.

    Separated from :func:`mux` so a track can come from somewhere other than an
    existing MP4 — the transport-stream remuxer builds its own and hands them
    straight here, rather than writing an intermediate file only to read it
    back.
    """
    # A stream with no sound is perfectly ordinary; an *empty* audio track is
    # not, and no player forgives one — so a silent source is written with one
    # track rather than two, one of which describes nothing.
    if audio is not None and not audio.samples:
        audio = None
    if True:
        groups = (_interleave(video, audio, chunk_duration) if audio is not None
                  else [(video, video.samples)])

        # The header must be written before the media, but its chunk offsets
        # depend on where the media lands. Build it once with placeholder
        # offsets to learn its length, then again with the real ones.
        movie_timescale = 1000
        for _attempt in range(4):
            offsets = _plan_offsets(groups, header_length=_header_length(
                video, audio, movie_timescale, groups))
            header = _build_header(video, audio, movie_timescale, groups, offsets)
            if len(header) == _header_length(video, audio, movie_timescale, groups):
                break
        else:  # pragma: no cover - the size stabilises on the first pass
            raise Mp4Error("could not settle the header size")

        output = Path(output_path)
        written = 0
        with open(output, "wb") as out:
            out.write(header)
            for track, samples in groups:
                for sample in samples:
                    track.handle.seek(sample.offset)
                    data = track.handle.read(sample.size)
                    if len(data) != sample.size:
                        raise Mp4Error("source file ended inside a sample")
                    out.write(data)
                    written += len(data)
                if on_progress is not None:
                    on_progress(written)
        return output


def _interleave(video: Track, audio: Track,
                chunk_duration: float) -> list[tuple[Track, list[Sample]]]:
    """Split both tracks into time-ordered groups of samples."""
    groups: list[tuple[Track, list[Sample]]] = []
    cursors = {id(video): 0, id(audio): 0}
    times = {id(video): 0.0, id(audio): 0.0}
    boundary = chunk_duration

    while cursors[id(video)] < len(video.samples) or cursors[id(audio)] < len(audio.samples):
        for track in (video, audio):
            key = id(track)
            batch: list[Sample] = []
            while cursors[key] < len(track.samples) and times[key] < boundary:
                sample = track.samples[cursors[key]]
                batch.append(sample)
                times[key] += sample.duration / (track.timescale or 1)
                cursors[key] += 1
            if batch:
                groups.append((track, batch))
        boundary += chunk_duration
    return groups


def _plan_offsets(groups: list[tuple[Track, list[Sample]]],
                  header_length: int) -> dict[int, list[int]]:
    """Where each track's chunks will start once the header is in front."""
    offsets: dict[int, list[int]] = {}
    position = header_length
    for track, samples in groups:
        offsets.setdefault(id(track), []).append(position)
        position += sum(s.size for s in samples)
    return offsets


def _chunk_plan(groups: list[tuple[Track, list[Sample]]],
                track: Track) -> list[int]:
    return [len(samples) for owner, samples in groups if owner is track]


def _build_header(video: Track, audio: "Track | None", movie_timescale: int,
                  groups: list[tuple[Track, list[Sample]]],
                  offsets: dict[int, list[int]]) -> bytes:
    present = [track for track in (video, audio) if track is not None]
    longest = max(
        sum(s.duration for s in track.samples) / (track.timescale or 1)
        for track in present
    )
    mvhd = _full(b"mvhd", 0, 0, struct.pack(
        ">IIII", 0, 0, movie_timescale, int(longest * movie_timescale)
    ) + struct.pack(">IHHII", 0x00010000, 0x0100, 0, 0, 0)
        + _MATRIX + b"\0" * 24 + struct.pack(">I", 3))

    tracks = b"".join(
        _track_box(track, index + 1, movie_timescale,
                   offsets.get(id(track), []), _chunk_plan(groups, track))
        for index, track in enumerate(present)
    )
    moov = _box(b"moov", mvhd + tracks)

    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 0x200)
                + b"isomiso2avc1mp41")
    media_size = sum(sum(s.size for s in samples) for _track, samples in groups)
    if media_size + len(ftyp) + len(moov) > 0xFFFFFFFF:
        mdat = struct.pack(">I4sQ", 1, b"mdat", media_size + 16)
    else:
        mdat = struct.pack(">I4s", media_size + 8, b"mdat")
    return ftyp + moov + mdat


def _header_length(video: Track, audio: "Track | None", movie_timescale: int,
                   groups: list[tuple[Track, list[Sample]]]) -> int:
    """Header size with placeholder offsets — the size does not depend on them."""
    placeholder = _plan_offsets(groups, header_length=0)
    return len(_build_header(video, audio, movie_timescale, groups, placeholder))


def probe_kind(path: str | Path) -> bytes:
    """``b"vide"``, ``b"soun"`` or ``b""`` when the file cannot be read."""
    try:
        handle, track = open_track(path)
    except (OSError, Mp4Error):
        return b""
    try:
        return track.kind
    finally:
        handle.close()
