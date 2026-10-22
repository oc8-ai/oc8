from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from oc8.runtime_provisioner.app import create_provisioner_app
from oc8.runtime_provisioner.policy import ProvisionerPolicy
from oc8.sandbox.types import ExecResult, SandboxHandle, SandboxSpec

pytestmark = pytest.mark.asyncio


class FakeDriver:
    def __init__(self) -> None:
        self.provisioned: list[SandboxSpec] = []
        self.removed: list[str] = []
        self.reap_calls = 0

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        self.provisioned.append(spec)
        return SandboxHandle("docker-id", spec.image)

    async def exec(
        self, handle: SandboxHandle, command: list[str], **_kwargs: object
    ) -> ExecResult:
        return ExecResult(0, "ok")

    async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        assert content == b"payload"

    async def fs_read(self, handle: SandboxHandle, path: str) -> bytes:
        return b"content"

    async def teardown(self, handle: SandboxHandle) -> None:
        self.removed.append(handle.container_id)

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        return 0

    async def logs(self, handle: SandboxHandle) -> str:
        return "logs"

    async def reap_orphans(self) -> int:
        self.reap_calls += 1
        return 2


def _app(driver: FakeDriver) -> FastAPI:
    return create_provisioner_app(
        token="provisioner-token",
        driver=driver,
        policy=ProvisionerPolicy(
            allowed_images={"oc8-runtime:1"},
            agent_network="oc8_agents",
            session_root="/var/lib/oc8/sessions",
            platform_mounts={},
        ),
    )


async def test_api_requires_bearer_token_and_returns_opaque_handle() -> None:
    driver = FakeDriver()
    transport = httpx.ASGITransport(app=_app(driver))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (
            await client.post("/v1/sandboxes", json={"image": "oc8-runtime:1"})
        ).status_code == 401
        response = await client.post(
            "/v1/sandboxes",
            headers={"Authorization": "Bearer provisioner-token"},
            json={"image": "oc8-runtime:1"},
        )

    assert response.status_code == 201
    assert response.json()["container_id"] != "docker-id"
    assert driver.provisioned[0].cap_drop == ["ALL"]


async def test_api_reaps_only_through_the_fixed_driver_operation() -> None:
    driver = FakeDriver()
    transport = httpx.ASGITransport(app=_app(driver))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/v1/reap-orphans")
        reaped = await client.post(
            "/v1/reap-orphans", headers={"Authorization": "Bearer provisioner-token"}
        )

    assert denied.status_code == 401
    assert reaped.json() == {"count": 2}
    assert driver.reap_calls == 1


async def test_api_rejects_unknown_fields_and_policy_violations() -> None:
    transport = httpx.ASGITransport(app=_app(FakeDriver()))
    headers = {"Authorization": "Bearer provisioner-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unknown_field = await client.post(
            "/v1/sandboxes",
            headers=headers,
            json={"image": "oc8-runtime:1", "privileged": True},
        )
        forbidden_image = await client.post(
            "/v1/sandboxes",
            headers=headers,
            json={"image": "attacker:latest"},
        )

    assert unknown_field.status_code == 422
    assert forbidden_image.status_code == 422


async def test_api_serves_only_the_typed_driver_endpoints() -> None:
    driver = FakeDriver()
    transport = httpx.ASGITransport(app=_app(driver))
    headers = {"Authorization": "Bearer provisioner-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/sandboxes", headers=headers, json={"image": "oc8-runtime:1"}
        )
        handle = created.json()["container_id"]
        assert (
            await client.post(
                f"/v1/sandboxes/{handle}/exec", headers=headers, json={"command": ["echo", "ok"]}
            )
        ).json() == {"exit_code": 0, "output": "ok"}
        assert (
            await client.post(
                f"/v1/sandboxes/{handle}/fs/write",
                headers=headers,
                json={"path": "/x", "content_base64": base64.b64encode(b"payload").decode()},
            )
        ).status_code == 204
        assert (
            await client.post(
                f"/v1/sandboxes/{handle}/fs/read", headers=headers, json={"path": "/x"}
            )
        ).json() == {"content_base64": base64.b64encode(b"content").decode()}
        assert (await client.delete(f"/v1/sandboxes/{handle}", headers=headers)).status_code == 204

    assert driver.removed == ["docker-id"]


async def test_api_rejects_invalid_base64_and_unknown_handles() -> None:
    transport = httpx.ASGITransport(app=_app(FakeDriver()))
    headers = {"Authorization": "Bearer provisioner-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/v1/sandboxes/not-a-handle/logs", headers=headers)
        created = await client.post(
            "/v1/sandboxes",
            headers=headers,
            json={"image": "oc8-runtime:1"},
        )
        invalid_base64 = await client.post(
            f"/v1/sandboxes/{created.json()['container_id']}/fs/write",
            headers=headers,
            json={"path": "/x", "content_base64": "not base64!"},
        )

    assert missing.status_code == 404
    assert invalid_base64.status_code == 422


class ArchiveDriver(FakeDriver):
    def __init__(self) -> None:
        super().__init__()
        self.archives: list[tuple[str, str, bytes]] = []

    async def put_archive(self, handle: SandboxHandle, path: str, data: bytes) -> None:
        self.archives.append((handle.container_id, path, data))

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        from oc8.sandbox.types import ArchiveTooLarge

        data = b"T" * 2048 if path == "/big" else b"OUTTAR"
        if max_bytes is not None and len(data) > max_bytes:
            raise ArchiveTooLarge("over the cap")
        return data


def _archive_app(driver: FakeDriver, *, archive_max_bytes: int = 1024) -> FastAPI:
    return create_provisioner_app(
        token="provisioner-token",
        driver=driver,
        archive_max_bytes=archive_max_bytes,
        policy=ProvisionerPolicy(
            allowed_images={"oc8-runtime:1"},
            agent_network="oc8_agents",
            session_root="/var/lib/oc8/sessions",
            platform_mounts={},
        ),
    )


_AUTH = {"Authorization": "Bearer provisioner-token"}


async def test_a_seeded_spec_reaches_the_driver_with_its_seed() -> None:
    driver = ArchiveDriver()
    transport = httpx.ASGITransport(app=_archive_app(driver))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/sandboxes/seeded",
            headers=_AUTH,
            data={"spec": '{"image": "oc8-runtime:1", "scratch": [{"path": "/w", "size_mb": 8}]}'},
            files={"seed": ("seed.tar", b"SEEDTAR", "application/x-tar")},
        )

    assert created.status_code == 201, created.text
    assert created.json()["container_id"] != "docker-id"
    spec = driver.provisioned[0]
    assert spec.seed == b"SEEDTAR"
    assert spec.scratch[0].path == "/w"
    assert spec.cap_drop == ["ALL"]  # the policy still ran


async def test_a_seeded_spec_is_checked_by_the_policy_and_the_size_cap() -> None:
    driver = ArchiveDriver()
    transport = httpx.ASGITransport(app=_archive_app(driver, archive_max_bytes=4))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        too_big = await client.post(
            "/v1/sandboxes/seeded",
            headers=_AUTH,
            data={"spec": '{"image": "oc8-runtime:1"}'},
            files={"seed": ("seed.tar", b"SEEDTAR", "application/x-tar")},
        )
        forbidden = await client.post(
            "/v1/sandboxes/seeded",
            headers=_AUTH,
            data={"spec": '{"image": "attacker:latest"}'},
            files={"seed": ("seed.tar", b"S", "application/x-tar")},
        )
        unknown_field = await client.post(
            "/v1/sandboxes/seeded",
            headers=_AUTH,
            data={"spec": '{"image": "oc8-runtime:1", "privileged": true}'},
            files={"seed": ("seed.tar", b"S", "application/x-tar")},
        )
        unauthenticated = await client.post(
            "/v1/sandboxes/seeded",
            data={"spec": '{"image": "oc8-runtime:1"}'},
            files={"seed": ("seed.tar", b"S", "application/x-tar")},
        )

    assert too_big.status_code == 413
    assert forbidden.status_code == 422
    assert unknown_field.status_code == 422
    assert unauthenticated.status_code == 401
    assert driver.provisioned == []


async def test_archives_stream_as_tar_bodies_within_the_cap() -> None:
    driver = ArchiveDriver()
    transport = httpx.ASGITransport(app=_archive_app(driver))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        handle = (
            await client.post("/v1/sandboxes", headers=_AUTH, json={"image": "oc8-runtime:1"})
        ).json()["container_id"]
        put = await client.put(
            f"/v1/sandboxes/{handle}/archive",
            params={"path": "/workspace"},
            headers={**_AUTH, "Content-Type": "application/x-tar"},
            content=b"INTAR",
        )
        got = await client.get(
            f"/v1/sandboxes/{handle}/archive", params={"path": "/workspace"}, headers=_AUTH
        )
        too_big_in = await client.put(
            f"/v1/sandboxes/{handle}/archive",
            params={"path": "/workspace"},
            headers={**_AUTH, "Content-Type": "application/x-tar"},
            content=b"X" * 2048,
        )
        too_big_out = await client.get(
            f"/v1/sandboxes/{handle}/archive", params={"path": "/big"}, headers=_AUTH
        )
        wrong_type = await client.put(
            f"/v1/sandboxes/{handle}/archive",
            params={"path": "/workspace"},
            headers={**_AUTH, "Content-Type": "application/json"},
            content=b"{}",
        )
        relative = await client.put(
            f"/v1/sandboxes/{handle}/archive",
            params={"path": "workspace"},
            headers={**_AUTH, "Content-Type": "application/x-tar"},
            content=b"INTAR",
        )
        unknown = await client.get(
            "/v1/sandboxes/not-a-handle/archive", params={"path": "/w"}, headers=_AUTH
        )

    assert put.status_code == 204
    assert driver.archives == [("docker-id", "/workspace", b"INTAR")]
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/x-tar"
    assert got.content == b"OUTTAR"
    assert too_big_in.status_code == 413
    assert too_big_out.status_code == 413
    assert wrong_type.status_code == 415
    assert relative.status_code == 422
    assert unknown.status_code == 404
    assert len(driver.archives) == 1


async def test_the_archive_cap_comes_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("OC8_SANDBOX_ARCHIVE_MAX_BYTES", "3")
    try:
        app = create_provisioner_app(
            token="provisioner-token",
            driver=ArchiveDriver(),
            policy=ProvisionerPolicy(
                allowed_images={"oc8-runtime:1"},
                agent_network="oc8_agents",
                session_root="/var/lib/oc8/sessions",
                platform_mounts={},
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            handle = (
                await client.post("/v1/sandboxes", headers=_AUTH, json={"image": "oc8-runtime:1"})
            ).json()["container_id"]
            response = await client.put(
                f"/v1/sandboxes/{handle}/archive",
                params={"path": "/w"},
                headers={**_AUTH, "Content-Type": "application/x-tar"},
                content=b"four",
            )
    finally:
        get_settings.cache_clear()
    assert response.status_code == 413


async def test_a_seeded_spec_without_a_content_length_is_refused() -> None:
    """The declared length is what bounds the read: the server reads exactly
    that much. Without it there is no bound before the body is spooled."""
    driver = ArchiveDriver()
    transport = httpx.ASGITransport(app=_archive_app(driver))
    boundary = "b0undary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="spec"\r\n\r\n'
        '{"image": "oc8-runtime:1"}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="seed"; filename="s.tar"\r\n'
        "Content-Type: application/x-tar\r\n\r\nS\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    async def chunked() -> Any:
        yield body

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/sandboxes/seeded",
            headers={**_AUTH, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            content=chunked(),
        )
    assert response.status_code == 411
    assert driver.provisioned == []


class CappedArchiveDriver(ArchiveDriver):
    def __init__(self) -> None:
        super().__init__()
        self.caps: list[int | None] = []

    async def get_archive(
        self, handle: SandboxHandle, path: str, *, max_bytes: int | None = None
    ) -> bytes:
        from oc8.sandbox.types import ArchiveTooLarge

        self.caps.append(max_bytes)
        if path == "/big":
            raise ArchiveTooLarge("too big")
        return b"OUTTAR"


async def test_the_archive_cap_is_enforced_by_the_driver_while_reading() -> None:
    """The broker hands its cap to the driver, so an oversized archive is cut
    off while it is read, not after it is all in memory. A smaller cap the
    caller asks for wins."""
    driver = CappedArchiveDriver()
    transport = httpx.ASGITransport(app=_archive_app(driver, archive_max_bytes=1024))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        handle = (
            await client.post("/v1/sandboxes", headers=_AUTH, json={"image": "oc8-runtime:1"})
        ).json()["container_id"]
        big = await client.get(
            f"/v1/sandboxes/{handle}/archive", params={"path": "/big"}, headers=_AUTH
        )
        ok = await client.get(
            f"/v1/sandboxes/{handle}/archive",
            params={"path": "/w", "max_bytes": 100},
            headers=_AUTH,
        )
        larger = await client.get(
            f"/v1/sandboxes/{handle}/archive",
            params={"path": "/w", "max_bytes": 1 << 30},
            headers=_AUTH,
        )

    assert big.status_code == 413
    assert ok.content == b"OUTTAR"
    assert larger.status_code == 200
    assert driver.caps == [1024, 100, 1024]


async def test_a_wait_past_its_timeout_is_a_504() -> None:
    class _StillRunning(FakeDriver):
        async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
            raise TimeoutError("still running")

    transport = httpx.ASGITransport(app=_app(_StillRunning()))
    auth = {"Authorization": "Bearer provisioner-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post("/v1/sandboxes", headers=auth, json={"image": "oc8-runtime:1"})
        handle = created.json()["container_id"]
        waited = await client.post(
            f"/v1/sandboxes/{handle}/wait", headers=auth, json={"timeout_s": 1}
        )
    assert waited.status_code == 504
