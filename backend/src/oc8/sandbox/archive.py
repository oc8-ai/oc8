"""Reading archives that came out of a sandbox.

Everything ``SandboxDriver.get_archive`` returns is UNTRUSTED: the process in
the sandbox chose every name, link target and size in it. Never hand such an
archive to ``tarfile.extractall`` without ``filter="data"`` and limits on
member count and total size; to read files into memory, use
``safe_extract_to_memory``.
"""

from __future__ import annotations

import io
import posixpath
import tarfile

from oc8.sandbox.types import SandboxError


def is_outside(name: str) -> bool:
    """Whether a member name would land outside the archive's root: absolute,
    or climbing out through ``..``."""
    if name.startswith("/"):
        return True
    return ".." in posixpath.normpath(name).split("/")


def safe_extract_to_memory(
    data: bytes, *, max_members: int, max_total_bytes: int
) -> dict[str, bytes]:
    """The regular files in the tar ``data``, by normalised name.

    Directories are skipped. Everything else is refused with ``SandboxError``:
    symlinks and hardlinks, devices and fifos, names that are absolute or
    climb out with ``..``, more than ``max_members`` members, more than
    ``max_total_bytes`` of declared file content (checked from the headers,
    before content is copied), and compressed archives.
    """
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for count, member in enumerate(tar, start=1):
                if count > max_members:
                    raise SandboxError(f"archive has more than {max_members} members")
                if is_outside(member.name):
                    raise SandboxError(f"archive path leaves its root: {member.name!r}")
                if member.issym() or member.islnk():
                    raise SandboxError(f"archive contains a link: {member.name!r}")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise SandboxError(f"archive contains a special file: {member.name!r}")
                total += member.size
                if total > max_total_bytes:
                    raise SandboxError(f"archive content exceeds {max_total_bytes} bytes in size")
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise SandboxError(f"archive member is unreadable: {member.name!r}")
                files[posixpath.normpath(member.name)] = extracted.read()
    except tarfile.TarError as exc:
        raise SandboxError(f"archive is not a plain tar: {exc}") from exc
    return files
