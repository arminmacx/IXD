"""MPEG transport stream → MP4, without re-encoding a single frame.

HLS on a great many sites delivers MPEG-TS segments, and concatenating them
gives a correct, playable `.ts` that a good half of the world's players will not
open by name and that carries no seek index at all. Every commercial download
manager turns it into an MP4, and it is not a rename: the coded frames are
identical, but everything around them is a different shape.

What has to change:

* **Framing.** A transport stream is 188-byte packets carrying PES packets
  carrying elementary streams. MP4 stores samples in one contiguous run with a
  table describing them.
* **H.264 byte format.** Annex-B separates NAL units with start codes and
  repeats its parameter sets throughout the stream. MP4 prefixes each NAL with
  its length and states the parameter sets once, in an `avcC`.
* **AAC framing.** ADTS puts a seven-byte header on every frame. MP4 stores raw
  frames and describes them once, in an `esds`.
* **Timing.** A transport stream carries presentation and decode stamps on a
  90 kHz clock, sparsely — not every packet has one. MP4 wants a duration for
  every sample and a composition offset where the two differ.

Nothing here re-encodes: every byte of every frame is copied. The work is
entirely in taking the packaging apart and putting a different one on.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

from .mp4 import Mp4Error, Sample, Track, write_mp4

PACKET = 188
SYNC = 0x47

#: The stream types this can carry across. Anything else is left alone rather
#: than mangled: a `.ts` that plays is worth more than an `.mp4` that does not.
STREAM_H264 = 0x1B
STREAM_AAC_ADTS = 0x0F
STREAM_AAC_LATM = 0x11

#: The 90 kHz clock every stamp in a transport stream is measured on.
CLOCK = 90000

#: Sampling frequencies by the index an ADTS header carries.
ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
              16000, 12000, 11025, 8000, 7350, 0, 0, 0)


class TsError(Mp4Error):
    """This transport stream cannot be carried into an MP4 as it stands."""


# ---------------------------------------------------------------------------
# transport layer
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _Pes:
    """One elementary-stream packet, reassembled from its transport packets."""

    pid: int
    pts: int | None = None
    dts: int | None = None
    chunks: list[bytes] = field(default_factory=list)

    def payload(self) -> bytes:
        return b"".join(self.chunks)


def _read_stamp(data: bytes, at: int) -> int:
    """A 33-bit PTS/DTS, spread across five bytes with marker bits between."""
    return (((data[at] >> 1) & 0x07) << 30
            | data[at + 1] << 22
            | ((data[at + 2] >> 1) & 0x7F) << 15
            | data[at + 3] << 7
            | ((data[at + 4] >> 1) & 0x7F))


def _parse_pes(payload: bytes, pid: int) -> _Pes | None:
    """Start a PES packet from the beginning of its first transport payload."""
    if len(payload) < 9 or payload[:3] != b"\x00\x00\x01":
        return None
    flags = payload[7]
    header_length = payload[8]
    body = payload[9 + header_length:]
    packet = _Pes(pid=pid, chunks=[body] if body else [])
    if flags & 0x80 and len(payload) >= 14:            # PTS present
        packet.pts = _read_stamp(payload, 9)
        if flags & 0x40 and len(payload) >= 19:        # …and DTS
            packet.dts = _read_stamp(payload, 14)
    if packet.dts is None:
        packet.dts = packet.pts
    return packet


def iter_pes(handle: BinaryIO, streams: dict[int, int]) -> Iterator[_Pes]:
    """Walk a transport stream, yielding each completed PES packet in order.

    The program map is read from the stream itself rather than assumed: a PID
    means nothing without it, and sites do not agree on which numbers to use.
    """
    program_pid = -1
    open_packets: dict[int, _Pes] = {}
    pmt_seen = False

    while True:
        packet = handle.read(PACKET)
        if len(packet) < PACKET:
            break
        if packet[0] != SYNC:
            # Resynchronise: a segment boundary can leave a partial packet.
            offset = packet.find(bytes([SYNC]), 1)
            if offset < 0:
                continue
            handle.seek(handle.tell() - PACKET + offset)
            continue

        pid = ((packet[1] & 0x1F) << 8) | packet[2]
        payload_start = bool(packet[1] & 0x40)
        control = (packet[3] >> 4) & 0x03
        index = 4
        if control & 0x02:                              # adaptation field
            index += 1 + packet[4]
        if not control & 0x01 or index >= PACKET:
            continue
        body = packet[index:]

        if pid == 0:                                    # PAT
            if payload_start and len(body) > 1:
                body = body[1 + body[0]:]
            if len(body) >= 13:
                section_length = ((body[1] & 0x0F) << 8) | body[2]
                end = min(3 + section_length - 4, len(body))
                cursor = 8
                while cursor + 4 <= end:
                    number = (body[cursor] << 8) | body[cursor + 1]
                    entry = ((body[cursor + 2] & 0x1F) << 8) | body[cursor + 3]
                    # Program number zero is the network information table, not
                    # a program map. Taking the first entry blindly pointed the
                    # whole parse at it on any stream that carries one.
                    if number:
                        program_pid = entry
                        break
                    cursor += 4
            continue

        if pid == program_pid and not pmt_seen:         # PMT
            if payload_start and len(body) > 1:
                body = body[1 + body[0]:]
            if len(body) < 12:
                continue
            section_length = ((body[1] & 0x0F) << 8) | body[2]
            info_length = ((body[10] & 0x0F) << 8) | body[11]
            cursor = 12 + info_length
            end = min(3 + section_length - 4, len(body))
            while cursor + 5 <= end:
                stream_type = body[cursor]
                stream_pid = ((body[cursor + 1] & 0x1F) << 8) | body[cursor + 2]
                extra = ((body[cursor + 3] & 0x0F) << 8) | body[cursor + 4]
                streams[stream_pid] = stream_type
                cursor += 5 + extra
            pmt_seen = bool(streams)
            continue

        if pid not in streams:
            # No program map yet, or one this did not read. A PES packet says
            # what it carries in its own header — stream ids 0xE0–0xEF are
            # video, 0xC0–0xDF audio — so the stream is classified from that
            # rather than abandoned. Depending on the map alone meant one
            # unexpected table layout produced "no video track" on a file full
            # of video.
            if not (payload_start and len(body) >= 4
                    and body[:3] == b"\x00\x00\x01"):
                continue
            stream_id = body[3]
            if 0xE0 <= stream_id <= 0xEF:
                streams[pid] = STREAM_H264
            elif 0xC0 <= stream_id <= 0xDF:
                streams[pid] = STREAM_AAC_ADTS
            else:
                continue

        if payload_start:
            previous = open_packets.pop(pid, None)
            if previous is not None:
                yield previous
            started = _parse_pes(body, pid)
            if started is not None:
                open_packets[pid] = started
        elif pid in open_packets:
            open_packets[pid].chunks.append(body)

    for leftover in open_packets.values():
        yield leftover


# ---------------------------------------------------------------------------
# H.264: Annex-B in, AVCC out
# ---------------------------------------------------------------------------
def split_annexb(data: bytes) -> Iterator[bytes]:
    """Yield each NAL unit, without its start code."""
    length = len(data)
    index = 0
    start = -1
    while index < length - 2:
        if data[index] == 0 and data[index + 1] == 0:
            if data[index + 2] == 1:
                if start >= 0:
                    yield data[start:index]
                index += 3
                start = index
                continue
            if (index < length - 3 and data[index + 2] == 0
                    and data[index + 3] == 1):
                if start >= 0:
                    yield data[start:index]
                index += 4
                start = index
                continue
        index += 1
    if start >= 0:
        yield data[start:]


class _Bits:
    """Just enough of a bit reader to walk a sequence parameter set."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def bit(self) -> int:
        byte = self.at >> 3
        if byte >= len(self.data):
            raise TsError("the sequence parameter set ended early")
        value = (self.data[byte] >> (7 - (self.at & 7))) & 1
        self.at += 1
        return value

    def bits(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self) -> int:
        """Unsigned exponential-Golomb, which is how H.264 writes most things."""
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise TsError("malformed exponential-Golomb value")
        return (1 << zeros) - 1 + (self.bits(zeros) if zeros else 0)

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def _unescape(data: bytes) -> bytes:
    """Remove emulation-prevention bytes, which the bit reader must not see."""
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def sps_dimensions(sps: bytes) -> tuple[int, int]:
    """The coded picture size, in pixels, from a sequence parameter set."""
    reader = _Bits(_unescape(sps[1:]))
    profile = reader.bits(8)
    reader.bits(8)                                   # constraint flags + reserved
    reader.bits(8)                                   # level
    reader.ue()                                      # seq_parameter_set_id
    chroma = 1
    if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma = reader.ue()
        if chroma == 3:
            reader.bit()                             # separate_colour_plane
        reader.ue()                                  # bit_depth_luma_minus8
        reader.ue()                                  # bit_depth_chroma_minus8
        reader.bit()                                 # lossless
        if reader.bit():                             # seq_scaling_matrix
            for i in range(8 if chroma != 3 else 12):
                if reader.bit():
                    size = 16 if i < 6 else 64
                    last = next_scale = 8
                    for _ in range(size):
                        if next_scale:
                            next_scale = (last + reader.se() + 256) % 256
                        last = next_scale or last
    reader.ue()                                      # log2_max_frame_num_minus4
    order = reader.ue()
    if order == 0:
        reader.ue()
    elif order == 1:
        reader.bit()
        reader.se()
        reader.se()
        for _ in range(reader.ue()):
            reader.se()
    reader.ue()                                      # max_num_ref_frames
    reader.bit()                                     # gaps_in_frame_num_allowed
    width_units = reader.ue() + 1
    height_units = reader.ue() + 1
    frame_mbs_only = reader.bit()
    if not frame_mbs_only:
        reader.bit()                                 # mb_adaptive_frame_field
    reader.bit()                                     # direct_8x8_inference
    left = right = top = bottom = 0
    if reader.bit():                                 # frame_cropping
        left, right, top, bottom = (reader.ue(), reader.ue(),
                                    reader.ue(), reader.ue())

    sub_width = 1 if chroma == 3 else 2
    sub_height = (1 if chroma == 3 else 2) if chroma != 1 else 2
    if chroma == 0:
        sub_width = sub_height = 1
    width = width_units * 16 - (left + right) * sub_width
    height = ((2 - frame_mbs_only) * height_units * 16
              - (top + bottom) * sub_height * (2 - frame_mbs_only))
    return max(width, 0), max(height, 0)


def _avcc(sps: bytes, pps: bytes) -> bytes:
    return (bytes([1, sps[1], sps[2], sps[3], 0xFF, 0xE1])
            + struct.pack(">H", len(sps)) + sps
            + bytes([1]) + struct.pack(">H", len(pps)) + pps)


def _avc1(sps: bytes, pps: bytes, width: int, height: int) -> bytes:
    from .mp4 import _box                                    # noqa: PLC0415

    entry = (b"\0" * 6 + struct.pack(">H", 1)                 # reserved, index
             + b"\0" * 16
             + struct.pack(">HH", width, height)
             + struct.pack(">II", 0x00480000, 0x00480000)     # 72 dpi
             + struct.pack(">I", 0)
             + struct.pack(">H", 1)                           # frame count
             + b"\0" * 32                                     # compressor name
             + struct.pack(">Hh", 24, -1)
             + _box(b"avcC", _avcc(sps, pps)))
    # `stsd` is a FullBox and `mp4.py` writes it with `_box`, so the version and
    # flags belong to the payload — as they do when one is copied out of an
    # existing file. Omitting them shifts the entry count into them, the entry
    # count into the first entry's size, and an independent demuxer calls the
    # result corrupt while this project's own parser reads it happily.
    return b"\0\0\0\0" + struct.pack(">I", 1) + _box(b"avc1", entry)


# ---------------------------------------------------------------------------
# AAC: ADTS in, raw frames out
# ---------------------------------------------------------------------------
def split_adts(data: bytes) -> Iterator[tuple[bytes, int, int, int]]:
    """Yield ``(raw frame, profile, rate index, channels)`` for each ADTS frame."""
    index = 0
    length = len(data)
    while index + 7 <= length:
        if data[index] != 0xFF or (data[index + 1] & 0xF0) != 0xF0:
            index += 1
            continue
        protection = data[index + 1] & 1
        profile = ((data[index + 2] >> 6) & 0x03) + 1
        rate_index = (data[index + 2] >> 2) & 0x0F
        channels = (((data[index + 2] & 1) << 2) | ((data[index + 3] >> 6) & 0x03))
        frame_length = (((data[index + 3] & 0x03) << 11)
                        | (data[index + 4] << 3)
                        | ((data[index + 5] >> 5) & 0x07))
        if frame_length < 7 or index + frame_length > length:
            break
        header = 7 if protection else 9
        yield (data[index + header:index + frame_length],
               profile, rate_index, channels)
        index += frame_length


def _esds(profile: int, rate_index: int, channels: int) -> bytes:
    """An `esds` carrying the two-byte AudioSpecificConfig ADTS implies."""
    config = bytes([(profile << 3) | (rate_index >> 1),
                    ((rate_index & 1) << 7) | (channels << 3)])

    def descriptor(tag: int, body: bytes) -> bytes:
        return bytes([tag, len(body)]) + body

    decoder_specific = descriptor(0x05, config)
    decoder_config = descriptor(
        0x04, bytes([0x40, 0x15]) + b"\0" * 3 + b"\0" * 8 + decoder_specific)
    sl = descriptor(0x06, bytes([0x02]))
    es = descriptor(0x03, struct.pack(">HB", 1, 0) + decoder_config + sl)
    return b"\0\0\0\0" + es


def _mp4a(profile: int, rate_index: int, channels: int) -> bytes:
    from .mp4 import _box                                    # noqa: PLC0415

    rate = ADTS_RATES[rate_index] or 44100
    entry = (b"\0" * 6 + struct.pack(">H", 1)
             + b"\0" * 8
             + struct.pack(">HH", channels or 2, 16)
             + struct.pack(">HH", 0, 0)
             + struct.pack(">I", min(rate, 0xFFFF) << 16)
             + _box(b"esds", _esds(profile, rate_index, channels)))
    return b"\0\0\0\0" + struct.pack(">I", 1) + _box(b"mp4a", entry)


# ---------------------------------------------------------------------------
# the remux itself
# ---------------------------------------------------------------------------
def _durations(stamps: list[int], samples: list[Sample], default: int) -> None:
    """Give every sample the gap to the next one, in its own timescale."""
    for index, sample in enumerate(samples):
        if index + 1 < len(stamps):
            gap = stamps[index + 1] - stamps[index]
            # A stamp that goes backwards is a discontinuity, not a duration.
            sample.duration = gap if 0 < gap < CLOCK * 10 else default
        else:
            sample.duration = samples[index - 1].duration if index else default


def remux(source: str | Path, output: str | Path,
          on_progress: "callable | None" = None) -> Path:
    """Rewrite a transport stream as an MP4, copying every coded frame.

    Raises :class:`TsError` when the stream carries something this cannot
    describe — an unsupported codec, or no video at all. The caller keeps the
    transport stream in that case: a `.ts` that plays is worth more than an
    `.mp4` that does not.
    """
    source = Path(source)
    video_path = source.with_suffix(source.suffix + ".vide")
    audio_path = source.with_suffix(source.suffix + ".soun")

    sps = pps = b""
    video_samples: list[Sample] = []
    video_stamps: list[int] = []
    video_offsets: list[int] = []
    audio_samples: list[Sample] = []
    audio_config: tuple[int, int, int] | None = None
    video_pid = audio_pid = -1

    with open(source, "rb") as handle, \
            open(video_path, "wb") as video_out, \
            open(audio_path, "wb") as audio_out:
        # Filled in by the walk itself as the program map goes past, which is
        # why it is passed in rather than returned: a PID means nothing without
        # it, and it does not arrive until after the first packets do.
        streams: dict[int, int] = {}
        for packet in iter_pes(handle, streams):
            kind = streams.get(packet.pid)
            if kind is None:
                continue

            if kind == STREAM_H264:
                if video_pid < 0:
                    video_pid = packet.pid
                elif packet.pid != video_pid:
                    continue
                units: list[bytes] = []
                sync = False
                for nal in split_annexb(packet.payload()):
                    if not nal:
                        continue
                    nal_type = nal[0] & 0x1F
                    if nal_type == 7:
                        sps = sps or nal
                        continue
                    if nal_type == 8:
                        pps = pps or nal
                        continue
                    if nal_type in (9, 12):          # delimiter, filler
                        continue
                    if nal_type == 5:
                        sync = True
                    units.append(nal)
                if not units:
                    continue
                offset = video_out.tell()
                for nal in units:
                    video_out.write(struct.pack(">I", len(nal)) + nal)
                size = video_out.tell() - offset
                pts = packet.pts or 0
                dts = packet.dts if packet.dts is not None else pts
                video_stamps.append(dts)
                video_offsets.append(offset)
                video_samples.append(Sample(
                    offset=offset, size=size, duration=0,
                    composition_offset=max(pts - dts, 0), sync=sync,
                ))

            elif kind in (STREAM_AAC_ADTS, STREAM_AAC_LATM):
                if kind == STREAM_AAC_LATM:
                    raise TsError("this stream carries AAC in LATM framing, "
                                  "which cannot be copied into an MP4 as it is")
                if audio_pid < 0:
                    audio_pid = packet.pid
                elif packet.pid != audio_pid:
                    continue
                for frame, profile, rate_index, channels in split_adts(packet.payload()):
                    if audio_config is None:
                        audio_config = (profile, rate_index, channels)
                    offset = audio_out.tell()
                    audio_out.write(frame)
                    audio_samples.append(Sample(
                        offset=offset, size=len(frame),
                        duration=1024, sync=True,
                    ))

    if not video_samples or not sps or not pps:
        for path in (video_path, audio_path):
            path.unlink(missing_ok=True)
        # What was there matters more than what was not: "no H.264 video track"
        # is the same sentence whether the stream is HEVC, whether the program
        # map was misread, or whether the parameter sets are simply carried out
        # of band. Saying which costs nothing and is the difference between a
        # report that can be acted on and one that cannot.
        found = ", ".join(
            f"PID {pid} type 0x{kind:02x}" for pid, kind in sorted(streams.items())
        ) or "no elementary streams at all"
        raise TsError(
            "this transport stream carries no H.264 that could be described in "
            f"an MP4 — it holds {found}"
            + (f"; {len(video_samples)} video packets were read but "
               f"{'no sequence parameter set' if not sps else 'no picture parameter set'}"
               " was found among them" if video_samples else "")
        )

    _durations(video_stamps, video_samples, default=CLOCK // 25)
    width, height = sps_dimensions(sps)

    video_handle = open(video_path, "rb")
    audio_handle = open(audio_path, "rb") if audio_samples else None
    try:
        video = Track(
            handle=video_handle, kind=b"vide", timescale=CLOCK,
            duration=sum(s.duration for s in video_samples),
            sample_description=_avc1(sps, pps, width, height),
            samples=video_samples, width=width, height=height,
        )
        if audio_samples and audio_config is not None:
            profile, rate_index, channels = audio_config
            audio: "Track | None" = Track(
                handle=audio_handle, kind=b"soun",
                timescale=ADTS_RATES[rate_index] or 44100,
                duration=len(audio_samples) * 1024,
                sample_description=_mp4a(profile, rate_index, channels),
                samples=audio_samples,
            )
        else:
            # A stream with no sound is a perfectly ordinary thing; an empty
            # audio track is not, and no player forgives one.
            audio = None
        written = write_mp4(video, audio, output, on_progress=on_progress)
    finally:
        video_handle.close()
        if audio_handle is not None:
            audio_handle.close()
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
    return Path(written)
