"""The SandboxDriver contract. v1 implementation: DockerSandboxDriver."""

from __future__ import annotations

from typing import Protocol

from oc8.sandbox.types import ExecResult, SandboxHandle, SandboxSpec


class SandboxDriver(Protocol):
    async def provision(self, spec: SandboxSpec) -> SandboxHandle: ...

    async def exec(
        self,
        handle: SandboxHandle,
        command: list[str],
        *,
        workdir: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ExecResult: ...

    async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None: ...

    async def fs_read(self, handle: SandboxHandle, path: str) -> bytes: ...

    async def put_archive(self, handle: SandboxHandle, path: str, data: bytes) -> None:
        """Unpack the tar archive ``data`` into the directory ``path``.

        ``path`` is absolute; a missing directory is created. Unlike
        ``fs_write`` this has no size cap of its own beyond what the transport
        and the target directory allow.
        """
        ...

    async def get_archive(self, handle: SandboxHandle, path: str) -> bytes:
        """A tar archive of ``path``.

        For a directory, entries are named relative to it (``a.txt``,
        ``sub/b.bin``), so ``put_archive(h, path, get_archive(h, path))``
        restores it in place. For a file, the archive holds that one file
        under its base name. A missing path raises ``SandboxError``.
        """
        ...

    async def teardown(self, handle: SandboxHandle) -> None: ...

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        """Block until the container exits, for at most timeout_s; return its
        exit code.

        Two endings, and callers have been getting them the wrong way round:
        reaching timeout_s RAISES (the reference driver lets docker-py's
        requests.exceptions.ReadTimeout out), while -1 is returned only for a
        docker-level fault -- container gone, daemon error. So -1 is "no
        verdict", never "it took too long", and a caller that wants a bound on
        the work gets it from the exception, not from the return value.
        """
        ...

    async def logs(self, handle: SandboxHandle) -> str: ...

    async def reap_orphans(self) -> int:
        """Remove containers whose run has finished; return how many."""
        ...
