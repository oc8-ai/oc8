"""`safe_extract_to_memory`: the one way to read an archive a sandbox produced.

Everything a sandbox returns is untrusted -- the agent inside it chose every
name, link and size in the tar. These are the shapes a hostile one would try.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from oc8.sandbox.archive import safe_extract_to_memory
from oc8.sandbox.types import SandboxError


def _tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, content in members:
            tar.addfile(info, io.BytesIO(content) if content is not None else None)
    return buf.getvalue()


def _file(name: str, content: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    return info, content


def _link(name: str, target: str, kind: bytes) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def _dir(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, None


def test_regular_files_come_back_by_name_and_directories_are_dropped() -> None:
    data = _tar(_dir("sub"), _file("a.txt", b"alpha"), _file("./sub/b.txt", b"beta"))
    assert safe_extract_to_memory(data, max_members=10, max_total_bytes=100) == {
        "a.txt": b"alpha",
        "sub/b.txt": b"beta",
    }


@pytest.mark.parametrize(
    "member",
    [
        _link("evil", "/etc/passwd", tarfile.SYMTYPE),
        _link("evil", "a.txt", tarfile.SYMTYPE),
        _link("evil", "a.txt", tarfile.LNKTYPE),
    ],
    ids=["absolute-symlink", "relative-symlink", "hardlink"],
)
def test_links_are_rejected(member: tuple[tarfile.TarInfo, None]) -> None:
    data = _tar(_file("a.txt", b"alpha"), member)
    with pytest.raises(SandboxError, match="link"):
        safe_extract_to_memory(data, max_members=10, max_total_bytes=100)


@pytest.mark.parametrize("name", ["../escape.txt", "sub/../../escape.txt", "/etc/passwd"])
def test_a_name_outside_the_archive_root_is_rejected(name: str) -> None:
    with pytest.raises(SandboxError, match="path"):
        safe_extract_to_memory(_tar(_file(name, b"x")), max_members=10, max_total_bytes=100)


def test_a_device_or_fifo_is_rejected() -> None:
    fifo = tarfile.TarInfo("pipe")
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(SandboxError):
        safe_extract_to_memory(_tar((fifo, None)), max_members=10, max_total_bytes=100)


def test_a_bomb_over_the_size_limit_is_rejected_from_its_headers() -> None:
    """Rejected on the declared sizes, before any content is copied out."""
    data = _tar(_file("a.bin", b"x" * 600), _file("b.bin", b"y" * 600))
    with pytest.raises(SandboxError, match="size"):
        safe_extract_to_memory(data, max_members=10, max_total_bytes=1000)


def test_too_many_members_are_rejected() -> None:
    data = _tar(*(_file(f"f{i}", b"") for i in range(6)))
    with pytest.raises(SandboxError, match="members"):
        safe_extract_to_memory(data, max_members=5, max_total_bytes=1000)


def test_a_compressed_archive_is_not_unpacked() -> None:
    """Compression would turn a small body into an unbounded one."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info, content = _file("a.txt", b"alpha")
        tar.addfile(info, io.BytesIO(content))
    with pytest.raises(SandboxError):
        safe_extract_to_memory(buf.getvalue(), max_members=10, max_total_bytes=100)


def test_links_and_special_files_can_be_skipped_and_are_reported() -> None:
    fifo = tarfile.TarInfo("pipe")
    fifo.type = tarfile.FIFOTYPE
    data = _tar(
        _file("keep.txt", b"k"),
        _link("sym", "/etc/shadow", tarfile.SYMTYPE),
        _link("hard", "keep.txt", tarfile.LNKTYPE),
        (fifo, None),
    )
    skipped: list[str] = []
    files = safe_extract_to_memory(
        data, max_members=10, max_total_bytes=100, on_link="skip", skipped=skipped
    )
    assert files == {"keep.txt": b"k"}
    assert skipped == ["sym", "hard", "pipe"]


def test_skipping_links_still_rejects_names_outside_the_root() -> None:
    data = _tar(_link("../sym", "x", tarfile.SYMTYPE))
    with pytest.raises(SandboxError):
        safe_extract_to_memory(data, max_members=10, max_total_bytes=100, on_link="skip")
