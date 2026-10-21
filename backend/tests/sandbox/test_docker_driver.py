from __future__ import annotations

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
