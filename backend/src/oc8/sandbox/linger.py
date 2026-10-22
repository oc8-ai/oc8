"""A sandbox outlives its command, and is read only as its own user.

Both halves are shared by every driver (Docker here; a Kubernetes driver
does the same with an ``emptyDir`` at ``/.oc8`` and exec as the pod's
``runAsUser``).

**Lingering.** The spec's command runs under ``lingering_command``::

    sh -c 'trap : TERM INT; "$@"; c=$?; echo; echo >&2;
           echo "$c" > /.oc8/exit; trap "" TERM INT;
           exec sleep 2147483647 >/dev/null 2>&1' -- <cmd>

so the sandbox stays up after the command exits, with the command's exit
code in ``/.oc8/exit``. ``wait`` reports completion from that file; reading
output and a CLI home after the command, through exec, keeps working on any
driver, including one that cannot read a stopped sandbox at all. Teardown
removes the sandbox as before (SIGKILL; nothing ignores that). Before the
``exec sleep`` the wrapper switches the traps to IGNORE: a caught trap is
reset by ``exec``, an ignored one is kept, so a stop that arrives after the
command exited (a parked leg's ``stop_cli``) cannot end the ``sleep`` and,
with tini as PID 1, the whole sandbox. The two ``echo``s end a last line the command
left unterminated: a log driver holds a partial line back until the stream
closes, and a lingering sandbox's stream stays open (so the log gains a blank
line on each stream). The trap makes the wrapper survive a SIGTERM
meant for the command (a caught signal is reset to its default in the
command, so the command itself still gets it): to stop the command, signal
every process of the sandbox user (``kill -TERM -1``), not PID 1.

The marker lives in a directory the sandbox can write (it must, to record
the exit), so the sandbox can also write it early or forge a code. That only
ends its own leg sooner; nothing is trusted from it beyond "the leg is over".

**Every image needs ``/bin/sh``.** All images oc8 runs have one: the CLI
runtime base (node:22-bookworm-slim), the isolated shell (python slim), the
hooks worker (python alpine), and nanoclaw's agent and provisioner images
(Debian, oven/bun). A spec with no command keeps the image's own and is not
wrapped; ``wait`` then reports the container's own exit, as before.

**Reading as the sandbox user.** ``archive_command`` is ``tar -c`` (never
``-h``) of a path, run through exec AS THE SANDBOX'S CONFIGURED USER, which
the driver takes from the sandbox's configuration, never from the sandbox.
The read can then reach exactly what the sandbox's own processes can, so a
link or a swapped directory pointing at ``/etc/shadow`` yields nothing.
"""

from __future__ import annotations

import tarfile
import tempfile
from typing import IO

from oc8.sandbox.archive import is_outside
from oc8.sandbox.types import SandboxError

#: Driver-provided, small, writable by the sandbox user.
MARKER_DIR = "/.oc8"
EXIT_FILE = f"{MARKER_DIR}/exit"

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
