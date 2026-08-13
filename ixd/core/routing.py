"""Proxy selection, rotation and per-download network profiles.

Rotation is the engine's answer to region locks, IP bans and rate limits: when
a transfer or an extraction trips a 403/429 (or simply cannot connect), the
manager advances to the next healthy proxy and the operation is retried on the
new route rather than failing outright.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .errors import HttpError, NetworkError, ProxyError, RateLimitedError
from .events import EventBus, EventType
from .models import Download, ProxyEntry, ProxyScheme
from .net import NetworkProfile
from .system_proxy import SystemProxy, detect as detect_system_proxy

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Settings
    from .db import Database


#: Failures that justify moving to a different exit IP.
ROTATABLE_STATUS_CODES = frozenset({403, 407, 421, 429, 451, 503})


def should_rotate(error: BaseException) -> bool:
    """Decide whether ``error`` is the kind a different proxy might fix."""
    if isinstance(error, (ProxyError, RateLimitedError)):
        return True
    if isinstance(error, HttpError):
        return error.status in ROTATABLE_STATUS_CODES
    return isinstance(error, NetworkError)


class ProxyManager:
    """Owns the proxy pool and decides which one the next request uses."""

    def __init__(self, db: "Database", settings: "Settings", events: EventBus) -> None:
        self.db = db
        self.settings = settings
        self.events = events
        self._lock = threading.RLock()
        self._index = 0
        self._current: ProxyEntry | None = None
        self._system: SystemProxy | None = None
        self.refresh()

    # ------------------------------------------------------------------
    def system_proxy(self) -> SystemProxy:
        """What the operating system is currently configured to use."""
        with self._lock:
            if self._system is not None:
                return self._system
        return detect_system_proxy()

    def _bypass(self) -> tuple[str, ...]:
        with self._lock:
            return self._system.bypass if self._system is not None else ()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload the pool and re-resolve the active proxy from settings."""
        with self._lock:
            self._pool = [
                p for p in self.db.list_proxies(enabled_only=True)
                if p.fail_count < self.settings.get_int("proxy_max_failures", 3)
            ]
            mode = self.settings.get("proxy_mode", "none")

            if mode == "system":
                # Re-read every refresh: the desktop's proxy can change while
                # the application is running (docking, VPN, network switch).
                self._system = detect_system_proxy()
                self._current = self._system.proxy
                return
            self._system = None

            if mode == "none" or not self._pool:
                self._current = None
                return
            if mode == "single":
                active_id = self.settings.get("active_proxy_id")
                self._current = next(
                    (p for p in self._pool if p.id == active_id), self._pool[0]
                )
                return
            self._index %= len(self._pool)
            self._current = self._pool[self._index]

    @property
    def pool_size(self) -> int:
        with self._lock:
            return len(self._pool)

    def current(self) -> ProxyEntry | None:
        with self._lock:
            return self._current

    def rotate(self, reason: str = "") -> ProxyEntry | None:
        """Advance to the next healthy proxy and return it."""
        with self._lock:
            # "system" follows the desktop's choice; there is no pool to
            # advance through, and overriding it would defeat the setting.
            if self.settings.get("proxy_mode", "none") in ("none", "system"):
                return None
            self.refresh()
            if not self._pool:
                self._current = None
                return None
            self._index = (self._index + 1) % len(self._pool)
            self._current = self._pool[self._index]
            selected = self._current
        self.events.emit(
            EventType.PROXY_ROTATED,
            proxy=selected.as_url() if selected else "direct",
            reason=reason,
        )
        self.db.log_event(
            f"Rotated to proxy {selected.as_url() if selected else 'direct'} ({reason})",
            level="warning",
        )
        return selected

    def report_failure(self, proxy: ProxyEntry | None, error: str) -> None:
        if proxy is None or proxy.id is None:
            return
        self.db.record_proxy_failure(proxy.id, error)

    def report_success(self, proxy: ProxyEntry | None) -> None:
        if proxy is None or proxy.id is None or proxy.fail_count == 0:
            return
        self.db.reset_proxy_failures(proxy.id)
        proxy.fail_count = 0

    # ------------------------------------------------------------------
    def resolve_for(self, download: Download | None = None) -> ProxyEntry | None:
        """Per-download override wins over the global policy."""
        if download is not None and download.proxy_id:
            explicit = self.db.get_proxy(download.proxy_id)
            if explicit and explicit.enabled:
                return explicit
        return self.current()

    def profile_for(self, download: Download | None = None,
                    *, timeout: float | None = None) -> NetworkProfile:
        """Build the :class:`NetworkProfile` a request should be made with."""
        interface = ""
        if download is not None and download.network_interface:
            interface = download.network_interface
        elif download is not None and download.queue_id:
            queue = self.db.get_queue(download.queue_id)
            if queue and queue.network_interface:
                interface = queue.network_interface
        if not interface:
            interface = self.settings.get("network_interface", "") or ""

        user_agent = (download.user_agent if download and download.user_agent
                      else self.settings.get("user_agent"))

        return NetworkProfile(
            proxy=self.resolve_for(download),
            interface=interface,
            timeout=timeout if timeout is not None else self.settings.get_float("socket_timeout", 30.0),
            connect_timeout=self.settings.get_float("connect_timeout", 8.0),
            verify_tls=self.settings.get_bool("verify_tls", True),
            user_agent=user_agent,
            prefer_ipv6=self.settings.get_bool("ipv6_enabled", True),
            proxy_bypass=self._bypass(),
        )


def parse_proxy_url(url: str, label: str = "") -> ProxyEntry:
    """Parse ``socks5://user:pass@host:1080`` into a :class:`ProxyEntry`."""
    import urllib.parse

    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)

    scheme_text = (parsed.scheme or "http").lower()
    aliases = {
        "socks": ProxyScheme.SOCKS5, "socks5": ProxyScheme.SOCKS5,
        "socks5h": ProxyScheme.SOCKS5H, "http": ProxyScheme.HTTP,
        "https": ProxyScheme.HTTPS,
    }
    if scheme_text not in aliases:
        raise ValueError(f"unsupported proxy scheme: {scheme_text!r}")
    scheme = aliases[scheme_text]

    if not parsed.hostname:
        raise ValueError(f"proxy URL is missing a host: {url!r}")
    default_port = 1080 if scheme in (ProxyScheme.SOCKS5, ProxyScheme.SOCKS5H) else 8080

    return ProxyEntry(
        label=label or f"{parsed.hostname}:{parsed.port or default_port}",
        scheme=scheme,
        host=parsed.hostname,
        port=parsed.port or default_port,
        username=urllib.parse.unquote(parsed.username or ""),
        password=urllib.parse.unquote(parsed.password or ""),
    )
