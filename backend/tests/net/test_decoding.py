"""`oc8.net.decoding`: each step yields at most what still fits, and a
decoder says whether its stream ended where the encoding says it ends."""

from __future__ import annotations

import gzip
import zlib

import pytest

from oc8.net.decoding import UnsupportedContentEncoding, content_decoder, decode_step


def _deflate(data: bytes, *, raw: bool) -> bytes:
    packer = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS if raw else zlib.MAX_WBITS)
    return packer.compress(data) + packer.flush()


def test_identity_splits_at_the_room_and_is_always_complete() -> None:
    decoder = content_decoder("")
    assert decode_step(decoder, b"abcdef", 10) == (b"abcdef", False)
    assert decode_step(decoder, b"abcdef", 4) == (b"abcd", True)
    assert decoder.eof is True


@pytest.mark.parametrize("encoding", ["gzip", "x-gzip", "GZIP", "gzip, identity"])
def test_gzip_is_decoded_and_ends(encoding: str) -> None:
    decoder = content_decoder(encoding)
    assert decoder.eof is False
    assert decode_step(decoder, gzip.compress(b"hello"), 100) == (b"hello", False)
    assert decoder.eof is True


@pytest.mark.parametrize("raw", [True, False])
def test_deflate_raw_and_zlib_wrapped_are_decoded_and_end(raw: bool) -> None:
    decoder = content_decoder("deflate")
    assert decode_step(decoder, _deflate(b"hello", raw=raw), 100) == (b"hello", False)
    assert decoder.eof is True


@pytest.mark.parametrize(
    ("encoding", "pack"),
    [
        ("gzip", lambda d: gzip.compress(d, compresslevel=9)),
        ("deflate", lambda d: _deflate(d, raw=True)),
        ("deflate", lambda d: _deflate(d, raw=False)),
    ],
)
def test_a_bomb_never_yields_more_than_the_room_plus_one(encoding: str, pack: object) -> None:
    bomb = pack(b"\0" * 32_000_000)  # type: ignore[operator]
    decoder = content_decoder(encoding)
    biggest = 0
    real = decoder.decompress

    def spy(data: bytes, max_length: int) -> bytes:
        nonlocal biggest
        out = real(data, max_length)
        biggest = max(biggest, len(out))
        return out

    decoder.decompress = spy  # type: ignore[method-assign]
    out, overflow = decode_step(decoder, bomb, 1_000_000)
    assert overflow is True
    assert len(out) == 1_000_000
    assert biggest <= 1_000_001


def test_a_truncated_gzip_stream_does_not_end() -> None:
    decoder = content_decoder("gzip")
    _out, overflow = decode_step(decoder, gzip.compress(b"hello world")[:-6], 100)
    assert overflow is False
    assert decoder.eof is False


def test_a_multi_member_gzip_stops_after_the_first_member() -> None:
    decoder = content_decoder("gzip")
    body = gzip.compress(b"a") + gzip.compress(b"\0" * 10_000_000)
    assert decode_step(decoder, body, 1_000_000) == (b"a", False)
    assert decoder.eof is True


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, gzip", "deflate, gzip"])
def test_other_encodings_and_chains_are_refused(encoding: str) -> None:
    with pytest.raises(UnsupportedContentEncoding):
        content_decoder(encoding)
