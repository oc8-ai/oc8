"""The sandbox contract, run against every driver that implements it.

Two drivers, one set of expectations:

- ``docker``: ``DockerSandboxDriver`` against the local daemon, as the other
  driver tests in this folder do;
- ``provisioner``: ``ProvisionerSandboxDriver`` talking HTTP to the real
  runtime-provisioner app, which in turn drives the local daemon. That proves
  the broker forwards seed, scratch and archives, not just that its routes
  exist.
"""

from __future__ import annotations

import io
import tarfile
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from oc8.runtime_provisioner.app import create_provisioner_app
from oc8.runtime_provisioner.policy import ProvisionerPolicy
from oc8.sandbox.docker_driver import DockerSandboxDriver
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.provisioner_driver import ProvisionerSandboxDriver
from oc8.sandbox.types import SandboxError, SandboxSpec, ScratchDir

pytestmark = pytest.mark.asyncio

_IMAGE = "alpine:latest"


def tar_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def files_of(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            if member.isfile():
                extracted = tar.extractfile(member)
                assert extracted is not None
                out[member.name] = extracted.read()
    return out


@pytest.fixture(params=["docker", "provisioner"])
async def driver(request: pytest.FixtureRequest) -> AsyncIterator[SandboxDriver]:
    docker_driver = DockerSandboxDriver()
    if request.param == "docker":
        yield docker_driver
        return
    app = create_provisioner_app(
        token="contract-token",
        driver=docker_driver,
        policy=ProvisionerPolicy(
            allowed_images={_IMAGE},
            agent_network="oc8_agents",
            session_root="/var/lib/oc8/sessions",
            platform_mounts={},
        ),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://provisioner", timeout=60.0
    )
    remote = ProvisionerSandboxDriver(
        base_url="http://provisioner", token="contract-token", client=client
    )
    yield remote
    await remote.aclose()


async def test_an_unchanged_spec_still_provisions_and_runs(driver: SandboxDriver) -> None:
    handle = await driver.provision(SandboxSpec(image=_IMAGE))
    try:
        result = await driver.exec(handle, ["echo", "hello"])
        assert result.exit_code == 0
        assert result.output.strip() == "hello"
    finally:
        await driver.teardown(handle)


async def test_the_seed_is_in_place_before_the_command_starts(driver: SandboxDriver) -> None:
    """The command's FIRST instruction reads the seeded file. A driver that
    starts the container before unpacking the seed fails this every time, not
    one run in ten: `cat` of a missing file exits 1 at once."""
    seed = tar_of({"etc/oc8/config.toml": b"token = 'abc'\n"})
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, command=["cat", "/etc/oc8/config.toml"], seed=seed)
    )
    try:
        assert await driver.wait(handle, timeout_s=30) == 0
        assert "token = 'abc'" in await driver.logs(handle)
    finally:
        await driver.teardown(handle)


async def test_put_archive_then_get_archive_round_trips(driver: SandboxDriver) -> None:
    handle = await driver.provision(SandboxSpec(image=_IMAGE, command=["sleep", "60"]))
    try:
        payload = tar_of({"a.txt": b"alpha", "sub/b.bin": bytes(range(256)) * 64})
        await driver.put_archive(handle, "/workspace/output", payload)
        got = files_of(await driver.get_archive(handle, "/workspace/output"))
        # Entries are named from inside the directory, so the archive can go
        # straight back with put_archive(handle, path, ...).
        assert got == {"a.txt": b"alpha", "sub/b.bin": bytes(range(256)) * 64}
    finally:
        await driver.teardown(handle)


async def test_get_archive_of_a_missing_path_raises(driver: SandboxDriver) -> None:
    handle = await driver.provision(SandboxSpec(image=_IMAGE, command=["sleep", "60"]))
    try:
        with pytest.raises(SandboxError):
            await driver.get_archive(handle, "/no/such/dir")
    finally:
        await driver.teardown(handle)


async def test_a_scratch_dir_is_writable_by_a_non_root_user(driver: SandboxDriver) -> None:
    """Kubernetes' emptyDir is world-writable, so a runtime image running as
    its own uid can write it. The Docker driver must give the same promise."""
    handle = await driver.provision(
        SandboxSpec(
            image=_IMAGE,
            command=["sleep", "60"],
            user="1000:1000",
            scratch=(ScratchDir(path="/scratch/home", size_mb=64),),
        )
    )
    try:
        result = await driver.exec(
            handle, ["sh", "-c", "echo hi > /scratch/home/f && cat /scratch/home/f"]
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "hi"
    finally:
        await driver.teardown(handle)


async def test_a_seed_into_a_scratch_dir_survives_the_start(driver: SandboxDriver) -> None:
    """The resume transcript is seeded INTO the CLI home, which is a scratch
    dir. A driver that mounts the scratch over the seeded files would hide
    them."""
    seed = tar_of({"home/agent/.cli/session.json": b'{"turns": 3}'})
    handle = await driver.provision(
        SandboxSpec(
            image=_IMAGE,
            command=["cat", "/home/agent/.cli/session.json"],
            scratch=(ScratchDir(path="/home/agent", size_mb=64),),
            seed=seed,
        )
    )
    try:
        assert await driver.wait(handle, timeout_s=30) == 0
        assert '{"turns": 3}' in await driver.logs(handle)
    finally:
        await driver.teardown(handle)


async def test_a_many_megabyte_archive_round_trips(driver: SandboxDriver) -> None:
    """Far past the 96 KiB the base64 `fs_write` path is sized for, and past
    any argv limit: archives travel as bodies, never as command arguments."""
    import random

    blob = random.Random(8).randbytes(24 * 1024 * 1024)
    handle = await driver.provision(SandboxSpec(image=_IMAGE, command=["sleep", "120"]))
    try:
        await driver.put_archive(handle, "/workspace", tar_of({"big.bin": blob}))
        size = await driver.exec(handle, ["sh", "-c", "wc -c < /workspace/big.bin"])
        assert size.output.strip() == str(len(blob))
        assert files_of(await driver.get_archive(handle, "/workspace/big.bin")) == {"big.bin": blob}
    finally:
        await driver.teardown(handle)


async def test_a_hardlink_pair_survives_a_round_trip_into_a_new_directory(
    driver: SandboxDriver,
) -> None:
    """A hardlink entry names its target by archive path. That path has to be
    renamed along with the entries, or the link points at nothing."""
    handle = await driver.provision(SandboxSpec(image=_IMAGE, command=["sleep", "60"]))
    try:
        made = await driver.exec(
            handle,
            ["sh", "-c", "mkdir -p /src && echo same > /src/a && ln /src/a /src/b"],
        )
        assert made.exit_code == 0, made.output
        await driver.put_archive(handle, "/dst", await driver.get_archive(handle, "/src"))
        check = await driver.exec(
            handle,
            [
                "sh",
                "-c",
                'cat /dst/a /dst/b && [ "$(stat -c %i /dst/a)" = "$(stat -c %i /dst/b)" ]',
            ],
        )
        assert check.exit_code == 0, check.output
        assert check.output.split() == ["same", "same"]
    finally:
        await driver.teardown(handle)


# ------------------------------------------------ after the command exits


async def test_after_the_command_exits_its_files_stay_readable_and_wait_has_its_code(
    driver: SandboxDriver,
) -> None:
    """The sandbox outlives its command: the last output pass and the CLI
    home save read it AFTER the command has exited, through exec. `wait`
    still reports the command's own exit code."""
    handle = await driver.provision(
        SandboxSpec(
            image=_IMAGE,
            user="1000:1000",
            scratch=(ScratchDir("/workspace", 16),),
            command=[
                "sh",
                "-c",
                "mkdir -p /workspace/output && echo done > /workspace/output/a.txt; exit 3",
            ],
        )
    )
    try:
        assert await driver.wait(handle, timeout_s=30) == 3
        assert files_of(await driver.get_archive(handle, "/workspace/output")) == {
            "a.txt": b"done\n"
        }
        res = await driver.exec(handle, ["cat", "/workspace/output/a.txt"])
        assert (res.exit_code, res.output) == (0, "done\n")
        # Asking again gives the same answer.
        assert await driver.wait(handle, timeout_s=5) == 3
    finally:
        await driver.teardown(handle)


async def test_wait_still_raises_when_the_command_outlives_the_timeout(
    driver: SandboxDriver,
) -> None:
    handle = await driver.provision(SandboxSpec(image=_IMAGE, command=["sleep", "60"]))
    try:
        with pytest.raises(Exception):  # noqa: B017 -- any raise; -1 would be "no verdict"
            await driver.wait(handle, timeout_s=1)
    finally:
        await driver.teardown(handle)


async def test_a_sandbox_whose_command_exited_is_still_torn_down(driver: SandboxDriver) -> None:
    import docker

    marker = f"torn-down-{uuid.uuid4().hex[:8]}"
    handle = await driver.provision(
        SandboxSpec(image=_IMAGE, command=["true"], labels={"oc8.test": marker})
    )
    assert await driver.wait(handle, timeout_s=30) == 0
    await driver.teardown(handle)
    client = docker.from_env()  # type: ignore[attr-defined]
    assert client.containers.list(all=True, filters={"label": f"oc8.test={marker}"}) == []


async def test_get_archive_reads_as_the_sandbox_user_never_with_more(
    driver: SandboxDriver,
) -> None:
    """The read runs as the spec's user, so it cannot reach what that user
    cannot: /etc/shadow, directly or through a link the sandbox planted."""
    handle = await driver.provision(
        SandboxSpec(
            image=_IMAGE,
            user="1000:1000",
            scratch=(ScratchDir("/workspace", 16),),
            command=["sleep", "60"],
        )
    )
    try:
        res = await driver.exec(
            handle,
            [
                "sh",
                "-c",
                "mkdir -p /workspace/output && cd /workspace/output && "
                "echo ok > ok.txt && ln -s /etc/shadow shadow.txt && ln -s /etc sub",
            ],
        )
        assert res.exit_code == 0, res.output
        with pytest.raises(SandboxError):
            await driver.get_archive(handle, "/etc/shadow")
        with pytest.raises(SandboxError):
            await driver.get_archive(handle, "/workspace/output/shadow.txt")
        with pytest.raises(SandboxError):
            await driver.get_archive(handle, "/workspace/output/sub/shadow")
        # The directory itself: links come back as links, never as content.
        data = await driver.get_archive(handle, "/workspace/output")
        assert files_of(data) == {"ok.txt": b"ok\n"}
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
            links = {m.name: m.linkname for m in tar.getmembers() if m.issym()}
        assert links == {"shadow.txt": "/etc/shadow", "sub": "/etc"}
    finally:
        await driver.teardown(handle)
