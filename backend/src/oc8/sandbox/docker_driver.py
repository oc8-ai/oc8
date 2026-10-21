"""Docker-backed SandboxDriver (v1) — talks to the host daemon via docker-py.

All blocking docker-py calls run in a worker thread (asyncio.to_thread) so the
event loop is never blocked. Containers get ONLY spec.env — never the host
environment — so oc8/provider secrets cannot leak into a sandbox."""

from __future__ import annotations

import asyncio
import io
import posixpath
import tarfile
import tempfile
import time
from collections.abc import Iterator
from typing import IO, Any

import docker
from docker.errors import NotFound
from docker.models.containers import Container
from docker.types import DriverConfig, Mount

from oc8.config import get_settings
from oc8.sandbox.archive import is_outside
from oc8.sandbox.scratch import validate_scratch
from oc8.sandbox.types import (
    ArchiveTooLarge,
    ExecResult,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
    ScratchDir,
)

#: Every container/label filter that acts on "ours" must match on both of
#: these -- see oc8.sandbox.reaper. NAMESPACE_LABEL is deliberately NOT part
#: of the Docker-side list filter reap uses (a container built before this
#: label existed would then be invisible forever); it is compared in Python
#: instead, with a missing label read as the default namespace.
NAMESPACE_LABEL = "oc8.namespace"

#: Unix time after which the reaper removes the container whatever its run
#: says. Stamped from ``SandboxSpec.max_lifetime_s``; see oc8.sandbox.reaper.
EXPIRES_LABEL = "oc8.expires-at"


def _labels(extra: dict[str, str], *, max_lifetime_s: int | None = None) -> dict[str, str]:
    # Ours-ness labels last, so a caller-supplied `spec.labels` entry of the
    # same key can never spoof which sandbox/namespace a container claims to
    # belong to. The deadline likewise comes only from `max_lifetime_s`: a
    # spec label of that key is dropped, never trusted.
    labels = {k: v for k, v in extra.items() if k != EXPIRES_LABEL}
    if max_lifetime_s is not None:
        labels[EXPIRES_LABEL] = str(int(time.time()) + int(max_lifetime_s))
    return {**labels, "oc8.sandbox": "1", NAMESPACE_LABEL: get_settings().deployment_namespace}


def _scratch_mount(scratch: ScratchDir, *, sized: bool, labels: dict[str, str]) -> Mount:
    """An anonymous volume for one scratch dir.

    Anonymous, so it is created with the container and removed with it
    (``remove(v=True)``). ``sized`` asks the local volume driver for a quota,
    which it grants only on a quota-capable backing filesystem (xfs with
    ``pquota``); elsewhere the daemon refuses the create and the driver retries
    unsized -- see ``DockerSandboxDriver._create``. The volume carries the
    container's labels, so a leaked one can be found and attributed.
    """
    driver_config = DriverConfig("local", {"size": f"{scratch.size_mb}m"}) if sized else None
    return Mount(
        target=scratch.path,
        source=None,
        type="volume",
        driver_config=driver_config,
        labels=labels,
    )


def _scratch_modes(scratch: tuple[ScratchDir, ...]) -> bytes:
    """A tar that makes every scratch dir mode 1777, owned by root.

    A fresh volume over a path the image lacks is root's and 0755, which a
    runtime image running as its own uid cannot write. Kubernetes' emptyDir is
    world-writable too; the sticky bit, as on /tmp, keeps one user from
    deleting another's files in it.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in scratch:
            info = tarfile.TarInfo(name=entry.path.strip("/"))
            info.type = tarfile.DIRTYPE
            info.mode = 0o1777
            info.mtime = int(time.time())
            tar.addfile(info)
    return buf.getvalue()


_GO_MODE_DIR = 1 << 31
#: Go's os.ModeSymlink, as reported in the archive stat header.
_GO_MODE_SYMLINK = 1 << 27


def _quota_refused(exc: BaseException) -> bool:
    return "quota" in str(exc).lower()


#: Above this, an archive read from Docker is spooled to a temporary file
#: instead of held in memory while it is rewritten.
_SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024


def _spool_capped(chunks: Iterator[bytes], max_bytes: int) -> IO[bytes]:
    """Copy Docker's archive stream into a spooled file, stopping at the cap.

    The stream is abandoned at the first chunk that crosses ``max_bytes``:
    nothing past it is read, and nothing past one chunk is buffered.
    """
    spool: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY_BYTES)
    total = 0
    try:
        for chunk in chunks:
            total += len(chunk)
            if total > max_bytes:
                raise ArchiveTooLarge(f"archive exceeds {max_bytes} bytes")
            spool.write(chunk)
    except BaseException:
        spool.close()
        close = getattr(chunks, "close", None)
        if callable(close):
            close()
        raise
    spool.seek(0)
    return spool


def _relative_name(name: str, top: str) -> str | None:
    """``name`` relative to the archived directory ``top``; None for ``top``
    itself. Anything that does not sit under ``top`` is refused: Docker never
    writes such a name, so one that appears was not written by Docker."""
    if name.rstrip("/") == top:
        return None
    prefix = top + "/"
    if not name.startswith(prefix):
        raise SandboxError(f"archive entry is outside {top!r}: {name!r}")
    relative = name[len(prefix) :]
    if not relative or is_outside(relative):
        raise SandboxError(f"archive entry is outside {top!r}: {name!r}")
    return relative


def _relative_to_directory(src_file: IO[bytes], top: str) -> bytes:
    """Rename a Docker directory archive's entries to be relative to it.

    Docker names them from the directory's own base name down
    (``output/a.txt``); the contract names them from inside it (``a.txt``).
    A hardlink names its target the same way, so ``linkname`` is renamed too,
    or the link would point at nothing once the archive is unpacked. Read and
    written as streams: one member at a time, never the whole input.
    """
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=src_file, mode="r|") as src,
        tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as dst,
    ):
        for member in src:
            relative = _relative_name(member.name, top)
            if relative is None:
                continue
            member.name = relative
            if member.islnk():
                target = _relative_name(member.linkname, top)
                if target is None:
                    raise SandboxError(f"archive hardlink is outside {top!r}: {member.linkname!r}")
                member.linkname = target
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
    return out.getvalue()


def _archive_cap(max_bytes: int | None) -> int:
    return max_bytes if max_bytes is not None else get_settings().sandbox_archive_max_bytes


#: Sensible default per-call exec timeout (seconds) for callers that want one
#: (e.g. the Slice 3 coding loop). ``exec`` itself defaults its ``timeout``
#: kwarg to ``None`` (no timeout) to keep existing callers/tests unchanged;
#: callers that want protection against hung commands should pass
#: ``timeout=DEFAULT_EXEC_TIMEOUT`` explicitly.
DEFAULT_EXEC_TIMEOUT = 300


def registry_host_of(image: str) -> str:
    """The registry host an image reference pulls from.

    A name with no registry (`alpine:latest`) is Docker Hub. The host is the
    first path segment when that segment contains a dot, a colon, or is
    `localhost`. The tag colon after the last slash is not part of the host.
    """
    name = image.split("@", 1)[0]
    slash = name.find("/")
    if slash < 0:
        return "docker.io"
    first = name[:slash]
    if first == "localhost" or "." in first or ":" in first:
        return first
    return "docker.io"


def _split_image_ref(image: str) -> tuple[str, str | None]:
    name = image.split("@", 1)[0]
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1 :]
    return name, None


def _pull_was_refused(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        word in text
        for word in (
            "unauthorized",
            "authentication",
            "denied",
            "pull access",
            "insufficient_scope",
        )
    )


def _ensure_local_image(client: docker.DockerClient, spec: SandboxSpec) -> None:  # type: ignore[name-defined]
    """Pull `spec.image` when the daemon does not have it yet.

    `registry_auth` is passed only to the pull. A private tag with no login
    fails before a container is created, and says to save the registry login
    on the runtime. A login whose host is not the image's registry is refused
    so the token is not sent anywhere else.
    """
    try:
        client.images.get(spec.image)
        return
    except docker.errors.ImageNotFound:
        pass
    auth = spec.registry_auth
    if auth is not None and auth.registry and auth.registry != registry_host_of(spec.image):
        raise SandboxError(
            f"registry login is for {auth.registry!r} but the image is on "
            f"{registry_host_of(spec.image)!r}"
        )
    repository, tag = _split_image_ref(spec.image)
    auth_config = None if auth is None else {"username": auth.username, "password": auth.password}
    try:
        client.images.pull(repository, tag=tag, auth_config=auth_config)
    except docker.errors.DockerException as exc:
        if auth is None and _pull_was_refused(exc):
            raise SandboxError(
                "image is missing and the registry refused an anonymous pull; "
                "add the registry login in the runtime setup"
            ) from exc
        raise SandboxError(f"image pull failed: {exc}") from exc


def _force_remove(container: Container) -> None:
    """Best-effort cleanup of a partially-provisioned container.

    Never lets a cleanup failure mask the original provisioning error.
    """
    try:
        container.remove(force=True, v=True)
    except docker.errors.DockerException:
        pass


class DockerSandboxDriver:
    def __init__(self, client: docker.DockerClient | None = None) -> None:  # type: ignore[name-defined]
        self._client = client or docker.from_env()  # type: ignore[attr-defined]
        # Whether this daemon can size a volume. None until the first sized
        # scratch dir asks; then remembered so later provisions do not pay a
        # refused create each.
        self._scratch_quota: bool | None = None

    def _start(self, spec: SandboxSpec, run_kwargs: dict[str, object]) -> Container:
        """Create and start the container.

        A spec with no scratch and no seed takes the one-call path it always
        took. Otherwise: create, unpack the archives into the CREATED
        container -- its filesystem and volumes exist, its process does not --
        and only then start. Scratch modes go in first, so a seed entry inside
        a scratch dir keeps its own mode.
        """
        if not spec.scratch and spec.seed is None:
            return self._client.containers.run(spec.image, detach=True, **run_kwargs)
        container = self._create(spec, run_kwargs)
        try:
            if spec.scratch and not container.put_archive("/", _scratch_modes(spec.scratch)):
                raise SandboxError("provision failed: scratch dirs were refused")
            if spec.seed is not None and not container.put_archive("/", spec.seed):
                raise SandboxError("provision failed: the seed was refused")
            container.start()
        except BaseException:
            _force_remove(container)
            raise
        return container

    def _create(self, spec: SandboxSpec, run_kwargs: dict[str, object]) -> Container:
        """Create (not start) the container, sizing scratch volumes if the
        daemon can and falling back to unsized ones if it cannot."""
        labels: Any = run_kwargs["labels"]
        if not spec.scratch:
            return self._client.containers.create(spec.image, **run_kwargs)
        if self._scratch_quota is not False:
            mounts = [_scratch_mount(s, sized=True, labels=labels) for s in spec.scratch]
            try:
                container = self._client.containers.create(spec.image, mounts=mounts, **run_kwargs)
            except docker.errors.APIError as exc:
                if not _quota_refused(exc):
                    raise
                self._scratch_quota = False
            else:
                self._scratch_quota = True
                return container
        mounts = [_scratch_mount(s, sized=False, labels=labels) for s in spec.scratch]
        return self._client.containers.create(spec.image, mounts=mounts, **run_kwargs)

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        """Create the container, unpack scratch modes and the seed, then start.

        The order (see ``_start``) is the guarantee: nothing in ``spec.seed`` can be missing
        when the command's first instruction runs, because the process does
        not exist until every archive is in. ``spec.egress`` is accepted and
        ignored -- Docker has no allowlist to apply, so the network settings
        stay exactly what ``network_disabled``/``network`` say.
        """

        validate_scratch(spec)

        def _run() -> SandboxHandle:
            run_kwargs: dict[str, object] = dict(
                command=spec.command,
                environment=dict(spec.env),
                working_dir=spec.workdir,
                labels=_labels(spec.labels, max_lifetime_s=spec.max_lifetime_s),
                tty=False,
                mem_limit=spec.mem_limit,
                pids_limit=spec.pids_limit,
                nano_cpus=int(spec.cpu_limit * 1_000_000_000),
                network_disabled=spec.network_disabled,
                cap_drop=spec.cap_drop,
                security_opt=["no-new-privileges"],
                privileged=False,
            )
            if spec.network is not None and not spec.network_disabled:
                run_kwargs["network"] = spec.network
            if spec.runtime is not None:
                run_kwargs["runtime"] = spec.runtime
            if spec.read_only:
                run_kwargs["read_only"] = True
            if spec.user is not None:
                run_kwargs["user"] = spec.user
            if spec.name is not None:
                run_kwargs["name"] = spec.name
            try:
                _ensure_local_image(self._client, spec)
            except docker.errors.DockerException as exc:
                raise SandboxError(f"provision failed: {exc}") from exc
            if spec.mounts:
                # Deliberately unvalidated here: the driver has no idea what root is
                # legitimate for a given caller. `sandbox.mounts.validate_mounts`
                # is the gate, and every caller must pass through it.
                run_kwargs["volumes"] = {
                    m.host_path: {"bind": m.container_path, "mode": "ro" if m.readonly else "rw"}
                    for m in spec.mounts
                }
            try:
                container = self._start(spec, run_kwargs)
            except docker.errors.DockerException as exc:
                raise SandboxError(f"provision failed: {exc}") from exc

            # `working_dir` above already makes Docker create the directory, so
            # this exec only has to cover the case a mount shadowed it. It runs
            # ONLY while the container is still up, because a one-shot image --
            # the provisioner is one -- can finish its whole job before this
            # line is reached. Exec-ing into an exited container then fails with
            # 126 or a 409, and provisioning reported a failure for a container
            # that had already succeeded. Measured 2026-08-02: one run in three
            # of `test_the_session_dbs_use_the_journal_mode_the_harness_requires`
            # died that way IN ISOLATION, which is also part of what this repo
            # has been recording as "nanoclaw is flaky in full runs".
            container.reload()
            if container.status == "running":
                try:
                    res = container.exec_run(["mkdir", "-p", spec.workdir])
                except docker.errors.APIError as exc:
                    # 409 is "container is not running" -- it exited between the
                    # reload and here. Same benign case as the branch above.
                    if exc.response is not None and exc.response.status_code == 409:
                        return SandboxHandle(container_id=container.id, image=spec.image)
                    _force_remove(container)
                    raise SandboxError(f"provision failed: {exc}") from exc
                except docker.errors.DockerException as exc:
                    _force_remove(container)
                    raise SandboxError(f"provision failed: {exc}") from exc
                if res.exit_code != 0:
                    # The docstring above already names this exact race's OTHER
                    # shape: the container can exit between `reload()` and here,
                    # and when it does the daemon does not always raise (the 409
                    # branch above) -- sometimes the exec API call itself
                    # succeeds, but the process it describes never truly started
                    # in a container whose namespace was already tearing down, so
                    # it comes back with a nonzero code that LOOKS like a real
                    # command failure (measured: 126) but is not one. A fresh
                    # status check is what tells the two apart: a container that
                    # is no longer running by the time we ask again cannot be
                    # trusted to have "genuinely" failed a command, so treat that
                    # the same as the 409 case. Only a container that is STILL
                    # running gets treated as a real failure.
                    container.reload()
                    if container.status != "running":
                        return SandboxHandle(container_id=container.id, image=spec.image)
                    _force_remove(container)
                    raise SandboxError(
                        f"provision failed: mkdir -p {spec.workdir} exited {res.exit_code}"
                    )
            return SandboxHandle(container_id=container.id, image=spec.image)

        return await asyncio.to_thread(_run)

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        """Block until the container exits, for at most timeout_s; return its exit code.

        This bound is what closes a WEDGED container: a run whose container never
        exits ends here, at timeout_s, and its caller's `finally` tears the
        container down. It ends by RAISING, not by returning -1 -- docker-py
        raises requests.exceptions.ReadTimeout on the request timeout, which is
        not a DockerException and so is not caught below (verified against the
        installed docker-py: APIClient.wait documents exactly that). The
        executor's `except Exception` turns it into a failed run that says why.
        -1 is returned for docker-level faults (container gone, daemon error).
        """

        def _run() -> int:
            try:
                container = self._client.containers.get(handle.container_id)
                res = container.wait(timeout=timeout_s)
                return int(res.get("StatusCode", -1))
            except docker.errors.DockerException:
                return -1

        return await asyncio.to_thread(_run)

    async def logs(self, handle: SandboxHandle) -> str:
        def _run() -> str:
            try:
                container = self._client.containers.get(handle.container_id)
                # Trailing window, not a hard cap on what a runtime plugin
                # CAN emit: claude_code_runtime already documents dropping
                # --include-partial-messages specifically to keep its own
                # output under the old 4000-char version of this, and
                # opencode_runtime has no equivalent lever to drop (its
                # verbosity comes from the MCP tool schema list itself, not
                # an optional flag) -- so a wide tool grant (github+jira+
                # hubspot together, ~80 tools) reliably pushed the terminal
                # event out before this cap could catch it. Bumped 20x rather
                # than removed: still a deliberate bound against one runaway
                # container's log flooding memory/DB, just sized for a
                # tool-schema-heavy session instead of a minimal one.
                out: str = container.logs().decode("utf-8", "replace")[-80_000:]
                return out
            except docker.errors.DockerException:
                return ""

        return await asyncio.to_thread(_run)

    async def teardown(self, handle: SandboxHandle) -> None:
        def _run() -> None:
            try:
                # v=True: scratch dirs are anonymous volumes and go with it.
                self._client.containers.get(handle.container_id).remove(force=True, v=True)
            except NotFound:
                return
            except docker.errors.DockerException as exc:
                raise SandboxError(f"teardown failed: {exc}") from exc

        await asyncio.to_thread(_run)

    async def exec(
        self,
        handle: SandboxHandle,
        command: list[str],
        *,
        workdir: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ExecResult:
        """Run ``command`` in the sandbox container.

        When ``timeout`` is given, the command is wrapped with the
        container's own ``timeout`` utility (busybox on alpine; verified to
        accept ``-s KILL``) so the *running process* is SIGKILLed on expiry —
        not just abandoned on the Python side. This frees the
        ``asyncio.to_thread`` worker even if the process itself ignores
        SIGTERM, closing the DoS gap where a hung command could hold a
        worker thread forever. A timed-out command yields a non-zero exit
        code (137 for SIGKILL). Callers that want a standing default should
        pass ``timeout=DEFAULT_EXEC_TIMEOUT``; ``exec`` itself defaults to
        ``None`` (no timeout) to keep existing callers unchanged.
        """
        if timeout is not None:
            command = ["timeout", "-s", "KILL", str(int(timeout)), *command]

        def _run() -> ExecResult:
            try:
                container = self._client.containers.get(handle.container_id)
                code, output = container.exec_run(command, workdir=workdir)
            except docker.errors.DockerException as exc:
                raise SandboxError(f"exec failed: {exc}") from exc
            text = output.decode("utf-8", errors="replace") if output else ""
            return ExecResult(exit_code=int(code), output=text)

        return await asyncio.to_thread(_run)

    async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        directory = posixpath.dirname(path) or "/"
        name = posixpath.basename(path)

        def _run() -> None:
            try:
                container = self._client.containers.get(handle.container_id)
                container.exec_run(["mkdir", "-p", directory])
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    info = tarfile.TarInfo(name=name)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                if not container.put_archive(directory, buf.getvalue()):
                    raise SandboxError(f"put_archive rejected write to {path}")
            except docker.errors.DockerException as exc:
                raise SandboxError(f"fs_write failed: {exc}") from exc

        await asyncio.to_thread(_run)

    async def fs_read(self, handle: SandboxHandle, path: str) -> bytes:
        def _run() -> bytes:
            try:
                container = self._client.containers.get(handle.container_id)
                code, output = container.exec_run(["cat", path], demux=True)
            except docker.errors.DockerException as exc:
                raise SandboxError(f"fs_read failed: {exc}") from exc
            if int(code) != 0:
                raise SandboxError(f"fs_read: {path} not readable (exit {code})")
            stdout, _stderr = output
            return stdout or b""

        return await asyncio.to_thread(_run)

    async def put_archive(self, handle: SandboxHandle, path: str, data: bytes) -> None:
        def _run() -> None:
            try:
                container = self._client.containers.get(handle.container_id)
                try:
                    accepted = container.put_archive(path, data)
                except NotFound:
                    # The container exists (`get` above found it), so it is the
                    # target directory that is missing. Creating it needs a
                    # running process, same as `fs_write`.
                    container.reload()
                    if container.status != "running":
                        raise SandboxError(f"put_archive: {path} does not exist") from None
                    code, _ = container.exec_run(["mkdir", "-p", path])
                    if int(code) != 0:
                        raise SandboxError(f"put_archive: cannot create {path}") from None
                    accepted = container.put_archive(path, data)
                if not accepted:
                    raise SandboxError(f"put_archive rejected the archive for {path}")
            except docker.errors.DockerException as exc:
                raise SandboxError(f"put_archive failed: {exc}") from exc

        await asyncio.to_thread(_run)

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        cap = _archive_cap(max_bytes)

        def _run() -> bytes:
            try:
                container = self._client.containers.get(handle.container_id)
                chunks, info = container.get_archive(path)
                spool = _spool_capped(chunks, cap)
            except NotFound as exc:
                raise SandboxError(f"get_archive: {path} does not exist") from exc
            except docker.errors.DockerException as exc:
                raise SandboxError(f"get_archive failed: {exc}") from exc
            with spool:
                # docker-py decodes the X-Docker-Container-Path-Stat header
                # into ``info``; its mode is Go's FileMode. The daemon reads
                # with more privilege than the sandbox user, so a link at the
                # requested path is refused here, never archived or followed.
                mode = int(info.get("mode", 0))
                if mode & _GO_MODE_SYMLINK or info.get("linkTarget"):
                    raise SandboxError(f"get_archive: {path} is a symlink")
                # Docker reports Go's FileMode, whose directory bit is 1 << 31.
                if mode & _GO_MODE_DIR:
                    try:
                        return _relative_to_directory(spool, str(info.get("name", "")))
                    except tarfile.TarError as exc:
                        raise SandboxError(f"get_archive: unreadable archive: {exc}") from exc
                return spool.read()

        return await asyncio.to_thread(_run)

    async def reap_orphans(self) -> int:
        """The fixed, no-argument orphan sweep the runtime-provisioner API
        exposes at ``POST /v1/reap-orphans`` (see
        ``oc8.runtime_provisioner.app``): remove containers whose run has
        finished. Deliberately just the count, not the container names --
        the provisioner boundary never lets a Docker identifier cross it.
        `oc8.sandbox.reaper.reap_orphaned_containers` is the richer,
        name-returning function the worker's own startup sweep calls
        in-process; this wraps it so `ProvisionerSandboxDriver.reap_orphans`
        has the same fixed operation to call over HTTP as it does today
        in-process.
        """
        # Deferred import: oc8.sandbox.reaper imports NAMESPACE_LABEL from
        # this module, so a module-level import here would be circular.
        from oc8.sandbox.reaper import reap_orphaned_containers

        removed = await reap_orphaned_containers(client=self._client)
        return len(removed)
