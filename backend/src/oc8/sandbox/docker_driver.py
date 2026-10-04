"""Docker-backed SandboxDriver (v1) — talks to the host daemon via docker-py.

All blocking docker-py calls run in a worker thread (asyncio.to_thread) so the
event loop is never blocked. Containers get ONLY spec.env — never the host
environment — so oc8/provider secrets cannot leak into a sandbox."""

from __future__ import annotations

import asyncio
import io
import posixpath
import tarfile

import docker
from docker.errors import NotFound
from docker.models.containers import Container

from oc8.config import get_settings
from oc8.sandbox.types import ExecResult, SandboxError, SandboxHandle, SandboxSpec

#: Every container/label filter that acts on "ours" must match on both of
#: these -- see oc8.sandbox.reaper. NAMESPACE_LABEL is deliberately NOT part
#: of the Docker-side list filter reap uses (a container built before this
#: label existed would then be invisible forever); it is compared in Python
#: instead, with a missing label read as the default namespace.
NAMESPACE_LABEL = "oc8.namespace"


def _labels(extra: dict[str, str]) -> dict[str, str]:
    # Ours-ness labels last, so a caller-supplied `spec.labels` entry of the
    # same key can never spoof which sandbox/namespace a container claims to
    # belong to.
    return {**extra, "oc8.sandbox": "1", NAMESPACE_LABEL: get_settings().deployment_namespace}


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
        container.remove(force=True)
    except docker.errors.DockerException:
        pass


class DockerSandboxDriver:
    def __init__(self, client: docker.DockerClient | None = None) -> None:  # type: ignore[name-defined]
        self._client = client or docker.from_env()  # type: ignore[attr-defined]

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        def _run() -> SandboxHandle:
            run_kwargs: dict[str, object] = dict(
                command=spec.command,
                detach=True,
                environment=dict(spec.env),
                working_dir=spec.workdir,
                labels=_labels(spec.labels),
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
                container = self._client.containers.run(spec.image, **run_kwargs)
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
                self._client.containers.get(handle.container_id).remove(force=True)
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
