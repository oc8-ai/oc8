"""Hook executors: in-process for trusted plugins; a pooled hardened sandbox
worker for community plugins, holding only a scoped internal-API token."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from oc8.auth import get_identity_provider
from oc8.hooks.types import HookCtx, HookHandler
from oc8.sandbox import get_sandbox_driver
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.naming import container_name
from oc8.sandbox.types import SandboxHandle, SandboxSpec

WORKER_REQUEST_PATH = "/workspace/req.json"
WORKER_RESPONSE_PATH = "/workspace/resp.json"
WORKER_ENTRYPOINT = ["python", "/workspace/worker.py"]

DEFAULT_COMMUNITY_REGIME: dict[str, Any] = {
    "runtime": "runsc",
    "read_only": True,
    "network_disabled": True,
    "mem_limit": "256m",
    "pids_limit": 128,
    "cpu_limit": 1.0,
}


class InProcessExecutor:
    """Runs a handler's Python callable directly in the control-plane
    process. Only for first_party/verified plugins whose code is trusted."""

    async def run_filter(self, handler: HookHandler, ctx: HookCtx, data: Any) -> Any:
        assert handler.fn is not None
        return handler.fn(ctx, data)

    async def run_action(
        self, handler: HookHandler, ctx: HookCtx, kwargs: dict[str, Any]
    ) -> None:
        assert handler.fn is not None
        handler.fn(ctx, **kwargs)


def make_scoped_token(tenant_id: uuid.UUID, plugin_id: str, scopes: list[str]) -> str:
    """A short-TTL kind=plugin token scoped to the plugin's granted
    permissions — the only credential a sandboxed worker ever receives."""
    return get_identity_provider().mint(
        tenant_id=tenant_id,
        subject=f"plugin:{plugin_id}",
        role="plugin",
        kind="plugin",
        scopes=scopes,
    )


@dataclass
class SandboxedExecutor:
    """Pooled: one hardened worker per (tenant, plugin_version). Worker code
    receives a scoped token, never DB creds."""

    driver: SandboxDriver | None = None
    image: str = "docker.io/library/python:3.13-alpine"
    regime: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_COMMUNITY_REGIME))
    grants: dict[str, list[str]] = field(default_factory=dict)  # plugin_id -> scopes
    _pool: dict[str, SandboxHandle] = field(default_factory=dict)

    def _driver(self) -> SandboxDriver:
        return self.driver or get_sandbox_driver()

    async def _ensure(self, key: str) -> SandboxHandle:
        if key not in self._pool:
            spec = SandboxSpec(
                image=self.image,
                # No run id: this worker is POOLED and outlives any one run.
                name=container_name(key, "hook"),
                command=["sleep", "3600"],
                runtime=self.regime.get("runtime"),
                read_only=bool(self.regime.get("read_only", False)),
                network_disabled=bool(self.regime.get("network_disabled", True)),
                mem_limit=str(self.regime.get("mem_limit", "256m")),
                pids_limit=int(self.regime.get("pids_limit", 128)),
                cpu_limit=float(self.regime.get("cpu_limit", 1.0)),
            )
            self._pool[key] = await self._driver().provision(spec)
        return self._pool[key]

    async def _invoke(self, handler: HookHandler, ctx: HookCtx, payload: dict[str, Any]) -> Any:
        key = handler.plugin_id
        handle = await self._ensure(key)
        token = make_scoped_token(ctx.tenant_id, handler.plugin_id, self.grants.get(key, []))
        request = {
            "point": handler.point,
            "tenant_id": str(ctx.tenant_id),
            "token": token,
            **payload,
        }
        driver = self._driver()
        await driver.fs_write(handle, WORKER_REQUEST_PATH, json.dumps(request).encode())
        result = await driver.exec(handle, WORKER_ENTRYPOINT, timeout=10.0)
        if result.exit_code != 0:
            raise RuntimeError(f"worker exit {result.exit_code}: {result.output[:500]}")
        raw = await driver.fs_read(handle, WORKER_RESPONSE_PATH)
        return json.loads(raw.decode())

    async def run_filter(self, handler: HookHandler, ctx: HookCtx, data: Any) -> Any:
        resp = await self._invoke(handler, ctx, {"data": data})
        return resp.get("data", data)

    async def run_action(
        self, handler: HookHandler, ctx: HookCtx, kwargs: dict[str, Any]
    ) -> None:
        await self._invoke(handler, ctx, {"kwargs": kwargs})

    async def teardown(self) -> None:
        driver = self._driver()
        for handle in self._pool.values():
            await driver.teardown(handle)
        self._pool.clear()
