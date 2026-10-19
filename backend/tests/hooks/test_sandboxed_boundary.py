"""SandboxedExecutor against a fake driver (no runsc) with an echo worker:
proves round-trip transform, scoped token present, no DB creds reach it."""

from __future__ import annotations

import json
import uuid
from typing import Any

from oc8.hooks.executor import WORKER_RESPONSE_PATH, SandboxedExecutor
from oc8.hooks.types import HookCtx, HookHandler
from oc8.sandbox.types import ExecResult, SandboxHandle, SandboxSpec


class FakeDriver:
    """In-memory fake standing in for a container: an echo worker that appends
    the plugin id to data and never sees DB creds."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.saw_token: str | None = None

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        assert spec.network_disabled is True  # boundary: no network
        return SandboxHandle(container_id="fake", image=spec.image)

    async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        self.files[path] = content
        req = json.loads(content.decode())
        self.saw_token = req.get("token")
        assert "db_url" not in req and "database_url" not in req  # never DB creds

    async def exec(
        self,
        handle: SandboxHandle,
        command: list[str],
        *,
        workdir: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ExecResult:
        req = json.loads(self.files["/workspace/req.json"].decode())
        assert req.get("token")  # scoped token present
        data: dict[str, Any] = req.get("data", {})
        data = {**data, "worker": "echo"}
        self.files[WORKER_RESPONSE_PATH] = json.dumps({"data": data}).encode()
        return ExecResult(exit_code=0, output="")

    async def fs_read(self, handle: SandboxHandle, path: str) -> bytes:
        return self.files[path]

    async def teardown(self, handle: SandboxHandle) -> None:
        return None


async def test_sandboxed_filter_roundtrip_with_scoped_token() -> None:
    driver = FakeDriver()
    ex = SandboxedExecutor(driver=driver, grants={"p1": ["api:tasks.read"]})
    ctx = HookCtx(tenant_id=uuid.uuid4())
    h = HookHandler("p1", "task.before_create", 10, False, None, ex)
    out = await ex.run_filter(h, ctx, {"title": "t"})
    assert out["worker"] == "echo"
    assert driver.saw_token  # the worker got a token, not DB creds

    # token is a plugin-kind token scoped to the grant
    from oc8.auth import get_identity_provider

    assert driver.saw_token is not None
    principal = get_identity_provider().verify(driver.saw_token)
    assert principal.kind == "plugin"
    assert principal.scopes == ["api:tasks.read"]


async def test_sandboxed_pool_reuses_worker_per_key() -> None:
    """Two dispatches for the same plugin reuse the pooled worker (one
    provision call), not a fresh container per call."""
    driver = FakeDriver()
    provisions = 0
    orig_provision = driver.provision

    async def counting_provision(spec: SandboxSpec) -> SandboxHandle:
        nonlocal provisions
        provisions += 1
        return await orig_provision(spec)

    driver.provision = counting_provision  # type: ignore[method-assign]
    ex = SandboxedExecutor(driver=driver, grants={"p1": []})
    ctx = HookCtx(tenant_id=uuid.uuid4())
    h = HookHandler("p1", "task.before_create", 10, False, None, ex)
    await ex.run_filter(h, ctx, {"title": "a"})
    await ex.run_filter(h, ctx, {"title": "b"})
    assert provisions == 1


async def test_hook_worker_carries_the_organisation_label() -> None:
    from oc8.sandbox.reaper import TENANT_LABEL

    driver = FakeDriver()
    seen: list[Any] = []
    original = driver.provision

    async def spy(spec: Any) -> SandboxHandle:
        seen.append(spec)
        return await original(spec)

    driver.provision = spy  # type: ignore[method-assign]
    ex = SandboxedExecutor(driver=driver, grants={"p1": []})
    tenant = uuid.uuid4()
    h = HookHandler("p1", "task.before_create", 10, False, None, ex)
    await ex.run_filter(h, HookCtx(tenant_id=tenant), {"title": "t"})
    assert seen[0].labels[TENANT_LABEL] == str(tenant)
