"""Deterministic Chrome extension identities, with no third-party crypto.

Chrome derives an extension's ID from its public key: the SHA-256 of the DER
``SubjectPublicKeyInfo`` is truncated to 16 bytes, rendered as hex, and each
nibble is shifted from the ``0-f`` alphabet into ``a-p``.  When a manifest
carries a ``"key"`` field, that key wins — so **embedding a fixed key makes the
ID of an unpacked extension predictable before the browser has ever seen it**.

That single fact removes the worst part of the setup flow.  Without it, the
native-messaging manifest cannot be written until the user has loaded the
extension, read a 32-character ID off ``chrome://extensions`` and typed it back
into an installer.  With it, the application registers itself up front and the
extension simply works the moment it is loaded.

Everything here is standard library only: DER is emitted by hand and the RSA
key pair is generated with :mod:`secrets` plus a Miller–Rabin primality test,
matching the project's rule that nothing depends on an external binary or
package.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

#: 1.2.840.113549.1.1.1 — rsaEncryption.
_RSA_OID = bytes([0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x01])
_PUBLIC_EXPONENT = 65537

#: Chrome renders the digest with this alphabet instead of hexadecimal.
_ID_ALPHABET = "abcdefghijklmnop"


# ----------------------------------------------------------------------
# DER
# ----------------------------------------------------------------------
def _der_length(count: int) -> bytes:
    if count < 0x80:
        return bytes([count])
    encoded = count.to_bytes((count.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(encoded)]) + encoded


def _der(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(payload)) + payload


def _der_integer(value: int) -> bytes:
    if value == 0:
        return _der(0x02, b"\x00")
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    if raw[0] & 0x80:                 # keep the value positive
        raw = b"\x00" + raw
    return _der(0x02, raw)


def _der_sequence(*items: bytes) -> bytes:
    return _der(0x30, b"".join(items))


def _der_bitstring(payload: bytes) -> bytes:
    return _der(0x03, b"\x00" + payload)      # zero unused trailing bits


def _rsa_algorithm_identifier() -> bytes:
    return _der_sequence(_der(0x06, _RSA_OID), _der(0x05, b""))


def public_key_der(modulus: int, exponent: int = _PUBLIC_EXPONENT) -> bytes:
    """Encode an RSA public key as a DER ``SubjectPublicKeyInfo``."""
    rsa_public_key = _der_sequence(_der_integer(modulus), _der_integer(exponent))
    return _der_sequence(_rsa_algorithm_identifier(), _der_bitstring(rsa_public_key))


def private_key_der(modulus: int, exponent: int, private_exponent: int,
                    prime1: int, prime2: int) -> bytes:
    """Encode an RSA private key as a DER PKCS#8 ``PrivateKeyInfo``."""
    exponent1 = private_exponent % (prime1 - 1)
    exponent2 = private_exponent % (prime2 - 1)
    coefficient = pow(prime2, -1, prime1)
    pkcs1 = _der_sequence(
        _der_integer(0),
        _der_integer(modulus),
        _der_integer(exponent),
        _der_integer(private_exponent),
        _der_integer(prime1),
        _der_integer(prime2),
        _der_integer(exponent1),
        _der_integer(exponent2),
        _der_integer(coefficient),
    )
    return _der_sequence(
        _der_integer(0), _rsa_algorithm_identifier(), _der(0x04, pkcs1)
    )


def to_pem(der: bytes, label: str) -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


# ----------------------------------------------------------------------
# identity
# ----------------------------------------------------------------------
def extension_id_from_der(der: bytes) -> str:
    """Chrome's extension ID for a DER ``SubjectPublicKeyInfo``."""
    digest = hashlib.sha256(der).hexdigest()[:32]
    return "".join(_ID_ALPHABET[int(character, 16)] for character in digest)


def extension_id_from_manifest_key(key: str) -> str:
    """Chrome's extension ID for a manifest ``"key"`` (base64 DER)."""
    try:
        der = base64.b64decode(key, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("the manifest key is not valid base64") from exc
    return extension_id_from_der(der)


def is_extension_id(value: str) -> bool:
    return (
        len(value) == 32
        and all(character in _ID_ALPHABET for character in value)
    )


# ----------------------------------------------------------------------
# key generation
# ----------------------------------------------------------------------
_SMALL_PRIMES = (
    3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71,
    73, 79, 83, 89, 97, 101, 103, 107, 109, 113, 127, 131, 137, 139, 149, 151,
    157, 163, 167, 173, 179, 181, 191, 193, 197, 199, 211, 223, 227, 229, 233,
    239, 241, 251,
)


def _is_probable_prime(candidate: int, rounds: int = 40) -> bool:
    """Miller–Rabin with random bases, after trial division."""
    if candidate < 2:
        return False
    for prime in _SMALL_PRIMES:
        if candidate == prime:
            return True
        if candidate % prime == 0:
            return False

    remainder = candidate - 1
    power = 0
    while remainder % 2 == 0:
        remainder //= 2
        power += 1

    for _ in range(rounds):
        base = secrets.randbelow(candidate - 3) + 2
        witness = pow(base, remainder, candidate)
        if witness in (1, candidate - 1):
            continue
        for _ in range(power - 1):
            witness = pow(witness, 2, candidate)
            if witness == candidate - 1:
                break
        else:
            return False
    return True


def _random_prime(bits: int) -> int:
    """A random prime with the top two bits set, so ``p * q`` has ``2 * bits``."""
    while True:
        candidate = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if (candidate - 1) % _PUBLIC_EXPONENT == 0:
            continue          # would make e non-invertible mod (p - 1)
        if _is_probable_prime(candidate):
            return candidate


def generate_key_pair(bits: int = 2048) -> tuple[str, str, str]:
    """Generate an RSA key pair for signing an extension identity.

    Returns ``(manifest_key, private_key_pem, extension_id)`` where
    ``manifest_key`` is the base64 string that goes into the manifest's
    ``"key"`` field and therefore fixes the extension's ID forever.
    """
    if bits < 1024 or bits % 2:
        raise ValueError("key size must be an even number of bits, at least 1024")

    half = bits // 2
    while True:
        prime1 = _random_prime(half)
        prime2 = _random_prime(half)
        if prime1 == prime2:
            continue
        modulus = prime1 * prime2
        if modulus.bit_length() != bits:
            continue
        totient = (prime1 - 1) * (prime2 - 1) // _gcd(prime1 - 1, prime2 - 1)
        try:
            private_exponent = pow(_PUBLIC_EXPONENT, -1, totient)
        except ValueError:
            continue
        break

    public_der = public_key_der(modulus)
    private_der = private_key_der(
        modulus, _PUBLIC_EXPONENT, private_exponent, prime1, prime2
    )
    return (
        base64.b64encode(public_der).decode("ascii"),
        to_pem(private_der, "PRIVATE KEY"),
        extension_id_from_der(public_der),
    )


def _gcd(first: int, second: int) -> int:
    while second:
        first, second = second, first % second
    return first
