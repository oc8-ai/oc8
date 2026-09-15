"""Self-service CRUD for a member's own API keys (Settings -> API keys).

Same footing as `api/v1/totp.py`: every route is reachable by any
authenticated caller and `unguarded(...)`, never `require_permission`,
because `member_id` is always derived from the caller's own `principal`
(`member_id_for_principal`), never a path parameter -- holding a valid
bearer token IS the authorization, since no other member's keys are ever
reachable through this file.

A key's authenticated access (what it can call the outward MCP gateway,
`api/mcp_external.py`, to do) is never wider than its owner's own live
permissions, resolved fresh on every call -- so there is nothing to
configure here beyond identity (name), where it may be used from
(allowed_origins), and on/off.
"""

from __future__ import annotations

import datetime as dt
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field, field_validator

from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.apikeys.service import (
    create_api_key,
    delete_api_key,
    get_own_api_key,
    list_api_keys,
    set_enabled,
)
from oc8.audit import append_event
from oc8.auth.totp_gate import member_id_for_principal
from oc8.models.api_keys import ApiKey
from oc8.schemas.base import CamelModel

router = APIRouter()

_UNGUARDED_REASON = (
    "reachable by any authenticated caller managing their OWN API keys; "
    "member_id is derived from the caller's own principal, never a path "
    "parameter, so no other member's keys are ever reachable through this file"
)


def _validated_origins(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        origin = raw.strip().rstrip("/")
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path:
            raise ValueError(f"not a valid origin (scheme://host[:port]): {raw!r}")
        out.append(origin)
    return out


class ApiKeyDTO(CamelModel):
    id: str
    name: str
    token_prefix: str
    enabled: bool
    allowed_origins: list[str]
    last_used_at: dt.datetime | None
    created_at: dt.datetime


class ApiKeyCreatedDTO(ApiKeyDTO):
    #: The full plaintext secret. Present ONLY in the create response -- it
    #: is never stored and can never be shown again.
    token: str


class CreateApiKeyRequest(CamelModel):
    name: str = Field(min_length=1, max_length=200)
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("allowed_origins")
    @classmethod
    def _check_origins(cls, values: list[str]) -> list[str]:
        return _validated_origins(values)


class UpdateApiKeyRequest(CamelModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    allowed_origins: list[str] | None = None

    @field_validator("allowed_origins")
    @classmethod
    def _check_origins(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else _validated_origins(values)


def _to_dto(row: ApiKey) -> ApiKeyDTO:
    return ApiKeyDTO(
        id=str(row.id),
        name=row.name,
        token_prefix=row.token_prefix,
        enabled=row.enabled,
        allowed_origins=list(row.allowed_origins or []),
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )


@router.get(
    "/settings/api-keys",
    response_model=list[ApiKeyDTO],
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def list_own_api_keys(principal: CurrentPrincipal, db: DbSession) -> list[ApiKeyDTO]:
    member_id = await member_id_for_principal(db, principal)
    if member_id is None:
        return []
    rows = await list_api_keys(db, member_id=member_id)
    return [_to_dto(r) for r in rows]


@router.post(
    "/settings/api-keys",
    response_model=ApiKeyCreatedDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def create_own_api_key(
    body: CreateApiKeyRequest, principal: CurrentPrincipal, db: DbSession
) -> ApiKeyCreatedDTO:
    member_id = await member_id_for_principal(db, principal)
    if member_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    row, token = await create_api_key(
        db,
        tenant_id=principal.tenant_id,
        member_id=member_id,
        name=body.name,
        allowed_origins=body.allowed_origins,
    )
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=member_id,
        category="api_key",
        action="api_key.created",
        resource={"api_key_id": str(row.id), "name": row.name},
        reason="self-service API key created through POST /settings/api-keys",
        principal=principal,
    )
    await db.commit()
    return ApiKeyCreatedDTO(**_to_dto(row).model_dump(by_alias=False), token=token)


@router.patch(
    "/settings/api-keys/{key_id}",
    response_model=ApiKeyDTO,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def update_own_api_key(
    key_id: uuid.UUID,
    body: UpdateApiKeyRequest,
    principal: CurrentPrincipal,
    db: DbSession,
) -> ApiKeyDTO:
    member_id = await member_id_for_principal(db, principal)
    if member_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")

    if body.enabled is not None:
        row = await set_enabled(db, member_id=member_id, key_id=key_id, enabled=body.enabled)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    else:
        row = await get_own_api_key(db, member_id=member_id, key_id=key_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")

    if body.name is not None:
        row.name = body.name
    if body.allowed_origins is not None:
        row.allowed_origins = body.allowed_origins

    await db.commit()
    return _to_dto(row)


@router.delete(
    "/settings/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def delete_own_api_key(key_id: uuid.UUID, principal: CurrentPrincipal, db: DbSession) -> None:
    member_id = await member_id_for_principal(db, principal)
    if member_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    row = await get_own_api_key(db, member_id=member_id, key_id=key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    name = row.name
    deleted = await delete_api_key(db, member_id=member_id, key_id=key_id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=member_id,
        category="api_key",
        action="api_key.deleted",
        resource={"api_key_id": str(key_id), "name": name},
        reason="self-service API key deleted through DELETE /settings/api-keys/{key_id}",
        principal=principal,
    )
    await db.commit()
