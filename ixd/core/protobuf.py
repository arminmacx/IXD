"""A minimal Protocol Buffers reader and writer.

Only what is needed to speak the media-streaming protocol described in
:mod:`ixd.extractors.sabr`: varints, length-delimited fields and the two fixed
widths. There is no schema compiler and no generated code — messages are built
and read as ``{field_number: value}`` maps, which is enough because the
messages involved are small and their shapes are known.

Written by hand for the same reason as everything else here: the project takes
no third-party dependencies, and pulling in a protobuf runtime to encode a few
dozen bytes would be absurd.
"""

from __future__ import annotations

from typing import Any, Iterator

#: Wire types, from the Protocol Buffers encoding specification.
WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH = 2
WIRE_FIXED32 = 5


# ----------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------
def encode_varint(value: int) -> bytes:
    """Base-128 varint, as used for tags, lengths and integer fields."""
    if value < 0:
        # Negative values are transmitted as their two's-complement 64-bit form.
        value += 1 << 64
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return encode_varint((field << 3) | wire)


class Message:
    """Builds a protobuf message from field numbers and values."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def varint(self, field: int, value: int | None) -> "Message":
        if value is None:
            return self
        self._parts.append(_tag(field, WIRE_VARINT) + encode_varint(int(value)))
        return self

    def boolean(self, field: int, value: bool | None) -> "Message":
        return self.varint(field, None if value is None else int(bool(value)))

    def raw(self, field: int, value: bytes | None) -> "Message":
        if value is None:
            return self
        self._parts.append(
            _tag(field, WIRE_LENGTH) + encode_varint(len(value)) + value
        )
        return self

    def string(self, field: int, value: str | None) -> "Message":
        if value is None:
            return self
        return self.raw(field, value.encode("utf-8"))

    def message(self, field: int, value: "Message | None") -> "Message":
        if value is None:
            return self
        return self.raw(field, value.to_bytes())

    def fixed32(self, field: int, value: int | None) -> "Message":
        if value is None:
            return self
        self._parts.append(
            _tag(field, WIRE_FIXED32) + int(value).to_bytes(4, "little", signed=False)
        )
        return self

    def to_bytes(self) -> bytes:
        return b"".join(self._parts)

    def __len__(self) -> int:
        return len(self.to_bytes())


# ----------------------------------------------------------------------
# reading
# ----------------------------------------------------------------------
class Reader:
    """Walks a protobuf buffer field by field."""

    def __init__(self, data: bytes, offset: int = 0, end: int | None = None) -> None:
        self.data = data
        self.offset = offset
        self.end = len(data) if end is None else end

    @property
    def exhausted(self) -> bool:
        return self.offset >= self.end

    def varint(self) -> int:
        result = 0
        shift = 0
        while True:
            if self.offset >= self.end:
                raise ValueError("truncated varint")
            byte = self.data[self.offset]
            self.offset += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 70:
                raise ValueError("varint is too long")

    def fields(self) -> Iterator[tuple[int, int, Any]]:
        """Yield ``(field_number, wire_type, value)`` until the buffer ends.

        Length-delimited values come back as ``bytes``; the caller decides
        whether they are a string, a nested message or an opaque blob.
        """
        while not self.exhausted:
            tag = self.varint()
            field, wire = tag >> 3, tag & 0x07
            if wire == WIRE_VARINT:
                yield field, wire, self.varint()
            elif wire == WIRE_LENGTH:
                length = self.varint()
                if self.offset + length > self.end:
                    raise ValueError("length-delimited field runs past the buffer")
                value = self.data[self.offset:self.offset + length]
                self.offset += length
                yield field, wire, value
            elif wire == WIRE_FIXED64:
                if self.offset + 8 > self.end:
                    raise ValueError("truncated fixed64")
                value = int.from_bytes(self.data[self.offset:self.offset + 8], "little")
                self.offset += 8
                yield field, wire, value
            elif wire == WIRE_FIXED32:
                if self.offset + 4 > self.end:
                    raise ValueError("truncated fixed32")
                value = int.from_bytes(self.data[self.offset:self.offset + 4], "little")
                self.offset += 4
                yield field, wire, value
            else:
                raise ValueError(f"unsupported wire type {wire}")


def parse(data: bytes) -> dict[int, Any]:
    """Decode a message into ``{field_number: value}``.

    Repeated fields collapse into a list. This is lossy for a field that is
    repeated exactly once, which does not matter for the messages read here.
    """
    result: dict[int, Any] = {}
    for field, _wire, value in Reader(data).fields():
        if field in result:
            existing = result[field]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[field] = [existing, value]
        else:
            result[field] = value
    return result


def as_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) else default


def as_text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""
