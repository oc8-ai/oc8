"""The Connections page's API (connections design).

Everybody manages their OWN connections (`connection:own`); holders of
`integration:manage` also see the organisation's accesses and everybody's
personal connections, and may revoke any of them. No response ever carries a
token, a secret ref or a secret value. A row the caller may not touch answers
404, never 403, so ids cannot be probed.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1.credentials import translate_field
from oc8.audit import append_event
from oc8.auth.principal import Principal
from oc8.authz.authority import authority_for_principal
from oc8.authz.permissions import CONNECTION_OWN, INTEGRATION, MANAGE, perm
from oc8.authz.scope import scope_for_principal
from oc8.connections.checks import classify_check_error
from oc8.connections.integrations import InstalledIntegration, Integration, installed_integrations
from oc8.connections.revoke import (
    delete_credential_with_secrets,
    delete_secret_refs,
    revoke_oauth_connection,
)
from oc8.credentials.registry import get_credential_type, list_credential_types_with_i18n
from oc8.credentials.service import (
    CredentialCheckFailed,
    CredentialFieldNotSet,
    CredentialInUse,
    CredentialNotFound,
    check_credential_values,
    collect_required_values,
    create_credential,
    delete_credential,
    get_credential,
    test_credential,
)
from oc8.schemas.dto import ConnectableIntegrationDTO, ConnectionDTO, CredentialCheckDTO
from oc8.schemas.requests import CheckConnectionCredentialRequest, CreateConnectionCredentialRequest

router = APIRouter()

_OWN = [Depends(require_permission(CONNECTION_OWN))]
_MANAGE = [Depends(require_permission(perm(INTEGRATION, MANAGE)))]


async def caller_member(db: AsyncSession, principal: Principal) -> m.OrgMember:
    try:
        member, _scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "this token does not belong to a person with an account"
        ) from exc
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no member record for this caller")
    return member


async def may_manage_integrations(request: Request, db: AsyncSession, principal: Principal) -> bool:
    authority = await authority_for_principal(request, db, principal)
    return perm(INTEGRATION, MANAGE) in authority.tenant_wide


async def _names(db: AsyncSession, ids: set[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    rows = (
        await db.execute(
            select(m.OrgMember.id, m.OrgMember.display_name, m.OrgMember.email).where(
                m.OrgMember.id.in_(wanted)
            )
        )
    ).all()
    return {row.id: (row.display_name or row.email or "") for row in rows}


def _owner(owner_id: uuid.UUID | None, caller_id: uuid.UUID) -> str:
    if owner_id is None:
        return "organisation"
    return "me" if owner_id == caller_id else "member"


def _oauth_dto(
    conn: m.OAuthConnection,
    caller_id: uuid.UUID,
    names: dict[uuid.UUID, str],
    by_provider: dict[str, str],
) -> ConnectionDTO:
    return ConnectionDTO(
        id=str(conn.id),
        kind="oauth",
        integration=by_provider.get(conn.provider),
        provider=conn.provider,
        account_label=conn.account_label,
        owner=_owner(conn.owner_member_id, caller_id),
        owner_member_id=str(conn.owner_member_id) if conn.owner_member_id else None,
        owner_name=names.get(conn.owner_member_id) if conn.owner_member_id else None,
        status=conn.status,
        grant_type=conn.grant_type,
        created_at=conn.created_at.isoformat(),
        created_by_name=names.get(conn.created_by_user_id) if conn.created_by_user_id else None,
    )


def credential_dto(
    cred: m.Credential,
    caller_id: uuid.UUID,
    names: dict[uuid.UUID, str],
    by_type: dict[str, str],
) -> ConnectionDTO:
    return ConnectionDTO(
        id=str(cred.id),
        kind="credential",
        integration=by_type.get(cred.credential_type),
        credential_type=cred.credential_type,
        account_label=cred.name,
        owner=_owner(cred.owner_member_id, caller_id),
        owner_member_id=str(cred.owner_member_id) if cred.owner_member_id else None,
        owner_name=names.get(cred.owner_member_id) if cred.owner_member_id else None,
        status="untested"
        if cred.last_test_ok is None
        else ("ok" if cred.last_test_ok else "failed"),
        created_at=cred.created_at.isoformat(),
        last_tested_at=cred.last_tested_at.isoformat() if cred.last_tested_at else None,
        last_test_ok=cred.last_test_ok,
    )


def _maps(items: list[InstalledIntegration]) -> tuple[dict[str, str], dict[str, str]]:
    by_provider = {
        i.integration.provider: i.integration.name for i in items if i.integration.provider
    }
    by_type = {
        i.integration.credential_type: i.integration.name
        for i in items
        if i.integration.credential_type
    }
    return by_provider, by_type


@router.get(
    "/connections/integrations", response_model=list[ConnectableIntegrationDTO], dependencies=_OWN
)
async def list_integrations(
    db: DbSession, principal: CurrentPrincipal
) -> list[ConnectableIntegrationDTO]:
    items = await installed_integrations(db)
    types = {
        t.name: (t, i18n)
        for t, i18n in await list_credential_types_with_i18n(db, tenant_id=principal.tenant_id)
    }
    out: list[ConnectableIntegrationDTO] = []
    for item in items:
        integ = item.integration
        fields: list[dict[str, object]] = []
        if integ.credential_type and integ.credential_type in types:
            spec, i18n = types[integ.credential_type]
            fields = [translate_field(f, i18n) for f in spec.fields]
        out.append(
            ConnectableIntegrationDTO(
                name=integ.name,
                label=integ.label,
                summary=integ.summary,
                tool_key=integ.tool_key,
                kind=integ.kind,
                provider=integ.provider,
                credential_type=integ.credential_type,
                credential_fields=fields,
                available=item.available,
                capa_id=str(item.capa_id),
                permissions=list(item.permissions),
            )
        )
    return out


@router.get("/connections/mine", response_model=list[ConnectionDTO], dependencies=_OWN)
async def list_mine(db: DbSession, principal: CurrentPrincipal) -> list[ConnectionDTO]:
    me = await caller_member(db, principal)
    by_provider, by_type = _maps(await installed_integrations(db))
    conns = (
        (
            await db.execute(
                select(m.OAuthConnection)
                .where(m.OAuthConnection.owner_member_id == me.id)
                .order_by(m.OAuthConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    creds = (
        (
            await db.execute(
                select(m.Credential)
                .where(m.Credential.owner_member_id == me.id)
                .order_by(m.Credential.created_at)
            )
        )
        .scalars()
        .all()
    )
    names = await _names(db, {me.id})
    return [_oauth_dto(c, me.id, names, by_provider) for c in conns] + [
        credential_dto(c, me.id, names, by_type) for c in creds
    ]


@router.get("/connections/organisation", response_model=list[ConnectionDTO], dependencies=_MANAGE)
async def list_organisation(db: DbSession, principal: CurrentPrincipal) -> list[ConnectionDTO]:
    me = await caller_member(db, principal)
    by_provider, by_type = _maps(await installed_integrations(db))
    conns = (
        (
            await db.execute(
                select(m.OAuthConnection)
                .where(
                    m.OAuthConnection.owner_member_id.is_(None),
                    # A model provider's device-code login is a model setting, not a connection.
                    m.OAuthConnection.grant_type != "device_code",
                )
                .order_by(m.OAuthConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    creds = (
        (
            await db.execute(
                select(m.Credential)
                .where(
                    m.Credential.owner_member_id.is_(None),
                    m.Credential.credential_type.in_(list(by_type)),
                )
                .order_by(m.Credential.created_at)
            )
        )
        .scalars()
        .all()
    )
    names = await _names(db, {c.created_by_user_id for c in conns})
    return [_oauth_dto(c, me.id, names, by_provider) for c in conns] + [
        credential_dto(c, me.id, names, by_type) for c in creds
    ]


@router.get("/connections/members", response_model=list[ConnectionDTO], dependencies=_MANAGE)
async def list_members(db: DbSession, principal: CurrentPrincipal) -> list[ConnectionDTO]:
    me = await caller_member(db, principal)
    by_provider, by_type = _maps(await installed_integrations(db))
    conns = (
        (
            await db.execute(
                select(m.OAuthConnection)
                .where(
                    m.OAuthConnection.owner_member_id.is_not(None),
                    m.OAuthConnection.owner_member_id != me.id,
                )
                .order_by(m.OAuthConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    creds = (
        (
            await db.execute(
                select(m.Credential)
                .where(
                    m.Credential.owner_member_id.is_not(None), m.Credential.owner_member_id != me.id
                )
                .order_by(m.Credential.created_at)
            )
        )
        .scalars()
        .all()
    )
    names = await _names(
        db, {c.owner_member_id for c in conns} | {c.owner_member_id for c in creds}
    )
    return [_oauth_dto(c, me.id, names, by_provider) for c in conns] + [
        credential_dto(c, me.id, names, by_type) for c in creds
    ]


async def _audit_revoked(
    db: AsyncSession, principal: Principal, *, kind: str, row_id: uuid.UUID, owner: uuid.UUID | None
) -> None:
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="connection",
        action="connection.revoked",
        resource={
            "kind": kind,
            "id": str(row_id),
            "owner_member_id": str(owner) if owner else None,
        },
        principal=principal,
    )


@router.delete(
    "/connections/oauth/{connection_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_OWN
)
async def delete_oauth(
    connection_id: uuid.UUID, request: Request, db: DbSession, principal: CurrentPrincipal
) -> None:
    me = await caller_member(db, principal)
    conn = await db.get(m.OAuthConnection, connection_id)
    if conn is None or conn.tenant_id != principal.tenant_id or conn.grant_type == "device_code":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    if conn.owner_member_id != me.id and not await may_manage_integrations(request, db, principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    owner = conn.owner_member_id
    await revoke_oauth_connection(db, conn)
    await _audit_revoked(db, principal, kind="oauth", row_id=connection_id, owner=owner)
    await db.commit()


@router.delete(
    "/connections/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_OWN,
)
async def delete_credential_connection(
    credential_id: uuid.UUID, request: Request, db: DbSession, principal: CurrentPrincipal
) -> None:
    me = await caller_member(db, principal)
    cred = await db.get(m.Credential, credential_id)
    if cred is None or cred.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found")
    if cred.owner_member_id != me.id and not await may_manage_integrations(request, db, principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found")
    owner = cred.owner_member_id
    if owner is None:
        # The organisation's access may still be wired into a setup form or a
        # login: the existing rule (409 while in use) applies unchanged.
        refs = dict(cred.secret_refs or {})
        try:
            await delete_credential(
                db, tenant_id=principal.tenant_id, credential_id=credential_id, owner="organisation"
            )
        except CredentialInUse as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await delete_secret_refs(db, tenant_id=principal.tenant_id, refs=refs.values())
    else:
        await delete_credential_with_secrets(db, cred)
    await _audit_revoked(db, principal, kind="credential", row_id=credential_id, owner=owner)
    await db.commit()


async def _credential_integration(db: AsyncSession, name: str) -> Integration:
    for item in await installed_integrations(db):
        if (
            item.integration.name == name
            and item.integration.kind == "credential"
            and item.available
        ):
            return item.integration
    raise HTTPException(status.HTTP_404_NOT_FOUND, "integration not available")


async def _free_name(
    db: AsyncSession, tenant_id: uuid.UUID, owner: uuid.UUID | None, base: str
) -> str:
    stmt = select(m.Credential.name).where(
        m.Credential.tenant_id == tenant_id,
        m.Credential.owner_member_id.is_(None)
        if owner is None
        else m.Credential.owner_member_id == owner,
    )
    taken = set((await db.execute(stmt)).scalars().all())
    if base not in taken:
        return base
    n = 2
    while f"{base} {n}" in taken:
        n += 1
    return f"{base} {n}"


def _check_result(exc: Exception | None) -> CredentialCheckDTO:
    if exc is None:
        return CredentialCheckDTO(ok=True)
    if isinstance(exc, CredentialCheckFailed):
        detail = exc.detail[:300] if exc.detail else None
        reason = exc.reason or classify_check_error(exc.internal)
        return CredentialCheckDTO(ok=False, reason=reason, detail=detail)
    return CredentialCheckDTO(ok=False, reason="invalid", detail=None)


@router.post("/connections/credentials/check", response_model=CredentialCheckDTO, dependencies=_OWN)
async def check_credential(
    body: CheckConnectionCredentialRequest, db: DbSession, principal: CurrentPrincipal
) -> CredentialCheckDTO:
    integration = await _credential_integration(db, body.integration)
    assert integration.credential_type is not None
    try:
        await check_credential_values(
            db,
            tenant_id=principal.tenant_id,
            credential_type=integration.credential_type,
            field_values=body.field_values,
        )
    except CredentialCheckFailed as exc:
        return _check_result(exc)
    return _check_result(None)


@router.post(
    "/connections/credentials",
    response_model=ConnectionDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=_OWN,
)
async def create_credential_connection(
    body: CreateConnectionCredentialRequest,
    request: Request,
    db: DbSession,
    principal: CurrentPrincipal,
) -> ConnectionDTO:
    me = await caller_member(db, principal)
    if body.owner == "organisation" and not await may_manage_integrations(request, db, principal):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requires permission: integration:manage")
    integration = await _credential_integration(db, body.integration)
    assert integration.credential_type is not None
    owner = None if body.owner == "organisation" else me.id
    cred_type = await get_credential_type(
        db, tenant_id=principal.tenant_id, name=integration.credential_type
    )
    try:
        collect_required_values(cred_type, body.field_values)
    except CredentialCheckFailed as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, exc.detail) from exc
    base = (body.name or "").strip() or integration.label
    cred = None
    for _attempt in range(5):
        name = await _free_name(db, principal.tenant_id, owner, base)
        try:
            # A savepoint: a name taken by a concurrent request since the
            # lookup must retry with the next suffix, not fail the request.
            async with db.begin_nested():
                cred = await create_credential(
                    db,
                    tenant_id=principal.tenant_id,
                    name=name,
                    credential_type=integration.credential_type,
                    field_values=body.field_values,
                    owner_member_id=owner,
                )
        except IntegrityError:
            continue
        break
    if cred is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "name already in use")
    # Records last_tested_at/last_test_ok; a failing login is still saved
    # (the person asked to save) and shows as "failed" with its own Test button.
    try:
        await test_credential(db, tenant_id=principal.tenant_id, credential_id=cred.id)
    except (CredentialCheckFailed, CredentialFieldNotSet):
        pass
    # Before the commit: the tenant binding is transaction-local, so a read
    # after it would see nothing under RLS.
    names = await _names(db, {me.id})
    dto = credential_dto(cred, me.id, names, {integration.credential_type: integration.name})
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="connection",
        action="connection.created",
        resource={
            "kind": "credential",
            "id": str(cred.id),
            "integration": integration.name,
            "owner_member_id": str(owner) if owner else None,
        },
        principal=principal,
    )
    await db.commit()
    return dto


@router.post(
    "/connections/credentials/{credential_id}/test",
    response_model=CredentialCheckDTO,
    dependencies=_OWN,
)
async def test_credential_connection(
    credential_id: uuid.UUID, request: Request, db: DbSession, principal: CurrentPrincipal
) -> CredentialCheckDTO:
    me = await caller_member(db, principal)
    try:
        cred = await get_credential(db, tenant_id=principal.tenant_id, credential_id=credential_id)
    except CredentialNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found") from exc
    if cred.owner_member_id != me.id and not await may_manage_integrations(request, db, principal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found")
    try:
        await test_credential(db, tenant_id=principal.tenant_id, credential_id=credential_id)
    except (CredentialCheckFailed, CredentialFieldNotSet) as exc:
        await db.commit()
        return _check_result(exc)
    await db.commit()
    return _check_result(None)
