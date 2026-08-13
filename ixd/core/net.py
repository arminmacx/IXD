"""Socket construction: proxies (HTTP/HTTPS/SOCKS5) and interface binding.

Every byte the engine transfers is created through :class:`SocketFactory`, so
proxy routing and VPN-interface pinning apply uniformly to the chunk workers
and to the media extractors.
"""

from __future__ import annotations

import base64
import ipaddress
import socket
import ssl
import struct
import sys
from dataclasses import dataclass, field

from .errors import NetworkError, ProxyError
from .models import ProxyEntry, ProxyScheme
from .system_proxy import host_is_bypassed

IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

# Darwin socket options for binding a socket to a specific interface index.
_IP_BOUND_IF = 25
_IPV6_BOUND_IF = 125
# Linux ioctls for "get interface address" and "get interface flags".
_SIOCGIFADDR = 0x8915
_SIOCGIFFLAGS = 0x8913
# Subset of the IFF_* flags in <net/if.h> that we care about.
_IFF_UP = 0x1
_IFF_LOOPBACK = 0x8
_IFF_RUNNING = 0x40

_SOCKS5_ERRORS = {
    1: "general SOCKS server failure",
    2: "connection not allowed by ruleset",
    3: "network unreachable",
    4: "host unreachable",
    5: "connection refused",
    6: "TTL expired",
    7: "command not supported",
    8: "address type not supported",
}


# ----------------------------------------------------------------------
# interface discovery
# ----------------------------------------------------------------------
def _linux_interface_ipv4(name: str) -> str | None:
    try:
        import fcntl
    except ImportError:
        return None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name.encode("utf-8")[:15])
        result = fcntl.ioctl(sock.fileno(), _SIOCGIFADDR, packed)
        return socket.inet_ntoa(result[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def list_interfaces() -> list[tuple[str, list[str]]]:
    """Enumerate usable network interfaces as ``(name, [addresses])``."""
    return [(entry.name, entry.addresses) for entry in describe_interfaces()]


@dataclass(slots=True)
class InterfaceInfo:
    """An adapter as the settings UI needs to describe it."""

    name: str
    addresses: list[str] = field(default_factory=list)
    up: bool = True
    loopback: bool = False

    def describe(self) -> str:
        bits = [self.name]
        if self.addresses:
            bits.append(", ".join(self.addresses))
        if not self.up:
            bits.append("down")
        return "  ".join(bits)


def _linux_interface_flags(name: str) -> int:
    """Read ``IFF_*`` flags for an adapter, or 0 when unavailable."""
    try:
        import fcntl
    except ImportError:
        return 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name.encode("utf-8")[:15])
        result = fcntl.ioctl(sock.fileno(), _SIOCGIFFLAGS, packed)
        return struct.unpack("H", result[16:18])[0]
    except OSError:
        return 0
    finally:
        sock.close()


def describe_interfaces() -> list[InterfaceInfo]:
    """Enumerate adapters with their addresses and link state.

    The settings UI offers these as a list so nobody has to know, or type, an
    adapter name.
    """
    entries: list[InterfaceInfo] = []
    try:
        names = [name for _, name in socket.if_nameindex()]
    except (OSError, AttributeError):
        names = []

    for name in names:
        addresses: list[str] = []
        up = True
        loopback = name == "lo"
        if IS_LINUX:
            ipv4 = _linux_interface_ipv4(name)
            if ipv4:
                addresses.append(ipv4)
            flags = _linux_interface_flags(name)
            if flags:
                up = bool(flags & _IFF_UP) and bool(flags & _IFF_RUNNING)
                loopback = bool(flags & _IFF_LOOPBACK)
        entries.append(InterfaceInfo(name, addresses, up, loopback))

    if not entries or IS_WINDOWS:
        # Without adapter names the user can still pin a source address.
        for address in _host_addresses():
            if not any(address in entry.addresses for entry in entries):
                entries.append(InterfaceInfo(address, [address], True, False))
    return entries


def _host_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    return addresses


def default_route_interface() -> str:
    """The adapter the system would use by default, for annotation only."""
    if IS_LINUX:
        try:
            with open("/proc/net/route", "r", encoding="ascii") as handle:
                next(handle, None)          # header
                for line in handle:
                    fields = line.split()
                    # Destination 00000000 is the default route.
                    if len(fields) > 2 and fields[1] == "00000000":
                        return fields[0]
        except (OSError, StopIteration):
            return ""
        return ""

    # Elsewhere, ask the routing table which adapter reaches the internet by
    # opening an unconnected UDP socket — no packet is actually sent.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("198.51.100.1", 9))       # TEST-NET-2, never routed
        local = probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()
    for entry in describe_interfaces():
        if local in entry.addresses:
            return entry.name
    return local


#: Receive buffer asked of the kernel for each connection, in bytes.
#:
#: The default on most systems caps a single TCP connection well below a fast
#: line's rate once the round trip is long, so every connection hits the same
#: ceiling and adding more stops helping. Four megabytes is generous without
#: being reckless: sixteen connections ask for 64 MB of buffer at most, and a
#: kernel that declines simply grants less.
_RECEIVE_BUFFER = 4 << 20


def _bind_to_interface(sock: socket.socket, interface: str, family: int) -> None:
    """Force this socket out through ``interface`` (name or literal address)."""
    if not interface:
        return

    # A literal IP is always honoured by binding the source address.
    try:
        ipaddress.ip_address(interface)
        sock.bind((interface, 0))
        return
    except ValueError:
        pass
    except OSError as exc:
        raise NetworkError(f"cannot bind to source address {interface}: {exc}") from exc

    if IS_LINUX:
        try:
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode("utf-8") + b"\0"
            )
            return
        except (OSError, AttributeError):
            # SO_BINDTODEVICE needs CAP_NET_RAW; fall back to source-address binding.
            address = _linux_interface_ipv4(interface)
            if address and family == socket.AF_INET:
                sock.bind((address, 0))
                return
            raise NetworkError(
                f"cannot bind to interface {interface!r} "
                "(needs CAP_NET_RAW, or the interface has no IPv4 address)"
            )
    elif IS_MACOS:
        try:
            index = socket.if_nametoindex(interface)
        except OSError as exc:
            raise NetworkError(f"unknown interface {interface!r}") from exc
        option = _IPV6_BOUND_IF if family == socket.AF_INET6 else _IP_BOUND_IF
        level = socket.IPPROTO_IPV6 if family == socket.AF_INET6 else socket.IPPROTO_IP
        try:
            sock.setsockopt(level, option, index)
            return
        except OSError as exc:
            raise NetworkError(f"cannot bind to interface {interface!r}: {exc}") from exc
    else:
        for name, addresses in list_interfaces():
            if name == interface and addresses:
                sock.bind((addresses[0], 0))
                return
        raise NetworkError(f"interface {interface!r} has no bindable address")


# ----------------------------------------------------------------------
# proxy handshakes
# ----------------------------------------------------------------------
def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            raise ProxyError("proxy closed the connection during handshake")
        buffer += chunk
    return buffer


def _socks5_connect(sock: socket.socket, proxy: ProxyEntry, host: str, port: int) -> None:
    """RFC 1928 CONNECT with optional RFC 1929 username/password auth."""
    methods = b"\x00"
    if proxy.username:
        methods += b"\x02"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)

    version, method = _recv_exact(sock, 2)
    if version != 5:
        raise ProxyError(f"bad SOCKS version from proxy: {version}", proxy.id)
    if method == 0xFF:
        raise ProxyError("proxy rejected all offered authentication methods", proxy.id)

    if method == 2:
        if not proxy.username:
            raise ProxyError("proxy demands authentication but none is configured", proxy.id)
        username = proxy.username.encode("utf-8")
        password = proxy.password.encode("utf-8")
        sock.sendall(
            b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password
        )
        _, status = _recv_exact(sock, 2)
        if status != 0:
            raise ProxyError("SOCKS5 authentication failed", proxy.id)
    elif method != 0:
        raise ProxyError(f"unsupported SOCKS5 auth method {method}", proxy.id)

    # socks5h defers name resolution to the proxy; socks5 resolves locally.
    request = b"\x05\x01\x00"
    if proxy.scheme is ProxyScheme.SOCKS5H:
        encoded = host.encode("idna") if not host.isascii() else host.encode("ascii")
        request += b"\x03" + bytes([len(encoded)]) + encoded
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                resolved = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            except OSError as exc:
                raise ProxyError(f"cannot resolve {host}: {exc}", proxy.id) from exc
            address = ipaddress.ip_address(resolved[0][4][0])
        if address.version == 4:
            request += b"\x01" + address.packed
        else:
            request += b"\x04" + address.packed
    request += struct.pack(">H", port)
    sock.sendall(request)

    reply = _recv_exact(sock, 4)
    if reply[0] != 5:
        raise ProxyError("malformed SOCKS5 reply", proxy.id)
    if reply[1] != 0:
        raise ProxyError(
            f"SOCKS5 refused: {_SOCKS5_ERRORS.get(reply[1], f'code {reply[1]}')}", proxy.id
        )
    atyp = reply[3]
    if atyp == 1:
        _recv_exact(sock, 4)
    elif atyp == 3:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length)
    elif atyp == 4:
        _recv_exact(sock, 16)
    else:
        raise ProxyError(f"unsupported SOCKS5 address type {atyp}", proxy.id)
    _recv_exact(sock, 2)  # bound port


def _http_connect(sock: socket.socket, proxy: ProxyEntry, host: str, port: int,
                  user_agent: str) -> None:
    """Open a CONNECT tunnel through an HTTP(S) proxy."""
    target = f"{host}:{port}"
    lines = [
        f"CONNECT {target} HTTP/1.1",
        f"Host: {target}",
        f"User-Agent: {user_agent}",
        "Proxy-Connection: Keep-Alive",
    ]
    if proxy.username:
        token = base64.b64encode(
            f"{proxy.username}:{proxy.password}".encode("utf-8")
        ).decode("ascii")
        lines.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyError("proxy closed the connection during CONNECT", proxy.id)
        buffer += chunk
        if len(buffer) > 65536:
            raise ProxyError("proxy sent an oversized CONNECT response", proxy.id)

    status_line = buffer.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise ProxyError(f"malformed CONNECT response: {status_line!r}", proxy.id)
    status = int(parts[1])
    if status != 200:
        raise ProxyError(f"proxy CONNECT failed: {status_line.strip()}", proxy.id)


# ----------------------------------------------------------------------
# factory
# ----------------------------------------------------------------------
@dataclass(slots=True)
class NetworkProfile:
    """Everything that decides *how* a connection is made."""

    proxy: ProxyEntry | None = None
    interface: str = ""
    timeout: float = 30.0
    connect_timeout: float = 8.0
    """How long to wait for a connection to be *established*, as opposed to
    how long a stalled read is tolerated once one is.

    They were the same thirty seconds, and that is why Pause did nothing for
    half a minute on a download stuck at "connecting": there is no response to
    abandon before a socket is open, so `_abandon_connections` has nothing to
    close and the worker sits inside `connect()` until it gives up. A host that
    is going to answer answers quickly; a host that is not should be found out
    quickly. A slow *transfer* is still given the full `timeout`.
    """
    verify_tls: bool = True
    user_agent: str = "IXD/1.0"
    prefer_ipv6: bool = True
    proxy_bypass: tuple[str, ...] = ()
    """Hosts that must be reached directly, even when a proxy is configured.

    A system proxy almost always excludes the loopback and the local network;
    honouring that is what keeps the application's own control socket working
    while a corporate proxy is in force.
    """

    def proxy_for(self, host: str) -> ProxyEntry | None:
        """The proxy to use for ``host`` — ``None`` when it is bypassed."""
        if self.proxy is None:
            return None
        if self.proxy_bypass and host_is_bypassed(host, self.proxy_bypass):
            return None
        return self.proxy

    def describe(self) -> str:
        bits = []
        bits.append(self.proxy.as_url() if self.proxy else "direct")
        if self.interface:
            bits.append(f"via {self.interface}")
        return " ".join(bits)


def build_ssl_context(verify: bool = True) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        context.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:
        pass
    return context


class SocketFactory:
    """Creates TCP sockets honouring the proxy and interface policy."""

    def __init__(self, profile: NetworkProfile | None = None) -> None:
        self.profile = profile or NetworkProfile()

    def _raw_connect(self, host: str, port: int) -> socket.socket:
        """Plain TCP connect with interface binding and happy-eyeballs ordering."""
        last_error: Exception | None = None
        try:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except OSError as exc:
            raise NetworkError(f"cannot resolve {host}: {exc}") from exc

        if not self.profile.prefer_ipv6:
            infos.sort(key=lambda i: i[0] != socket.AF_INET)

        for family, socktype, proto, _canonname, address in infos:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                # The shorter one until the connection exists, then the full
                # one for reading. See `NetworkProfile.connect_timeout`.
                sock.settimeout(max(1.0, self.profile.connect_timeout))
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # A receive buffer large enough that a fast link is not paced
                # by the kernel's default window. On a long, fat path the
                # default caps a single connection well below the line rate,
                # which is the difference between sixteen connections adding up
                # and sixteen connections each hitting the same ceiling.
                #
                # Requested, not required: a kernel that will not grant it says
                # so and the transfer proceeds on whatever it does grant.
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF,
                                    _RECEIVE_BUFFER)
                except OSError:
                    pass
                _bind_to_interface(sock, self.profile.interface, family)
                sock.connect(address)
                sock.settimeout(self.profile.timeout)
                return sock
            except (OSError, NetworkError) as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        raise NetworkError(f"cannot connect to {host}:{port}: {last_error}")

    def connect(self, host: str, port: int) -> socket.socket:
        """Return a socket connected (possibly tunnelled) to ``host:port``."""
        proxy = self.profile.proxy_for(host)
        if proxy is None:
            return self._raw_connect(host, port)

        sock = self._raw_connect(proxy.host, proxy.port)
        try:
            if proxy.scheme in (ProxyScheme.SOCKS5, ProxyScheme.SOCKS5H):
                _socks5_connect(sock, proxy, host, port)
            else:
                if proxy.scheme is ProxyScheme.HTTPS:
                    # TLS between us and the proxy, then CONNECT inside it.
                    context = build_ssl_context(self.profile.verify_tls)
                    sock = context.wrap_socket(sock, server_hostname=proxy.host)
                _http_connect(sock, proxy, host, port, self.profile.user_agent)
        except Exception:
            sock.close()
            raise
        return sock

    def connect_tls(self, host: str, port: int) -> socket.socket:
        """Connect and wrap in TLS with SNI set to ``host``."""
        sock = self.connect(host, port)
        context = build_ssl_context(self.profile.verify_tls)
        try:
            return context.wrap_socket(sock, server_hostname=host)
        except (ssl.SSLError, OSError) as exc:
            sock.close()
            raise NetworkError(f"TLS handshake with {host} failed: {exc}") from exc

    def connect_proxy_endpoint(self) -> socket.socket:
        """Connect straight to the proxy without tunnelling.

        Plain-HTTP requests are forwarded by an HTTP proxy in absolute-URI
        form, which is more widely permitted than CONNECT to port 80.
        """
        proxy = self.profile.proxy
        if proxy is None:
            raise NetworkError("no proxy configured")
        sock = self._raw_connect(proxy.host, proxy.port)
        if proxy.scheme is ProxyScheme.HTTPS:
            context = build_ssl_context(self.profile.verify_tls)
            try:
                return context.wrap_socket(sock, server_hostname=proxy.host)
            except (ssl.SSLError, OSError) as exc:
                sock.close()
                raise ProxyError(f"TLS handshake with proxy failed: {exc}", proxy.id) from exc
        return sock

    def proxy_auth_header(self) -> dict[str, str]:
        proxy = self.profile.proxy
        if proxy is None or not proxy.username:
            return {}
        token = base64.b64encode(
            f"{proxy.username}:{proxy.password}".encode("utf-8")
        ).decode("ascii")
        return {"Proxy-Authorization": f"Basic {token}"}

    def needs_absolute_uri(self, host: str = "") -> bool:
        """HTTP proxies want the absolute URI for plain-HTTP requests."""
        proxy = self.profile.proxy_for(host) if host else self.profile.proxy
        return proxy is not None and proxy.scheme in (ProxyScheme.HTTP, ProxyScheme.HTTPS)
