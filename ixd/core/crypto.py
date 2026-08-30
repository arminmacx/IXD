"""AES-128-CBC decryption for AES-128 protected HLS streams.

``#EXT-X-KEY:METHOD=AES-128`` covers a large part of the streaming web, so the
engine has to decrypt on its own.  Three backends, in the order they are tried:

* ``cryptography`` — used only if somebody installed it; nothing here needs it.
* **OpenSSL, through ctypes** — the one that actually runs.  ``libcrypto`` is
  in every build already, because ``ssl`` is imported for HTTPS and PyInstaller
  bundles it beside the binary.  Native speed, no new dependency.
* the pure-Python block cipher below — a last resort, and slow enough that the
  log says so.

The gap is not academic.  Measured on this machine: **0.48 MiB/s** in Python
against **277 MiB/s** through OpenSSL.  Worse, the Python path is bytecode, so
it holds the GIL — eight download workers decrypting managed 0.46 MiB/s
*between them* and starved the Qt event loop until the window had to be killed.
A ctypes call releases the GIL, so the native path lets the UI run as well as
running itself (context.md §3.92).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import os
import sys
import time

# ----------------------------------------------------------------------
# backend 1: the optional wheel, if this machine happens to have it
# ----------------------------------------------------------------------
try:  # pragma: no cover - depends on the host environment
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _HAS_CRYPTOGRAPHY = True
except ImportError:
    _HAS_CRYPTOGRAPHY = False


# ----------------------------------------------------------------------
# backend 2: the OpenSSL that is already in this process
# ----------------------------------------------------------------------
#: Names libcrypto ships under, per platform. Globs are searched too, because
#: the exact name is a build detail of whoever compiled CPython and this must
#: not depend on guessing it right on a platform we cannot run here (rule 8).
if sys.platform == "win32":
    _CRYPTO_NAMES = ("libcrypto-3-x64.dll", "libcrypto-3.dll",
                     "libcrypto-1_1-x64.dll", "libcrypto-1_1.dll")
    _CRYPTO_GLOB = "libcrypto*.dll"
elif sys.platform == "darwin":
    _CRYPTO_NAMES = ("libcrypto.3.dylib", "libcrypto.1.1.dylib", "libcrypto.dylib")
    _CRYPTO_GLOB = "libcrypto*.dylib"
else:
    _CRYPTO_NAMES = ("libcrypto.so.3", "libcrypto.so.1.1", "libcrypto.so")
    _CRYPTO_GLOB = "libcrypto*.so*"


def _libcrypto_directories() -> list[str]:
    """Where the OpenSSL this process is already using could be sitting."""
    directories: list[str] = []
    # A frozen build unpacks its bundled libcrypto here.
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        directories.append(bundle)
        directories.append(os.path.join(bundle, "_internal"))
    # Beside the executable, for a one-folder build.
    if getattr(sys, "frozen", False):
        root = os.path.dirname(sys.executable)
        directories.append(root)
        directories.append(os.path.join(root, "_internal"))
    # The directory CPython's own _ssl extension was loaded from — on Windows
    # that is where python.org's OpenSSL DLLs live.
    try:
        import _ssl  # noqa: PLC0415

        module_file = getattr(_ssl, "__file__", None)
        if module_file:
            directories.append(os.path.dirname(module_file))
            directories.append(os.path.dirname(os.path.dirname(module_file)))
            directories.append(os.path.join(os.path.dirname(module_file), "..", "DLLs"))
    except ImportError:  # pragma: no cover - ssl is not optional in practice
        pass
    seen: set[str] = set()
    unique: list[str] = []
    for directory in directories:
        resolved = os.path.normpath(directory)
        if resolved not in seen and os.path.isdir(resolved):
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _load_libcrypto():
    """Return a loaded libcrypto, or None. Never raises."""
    for directory in _libcrypto_directories():
        for name in _CRYPTO_NAMES:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                try:
                    return ctypes.CDLL(path)
                except OSError:
                    continue
        # The name is a build detail; take whatever libcrypto is in there.
        for path in sorted(glob.glob(os.path.join(directory, _CRYPTO_GLOB))):
            try:
                return ctypes.CDLL(path)
            except OSError:
                continue
    located = ctypes.util.find_library("crypto")
    if located:
        try:
            return ctypes.CDLL(located)
        except OSError:
            pass
    for name in _CRYPTO_NAMES:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


class _OpenSSLCBC:
    """AES-CBC through ``libcrypto``'s EVP interface.

    A ctypes call releases the GIL for its duration, which is the half of this
    that keeps the window alive; the other half is that it is ~580x faster.
    """

    #: EVP_DecryptUpdate takes an int length. Segments are far smaller than
    #: this, but the chain is kept across calls so a large one still works.
    CHUNK = 8 << 20

    def __init__(self, lib) -> None:
        self.lib = lib
        lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
        lib.EVP_CIPHER_CTX_new.argtypes = []
        lib.EVP_CIPHER_CTX_free.argtypes = [ctypes.c_void_p]
        lib.EVP_CIPHER_CTX_free.restype = None
        lib.EVP_CIPHER_CTX_set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.EVP_CIPHER_CTX_set_padding.restype = ctypes.c_int
        for suffix in ("128", "192", "256"):
            function = getattr(lib, f"EVP_aes_{suffix}_cbc")
            function.restype = ctypes.c_void_p
            function.argtypes = []
        for verb in ("Decrypt", "Encrypt"):
            init = getattr(lib, f"EVP_{verb}Init_ex")
            init.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_char_p, ctypes.c_char_p]
            init.restype = ctypes.c_int
            update = getattr(lib, f"EVP_{verb}Update")
            update.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                               ctypes.POINTER(ctypes.c_int), ctypes.c_char_p,
                               ctypes.c_int]
            update.restype = ctypes.c_int

    def _cipher(self, key: bytes):
        return getattr(self.lib, f"EVP_aes_{len(key) * 8}_cbc")()

    def run(self, key: bytes, iv: bytes, data: bytes, decrypt: bool) -> bytes:
        verb = "Decrypt" if decrypt else "Encrypt"
        init = getattr(self.lib, f"EVP_{verb}Init_ex")
        update = getattr(self.lib, f"EVP_{verb}Update")
        context = self.lib.EVP_CIPHER_CTX_new()
        if not context:
            raise OSError("EVP_CIPHER_CTX_new failed")
        try:
            if init(context, self._cipher(key), None, key, iv) != 1:
                raise OSError(f"EVP_{verb}Init_ex failed")
            # Padding is handled in this module, on both sides, so that the
            # three backends are byte-for-byte interchangeable.
            self.lib.EVP_CIPHER_CTX_set_padding(context, 0)
            written = ctypes.c_int(0)
            # The EVP call itself releases the GIL; copying its output does
            # not. `buffer.raw` would copy the whole buffer and then slice it,
            # so a segment would be copied twice with the interpreter locked --
            # `string_at` takes the written bytes only, once.
            if len(data) <= self.CHUNK:
                buffer = ctypes.create_string_buffer(len(data) + 32)
                if update(context, buffer, ctypes.byref(written), data, len(data)) != 1:
                    raise OSError(f"EVP_{verb}Update failed")
                return ctypes.string_at(buffer, written.value)
            out = bytearray()
            for offset in range(0, len(data), self.CHUNK):
                block = data[offset:offset + self.CHUNK]
                buffer = ctypes.create_string_buffer(len(block) + 32)
                if update(context, buffer, ctypes.byref(written), block, len(block)) != 1:
                    raise OSError(f"EVP_{verb}Update failed")
                out += ctypes.string_at(buffer, written.value)
            return bytes(out)
        finally:
            self.lib.EVP_CIPHER_CTX_free(context)


def _build_openssl_backend():
    library = _load_libcrypto()
    if library is None:
        return None, "libcrypto was not found beside this build or on the system"
    try:
        backend = _OpenSSLCBC(library)
    except AttributeError as error:      # an OpenSSL without the EVP names
        return None, f"libcrypto has no usable EVP interface ({error})"
    # Prove it on a known vector before trusting it with anybody's download.
    # FIPS-197 / NIST SP 800-38A AES-128-CBC, first block.
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    plain = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    cipher = bytes.fromhex("7649abac8119b246cee98e9b12e9197d")
    try:
        if backend.run(key, iv, cipher, decrypt=True) != plain:
            return None, "libcrypto failed the AES-128-CBC known-answer test"
        if backend.run(key, iv, plain, decrypt=False) != cipher:
            return None, "libcrypto failed the AES-128-CBC known-answer test"
    except OSError as error:
        return None, f"libcrypto rejected the known-answer test ({error})"
    return backend, getattr(library, "_name", "libcrypto")


_OPENSSL, _OPENSSL_DETAIL = _build_openssl_backend()

if _HAS_CRYPTOGRAPHY:
    _BACKEND = "cryptography"
elif _OPENSSL is not None:
    _BACKEND = "openssl"
else:
    _BACKEND = "python"

#: True when decryption runs as native code that releases the GIL. The engine
#: logs this either way: a backend that believes it worked and one that was
#: never reached look identical otherwise (CLAUDE.md rule 8).
_HAS_NATIVE_AES = _BACKEND in ("cryptography", "openssl")


SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)

INV_SBOX = bytearray(256)
for _index, _value in enumerate(SBOX):
    INV_SBOX[_value] = _index
INV_SBOX = bytes(INV_SBOX)

RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
        0x6C, 0xD8, 0xAB, 0x4D, 0x9A)


def _gmul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8) with the AES reduction polynomial."""
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        high = a & 0x80
        a = (a << 1) & 0xFF
        if high:
            a ^= 0x1B
        b >>= 1
    return result


# Multiplication tables for InvMixColumns (coefficients 0e, 0b, 0d, 09).
MUL14 = bytes(_gmul(x, 14) for x in range(256))
MUL11 = bytes(_gmul(x, 11) for x in range(256))
MUL13 = bytes(_gmul(x, 13) for x in range(256))
MUL9 = bytes(_gmul(x, 9) for x in range(256))
# ... and for the forward MixColumns (coefficients 02, 03).
MUL2 = bytes(_gmul(x, 2) for x in range(256))
MUL3 = bytes(_gmul(x, 3) for x in range(256))


class AES:
    """Minimal AES block cipher supporting 128/192/256-bit keys."""

    __slots__ = ("rounds", "round_keys")

    def __init__(self, key: bytes) -> None:
        if len(key) not in (16, 24, 32):
            raise ValueError(f"invalid AES key length: {len(key)}")
        key_words = len(key) // 4
        self.rounds = key_words + 6
        self.round_keys = self._expand_key(key, key_words, self.rounds)

    @staticmethod
    def _expand_key(key: bytes, key_words: int, rounds: int) -> list[bytes]:
        words: list[list[int]] = [list(key[4 * i:4 * i + 4]) for i in range(key_words)]
        total_words = 4 * (rounds + 1)

        for i in range(key_words, total_words):
            temp = list(words[i - 1])
            if i % key_words == 0:
                temp = temp[1:] + temp[:1]                       # RotWord
                temp = [SBOX[b] for b in temp]                   # SubWord
                temp[0] ^= RCON[i // key_words - 1]
            elif key_words > 6 and i % key_words == 4:
                temp = [SBOX[b] for b in temp]
            words.append([words[i - key_words][j] ^ temp[j] for j in range(4)])

        return [
            bytes(words[4 * r][j] for j in range(4)) +
            bytes(words[4 * r + 1][j] for j in range(4)) +
            bytes(words[4 * r + 2][j] for j in range(4)) +
            bytes(words[4 * r + 3][j] for j in range(4))
            for r in range(rounds + 1)
        ]

    def decrypt_block(self, block: bytes) -> bytes:
        """Inverse cipher on one 16-byte block (FIPS-197 §5.3)."""
        state = bytearray(16)
        round_key = self.round_keys[self.rounds]
        for i in range(16):
            state[i] = block[i] ^ round_key[i]

        for round_index in range(self.rounds - 1, 0, -1):
            self._inv_shift_rows(state)
            for i in range(16):
                state[i] = INV_SBOX[state[i]]
            round_key = self.round_keys[round_index]
            for i in range(16):
                state[i] ^= round_key[i]
            self._inv_mix_columns(state)

        self._inv_shift_rows(state)
        for i in range(16):
            state[i] = INV_SBOX[state[i]]
        round_key = self.round_keys[0]
        for i in range(16):
            state[i] ^= round_key[i]
        return bytes(state)

    def encrypt_block(self, block: bytes) -> bytes:
        """Forward cipher on one 16-byte block (FIPS-197 §5.1).

        The downloader only ever decrypts, but a matching encryptor makes the
        implementation verifiable against arbitrary data rather than only the
        fixed published vectors.
        """
        state = bytearray(16)
        round_key = self.round_keys[0]
        for i in range(16):
            state[i] = block[i] ^ round_key[i]

        for round_index in range(1, self.rounds):
            for i in range(16):
                state[i] = SBOX[state[i]]
            self._shift_rows(state)
            self._mix_columns(state)
            round_key = self.round_keys[round_index]
            for i in range(16):
                state[i] ^= round_key[i]

        for i in range(16):
            state[i] = SBOX[state[i]]
        self._shift_rows(state)
        round_key = self.round_keys[self.rounds]
        for i in range(16):
            state[i] ^= round_key[i]
        return bytes(state)

    @staticmethod
    def _shift_rows(state: bytearray) -> None:
        """Row r rotates left by r."""
        state[1], state[5], state[9], state[13] = state[5], state[9], state[13], state[1]
        state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
        state[3], state[7], state[11], state[15] = state[15], state[3], state[7], state[11]

    @staticmethod
    def _mix_columns(state: bytearray) -> None:
        for column in range(4):
            base = 4 * column
            a0, a1, a2, a3 = state[base], state[base + 1], state[base + 2], state[base + 3]
            state[base] = MUL2[a0] ^ MUL3[a1] ^ a2 ^ a3
            state[base + 1] = a0 ^ MUL2[a1] ^ MUL3[a2] ^ a3
            state[base + 2] = a0 ^ a1 ^ MUL2[a2] ^ MUL3[a3]
            state[base + 3] = MUL3[a0] ^ a1 ^ a2 ^ MUL2[a3]

    @staticmethod
    def _inv_shift_rows(state: bytearray) -> None:
        """Row r rotates right by r. State is column-major: s[r][c] = state[r+4c]."""
        # row 1: rotate right by 1
        state[1], state[5], state[9], state[13] = state[13], state[1], state[5], state[9]
        # row 2: rotate right by 2
        state[2], state[6], state[10], state[14] = state[10], state[14], state[2], state[6]
        # row 3: rotate right by 3
        state[3], state[7], state[11], state[15] = state[7], state[11], state[15], state[3]

    @staticmethod
    def _inv_mix_columns(state: bytearray) -> None:
        for column in range(4):
            base = 4 * column
            a0, a1, a2, a3 = state[base], state[base + 1], state[base + 2], state[base + 3]
            state[base] = MUL14[a0] ^ MUL11[a1] ^ MUL13[a2] ^ MUL9[a3]
            state[base + 1] = MUL9[a0] ^ MUL14[a1] ^ MUL11[a2] ^ MUL13[a3]
            state[base + 2] = MUL13[a0] ^ MUL9[a1] ^ MUL14[a2] ^ MUL11[a3]
            state[base + 3] = MUL11[a0] ^ MUL13[a1] ^ MUL9[a2] ^ MUL14[a3]


def _cbc_decrypt_pure(key: bytes, iv: bytes, data: bytes) -> bytes:
    """The fallback. Slow, and deliberately polite about it.

    Every operation here is bytecode, so this function holds the GIL for as
    long as it runs — and on a 2 MiB segment that is four seconds during which
    the Qt event loop gets nothing and the window is reported unresponsive.
    Yielding once per ``_YIELD_EVERY`` bytes costs ~1% of a path that is
    already slow and keeps the interface answering.
    """
    cipher = AES(key)
    output = bytearray(len(data))
    previous = iv
    since_yield = 0
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        decrypted = cipher.decrypt_block(block)
        for i in range(16):
            output[offset + i] = decrypted[i] ^ previous[i]
        previous = block
        since_yield += 16
        if since_yield >= _YIELD_EVERY:
            since_yield = 0
            time.sleep(0)      # drops the GIL; the only way the UI gets a turn
    return bytes(output)


#: How much plaintext the pure-Python path produces between GIL yields.
_YIELD_EVERY = 32 * 1024


def _run(key: bytes, iv: bytes, data: bytes, decrypt: bool) -> bytes:
    """Dispatch to whichever backend this build ended up with."""
    if _BACKEND == "cryptography":  # pragma: no cover - environment dependent
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        operation = cipher.decryptor() if decrypt else cipher.encryptor()
        return operation.update(data) + operation.finalize()
    if _BACKEND == "openssl":
        return _OPENSSL.run(key, iv, data, decrypt)
    if decrypt:
        return _cbc_decrypt_pure(key, iv, data)
    return _cbc_encrypt_pure(key, iv, data)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes, unpad: bool = True) -> bytes:
    """Decrypt AES-CBC ciphertext, optionally stripping PKCS#7 padding.

    HLS segments are padded per PKCS#7, but a segment that is part of a longer
    logical stream may not be — ``unpad=False`` covers that case.
    """
    if not data:
        return b""
    if len(data) % 16:
        # Trailing garbage would corrupt the block chain; drop the partial tail.
        data = data[:len(data) - (len(data) % 16)]
        if not data:
            return b""
    if len(iv) != 16:
        iv = iv.ljust(16, b"\0")[:16]

    plaintext = _run(key, iv, data, decrypt=True)

    if unpad and plaintext:
        pad_length = plaintext[-1]
        if 1 <= pad_length <= 16 and plaintext[-pad_length:] == bytes([pad_length]) * pad_length:
            plaintext = plaintext[:-pad_length]
    return plaintext


def _cbc_encrypt_pure(key: bytes, iv: bytes, data: bytes) -> bytes:
    cipher = AES(key)
    output = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = bytes(data[offset + i] ^ previous[i] for i in range(16))
        encrypted = cipher.encrypt_block(block)
        output += encrypted
        previous = encrypted
    return bytes(output)


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes, pad: bool = True) -> bytes:
    """AES-CBC encryption with PKCS#7 padding (mirror of the decrypt path)."""
    if pad:
        pad_length = 16 - (len(data) % 16)
        data = data + bytes([pad_length]) * pad_length
    elif len(data) % 16:
        raise ValueError("unpadded input must be a multiple of the block size")
    return _run(key, iv, data, decrypt=False)



def iv_for_sequence(sequence_number: int) -> bytes:
    """Default HLS IV when ``#EXT-X-KEY`` omits one: the media sequence number."""
    return sequence_number.to_bytes(16, "big")


def parse_hex_iv(value: str | None, sequence_number: int = 0) -> bytes:
    if not value:
        return iv_for_sequence(sequence_number)
    text = value.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return iv_for_sequence(sequence_number)
    return raw.rjust(16, b"\0")[:16]


def native_aes_available() -> bool:
    """True when decryption is native code that releases the GIL."""
    return _HAS_NATIVE_AES


def aes_backend() -> str:
    """Which of the three backends this process ended up with."""
    return _BACKEND


def aes_backend_report() -> tuple[str, str]:
    """``(level, message)`` for the log, describing the backend either way.

    The success case is logged as well as the failure. A backend that was
    never reached and one that believes it worked look identical otherwise,
    which is how the Windows taskbar bar stayed dead for two releases.
    """
    if _BACKEND == "cryptography":
        return "info", "AES: the cryptography wheel (native, threads run in parallel)"
    if _BACKEND == "openssl":
        return "info", f"AES: OpenSSL via ctypes ({_OPENSSL_DETAIL}) — native, threads run in parallel"
    return "warning", (
        "AES: pure Python — no native backend was reachable "
        f"({_OPENSSL_DETAIL}). Encrypted HLS will decrypt at roughly 0.5 MB/s "
        "and downloads of them will be slow.")
