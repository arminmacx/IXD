"""Server-driven adaptive streaming (SABR), implemented natively.

Some videos are published with no fetchable stream URL at all: the player
response carries only a *server* streaming endpoint, and the media is obtained
by asking that endpoint for it. This module speaks that protocol directly —
protobuf request, UMP-framed response — so those videos remain downloadable
without wrapping any external tool.

**Request.** A protobuf ``VideoPlaybackAbrRequest`` naming the wanted format,
the opaque per-session configuration taken from the player response, and the
ranges already held. The server decides what to send next.

**Response.** UMP: a flat sequence of ``varint type, varint size, payload``
parts. Its varint is *not* the protobuf one — the leading bits of the first
byte give the width — which is why a protobuf reader appears to parse the
stream and then drifts. The parts that matter are:

======  =====================================================================
20      media header: which format, which byte offset, how long
21      media: a header id followed by raw bytes of that format
22      end of the media run for a header id
43      redirect to a different server
58      the server reporting a problem
======  =====================================================================

**Limits.** The server serves roughly sixty seconds of playback per session and
then stops, changing its protection status from 2 ("pending") to 3. That
ceiling was measured against every variable that could plausibly move it —
request shape, six client identities, the session cookies, a proof-of-origin
token lifted from a real browser session, the playback cookie echoed back, and
a renewed session seeded with it. None of them shifted the boundary by a byte,
including a replay of a browser's *own* endpoint, configuration and token.

So this is a playback-rate ceiling, not an attestation gate: a player obtains a
whole video by playing it, staying inside the window as its playhead advances,
and that is not something a downloader can shortcut. :class:`SabrStream`
therefore reports precisely where it stopped rather than producing a truncated
file, and the caller decides what to tell the user.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from ..core.errors import CancelledError, ExtractionError
from ..core.protobuf import Message, parse

#: UMP part identifiers used here.
PART_MEDIA_HEADER = 20
PART_MEDIA = 21
PART_MEDIA_END = 22
PART_BUFFER_POLICY = 35
PART_REDIRECT = 43
#: Attestation state, *not* a failure. It accompanies perfectly good media —
#: 1 means the session is trusted, anything else means it is not yet, which is
#: why an anonymous session is cut off partway rather than refused outright.
PART_PROTECTION_STATUS = 58
PROTECTION_OK = 1
#: The server is still deciding; media keeps arriving meanwhile.
PROTECTION_PENDING = 2
#: The server wants the session attested and sends no media until it is.
PROTECTION_REQUIRED = 3

#: A context the server issues mid-session and expects **handed back** on
#: every request after it. Ignoring it is not harmless: the next request is
#: judged against a configuration the client no longer matches, and the reply
#: is 31 bytes of `sabr.malformed_config` with no media at all. That is the
#: shape of the Windows field report of 2026-08-13 — two of these arrived,
#: nothing here read them, and the session delivered 0 of 56,141,099 bytes.
PART_SABR_CONTEXT_UPDATE = 57
#: Which context types to keep sending, and which to stop sending.
PART_SABR_CONTEXT_SENDING_POLICY = 59

#: Field numbers inside a context update.
_CONTEXT_TYPE = 1
_CONTEXT_VALUE = 3
#: …and inside the sending policy, whose field 1 lists the types to start
#: sending. Only the withdrawal is acted on: a context is already sent from
#: the moment it arrives, so being told to start sending one changes nothing.
_POLICY_STOP = 2
#: Where an echoed context belongs in the streamer context (field 19 of the
#: request): repeated `{type, value}`. The value is field 3 in the update the
#: server sends and field 2 in the entry sent back — they are two different
#: messages that happen to carry the same pair.
_STREAMER_SABR_CONTEXT = 5
_SENT_CONTEXT_TYPE = 1
_SENT_CONTEXT_VALUE = 2

#: What each UMP part is, for the log. A number alone gave no way to tell an
#: interesting part from a dull one — "type 57" read as noise for as long as it
#: took to match it against a refusal.
PART_NAMES = {
    10: "onesie header", 11: "onesie data",
    20: "media header", 21: "media", 22: "media end",
    31: "live metadata", 35: "buffer policy",
    42: "format initialisation", 43: "redirect", 44: "sabr error",
    45: "sabr seek", 46: "reload player response",
    PART_SABR_CONTEXT_UPDATE: "sabr context update",
    PART_PROTECTION_STATUS: "protection status",
    PART_SABR_CONTEXT_SENDING_POLICY: "sabr context sending policy",
    61: "sabr ack", 62: "end of track",
}


def part_name(part_type: int) -> str:
    known = PART_NAMES.get(part_type)
    return f"{known} ({part_type})" if known else f"type {part_type}"


#: Field numbers within a media header.
_HEADER_ID = 1
_HEADER_ITAG = 3
_HEADER_START_RANGE = 6
_HEADER_IS_INIT = 8
_HEADER_SEQUENCE = 9
_HEADER_LENGTH = 14
_HEADER_TIME_RANGE = 15

def _readable(fields: dict) -> str:
    """A protobuf field map as something a log can carry.

    Nested messages arrive as bytes and are shown expanded one level, because a
    format description keeps everything that matters — the stream's identity
    and the ranges its opening occupies — one level down.
    """
    pieces: list[str] = []
    for number in sorted(fields):
        value = fields[number]
        if isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            text = raw.decode("utf-8", "replace")
            if raw and all(32 <= byte < 127 for byte in raw):
                pieces.append(f"{number}={text!r}")
                continue
            try:
                inner = parse(raw)
            except Exception:                 # noqa: BLE001 - not a message
                inner = {}
            if inner:
                pieces.append(f"{number}={{" + ", ".join(
                    f"{k}={v!r}" if not isinstance(v, (bytes, bytearray))
                    else f"{k}=<{len(v)} bytes>"
                    for k, v in sorted(inner.items())) + "}")
            else:
                pieces.append(f"{number}=<{len(raw)} bytes>")
        else:
            pieces.append(f"{number}={value!r}")
    return ", ".join(pieces) or "(empty)"


#: What the server says about a format before sending any of it: the ranges its
#: opening occupies, and the mime type. It arrives once per reply and carries no
#: media — which is what makes it the answer to "where are the first 965 bytes":
#: the endpoint describes them and expects a client to already hold them.
PART_FORMAT_INIT = 42

#: Field numbers within it. Read defensively — an unrecognised shape is logged
#: raw rather than guessed at, because a wrong reading here would place the
#: opening of a file at an offset that is merely plausible.
_INIT_FORMAT_ID = 2
_INIT_MIME = 5
_INIT_INIT_RANGE = 6
_INIT_INDEX_RANGE = 7
_RANGE_START = 1
_RANGE_END = 2

#: Field 7 of the next-request policy carries the playback cookie.
_POLICY_PLAYBACK_COOKIE = 7

#: Field numbers within ``ClientAbrState``.
_STATE_PLAYER_TIME_MS = 28
_STATE_TRACK_TYPES = 55

#: Track selection: 1 = audio only, 2 = video only, 3 = both.
TRACK_AUDIO = 1
TRACK_VIDEO = 2

#: Consecutive empty replies tolerated before concluding the session is spent.
#: Kept small deliberately: waiting was measured over several minutes and the
#: server never resumed, so patience only delays an unavoidable failure.
_EMPTY_LIMIT = 2
#: Never loop forever, however the server behaves.
_MAX_REQUESTS = 400
#: How many times to wind the session back and ask again for a stretch the
#: server skipped. A hole it will not fill has to end the transfer rather than
#: keep it running, so this is small and bounded.
_MAX_REFILLS = 4

#: How many replies may arrive full of data that is already held before
#: the transfer is called stuck. A session opened in the wrong place
#: does this indefinitely, at full speed, with no progress at all.
_STALLED_LIMIT = 3


def read_varint(buffer: bytes, position: int) -> tuple[int, int]:
    """Read one UMP varint, returning ``(value, next_position)``.

    The width is encoded in the leading bits of the first byte rather than in
    a continuation bit per byte, so this is not interchangeable with the
    protobuf varint despite looking similar for small values.
    """
    if position >= len(buffer):
        raise ValueError("varint runs past the end of the buffer")
    first = buffer[position]
    if first < 0x80:
        return first, position + 1
    if first < 0xC0:
        return (first & 0x3F) | (buffer[position + 1] << 6), position + 2
    if first < 0xE0:
        extra = int.from_bytes(buffer[position + 1:position + 3], "little")
        return (first & 0x1F) | (extra << 5), position + 3
    if first < 0xF0:
        extra = int.from_bytes(buffer[position + 1:position + 4], "little")
        return (first & 0x0F) | (extra << 4), position + 4
    return int.from_bytes(buffer[position + 1:position + 5], "little"), position + 5


def varint_width(first: int) -> int:
    """How many bytes the varint beginning with ``first`` occupies."""
    if first < 0x80:
        return 1
    if first < 0xC0:
        return 2
    if first < 0xE0:
        return 3
    if first < 0xF0:
        return 4
    return 5


def iter_parts(payload: bytes) -> Iterator[tuple[int, bytes]]:
    """Split a UMP response into ``(part_type, payload)`` pairs."""
    position = 0
    while position < len(payload):
        part_type, position = read_varint(payload, position)
        size, position = read_varint(payload, position)
        if position + size > len(payload):
            raise ValueError("UMP part claims more bytes than the response holds")
        yield part_type, payload[position:position + size]
        position += size


def iter_parts_streaming(chunks: Iterator[bytes]) -> Iterator[tuple[int, bytes]]:
    """Split a UMP response into parts *as it arrives*, not once it has.

    A reply is a few hundred kilobytes to several megabytes and tiles into
    parts of some tens of kilobytes. Reading the whole reply before writing
    any of it means the download makes no observable progress for as long as
    the reply takes to arrive, then jumps — which is exactly what a stalling,
    stuttering transfer looks like from the outside, and what the user reported.
    The bytes were moving the whole time; nothing recorded them until the end.

    Yielding parts as the socket delivers them makes progress continuous, and
    incidentally bounds memory to one part rather than to one whole reply.
    """
    buffer = bytearray()
    position = 0
    exhausted = False

    def pull() -> bool:
        """Take one more piece from the source; False when there is none."""
        nonlocal exhausted
        if exhausted:
            return False
        for piece in chunks:
            if piece:
                buffer.extend(piece)
                return True
        exhausted = True
        return False

    while True:
        # A part is a type, a length, and that many bytes; any of the three may
        # be split across reads, so each is waited for rather than assumed.
        while position >= len(buffer):
            if not pull():
                return
        while position + varint_width(buffer[position]) > len(buffer):
            if not pull():
                raise ValueError("UMP response ended inside a part header")
        part_type, cursor = read_varint(buffer, position)

        while cursor >= len(buffer):
            if not pull():
                raise ValueError("UMP response ended inside a part header")
        while cursor + varint_width(buffer[cursor]) > len(buffer):
            if not pull():
                raise ValueError("UMP response ended inside a part header")
        size, cursor = read_varint(buffer, cursor)

        while cursor + size > len(buffer):
            if not pull():
                raise ValueError(
                    "UMP part claims more bytes than the response holds")
        yield part_type, bytes(buffer[cursor:cursor + size])
        position = cursor + size

        # Nothing before the cursor will be looked at again, so it is dropped
        # rather than left to grow for the length of the reply.
        if position > (1 << 20):
            del buffer[:position]
            position = 0


@dataclass(slots=True)
class SabrFormat:
    """The identity of one stream, as the server expects it back."""

    itag: int
    last_modified: int
    size: int = 0
    is_audio: bool = False
    xtags: str = ""

    def to_message(self) -> Message:
        # The tag field is written even when empty, because that is what the
        # site's own player sends and the server matches on the encoded bytes.
        # Omitting it measurably halves how much media each reply carries.
        return (Message()
                .varint(1, self.itag)
                .varint(2, self.last_modified)
                .string(3, self.xtags))


@dataclass(slots=True)
class SabrResult:
    """What one exchange with the server produced."""

    bytes_written: int = 0
    #: Of those, how many were not already held. A reply can be full of data
    #: and gain nothing, which is what a session opened at the wrong position
    #: does — and it looks identical to healthy progress from the outside.
    gained: int = 0
    sequence: int = 0
    player_time_ms: int = 0
    finished: bool = False
    redirect: str = ""
    error: str = ""
    attested: bool = False
    protection_status: int = 0
    ranges: list[tuple[int, int]] = field(default_factory=list)


class SabrStream:
    """Fetches one format from a server-driven streaming endpoint."""

    def __init__(self, client: Any, endpoint: str, config_blob: bytes,
                 media_format: SabrFormat, user_agent: str = "",
                 client_id: int = 5, po_token: bytes | None = None,
                 streamer_context: bytes | None = None,
                 duration_ms: int = 0,
                 log: "Callable[[str], None] | None" = None) -> None:
        #: Where to say what the server actually sent. A gap at byte zero is
        #: invisible from outside this class, and reasoning about it from the
        #: outcome is how three sessions were spent — so the media headers are
        #: reported and the answer comes from the field rather than from here.
        self._log = log or (lambda _message: None)
        #: Media headers already described, so a long transfer does not narrate
        #: every segment it receives.
        self._described = 0
        #: Foreign itags this session was sent, and how often. A session asks
        #: for one stream and is sent others anyway; that is worth knowing once,
        #: not once per segment.
        self._foreign: dict[int, int] = {}
        #: What the server says this stream's opening bytes are, from its own
        #: format description. Set once the first such part arrives.
        self.init_range: tuple[int, int] | None = None
        self.index_range: tuple[int, int] | None = None
        self._described_format = False
        self._foreign_described = False
        #: UMP part types arriving that nothing here acts on, and their sizes.
        #: The initialisation segment is missing from every session measured,
        #: and an unread part is the remaining place it could be.
        self._unhandled: dict[int, int] = {}
        self.client = client
        self.endpoint = endpoint
        self.config = config_blob
        self.format = media_format
        self.user_agent = user_agent
        self.client_id = client_id
        self.po_token = po_token
        #: The stream's running time. Positions are expressed in time, not
        #: bytes, so without this a transfer cannot ask to resume at an offset
        #: — which is what left a session restarting from the beginning and
        #: running out of its allowance before it reached the missing tail.
        self.duration_ms = duration_ms
        #: The player's own streamer context, captured from its request. It
        #: carries the proof of origin together with the client identity the
        #: proof was issued against; the two cannot be separated, so when it is
        #: present it is sent back whole rather than rebuilt.
        self.streamer_context = streamer_context

        #: The stream's published segment index: ``(byte, start ms, ms)`` per
        #: piece, read from the ``sidx`` in its own header. This is the exact
        #: byte-to-time conversion; without it the only way to ask for a byte
        #: is to estimate from the stream's length, which is right only at
        #: constant bitrate and was measured 2.3 seconds of playback out on a
        #: real video — enough, on a long file, to land megabytes away.
        self.index: list[tuple[int, int, int]] = []

        self.playback_cookie: bytes = b""
        #: Contexts the server has issued this session, by type. They are
        #: handed back on every request after the one that delivered them —
        #: that is what the server means by asking for them, and a request
        #: without them is answered `sabr.malformed_config`.
        self.sabr_contexts: dict[int, bytes] = {}
        self._buffered_ms = 0
        self._request_number = 0
        self._sequence = 0
        self._player_time_ms = 0
        self._written = 0
        #: Last protection status the server reported, so a transfer that ends
        #: early can say whether it was refused or merely ran out.
        self._protection = 0
        #: Highest byte offset written. Summing what arrives is not the same
        #: measure: an initialisation segment can be re-sent, and a gap would
        #: go unnoticed. The end of the file is what says the file is complete.
        self._end = 0
        #: Every byte range actually written, merged. The highest offset alone
        #: is not proof of a whole file either: if the server skips a block and
        #: then delivers the rest, the end is reached while a hole remains in
        #: the middle. That hole reads back as zeros, and the result is a file
        #: whose picture freezes a few seconds in while its sound plays on.
        self._covered: list[list[int]] = []
        #: The playback position an interrupted attempt had reached, if this
        #: transfer is a continuation of one.
        self._resume_ms = 0
        #: The segment index that attempt had reached. A resumed session has to
        #: hand this back: the request that tells the server what is already
        #: held is only sent when a sequence is known, so without it a
        #: continuation carries nothing but a player clock — and the clock
        #: alone does not move the server, which is what left every resumed
        #: transfer being re-sent the file from byte zero.
        self._resume_sequence = 0
        #: The position to report next, when it has been chosen rather than
        #: followed from what has arrived — a resume, or a wind-back to fill a
        #: gap. It has to survive to the next request intact.
        self._pinned_ms: int | None = None
        #: How many times the transfer has wound itself back to the first
        #: missing byte after the server ignored where it was asked to start.
        self._byte_seeks = 0
        #: Bytes that arrived having already been held. Counted separately
        #: from the total written, because a failure that reports the total as
        #: though it were all re-sent describes a session that was working
        #: perfectly until its last few replies as one that never worked.
        self._resent = 0
        #: ``sequence -> (start byte, end position in ms)`` for every segment
        #: this session has been told about, which is what makes a wind-back
        #: exact rather than a conversion from bytes to time.
        self._segments: dict[int, tuple[int, int]] = {}

    # ------------------------------------------------------------------
    @staticmethod
    def config_from_player_response(response: dict) -> bytes:
        """Extract the opaque per-session configuration blob."""
        config = (((response.get("playerConfig") or {})
                   .get("mediaCommonConfig") or {})
                  .get("mediaUstreamerRequestConfig") or {})
        encoded = config.get("videoPlaybackUstreamerConfig") or ""
        if not encoded:
            return b""
        padding = "=" * (-len(encoded) % 4)
        try:
            return base64.urlsafe_b64decode(encoded + padding)
        except (ValueError, TypeError):
            return b""

    def _build_request(self) -> bytes:
        state = (Message()
                 .varint(_STATE_PLAYER_TIME_MS, self._player_time_ms)
                 .varint(_STATE_TRACK_TYPES,
                         TRACK_AUDIO if self.format.is_audio else TRACK_VIDEO))

        streamer: Message | None = None
        if not self.streamer_context:
            streamer = Message().message(1, Message().varint(3, self.client_id))
            if self.po_token:
                streamer.raw(2, self.po_token)
            if self.playback_cookie:
                # The server issues this with each reply and expects it back.
                # It is how a session is recognised as continuing rather than
                # restarting, and the player echoes it on every request.
                streamer.raw(3, self.playback_cookie)

        # The wanted stream is named twice: once as the selection, and once in
        # the slot for its own kind. Audio and video have *separate* slots —
        # announcing an audio track in the video slot leaves the request
        # self-contradictory and the server answers with nothing at all.
        preferred_slot = 16 if self.format.is_audio else 17

        request = Message().message(1, state)

        # Field 2 is the *selected* formats — the ones the client already holds
        # and has initialised. Naming the wanted stream there is what made the
        # server skip its initialisation segment.
        #
        # Measured, from the server's own format description (part 42) on
        # 2026-08-12:
        #
        #   1='p8XSB8VVrNY', 2={1=251,…}, 5='audio/webm; codecs="opus"',
        #   6={1=0, 2=258}, 7={1=259, 2=425}
        #
        # It describes and initialises **251** — a format never asked for —
        # with its init range at 0–258, and never describes 137, which is named
        # in field 2 *and* in the preferred-video slot. It then sends 137's
        # media from sequence 1, byte 965: exactly the behaviour of a stream
        # the server believes the client already opened.
        #
        # So a session that still needs the opening does not claim to hold it.
        # The preferred slot below still names the stream, which is how the
        # server knows what to send; only the claim of already having it goes.
        # Once something *is* held — a continuation — the claim is true and is
        # made, because that is what stops the server starting from the top.
        # The claim is "I hold this format's opening", and nothing else.
        #
        # It was first written as "I hold *something*" — `_sequence or
        # _covered` — which is true of every worker in a parallel pass: each is
        # told the whole coverage map so it asks only for its own stretch. So
        # all sixteen claimed to be initialised, none was sent the
        # initialisation segment, and the field log shows the first media
        # header at `start 2,034 sequence 1` with 2,034 bytes missing at byte
        # zero. The single-session path was fixed and the parallel one was not.
        #
        # Holding byte zero is exactly what the claim means, so that is what is
        # asked.
        if self.holds(0, 1):
            request.message(2, self.format.to_message())

        request = (request
                   .raw(5, self.config)
                   .message(preferred_slot, self.format.to_message()))
        # Contexts the server has issued go back with every subsequent
        # request. Appending them works for the captured context too: repeated
        # fields concatenate, so a serialised message with more entries added
        # on the end decodes as the same message with those entries — which is
        # what lets the player's own context be replayed *and* kept current
        # without having to take it apart.
        contexts = b"".join(
            Message().message(
                _STREAMER_SABR_CONTEXT,
                Message().varint(_SENT_CONTEXT_TYPE, kind)
                         .raw(_SENT_CONTEXT_VALUE, value),
            ).to_bytes()
            for kind, value in sorted(self.sabr_contexts.items())
        )
        if self.streamer_context:
            request.raw(19, self.streamer_context + contexts)
        elif contexts:
            request.raw(19, streamer.to_bytes() + contexts)
        else:
            request.message(19, streamer)

        if self._sequence:
            # Telling the server what is already held is what advances it; the
            # player clock alone does not.
            request.message(3, Message()
                            .message(1, self.format.to_message())
                            .varint(2, 0)
                            .varint(3, self._buffered_ms)
                            .varint(4, 1)
                            .varint(5, self._sequence))
        return request.to_bytes()

    def _expected_seconds(self) -> float:
        """Roughly how long this stream should take, for the safety deadline."""
        # A transfer paced by playback takes about the video's duration, which
        # is not known here — the buffered position is the best proxy once the
        # first replies have arrived.
        return max(120.0, self._buffered_ms / 1000.0 * 4)

    #: How much of a reply to take from the socket at a time. Small enough that
    #: progress is recorded smoothly, large enough not to make a system call
    #: per frame.
    _READ_SIZE = 64 << 10

    def _post(self, body: bytes) -> bytes:
        """Send one request and return the whole reply.

        Kept for callers that want the reply in hand — :meth:`probe` asks one
        question and reads one answer. The transfer itself uses
        :meth:`_post_parts`, because holding a reply before writing any of it
        is what made a running download look stalled.
        """
        with self._open(body) as response:
            return response.read_all(256 << 20)

    def _open(self, body: bytes):
        self._request_number += 1
        headers = {
            "Content-Type": "application/x-protobuf",
            "Accept": "*/*",
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        separator = "&" if "?" in self.endpoint else "?"
        url = f"{self.endpoint}{separator}rn={self._request_number}"
        return self.client.post(url, body, headers)

    def _post_parts(self, body: bytes) -> Iterator[tuple[int, bytes]]:
        """Send one request and yield its parts as the socket delivers them."""
        with self._open(body) as response:
            def pieces() -> Iterator[bytes]:
                while True:
                    piece = response.read(self._READ_SIZE)
                    if not piece:
                        return
                    yield piece
            yield from iter_parts_streaming(pieces())

    # ------------------------------------------------------------------
    def _consume(self, parts: "Iterator[tuple[int, bytes]] | bytes",
                 write: Callable[[int, bytes], None]) -> SabrResult:
        """Act on a reply's parts, writing media where each block belongs.

        Takes either a whole reply or an iterator over its parts. The transfer
        passes the iterator, so a block reaches the file — and the progress
        counter — the moment it arrives rather than when the reply ends.
        """
        result = SabrResult()
        headers: dict[int, dict[int, Any]] = {}
        offsets: dict[int, int] = {}

        if isinstance(parts, (bytes, bytearray)):
            parts = iter_parts(bytes(parts))

        for part_type, part in parts:
            if part_type == PART_MEDIA_HEADER:
                header = parse(part)
                itag = header.get(_HEADER_ITAG)
                is_init = bool(header.get(_HEADER_IS_INIT))

                # `_HEADER_IS_INIT` was defined here and never read, and the
                # filter below is why the initialisation segment went missing:
                # a header that does not repeat the itag was dropped, and its
                # body with it, because the body is only placeable through the
                # header that precedes it. The field log of 2026-08-12 has the
                # exact shape — 20,391,931 bytes delivered, 100% of the media,
                # and 965 bytes missing *at byte zero*.
                #
                # A session asks for one stream and names it three times over
                # (selection, preferred slot, track type), so an unlabelled
                # header belongs to that stream: there is nothing else in the
                # reply for it to belong to. A header naming a *different* itag
                # is still refused — that is a real mismatch, not an omission.
                if itag is not None and itag != self.format.itag:
                    # Said once per foreign itag, with a tally at the end of
                    # the session. It was said per header, and one download
                    # put sixty identical lines in the log — which buries the
                    # two lines that matter and is the opposite of what the
                    # logging was added for.
                    self._foreign[itag] = self._foreign.get(itag, 0) + 1
                    if self._foreign[itag] == 1:
                        self._log(f"this session is also being sent itag "
                                  f"{itag}; only {self.format.itag} is kept")
                    continue

                # The initialisation segment always, and the first ordinary
                # header once. Six per session was right while the opening was
                # missing and unbearable once sixteen sessions run at a time —
                # ninety-six lines saying the transfer is working, around the
                # two that say whether it is.
                if is_init or self._described < 1:
                    self._described += 1
                    self._log(
                        f"media header: itag {itag if itag is not None else '—'}"
                        f" start {header.get(_HEADER_START_RANGE, 0):,}"
                        f" sequence {header.get(_HEADER_SEQUENCE, 0)}"
                        f" length {header.get(_HEADER_LENGTH, 0):,}"
                        + (" · initialisation segment" if is_init else "")
                    )
                header_id = header.get(_HEADER_ID, 0)
                headers[header_id] = header
                start_range = header.get(_HEADER_START_RANGE, 0)
                offsets[header_id] = start_range
                sequence = header.get(_HEADER_SEQUENCE, 0)
                result.sequence = max(result.sequence, sequence)
                end_ms = self._advance_clock(header, result)
                # Where each segment begins, what it is numbered and when it
                # ends. Winding the session back is otherwise a guess: an
                # offset has to be turned into a playback position, and the
                # only exact conversion is the one the server itself published
                # for the segment sitting just before the wanted byte.
                if sequence:
                    self._segments[sequence] = (start_range, end_ms)

            elif part_type == PART_MEDIA:
                header_id, cursor = read_varint(part, 0)
                if header_id not in headers:
                    continue
                body = part[cursor:]
                if not body:
                    continue
                start = offsets[header_id]
                # How much of this block is new has to be measured before it is
                # recorded, or the coverage map will already contain it.
                fresh = self._uncovered_within(start, start + len(body))
                write(start, body)
                offsets[header_id] = start + len(body)
                self._end = max(self._end, start + len(body))
                self._cover(start, start + len(body))
                result.bytes_written += len(body)
                result.gained += fresh
                result.ranges.append((start, start + len(body)))

            elif part_type == PART_REDIRECT:
                redirect = parse(part)
                target = redirect.get(1)
                if isinstance(target, bytes):
                    result.redirect = target.decode("utf-8", "replace")

            elif part_type == PART_BUFFER_POLICY:
                cookie = parse(part).get(_POLICY_PLAYBACK_COOKIE)
                if isinstance(cookie, bytes) and cookie:
                    self.playback_cookie = cookie

            elif part_type == PART_PROTECTION_STATUS:
                status = parse(part).get(1)
                result.attested = status == PROTECTION_OK
                result.protection_status = status if isinstance(status, int) else 0

            elif part_type == PART_FORMAT_INIT:
                self._read_format_init(part)

            elif part_type == PART_SABR_CONTEXT_UPDATE:
                self._read_context_update(part)

            elif part_type == PART_SABR_CONTEXT_SENDING_POLICY:
                self._read_context_policy(part)

            elif part_type != PART_MEDIA_END:
                # Nothing here acts on this part. Recorded rather than ignored:
                # every session measured so far begins at sequence 1, byte 965
                # — the initialisation segment is never sent as media — and an
                # unread part is the one remaining place it could be arriving.
                # Named once each, with its size, which is enough to recognise
                # a 965-byte payload for what it is.
                if part_type not in self._unhandled:
                    # Named, and its shape written down. "type 57" read as
                    # noise for as long as it took to match it against a
                    # refusal; a part that says what it is and what it holds
                    # can be recognised from a single field report.
                    try:
                        shape = _readable(parse(part))
                    except Exception:      # noqa: BLE001
                        shape = part[:16].hex(" ")
                    self._log(f"the server sent a part this application does "
                              f"not read: {part_name(part_type)}, "
                              f"{len(part):,} bytes — {shape}")
                self._unhandled[part_type] = self._unhandled.get(part_type, 0) + 1

        return result

    def _read_context_update(self, part: bytes) -> None:
        """Keep a context the server has issued, to hand back on every request.

        From the Windows field log of 2026-08-13: two of these arrived, nothing
        read them, and the request that followed was answered with 31 bytes —
        `sabr.malformed_config` — and no media at all. The server had changed
        the configuration the session runs under and was then sent a request
        that did not reflect it.

        The whole part is kept as well as its parts. The mapping below is read
        off the wire rather than from a specification nobody publishes, so the
        structure of anything unexpected is written to the log: a report that
        still fails will then say what this got wrong, which is the only way it
        can be corrected.
        """
        try:
            fields = parse(part)
        except Exception:      # noqa: BLE001 - a part we cannot read is not fatal
            self._log(f"a {part_name(PART_SABR_CONTEXT_UPDATE)} arrived that "
                      f"could not be decoded ({len(part):,} bytes)")
            return

        kind = fields.get(_CONTEXT_TYPE)
        value = fields.get(_CONTEXT_VALUE)
        if not isinstance(kind, int) or not isinstance(value, bytes):
            self._log(
                f"a {part_name(PART_SABR_CONTEXT_UPDATE)} arrived in a shape "
                f"this does not recognise: {_readable(fields)}"
            )
            return

        first = kind not in self.sabr_contexts
        self.sabr_contexts[kind] = value
        if first:
            self._log(f"the server issued a session context (type {kind}, "
                      f"{len(value):,} bytes); it is sent back on every "
                      f"request from here on")

    def _read_context_policy(self, part: bytes) -> None:
        """Stop sending back a context the server has withdrawn."""
        try:
            fields = parse(part)
        except Exception:      # noqa: BLE001
            return
        stop = fields.get(_POLICY_STOP)
        for kind in (stop if isinstance(stop, list) else [stop]):
            if isinstance(kind, int) and self.sabr_contexts.pop(kind, None) is not None:
                self._log(f"the server withdrew session context type {kind}")

    def _read_format_init(self, part: bytes) -> None:
        """Record what the server says a format's opening bytes are.

        This part carries no media. It names the format and the byte ranges its
        initialisation and index segments occupy — and every session measured
        announces those ranges and then begins at the byte immediately after
        them. That is the whole of the missing-965-bytes problem stated by the
        server itself, so it is read rather than counted.

        Anything unrecognised is logged as it arrived. A wrong reading here
        would place a file's opening at an offset that is merely plausible,
        which is the class of defect this project has been bitten by most.
        """
        try:
            fields = parse(part)
        except Exception:                     # noqa: BLE001 - logged, not fatal
            self._log(f"a format description arrived that could not be read "
                      f"({len(part):,} bytes)")
            return

        def span(raw: Any) -> tuple[int, int] | None:
            if not isinstance(raw, (bytes, bytearray)):
                return None
            try:
                inner = parse(bytes(raw))
            except Exception:                 # noqa: BLE001
                return None
            start, end = inner.get(_RANGE_START), inner.get(_RANGE_END)
            if isinstance(start, int) and isinstance(end, int):
                return start, end
            return None

        itag = None
        raw_format = fields.get(_INIT_FORMAT_ID)
        if isinstance(raw_format, (bytes, bytearray)):
            try:
                itag = parse(bytes(raw_format)).get(1)
            except Exception:                 # noqa: BLE001
                itag = None
        if itag is not None and itag != self.format.itag:
            # Said once. Ten of these arrived per session and every one was
            # skipped here in silence, which read from outside as the part
            # never having been parsed at all — so the description this stream
            # needs looked absent when what was absent is any description *of
            # this stream*.
            if not self._foreign_described:
                self._foreign_described = True
                # The whole description, not just the itag it names. Reading
                # only the itag out of it left three sessions unable to say
                # whether the server withholds this stream's opening or merely
                # never mentions the stream — and whether the fields are being
                # read as the server means them at all.
                self._log(f"the server described itag {itag}, not "
                          f"{self.format.itag}; this stream's own opening is "
                          f"never described. That description reads: "
                          f"{_readable(fields)}")
            return

        initial = span(fields.get(_INIT_INIT_RANGE))
        index = span(fields.get(_INIT_INDEX_RANGE))
        mime = fields.get(_INIT_MIME)
        if isinstance(mime, (bytes, bytearray)):
            mime = bytes(mime).decode("utf-8", "replace")

        if initial:
            self.init_range = initial
        if index:
            self.index_range = index

        if self._described_format:
            return
        self._described_format = True
        if initial or index:
            self._log(
                "the server describes this stream's opening: "
                + (f"initialisation {initial[0]:,}–{initial[1]:,}" if initial
                   else "no initialisation range")
                + (f", index {index[0]:,}–{index[1]:,}" if index else "")
                + (f" ({mime})" if isinstance(mime, str) and mime else "")
                + " — and sends none of it"
            )
        else:
            self._log(f"a format description arrived in a shape this "
                      f"application does not know: fields "
                      f"{sorted(fields)} ({len(part):,} bytes)")

    def _uncovered_within(self, start: int, end: int) -> int:
        """How many bytes of ``start..end`` are not already held."""
        if end <= start:
            return 0
        held = 0
        for span_start, span_end in self._covered:
            overlap = min(end, span_end) - max(start, span_start)
            if overlap > 0:
                held += overlap
        return max(0, (end - start) - held)

    def _cover(self, start: int, end: int) -> None:
        """Record a written range, merging it into the coverage map."""
        if end <= start:
            return
        merged: list[list[int]] = []
        placed = False
        for span in self._covered:
            if span[1] < start:
                merged.append(span)
            elif end < span[0]:
                if not placed:
                    merged.append([start, end])
                    placed = True
                merged.append(span)
            else:                       # overlapping or touching: absorb it
                start = min(start, span[0])
                end = max(end, span[1])
        if not placed:
            merged.append([start, end])
        merged.sort()
        self._covered = merged

    def _seek_to_byte(self, offset: int) -> None:
        """Wind the session back so the server re-sends from ``offset``.

        The server decides what to send from the position the player reports
        and from what the player says it already holds. Both have to move: a
        request that asks for an earlier time while still claiming the later
        sequence is answered with nothing, and the transfer stalls instead of
        filling the hole.
        """
        # The best way back is the one the ordinary case already uses: declare
        # holding everything up to the segment before the wanted byte, and the
        # server sends the next one — which is the wanted one. That needs no
        # conversion and no assumption about how the stream is paced, and it
        # is the only form of request this endpoint is known to advance on.
        # Abandoning the sequence and asking by clock alone is a strictly
        # weaker request, so it is the fallback rather than the method.
        # The published index, when the stream carries one. It is exact, so it
        # outranks both the segments seen so far and the estimate: those are
        # ways of coping without it.
        located = self._index_before(offset)
        if located is not None:
            self._player_time_ms = located
            self._buffered_ms = located
            self._pinned_ms = located
            self._sequence = 0
            self.playback_cookie = b""
            return

        preceding = self._segment_before(offset)
        if preceding is not None:
            sequence, end_ms = preceding
            self._sequence = sequence
            self._player_time_ms = end_ms
            self._buffered_ms = end_ms
            self._pinned_ms = end_ms
            self.playback_cookie = b""
            return

        size = self.format.size or 0
        # The stream's own running time when it is known; otherwise how far
        # playback has been taken, which is all a mid-transfer seek has.
        duration_ms = self.duration_ms or self._buffered_ms or 0
        if size > 0 and duration_ms > 0:
            # Where this byte falls in the running time, which is the only
            # position the protocol understands.
            self._player_time_ms = int(duration_ms * (offset / size))
        else:
            self._player_time_ms = 0
        self._buffered_ms = self._player_time_ms
        self._sequence = 0
        self.playback_cookie = b""
        # A chosen position must reach the wire as chosen. The reported clock
        # otherwise follows what has already arrived, which for a wind-back is
        # a *later* time than the one being asked for — so the seek was undone
        # before the request was built and the gap was never re-sent.
        self._pinned_ms = self._player_time_ms

    def _index_before(self, offset: int) -> int | None:
        """The published start time of the piece containing ``offset``.

        The *start* of that piece, not the end: asking from where it begins is
        what makes the server send the piece the wanted byte is inside. Asking
        from its end asks for the next one, which skips exactly the bytes being
        sought.
        """
        if not self.index:
            return None
        found = None
        for start_byte, start_ms, _duration in self.index:
            if start_byte <= offset:
                found = start_ms
            else:
                break
        return found if found is not None else self.index[0][1]

    def _segment_before(self, offset: int) -> tuple[int, int] | None:
        """``(sequence, end_ms)`` of the last segment ending at or before ``offset``.

        Only segments this session has actually seen described are eligible,
        because only those carry a position the server published itself.
        """
        best: tuple[int, int] | None = None
        best_start = -1
        for sequence, (start, end_ms) in self._segments.items():
            # Strictly before: a segment starting *at* the wanted byte is the
            # one that is missing, and claiming to hold it is how the request
            # would ask the server to skip the very gap it is sent to fill.
            if start < offset and end_ms and start > best_start:
                best_start = start
                best = (sequence, end_ms)
        return best

    def coverage(self) -> list[list[int]]:
        """The byte ranges held so far, in a form that survives storage.

        A transfer that is interrupted has to be able to say what it already
        has: positions here are times, not offsets, so without this a resumed
        session has no way to ask for the remainder and begins again at nothing.
        """
        return [[start, end] for start, end in self._covered]

    def restore(self, ranges: Any, player_ms: int = 0, sequence: int = 0) -> None:
        """Take back a coverage map saved by an earlier attempt.

        ``player_ms`` is the playback position that attempt had reached. It is
        recorded because it is the only *exact* way back: a byte offset has to
        be converted into a time before the server understands it, and that
        conversion needs the stream's running time, which an older transfer may
        never have stored. Without it the conversion yields zero, the session
        opens at the beginning, and every reply re-sends bytes already held —
        so the transfer runs at full speed while its progress, which is the
        furthest byte reached, does not move at all.

        ``sequence`` is the segment index that attempt had reached, and it is
        the part that actually moves the server. What is already held is
        declared in a request field that is only written when a sequence is
        known; a resumed session that has none therefore asks for the stream
        while claiming to hold nothing, and is answered — correctly, from the
        server's point of view — with the file from the beginning.
        """
        self._resume_ms = max(0, int(player_ms or 0))
        self._resume_sequence = max(0, int(sequence or 0))
        for span in ranges or ():
            try:
                start, end = int(span[0]), int(span[1])
            except (TypeError, ValueError, IndexError):
                continue
            if end > start:
                self._cover(start, end)
        if self._covered:
            self._end = max(self._end, self._covered[-1][1])

    def holds(self, start: int, end: int) -> bool:
        """Whether ``start..end`` is already covered in full."""
        return self._uncovered_within(start, end) == 0

    def note_written(self, start: int, end: int) -> None:
        """Record bytes written for this stream by someone else.

        The initialisation and index segments never come down this session —
        the server assumes a player fetched them separately, so the engine
        retrieves them over the ordinary URL. They are still part of the file,
        and coverage that does not know about them reports a permanent gap at
        byte zero, which would leave every transfer waiting for a range that
        has already arrived.
        """
        self._cover(start, end)
        self._end = max(self._end, end)

    def missing(self, size: int) -> list[tuple[int, int]]:
        """Byte ranges of ``size`` that were never written.

        A sparse file reads its unwritten parts back as zeros, so a gap does
        not announce itself — it produces a file that opens, plays, and then
        stops moving. This is what makes such a file detectable before it is
        published rather than by watching it.
        """
        if size <= 0:
            return []
        gaps: list[tuple[int, int]] = []
        cursor = 0
        for start, end in self._covered:
            if start > cursor:
                gaps.append((cursor, min(start, size)))
            cursor = max(cursor, end)
            if cursor >= size:
                break
        if cursor < size:
            gaps.append((cursor, size))
        return [(a, b) for a, b in gaps if b > a]

    def probe(self) -> str:
        """Ask for the first block; return "" if the stream is servable.

        Which streams a streaming endpoint will actually hand over is not
        knowable from the player response: the same session that serves one
        resolution in full refuses a higher one, either by answering
        ``sabr.no_video_selected`` or by refusing the request outright. It is
        also not a fixed property — it varies by video and by where the request
        comes from — so it cannot be predicted and has to be asked.

        One exchange is enough to tell, and it costs a fraction of a second
        against a download that would otherwise fail after being queued.
        """
        try:
            payload = self._post(self._build_request())
        except Exception as exc:                 # noqa: BLE001 - any refusal
            return f"{type(exc).__name__}: {exc}"
        try:
            result = self._consume(payload, lambda offset, data: None)
        except ValueError as exc:
            return f"malformed streaming response: {exc}"
        if result.error:
            return result.error
        if result.bytes_written or self._end:
            return ""
        # A reply carrying no media and naming no fault still means this stream
        # is not on offer; reporting it as available would only move the
        # failure later.
        return "the streaming server returned no media for this stream"

    def _advance_clock(self, header: dict[int, Any], result: SabrResult) -> int:
        """Move the player clock to the end of the segment just described.

        Returns that segment's end position in milliseconds, or 0 when the
        header carried no time range to derive one from.
        """
        raw = header.get(_HEADER_TIME_RANGE)
        if not isinstance(raw, bytes):
            return 0
        time_range = parse(raw)
        scale = time_range.get(3) or 1000
        if not scale:
            return 0
        end_ms = int((time_range.get(1, 0) + time_range.get(2, 0)) * 1000 / scale)
        result.player_time_ms = max(result.player_time_ms, end_ms)
        return end_ms

    # ------------------------------------------------------------------
    def download(self, write: Callable[[int, bytes], None],
                 should_stop: Callable[[], bool] | None = None,
                 on_progress: Callable[[int], None] | None = None) -> int:
        """Pull the whole stream, writing each block at its byte offset.

        The reported position is elapsed time since the session opened, which
        is what a player would send. It does not extend the session: the server
        stops at a fixed amount of media per session regardless — measured by
        letting a session idle for several minutes, during which it never
        resumed — so an empty reply means the session is spent, not that
        waiting would help.
        """
        empty_replies = 0
        refills = 0
        stalled = 0
        started = time.monotonic()

        # Resuming: whatever is already on disk was restored into the coverage
        # map before this ran, so the session opens at the first byte still
        # wanted rather than at the beginning. Starting from zero would spend
        # the whole allowance re-fetching bytes already held and stop before
        # reaching the part that is actually missing — which is why an
        # interrupted transfer could not be resumed at all.
        if self.format.size and self._covered:
            outstanding = self.missing(self.format.size)
            if not outstanding:
                # Nothing left to ask for. A continuation whose only gap was
                # the header — which is fetched over an ordinary link before
                # this runs — is finished the moment it starts, and opening a
                # session to be told so is a request for nothing.
                return self._end
            if self._resume_ms and self._resume_sequence:
                # The exact position the interrupted attempt had reached,
                # declared the way a player declares it: the clock *and* the
                # segments already held. Sending the clock on its own left the
                # request indistinguishable from a first one, and the server
                # answered it the same way — from byte zero.
                self._player_time_ms = self._resume_ms
                self._buffered_ms = self._resume_ms
                self._sequence = self._resume_sequence
                self._pinned_ms = self._resume_ms
                self.playback_cookie = b""
            else:
                # No usable record of where the last attempt stopped, so the
                # position is worked out from the first missing byte.
                self._seek_to_byte(outstanding[0][0])
        # Give up only well past the point where a healthy transfer would have
        # finished, so a genuinely dead session cannot hang the download.
        deadline = started + max(600.0, self._expected_seconds() * 3)

        while self._request_number < _MAX_REQUESTS:
            if should_stop is not None and should_stop():
                # Being asked to stop is not the server stopping. Falling out
                # of the loop here reached the checks below and reported a
                # pause as “the streaming server stopped after N of M bytes and
                # would not continue” — blaming the origin for the user's own
                # click, and, when the pause landed before this session had
                # gained anything, failing the download outright instead of
                # pausing it.
                raise CancelledError("stopped")
            if time.monotonic() > deadline:
                break

            # The position to report is how far the media itself has been
            # taken, not how long the transfer has been running. The server
            # sends up to a fixed amount beyond where the player says it is, so
            # a position pinned to the wall clock asks for the next second of
            # video once a second — which is why a session appeared to stop
            # after a minute. Reporting the end of what has already arrived
            # keeps that window ahead of the transfer at full speed.
            #
            # A position that was *chosen* — a resume, or a wind-back to fill a
            # gap — outranks that, and only for the one request it was chosen
            # for. Taking the later of the two undid every backward seek the
            # moment the transfer had been running longer than the seek target,
            # which is most of them: the gap was asked for and the request went
            # out pointing past it.
            if self._pinned_ms is not None:
                self._player_time_ms = self._pinned_ms
                self._pinned_ms = None
            else:
                self._player_time_ms = self._buffered_ms
            try:
                result = self._consume(
                    self._post_parts(self._build_request()), write)
            except ValueError as exc:
                raise ExtractionError(f"malformed streaming response: {exc}") from exc

            if result.error:
                raise ExtractionError(
                    f"the streaming server refused to continue: {result.error}"
                )

            if result.redirect:
                self.endpoint = result.redirect
                continue

            self._written += result.bytes_written
            self._resent += result.bytes_written - result.gained

            # Bytes arriving is not the same as progress being made. A session
            # that opens in the wrong place re-sends what is already held, and
            # every one of those replies is full of data while the furthest
            # byte reached — which is what progress means here — does not move.
            # That is a transfer running at full speed and getting nowhere, and
            # it has to be recognised rather than left to run out its
            # allowance behind a progress bar that never changes.
            if result.bytes_written and not result.gained:
                stalled += 1
                if stalled >= _STALLED_LIMIT:
                    # The session is being answered from the wrong place. Where
                    # it was *asked* to start is expressed in playback time, and
                    # the server is entitled to disregard it; the first byte
                    # still wanted is not a matter of opinion, so the position
                    # is worked out from that instead and the session wound
                    # back to it. Failing here outright was what turned every
                    # such session into a dead transfer — and a resume that
                    # opens in the wrong place is exactly the case this has to
                    # recover from, not the case it should give up on.
                    outstanding = (self.missing(self.format.size)
                                   if self.format.size else [])
                    if outstanding and self._byte_seeks < _MAX_REFILLS:
                        self._byte_seeks += 1
                        stalled = 0
                        self._seek_to_byte(outstanding[0][0])
                        continue
                    still = (sum(b - a for a, b in
                                 self.missing(self.format.size))
                             if self.format.size else 0)
                    raise ExtractionError(
                        f"the streaming server re-sent {self._resent:,} bytes "
                        f"that were already held and none of the {still:,} "
                        f"still missing, across {self._byte_seeks + 1} "
                        "attempt(s) to start it at the first byte wanted. The "
                        "session could not be made to open anywhere but where "
                        "it chose to."
                    )
            elif result.gained:
                stalled = 0

            if on_progress is not None and result.bytes_written:
                on_progress(result.bytes_written)

            if result.protection_status:
                self._protection = result.protection_status
            if result.sequence:
                self._sequence = max(self._sequence, result.sequence)
            if result.player_time_ms:
                self._buffered_ms = max(self._buffered_ms, result.player_time_ms)

            # Reaching the end is not the same as having all of it. The server
            # can skip a stretch and then deliver a block at the very end,
            # which takes the highest offset to the file size while a hole
            # remains in the middle — the file then plays for a few seconds,
            # freezes, and goes on producing sound.
            #
            # A hole is asked for again rather than being treated as failure:
            # the session is wound back to where it starts and the server
            # re-sends from there. The number of attempts is bounded, because
            # a gap the server will not fill must end the transfer rather than
            # keep it running to its deadline.
            if self.format.size and self._end >= self.format.size:
                gaps = self.missing(self.format.size)
                if not gaps or refills >= _MAX_REFILLS:
                    break
                refills += 1
                start = gaps[0][0]
                self._seek_to_byte(start)
                continue

            if result.bytes_written == 0:
                empty_replies += 1
                if empty_replies >= _EMPTY_LIMIT:
                    break
            else:
                empty_replies = 0

        if self._foreign:
            self._log("this session was also sent " + ", ".join(
                f"itag {itag} ×{count}"
                for itag, count in sorted(self._foreign.items())))
        if self._unhandled:
            self._log("parts this application does not read: " + ", ".join(
                f"{part_name(kind)} ×{count}"
                for kind, count in sorted(self._unhandled.items())))

        gaps = self.missing(self.format.size) if self.format.size else []
        if gaps and self._end >= self.format.size:
            # Everything up to the end arrived, but not all of it. Publishing
            # this would produce exactly the corrupt file described above, so
            # it is refused rather than handed over as finished.
            held = sum(b - a for a, b in gaps)
            first = gaps[0]
            raise ExtractionError(
                f"the streaming server left {held:,} bytes unsent across "
                f"{len(gaps)} gap(s) — the first at byte {first[0]:,} — so the "
                "file would have played for a few seconds and then frozen "
                "while its sound continued. It was not kept."
            )

        if self.format.size and self._end < self.format.size:
            share = self._end / self.format.size
            if self._protection == PROTECTION_REQUIRED and not self.po_token:
                raise ExtractionError(
                    f"the streaming server sent {self._end:,} of "
                    f"{self.format.size:,} bytes ({share:.0%}) and then asked "
                    "this session to prove it came from a real player. About a "
                    "minute of any stream is served without that proof; past "
                    "it the server answers with an attestation demand and no "
                    "media. The proof is a token the browser mints per page — "
                    "it is not a sign-in — so playing the video once in a "
                    "browser with the extension installed supplies it, or it "
                    "can be pasted into Settings as the YouTube token."
                )
            raise ExtractionError(
                f"the streaming server stopped after {self._end:,} of "
                f"{self.format.size:,} bytes ({share:.0%}) and would not "
                "continue."
            )
        return self._end


def stream_from_context(client: Any, context: dict, user_agent: str = "") -> SabrStream:
    """Build a stream from the context a :class:`MediaFormat` carries.

    The context travels as JSON — through the database and the control socket —
    so the binary parts are text by the time they arrive and have to be decoded
    back before they can be put on the wire.
    """
    config_blob = base64.b64decode(context.get("config") or "")
    token = str(context.get("po_token") or "")
    po_token = None
    if token:
        try:
            po_token = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (ValueError, TypeError):
            po_token = None
    media_format = SabrFormat(
        itag=int(context.get("itag") or 0),
        last_modified=int(context.get("last_modified") or 0),
        size=int(context.get("size") or 0),
        is_audio=bool(context.get("is_audio")),
        xtags=str(context.get("xtags") or ""),
    )
    streamer = str(context.get("streamer_context") or "")
    streamer_context = None
    if streamer:
        try:
            streamer_context = base64.b64decode(streamer)
        except (ValueError, TypeError):
            streamer_context = None

    # The running time travels in the context and was being dropped here, which
    # left every stream built this way unable to convert a byte offset into the
    # playback position the protocol is addressed in — the one conversion a
    # wind-back depends on.
    try:
        duration_ms = int(float(context.get("duration") or 0) * 1000)
    except (TypeError, ValueError):
        duration_ms = 0

    return SabrStream(
        client, str(context.get("endpoint") or ""), config_blob, media_format,
        user_agent=user_agent,
        client_id=int(context.get("client_id") or 5),
        po_token=po_token,
        streamer_context=streamer_context,
        duration_ms=duration_ms,
    )


def format_from_entry(entry: dict) -> SabrFormat:
    """Build a :class:`SabrFormat` from one ``adaptiveFormats`` entry."""
    mime = entry.get("mimeType", "") or ""
    try:
        size = int(entry.get("contentLength") or 0)
    except (TypeError, ValueError):
        size = 0
    return SabrFormat(
        itag=int(entry.get("itag") or 0),
        last_modified=int(entry.get("lastModified") or 0),
        size=size,
        is_audio=mime.startswith("audio/"),
        xtags=entry.get("xtags") or "",
    )
