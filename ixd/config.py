"""Filesystem layout, platform helpers and the default settings map.

Everything user-visible that needs to survive a restart lives either in the
SQLite database (see :mod:`ixd.core.db`) or in the JSON settings blob managed
here.  Nothing in this module touches the network or Qt so it can safely be
imported by the headless daemon, the native-messaging host and the GUI alike.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

APP_NAME = "Internet Xtreme Downloader"
APP_SLUG = "ixd"
APP_ID = "com.ixd.downloader"
NATIVE_HOST_NAME = "com.ixd.downloader"

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _base_data_dir() -> Path:
    """Per-platform writable application data directory."""
    override = os.environ.get("IXD_HOME")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        root = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(root) / "IXD"
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / "IXD"
    root = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(root) / APP_SLUG


def _default_download_dir() -> Path:
    xdg = Path.home() / "Downloads"
    if IS_LINUX:
        # Honour a user-configured XDG download directory when one exists.
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "user-dirs.dirs"
        try:
            for line in cfg.read_text(encoding="utf-8").splitlines():
                if line.startswith("XDG_DOWNLOAD_DIR"):
                    value = line.split("=", 1)[1].strip().strip('"')
                    value = value.replace("$HOME", str(Path.home()))
                    return Path(value)
        except OSError:
            pass
    return xdg


DATA_DIR = _base_data_dir()
DB_PATH = DATA_DIR / "state.sqlite3"
LOG_DIR = DATA_DIR / "logs"
TEMP_DIR = DATA_DIR / "incomplete"
SETTINGS_PATH = DATA_DIR / "settings.json"
IPC_PORT_FILE = DATA_DIR / "ipc.json"

#: Categories drive the sidebar and the default per-type destination folders.
CATEGORY_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "Video": (
        "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg",
        "3gp", "ts", "m2ts", "ogv", "vob",
    ),
    "Audio": ("mp3", "m4a", "aac", "flac", "wav", "ogg", "opus", "wma", "alac", "aiff"),
    "Documents": (
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp",
        "txt", "rtf", "epub", "mobi", "csv", "djvu",
    ),
    "Compressed": ("zip", "rar", "7z", "tar", "gz", "bz2", "xz", "zst", "tgz", "iso", "cab"),
    "Programs": ("exe", "msi", "dmg", "pkg", "deb", "rpm", "appimage", "apk", "bin", "run", "snap", "flatpak"),
    "Images": ("jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "tiff", "ico", "heic", "raw"),
}

CATEGORY_ORDER = ("Video", "Audio", "Documents", "Compressed", "Programs", "Images", "Other")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_SETTINGS: dict[str, Any] = {
    # --- transfer engine -------------------------------------------------
    "download_dir": str(_default_download_dir()),
    "categorize_into_subfolders": True,
    "max_concurrent_downloads": 4,
    "connections_per_download": 8,
    "max_connections_per_download": 32,
    "min_chunk_size": 1 << 20,          # never split below 1 MiB
    # How long to wait for a connection to be established, as opposed to how
    # long a stalled read is tolerated once one is. Kept short: there is no
    # response to abandon before a socket is open, so a download stuck at
    # "connecting" cannot be paused until this elapses.
    "connect_timeout": 8.0,
    # How much is taken from a socket per read. Larger means fewer syscalls and
    # fewer rate-limiter round trips per megabyte; it costs this much memory
    # per connection, so sixteen connections hold sixteen of these.
    "read_buffer": 1 << 18,             # 256 KiB
    "chunk_split_threshold": 4 << 20,   # only steal work from chunks above this
    "dynamic_chunking": True,
    "socket_timeout": 30.0,
    "read_buffer": 1 << 16,
    "max_retries": 5,
    "retry_backoff": 2.0,
    "global_speed_limit": 0,            # bytes/sec, 0 = unlimited
    "per_download_speed_limit": 0,
    "progress_flush_interval": 1.0,     # seconds between SQLite chunk flushes
    # --- network ---------------------------------------------------------
    "user_agent": DEFAULT_USER_AGENT,
    "proxy_mode": "none",               # none | system | single | rotate
    "active_proxy_id": None,
    "proxy_rotate_on_error": True,
    "proxy_max_failures": 3,
    "network_interface": "",            # e.g. "tun0"/"wg0"; empty = system default
    "verify_tls": True,
    "ipv6_enabled": True,
    # --- integrity -------------------------------------------------------
    "auto_verify_headers": True,
    "hash_algorithms": ["sha256"],
    "hash_chunk_size": 4 << 20,
    # --- behaviour -------------------------------------------------------
    # Start with the session, always with the window down: a download manager
    # is only useful if it is already running when a download starts, and a
    # window that opens by itself at every login is why people turn this off.
    # The registration is refreshed on every start, so a rebuilt or moved
    # application does not leave the session launching a path that is gone.
    "launch_at_startup": False,
    # What to do once every download has finished: nothing, exit, sleep,
    # hibernate or shutdown. It fires **once** and resets itself — a machine
    # that shuts down every time a download ends is a machine nobody can use,
    # and "shut down when this queue is done" is a decision about tonight
    # rather than a standing policy.
    "completion_action": "nothing",
    # How long the countdown runs before it happens. Long enough to be back at
    # the desk and stop it; the window offers a Cancel for the whole period.
    "completion_grace_seconds": 60,
    "start_minimized": False,
    "minimize_to_tray": True,
    # Off by default: `QSystemTrayIcon.isVisible()` only says that `show()` was
    # called, not that any desktop draws it, so a session without a tray
    # swallowed the whole application — the window closed and the process had
    # to be killed by hand.
    "close_to_tray": False,
    # Whether anything is recorded at all. On by default because the log is
    # the instrument every field report is answered with — but it is a record
    # of what you downloaded and from where, so it is a choice rather than an
    # assumption. Off means nothing is written and what was written is dropped.
    "keep_log": True,
    # The log holds this launch only. It is read by copying the whole of it
    # into a report, and one that spans a fortnight of launches buries the
    # fifty lines that describe the fault.
    "clear_log_on_launch": True,
    # How much of the log to keep across restarts, when the above is off.
    "log_lines_kept": 2000,
    # Rewrite an assembled MPEG transport stream as an MP4. Every frame is
    # copied; only the packaging changes. Off means keeping the `.ts` the site
    # actually served.
    "remux_transport_streams": True,
    "autostart_downloads": True,
    # A new download opens its own progress window. Closing that window leaves
    # the transfer running in the main list; opening the row brings it back.
    "show_download_window": True,
    "notify_on_complete": True,
    "theme": "dark",
    "accent": "#5B8CFF",
    # --- integration -----------------------------------------------------
    "ipc_host": "127.0.0.1",
    "ipc_port": 47615,
    "ipc_token": "",                    # generated on first run
    "browser_integration": True,
    "intercept_min_size": 0,
    "intercept_extensions": [],         # empty = every extension the extension offers
    "ignored_hosts": ["localhost", "127.0.0.1"],
    # --- media -----------------------------------------------------------
    "preferred_video_quality": "1080p",
    # Which container to take when a quality is published in more than one.
    #
    # At 60fps and above, the same resolution is offered as WebM (VP9/AV1) and
    # as MP4 (H.264), and nothing but bitrate used to separate them — on a real
    # video the two sat 0.3% apart, so the container was decided by an accident
    # and the larger file won: 198 MB of VP9 against 136 MB of H.264 for the
    # same 1080p60. MP4 is the default because it is the more widely playable
    # of the two. "webm" prefers the other way; "any" restores deciding on
    # bitrate alone. It is only ever a tie-break — a resolution published in
    # one container only is still offered.
    "preferred_video_container": "mp4",
    "prefer_progressive": True,
    # Optional YouTube attestation values. Some networks (datacenter ranges in
    # particular) are served proof-of-origin-gated responses; pasting a PO
    # token and visitor data here restores extraction on those networks.
    "youtube_po_token": "",
    "youtube_visitor_data": "",
}


class Settings:
    """Thread-safe JSON-backed settings store with atomic writes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else SETTINGS_PATH
        self._lock = threading.RLock()
        self._data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._listeners: list[Any] = []
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    # Merge so that new keys introduced by upgrades get defaults.
                    merged = dict(DEFAULT_SETTINGS)
                    merged.update(raw)
                    self._data = merged
            except (OSError, ValueError):
                self._data = dict(DEFAULT_SETTINGS)
            if not self._data.get("ipc_token"):
                self._data["ipc_token"] = os.urandom(24).hex()
                self._flush_unlocked()

    def _flush_unlocked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def save(self) -> None:
        with self._lock:
            self._flush_unlocked()

    # -- access -----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULT_SETTINGS.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        with self._lock:
            self._data[key] = value
            if save:
                self._flush_unlocked()
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(key, value)
            except Exception:  # a broken listener must never break settings
                pass

    def update(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value, save=False)
        self.save()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def on_change(self, callback) -> None:
        with self._lock:
            self._listeners.append(callback)


#: Where this application's data lived before it was renamed. The download
#: history, the settings and every partially fetched file are in there, and a
#: rename that leaves them behind reads to the user as "it lost everything".
_FORMER_DIR_NAMES = ("xai-dm", "XAIDownloadManager")


def _former_data_dirs() -> list[Path]:
    if os.environ.get("IXD_HOME"):
        return []          # an explicit home is an explicit answer
    if IS_WINDOWS:
        root = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    elif IS_MACOS:
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return [root / name for name in _FORMER_DIR_NAMES]


def migrate_former_data_dir() -> Path | None:
    """Adopt the pre-rename data directory, once, if this one is not in use.

    Moved rather than copied: two directories holding two halves of one
    download history is a worse outcome than either. Nothing happens if the
    new directory already holds a database — a user who has started using the
    renamed application is not silently rolled back to an older state.
    """
    if DB_PATH.exists() or SETTINGS_PATH.exists():
        return None
    for former in _former_data_dirs():
        if former == DATA_DIR or not former.is_dir():
            continue
        if not (former / "state.sqlite3").exists():
            continue
        try:
            DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
            if DATA_DIR.exists():
                # Created empty by an earlier `ensure_dirs`: merge into it
                # rather than fail the rename on a directory that exists.
                for entry in former.iterdir():
                    target = DATA_DIR / entry.name
                    if not target.exists():
                        entry.rename(target)
                try:
                    former.rmdir()
                except OSError:
                    pass
            else:
                former.rename(DATA_DIR)
        except OSError:
            return None
        return former
    return None


def ensure_dirs() -> None:
    """Create every directory the application writes into."""
    migrate_former_data_dir()
    for directory in (DATA_DIR, LOG_DIR, TEMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def category_for(filename: str, mime: str = "") -> str:
    """Classify a file by extension first, then by MIME family."""
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext:
        for category, extensions in CATEGORY_EXTENSIONS.items():
            if ext in extensions:
                return category
    mime = (mime or "").lower()
    if mime.startswith("video/"):
        return "Video"
    if mime.startswith("audio/"):
        return "Audio"
    if mime.startswith("image/"):
        return "Images"
    if mime.startswith("text/") or "pdf" in mime or "document" in mime:
        return "Documents"
    if "zip" in mime or "compress" in mime or "tar" in mime or "rar" in mime:
        return "Compressed"
    return "Other"


def destination_for(settings: Settings, filename: str, mime: str = "") -> Path:
    """Resolve the folder a finished file should land in."""
    root = Path(settings.get("download_dir")).expanduser()
    if settings.get_bool("categorize_into_subfolders", True):
        return root / category_for(filename, mime)
    return root
