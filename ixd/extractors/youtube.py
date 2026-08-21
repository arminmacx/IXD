"""Native YouTube extraction via the InnerTube player API.

No external binary is involved.  The plugin talks to the same private endpoint
the official clients use, then normalises ``streamingData`` into
:class:`MediaFormat` objects the chunking engine can download directly.

Two mechanisms are implemented:

* **Client rotation** — several InnerTube client identities are tried in turn.
  The mobile clients return plain ``url`` fields, which is the fast path.
* **Signature deciphering** — when a response instead returns
  ``signatureCipher``, the player JavaScript is fetched and its transform
  pipeline (reverse / splice / swap) is parsed out and replayed in Python.

Known boundary: YouTube additionally applies an ``n`` query-parameter
throttling transform whose implementation is an obfuscated JavaScript routine.
Replaying it faithfully needs a JS interpreter, which is out of scope here, so
the extractor prefers client identities that do not require it.  This is
documented in ``context.md`` rather than silently ignored.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field, replace
from typing import Any

from ..core.errors import ExtractionError
from ..core.models import MediaFormat, MediaInfo
from ..core.protobuf import parse
from .base import Extractor, register

INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/player"

#: The player endpoint's key is **not kept here**.
#:
#: It is not a credential. YouTube publishes it in the watch page itself —
#: `ytcfg.set({"INNERTUBE_API_KEY": …})` — the same value for every visitor,
#: and `_live_config` already reads it from there and prefers it over anything
#: else. A copy in this file was therefore a stale duplicate of page data.
#:
#: It was also an `AIza…` literal in a public repository, which GitHub's secret
#: scanning flags as a leaked Google API key. There is nothing to rotate: it is
#: not ours, it grants access to nothing of ours, and every visitor to
#: youtube.com is served it. But an alert nobody can act on is an alert that
#: teaches people to ignore alerts, so the literal is gone and the page is the
#: only source. The request omits the parameter entirely when the page did not
#: give one.

_ITAG_HINTS: dict[int, tuple[str, str, str]] = {
    # itag: (extension, vcodec, acodec) for common progressive streams
    18: ("mp4", "h264", "aac"),
    22: ("mp4", "h264", "aac"),
    37: ("mp4", "h264", "aac"),
    43: ("webm", "vp8", "vorbis"),
}


@dataclass(slots=True)
class InnerTubeClient:
    """One client identity the player endpoint accepts."""

    key: str
    client_name: str
    client_version: str
    user_agent: str
    client_id: int
    extra_context: dict[str, Any] = field(default_factory=dict)
    #: Filled in from the watch page; empty until one is read.
    api_key: str = ""

    def context(self) -> dict[str, Any]:
        client: dict[str, Any] = {
            "clientName": self.client_name,
            "clientVersion": self.client_version,
            "hl": "en",
            "gl": "US",
            "timeZone": "UTC",
            "utcOffsetMinutes": 0,
        }
        client.update(self.extra_context)
        return {"client": client}


DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

#: Ordered by how likely they are to return directly usable URLs.
#:
#: Versions pinned here are a starting point, not a contract: the API rejects
#: an identity whose version has aged out (``FAILED_PRECONDITION``, or a flat
#: "no longer supported"), and hard-coded values age out by definition. The
#: extractor therefore re-stamps the browser identities from the live ``ytcfg``
#: block on the watch page before using them — see ``_apply_live_config``.
CLIENTS: tuple[InnerTubeClient, ...] = (
    InnerTubeClient(
        # First by a distance: this identity is answered without credentials
        # and its URLs carry no throttling nonce, so they are usable exactly as
        # issued. The version matters — an older one is refused outright.
        key="android",
        client_name="ANDROID",
        client_version="20.10.38",
        client_id=3,
        user_agent="com.google.android.youtube/20.10.38 (Linux; U; Android 15) gzip",
        extra_context={"androidSdkVersion": 35, "osName": "Android",
                       "osVersion": "15"},
    ),
    InnerTubeClient(
        key="tv",
        client_name="TVHTML5",
        client_version="7.20250219.14.00",
        client_id=7,
        user_agent="Mozilla/5.0 (ChromiumStylePlatform) Cobalt/25.master.0-qa "
                   "(unlike Gecko) v8/8.8.278.8-jit gles Starboard/15",
    ),
    InnerTubeClient(
        key="android_vr",
        client_name="ANDROID_VR",
        client_version="1.62.27",
        client_id=28,
        user_agent="com.google.android.apps.youtube.vr.oculus/1.62.27 "
                   "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip",
        extra_context={
            "deviceMake": "Oculus", "deviceModel": "Quest 3",
            "osName": "Android", "osVersion": "12L", "androidSdkVersion": 32,
        },
    ),
    InnerTubeClient(
        key="ios",
        client_name="IOS",
        client_version="20.10.4",
        client_id=5,
        user_agent="com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X)",
        extra_context={
            "deviceMake": "Apple",
            "deviceModel": "iPhone16,2",
            "osName": "iPhone",
            "osVersion": "18.3.2.22D82",
        },
    ),
    InnerTubeClient(
        key="web_embedded",
        client_name="WEB_EMBEDDED_PLAYER",
        client_version="1.20250219.01.00",
        client_id=56,
        user_agent=DESKTOP_USER_AGENT,
    ),
    InnerTubeClient(
        key="mweb",
        client_name="MWEB",
        client_version="2.20250219.01.00",
        client_id=2,
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Mobile/15E148 Safari/604.1",
    ),
    InnerTubeClient(
        key="web",
        client_name="WEB",
        client_version="2.20250219.01.00",
        client_id=1,
        user_agent=DESKTOP_USER_AGENT,
    ),
)

#: Client identities whose version is published in the watch page's ``ytcfg``.
_LIVE_VERSION_CLIENTS = frozenset({"WEB", "MWEB", "WEB_EMBEDDED_PLAYER"})


# ----------------------------------------------------------------------
# signature deciphering
# ----------------------------------------------------------------------
class SignatureDecipher:
    """Replays the player's signature transform pipeline in Python.

    The player JS defines a top-level function that splits the signature into
    characters and applies a sequence of three primitive operations from a
    helper object.  Both the function and the helper are located by pattern,
    the operations are classified by their bodies, and the resulting program is
    executed natively.
    """

    _MAIN_PATTERNS = (
        r"\b(?P<name>[a-zA-Z0-9_$]+)\s*=\s*function\(\s*(?P<arg>[a-zA-Z0-9_$]+)\s*\)\s*"
        r"\{\s*(?P=arg)\s*=\s*(?P=arg)\.split\(\s*(?:\"\"|'')\s*\)\s*;(?P<body>.+?)"
        r"return\s+(?P=arg)\.join\(\s*(?:\"\"|'')\s*\)\s*\}",
        r"(?:function\s+(?P<name2>[a-zA-Z0-9_$]+)|(?P<name3>[a-zA-Z0-9_$]+)\s*=\s*function)"
        r"\(\s*(?P<arg2>[a-zA-Z0-9_$]+)\s*\)\s*\{\s*(?P=arg2)\s*=\s*(?P=arg2)"
        r"\.split\(\s*(?:\"\"|'')\s*\)\s*;(?P<body2>.+?)return\s+(?P=arg2)"
        r"\.join\(\s*(?:\"\"|'')\s*\)\s*\}",
    )

    def __init__(self, player_js: str) -> None:
        self.table = self._string_table(player_js)
        self.operations = self._parse(player_js)

    @staticmethod
    def _string_table(js: str) -> tuple[str, list[str]]:
        """The player's hoisted string table, when it has one.

        Recent players lift every string literal into one array —
        ``var A='back;replace;…;www.youtube.com'.split(";")`` — and then refer
        to them by index. `a.split("")` becomes ``a[A[21]](A[3])``, which is
        why every pattern here matching a literal `split("")` found nothing:
        there is no literal left to match.

        Returns ``(name, entries)``, or ``("", [])`` for a player without one.
        """
        match = re.search(
            r"var\s+([A-Za-z0-9_$]+)\s*=\s*'((?:[^'\\]|\\.)*)'\s*\.\s*split\("
            r"\s*['\"];['\"]\s*\)", js)
        if not match:
            return "", []
        try:
            raw = match.group(2).encode("utf-8").decode("unicode_escape")
        except (UnicodeDecodeError, ValueError):
            raw = match.group(2)
        return match.group(1), raw.split(";")

    def _resolve(self, text: str) -> str:
        """Replace ``A[12]`` with the string it stands for.

        Only exact, literal indices. An index the player computes at run time —
        ``A[D^3864]``, where `D` comes from the call — is left alone, because
        substituting a guess for it would classify a transform wrongly and
        silently produce a signature that is merely plausible.
        """
        name, entries = self.table
        if not name or not entries:
            return text

        def swap(match: "re.Match[str]") -> str:
            index = int(match.group(1))
            if 0 <= index < len(entries):
                return f'"{entries[index]}"'
            return match.group(0)

        return re.sub(re.escape(name) + r"\[(\d+)\]", swap, text)

    # -- parsing --------------------------------------------------------
    def _parse(self, js: str) -> list[tuple[str, int]]:
        body = ""
        for pattern in self._MAIN_PATTERNS:
            match = re.search(pattern, js, re.DOTALL)
            if match:
                groups = match.groupdict()
                body = groups.get("body") or groups.get("body2") or ""
                if body:
                    break
        if not body:
            raise ExtractionError(self._why_not_found(js))

        calls = re.findall(r"([a-zA-Z0-9_$]+)\.([a-zA-Z0-9_$]+)\(\s*\w+\s*,\s*(\d+)\s*\)", body)
        if not calls:
            raise ExtractionError("signature function contains no transform calls")

        helper_name = calls[0][0]
        helper_body = self._helper_body(js, helper_name)
        classified = self._classify(helper_body)

        operations: list[tuple[str, int]] = []
        for _object_name, method, argument in calls:
            kind = classified.get(method)
            if kind is None:
                raise ExtractionError(f"unknown signature transform: {method}")
            operations.append((kind, int(argument)))
        return operations

    def _why_not_found(self, js: str) -> str:
        """Say which obfuscation defeated the search, not merely that one did.

        "Could not locate the signature function" covered three different
        players and sent several sessions looking in the wrong place. The one
        measured on 2026-08-12 (`40e2f4f3`) hoists its strings into a table and
        then indexes that table with values it computes at run time:

            Y=H[A[D^3845]](A[3]), P1[A[0]](Y,D^3867), P1[A[D^3864]](Y,D^3897),
            … E=Y[A[D^3872]](A[3])

        `D` comes from the caller's own arguments, so the method names do not
        exist anywhere in the file — they are produced while the player runs.
        No pattern can read that; only an engine that executes it can, which is
        what a browser is and what this tree deliberately is not.
        """
        name, entries = self.table
        if name and re.search(re.escape(name) + r"\[[A-Za-z_$][\w$]*\s*\^", js):
            return (
                "this player computes its signature function's method names "
                "while it runs — it holds its strings in a table and indexes "
                f"that table as `{name}[D^1234]`, where D comes from the call "
                "— so the names exist nowhere in the file and no pattern can "
                "find them. Running the player is the only way to read it."
            )
        if name:
            return (
                "this player holds its strings in a table and the signature "
                "function was not found among them"
            )
        return "could not locate the signature function in player JS"

    @staticmethod
    def _helper_body(js: str, name: str) -> str:
        """Extract ``var <name>={...};`` with brace matching."""
        match = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*\{", js)
        if not match:
            raise ExtractionError(f"helper object {name} not found in player JS")
        start = match.end() - 1
        depth = 0
        for index in range(start, len(js)):
            character = js[index]
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return js[start:index + 1]
        raise ExtractionError(f"helper object {name} is not balanced")

    def _classify(self, helper_body: str) -> dict[str, str]:
        """Map each helper method name onto reverse / splice / swap.

        Resolved through the string table first: a helper written as
        ``Xr:function(J){J[A[29]]()}`` says nothing to a search for the word
        "reverse" until `A[29]` is read as the string it stands for.
        """
        helper_body = self._resolve(helper_body)
        classified: dict[str, str] = {}
        for match in re.finditer(
            r"([a-zA-Z0-9_$]+)\s*:\s*function\s*\([^)]*\)\s*\{", helper_body
        ):
            name = match.group(1)
            start = match.end() - 1
            depth = 0
            end = start
            for index in range(start, len(helper_body)):
                character = helper_body[index]
                if character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
            body = helper_body[start:end + 1]
            if "reverse" in body:
                classified[name] = "reverse"
            elif "splice" in body:
                classified[name] = "splice"
            elif "%" in body and "length" in body:
                classified[name] = "swap"
            elif "unshift" in body:
                classified[name] = "unshift"
        return classified

    # -- execution ------------------------------------------------------
    def decipher(self, signature: str) -> str:
        characters = list(signature)
        for kind, argument in self.operations:
            if kind == "reverse":
                characters.reverse()
            elif kind == "splice":
                del characters[:argument]
            elif kind == "swap":
                position = argument % len(characters)
                characters[0], characters[position] = characters[position], characters[0]
            elif kind == "unshift":
                shift = argument % len(characters)
                characters = characters[shift:] + characters[:shift]
        return "".join(characters)


def _parse_xtags(raw: Any) -> dict[str, str]:
    """Decode a stream's track tags into ``{name: value}``.

    These are what tell an original audio track from a machine dubbing, and
    the site publishes them **base64url-encoded protobuf** — a repeated
    ``(name, value)`` message — not the ``name=value:name=value`` text they
    were being read as. Reading a real value that way yields nothing at all:

        ChQKBWFjb250EgtkdWJiZWQtYXV0bwoNCgRsYW5nEgVkZS1ERQ
        -> acont=dubbed-auto, lang=de-DE

    and the empty result made a German auto-dub indistinguishable from an
    untagged original — so it was treated as the default track and won the
    tie-break on bitrate. That is a video downloaded in the wrong language,
    discovered on playback, and it is the reported fault twice over.

    The plain-text form is still accepted, because costing nothing is cheaper
    than establishing that no response anywhere uses it.
    """
    text = str(raw or "")
    if not text:
        return {}

    tags: dict[str, str] = {}
    try:
        blob = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        entries = parse(blob).get(1)
        for entry in (entries if isinstance(entries, list) else [entries]):
            if not isinstance(entry, bytes):
                continue
            pair = parse(entry)
            name, value = pair.get(1), pair.get(2)
            if isinstance(name, bytes) and isinstance(value, bytes):
                tags[name.decode("utf-8", "replace")] = \
                    value.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - not encoded that way; try the other
        tags = {}
    if tags:
        return tags

    for part in text.split(":"):
        name, _, value = part.partition("=")
        if name and value:
            tags[name] = value
    return tags


def _stream_identity(media: MediaFormat) -> str:
    """What makes this stream *this* stream, rather than the itag alone.

    An itag names a rendition — "128k AAC" — and a video that publishes machine
    dubbings publishes every one of them under that same itag. The response for
    a video with eleven audio languages therefore holds eleven entries reading
    ``140``, differing only in their track tags, and any table keyed on the itag
    keeps whichever arrived first and discards the rest.

    That is what produced a German soundtrack on an English video three times
    over: the site lists the dubbings before the original, so the original was
    dropped here, and every later attempt to prefer it was choosing from a list
    it had already been removed from. Two sessions of ranking fixes were correct
    and had no effect for exactly this reason.

    For media with one audio track — everything else — the track key is empty
    and this is the itag, so nothing about the ordinary case changes.
    """
    return f"{media.format_id}\x00{media.audio_track_key}"


def _header_end(entry: dict) -> int:
    """Last byte of a stream's header (initialisation + index segments).

    Adaptive media is delivered without it: the server assumes a player has
    already fetched the header separately, so a file assembled purely from the
    streaming responses begins with a hole where its ``moov`` should be.
    """
    end = 0
    for key in ("initRange", "indexRange"):
        block = entry.get(key) or {}
        try:
            end = max(end, int(block.get("end") or 0))
        except (TypeError, ValueError):
            continue
    return end


# ----------------------------------------------------------------------
# extractor
# ----------------------------------------------------------------------
@register
class YouTubeExtractor(Extractor):
    name = "youtube"
    priority = 100
    url_patterns = (
        r"^https?://(?:[\w-]+\.)?youtube\.com/(?:watch|shorts|embed|live|v)\b",
        r"^https?://(?:[\w-]+\.)?youtube\.com/.*[?&]v=",
        r"^https?://youtu\.be/[\w-]{6,}",
        r"^https?://(?:www\.)?youtube-nocookie\.com/embed/",
    )

    _VIDEO_ID_PATTERNS = (
        r"[?&]v=([0-9A-Za-z_-]{11})",
        r"youtu\.be/([0-9A-Za-z_-]{11})",
        r"/shorts/([0-9A-Za-z_-]{11})",
        r"/embed/([0-9A-Za-z_-]{11})",
        r"/live/([0-9A-Za-z_-]{11})",
        r"/v/([0-9A-Za-z_-]{11})",
    )

    def __init__(self, client: Any, options: dict | None = None) -> None:
        super().__init__(client, options)
        #: Per-thread HTTP client, set while the identities are asked at once.
        self._local = threading.local()
        self._decipher: SignatureDecipher | None = None
        self._player_js_url: str = ""
        self._player_js_error: str = ""
        self._pages: dict[str, str] = {}
        self._warmed = False
        self._config: dict[str, Any] | None = None
        self._live_visitor_data: str = ""
        self._api_key: str = ""

    # -- optional attestation tokens ------------------------------------
    @property
    def _po_token(self) -> str:
        return str(self.options.get("po_token") or "")

    @property
    def _visitor_data(self) -> str:
        # A value the user pasted wins; otherwise use the one the site itself
        # handed out when we loaded the page, which is what a browser does.
        return str(self.options.get("visitor_data") or "") or self._live_visitor_data

    @property
    def _has_cookies(self) -> bool:
        return bool(self.options.get("cookies"))

    def _captured_session(self) -> tuple[bytes, bytes, str]:
        """The player's own session: (config, streamer context, endpoint).

        A proof of origin belongs to the session it was minted for, so a
        request rebuilt from *our* player response is a different session and
        the proof does not carry to it. The browser's own request is therefore
        reused as the template: its configuration blob and streamer context go
        back out unchanged, and only the stream and position are ours.

        Anything missing simply yields empty values — a captured request is an
        improvement when present, never a requirement.
        """
        encoded = str(self.options.get("player_request") or "")
        endpoint = str(self.options.get("player_endpoint") or "")
        if not encoded:
            return b"", b"", endpoint
        try:
            body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError):
            return b"", b"", endpoint
        try:
            fields = parse(body)
        except Exception:      # noqa: BLE001 - an unreadable body is simply unused
            return b"", b"", endpoint
        config = fields.get(5)
        streamer = fields.get(19)
        return (config if isinstance(config, bytes) else b"",
                streamer if isinstance(streamer, bytes) else b"",
                endpoint)

    # -- shared page + live configuration -------------------------------
    def _browser_headers(self, navigating: bool = True) -> dict[str, str]:
        """Headers a real browser sends for a top-level navigation."""
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8,"
                      "application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Upgrade-Insecure-Requests": "1",
        }
        if navigating:
            headers.update({
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            })
        return headers

    def _warm_session(self) -> None:
        """Obtain a session cookie before asking for a video page.

        A browser never arrives at a watch page cold — it already holds the
        cookies the site handed out on a previous visit. Requesting the watch
        page with an empty jar is answered with **HTTP 429**, which is not a
        rate limit in any meaningful sense: the very first request is refused.
        Fetching the home page first, so the site can set its session cookie,
        turns that 429 into a normal 200 with a full player response.

        Measured directly: cold jar → 429, regardless of how browser-like the
        headers are; after a home-page warm-up → 200 and 1.8 MB of page.
        """
        if self._warmed:
            return
        self._warmed = True      # one attempt per extraction, success or not
        if self.client.cookies.header_for("www.youtube.com"):
            return               # the caller supplied a real browser session
        try:
            self.client.get_text(
                "https://www.youtube.com/", self._browser_headers(), limit=4 << 20
            )
        except Exception:  # noqa: BLE001 - the watch page is tried regardless
            pass

    def _watch_page(self, video_id: str) -> str:
        """Fetch the watch page once and reuse it.

        Three separate things are read out of this document — the embedded
        player response, the player JavaScript URL and the live ``ytcfg``
        block. Fetching it once instead of three times is both faster and far
        less likely to trip rate limiting.
        """
        cached = self._pages.get(video_id)
        if cached is not None:
            return cached
        self._warm_session()
        html = self.client.get_text(
            f"https://www.youtube.com/watch?v={video_id}",
            self._browser_headers(), limit=8 << 20,
        )
        self._pages[video_id] = html
        return html

    def _live_config(self, video_id: str) -> dict[str, Any]:
        """Parse the ``ytcfg.set({...})` block the page ships with.

        It carries the API key, the client version the site is currently
        serving, and a freshly minted ``VISITOR_DATA``. Using those instead of
        constants is what stops the API rejecting us for looking like a client
        that no longer exists.
        """
        if self._config is not None:
            return self._config

        self._config = {}
        try:
            html = self._watch_page(video_id)
        except Exception:  # noqa: BLE001 - constants remain the fallback
            return self._config

        for marker in ('ytcfg.set(', 'ytcfg.set ('):
            position = html.find(marker)
            while position != -1:
                brace = html.find("{", position)
                if brace == -1:
                    break
                try:
                    data = json.loads(self._balanced_json(html, brace))
                except (ValueError, ExtractionError):
                    position = html.find(marker, position + len(marker))
                    continue
                if isinstance(data, dict) and "INNERTUBE_CLIENT_VERSION" in data:
                    self._config = data
                    break
                position = html.find(marker, position + len(marker))
            if self._config:
                break

        self._api_key = str(self._config.get("INNERTUBE_API_KEY") or "")
        self._live_visitor_data = str(self._config.get("VISITOR_DATA") or "")
        if not self._live_visitor_data:
            context = (self._config.get("INNERTUBE_CONTEXT") or {}).get("client") or {}
            self._live_visitor_data = str(context.get("visitorData") or "")
        return self._config

    def _apply_live_config(self, client: InnerTubeClient,
                           video_id: str) -> InnerTubeClient:
        """Re-stamp a browser identity with the version the site is serving."""
        if client.client_name not in _LIVE_VERSION_CLIENTS:
            return client
        config = self._live_config(video_id)
        version = str(config.get("INNERTUBE_CLIENT_VERSION") or "")
        if not version:
            return client
        if client.client_name != "WEB":
            # MWEB and the embedded player share the WEB build number but keep
            # their own leading component.
            head = client.client_version.split(".", 1)[0]
            version = head + version[version.find("."):] if "." in version else version
        return replace(
            client,
            client_version=version,
            api_key=self._api_key or client.api_key,
        )

    # -- helpers --------------------------------------------------------
    @classmethod
    def video_id(cls, url: str) -> str:
        for pattern in cls._VIDEO_ID_PATTERNS:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        # A bare 11-character id is also accepted.
        if re.fullmatch(r"[0-9A-Za-z_-]{11}", url.strip()):
            return url.strip()
        raise ExtractionError(f"cannot find a YouTube video id in {url!r}")

    def _call_player(self, video_id: str, client: InnerTubeClient,
                     start_seconds: int = 0) -> dict[str, Any]:
        client = self._apply_live_config(client, video_id)
        context = client.context()
        visitor_data = self._visitor_data
        if visitor_data:
            context["client"]["visitorData"] = visitor_data
        payload: dict[str, Any] = {
            "context": context,
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
            "playbackContext": {
                "contentPlaybackContext": {
                    "html5Preference": "HTML5_PREF_WANTS",
                    # Asking from a later position yields a link whose grant
                    # covers that part of the file, which is how a long video
                    # is fetched past the opening window it is issued with.
                    "startTimeSecs": int(start_seconds),
                    "currentPlaybackTimeSecs": int(start_seconds),
                },
            },
        }
        if self._po_token:
            # Proof-of-origin attestation, when the user supplies one.
            payload["serviceIntegrityDimensions"] = {"poToken": self._po_token}
        headers = {
            "Content-Type": "application/json",
            "User-Agent": client.user_agent,
            "X-YouTube-Client-Name": str(client.client_id),
            "X-YouTube-Client-Version": client.client_version,
            "Origin": "https://www.youtube.com",
            "Referer": f"https://www.youtube.com/watch?v={video_id}",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
        }
        if visitor_data:
            # The API pairs the session identity in the body with this header;
            # sending only one of the two is treated as inconsistent.
            headers["X-Goog-Visitor-Id"] = visitor_data
        # `key=` only when the page published one. An empty value is worse
        # than no parameter: it is a key the endpoint can reject, rather than a
        # request it reads the client context of.
        query = "prettyPrint=false"
        if client.api_key:
            query = f"key={urllib.parse.quote(client.api_key)}&" + query
        url = f"{INNERTUBE_URL}?{query}"
        # A client of this call's own when one is supplied. The identities are
        # asked concurrently, and one HTTP client reuses its connections — two
        # threads posting through the same one interleave on the wire.
        http = getattr(self._local, "client", None) or self.client
        # A player request is a read: the same identity asked twice returns the
        # same streams. Saying so lets it reuse a pooled connection instead of
        # handshaking afresh for every client identity in the rotation.
        with http.request("POST", url, headers, body=json.dumps(payload),
                          decode=True, idempotent=True) as response:
            text = response.text()
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ExtractionError(f"InnerTube returned invalid JSON: {exc}") from exc

    @staticmethod
    def _balanced_json(text: str, start: int) -> str:
        """Slice the complete JSON object beginning at ``text[start] == '{'``.

        Regexes cannot match balanced braces, and the player response contains
        deeply nested objects plus braces inside strings, so scan explicitly.
        """
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            character = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
        raise ExtractionError("unterminated player response object")

    def _player_response_from_page(self, video_id: str, refresh: bool = False
                                   ) -> dict[str, Any]:
        """Read ``ytInitialPlayerResponse`` straight out of the watch page.

        The InnerTube endpoint is frequently gated by attestation while the
        rendered page still embeds a usable player response, so this is a
        genuinely independent route rather than a duplicate of the API call.
        """
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if refresh:
            # The site answers the same request with different payloads: some
            # responses embed usable stream URLs, others only a server-side
            # streaming endpoint. Dropping the cache is what allows a second
            # look to land on a different one.
            self._pages.pop(video_id, None)

        last_error = ""
        for index, page_url in enumerate((
            f"https://www.youtube.com/watch?v={video_id}",
            f"https://www.youtube.com/embed/{video_id}",
        )):
            try:
                html = (self._watch_page(video_id) if index == 0
                        else self.client.get_text(page_url, headers, limit=8 << 20))
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
            for marker in ("ytInitialPlayerResponse", "var ytInitialPlayerResponse"):
                position = html.find(marker)
                while position != -1:
                    brace = html.find("{", position)
                    if brace == -1:
                        break
                    try:
                        return json.loads(self._balanced_json(html, brace))
                    except (ValueError, ExtractionError):
                        position = html.find(marker, position + 1)
                        continue
                if position != -1:
                    break
        raise ExtractionError(
            f"no player response embedded in the watch page ({last_error or 'not found'})"
        )

    def _load_player_js(self, video_id: str) -> None:
        """Fetch and parse the player JS so signatures can be deciphered."""
        if self._decipher is not None:
            return
        # A player script that could not be fetched or parsed will not become
        # fetchable between one format entry and the next, and the script is
        # a couple of megabytes: retrying it per entry costs a download apiece
        # for the same answer.
        if self._player_js_error:
            raise ExtractionError(self._player_js_error)
        try:
            self._load_player_js_now(video_id)
        except Exception as exc:  # noqa: BLE001 - remembered, then re-raised
            self._player_js_error = str(exc) or exc.__class__.__name__
            raise

    def _load_player_js_now(self, video_id: str) -> None:
        html = self._watch_page(video_id)
        path = self._first(r'"jsUrl"\s*:\s*"([^"]+)"', html)
        if not path:
            path = self._first(r'src="([^"]*/base\.js)"', html)
        if not path:
            raise ExtractionError("could not locate the player JavaScript URL")
        self._player_js_url = urllib.parse.urljoin("https://www.youtube.com", path)
        player_js = self.client.get_text(self._player_js_url)
        self._decipher = SignatureDecipher(player_js)

    def _resolve_url(self, entry: dict[str, Any], video_id: str) -> str:
        """Return a playable URL, deciphering ``signatureCipher`` when present."""
        direct = entry.get("url")
        if direct:
            return self._attested(direct)

        cipher_text = entry.get("signatureCipher") or entry.get("cipher")
        if not cipher_text:
            return ""

        parsed = urllib.parse.parse_qs(cipher_text)
        base_url = (parsed.get("url") or [""])[0]
        signature = (parsed.get("s") or [""])[0]
        parameter = (parsed.get("sp") or ["signature"])[0]
        if not base_url or not signature:
            return self._attested(base_url)

        self._load_player_js(video_id)
        assert self._decipher is not None
        deciphered = self._decipher.decipher(signature)
        separator = "&" if "?" in base_url else "?"
        return self._attested(
            f"{base_url}{separator}{parameter}={urllib.parse.quote(deciphered)}")

    def _attested(self, url: str) -> str:
        """Carry the proof of origin on the address itself.

        The token was being presented to the **API** — as
        ``serviceIntegrityDimensions.poToken`` on the player request — and to
        the streaming session, and never on an ordinary ``videoplayback``
        address, which takes it as the ``pot`` query parameter. So the two
        routes to the same CDN were not equally credentialled, and the field
        log says exactly that: every server-driven transfer completed, every
        plain GET was answered **403**, whatever headers accompanied it and
        whether the address came from extraction or from the browser's own
        capture.

        Nothing is invented here. The token is the one the browser minted for
        this visitor and handed over; this puts it where an ordinary request
        carries it.
        """
        token = self._po_token
        if not token or not url or "videoplayback" not in url:
            return url
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return url
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(key == "pot" for key, _ in query):
            return url
        query.append(("pot", token))
        return urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, parts.path,
            urllib.parse.urlencode(query), parts.fragment,
        ))

    # -- format normalisation -------------------------------------------
    #: The audio-track fields of :class:`MediaFormat`, in the order
    #: :meth:`_audio_track` returns them.
    _AUDIO_TRACK_FIELDS = ("audio_is_default", "audio_language", "audio_kind",
                           "audio_track_id", "audio_variant", "audio_tags")

    @staticmethod
    def _audio_track(entry: dict[str, Any]) -> tuple[bool, str, str, str, str, str]:
        """``(is_default, language, kind, track_id, variant, tags)`` for a stream.

        A video may publish several audio tracks — the original plus machine
        dubbings — which are otherwise identical in container, codec and rough
        bitrate. The site marks them in two places, and neither is always
        present: an ``audioTrack`` object, and an ``xtags`` string carrying
        ``acont`` (content type) and ``lang``. Both are read, because choosing
        on bitrate alone leaves the language of the finished file to chance.

        The raw tag string comes back untouched as well. It is what tells two
        entries apart when everything else about them matches, so it is the
        stream's identity and not merely a description of it.

        A language is published more than once in its own right: alongside the
        plain mix there is a loudness-compressed one (``drc``) and a boosted one
        (``vb``). They are the same words in the same language, so the earlier
        checks pass them, and their bitrates differ by tens of bits per second —
        enough for a tie-break on bitrate to pick one of the processed mixes
        over the one the site actually plays.
        """
        track = entry.get("audioTrack") or {}
        track_id = str(track.get("id") or "")
        language = track_id.split(".")[0]
        is_default = track.get("audioIsDefault")

        raw_tags = str(entry.get("xtags") or "")
        tags = _parse_xtags(raw_tags)
        if tags.get("lang"):
            language = language or tags["lang"]
        content = tags.get("acont", "")
        # "original", "dubbed-auto", "descriptive", …
        kind = "" if not content else (
            "original" if content == "original"
            else ("dubbed" if content.startswith("dubbed") else content)
        )
        variant = next((name for name in ("drc", "vb") if tags.get(name)), "")

        if is_default is None:
            # Nothing said either way: an entry that names no track at all is
            # the ordinary one, and an explicitly dubbed entry is not.
            is_default = kind != "dubbed"
        return bool(is_default), language, kind, track_id, variant, raw_tags

    def _formats_from(self, streaming_data: dict[str, Any], video_id: str,
                      client: InnerTubeClient,
                      duration_seconds: float = 0.0) -> list[MediaFormat]:
        formats: list[MediaFormat] = []
        entries = list(streaming_data.get("formats") or [])
        entries += list(streaming_data.get("adaptiveFormats") or [])

        for entry in entries:
            try:
                url = self._resolve_url(entry, video_id)
            except Exception:  # noqa: BLE001 - one unreadable entry, not a
                continue       # reason to abandon the rest of the list
            if not url:
                continue

            itag = int(entry.get("itag", 0) or 0)
            mime = entry.get("mimeType", "") or ""
            extension = "mp4"
            if "webm" in mime:
                extension = "webm"
            elif "mp4a" in mime and "video" not in mime:
                extension = "m4a"

            codecs = self._first(r'codecs="([^"]+)"', mime)
            codec_list = [c.strip() for c in codecs.split(",")] if codecs else []
            is_video = mime.startswith("video/")
            is_audio = mime.startswith("audio/")

            if itag in _ITAG_HINTS:
                extension, vcodec, acodec = _ITAG_HINTS[itag]
            else:
                vcodec = codec_list[0] if is_video and codec_list else "none"
                acodec = "none"
                if is_audio and codec_list:
                    acodec = codec_list[0]
                elif is_video and len(codec_list) > 1:
                    acodec = codec_list[1]

            try:
                filesize = int(entry.get("contentLength", 0) or 0)
            except ValueError:
                filesize = 0

            # What decides whether a URL serves the whole file is
            # ``ratebypass=yes``. Measured against this video: the progressive
            # URL carries it and answers at every offset from head to tail,
            # while the adaptive URLs do not and are refused about a third of
            # the way in. Both are signed into ``sparams``, so neither can be
            # added or removed — the flag can only be read.
            restricted = "ratebypass=yes" not in url
            note = f"itag {itag} · {client.key}"
            if restricted:
                note += " · partial access"

            formats.append(MediaFormat(
                format_id=str(itag or entry.get("itag") or len(formats)),
                url=url,
                ext=extension,
                protocol="https",
                width=int(entry.get("width", 0) or 0),
                height=int(entry.get("height", 0) or 0),
                fps=float(entry.get("fps", 0) or 0),
                tbr=float(entry.get("bitrate", 0) or 0) / 1000.0,
                vcodec=vcodec,
                acodec=acodec,
                filesize=filesize,
                quality_label=entry.get("qualityLabel", "") or "",
                note=note,
                restricted=restricted,
                refresh={"video_id": video_id, "itag": itag,
                         "client": client.key, "duration": duration_seconds},
                http_headers={"User-Agent": client.user_agent},
                **dict(zip(self._AUDIO_TRACK_FIELDS,
                            self._audio_track(entry))),
            ))
        # Unrestricted streams first, so "best available" never picks a URL
        # that cannot deliver the whole file when a complete one exists.
        formats.sort(key=lambda media: media.restricted)
        return formats

    #: A streaming session hands over roughly this much playback and no more,
    #: measured repeatedly against live videos. Anything longer cannot be
    #: obtained this way, which the format list has to say rather than
    #: discovering it a third of the way into a download.
    SABR_SESSION_SECONDS = 60

    def _sabr_formats(self, response: dict[str, Any], streaming_data: dict[str, Any],
                      client: InnerTubeClient, source: str = "") -> list[MediaFormat]:
        """Describe streams that are only obtainable from the server.

        When a response publishes no directly fetchable URL, the media is still
        available — the client asks the streaming endpoint for it instead. The
        details that exchange needs travel on the format so the transfer engine
        can carry it out.
        """
        from .sabr import SabrStream      # noqa: PLC0415 - avoids a cycle

        endpoint = streaming_data.get("serverAbrStreamingUrl") or ""
        config_blob = SabrStream.config_from_player_response(response)

        # When the browser's own request was captured, its session is used
        # instead of the one we would open. That session has already been
        # attested, which is the difference between a stream that runs to the
        # end and one the server stops about a minute in.
        captured_config, captured_streamer, captured_endpoint = self._captured_session()
        if captured_config:
            config_blob = captured_config
        if captured_endpoint:
            endpoint = captured_endpoint

        if not endpoint or not config_blob:
            return []

        encoded_config = base64.b64encode(config_blob).decode("ascii")
        encoded_streamer = (base64.b64encode(captured_streamer).decode("ascii")
                            if captured_streamer else "")
        try:
            duration = float((response.get("videoDetails") or {}).get("lengthSeconds") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        # An unattested session is served about a minute of media and then
        # refused, so a longer video cannot be promised in full — unless a
        # proof-of-origin token is in hand, which is what lifts the limit.
        beyond_session = (duration > self.SABR_SESSION_SECONDS
                          and not self._po_token)
        formats: list[MediaFormat] = []

        for entry in streaming_data.get("adaptiveFormats") or []:
            itag = int(entry.get("itag") or 0)
            mime = entry.get("mimeType", "") or ""
            codecs = self._first(r'codecs="([^"]+)"', mime)
            codec_list = [c.strip() for c in codecs.split(",")] if codecs else []
            is_audio = mime.startswith("audio/")
            extension = "webm" if "webm" in mime else ("m4a" if is_audio else "mp4")
            try:
                filesize = int(entry.get("contentLength") or 0)
            except (TypeError, ValueError):
                filesize = 0

            formats.append(MediaFormat(
                format_id=str(itag),
                url=endpoint,
                ext=extension,
                protocol="sabr",
                width=int(entry.get("width", 0) or 0),
                height=int(entry.get("height", 0) or 0),
                fps=float(entry.get("fps", 0) or 0),
                tbr=float(entry.get("bitrate", 0) or 0) / 1000.0,
                vcodec="none" if is_audio else (codec_list[0] if codec_list else "h264"),
                acodec=(codec_list[0] if is_audio and codec_list else "none"),
                filesize=filesize,
                quality_label=entry.get("qualityLabel", "") or "",
                note=f"itag {itag} · {client.key} · server-driven"
                     + (" · partial only" if beyond_session else ""),
                restricted=beyond_session,
                http_headers={"User-Agent": client.user_agent},
                **dict(zip(self._AUDIO_TRACK_FIELDS,
                            self._audio_track(entry))),
                sabr={
                    "endpoint": endpoint,
                    "config": encoded_config,
                    "itag": itag,
                    "last_modified": int(entry.get("lastModified") or 0),
                    "size": filesize,
                    "is_audio": is_audio,
                    "xtags": entry.get("xtags") or "",
                    "client_id": client.client_id,
                    "source": source or client.key,
                    "po_token": self._po_token,
                    # Positions in this protocol are times, not offsets, so
                    # resuming at a byte needs the running time to convert it.
                    "duration": duration,
                    # Sent back verbatim when present: it carries the proof of
                    # origin and the client identity it was issued against, and
                    # reassembling either by hand produces a session the proof
                    # does not apply to.
                    "streamer_context": encoded_streamer,
                    # The streaming server delivers media but not the file's
                    # header, assuming a player already fetched it. Its byte
                    # range is published here, and it is small enough to come
                    # over the ordinary URL even when that URL is restricted
                    # to an opening portion.
                    "header_url": entry.get("url", "") or "",
                    "header_end": _header_end(entry),
                },
            ))
        return formats

    #: Streaming endpoints, best first. A page-scraped session is refused
    #: outright by the streaming server unless it has been attested, while the
    #: mobile API hands the same session a usable — if bounded — grant. Since
    #: several sources describe the identical stream, the choice of endpoint
    #: decides whether the download works at all.
    #: Which streaming endpoint to ask, best first. The mobile ones answer an
    #: unattested session; the watch page's refuses it outright, so it is a
    #: last resort rather than a peer.
    _SABR_SOURCE_ORDER = ("android", "ios", "android_vr", "tv", "mweb",
                          "web_embedded", "web", "watch-page")

    #: The order to prefer when a proof of origin is in hand. A proof is minted
    #: by the *web* player, for the web client identity and the browser's
    #: visitor id. Presenting it against a mobile endpoint — whose session was
    #: opened under a different client with a different ustreamer config — does
    #: not attest that session, and the refusal is silent: the stream simply
    #: stops where an unattested one would have stopped, which is exactly how
    #: a working token looks like a useless one.
    _SABR_SOURCE_ORDER_ATTESTED = ("web", "mweb", "web_embedded", "watch-page",
                                   "android", "ios", "android_vr", "tv")

    @staticmethod
    def _header_sources(formats: list[MediaFormat]) -> dict[str, tuple[str, int]]:
        """Where each rendition's header bytes can be fetched from.

        A stream's initialisation and index segments never travel over the
        streaming session — the server assumes a player fetched them itself —
        so they have to come over an ordinary URL. The entry that produced a
        server-driven format frequently publishes no URL at all, which is why
        it is server-driven in the first place; taken alone that leaves those
        few kilobytes with no source, and the transfer runs to the last byte of
        the file and is then refused for a hole at byte zero.

        Any other description of the same rendition will do. The header sits in
        the opening kilobytes, so even a link the site will only serve the
        first portion of can deliver it in full.

        Keyed by the stream, not by the itag: a video's dubbings share an itag
        and each has a header of its own, so an itag-keyed table would hand one
        track the initialisation segment of another language's. That produces a
        file whose header describes bytes it does not contain.
        """
        urls: dict[str, str] = {}
        ends: dict[str, int] = {}
        for media in formats:
            key = _stream_identity(media)
            if media.sabr:
                # A server-driven entry's own ``url`` is the endpoint, not the
                # media, so only its recorded header URL is a candidate.
                candidate = str(media.sabr.get("header_url") or "")
                try:
                    end = int(media.sabr.get("header_end") or 0)
                except (TypeError, ValueError):
                    end = 0
                if end:
                    ends[key] = max(ends.get(key, 0), end)
            else:
                candidate = media.url
            if candidate and key not in urls:
                urls[key] = candidate
        return {key: (url, ends.get(key, 0)) for key, url in urls.items()}

    def _fill_headers_from_dash(self, formats: list[MediaFormat],
                                manifest_url: str) -> None:
        """Last resort for a header URL: the video's own DASH manifest.

        A rendition described by every response as server-driven and by none of
        them with a URL has nowhere to fetch its index from, and the file
        cannot be assembled without it. The manifest is a different publication
        of the same streams — one representation per itag, each with a plain
        ``BaseURL`` — so it supplies exactly the link that was missing.

        Fetched only when something actually needs it, and a manifest that
        cannot be read leaves the formats as they were: this is one more place
        to look, not a step the extraction depends on.
        """
        wanting = [media for media in formats
                   if media.sabr
                   and int(media.sabr.get("header_end") or 0)
                   and not media.sabr.get("header_url")]
        if not wanting or not manifest_url:
            return

        from . import dash                       # noqa: PLC0415 - avoids a cycle

        try:
            published = dash.fetch_formats(self.client, manifest_url,
                                           self._browser_headers(False))
        except Exception:  # noqa: BLE001 - an extra source, never a dependency
            return

        # A YouTube manifest names each representation by its itag, which is
        # what ties it back to the stream that is missing a link.
        #
        # An itag is only a sound tie when the manifest publishes it once. On a
        # video with machine dubbings it publishes one representation per audio
        # language, all named ``140``, and there is nothing in the manifest to
        # say which language each is. Guessing would give a track the
        # initialisation segment of another language's — a file whose header
        # contradicts its media, which no player reports as a language problem.
        # An ambiguous itag is therefore left alone: no header here fails
        # loudly and is diagnosable, the wrong header does not.
        counts: dict[str, int] = {}
        for media in published:
            if media.url:
                counts[media.format_id] = counts.get(media.format_id, 0) + 1
        by_itag = {media.format_id: media.url
                   for media in published
                   if media.url and counts.get(media.format_id) == 1}
        for media in wanting:
            url = by_itag.get(media.format_id, "")
            if url:
                media.sabr["header_url"] = url

    def _prefer_usable_sabr(self, formats: list[MediaFormat]) -> list[MediaFormat]:
        """Keep one server-driven entry per stream, from the best endpoint."""
        order = (self._SABR_SOURCE_ORDER_ATTESTED if self._po_token
                 else self._SABR_SOURCE_ORDER)

        # Done before anything is discarded: the entry that carries a usable
        # header URL is very often one of the copies about to be dropped, and
        # dropping it takes the only source for those bytes with it.
        sources = self._header_sources(formats)
        for media in formats:
            if not media.sabr:
                continue
            url, end = sources.get(_stream_identity(media), ("", 0))
            if url and not media.sabr.get("header_url"):
                media.sabr["header_url"] = url
            if end and not media.sabr.get("header_end"):
                media.sabr["header_end"] = end

        def rank(media: MediaFormat) -> int:
            source = (media.sabr or {}).get("source", "")
            try:
                return order.index(source)
            except ValueError:
                # Anything unrecognised — the watch page included — goes last.
                return len(order)

        # Every table below is keyed on the stream rather than on the itag. A
        # video's original audio and its dubbings all read ``140``, so an
        # itag-keyed table treats them as one stream and keeps a single
        # language — which is the whole of the German-soundtrack defect.
        best: dict[str, MediaFormat] = {}
        result: list[MediaFormat] = []
        for media in formats:
            if not media.sabr:
                result.append(media)
                continue
            key = _stream_identity(media)
            current = best.get(key)
            if current is None or rank(media) < rank(current):
                best[key] = media

        combined = result + list(best.values())

        # The same stream often appears twice: once as a link the site will
        # only serve part of, and once as a server-driven entry that can
        # deliver the whole thing. Showing both puts a greyed-out duplicate
        # next to every real choice, so the unusable copy is dropped.
        usable = {_stream_identity(f) for f in combined if not f.restricted}
        combined = [f for f in combined
                    if not (f.restricted and _stream_identity(f) in usable)]

        # What remains can still hold the same stream twice: a direct link the
        # site serves only the opening of, and the server-driven entry for the
        # same stream. They are one choice as far as the user is concerned, and
        # the server-driven one is the copy that can deliver the whole stream,
        # so the capped link is dropped rather than listed beside it.
        server_driven = {_stream_identity(f) for f in combined if f.sabr}
        return [f for f in combined
                if f.sabr
                or not (f.restricted and _stream_identity(f) in server_driven)]

    def _prefetch_clients(self, video_id: str,
                          clients: list[InnerTubeClient]
                          ) -> dict[str, Any]:
        """Ask every client identity at once, returning answers by key.

        Each thread gets an HTTP client of its own — one client reuses its
        connections, and two threads posting through the same one interleave on
        the wire. The cookie jar is shared on purpose: the session warmed for
        the watch page is what the identities are asked with.

        A failure is stored rather than raised, so the walk below reports it in
        exactly the place and order it always did.
        """
        if not clients:
            return {}
        # The watch page is fetched first and stamps the live client versions
        # onto the browser identities, so it must already have happened.
        answers: dict[str, Any] = {}
        lock = threading.Lock()

        def ask(client: InnerTubeClient) -> None:
            try:
                self._local.client = self.client.clone() \
                    if hasattr(self.client, "clone") else self.client
                answer: Any = self._call_player(video_id, client)
            except Exception as exc:  # noqa: BLE001 - reported by the walk
                answer = exc
            finally:
                self._local.client = None
            with lock:
                answers[client.key] = answer

        threads = [threading.Thread(target=ask, args=(client,),
                                    name=f"ixd-innertube-{client.key}",
                                    daemon=True)
                   for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30.0)
        return answers

    # -- entry point ----------------------------------------------------
    def extract(self, url: str) -> MediaInfo:
        video_id = self.video_id(url)
        errors: list[str] = []
        gated = False          # a response existed but its streams were withheld
        last_details: dict[str, Any] = {}
        collected: list[MediaFormat] = []
        seen_ids: set[str] = set()
        title = ""
        duration = 0.0
        thumbnail = ""
        streaming_extras: dict[str, str] = {}

        # The embedded watch-page response is tried first: it survives the
        # attestation gating that currently blocks the mobile API clients, and
        # it also supplies the title/duration metadata in the same request.
        bot_checked = False    # the account/IP was asked to prove it is human
        # The watch page is asked more than once on purpose: the same request
        # is answered with different payloads, and only some of them carry
        # stream URLs that can be fetched in full. It is tried first, and again
        # at the end if every stream found so far is restricted.
        strategies: list[tuple[str, InnerTubeClient, bool]] = [
            ("watch-page", CLIENTS[-1], True)
        ]
        strategies += [(client.key, client, False) for client in CLIENTS]
        strategies += [(f"watch-page retry {i}", CLIENTS[-1], True) for i in (1, 2)]

        # The identities are independent requests to the same endpoint, and
        # asking them one after another was most of the wait before the quality
        # menu appeared: measured at 12.2 seconds for one video, of which 8.2
        # was seven client calls taking their turn. They are asked at once
        # instead, and the walk below reads answers already in hand. Its order,
        # its early exit and its error reporting are unchanged — only the
        # waiting is shared.
        #
        # Filled on reaching the first identity, never before: the watch page
        # goes first for two reasons that both still hold. It warms the session
        # — arriving cold is answered `HTTP 429` on the very first request —
        # and it stamps the live client versions onto the browser identities,
        # which is what stops them ageing out. Asking them in advance would
        # undo both.
        prefetched: dict[str, Any] | None = None

        for label, client, from_page in strategies:
            if label.startswith("watch-page retry") and any(
                not media.restricted for media in collected
            ):
                break      # an unrestricted stream is already in hand

            try:
                if from_page:
                    response = self._player_response_from_page(
                        video_id, refresh=label.startswith("watch-page retry"))
                else:
                    if prefetched is None:
                        prefetched = self._prefetch_clients(video_id, [
                            candidate for _, candidate, page in strategies
                            if not page
                        ])
                    answer = prefetched.get(client.key)
                    if isinstance(answer, Exception):
                        raise answer
                    response = (answer if answer is not None
                                else self._call_player(video_id, client))
            except Exception as exc:  # noqa: BLE001 - try the next identity
                errors.append(f"{label}: {exc}")
                continue

            status = (response.get("playabilityStatus") or {})
            state = status.get("status", "")
            if state not in ("OK", "LIVE_STREAM_OFFLINE"):
                reason = status.get("reason") or status.get("messages") or state
                if "not a bot" in str(reason).lower() or state == "LOGIN_REQUIRED":
                    bot_checked = True
                errors.append(f"{label}: {reason}")
                continue

            details = response.get("videoDetails") or {}
            if details:
                last_details = details
                title = title or details.get("title", "")
                try:
                    duration = duration or float(details.get("lengthSeconds", 0) or 0)
                except ValueError:
                    pass
                thumbnails = ((details.get("thumbnail") or {}).get("thumbnails") or [])
                if thumbnails and not thumbnail:
                    thumbnail = thumbnails[-1].get("url", "")

            streaming_data = response.get("streamingData") or {}
            for key in ("hlsManifestUrl", "dashManifestUrl"):
                if streaming_data.get(key) and key not in streaming_extras:
                    streaming_extras[key] = streaming_data[key]

            entries = list(streaming_data.get("formats") or []) + \
                list(streaming_data.get("adaptiveFormats") or [])
            try:
                duration_seconds = float(details.get("lengthSeconds") or 0)
            except (TypeError, ValueError):
                duration_seconds = 0.0
            usable = self._formats_from(streaming_data, video_id, client,
                                        duration_seconds)

            # Which renditions a direct URL was actually published for. An
            # entry that resolved to a link the site serves only the opening of
            # does not count as covered: it cannot produce a whole file, so the
            # server-driven copy of that same rendition is still wanted.
            covered = {media.format_id for media in usable if not media.restricted}
            # The response routinely publishes a fetchable URL for the 360p
            # progressive stream and *nothing but* a streaming endpoint for
            # every adaptive rendition above it. Gating the server-driven
            # formats on "no usable URL at all" therefore threw away 720p,
            # 1080p and the audio tracks whenever that one 360p link came back
            # complete — which is the ordinary case, and the reason the panel
            # offered a single quality. What decides it is whether a rendition
            # is covered by a direct link, not whether *any* rendition is.
            uncovered = any(
                str(entry.get("itag") or "") not in covered
                for entry in (streaming_data.get("adaptiveFormats") or [])
            )
            if streaming_data.get("serverAbrStreamingUrl") and (
                not usable or uncovered
            ):
                # Either there is no fetchable URL at all, or some rendition is
                # published only through the streaming endpoint — or only as a
                # link restricted to an opening slice. Asking the server to
                # stream it is the way to get the whole file, so the endpoint is
                # recorded even when direct URLs exist alongside it.
                #
                # Endpoints differ in whether they will talk to an unattested
                # session, so every source is kept and ranked afterwards rather
                # than the first one winning.
                usable = usable + self._sabr_formats(
                    response, streaming_data, client, label
                )

            if not usable:
                if streaming_data.get("serverAbrStreamingUrl"):
                    gated = True
                    errors.append(
                        f"{label}: server-side ABR only — {len(entries)} format(s) "
                        "carried neither a URL nor a signature"
                    )
                elif entries:
                    gated = True
                    errors.append(
                        f"{label}: {len(entries)} format(s) had no resolvable URL"
                    )
                else:
                    errors.append(f"{label}: response contained no streaming data")

            for media_format in usable:
                source = (media_format.sabr or {}).get("source", "")
                identity = (f"{_stream_identity(media_format)}"
                            f"-{media_format.height}"
                            f"-{media_format.acodec}-{source}")
                if identity not in seen_ids:
                    seen_ids.add(identity)
                    collected.append(media_format)

            # Stop as soon as the *best* rendition on offer can be had whole,
            # from an endpoint that will talk to this session.
            #
            # "A complete, unrestricted option exists" was the old test, and
            # the 360p progressive stream satisfies it on virtually every
            # video — so the walk stopped at the first response and the
            # identities that supply 720p and above were never asked. A whole
            # file at a quarter of the available resolution is not a reason to
            # stop looking. Neither is a whole file from the watch page's own
            # streaming session, which the streaming server refuses outright.
            tallest = max((f.height or 0) for f in collected) if collected else 0
            whole = [f for f in collected
                     if not f.restricted and (f.height or 0) >= tallest]
            if tallest and whole and not any(
                (f.sabr or {}).get("source", "").startswith("watch-page")
                for f in whole
            ):
                break

        if not collected and streaming_extras.get("hlsManifestUrl"):
            # Live streams expose only a master playlist.
            collected.append(MediaFormat(
                format_id="hls-live",
                url=streaming_extras["hlsManifestUrl"],
                ext="mp4",
                protocol="m3u8",
                vcodec="h264",
                acodec="aac",
                note="live HLS",
            ))

        if not collected:
            detail = "; ".join(errors[:4]) if errors else "unknown reason"
            if bot_checked:
                remedy = (
                    "Downloading from the browser extension sends your existing "
                    "YouTube session with the request, which is what the check "
                    "is asking for."
                    if not self._has_cookies else
                    "A browser session was sent but was not accepted."
                )
                raise ExtractionError(
                    "YouTube is challenging this connection to prove it is not "
                    f"automated, so it served no streams. {remedy} Playing the "
                    "video and using the panel's “Already loaded by the player” "
                    f"list also works, since the player has already passed the "
                    f"check. Details: {detail}"
                )
            if gated:
                raise ExtractionError(
                    "YouTube answered with its server-side streaming endpoint "
                    "only, and published no directly fetchable stream URLs for "
                    "this video. Play the video and use the download panel's "
                    "“Already loaded by the player” list — those streams are the "
                    "ones the player negotiated and can be downloaded normally. "
                    f"Details: {detail}"
                )
            raise ExtractionError(f"YouTube returned no playable streams — {detail}")

        # A stream that can actually be fetched in full outranks a better-looking
        # one that cannot, so restricted URLs sort last regardless of quality.
        collected = self._prefer_usable_sabr(collected)
        self._fill_headers_from_dash(collected,
                                     streaming_extras.get("dashManifestUrl", ""))
        collected.sort(
            key=lambda f: (not f.restricted, f.is_progressive, f.height, f.tbr),
            reverse=True,
        )
        return MediaInfo(
            title=title or last_details.get("title", "") or f"youtube-{video_id}",
            formats=collected,
            webpage_url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail=thumbnail,
            duration=duration,
            extractor=self.name,
            http_headers={"Referer": "https://www.youtube.com/"},
        )
