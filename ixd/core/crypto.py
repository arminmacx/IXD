"""AES-128-CBC decryption for AES-128 protected HLS segments.

HLS ``#EXT-X-KEY:METHOD=AES-128`` streams are extremely common, so the engine
must be able to decrypt them on its own.  A self-contained implementation keeps
that capability dependency-free; when the optional ``cryptography`` wheel
happens to be installed we transparently use its native AES instead, which is
roughly two orders of magnitude faster.
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# optional native acceleration
# ----------------------------------------------------------------------
try:  # pragma: no cover - depends on the host environment
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _HAS_NATIVE_AES = True
except ImportError:  # pragma: no cover
    _HAS_NATIVE_AES = False


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
    cipher = AES(key)
    output = bytearray(len(data))
    previous = iv
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        decrypted = cipher.decrypt_block(block)
        for i in range(16):
            output[offset + i] = decrypted[i] ^ previous[i]
        previous = block
    return bytes(output)


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

    if _HAS_NATIVE_AES:  # pragma: no cover - environment dependent
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plaintext = decryptor.update(data) + decryptor.finalize()
    else:
        plaintext = _cbc_decrypt_pure(key, iv, data)

    if unpad and plaintext:
        pad_length = plaintext[-1]
        if 1 <= pad_length <= 16 and plaintext[-pad_length:] == bytes([pad_length]) * pad_length:
            plaintext = plaintext[:-pad_length]
    return plaintext


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes, pad: bool = True) -> bytes:
    """AES-CBC encryption with PKCS#7 padding (mirror of the decrypt path)."""
    if pad:
        pad_length = 16 - (len(data) % 16)
        data = data + bytes([pad_length]) * pad_length
    elif len(data) % 16:
        raise ValueError("unpadded input must be a multiple of the block size")

    if _HAS_NATIVE_AES:  # pragma: no cover - environment dependent
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return encryptor.update(data) + encryptor.finalize()

    cipher = AES(key)
    output = bytearray()
    previous = iv
    for offset in range(0, len(data), 16):
        block = bytes(
            data[offset + i] ^ previous[i] for i in range(16)
        )
        encrypted = cipher.encrypt_block(block)
        output += encrypted
        previous = encrypted
    return bytes(output)


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
    """True when the fast native backend is in use."""
    return _HAS_NATIVE_AES
