"""The sandbox contract every driver shares: lingering, the ``/.oc8``
marker dir, ``wait``, image tools and reads as the sandbox user.

A new driver (Docker in this repository; a Kubernetes one elsewhere) builds
on these helpers and follows this text. The protocol itself is
``oc8.sandbox.driver.SandboxDriver``.

**1. Lingering, and where the wrapper goes.** The spec's command runs under
``lingering_command``::

    sh -c 'trap : TERM INT; "$@"; c=$?; echo; echo >&2;
           echo "$c" > /.oc8/exit; trap "" TERM INT;
           exec sleep 2147483647 >/dev/null 2>&1' -- <cmd>

so the sandbox stays up after the command exits, with the command's exit
code in ``/.oc8/exit``; reading output and state through exec then works
after the command on any driver, including one that cannot read a stopped
sandbox. Teardown removes the sandbox in any state (SIGKILL).

- The wrapper replaces the spec's COMMAND (Docker ``Cmd``; on an
  orchestrator the container ``args``), never the image ENTRYPOINT, which is
  kept (tini in the CLI images).
- An empty command is not wrapped: the image's own CMD runs and ``wait``
  reports the container's own exit. A spec with a seed must name its
  command, or nothing guarantees the seed is in place before it starts.
- ``trap : TERM INT`` keeps the wrapper alive when the command is stopped (a
  caught signal is reset to its default in the command, which still gets
  it); ``trap "" TERM INT`` before the ``exec sleep`` keeps the sleep alive
  too (an ignored signal survives ``exec``). To stop the command, signal
  every process of the sandbox user (``STOP_COMMAND``, ``kill -TERM -1``),
  never PID 1.
- The two ``echo``s end a last line the command left unterminated: a log
  driver holds a partial line back while the stream is open, and a
  lingering sandbox's stream stays open. The log gains one blank line per
  stream.

**2. The ``/.oc8`` dir.** Driver-provided, small, writable by the sandbox user
(Docker: a tmpfs, mode 1777; Kubernetes: an ``emptyDir``). It holds:

- ``START_FILE`` (``/.oc8/start``): a driver that cannot unpack the seed
  before the container starts (Kubernetes) runs ``gated_command``, which
  waits for this file, and creates it after the seed is in;
- ``EXIT_FILE`` (``/.oc8/exit``): the command's exit code.

Everything in it is UNTRUSTED and forgeable: the sandbox can write the exit
code early or falsely, or put a FIFO or a link there. Nothing is trusted from
it beyond "the leg is over", and it must never be a seed target.

**3. ``wait``.** Report completion from ``EXIT_FILE``, read with
``READ_EXIT_COMMAND`` (a regular file only, never a link, at most 32 bytes,
killed after 2 s), through exec as the sandbox user. Bound every look in the
driver's own process as well, and check the deadline on every iteration,
whether the look succeeded, failed or timed out. Before returning, let the
log settle (the last output may still be on its way into it). A sandbox that
stopped anyway reports its own exit code. Reaching the timeout raises
``TimeoutError``; -1 is a driver-level fault, never "too long".

**4. Tools every sandbox image must have.** ``/bin/sh`` (with ``kill``,
``trap``, ``read``), ``tar``, ``timeout``, ``head`` (with ``-c``), ``stat -c``
or ``date -r``, and for the output listing GNU ``find -printf`` and
``head -z`` (busybox lacks the last two: the output sync then logs and syncs
nothing). Debian-based images have all of them; all images oc8 runs do: the
CLI base (node:22-bookworm-slim), the isolated shell (python slim), the
hooks worker (python alpine; it needs no output listing) and nanoclaw's
Debian and oven/bun images.

**5. Reading as the sandbox user.** ``archive_command`` is ``tar -c`` (never
``-h``) of a path, run through exec AS THE SANDBOX'S CONFIGURED USER (taken
from the sandbox's configuration, never from the sandbox), so it reaches
exactly what the sandbox's processes can:

- tar's stdout is the archive; stderr is read separately, for errors only;
- exit 0 or 1 (GNU tar: a file changed while it was read) is success; 40
  (``ARCHIVE_MISSING``) is a missing or unreadable path; 41
  (``ARCHIVE_SYMLINK``) a symlink at the path itself; anything else fails;
- links INSIDE the tree come back as link entries; links in the path's
  INTERMEDIATE components are resolved by the shell, as that user -- so they
  reach nothing that user could not read anyway;
- ``relative_archive`` renames entries to the contract's layout.

**6. ``max_lifetime_s``.** An upper bound on the sandbox's life, set by the
runtime to cover its whole leg (deadline, stop, state saves, last output
pass) plus slack. Past it the driver (or its reaper) may remove the sandbox
whatever the run says; it is a backstop for a worker that died, not a
deadline a leg is expected to reach.
"""

from __future__ import annotations

import tarfile
import tempfile
from typing import IO

from oc8.sandbox.archive import is_outside
from oc8.sandbox.types import SandboxError

#: Driver-provided, small, writable by the sandbox user; untrusted.
MARKER_DIR = "/.oc8"
EXIT_FILE = f"{MARKER_DIR}/exit"
START_FILE = f"{MARKER_DIR}/start"

_WRAPPER = (
    'trap : TERM INT; "$@"; c=$?; echo; echo >&2; '
    f"echo \"$c\" > {EXIT_FILE}; trap '' TERM INT; "
    "exec sleep 2147483647 >/dev/null 2>&1"
)

#: Above this, a rewritten archive is spooled to disk rather than memory.
SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024

#: Exit codes of ``archive_command`` that are not tar's own.
ARCHIVE_MISSING = 40
ARCHIVE_SYMLINK = 41


def lingering_command(command: list[str]) -> list[str]:
    """``command`` under the lingering wrapper; an empty one stays empty."""
    if not command:
        return command
    return ["sh", "-c", _WRAPPER, "--", *command]


def gated_command(command: list[str]) -> list[str]:
    """``lingering_command(command)`` that first waits for ``START_FILE``: for
    a driver that seeds a sandbox after its container has started. Nothing
    of ``command`` runs before the driver creates the file. An empty command
    stays empty (and a seeded spec must name one)."""
    if not command:
        return command
    gate = f"until [ -e {START_FILE} ]; do sleep 0.2; done; "
    return ["sh", "-c", gate + _WRAPPER, "--", *command]


#: Prints the recorded exit code, or nothing while the command still runs.
#: Only a regular file, never a link, at most 32 bytes, and killed after 2 s:
#: the sandbox can put a FIFO or a link at the marker path.
READ_EXIT_COMMAND = [
    "timeout",
    "-s",
    "KILL",
    "2",
    "sh",
    "-c",
    f"[ -f {EXIT_FILE} ] && [ ! -L {EXIT_FILE} ] && head -c 32 {EXIT_FILE}",
]

#: When the command ended (the marker's mtime, Unix seconds), or nothing.
EXIT_TIME_COMMAND = [
    "timeout",
    "-s",
    "KILL",
    "5",
    "sh",
    "-c",
    f"stat -c %Y {EXIT_FILE} 2>/dev/null || date -r {EXIT_FILE} +%s 2>/dev/null",
]

#: Signals every process of the sandbox user but the caller and PID 1: the
#: command and its children. The wrapper traps it and lives on.
STOP_COMMAND = ["sh", "-c", "kill -TERM -1 2>/dev/null; exit 0"]


def parse_exit(output: str) -> int | None:
    text = output.strip()
    if not text or not text.lstrip("-").isdigit():
        return None
    return int(text)


def archive_command(path: str) -> list[str]:
    """A tar of ``path`` on stdout: a directory's contents named from inside
    it (``./a.txt``), a file under its base name. A symlink at ``path``
    exits ``ARCHIVE_SYMLINK``, a missing or unreadable one ``ARCHIVE_MISSING``.
    Links inside a directory are archived as links (no ``-h``)."""
    script = (
        'p="$1"; '
        f'[ -L "$p" ] && {{ echo "is a symlink" >&2; exit {ARCHIVE_SYMLINK}; }}; '
        'if [ -d "$p" ]; then cd "$p" && exec tar -c -f - .; fi; '
        'if [ -f "$p" ] && [ -r "$p" ]; then '
        'cd "$(dirname "$p")" && exec tar -c -f - "$(basename "$p")"; fi; '
        f'echo "missing or unreadable" >&2; exit {ARCHIVE_MISSING}'
    )
    return ["sh", "-c", script, "--", path]


def tar_succeeded(code: int) -> bool:
    """GNU tar exits 1 when a file changed while it was read: the archive is
    still whole, which is normal for a live sandbox."""
    return code in (0, 1)


def archive_error(path: str, code: int, stderr: str) -> SandboxError:
    if code == ARCHIVE_SYMLINK:
        return SandboxError(f"get_archive: {path} is a symlink")
    if code == ARCHIVE_MISSING:
        return SandboxError(f"get_archive: {path} does not exist or is not readable")
    return SandboxError(f"get_archive: tar of {path} exited {code}: {stderr[-300:]}")


def _strip_dot(name: str) -> str | None:
    """``./a/b`` -> ``a/b``; ``.`` and ``./`` -> None (the directory itself)."""
    stripped = name
    while stripped.startswith("./"):
        stripped = stripped[2:]
    stripped = stripped.rstrip("/")
    if stripped in ("", "."):
        return None
    if stripped.startswith("/") or is_outside(stripped):
        raise SandboxError(f"archive entry is outside the directory: {name!r}")
    return stripped


def relative_archive(src_file: IO[bytes]) -> bytes:
    """``tar -c -C dir .`` output renamed to the contract's layout: entries
    and hardlink targets named from inside the directory, without ``./``.
    Streams one member at a time into a spooled file (on disk above
    ``SPOOL_IN_MEMORY_BYTES``), so only the returned result is held whole."""
    out: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY_BYTES)
    try:
        with (
            tarfile.open(fileobj=src_file, mode="r|") as src,
            tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as dst,
        ):
            for member in src:
                name = _strip_dot(member.name)
                if name is None:
                    continue
                member.name = name
                if member.islnk():
                    target = _strip_dot(member.linkname)
                    if target is None:
                        raise SandboxError(
                            f"archive hardlink is outside the directory: {member.linkname!r}"
                        )
                    member.linkname = target
                dst.addfile(member, src.extractfile(member) if member.isfile() else None)
    except tarfile.TarError as exc:
        out.close()
        raise SandboxError(f"get_archive: unreadable archive: {exc}") from exc
    except BaseException:
        out.close()
        raise
    with out:
        out.seek(0)
        return out.read()
