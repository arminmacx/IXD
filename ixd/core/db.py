"""SQLite persistence: downloads, chunk progress, queues, schedules, proxies.

The engine flushes chunk cursors here roughly once a second, which is what
makes crash recovery work — on restart every partially finished chunk knows
exactly which byte it stopped at.

Connections are thread-local (SQLite objects are not shareable across threads)
and every write goes through a process-wide lock so that the many worker
threads never collide on the writer.  WAL keeps readers lock-free.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import (
    Chunk,
    ChunkStatus,
    Download,
    DownloadQueue,
    DownloadStatus,
    HashStatus,
    MediaSegment,
    ProxyEntry,
    ProxyScheme,
    QueueMode,
    Schedule,
    ScheduleAction,
    TransferMode,
)

SCHEMA_VERSION = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    url                TEXT    NOT NULL,
    original_url       TEXT    NOT NULL DEFAULT '',
    filename           TEXT    NOT NULL DEFAULT '',
    -- Whether that filename is a guess of ours rather than a name the user
    -- typed or the server published. A guess may be replaced once the origin
    -- says what the file is really called; a choice may not.
    auto_named         INTEGER NOT NULL DEFAULT 0,
    dest_dir           TEXT    NOT NULL DEFAULT '',
    temp_path          TEXT    NOT NULL DEFAULT '',
    total_size         INTEGER NOT NULL DEFAULT 0,
    downloaded         INTEGER NOT NULL DEFAULT 0,
    status             TEXT    NOT NULL DEFAULT 'queued',
    mode               TEXT    NOT NULL DEFAULT 'ranged',
    category           TEXT    NOT NULL DEFAULT 'Other',
    queue_id           INTEGER REFERENCES queues(id) ON DELETE SET NULL,
    priority           INTEGER NOT NULL DEFAULT 0,
    connections        INTEGER NOT NULL DEFAULT 8,
    supports_ranges    INTEGER NOT NULL DEFAULT 0,
    etag               TEXT    NOT NULL DEFAULT '',
    last_modified      TEXT    NOT NULL DEFAULT '',
    mime               TEXT    NOT NULL DEFAULT '',
    referer            TEXT    NOT NULL DEFAULT '',
    user_agent         TEXT    NOT NULL DEFAULT '',
    cookies            TEXT    NOT NULL DEFAULT '',
    extra_headers      TEXT    NOT NULL DEFAULT '{}',
    expected_hash      TEXT    NOT NULL DEFAULT '',
    expected_hash_algo TEXT    NOT NULL DEFAULT 'sha256',
    computed_hash      TEXT    NOT NULL DEFAULT '',
    hash_status        TEXT    NOT NULL DEFAULT 'unknown',
    server_digest      TEXT    NOT NULL DEFAULT '',
    segments           TEXT    NOT NULL DEFAULT '[]',
    sabr_context       TEXT    NOT NULL DEFAULT '{}',
    mux_group          TEXT    NOT NULL DEFAULT '',
    media_title        TEXT    NOT NULL DEFAULT '',
    format_id          TEXT    NOT NULL DEFAULT '',
    proxy_id           INTEGER,
    network_interface  TEXT    NOT NULL DEFAULT '',
    speed_limit        INTEGER NOT NULL DEFAULT 0,
    error              TEXT    NOT NULL DEFAULT '',
    created_at         REAL    NOT NULL DEFAULT 0,
    started_at         REAL    NOT NULL DEFAULT 0,
    completed_at       REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
    idx         INTEGER NOT NULL,
    start_byte  INTEGER NOT NULL,
    end_byte    INTEGER NOT NULL,
    downloaded  INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'pending',
    UNIQUE(download_id, idx)
);

CREATE TABLE IF NOT EXISTS queues (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    mode              TEXT    NOT NULL DEFAULT 'sequential',
    max_concurrent    INTEGER NOT NULL DEFAULT 1,
    enabled           INTEGER NOT NULL DEFAULT 1,
    order_index       INTEGER NOT NULL DEFAULT 0,
    speed_limit       INTEGER NOT NULL DEFAULT 0,
    network_interface TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL DEFAULT '',
    queue_id     INTEGER REFERENCES queues(id) ON DELETE CASCADE,
    days_mask    INTEGER NOT NULL DEFAULT 127,
    start_time   TEXT    NOT NULL DEFAULT '02:00',
    end_time     TEXT    NOT NULL DEFAULT '06:00',
    action_start TEXT    NOT NULL DEFAULT 'start',
    action_end   TEXT    NOT NULL DEFAULT 'pause',
    speed_limit  INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS proxies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL DEFAULT '',
    scheme      TEXT    NOT NULL DEFAULT 'http',
    host        TEXT    NOT NULL,
    port        INTEGER NOT NULL,
    username    TEXT    NOT NULL DEFAULT '',
    password    TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    order_index INTEGER NOT NULL DEFAULT 0,
    fail_count  INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id INTEGER,
    ts          REAL NOT NULL,
    level       TEXT NOT NULL DEFAULT 'info',
    message     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_chunks_download ON chunks(download_id);
CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
CREATE INDEX IF NOT EXISTS idx_downloads_queue ON downloads(queue_id);
CREATE INDEX IF NOT EXISTS idx_events_download ON events(download_id, ts);
"""

_DOWNLOAD_COLUMNS = (
    "url", "original_url", "filename", "auto_named", "dest_dir", "temp_path",
    "total_size",
    "downloaded", "status", "mode", "category", "queue_id", "priority",
    "connections", "supports_ranges", "etag", "last_modified", "mime", "referer",
    "user_agent", "cookies", "extra_headers", "expected_hash", "expected_hash_algo",
    "computed_hash", "hash_status", "server_digest", "segments", "sabr_context",
    "mux_group", "media_title",
    "format_id", "proxy_id", "network_interface", "speed_limit", "error",
    "created_at", "started_at", "completed_at",
)


def _int(row: Any, column: str, default: int = 0) -> int:
    """Read an integer column, tolerating a row that predates it."""
    try:
        value = row[column]
    except (IndexError, KeyError):
        return default
    return default if value is None else int(value)


def _text(row: Any, column: str) -> str:
    """Read a text column, tolerating a row that predates it."""
    try:
        return row[column] or ""
    except (IndexError, KeyError):
        return ""


def _json_object(row: Any, column: str) -> dict[str, Any]:
    """Read a JSON object column, tolerating a row that predates it."""
    try:
        raw = row[column]
    except (IndexError, KeyError):
        return {}
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _scheme_text(scheme: Any) -> str:
    """A proxy scheme as text, whether it arrives as an enum or a string.

    `ProxyScheme` subclasses `str`, so anything that carries it through a
    boundary that flattens values — Qt's `QVariant`, JSON, the control socket —
    hands back a plain string. `.value` then raises `AttributeError`, which is
    how every proxy the settings panel added failed to be written while looking
    like it had been.
    """
    return getattr(scheme, "value", None) or str(scheme or "http")


class Database:
    """Thread-safe data-access layer over a single SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        #: Whether what happens is written down at all. Every message in the
        #: application — the engine's and the extension's alike — arrives
        #: through `log_event`, so this one flag is the whole switch. The
        #: service keeps it in step with the `keep_log` setting.
        self.log_enabled = True
        self._init_schema()

    def _upgrade(self, conn: sqlite3.Connection, current: int) -> None:
        """Bring an older database up to the current schema.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
        column added later has to be applied explicitly or every read of it
        fails on databases created by an earlier version.
        """
        if current < 2:
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
            }
            if "sabr_context" not in existing:
                conn.execute(
                    "ALTER TABLE downloads ADD COLUMN sabr_context "
                    "TEXT NOT NULL DEFAULT '{}'"
                )
        if current < 3:
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
            }
            if "mux_group" not in existing:
                conn.execute(
                    "ALTER TABLE downloads ADD COLUMN mux_group "
                    "TEXT NOT NULL DEFAULT ''"
                )
        if current < 4:
            # Zero now means "follow the connections-per-download setting".
            #
            # Every download used to be stamped with whatever the setting said
            # when it was added, so raising the setting afterwards changed
            # nothing for anything already in the list — and the setting is the
            # one place a person expects to change it. Those stamps were never
            # a choice; the Add dialog simply passed the value it had opened
            # on. Unfinished downloads are released so they follow the setting
            # from now on; finished ones are left exactly as they are.
            conn.execute(
                "UPDATE downloads SET connections=0 WHERE status != ?",
                (DownloadStatus.COMPLETED.value,),
            )
        if current < 5:
            # A name derived from a URL is a guess, and a guess has to be
            # replaceable. Reported from GitHub, whose release assets redirect
            # to a path ending in a UUID: the download arrived called
            # `74709710-bf21-4cd4-926a-526ff561a1bb` with no extension, while
            # the response said `filename=ixd_1.0.3_amd64.deb` all along.
            #
            # Existing rows are marked as *chosen* rather than guessed. Their
            # files are already on disk under those names, and renaming
            # somebody's finished downloads underneath them is worse than
            # leaving a bad name alone.
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
            }
            if "auto_named" not in existing:
                conn.execute(
                    "ALTER TABLE downloads ADD COLUMN auto_named "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    # ------------------------------------------------------------------
    # connection plumbing
    # ------------------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self.conn
            conn.executescript(_SCHEMA)
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                self._upgrade(conn, current)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            if conn.execute("SELECT COUNT(*) FROM queues").fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO queues (name, mode, max_concurrent, enabled, order_index)"
                    " VALUES (?,?,?,?,?)",
                    ("Main Queue", QueueMode.CONCURRENT.value, 4, 1, 0),
                )
                conn.execute(
                    "INSERT INTO queues (name, mode, max_concurrent, enabled, order_index)"
                    " VALUES (?,?,?,?,?)",
                    ("Overnight", QueueMode.SEQUENTIAL.value, 1, 1, 1),
                )

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, params)

    def _executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._write_lock:
            self.conn.executemany(sql, list(seq))

    # ------------------------------------------------------------------
    # downloads
    # ------------------------------------------------------------------
    @staticmethod
    def _download_values(d: Download) -> list[Any]:
        return [
            d.url, d.original_url or d.url, d.filename, int(d.auto_named),
            d.dest_dir, d.temp_path,
            d.total_size, d.downloaded, d.status.value, d.mode.value, d.category,
            d.queue_id, d.priority, d.connections, int(d.supports_ranges), d.etag,
            d.last_modified, d.mime, d.referer, d.user_agent, d.cookies,
            json.dumps(d.extra_headers), d.expected_hash, d.expected_hash_algo,
            d.computed_hash, d.hash_status.value, d.server_digest,
            json.dumps([s.to_dict() for s in d.segments]),
            json.dumps(d.sabr_context or {}), d.mux_group, d.media_title,
            d.format_id, d.proxy_id, d.network_interface, d.speed_limit, d.error,
            d.created_at, d.started_at, d.completed_at,
        ]

    @staticmethod
    def _download_from_row(row: sqlite3.Row) -> Download:
        try:
            headers = json.loads(row["extra_headers"] or "{}")
        except ValueError:
            headers = {}
        try:
            segments = [MediaSegment.from_dict(s) for s in json.loads(row["segments"] or "[]")]
        except (ValueError, KeyError, TypeError):
            segments = []
        return Download(
            id=row["id"], url=row["url"], original_url=row["original_url"],
            filename=row["filename"], auto_named=bool(_int(row, "auto_named")),
            dest_dir=row["dest_dir"], temp_path=row["temp_path"],
            total_size=row["total_size"], downloaded=row["downloaded"],
            status=DownloadStatus(row["status"]), mode=TransferMode(row["mode"]),
            category=row["category"], queue_id=row["queue_id"], priority=row["priority"],
            connections=row["connections"], supports_ranges=bool(row["supports_ranges"]),
            etag=row["etag"], last_modified=row["last_modified"], mime=row["mime"],
            referer=row["referer"], user_agent=row["user_agent"], cookies=row["cookies"],
            extra_headers=headers, expected_hash=row["expected_hash"],
            expected_hash_algo=row["expected_hash_algo"], computed_hash=row["computed_hash"],
            hash_status=HashStatus(row["hash_status"]), server_digest=row["server_digest"],
            segments=segments, sabr_context=_json_object(row, "sabr_context"),
            mux_group=_text(row, "mux_group"), media_title=row["media_title"], format_id=row["format_id"],
            proxy_id=row["proxy_id"], network_interface=row["network_interface"],
            speed_limit=row["speed_limit"], error=row["error"],
            created_at=row["created_at"], started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    def insert_download(self, d: Download) -> int:
        columns = ", ".join(_DOWNLOAD_COLUMNS)
        placeholders = ", ".join("?" * len(_DOWNLOAD_COLUMNS))
        with self._write_lock:
            cursor = self.conn.execute(
                f"INSERT INTO downloads ({columns}) VALUES ({placeholders})",
                self._download_values(d),
            )
            d.id = int(cursor.lastrowid)
        return d.id

    def update_download(self, d: Download) -> None:
        if d.id is None:
            raise ValueError("cannot update a download without an id")
        assignments = ", ".join(f"{col}=?" for col in _DOWNLOAD_COLUMNS)
        self._execute(
            f"UPDATE downloads SET {assignments} WHERE id=?",
            [*self._download_values(d), d.id],
        )

    def update_download_fields(self, download_id: int, **fields: Any) -> None:
        """Patch individual columns without rewriting the whole row."""
        if not fields:
            return
        clean: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in _DOWNLOAD_COLUMNS:
                raise KeyError(f"unknown download column: {key}")
            if hasattr(value, "value"):        # Enum
                value = value.value
            elif isinstance(value, bool):
                value = int(value)
            elif isinstance(value, dict):
                value = json.dumps(value)
            clean[key] = value
        assignments = ", ".join(f"{key}=?" for key in clean)
        self._execute(
            f"UPDATE downloads SET {assignments} WHERE id=?",
            [*clean.values(), download_id],
        )

    def get_download(self, download_id: int) -> Download | None:
        row = self.conn.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()
        return self._download_from_row(row) if row else None

    def list_downloads(self, status: DownloadStatus | None = None) -> list[Download]:
        if status is not None:
            rows = self.conn.execute(
                "SELECT * FROM downloads WHERE status=? ORDER BY priority DESC, id ASC",
                (status.value,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM downloads ORDER BY priority DESC, id ASC"
            ).fetchall()
        return [self._download_from_row(r) for r in rows]

    def delete_download(self, download_id: int) -> None:
        self._execute("DELETE FROM downloads WHERE id=?", (download_id,))

    def find_download_by_url(self, url: str) -> Download | None:
        row = self.conn.execute(
            "SELECT * FROM downloads WHERE url=? OR original_url=? ORDER BY id DESC LIMIT 1",
            (url, url),
        ).fetchone()
        return self._download_from_row(row) if row else None

    # ------------------------------------------------------------------
    # chunks
    # ------------------------------------------------------------------
    def replace_chunks(self, download_id: int, chunks: Sequence[Chunk]) -> None:
        with self._write_lock:
            self.conn.execute("DELETE FROM chunks WHERE download_id=?", (download_id,))
            self.conn.executemany(
                "INSERT INTO chunks (download_id, idx, start_byte, end_byte, downloaded, status)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (download_id, c.index, c.start, c.end, c.downloaded, c.status.value)
                    for c in chunks
                ],
            )
            for chunk in chunks:
                chunk.download_id = download_id

    def upsert_chunk(self, download_id: int, chunk: Chunk) -> None:
        self._execute(
            "INSERT INTO chunks (download_id, idx, start_byte, end_byte, downloaded, status)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(download_id, idx) DO UPDATE SET"
            "   start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
            "   downloaded=excluded.downloaded, status=excluded.status",
            (download_id, chunk.index, chunk.start, chunk.end, chunk.downloaded,
             chunk.status.value),
        )
        chunk.download_id = download_id

    def flush_chunk_progress(self, download_id: int, chunks: Sequence[Chunk]) -> None:
        """Hot path: persist cursors for every chunk in one statement batch."""
        self._executemany(
            "INSERT INTO chunks (download_id, idx, start_byte, end_byte, downloaded, status)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(download_id, idx) DO UPDATE SET"
            "   start_byte=excluded.start_byte, end_byte=excluded.end_byte,"
            "   downloaded=excluded.downloaded, status=excluded.status",
            [
                (download_id, c.index, c.start, c.end, c.downloaded, c.status.value)
                for c in chunks
            ],
        )

    def load_chunks(self, download_id: int) -> list[Chunk]:
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE download_id=? ORDER BY idx", (download_id,)
        ).fetchall()
        return [Chunk.from_row(r) for r in rows]

    def clear_chunks(self, download_id: int) -> None:
        self._execute("DELETE FROM chunks WHERE download_id=?", (download_id,))

    # ------------------------------------------------------------------
    # queues
    # ------------------------------------------------------------------
    def list_queues(self) -> list[DownloadQueue]:
        rows = self.conn.execute("SELECT * FROM queues ORDER BY order_index, id").fetchall()
        return [DownloadQueue.from_row(r) for r in rows]

    def get_queue(self, queue_id: int) -> DownloadQueue | None:
        row = self.conn.execute("SELECT * FROM queues WHERE id=?", (queue_id,)).fetchone()
        return DownloadQueue.from_row(row) if row else None

    def insert_queue(self, q: DownloadQueue) -> int:
        with self._write_lock:
            cursor = self.conn.execute(
                "INSERT INTO queues (name, mode, max_concurrent, enabled, order_index,"
                " speed_limit, network_interface) VALUES (?,?,?,?,?,?,?)",
                (q.name, q.mode.value, q.max_concurrent, int(q.enabled), q.order_index,
                 q.speed_limit, q.network_interface),
            )
            q.id = int(cursor.lastrowid)
        return q.id

    def update_queue(self, q: DownloadQueue) -> None:
        self._execute(
            "UPDATE queues SET name=?, mode=?, max_concurrent=?, enabled=?, order_index=?,"
            " speed_limit=?, network_interface=? WHERE id=?",
            (q.name, q.mode.value, q.max_concurrent, int(q.enabled), q.order_index,
             q.speed_limit, q.network_interface, q.id),
        )

    def delete_queue(self, queue_id: int) -> None:
        self._execute("DELETE FROM queues WHERE id=?", (queue_id,))

    def downloads_in_queue(self, queue_id: int) -> list[Download]:
        rows = self.conn.execute(
            "SELECT * FROM downloads WHERE queue_id=? ORDER BY priority DESC, id ASC",
            (queue_id,),
        ).fetchall()
        return [self._download_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # schedules
    # ------------------------------------------------------------------
    def list_schedules(self) -> list[Schedule]:
        rows = self.conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        return [Schedule.from_row(r) for r in rows]

    def insert_schedule(self, s: Schedule) -> int:
        with self._write_lock:
            cursor = self.conn.execute(
                "INSERT INTO schedules (name, queue_id, days_mask, start_time, end_time,"
                " action_start, action_end, speed_limit, enabled) VALUES (?,?,?,?,?,?,?,?,?)",
                (s.name, s.queue_id, s.days_mask, s.start_time, s.end_time,
                 s.action_start.value, s.action_end.value, s.speed_limit, int(s.enabled)),
            )
            s.id = int(cursor.lastrowid)
        return s.id

    def update_schedule(self, s: Schedule) -> None:
        self._execute(
            "UPDATE schedules SET name=?, queue_id=?, days_mask=?, start_time=?, end_time=?,"
            " action_start=?, action_end=?, speed_limit=?, enabled=? WHERE id=?",
            (s.name, s.queue_id, s.days_mask, s.start_time, s.end_time,
             s.action_start.value, s.action_end.value, s.speed_limit, int(s.enabled), s.id),
        )

    def delete_schedule(self, schedule_id: int) -> None:
        self._execute("DELETE FROM schedules WHERE id=?", (schedule_id,))

    # ------------------------------------------------------------------
    # proxies
    # ------------------------------------------------------------------
    def list_proxies(self, enabled_only: bool = False) -> list[ProxyEntry]:
        sql = "SELECT * FROM proxies"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY order_index, id"
        return [ProxyEntry.from_row(r) for r in self.conn.execute(sql).fetchall()]

    def get_proxy(self, proxy_id: int) -> ProxyEntry | None:
        row = self.conn.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
        return ProxyEntry.from_row(row) if row else None

    def insert_proxy(self, p: ProxyEntry) -> int:
        with self._write_lock:
            cursor = self.conn.execute(
                "INSERT INTO proxies (label, scheme, host, port, username, password,"
                " enabled, order_index, fail_count, last_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (p.label, _scheme_text(p.scheme), p.host, p.port, p.username, p.password,
                 int(p.enabled), p.order_index, p.fail_count, p.last_error),
            )
            p.id = int(cursor.lastrowid)
        return p.id

    def update_proxy(self, p: ProxyEntry) -> None:
        self._execute(
            "UPDATE proxies SET label=?, scheme=?, host=?, port=?, username=?, password=?,"
            " enabled=?, order_index=?, fail_count=?, last_error=? WHERE id=?",
            (p.label, _scheme_text(p.scheme), p.host, p.port, p.username, p.password,
             int(p.enabled), p.order_index, p.fail_count, p.last_error, p.id),
        )

    def delete_proxy(self, proxy_id: int) -> None:
        self._execute("DELETE FROM proxies WHERE id=?", (proxy_id,))

    def record_proxy_failure(self, proxy_id: int, error: str) -> None:
        self._execute(
            "UPDATE proxies SET fail_count=fail_count+1, last_error=? WHERE id=?",
            (error[:500], proxy_id),
        )

    def reset_proxy_failures(self, proxy_id: int) -> None:
        self._execute(
            "UPDATE proxies SET fail_count=0, last_error='' WHERE id=?", (proxy_id,)
        )

    # ------------------------------------------------------------------
    # events / audit log
    # ------------------------------------------------------------------
    def log_event(self, message: str, download_id: int | None = None, level: str = "info") -> None:
        # Switched off means nothing is written, not "written and hidden".
        # Anyone who turns it off has said they do not want a record kept.
        if not self.log_enabled:
            return
        self._execute(
            "INSERT INTO events (download_id, ts, level, message) VALUES (?,?,?,?)",
            (download_id, time.time(), level, message[:2000]),
        )

    def recent_events(self, limit: int = 200, download_id: int | None = None) -> list[dict[str, Any]]:
        if download_id is None:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE download_id=? ORDER BY id DESC LIMIT ?",
                (download_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_events(self) -> None:
        """Empty the log. Only the log — no download is touched."""
        self._execute("DELETE FROM events")

    def prune_events(self, keep: int = 5000) -> None:
        self._execute(
            "DELETE FROM events WHERE id NOT IN"
            " (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
            (keep,),
        )

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    def recover_interrupted(self) -> int:
        """Anything left mid-flight by a crash becomes paused, not lost."""
        active = (
            DownloadStatus.DOWNLOADING.value,
            DownloadStatus.CONNECTING.value,
            DownloadStatus.ASSEMBLING.value,
            DownloadStatus.VERIFYING.value,
        )
        with self._write_lock:
            cursor = self.conn.execute(
                f"UPDATE downloads SET status=? WHERE status IN ({','.join('?' * len(active))})",
                (DownloadStatus.PAUSED.value, *active),
            )
            self.conn.execute(
                "UPDATE chunks SET status=? WHERE status=?",
                (ChunkStatus.PENDING.value, ChunkStatus.ACTIVE.value),
            )
            return cursor.rowcount or 0

    def park_queued(self) -> int:
        """A download queued in a previous session does not start itself.

        `recover_interrupted` already refuses to resume a transfer that was
        running when the process died: a restart resumes nothing on its own.
        Queued rows were the hole in that. Nothing ever cleared them, and the
        supervisor's pump reads the whole table every second and starts
        anything it finds queued — so a download that never got a free slot
        months ago was still sitting there, and the next launch started it.

        On Windows, where the application launches at login, that is the whole
        of a field report: sign in, and downloads nobody asked for that day
        begin at once. They were added, once, and then left behind a
        concurrency limit.

        Parked, not lost — the same word the interrupted ones get. They are in
        the list, at their byte count, waiting to be started.
        """
        with self._write_lock:
            cursor = self.conn.execute(
                "UPDATE downloads SET status=? WHERE status IN (?, ?)",
                (DownloadStatus.PAUSED.value,
                 DownloadStatus.QUEUED.value,
                 DownloadStatus.SCHEDULED.value),
            )
            return cursor.rowcount or 0

    def vacuum(self) -> None:
        with self._write_lock:
            self.conn.execute("VACUUM")

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,"
            " SUM(downloaded) AS bytes FROM downloads"
        ).fetchone()
        return {
            "total": row["total"] or 0,
            "completed": row["completed"] or 0,
            "bytes": row["bytes"] or 0,
        }
