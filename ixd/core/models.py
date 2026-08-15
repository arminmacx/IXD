"""Domain models shared by the engine, the persistence layer and the UI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DownloadStatus(str, Enum):
    """Lifecycle of a single download."""

    QUEUED = "queued"            # waiting for a free concurrency slot
    SCHEDULED = "scheduled"      # held by a queue schedule window
    CONNECTING = "connecting"    # probing headers / resolving media
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    ASSEMBLING = "assembling"    # concatenating segments
    VERIFYING = "verifying"      # hashing the finished file
    COMPLETED = "completed"
    ERROR = "error"
    NEEDS_LINK = "needs_link"    # source expired, waiting for a refreshed URL
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {DownloadStatus.COMPLETED, DownloadStatus.CANCELLED}

    @property
    def is_active(self) -> bool:
        return self in {
            DownloadStatus.CONNECTING,
            DownloadStatus.DOWNLOADING,
            DownloadStatus.ASSEMBLING,
            DownloadStatus.VERIFYING,
        }

    @property
    def is_startable(self) -> bool:
        return self in {
            DownloadStatus.QUEUED,
            DownloadStatus.PAUSED,
            DownloadStatus.ERROR,
            DownloadStatus.SCHEDULED,
        }


class ChunkStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"


class HashStatus(str, Enum):
    UNKNOWN = "unknown"
    PENDING = "pending"
    VERIFIED = "verified"
    CORRUPTED = "corrupted"
    NO_REFERENCE = "no_reference"   # nothing to compare against; hash computed only


class TransferMode(str, Enum):
    RANGED = "ranged"        # one URL + HTTP Range requests
    SEGMENTED = "segmented"  # HLS/DASH playlist of segments
    SINGLE = "single"        # server refuses ranges; one linear stream
    SABR = "sabr"            # server-driven: the origin decides what to send


class QueueMode(str, Enum):
    SEQUENTIAL = "sequential"  # one download at a time inside the queue
    CONCURRENT = "concurrent"  # up to max_concurrent at a time


class ScheduleAction(str, Enum):
    START = "start"
    PAUSE = "pause"
    STOP = "stop"
    NOTHING = "nothing"


class ProxyScheme(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"
    SOCKS5H = "socks5h"   # hostname resolved by the proxy


@dataclass(slots=True)
class Chunk:
    """A contiguous byte range of the target file owned by one worker."""

    index: int
    start: int
    end: int                      # inclusive; -1 when the length is unknown
    downloaded: int = 0
    status: ChunkStatus = ChunkStatus.PENDING
    id: int | None = None
    download_id: int | None = None
    speed: float = 0.0            # runtime only, bytes/sec

    @property
    def size(self) -> int:
        if self.end < 0:
            return -1
        return self.end - self.start + 1

    @property
    def remaining(self) -> int:
        if self.end < 0:
            return -1
        return max(0, self.size - self.downloaded)

    @property
    def cursor(self) -> int:
        """Absolute file offset the next byte should be written to."""
        return self.start + self.downloaded

    @property
    def progress(self) -> float:
        if self.size <= 0:
            return 0.0
        return min(1.0, self.downloaded / self.size)

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "download_id": self.download_id,
            "idx": self.index,
            "start_byte": self.start,
            "end_byte": self.end,
            "downloaded": self.downloaded,
            "status": self.status.value,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Chunk":
        return cls(
            index=row["idx"],
            start=row["start_byte"],
            end=row["end_byte"],
            downloaded=row["downloaded"],
            status=ChunkStatus(row["status"]),
            id=row["id"],
            download_id=row["download_id"],
        )


@dataclass(slots=True)
class MediaSegment:
    """One HLS/DASH segment, optionally AES-128 encrypted."""

    index: int
    url: str
    duration: float = 0.0
    byte_range: tuple[int, int] | None = None   # (start, end) inclusive
    key_url: str | None = None
    key_iv: str | None = None                   # hex string
    init: bool = False                          # DASH/fMP4 initialisation segment

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "url": self.url,
            "duration": self.duration,
            "byte_range": list(self.byte_range) if self.byte_range else None,
            "key_url": self.key_url,
            "key_iv": self.key_iv,
            "init": self.init,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaSegment":
        byte_range = data.get("byte_range")
        return cls(
            index=int(data["index"]),
            url=data["url"],
            duration=float(data.get("duration") or 0.0),
            byte_range=(int(byte_range[0]), int(byte_range[1])) if byte_range else None,
            key_url=data.get("key_url"),
            key_iv=data.get("key_iv"),
            init=bool(data.get("init")),
        )


@dataclass(slots=True)
class MediaFormat:
    """A selectable stream produced by an extractor."""

    format_id: str
    url: str
    ext: str = "mp4"
    protocol: str = "https"        # https | m3u8 | dash
    width: int = 0
    height: int = 0
    fps: float = 0.0
    tbr: float = 0.0               # total bitrate, kbit/s
    vcodec: str = "none"
    acodec: str = "none"
    filesize: int = 0
    quality_label: str = ""
    note: str = ""
    manifest_url: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)
    #: Pre-resolved segment list (DASH); HLS resolves lazily via its playlist.
    segments: list["MediaSegment"] = field(default_factory=list)
    #: The origin will serve only an opening portion of this URL.
    #:
    #: Some CDNs issue links that answer a large range at offset 0 but refuse
    #: any range past a fixed point, and reissuing the link changes nothing.
    #: Such a stream cannot produce a complete file, so it must never outrank
    #: a lower-quality one that can.
    restricted: bool = False
    #: Everything a server-driven transfer needs: the streaming endpoint, the
    #: session configuration and this stream's identity. Empty for ordinary
    #: formats, which are fetched from ``url`` directly.
    sabr: dict[str, Any] = field(default_factory=dict)
    #: How to ask the site for this same stream again, starting from a given
    #: position. Signed media links are short-lived and are issued covering
    #: only part of a long file, so the transfer has to be able to re-obtain
    #: one rather than give up when the grant runs out.
    refresh: dict[str, Any] = field(default_factory=dict)
    #: Whether this is the track the video was published with.
    #:
    #: A video may carry several audio tracks — the original plus machine
    #: dubbings in other languages — and they are otherwise indistinguishable:
    #: same container, same codec, comparable bitrates. Choosing between them
    #: on bitrate alone means the language of the finished file is decided by
    #: chance, and a viewer discovers it only on playback.
    audio_is_default: bool = True
    #: The track's language tag, when the site states one ("en", "de", …).
    audio_language: str = ""
    #: What the track is: "original", "dubbed", or empty when unstated.
    audio_kind: str = ""
    #: The site's own name for this track ("en-US.4"), when it states one.
    audio_track_id: str = ""
    #: A processing variant of a track rather than the track itself: a
    #: loudness-compressed mix ("drc") or a volume-boosted one ("vb"). Same
    #: language, same content, but not the mix the site plays by default — so
    #: it must never win a tie-break on bitrate against the plain one.
    audio_variant: str = ""
    #: The site's raw track tags, kept verbatim.
    #:
    #: This is the only thing that tells two entries apart when they share an
    #: itag, a codec and a bitrate — which every audio track of a dubbed video
    #: does — so it is the identity, not a description. It has to survive
    #: unparsed because it is also what a later request names the track with.
    audio_tags: str = ""

    @property
    def audio_track_key(self) -> str:
        """What distinguishes this audio track from the others of its stream.

        Empty for media that publishes one track, which is most of it — so
        anything keyed on this behaves exactly as it did before for such media.
        """
        return self.audio_tags or self.audio_track_id or self.audio_language

    @property
    def audio_description(self) -> str:
        """How to name this track's audio to a person, or "" for the usual one."""
        if (self.audio_is_default and self.audio_kind in ("", "original")
                and not self.audio_variant):
            return ""
        bits = [self.audio_language.upper()] if self.audio_language else []
        if self.audio_kind == "dubbed":
            bits.append("dubbed")
        if self.audio_variant == "drc":
            bits.append("compressed")
        elif self.audio_variant == "vb":
            bits.append("boosted")
        elif self.audio_variant:
            bits.append(self.audio_variant)
        return " ".join(bits)

    @property
    def has_video(self) -> bool:
        return self.vcodec not in ("", "none")

    @property
    def has_audio(self) -> bool:
        return self.acodec not in ("", "none")

    @property
    def is_progressive(self) -> bool:
        return self.has_video and self.has_audio

    def describe(self) -> str:
        bits = []
        if self.quality_label:
            bits.append(self.quality_label)
        elif self.height:
            bits.append(f"{self.height}p")
        if self.fps and self.fps >= 50:
            bits.append(f"{self.fps:g}fps")
        if not self.has_video and self.has_audio:
            bits.append("audio only")
        elif not self.has_audio and self.has_video:
            bits.append("video only")
        bits.append(self.ext)
        if self.tbr:
            bits.append(f"{self.tbr:.0f}k")
        if self.note:
            bits.append(self.note)
        return " · ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_id": self.format_id, "url": self.url, "ext": self.ext,
            "protocol": self.protocol, "width": self.width, "height": self.height,
            "fps": self.fps, "tbr": self.tbr, "vcodec": self.vcodec,
            "acodec": self.acodec, "filesize": self.filesize,
            "quality_label": self.quality_label, "note": self.note,
            "manifest_url": self.manifest_url, "http_headers": dict(self.http_headers),
            "segments": [s.to_dict() for s in self.segments],
            "restricted": self.restricted,
            "sabr": bool(self.sabr),
            "refresh": bool(self.refresh),
            # The track travels too. Without it every audio entry crossing the
            # control socket arrives looking like the default one, so the panel
            # cannot say which language it is offering and a round trip through
            # a dict silently promotes a dubbing to the original.
            "audio_is_default": self.audio_is_default,
            "audio_language": self.audio_language,
            "audio_kind": self.audio_kind,
            "audio_track_id": self.audio_track_id,
            "audio_variant": self.audio_variant,
            "audio_tags": self.audio_tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaFormat":
        return cls(
            format_id=str(data.get("format_id", "")),
            url=data.get("url", ""),
            ext=data.get("ext", "mp4"),
            protocol=data.get("protocol", "https"),
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            fps=float(data.get("fps") or 0.0),
            tbr=float(data.get("tbr") or 0.0),
            vcodec=data.get("vcodec", "none"),
            acodec=data.get("acodec", "none"),
            filesize=int(data.get("filesize") or 0),
            quality_label=data.get("quality_label", ""),
            note=data.get("note", ""),
            manifest_url=data.get("manifest_url", ""),
            http_headers=dict(data.get("http_headers") or {}),
            segments=[MediaSegment.from_dict(s) for s in (data.get("segments") or [])],
            restricted=bool(data.get("restricted")),
            audio_is_default=bool(data.get("audio_is_default", True)),
            audio_language=str(data.get("audio_language") or ""),
            audio_kind=str(data.get("audio_kind") or ""),
            audio_track_id=str(data.get("audio_track_id") or ""),
            audio_variant=str(data.get("audio_variant") or ""),
            audio_tags=str(data.get("audio_tags") or ""),
        )


@dataclass(slots=True)
class MediaInfo:
    """Result of an extraction: a title plus every stream we can offer."""

    title: str
    formats: list[MediaFormat]
    webpage_url: str = ""
    thumbnail: str = ""
    duration: float = 0.0
    extractor: str = ""
    http_headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ProxyEntry:
    id: int | None = None
    label: str = ""
    scheme: ProxyScheme = ProxyScheme.HTTP
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    enabled: bool = True
    order_index: int = 0
    fail_count: int = 0
    last_error: str = ""

    def as_url(self) -> str:
        auth = ""
        if self.username:
            auth = f"{self.username}:{'*' * len(self.password)}@"
        return f"{self.scheme.value}://{auth}{self.host}:{self.port}"

    @classmethod
    def from_row(cls, row: Any) -> "ProxyEntry":
        return cls(
            id=row["id"], label=row["label"], scheme=ProxyScheme(row["scheme"]),
            host=row["host"], port=row["port"], username=row["username"] or "",
            password=row["password"] or "", enabled=bool(row["enabled"]),
            order_index=row["order_index"], fail_count=row["fail_count"],
            last_error=row["last_error"] or "",
        )


@dataclass(slots=True)
class DownloadQueue:
    id: int | None = None
    name: str = ""
    mode: QueueMode = QueueMode.SEQUENTIAL
    max_concurrent: int = 1
    enabled: bool = True
    order_index: int = 0
    speed_limit: int = 0
    network_interface: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "DownloadQueue":
        return cls(
            id=row["id"], name=row["name"], mode=QueueMode(row["mode"]),
            max_concurrent=row["max_concurrent"], enabled=bool(row["enabled"]),
            order_index=row["order_index"], speed_limit=row["speed_limit"],
            network_interface=row["network_interface"] or "",
        )


@dataclass(slots=True)
class Schedule:
    """A recurring time window that drives a queue and/or the speed cap."""

    id: int | None = None
    name: str = ""
    queue_id: int | None = None
    days_mask: int = 0b1111111        # bit 0 = Monday .. bit 6 = Sunday
    start_time: str = "02:00"
    end_time: str = "06:00"
    action_start: ScheduleAction = ScheduleAction.START
    action_end: ScheduleAction = ScheduleAction.PAUSE
    speed_limit: int = 0              # bytes/sec enforced while inside the window
    enabled: bool = True

    def covers_day(self, weekday: int) -> bool:
        return bool(self.days_mask & (1 << weekday))

    @classmethod
    def from_row(cls, row: Any) -> "Schedule":
        return cls(
            id=row["id"], name=row["name"], queue_id=row["queue_id"],
            days_mask=row["days_mask"], start_time=row["start_time"],
            end_time=row["end_time"], action_start=ScheduleAction(row["action_start"]),
            action_end=ScheduleAction(row["action_end"]), speed_limit=row["speed_limit"],
            enabled=bool(row["enabled"]),
        )


@dataclass(slots=True)
class Download:
    """The full persisted state of one transfer."""

    id: int | None = None
    url: str = ""
    original_url: str = ""
    filename: str = ""
    #: True while ``filename`` is only a guess taken from the URL. A guess is
    #: replaced the moment the origin publishes a real one — a name the user
    #: typed, or one the server sent in `Content-Disposition`, never is.
    auto_named: bool = False
    dest_dir: str = ""
    temp_path: str = ""
    total_size: int = 0                 # 0 = unknown
    downloaded: int = 0
    status: DownloadStatus = DownloadStatus.QUEUED
    mode: TransferMode = TransferMode.RANGED
    category: str = "Other"
    queue_id: int | None = None
    priority: int = 0
    #: How many workers this download may run. ``0`` means "not specified" —
    #: use the global setting. It used to default to 8, which is not a default
    #: but an answer: every download claimed to have been configured for eight
    #: connections, so the global setting could never apply to one.
    connections: int = 0
    supports_ranges: bool = False
    etag: str = ""
    last_modified: str = ""
    mime: str = ""
    referer: str = ""
    user_agent: str = ""
    cookies: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    # integrity
    expected_hash: str = ""
    expected_hash_algo: str = "sha256"
    computed_hash: str = ""
    hash_status: HashStatus = HashStatus.UNKNOWN
    server_digest: str = ""             # raw Content-MD5 / Digest header
    # media
    segments: list[MediaSegment] = field(default_factory=list)
    media_title: str = ""
    format_id: str = ""
    #: Opaque per-session state for a server-driven transfer (TransferMode.SABR):
    #: the streaming endpoint, the session configuration and the stream's
    #: identity. Stored as JSON so a SABR download survives a restart like any
    #: other, and empty for every ordinary transfer.
    sabr_context: dict[str, Any] = field(default_factory=dict)
    #: Pairs a video-only download with its audio track, as ``"<token>:<role>"``.
    #: Adaptive streams publish the two separately; once both have arrived they
    #: are combined into one playable file, so the pairing has to outlive a
    #: restart just as the transfers do.
    mux_group: str = ""
    # routing
    proxy_id: int | None = None
    network_interface: str = ""
    speed_limit: int = 0
    # bookkeeping
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    # runtime-only (never persisted)
    chunks: list[Chunk] = field(default_factory=list)
    speed: float = 0.0
    eta: float = 0.0
    stage: str = ""
    """What is happening while the status reads "assembling".

    Concatenating segments, joining a video to its audio and rewrapping a
    transport stream are three different waits, and from outside they were the
    same word. Not a column: it means nothing once the work has finished.
    """
    #: How many streaming sessions are running on this download right now.
    #:
    #: Non-zero only during a parallel server-driven pass, and it is what tells
    #: the connection bars that ``chunks`` holds one entry per session rather
    #: than one per track — the two look alike and mean opposite things.
    live_workers: int = 0

    @property
    def filepath(self) -> str:
        import os
        return os.path.join(self.dest_dir, self.filename) if self.dest_dir else self.filename

    @property
    def progress(self) -> float:
        if self.total_size <= 0:
            return 0.0
        return min(1.0, self.downloaded / self.total_size)

    @property
    def remaining(self) -> int:
        if self.total_size <= 0:
            return -1
        return max(0, self.total_size - self.downloaded)

    @property
    def can_resume(self) -> bool:
        """Whether stopping now and continuing later keeps what has arrived.

        Not the same question as ``supports_ranges``, which is only "does this
        one URL answer a Range request". Three of the four transfer modes keep
        their progress by another route entirely, and reporting the flag as
        though it were resume capability told a user their YouTube download
        would start again from zero — which it does not.

        * ``RANGED`` — the chunk map plus Range requests, the ordinary case.
        * ``SEGMENTED`` — every finished segment is a file on disk;
          ``_reconcile_segment_bands`` counts them and fetches only the rest.
        * ``SABR`` — a session is short-lived by design and the transfer
          already opens as many as it takes, each continuing from the position
          written into ``sabr_context``. A pause is one more of those.
        * ``SINGLE`` — the server refuses ranges and there is nothing to
          continue from. This is the only mode that truly starts again.
        """
        if self.mode in (TransferMode.SEGMENTED, TransferMode.SABR):
            return True
        return bool(self.supports_ranges)

    @property
    def resume_note(self) -> str:
        """How resuming works here, in the words the user needs.

        "Yes" alone is not enough for the modes that do not use ranges: the
        answer differs in *what* is kept, and the address a stream was signed
        with can expire even where the bytes survive.
        """
        if self.mode is TransferMode.SEGMENTED:
            return "Yes — finished segments are kept"
        if self.mode is TransferMode.SABR:
            return "Yes — the session reopens where it stopped"
        if self.supports_ranges:
            return "Yes"
        return "No — this server refuses ranges"

    @property
    def media_context(self) -> dict[str, Any]:
        """Everything the engine needs to re-obtain this stream.

        Stored in one place so a transfer can both drive a server-driven
        exchange and ask for a fresh link when a signed one runs out.
        """
        return self.sabr_context

    def to_public_dict(self) -> dict[str, Any]:
        """Serialisable snapshot used by the IPC layer and the browser extension."""
        return {
            "id": self.id,
            "url": self.url,
            "filename": self.filename,
            "dest_dir": self.dest_dir,
            "filepath": self.filepath,
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "progress": self.progress,
            "status": self.status.value,
            "mode": self.mode.value,
            "category": self.category,
            "queue_id": self.queue_id,
            "speed": self.speed,
            "stage": self.stage,
            "eta": self.eta,
            "connections": self.connections,
            "supports_ranges": self.supports_ranges,
            "can_resume": self.can_resume,
            "resume_note": self.resume_note,
            "error": self.error,
            "hash_status": self.hash_status.value,
            "computed_hash": self.computed_hash,
            "expected_hash": self.expected_hash,
            "expected_hash_algo": self.expected_hash_algo,
            "media_title": self.media_title,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "chunks": [
                {"index": c.index, "progress": c.progress, "downloaded": c.downloaded,
                 "size": c.size, "status": c.status.value, "speed": c.speed}
                for c in self.display_chunks
            ],
        }

    @property
    def display_chunks(self) -> list["Chunk"]:
        """The chunks worth showing as connections.

        These bars exist to show a file being fetched over several connections
        at once. A paired quality is not that: its two chunks are two whole
        tracks of one file, fetched in turn, and drawing them side by side
        reads as two downloads — which is the very thing this download exists
        to stop looking like. They are shown as the single transfer they are.
        """
        if self.live_workers > 1:
            # One entry per streaming session, which is exactly what these bars
            # are for: several connections working on one file at once.
            return list(self.chunks)
        if self.mode is TransferMode.SABR and len(self.chunks) > 1:
            held = sum(c.downloaded for c in self.chunks)
            total = sum(c.size for c in self.chunks if c.size > 0)
            # The state of the whole transfer, not of its first track.
            #
            # This used to fall back to ``chunks[0]`` — the video — which is
            # finished long before the download is: it is the larger track by
            # far and it is fetched first, so on a 208 MB pair it reached DONE
            # at 95%. The bar turned "complete" green while the audio was
            # still arriving and the row above it still read 95% in blue. One
            # of them had to be wrong, and it was this one.
            statuses = [c.status for c in self.chunks]
            if all(status is ChunkStatus.DONE for status in statuses):
                status = ChunkStatus.DONE
            elif any(status is ChunkStatus.ACTIVE for status in statuses):
                status = ChunkStatus.ACTIVE
            elif any(status is ChunkStatus.FAILED for status in statuses):
                status = ChunkStatus.FAILED
            else:
                status = ChunkStatus.PENDING
            combined = Chunk(
                index=0, start=0, end=(total - 1) if total else -1,
                downloaded=held,
                status=status,
            )
            combined.speed = sum(c.speed for c in self.chunks)
            return [combined]
        return list(self.chunks)
