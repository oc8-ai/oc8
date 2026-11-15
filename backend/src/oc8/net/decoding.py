"""Bounded decoding of a compressed response body.

httpx inflates each received chunk whole: a few KB of gzip can become many
MB in memory before a caller's size check runs. Readers that cap a body read
the raw bytes instead and decode them here, asking each step for no more than
what still fits (`max_length`), so the cap holds for the decoded size too.
"""

from __future__ import annotations

import zlib
from typing import Any, Protocol

#: The only encodings decoded here; ask for nothing else.
ACCEPT_ENCODING = "gzip, deflate"


class UnsupportedContentEncoding(ValueError):
    """The response's Content-Encoding is not one decoded here."""


class BoundedDecoder(Protocol):
    @property
    def unconsumed_tail(self) -> bytes: ...

    #: Whether the encoded stream reached its own end (a gzip trailer, the
    #: final deflate block). False after the last chunk: the body was cut.
    @property
    def eof(self) -> bool: ...

    def decompress(self, data: bytes, max_length: int) -> bytes: ...


class _Identity:
    unconsumed_tail = b""
    eof = True

    def decompress(self, data: bytes, max_length: int) -> bytes:
        self.unconsumed_tail = data[max_length:]
        return data[:max_length]


class _Deflate:
    """`deflate` is zlib-wrapped by the standard and raw in practice; try the
    first and fall back to the second on the first chunk."""

    def __init__(self) -> None:
        self._inner: Any = zlib.decompressobj()
        self._started = False

    @property
    def unconsumed_tail(self) -> bytes:
        return bytes(self._inner.unconsumed_tail)

    @property
    def eof(self) -> bool:
        return bool(self._inner.eof)

    def decompress(self, data: bytes, max_length: int) -> bytes:
        if not self._started:
            self._started = True
            try:
                return bytes(self._inner.decompress(data, max_length))
            except zlib.error:
                self._inner = zlib.decompressobj(-zlib.MAX_WBITS)
        return bytes(self._inner.decompress(data, max_length))


class _Gzip:
    def __init__(self) -> None:
        self._inner: Any = zlib.decompressobj(zlib.MAX_WBITS | 16)

    @property
    def unconsumed_tail(self) -> bytes:
        return bytes(self._inner.unconsumed_tail)

    @property
    def eof(self) -> bool:
        return bool(self._inner.eof)

    def decompress(self, data: bytes, max_length: int) -> bytes:
        return bytes(self._inner.decompress(data, max_length))


def content_decoder(content_encoding: str) -> BoundedDecoder:
    """A decoder for a Content-Encoding header value, or
    `UnsupportedContentEncoding`. A decode failure raises `zlib.error`; a
    stream that stops before its end leaves `eof` False. Data after the end
    (a second gzip member, trailing bytes) is ignored, as httpx does."""
    encodings = [e.strip().lower() for e in content_encoding.split(",") if e.strip()]
    encodings = [e for e in encodings if e != "identity"]
    if not encodings:
        return _Identity()
    if encodings == ["gzip"] or encodings == ["x-gzip"]:
        return _Gzip()
    if encodings == ["deflate"]:
        return _Deflate()
    raise UnsupportedContentEncoding(content_encoding)


def decode_step(decoder: BoundedDecoder, chunk: bytes, room: int) -> tuple[bytes, bool]:
    """Decode one raw chunk into at most `room` bytes. Returns the decoded
    bytes and whether the chunk held more than `room` (the body overflows).
    No single decompress call yields more than `room + 1` bytes."""
    out = bytearray()
    data = chunk
    while data:
        left = room - len(out)
        piece = decoder.decompress(data, left + 1)
        if len(piece) > left:
            out.extend(piece[:left])
            return bytes(out), True
        out.extend(piece)
        data = decoder.unconsumed_tail
    return bytes(out), False
