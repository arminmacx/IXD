"""Post-processing integrity checks.

Two independent sources of truth are supported:

* **Server-advertised digests** — ``Content-MD5`` (RFC 1864), ``Digest``
  (RFC 3230) and ``Repr-Digest`` (RFC 9530) are parsed and validated
  automatically when present.
* **User-supplied hashes** — pasted from a release page into the download
  properties dialog, compared case-insensitively.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable, Iterable

from .models import HashStatus

SUPPORTED_ALGORITHMS = ("md5", "sha1", "sha256", "sha512", "blake2b")

#: Digest header token → hashlib name.
_DIGEST_ALGORITHM_MAP = {
    "md5": "md5",
    "sha": "sha1",
    "sha-1": "sha1",
    "sha1": "sha1",
    "sha-256": "sha256",
    "sha256": "sha256",
    "sha-512": "sha512",
    "sha512": "sha512",
    "unixsum": "",
    "unixcksum": "",
}


@dataclass(slots=True)
class DigestExpectation:
    """A single algorithm/value pair we intend to verify against."""

    algorithm: str
    value: str          # always lowercase hex
    source: str         # "header" | "user"


@dataclass(slots=True)
class VerificationResult:
    status: HashStatus
    computed: dict[str, str]
    expectations: list[DigestExpectation]
    failures: list[str]

    @property
    def primary_hash(self) -> str:
        for algorithm in ("sha256", "sha1", "md5"):
            if algorithm in self.computed:
                return self.computed[algorithm]
        return next(iter(self.computed.values()), "")

    def describe(self) -> str:
        if self.status is HashStatus.VERIFIED:
            names = ", ".join(sorted({e.algorithm for e in self.expectations}))
            return f"Verified ({names})"
        if self.status is HashStatus.CORRUPTED:
            return "Corrupted: " + "; ".join(self.failures)
        if self.status is HashStatus.NO_REFERENCE:
            return "Hashed (no reference to compare)"
        return "Not verified"


def _strip_algorithm_prefix(value: str) -> str:
    """Drop a leading ``sha-256=`` style label without eating base64 padding.

    In base64 an ``=`` only ever appears in the trailing padding run, so any
    ``=`` followed by a non-``=`` character must be a label separator.
    """
    separator = -1
    for index, character in enumerate(value):
        if character == "=" and index + 1 < len(value) and value[index + 1] != "=":
            separator = index
    return value[separator + 1:] if separator >= 0 else value


def normalize_hash(value: str) -> str:
    """Accept hex or base64 and return lowercase hex."""
    value = (value or "").strip()
    if not value:
        return ""
    value = _strip_algorithm_prefix(value).strip()
    value = value.strip(":").strip()
    if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) % 2 == 0:
        return value.lower()
    try:
        padded = value + "=" * (-len(value) % 4)
        return binascii.hexlify(base64.b64decode(padded, validate=False)).decode("ascii")
    except (binascii.Error, ValueError):
        return value.lower()


def algorithm_for_hex(hex_value: str) -> str:
    """Guess the algorithm from a bare hex digest's length."""
    return {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}.get(len(hex_value or ""), "")


def parse_server_digests(headers: dict[str, str]) -> list[DigestExpectation]:
    """Extract every digest the origin advertised."""
    expectations: list[DigestExpectation] = []
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}

    content_md5 = lowered.get("content-md5")
    if content_md5:
        hex_value = normalize_hash(content_md5)
        if len(hex_value) == 32:
            expectations.append(DigestExpectation("md5", hex_value, "header"))

    for header_name in ("digest", "repr-digest", "want-digest"):
        raw = lowered.get(header_name)
        if not raw:
            continue
        for part in raw.split(","):
            part = part.strip()
            if "=" not in part:
                continue
            token, _, value = part.partition("=")
            algorithm = _DIGEST_ALGORITHM_MAP.get(token.strip().lower(), "")
            if not algorithm:
                continue
            hex_value = normalize_hash(value)
            if hex_value:
                expectations.append(DigestExpectation(algorithm, hex_value, "header"))
    return expectations


def hash_file(path: str, algorithms: Iterable[str], chunk_size: int = 4 << 20,
              progress: Callable[[int, int], None] | None = None,
              stop_event: threading.Event | None = None) -> dict[str, str]:
    """Stream a file through several hashers in one pass."""
    names = [a for a in dict.fromkeys(algorithms) if a in SUPPORTED_ALGORITHMS]
    if not names:
        names = ["sha256"]
    hashers = {name: hashlib.new(name) for name in names}

    total = os.path.getsize(path)
    processed = 0
    with open(path, "rb", buffering=0) as handle:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("hashing cancelled")
            block = handle.read(chunk_size)
            if not block:
                break
            for hasher in hashers.values():
                hasher.update(block)
            processed += len(block)
            if progress is not None:
                progress(processed, total)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}


def verify_file(path: str, *, expected_hash: str = "", expected_algorithm: str = "",
                server_headers: dict[str, str] | None = None,
                extra_algorithms: Iterable[str] = (),
                chunk_size: int = 4 << 20,
                progress: Callable[[int, int], None] | None = None,
                stop_event: threading.Event | None = None) -> VerificationResult:
    """Hash ``path`` once and check it against every available expectation."""
    expectations: list[DigestExpectation] = list(parse_server_digests(server_headers or {}))

    user_hash = normalize_hash(expected_hash)
    if user_hash:
        algorithm = (expected_algorithm or "").lower().strip()
        if algorithm not in SUPPORTED_ALGORITHMS:
            algorithm = algorithm_for_hex(user_hash) or "sha256"
        expectations.append(DigestExpectation(algorithm, user_hash, "user"))

    needed = {e.algorithm for e in expectations if e.algorithm in SUPPORTED_ALGORITHMS}
    needed.update(a for a in extra_algorithms if a in SUPPORTED_ALGORITHMS)
    if not needed:
        needed = {"sha256"}

    computed = hash_file(path, sorted(needed), chunk_size, progress, stop_event)

    failures: list[str] = []
    checked = 0
    for expectation in expectations:
        actual = computed.get(expectation.algorithm)
        if not actual:
            continue
        checked += 1
        if actual != expectation.value:
            failures.append(
                f"{expectation.algorithm} ({expectation.source}): "
                f"expected {expectation.value[:16]}…, got {actual[:16]}…"
            )

    if failures:
        status = HashStatus.CORRUPTED
    elif checked:
        status = HashStatus.VERIFIED
    else:
        status = HashStatus.NO_REFERENCE

    return VerificationResult(
        status=status,
        computed=computed,
        expectations=expectations,
        failures=failures,
    )
