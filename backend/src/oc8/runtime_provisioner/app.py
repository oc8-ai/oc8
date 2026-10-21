"""FastAPI application exposing the private provisioner capability."""

from __future__ import annotations

import hashlib
import hmac
import os
import posixpath
from base64 import b64decode, b64encode
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from oc8.config import get_settings
from oc8.sandbox import DockerSandboxDriver
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
from oc8.sandbox.types import SandboxError, SandboxHandle, SandboxSpec

from .policy import ProvisionerPolicy
from .store import ProvisionerStore

#: Room a seeded provision request may take beyond the seed itself: the JSON
#: spec part and the multipart framing.
_SEEDED_OVERHEAD_BYTES = 2 * 1024 * 1024


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="archive exceeds the size cap"
    )


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    return int(raw) if raw is not None and raw.isdigit() else None


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
    # Images a runtime capa declares for itself. Looked up here, at call time,
    # so a test can patch the helper without importing this module first.
    from oc8.runtime.images import declared_runtime_images

    capa_images = declared_runtime_images()
    return ProvisionerPolicy(
        allowed_images={agent_image, nanoclaw_agent, nanoclaw_provisioner, hook_image}
        | cli_images
        | capa_images,
        networked_images={agent_image, nanoclaw_agent} | cli_images | capa_images,
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
    archive_max_bytes: int | None = None,
) -> FastAPI:
    """Create a token-protected API that never exposes Docker identifiers.

    ``archive_max_bytes`` caps every tar that crosses the API (a seed, a
    ``put_archive`` body, a ``get_archive`` result); None takes
    ``sandbox_archive_max_bytes`` from the settings.
    """
    selected_token = token if token is not None else get_settings().sandbox_provisioner_token
    if not selected_token:
        raise ValueError("runtime provisioner requires OC8_SANDBOX_PROVISIONER_TOKEN")

    active_driver = driver if driver is not None else DockerSandboxDriver()
    active_policy = policy if policy is not None else _default_policy()
    archive_cap = (
        archive_max_bytes
        if archive_max_bytes is not None
        else get_settings().sandbox_archive_max_bytes
    )
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

    async def provision_domain(spec: SandboxSpec, request_owner: str) -> SandboxHandleDTO:
        try:
            handle = await active_driver.provision(active_policy.validate(spec))
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
        opaque = store.put(handle, owner=request_owner)
        return SandboxHandleDTO(container_id=opaque.container_id, image=opaque.image)

    @app.post("/v1/sandboxes", response_model=SandboxHandleDTO, status_code=status.HTTP_201_CREATED)
    async def provision(
        spec: SandboxSpecDTO,
        request_owner: str = Depends(authenticated_owner),
    ) -> SandboxHandleDTO:
        return await provision_domain(spec.to_domain(), request_owner)

    @app.post(
        "/v1/sandboxes/seeded",
        response_model=SandboxHandleDTO,
        status_code=status.HTTP_201_CREATED,
    )
    async def provision_seeded(
        request: Request,
        request_owner: str = Depends(authenticated_owner),
    ) -> SandboxHandleDTO:
        """Provision with a seed: multipart with a JSON ``spec`` field and an
        ``application/x-tar`` ``seed`` file. One request, so the driver can
        create, seed and start in that order.

        The body is read only here, after authentication: no route parameter
        asks FastAPI to parse it first. Starlette spools a large part to disk.
        """
        declared = _declared_length(request)
        if declared is not None and declared > archive_cap + _SEEDED_OVERHEAD_BYTES:
            raise _too_large()
        try:
            async with request.form(max_files=1, max_fields=1) as form:
                raw_spec = form.get("spec")
                seed_part = form.get("seed")
                if not isinstance(raw_spec, str) or not isinstance(seed_part, UploadFile):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail="expected a spec field and a seed file",
                    )
                seed = await seed_part.read(archive_cap + 1)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_400_BAD_REQUEST:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="invalid sandbox request",
                ) from exc
            raise
        if len(seed) > archive_cap:
            raise _too_large()
        try:
            spec = SandboxSpecDTO.model_validate_json(raw_spec)
        except ValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid sandbox request",
            ) from exc
        return await provision_domain(spec.to_domain(seed=seed), request_owner)

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

    @app.put("/v1/sandboxes/{opaque_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
    async def put_archive(
        opaque_id: str,
        path: str,
        request: Request,
        request_owner: str = Depends(authenticated_owner),
    ) -> Response:
        """Unpack an ``application/x-tar`` body into ``path``. The body is
        read in chunks and refused with 413 as soon as it passes the cap."""
        handle = resolve(opaque_id, request_owner)
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type != TAR_MEDIA_TYPE:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"expected {TAR_MEDIA_TYPE}",
            )
        if not posixpath.isabs(path):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid sandbox request",
            )
        declared = _declared_length(request)
        if declared is not None and declared > archive_cap:
            raise _too_large()
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > archive_cap:
                raise _too_large()
        try:
            await active_driver.put_archive(handle, path, bytes(body))
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid sandbox request",
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/sandboxes/{opaque_id}/archive")
    async def get_archive(
        opaque_id: str,
        path: str,
        request_owner: str = Depends(authenticated_owner),
    ) -> Response:
        """A tar of ``path`` as an ``application/x-tar`` body."""
        handle = resolve(opaque_id, request_owner)
        if not posixpath.isabs(path):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="invalid sandbox request",
            )
        try:
            data = await active_driver.get_archive(handle, path)
        except SandboxError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail="sandbox operation failed"
            ) from exc
        if len(data) > archive_cap:
            raise _too_large()
        return Response(content=data, media_type=TAR_MEDIA_TYPE)

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
