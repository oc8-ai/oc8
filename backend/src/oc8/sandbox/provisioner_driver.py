"""Typed client for the private runtime-provisioner service.

This is deliberately a narrow SandboxDriver implementation, never a generic
Docker HTTP proxy.  Server-side policy and opaque-handle ownership arrive with
the provisioner service in the next task.
"""

from __future__ import annotations

import binascii
import json as jsonlib
from typing import Any, TypeVar

import httpx
from pydantic import ValidationError

from oc8.sandbox.provisioner_models import (
    TAR_MEDIA_TYPE,
    ExecRequestDTO,
    ExecResponseDTO,
    FileReadRequestDTO,
    FileReadResponseDTO,
    FileWriteRequestDTO,
    LogsResponseDTO,
    ReapResponseDTO,
    SandboxHandleDTO,
    SandboxSpecDTO,
    WaitRequestDTO,
    WaitResponseDTO,
)
from oc8.sandbox.types import ExecResult, SandboxError, SandboxHandle, SandboxSpec

#: Archives can be many MB. The client's 30s default is for calls that answer
#: near-instantly; a transfer gets longer, still bounded so a wedged
#: provisioner fails the caller instead of hanging it.
_ARCHIVE_TIMEOUT_S = 300.0

TransportResponse = TypeVar(
    "TransportResponse",
    SandboxHandleDTO,
    ExecResponseDTO,
    FileReadResponseDTO,
    WaitResponseDTO,
    LogsResponseDTO,
    ReapResponseDTO,
)


class ProvisionerSandboxDriver:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        if client is not None:
            self._client.headers["Authorization"] = f"Bearer {token}"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - httpx per-request override, not asyncio
        **body: Any,
    ) -> httpx.Response:
        # httpx treats an explicit `timeout=None` as "no timeout at all", not
        # "use the client's own default" -- those are different sentinels
        # (USE_CLIENT_DEFAULT vs None). Only override per-request when a
        # caller actually passed one (`wait`, below); every other call keeps
        # the client's 30s default by omitting the kwarg entirely.
        try:
            if timeout is not None:
                response = await self._client.request(
                    method, path, json=json, timeout=timeout, **body
                )
            else:
                response = await self._client.request(method, path, json=json, **body)
        except httpx.HTTPError as exc:
            raise SandboxError("provisioner request failed") from exc
        if not response.is_success:
            raise SandboxError(f"provisioner request failed (HTTP {response.status_code})")
        return response

    @staticmethod
    def _model(response: httpx.Response, model: type[TransportResponse]) -> TransportResponse:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise SandboxError("provisioner returned an invalid response") from exc

    async def provision(self, spec: SandboxSpec) -> SandboxHandle:
        wire = SandboxSpecDTO.from_domain(spec).to_wire()
        if spec.seed is None:
            response = await self._request("POST", "/v1/sandboxes", json=wire)
        else:
            # One request, so the provisioner can create, seed and start in
            # that order without the container ever running unseeded.
            response = await self._request(
                "POST",
                "/v1/sandboxes/seeded",
                data={"spec": jsonlib.dumps(wire)},
                files={"seed": ("seed.tar", spec.seed, TAR_MEDIA_TYPE)},
                timeout=_ARCHIVE_TIMEOUT_S,
            )
        return self._model(response, SandboxHandleDTO).to_domain()

    async def exec(
        self,
        handle: SandboxHandle,
        command: list[str],
        *,
        workdir: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - SandboxDriver contract
    ) -> ExecResult:
        request = ExecRequestDTO(command=command, workdir=workdir, timeout=timeout)
        response = await self._request(
            "POST",
            f"/v1/sandboxes/{handle.container_id}/exec",
            json=request.model_dump(mode="json"),
        )
        result = self._model(response, ExecResponseDTO)
        return ExecResult(exit_code=result.exit_code, output=result.output)

    async def fs_write(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        request = FileWriteRequestDTO.from_content(path, content)
        await self._request(
            "POST",
            f"/v1/sandboxes/{handle.container_id}/fs/write",
            json=request.model_dump(mode="json"),
        )

    async def fs_read(self, handle: SandboxHandle, path: str) -> bytes:
        request = FileReadRequestDTO(path=path)
        response = await self._request(
            "POST",
            f"/v1/sandboxes/{handle.container_id}/fs/read",
            json=request.model_dump(mode="json"),
        )
        result = self._model(response, FileReadResponseDTO)
        try:
            return result.content()
        except (ValueError, binascii.Error) as exc:
            raise SandboxError("provisioner returned invalid file content") from exc

    async def put_archive(self, handle: SandboxHandle, path: str, data: bytes) -> None:
        await self._request(
            "PUT",
            f"/v1/sandboxes/{handle.container_id}/archive",
            params={"path": path},
            content=data,
            headers={"Content-Type": TAR_MEDIA_TYPE},
            timeout=_ARCHIVE_TIMEOUT_S,
        )

    async def get_archive(self, handle: SandboxHandle, path: str) -> bytes:
        response = await self._request(
            "GET",
            f"/v1/sandboxes/{handle.container_id}/archive",
            params={"path": path},
            timeout=_ARCHIVE_TIMEOUT_S,
        )
        return response.content

    async def teardown(self, handle: SandboxHandle) -> None:
        await self._request("DELETE", f"/v1/sandboxes/{handle.container_id}")

    async def wait(self, handle: SandboxHandle, timeout_s: float = 600.0) -> int:
        # The client's own default (30s, __init__ above) is for every OTHER
        # call, which all answer near-instantly. This one is different by
        # design: the server holds the connection open for up to `timeout_s`
        # while the container actually runs -- a real agent step (one model
        # call plus a tool round trip) routinely exceeds 30s. Without this
        # override every run past that mark raised `SandboxError("provisioner
        # request failed")` from httpx's own 30s ReadTimeout, indistinguishable
        # from a genuinely broken provisioner and, until the tenant-GUC fix
        # alongside this one, capable of stranding the run in "running" for
        # the full 10-minute reconciler window. +30s of headroom over the
        # server's own bound, so a slow-but-real response is never raced.
        response = await self._request(
            "POST",
            f"/v1/sandboxes/{handle.container_id}/wait",
            json=WaitRequestDTO(timeout_s=timeout_s).model_dump(mode="json"),
            timeout=timeout_s + 30.0,
        )
        result = self._model(response, WaitResponseDTO)
        return result.exit_code

    async def logs(self, handle: SandboxHandle) -> str:
        response = await self._request("GET", f"/v1/sandboxes/{handle.container_id}/logs")
        result = self._model(response, LogsResponseDTO)
        return result.output

    async def reap_orphans(self) -> int:
        """Invoke the provisioner's fixed orphan-reaping operation.

        There are intentionally no labels, container IDs, or Docker filters in
        this request: the provisioner owns those choices server-side.
        """
        response = await self._request("POST", "/v1/reap-orphans")
        return self._model(response, ReapResponseDTO).count
