"""Sandbox capability: isolated Docker workspaces for agent runs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from oc8.config import get_settings
from oc8.sandbox.docker_driver import DockerSandboxDriver
from oc8.sandbox.driver import SandboxDriver
from oc8.sandbox.provisioner_driver import ProvisionerSandboxDriver
from oc8.sandbox.types import (
    ExecResult,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
    ScratchDir,
)

__all__ = [
    "DockerSandboxDriver",
    "ExecResult",
    "ProvisionerSandboxDriver",
    "SandboxDriver",
    "SandboxError",
    "SandboxHandle",
    "SandboxSpec",
    "ScratchDir",
    "get_sandbox_driver",
    "sandbox_session",
    "set_sandbox_driver",
]

_driver: SandboxDriver | None = None


def get_sandbox_driver() -> SandboxDriver:
    global _driver
    if _driver is None:
        settings = get_settings()
        if settings.sandbox_driver == "provisioner":
            _driver = ProvisionerSandboxDriver(
                base_url=settings.sandbox_provisioner_url,
                token=settings.sandbox_provisioner_token,
            )
        else:
            _driver = DockerSandboxDriver()
    return _driver


def set_sandbox_driver(driver: SandboxDriver) -> None:
    global _driver
    _driver = driver


@asynccontextmanager
async def sandbox_session(
    spec: SandboxSpec, *, driver: SandboxDriver | None = None
) -> AsyncIterator[tuple[SandboxDriver, SandboxHandle]]:
    drv = driver or get_sandbox_driver()
    handle = await drv.provision(spec)
    try:
        yield drv, handle
    finally:
        await drv.teardown(handle)
