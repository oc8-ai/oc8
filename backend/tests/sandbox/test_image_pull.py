from __future__ import annotations

import docker
import pytest

from oc8.sandbox.docker_driver import DockerSandboxDriver, registry_host_of
from oc8.sandbox.provisioner_models import SandboxSpecDTO
from oc8.sandbox.types import RegistryAuth, SandboxError, SandboxSpec


class _Images:
    def __init__(self) -> None:
        self.have: set[str] = set()
        self.pulls: list[tuple[str, str | None, dict[str, str] | None]] = []
        self.fail: Exception | None = None

    def get(self, name: str) -> object:
        if name not in self.have:
            raise docker.errors.ImageNotFound(name)
        return object()

    def pull(
        self,
        repository: str,
        tag: str | None = None,
        auth_config: dict[str, str] | None = None,
    ) -> None:
        self.pulls.append((repository, tag, auth_config))
        if self.fail is not None:
            raise self.fail
        self.have.add(f"{repository}:{tag}" if tag else repository)


class _Container:
    id = "container-1"
    status = "exited"

    def reload(self) -> None:
        return None


class _Containers:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, image: str, **kwargs: object) -> _Container:
        self.calls.append((image, kwargs))
        return _Container()


class _Client:
    def __init__(self) -> None:
        self.images = _Images()
        self.containers = _Containers()


def test_registry_host_ignores_the_tag_colon() -> None:
    assert registry_host_of("alpine:latest") == "docker.io"
    assert registry_host_of("ghcr.io/acme/runtime:1") == "ghcr.io"
    assert registry_host_of("registry.example:5000/acme/runtime:1") == "registry.example:5000"


@pytest.mark.asyncio
async def test_a_local_image_is_not_pulled_and_the_token_stays_out_of_the_container() -> None:
    client = _Client()
    client.images.have.add("ghcr.io/acme/runtime:1")
    driver = DockerSandboxDriver(client)  # type: ignore[arg-type]
    auth = RegistryAuth(username="acme", password="token-value", registry="ghcr.io")
    await driver.provision(
        SandboxSpec(image="ghcr.io/acme/runtime:1", env={"HOME": "/home/node"}, registry_auth=auth)
    )
    assert client.images.pulls == []
    _image, kwargs = client.containers.calls[0]
    assert kwargs["environment"] == {"HOME": "/home/node"}
    assert "token-value" not in str(kwargs["environment"])


@pytest.mark.asyncio
async def test_a_missing_private_image_is_pulled_with_the_registry_login() -> None:
    client = _Client()
    driver = DockerSandboxDriver(client)  # type: ignore[arg-type]
    auth = RegistryAuth(username="acme", password="token-value", registry="ghcr.io")
    await driver.provision(SandboxSpec(image="ghcr.io/acme/runtime:1", registry_auth=auth))
    assert client.images.pulls == [
        ("ghcr.io/acme/runtime", "1", {"username": "acme", "password": "token-value"})
    ]
    assert client.containers.calls[0][1]["environment"] == {}


@pytest.mark.asyncio
async def test_a_missing_private_image_without_a_login_fails_before_a_container_starts() -> None:
    client = _Client()
    client.images.fail = docker.errors.APIError("unauthorized: authentication required")
    driver = DockerSandboxDriver(client)  # type: ignore[arg-type]
    with pytest.raises(SandboxError, match="registry login"):
        await driver.provision(SandboxSpec(image="ghcr.io/acme/runtime:1"))
    assert client.containers.calls == []


@pytest.mark.asyncio
async def test_a_login_for_a_different_registry_is_not_sent() -> None:
    client = _Client()
    driver = DockerSandboxDriver(client)  # type: ignore[arg-type]
    auth = RegistryAuth(username="acme", password="token-value", registry="ghcr.io")
    with pytest.raises(SandboxError, match=r"registry\.example:5000"):
        await driver.provision(
            SandboxSpec(image="registry.example:5000/acme/runtime:1", registry_auth=auth)
        )
    assert client.images.pulls == []
    assert client.containers.calls == []


def test_registry_login_survives_the_provisioner_payload_and_not_the_container_env() -> None:
    spec = SandboxSpec(
        image="ghcr.io/acme/runtime:1",
        env={"HOME": "/tmp"},
        registry_auth=RegistryAuth(username="acme", password="token-value", registry="ghcr.io"),
    )
    restored = SandboxSpecDTO.from_domain(spec).to_domain()
    assert restored.registry_auth == spec.registry_auth
    assert restored.env == {"HOME": "/tmp"}
    assert "token-value" not in restored.env.values()
