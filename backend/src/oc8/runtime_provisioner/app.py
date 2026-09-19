"""FastAPI application exposing the private provisioner capability."""

from __future__ import annotations

import hashlib
import hmac
import os
from base64 import b64decode, b64encode
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status

from oc8.config import get_settings
from oc8.sandbox import DockerSandboxDriver
from oc8.sandbox.provisioner_models import (
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
from oc8.sandbox.types import SandboxError, SandboxHandle

from .policy import ProvisionerPolicy
from .store import ProvisionerStore


def _default_policy() -> ProvisionerPolicy:
    settings = get_settings()
    agent_image = settings.agent_runtime_image
    nanoclaw_agent = os.environ.get("OC8_NANOCLAW_AGENT_IMAGE", "nanoclaw-agent:latest")
    nanoclaw_provisioner = os.environ.get(
        "OC8_NANOCLAW_PROVISIONER_IMAGE", "nanoclaw-provisioner:latest"
    )
    hook_image = os.environ.get("OC8_HOOK_RUNTIME_IMAGE", "docker.io/library/python:3.13-alpine")
    # The headless-CLI runtimes each ship their own image and all three need the
    # agent network: their harness reaches oc8's /llm and /mcp gateways, so a
    # network-disabled sandbox would leave them with no model and no tools.
    cli_images = {
        settings.claude_code_agent_image,
        settings.codex_agent_image,
        settings.opencode_agent_image,
    }
    return ProvisionerPolicy(
        allowed_images={agent_image, nanoclaw_agent, nanoclaw_provisioner, hook_image} | cli_images,
        networked_images={agent_image, nanoclaw_agent} | cli_images,
        agent_network=settings.agent_runtime_network,
        session_root=settings.runtime_session_root,
        platform_mounts={
            str(Path(path).resolve()): target
            for path, target in _platform_mounts_from_environment().items()
        },
    )


def _platform_mounts_from_environment() -> dict[str, str]:
    return {
        path: target
        for path, target in {
            os.environ.get("OC8_NANOCLAW_RUNNER_ROOT", ""): "/app/src",
            os.environ.get("OC8_NANOCLAW_SKILLS_ROOT", ""): "/app/skills",
            os.environ.get("OC8_NANOCLAW_BRIDGE_PATH", ""): "/app/oc8-mcp-bridge.mjs",
        }.items()
        if path
    }


def create_provisioner_app(
    *,
    token: str | None = None,
    driver: Any | None = None,
    policy: ProvisionerPolicy | None = None,
) -> FastAPI:
    """Create a token-protected API that never exposes Docker identifiers."""
    selected_token = token if token is not None else get_settings().sandbox_provisioner_token
    if not selected_token:
        raise ValueError("runtime provisioner requires OC8_SANDBOX_PROVISIONER_TOKEN")

    active_driver = driver if driver is not None else DockerSandboxDriver()
    active_policy = policy if policy is not None else _default_policy()
    store = ProvisionerStore()
    owner = hashlib.sha256(selected_token.encode("utf-8")).hexdigest()
    app = FastAPI(title="OC8 Runtime Provisioner", docs_url=None, redoc_url=None)

    def authenticated_owner(
        authorization: Annotated[str | None, Header()] = None,
    ) -> str:
        expected = f"Bearer {selected_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        return owner

    def resolve(opaque_id: str, request_owner: str) -> SandboxHandle:
        handle = store.get(opaque_id, owner=request_owner)
        if handle is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        return handle

    @app.post("/v1/sandboxes", response_model=SandboxHandleDTO, status_code=status.HTTP_201_CREATED)
    async def provision(
        spec: SandboxSpecDTO,
        request_owner: str = Depends(authenticated_owner),
    ) -> SandboxHandleDTO:
        try:
            handle = await active_driver.provision(active_policy.validate(spec.to_domain()))
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        opaque = store.put(handle, owner=request_owner)
        return SandboxHandleDTO(container_id=opaque.container_id, image=opaque.image)

    @app.post("/v1/sandboxes/{opaque_id}/exec", response_model=ExecResponseDTO)
    async def execute(
        opaque_id: str,
        request: ExecRequestDTO,
        request_owner: str = Depends(authenticated_owner),
    ) -> ExecResponseDTO:
        try:
            result = await active_driver.exec(
                resolve(opaque_id, request_owner),
                request.command,
                workdir=request.workdir,
                timeout=request.timeout,
            )
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="sandbox operation failed"
            ) from exc
        return ExecResponseDTO(exit_code=result.exit_code, output=result.output)

    @app.post("/v1/sandboxes/{opaque_id}/fs/write", status_code=status.HTTP_204_NO_CONTENT)
    async def write_file(
        opaque_id: str,
        request: FileWriteRequestDTO,
        request_owner: str = Depends(authenticated_owner),
    ) -> Response:
        try:
            content = b64decode(request.content_base64, validate=True)
            await active_driver.fs_write(resolve(opaque_id, request_owner), request.path, content)
        except (SandboxError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid sandbox request",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/sandboxes/{opaque_id}/fs/read", response_model=FileReadResponseDTO)
    async def read_file(
        opaque_id: str,
        request: FileReadRequestDTO,
        request_owner: str = Depends(authenticated_owner),
    ) -> FileReadResponseDTO:
        try:
            content = await active_driver.fs_read(resolve(opaque_id, request_owner), request.path)
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="sandbox operation failed"
            ) from exc
        return FileReadResponseDTO(content_base64=b64encode(content).decode("ascii"))

    @app.delete("/v1/sandboxes/{opaque_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def teardown(
        opaque_id: str,
        request_owner: str = Depends(authenticated_owner),
    ) -> Response:
        try:
            handle = resolve(opaque_id, request_owner)
            await active_driver.teardown(handle)
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="sandbox operation failed"
            ) from exc
        store.discard(opaque_id, owner=request_owner)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/sandboxes/{opaque_id}/wait", response_model=WaitResponseDTO)
    async def wait(
        opaque_id: str,
        request: WaitRequestDTO,
        request_owner: str = Depends(authenticated_owner),
    ) -> WaitResponseDTO:
        try:
            exit_code = await active_driver.wait(
                resolve(opaque_id, request_owner), request.timeout_s
            )
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="sandbox operation failed"
            ) from exc
        return WaitResponseDTO(exit_code=exit_code)

    @app.get("/v1/sandboxes/{opaque_id}/logs", response_model=LogsResponseDTO)
    async def logs(
        opaque_id: str,
        request_owner: str = Depends(authenticated_owner),
    ) -> LogsResponseDTO:
        try:
            output = await active_driver.logs(resolve(opaque_id, request_owner))
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="sandbox operation failed"
            ) from exc
        return LogsResponseDTO(output=output)

    @app.post("/v1/reap-orphans", response_model=ReapResponseDTO)
    async def reap_orphans(request_owner: str = Depends(authenticated_owner)) -> ReapResponseDTO:
        """Run the fixed stale-sandbox sweep without accepting Docker controls."""
        del request_owner  # Authentication is required even though it has no per-handle scope.
        try:
            count = await active_driver.reap_orphans()
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="sandbox operation failed"
            ) from exc
        return ReapResponseDTO(count=count)

    return app
