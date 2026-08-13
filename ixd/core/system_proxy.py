"""Read the operating system's own proxy configuration.

"Use the system proxy" is the setting most people actually want: whatever the
browser and the rest of the desktop already use, without retyping it here. That
is not one lookup though — every platform, and on Linux every desktop, keeps it
somewhere else, and the environment can override all of them.

Resolution order (first hit wins):

1. ``https_proxy`` / ``http_proxy`` / ``all_proxy`` environment variables,
   which by convention override everything else.
2. The desktop's own setting — GNOME/GSettings or KDE's ``kioslaverc`` on
   Linux, ``scutil --proxy`` on macOS, ``Internet Settings`` in the registry on
   Windows.

The bypass list travels with the result, because a system proxy that is not
applied to ``localhost`` will break the application's own control socket.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .models import ProxyEntry, ProxyScheme

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

#: Never routed through a proxy — the control socket lives here.
_ALWAYS_BYPASS = ("localhost", "127.0.0.1", "::1")


@dataclass(slots=True)
class SystemProxy:
    """What the operating system says traffic should go through."""

    proxy: ProxyEntry | None = None
    bypass: tuple[str, ...] = field(default_factory=lambda: _ALWAYS_BYPASS)
    source: str = "none"
    note: str = ""

    @property
    def configured(self) -> bool:
        return self.proxy is not None

    def describe(self) -> str:
        if self.proxy is None:
            return f"no system proxy configured ({self.source})"
        return f"{self.proxy.as_url()} (from {self.source})"


def _entry(scheme: ProxyScheme, host: str, port: int, label: str,
           username: str = "", password: str = "") -> ProxyEntry | None:
    if not host or port <= 0:
        return None
    return ProxyEntry(
        label=label, scheme=scheme, host=host, port=int(port),
        username=username, password=password, enabled=True,
    )


def _parse_proxy_value(value: str, default_scheme: ProxyScheme,
                       label: str) -> ProxyEntry | None:
    """Parse ``[scheme://][user:pass@]host[:port]``."""
    import urllib.parse

    raw = (value or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"{default_scheme.value}://{raw}"
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return None

    aliases = {
        "http": ProxyScheme.HTTP, "https": ProxyScheme.HTTPS,
        "socks": ProxyScheme.SOCKS5, "socks5": ProxyScheme.SOCKS5,
        "socks4": ProxyScheme.SOCKS5, "socks5h": ProxyScheme.SOCKS5H,
    }
    scheme = aliases.get((parsed.scheme or "").lower())
    if scheme is None:
        return None
    default_port = 1080 if scheme in (ProxyScheme.SOCKS5, ProxyScheme.SOCKS5H) else 8080
    return _entry(
        scheme, parsed.hostname or "", parsed.port or default_port, label,
        urllib.parse.unquote(parsed.username or ""),
        urllib.parse.unquote(parsed.password or ""),
    )


def _split_bypass(value: str) -> tuple[str, ...]:
    parts = [p.strip().lower() for p in re.split(r"[,;\s]+", value or "") if p.strip()]
    merged = list(_ALWAYS_BYPASS)
    for part in parts:
        if part and part not in merged:
            merged.append(part)
    return tuple(merged)


# ----------------------------------------------------------------------
# sources
# ----------------------------------------------------------------------
def _from_environment() -> SystemProxy | None:
    def read(*names: str) -> str:
        for name in names:
            value = os.environ.get(name) or os.environ.get(name.upper())
            if value:
                return value
        return ""

    value = read("https_proxy") or read("http_proxy") or read("all_proxy")
    if not value:
        return None
    proxy = _parse_proxy_value(value, ProxyScheme.HTTP, "System (environment)")
    if proxy is None:
        return None
    return SystemProxy(
        proxy=proxy,
        bypass=_split_bypass(read("no_proxy")),
        source="environment",
    )


def _gsettings(key: str, schema: str = "org.gnome.system.proxy") -> str:
    try:
        completed = subprocess.run(
            ["gsettings", "get", schema, key],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip().strip("'\"")


def _from_gnome() -> SystemProxy | None:
    mode = _gsettings("mode")
    if not mode:
        return None
    if mode == "none":
        return SystemProxy(source="GNOME (direct)")
    if mode == "auto":
        return SystemProxy(
            source="GNOME (automatic)",
            note="The desktop uses a proxy auto-configuration script, which "
                 "this application cannot evaluate. Enter the proxy manually.",
        )

    ignored = _gsettings("ignore-hosts")
    bypass = _split_bypass(re.sub(r"[\[\]']", " ", ignored))

    for schema, scheme in (
        ("org.gnome.system.proxy.https", ProxyScheme.HTTPS),
        ("org.gnome.system.proxy.http", ProxyScheme.HTTP),
        ("org.gnome.system.proxy.socks", ProxyScheme.SOCKS5),
    ):
        host = _gsettings("host", schema)
        port = _gsettings("port", schema)
        if not host or not port.isdigit() or int(port) <= 0:
            continue
        username = password = ""
        if scheme is ProxyScheme.HTTP and _gsettings("use-authentication", schema) == "true":
            username = _gsettings("authentication-user", schema)
            password = _gsettings("authentication-password", schema)
        # An https-scheme entry in GNOME means "proxy for https traffic", which
        # is an ordinary HTTP CONNECT proxy, not a TLS connection to the proxy.
        effective = ProxyScheme.HTTP if scheme is ProxyScheme.HTTPS else scheme
        proxy = _entry(effective, host, int(port), "System (GNOME)", username, password)
        if proxy is not None:
            return SystemProxy(proxy=proxy, bypass=bypass, source="GNOME")
    return SystemProxy(source="GNOME (nothing set)")


def _from_kde() -> SystemProxy | None:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    path = config / "kioslaverc"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    section = ""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Proxy Settings" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    # ProxyType: 0 none, 1 manual, 2 PAC, 3 WPAD, 4 environment.
    proxy_type = values.get("ProxyType", "0")
    if proxy_type != "1":
        return None
    bypass = _split_bypass(values.get("NoProxyFor", ""))
    for key, scheme in (
        ("httpsProxy", ProxyScheme.HTTP),
        ("httpProxy", ProxyScheme.HTTP),
        ("socksProxy", ProxyScheme.SOCKS5),
    ):
        # KDE stores "http://host 8080" — a space, not a colon.
        raw = values.get(key, "").replace(" ", ":")
        proxy = _parse_proxy_value(raw, scheme, "System (KDE)")
        if proxy is not None:
            return SystemProxy(proxy=proxy, bypass=bypass, source="KDE")
    return None


def _from_macos() -> SystemProxy | None:
    try:
        completed = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()

    bypass = _split_bypass(" ".join(
        value for key, value in values.items() if key.startswith("ExceptionsList")
    ))

    for enable, host_key, port_key, scheme in (
        ("HTTPSEnable", "HTTPSProxy", "HTTPSPort", ProxyScheme.HTTP),
        ("HTTPEnable", "HTTPProxy", "HTTPPort", ProxyScheme.HTTP),
        ("SOCKSEnable", "SOCKSProxy", "SOCKSPort", ProxyScheme.SOCKS5),
    ):
        if values.get(enable) != "1":
            continue
        port = values.get(port_key, "")
        proxy = _entry(
            scheme, values.get(host_key, ""), int(port) if port.isdigit() else 0,
            "System (macOS)",
        )
        if proxy is not None:
            return SystemProxy(proxy=proxy, bypass=bypass, source="macOS")
    return SystemProxy(source="macOS (direct)")


def _from_windows() -> SystemProxy | None:
    import winreg      # noqa: PLC0415 - Windows only

    path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enabled:
                return SystemProxy(source="Windows (direct)")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            try:
                override, _ = winreg.QueryValueEx(key, "ProxyOverride")
            except OSError:
                override = ""
    except OSError:
        return None

    bypass = _split_bypass(str(override).replace("<local>", ""))

    # ProxyServer is either "host:port" or "http=host:port;https=host:port".
    text = str(server or "")
    if "=" in text:
        mapping = {}
        for part in text.split(";"):
            if "=" in part:
                scheme_name, _, value = part.partition("=")
                mapping[scheme_name.strip().lower()] = value.strip()
        text = mapping.get("https") or mapping.get("http") or mapping.get("socks") or ""

    proxy = _parse_proxy_value(text, ProxyScheme.HTTP, "System (Windows)")
    if proxy is None:
        return SystemProxy(source="Windows (nothing set)")
    return SystemProxy(proxy=proxy, bypass=bypass, source="Windows")


# ----------------------------------------------------------------------
def detect() -> SystemProxy:
    """Resolve the system proxy, or report why there is none."""
    from_environment = _from_environment()
    if from_environment is not None:
        return from_environment

    sources = []
    if IS_WINDOWS:
        sources = [_from_windows]
    elif IS_MACOS:
        sources = [_from_macos]
    else:
        sources = [_from_gnome, _from_kde]

    fallback: SystemProxy | None = None
    for source in sources:
        try:
            result = source()
        except Exception:  # noqa: BLE001 - a broken desktop tool is not fatal
            continue
        if result is None:
            continue
        if result.configured:
            return result
        fallback = fallback or result

    return fallback or SystemProxy(source="none")


def host_is_bypassed(host: str, bypass: tuple[str, ...] | list[str]) -> bool:
    """Match a hostname against a proxy bypass list.

    Supports the three forms these lists actually use: an exact host, a
    ``.example.com`` suffix, and a ``*.example.com`` wildcard.
    """
    target = (host or "").strip().lower().rstrip(".")
    if not target:
        return False
    for raw in bypass:
        pattern = (raw or "").strip().lower().rstrip(".")
        if not pattern or pattern == "<local>":
            continue
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            pattern = pattern[1:]
        if pattern.startswith("."):
            if target == pattern[1:] or target.endswith(pattern):
                return True
            continue
        if target == pattern or target.endswith("." + pattern):
            return True
    return False
