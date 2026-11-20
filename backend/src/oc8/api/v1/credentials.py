"""Central Settings > Credentials CRUD + test (unified credentials
framework design, §4). Metadata + non-secret field values only, ever --
secret field values are write-only, exactly like /secrets already is.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import MANAGE, SECRET, VIEW, perm
from oc8.capas.i18n import translations_for
from oc8.capas.manifest import SetupFieldSpec
from oc8.credentials.enablement import department_ids_by_credential
from oc8.credentials.registry import (
    CredentialTypeI18n,
    CredentialTypeNotFound,
    list_credential_types,
    list_credential_types_with_i18n,
)
from oc8.credentials.service import (
    CredentialFieldNotSet,
    CredentialInUse,
    CredentialNotFound,
    create_credential,
    delete_credential,
    list_credentials,
    missing_required,
    test_credential,
    update_credential,
)
from oc8.schemas.dto import ConnectionUsedByDTO, CredentialDTO, CredentialTypeDTO
from oc8.schemas.requests import CreateCredentialRequest, UpdateCredentialRequest
from oc8.secrets.keyprovider import SecretStoreUnavailable

router = APIRouter()


@dataclass(frozen=True)
class CredentialFacts:
    """What a login listing shows besides the row: the departments it is
    enabled for, the required fields it lacks and what uses it."""

    department_ids: list[uuid.UUID]
    incomplete: list[str]
    used_by: ConnectionUsedByDTO


async def credential_facts(
    db: AsyncSession, tenant_id: uuid.UUID, creds: list[m.Credential]
) -> dict[uuid.UUID, CredentialFacts]:
    """`CredentialFacts` for each row, loaded in a few queries."""
    if not creds:
        return {}
    ids = [c.id for c in creds]
    enabled = await department_ids_by_credential(db, ids)
    types = {t.name: t for t in await list_credential_types(db, tenant_id=tenant_id)}
    agents: dict[uuid.UUID, set[uuid.UUID]] = {i: set() for i in ids}
    for credential_id, agent_id in await db.execute(
        select(m.AgentToolAccess.credential_id, m.AgentToolAccess.agent_id).where(
            m.AgentToolAccess.credential_id.in_(ids)
        )
    ):
        agents[credential_id].add(agent_id)
    wired: dict[uuid.UUID, set[uuid.UUID]] = {i: set() for i in ids}
    for credential_id, department_id in await db.execute(
        select(m.McpConnection.credential_id, m.McpConnection.department_id).where(
            m.McpConnection.credential_id.in_(ids), m.McpConnection.department_id.is_not(None)
        )
    ):
        wired[credential_id].add(department_id)
    sources: dict[str, list[str]] = {str(i): [] for i in ids}
    for name, config in await db.execute(
        select(m.DataSource.name, m.DataSource.config).where(m.DataSource.deleted_at.is_(None))
    ):
        named = (config or {}).get("credential") if isinstance(config, dict) else None
        if isinstance(named, str) and named in sources:
            sources[named].append(name)
    out: dict[uuid.UUID, CredentialFacts] = {}
    for cred in creds:
        cred_type = types.get(cred.credential_type)
        incomplete = (
            missing_required(cred_type, cred.field_values or {}, cred.secret_refs or {})
            if cred_type is not None
            else []
        )
        departments = list(dict.fromkeys([*enabled[cred.id], *sorted(wired[cred.id])]))
        out[cred.id] = CredentialFacts(
            department_ids=enabled[cred.id],
            incomplete=incomplete,
            used_by=ConnectionUsedByDTO(
                departments=[str(d) for d in departments],
                agents=sorted(str(a) for a in agents[cred.id]),
                data_sources=sorted(sources[str(cred.id)]),
            ),
        )
    return out


def _to_dto(cred: m.Credential, facts: CredentialFacts | None = None) -> CredentialDTO:
    extra: dict[str, Any] = (
        {}
        if facts is None
        else {
            "department_ids": [str(d) for d in facts.department_ids],
            "incomplete": facts.incomplete,
            "used_by": facts.used_by,
        }
    )
    return CredentialDTO(
        **extra,
        id=str(cred.id),
        name=cred.name,
        credential_type=cred.credential_type,
        field_values=cred.field_values,
        last_tested_at=cred.last_tested_at.isoformat() if cred.last_tested_at else None,
        last_test_ok=cred.last_test_ok,
        all_departments=cred.all_departments,
        created_at=cred.created_at.isoformat(),
        updated_at=cred.updated_at.isoformat(),
    )


@router.post(
    "/credentials",
    response_model=CredentialDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(SECRET, MANAGE)))],
)
async def create_credential_endpoint(
    body: CreateCredentialRequest, db: DbSession, principal: CurrentPrincipal
) -> CredentialDTO:
    try:
        cred = await create_credential(
            db,
            tenant_id=principal.tenant_id,
            name=body.name,
            credential_type=body.credential_type,
            field_values=body.field_values,
        )
    except CredentialTypeNotFound as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown credential type: {exc}") from exc
    except SecretStoreUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    # create_credential's second flush (writing secret_refs) triggers the
    # onupdate=func.now() on updated_at without eagerly refreshing it back
    # onto this instance -- an explicit refresh avoids a lazy load below
    # (a sync attribute access the async session can't service).
    await db.refresh(cred)
    dto = _to_dto(cred)
    await db.commit()
    return dto


@router.get(
    "/credentials",
    response_model=list[CredentialDTO],
    dependencies=[Depends(require_permission(perm(SECRET, VIEW)))],
)
async def list_credentials_endpoint(
    db: DbSession,
    principal: CurrentPrincipal,
    type: str | None = Query(default=None, alias="type"),
) -> list[CredentialDTO]:
    rows = await list_credentials(
        db, tenant_id=principal.tenant_id, credential_type=type, owner="organisation"
    )
    facts = await credential_facts(db, principal.tenant_id, rows)
    return [_to_dto(r, facts[r.id]) for r in rows]


@router.patch(
    "/credentials/{credential_id}",
    response_model=CredentialDTO,
    dependencies=[Depends(require_permission(perm(SECRET, MANAGE)))],
)
async def update_credential_endpoint(
    credential_id: uuid.UUID,
    body: UpdateCredentialRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> CredentialDTO:
    try:
        cred = await update_credential(
            db,
            tenant_id=principal.tenant_id,
            credential_id=credential_id,
            owner="organisation",
            name=body.name,
            field_values=body.field_values,
        )
    except CredentialNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found") from exc
    except SecretStoreUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    # Same reason as create_credential_endpoint's refresh above: the UPDATE
    # this call flushes re-triggers onupdate=func.now() without eagerly
    # syncing it back onto this instance.
    await db.refresh(cred)
    dto = _to_dto(cred)
    await db.commit()
    return dto


@router.delete(
    "/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(SECRET, MANAGE)))],
)
async def delete_credential_endpoint(
    credential_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal, force: bool = False
) -> None:
    try:
        await delete_credential(
            db,
            tenant_id=principal.tenant_id,
            credential_id=credential_id,
            owner="organisation",
            force=force,
        )
    except CredentialNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found") from exc
    except CredentialInUse as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await db.commit()


@router.post(
    "/credentials/{credential_id}/test",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_permission(perm(SECRET, MANAGE)))],
)
async def test_credential_endpoint(
    credential_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> dict[str, bool]:
    try:
        await test_credential(
            db, tenant_id=principal.tenant_id, credential_id=credential_id, owner="organisation"
        )
    except CredentialNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found") from exc
    except CredentialFieldNotSet as exc:
        # A REQUIRED field with no value (test_credential only lets an
        # OPTIONAL one through unset) -- a misconfigured credential, not a
        # missing one, so this is a 422 like a real validate_entry_point
        # failure would be, not the 404 CredentialNotFound gets.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"credential {exc.credential_name!r} has no value set for field {exc.field_key!r}",
        ) from exc
    except ValueError as exc:
        await db.commit()  # persist the failed last_tested_at/last_test_ok before reporting
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await db.commit()
    return {"ok": True}


@router.get(
    "/credential-types",
    response_model=list[CredentialTypeDTO],
    dependencies=[Depends(require_permission(perm(SECRET, VIEW)))],
)
async def list_credential_types_endpoint(
    db: DbSession, principal: CurrentPrincipal
) -> list[CredentialTypeDTO]:
    types = await list_credential_types_with_i18n(db, tenant_id=principal.tenant_id)
    return [
        CredentialTypeDTO(
            name=t.name,
            display_name=t.display_name,
            display_name_translations=translations_for(i18n, t.display_name),
            fields=[translate_field(f, i18n) for f in t.fields],
        )
        for t, i18n in types
    ]


def translate_field(field: SetupFieldSpec, i18n: CredentialTypeI18n) -> dict[str, Any]:
    out = field.model_dump(mode="json")
    for prop in ("label", "help", "placeholder"):
        value = out.get(prop)
        if isinstance(value, str) and value:
            resolved = translations_for(i18n, value)
            if resolved:
                out[f"{prop}_translations"] = resolved
    return out
