"""The SandboxDriver contract. v1 implementation: DockerSandboxDriver.

The shared rules every driver follows live in ``oc8.sandbox.linger``'s
docstring: the lingering wrapper and where it goes (the command/args, never
the entrypoint), the ``/.oc8`` dir and its untrusted markers, ``wait``, the
tools every sandbox image needs (``sh``, ``tar``, ``timeout``, ``head``,
``stat``/``date``, ``kill``, GNU ``find -printf``, ``head -z``), reads as the
sandbox user and ``max_lifetime_s``. The spec fields are documented on
``oc8.sandbox.types.SandboxSpec``.

In short: a sandbox outlives its command until teardown and stays readable
through exec; ``exec`` and every read run as the sandbox's configured user,
never with more privilege.
"""

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

        Identity and ownership: a driver may unpack as root (Docker's archive
        API) or as the sandbox user (tar over exec), and may ignore the
        archive's owner fields. A portable caller therefore targets only the
        sandbox's scratch dirs (writable by any user) and does not rely on
        header ownership. Content is the CALLER's, never the sandbox's:
        never ``put_archive`` onto a path the sandbox controls after its
        command has started (it could have swapped a directory for a link to
        somewhere the unpacking identity may write). The seed
        (``SandboxSpec.seed``) is the way to put files in before start.
        """
        ...

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        """A tar archive of ``path``.

        For a directory, entries are named relative to it (``a.txt``,
        ``sub/b.bin``), and so are hardlink targets, so
        ``put_archive(h, path, get_archive(h, path))`` restores it in place.
        For a file, the archive holds that one file under its base name. A
        missing path raises ``SandboxError``.

        ``max_bytes`` (default: ``sandbox_archive_max_bytes``) is enforced
        WHILE reading: past it the read stops and ``ArchiveTooLarge`` is
        raised, so an oversized archive is never buffered whole.

        Read with ``tar -c`` (``oc8.sandbox.linger.archive_command``) through
        exec, AS THE SANDBOX'S CONFIGURED USER -- the user the driver set at
        create time, never one the sandbox reports -- so it reaches exactly
        what the sandbox's own processes can. Never through a privileged path
        (Docker's archive API reads as root). Never follows symlinks: a link
        inside the tree is returned as a link entry, and a symlink at
        ``path`` itself raises ``SandboxError``; never ``tar -h``. Works after
        the command has exited, because the sandbox lingers.

        The result is UNTRUSTED. The process in the sandbox chose every name,
        link and size in it. Never unpack it with plain ``extractall``: use
        ``oc8.sandbox.archive.safe_extract_to_memory``, or ``tarfile`` with
        ``filter="data"`` plus limits on member count and total size.
        """
        ...

    async def teardown(self, handle: SandboxHandle) -> None:
        """Remove the sandbox, whether or not its command has exited."""
        ...

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        """Block until the sandbox's COMMAND exits, for at most timeout_s;
        return its exit code (from ``/.oc8/exit``; the sandbox itself lingers
        until teardown). A sandbox that stopped anyway reports its own code.

        Two endings, and callers have been getting them the wrong way round:
        reaching timeout_s RAISES (``TimeoutError`` from the Docker driver,
        ``SandboxError`` over the provisioner), while -1 is returned only for
        a driver-level fault -- sandbox gone, daemon error. So -1 is "no
        verdict", never "it took too long", and a caller that wants a bound on
        the work gets it from the exception, not from the return value.
        """
        ...

    async def logs(self, handle: SandboxHandle) -> str: ...

    async def reap_orphans(self) -> int:
        """Remove containers whose run has finished; return how many."""
        ...
