from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import docker
import pytest

from oc8.sandbox.docker_driver import DockerSandboxDriver
from oc8.sandbox.naming import container_name
from oc8.sandbox.types import SandboxError, SandboxSpec

pytestmark = pytest.mark.asyncio

_IMAGE = "alpine:latest"


async def test_provision_then_teardown() -> None:
    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    try:
        c = client.containers.get(handle.container_id)
        assert c.status in {"created", "running"}
        assert c.labels.get("oc8.sandbox") == "1"
        # The default namespace, so a lone deployment's reaper still finds it.
        assert c.labels.get("oc8.namespace") == "default"
    finally:
        await driver.teardown(handle)
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(handle.container_id)


async def test_a_spec_label_cannot_spoof_the_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The namespace is a trust boundary between deployments sharing a host,
    not a caller-supplied hint -- a spec label of the same key must lose."""
    from oc8.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("OC8_DEPLOYMENT_NAMESPACE", "staging")
    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, labels={"oc8.namespace": "attacker-controlled"})
    )
    try:
        c = client.containers.get(handle.container_id)
        assert c.labels.get("oc8.namespace") == "staging"
    finally:
        await driver.teardown(handle)
        get_settings.cache_clear()


async def test_teardown_is_idempotent() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    await driver.teardown(handle)
    # Second teardown of an already-removed container must not raise.
    await driver.teardown(handle)


async def test_provision_applies_security_hardening() -> None:
    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    try:
        # Inspect via an independent client so we verify what Docker actually
        # applied, not just what we asked for.
        container = client.containers.get(handle.container_id)
        host_config = container.attrs["HostConfig"]
        assert host_config["Memory"] == 1073741824  # 1g
        assert host_config["PidsLimit"] == 512
        assert host_config["NanoCpus"] == 2_000_000_000
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host_config["SecurityOpt"]
        assert host_config["Privileged"] is False
        assert container.attrs["Config"]["NetworkDisabled"] is True
    finally:
        await driver.teardown(handle)


async def test_exec_returns_output_and_code() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    try:
        ok = await driver.exec(handle, ["echo", "hello"])
        assert ok.exit_code == 0
        assert ok.output.strip() == "hello"
        bad = await driver.exec(handle, ["sh", "-c", "exit 3"])
        assert bad.exit_code == 3
    finally:
        await driver.teardown(handle)


async def test_exec_timeout_kills_hung_command() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    try:
        start = time.monotonic()
        hung = await driver.exec(handle, ["sleep", "30"], timeout=1)
        elapsed = time.monotonic() - start
        assert elapsed < 10
        assert hung.exit_code != 0

        ok = await driver.exec(handle, ["echo", "ok"], timeout=5)
        assert ok.exit_code == 0
        assert ok.output.strip() == "ok"
    finally:
        await driver.teardown(handle)


async def test_fs_write_then_read_roundtrip() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    try:
        payload = b"line1\nline2\n"
        await driver.fs_write(handle, "/workspace/hello.txt", payload)
        got = await driver.fs_read(handle, "/workspace/hello.txt")
        assert got == payload
    finally:
        await driver.teardown(handle)


async def test_fs_read_missing_file_raises() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    try:
        with pytest.raises(SandboxError):
            await driver.fs_read(handle, "/workspace/nope.txt")
    finally:
        await driver.teardown(handle)


async def test_a_spec_can_pin_the_container_user() -> None:
    """Bind mounts carry HOST ownership: a container writing a session folder the
    control plane created has to be the same uid, or it cannot read its own config
    on any host whose bind mounts are not ownership-virtualised (i.e. Linux)."""
    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, command=["sleep", "30"], user="1000:1000")
    )
    try:
        result = await driver.exec(handle, ["id", "-u"])
        assert result.output.strip() == "1000"
    finally:
        await driver.teardown(handle)


async def test_a_bind_mount_is_visible_in_the_container(tmp_path: Path) -> None:
    """Not just accepted -- actually mounted. A silently ignored volume would
    look identical from the API's side."""
    from oc8.sandbox.types import BindMount

    (tmp_path / "hello.txt").write_text("from the host")
    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(
            image=_IMAGE,
            command=["sleep", "30"],
            mounts=[BindMount(host_path=str(tmp_path), container_path="/mnt/in", readonly=True)],
        )
    )
    try:
        result = await driver.exec(handle, ["cat", "/mnt/in/hello.txt"])
        assert "from the host" in result.output
    finally:
        await driver.teardown(handle)


async def test_a_named_spec_reaches_docker_as_that_name() -> None:
    """Asked through an independent client, so this proves what Docker applied.

    Without it `docker ps` shows Docker's own invention (`tender_lamarr`) and an
    operator has to inspect each container's mounts to tell whose it is.
    """
    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    name = container_name("Nora", "agent", uuid.uuid4())
    handle = await driver.provision(SandboxSpec(image=_IMAGE, name=name))
    try:
        container = client.containers.get(handle.container_id)
        assert container.name == name
    finally:
        await driver.teardown(handle)


async def test_a_lifetime_becomes_a_deadline_label_the_reaper_reads() -> None:
    from oc8.sandbox.reaper import EXPIRES_LABEL

    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    before = int(time.time())
    handle = await driver.provision(SandboxSpec(image=_IMAGE, max_lifetime_s=600))
    try:
        deadline = int(client.containers.get(handle.container_id).labels[EXPIRES_LABEL])
        assert before + 600 <= deadline <= int(time.time()) + 600
    finally:
        await driver.teardown(handle)


async def test_no_lifetime_means_no_deadline_even_if_a_label_claims_one() -> None:
    from oc8.sandbox.reaper import EXPIRES_LABEL

    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    handle = await driver.provision(SandboxSpec(image=_IMAGE, labels={EXPIRES_LABEL: "1"}))
    try:
        assert EXPIRES_LABEL not in client.containers.get(handle.container_id).labels
    finally:
        await driver.teardown(handle)


async def test_teardown_removes_the_scratch_volume() -> None:
    """Scratch is an anonymous volume. Removing the container without its
    volumes would leave one behind on the host for every run."""
    from oc8.sandbox.types import ScratchDir

    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, scratch=(ScratchDir(path="/scratch", size_mb=16),))
    )
    try:
        mounts = client.containers.get(handle.container_id).attrs["Mounts"]
        volumes = [m["Name"] for m in mounts if m["Destination"] == "/scratch"]
        assert len(volumes) == 1
    finally:
        await driver.teardown(handle)
    with pytest.raises(docker.errors.NotFound):
        client.volumes.get(volumes[0])


async def test_restricted_egress_leaves_the_docker_network_as_it_is() -> None:
    """Docker has no FQDN allowlist. `egress` is accepted and ignored; the
    network the spec asks for is the one the container gets."""
    driver = DockerSandboxDriver()
    client = docker.from_env()  # type: ignore[attr-defined]
    handle = await driver.provision(SandboxSpec(image=_IMAGE, egress="restricted"))
    try:
        assert client.containers.get(handle.container_id).attrs["Config"]["NetworkDisabled"] is True
    finally:
        await driver.teardown(handle)


# ------------------------------------------------- archives: bounds and names

import io  # noqa: E402
import tarfile  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from typing import Any  # noqa: E402

from oc8.sandbox.types import ArchiveTooLarge, SandboxHandle, ScratchDir  # noqa: E402


def _tar_bytes(files: dict[str, bytes], *, links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.LNKTYPE
            info.linkname = target
            tar.addfile(info)
    return buf.getvalue()


class _StopReading(AssertionError):
    pass


def _chunks_failing_past(data: bytes, *, size: int, allowed: int) -> Iterator[bytes]:
    """Docker's archive stream, in `size` chunks; reading past the first chunk
    that crosses `allowed` bytes fails the test."""
    sent = 0
    for start in range(0, len(data), size):
        if sent > allowed:
            raise _StopReading(f"read on after {sent} bytes with a cap of {allowed}")
        chunk = data[start : start + size]
        sent += len(chunk)
        yield chunk


class _ExecApi:
    """docker-py's low-level exec API, streaming ``chunks`` as tar's stdout."""

    def __init__(self, chunks: Iterator[bytes], *, exit_code: int = 0) -> None:
        self._chunks = chunks
        self._exit_code = exit_code
        self.created: list[dict[str, Any]] = []

    def exec_create(self, container: str, cmd: list[str], **kw: Any) -> dict[str, str]:
        self.created.append({"cmd": cmd, **kw})
        return {"Id": "e1"}

    def exec_start(self, exec_id: str, **kw: Any) -> Iterator[tuple[bytes | None, bytes | None]]:
        return ((chunk, None) for chunk in self._chunks)

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"ExitCode": self._exit_code}


class _ArchiveContainer:
    id = "c"
    attrs: dict[str, Any] = {"Config": {"User": "1000:1000"}}


class _ArchiveClient:
    def __init__(self, api: _ExecApi) -> None:
        self.api = api

        class _Containers:
            def get(self, _id: str) -> _ArchiveContainer:
                return _ArchiveContainer()

        self.containers = _Containers()


async def test_get_archive_stops_reading_at_the_cap() -> None:
    data = _tar_bytes({"./big.bin": b"z" * 64 * 1024})
    chunks = _chunks_failing_past(data, size=1024, allowed=8 * 1024)
    driver = DockerSandboxDriver(client=_ArchiveClient(_ExecApi(chunks)))
    with pytest.raises(ArchiveTooLarge):
        await driver.get_archive(SandboxHandle("c", _IMAGE), "/x", max_bytes=8 * 1024)


async def test_get_archive_runs_tar_through_exec_as_the_configured_user() -> None:
    """The read runs as the user the container was CONFIGURED with, never
    root and never a uid the sandbox reports, and never follows links."""
    api = _ExecApi(iter([_tar_bytes({"./a.txt": b"a"})]))
    driver = DockerSandboxDriver(client=_ArchiveClient(api))
    data = await driver.get_archive(SandboxHandle("c", _IMAGE), "/workspace/output")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        assert tar.getnames() == ["a.txt"]
    (created,) = api.created
    assert created["user"] == "1000:1000"
    script = " ".join(created["cmd"])
    assert "tar -c" in script and " -h" not in script and "--dereference" not in script
    assert created["cmd"][-1] == "/workspace/output"


async def test_a_failed_tar_is_an_error() -> None:
    api = _ExecApi(iter([b""]), exit_code=41)
    driver = DockerSandboxDriver(client=_ArchiveClient(api))
    with pytest.raises(SandboxError, match="symlink"):
        await driver.get_archive(SandboxHandle("c", _IMAGE), "/x")


async def test_get_archive_cap_defaults_to_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("OC8_SANDBOX_ARCHIVE_MAX_BYTES", "4096")
    try:
        data = _tar_bytes({"./big.bin": b"z" * 64 * 1024})
        chunks = _chunks_failing_past(data, size=1024, allowed=4096)
        driver = DockerSandboxDriver(client=_ArchiveClient(_ExecApi(chunks)))
        with pytest.raises(ArchiveTooLarge):
            await driver.get_archive(SandboxHandle("c", _IMAGE), "/x")
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "files,links",
    [
        ({"./../../etc/evil": b"x"}, None),
        ({"/etc/evil": b"x"}, None),
        ({"./a": b"x"}, {"./b": "../../etc/passwd"}),
    ],
    ids=["dotdot", "absolute", "hardlink-out"],
)
async def test_get_archive_rejects_names_outside_the_directory(
    files: dict[str, bytes], links: dict[str, str] | None
) -> None:
    data = _tar_bytes(files, links=links)
    driver = DockerSandboxDriver(client=_ArchiveClient(_ExecApi(iter([data]))))
    with pytest.raises(SandboxError, match="outside"):
        await driver.get_archive(SandboxHandle("c", _IMAGE), "/output", max_bytes=1 << 20)


# ------------------------------------------- failed provisions leave nothing


def _leftovers(run: str) -> tuple[list[Any], list[Any]]:
    client = docker.from_env()  # type: ignore[attr-defined]
    label = {"label": f"oc8.run={run}"}
    return client.containers.list(all=True, filters=label), client.volumes.list(filters=label)


async def test_a_refused_seed_leaves_no_container_and_no_volume() -> None:
    run = str(uuid.uuid4())
    driver = DockerSandboxDriver()
    with pytest.raises(SandboxError):
        await driver.provision(
            SandboxSpec(
                image=_IMAGE,
                labels={"oc8.run": run},
                scratch=(ScratchDir("/scratch", 16),),
                seed=b"this is not a tar archive" * 100,
            )
        )
    assert _leftovers(run) == ([], [])


async def test_a_failed_start_leaves_no_container_and_no_volume() -> None:
    run = str(uuid.uuid4())
    driver = DockerSandboxDriver()
    with pytest.raises(SandboxError):
        await driver.provision(
            SandboxSpec(
                image=_IMAGE,
                # An unknown user fails the start itself. (A missing binary no
                # longer does: the lingering wrapper starts, and `wait`
                # reports the command's 127 -- see the test below.)
                user="no-such-user",
                labels={"oc8.run": run},
                scratch=(ScratchDir("/scratch", 16),),
            )
        )
    assert _leftovers(run) == ([], [])


async def test_a_missing_binary_is_the_commands_exit_127() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_IMAGE, command=["/no/such/binary"]))
    try:
        assert await driver.wait(handle, timeout_s=30) == 127
    finally:
        await driver.teardown(handle)


async def test_scratch_volumes_carry_the_sandbox_labels() -> None:
    """How the two tests above can see a leaked volume at all, and how an
    operator finds whose volume is whose."""
    run = str(uuid.uuid4())
    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, labels={"oc8.run": run}, scratch=(ScratchDir("/s", 16),))
    )
    try:
        _containers, volumes = _leftovers(run)
        assert len(volumes) == 1
        assert volumes[0].attrs["Labels"]["oc8.sandbox"] == "1"
    finally:
        await driver.teardown(handle)
    assert _leftovers(run) == ([], [])


# ------------------------------------------------------ spec combinations


@pytest.mark.parametrize(
    "extra",
    [{"seed": _tar_bytes({"a": b"x"})}, {"scratch": (ScratchDir("/s", 16),)}],
    ids=["seed", "scratch"],
)
async def test_read_only_cannot_be_combined_with_seed_or_scratch(extra: dict[str, Any]) -> None:
    driver = DockerSandboxDriver()
    with pytest.raises(SandboxError, match="read_only"):
        await driver.provision(SandboxSpec(image=_IMAGE, read_only=True, **extra))


@pytest.mark.parametrize("path", ["/etc", "/etc/ssl", "/usr/bin", "/proc", "/lib64", "/dev/x"])
async def test_a_scratch_dir_over_a_system_path_is_refused(path: str) -> None:
    driver = DockerSandboxDriver()
    with pytest.raises(SandboxError, match="scratch"):
        await driver.provision(SandboxSpec(image=_IMAGE, scratch=(ScratchDir(path, 16),)))


async def test_a_scratch_dir_is_sticky_and_world_writable() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, command=["sleep", "30"], scratch=(ScratchDir("/s", 16),))
    )
    try:
        result = await driver.exec(handle, ["stat", "-c", "%a", "/s"])
        assert result.output.strip() == "1777"
    finally:
        await driver.teardown(handle)


async def test_get_archive_refuses_a_symlink_path() -> None:
    """The archive API runs as the daemon, not the sandbox user: a link at the
    requested path is refused rather than archived or followed."""
    driver = DockerSandboxDriver()
    handle = await driver.provision(SandboxSpec(image=_IMAGE, command=["sleep", "30"]))
    try:
        res = await driver.exec(handle, ["sh", "-c", "ln -s /etc/shadow /tmp/link"])
        assert res.exit_code == 0
        with pytest.raises(SandboxError, match="symlink"):
            await driver.get_archive(handle, "/tmp/link")
    finally:
        await driver.teardown(handle)


async def test_stopping_the_command_keeps_the_sandbox_readable() -> None:
    """`STOP_COMMAND` (what a parked CLI leg sends) ends the command, not the
    sandbox: `wait` reports the command's 143 and files stay readable."""
    from oc8.sandbox.linger import STOP_COMMAND

    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(
            image=_IMAGE,
            user="1000:1000",
            scratch=(ScratchDir("/w", 16),),
            command=["sh", "-c", "echo kept > /w/a.txt; exec sleep 300"],
        )
    )
    try:
        await driver.exec(handle, ["sh", "-c", "until [ -f /w/a.txt ]; do sleep 0.1; done"])
        await driver.exec(handle, STOP_COMMAND)
        assert await driver.wait(handle, timeout_s=10) == 143
        res = await driver.exec(handle, ["cat", "/w/a.txt"])
        assert (res.exit_code, res.output) == (0, "kept\n")
    finally:
        await driver.teardown(handle)


class _InitDriver(DockerSandboxDriver):
    """Runs every container with Docker's init (tini) as PID 1, as the CLI
    images do through their ENTRYPOINT."""

    def _create(self, spec: SandboxSpec, run_kwargs: dict[str, object]) -> Any:
        return super()._create(spec, {**run_kwargs, "init": True})

    def _start(self, spec: SandboxSpec, run_kwargs: dict[str, object]) -> Any:
        return super()._start(spec, {**run_kwargs, "init": True})


async def test_a_stop_after_the_command_exited_leaves_the_sandbox_up_under_tini() -> None:
    """A parked leg whose CLI had already exited still sends the stop. The
    wrapper is then `sleep`, which must ignore it, or tini exits and the
    sandbox can no longer be read."""
    from oc8.sandbox.linger import STOP_COMMAND

    driver = _InitDriver()
    handle = await driver.provision(
        SandboxSpec(
            image=_IMAGE,
            user="1000:1000",
            scratch=(ScratchDir("/w", 16),),
            command=["sh", "-c", "echo kept > /w/a.txt"],
        )
    )
    try:
        assert await driver.wait(handle, timeout_s=30) == 0
        await driver.exec(handle, STOP_COMMAND)
        await asyncio.sleep(1)
        container = docker.from_env().containers.get(handle.container_id)  # type: ignore[attr-defined]
        assert container.status == "running"
        assert files_of_tar(await driver.get_archive(handle, "/w")) == {"a.txt": b"kept\n"}
        assert await driver.wait(handle, timeout_s=5) == 0
    finally:
        await driver.teardown(handle)


async def test_a_fifo_at_the_exit_marker_cannot_wedge_wait() -> None:
    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, command=["sh", "-c", "mkfifo /.oc8/exit; exec sleep 120"])
    )
    try:
        await driver.exec(handle, ["sh", "-c", "until [ -p /.oc8/exit ]; do sleep 0.1; done"])
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await driver.wait(handle, timeout_s=3)
        assert time.monotonic() - started < 3 + 6
    finally:
        await driver.teardown(handle)


def files_of_tar(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                f = tar.extractfile(member)
                assert f is not None
                out[member.name] = f.read()
    return out


class _HungContainer:
    """A container whose every daemon call blocks, as an exec into a dying
    sandbox can on some daemons."""

    id = "c"
    attrs: dict[str, Any] = {"Config": {"User": ""}}

    def exec_run(self, *a: Any, **kw: Any) -> Any:
        time.sleep(5)


class _HungApi:
    def exec_create(self, *a: Any, **kw: Any) -> dict[str, str]:
        time.sleep(5)
        return {"Id": "e"}


class _HungClient:
    api = _HungApi()

    class containers:
        @staticmethod
        def get(_id: str) -> _HungContainer:
            return _HungContainer()


async def test_a_hung_get_archive_is_abandoned_on_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_archive_timeout_s", 0.5)
    driver = DockerSandboxDriver(client=_HungClient())
    started = time.monotonic()
    with pytest.raises(SandboxError, match="did not return"):
        await driver.get_archive(SandboxHandle("c", _IMAGE), "/workspace/output")
    assert time.monotonic() - started < 2


async def test_an_untimed_exec_still_has_a_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8.config import get_settings

    monkeypatch.setattr(get_settings(), "sandbox_exec_default_timeout_s", 0.5)
    driver = DockerSandboxDriver(client=_HungClient())
    started = time.monotonic()
    with pytest.raises(SandboxError, match="did not return"):
        await driver.exec(SandboxHandle("c", _IMAGE), ["true"])
    assert time.monotonic() - started < 2


async def test_a_scratch_dir_starts_empty_over_an_image_directory() -> None:
    """Like an emptyDir: the image's own files at that path are not copied
    into the scratch volume (alpine ships /media/cdrom, /media/usb, ...)."""
    driver = DockerSandboxDriver()
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, scratch=(ScratchDir("/media", 16),), command=["sleep", "30"])
    )
    try:
        res = await driver.exec(handle, ["ls", "-A", "/media"])
        assert (res.exit_code, res.output) == (0, "")
    finally:
        await driver.teardown(handle)
