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
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1.credentials import translate_field
from oc8.audit import append_event
from oc8.auth.principal import Principal
from oc8.authz.authority import authority_for_principal
from oc8.authz.permissions import CONNECTION_OWN, INTEGRATION, MANAGE, perm
from oc8.authz.scope import scope_for_principal
from oc8.connections.integrations import InstalledIntegration, installed_integrations
from oc8.connections.revoke import delete_credential_with_secrets, revoke_oauth_connection
from oc8.credentials.registry import list_credential_types_with_i18n
from oc8.credentials.service import CredentialInUse, delete_credential
from oc8.schemas.dto import ConnectableIntegrationDTO, ConnectionDTO
from oc8.secrets.service import SecretNotFound, delete_secret

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
        for ref in refs.values():
            try:
                await delete_secret(db, tenant_id=principal.tenant_id, ref=ref)
            except SecretNotFound:
                continue
    else:
        await delete_credential_with_secrets(db, cred)
    await _audit_revoked(db, principal, kind="credential", row_id=credential_id, owner=owner)
    await db.commit()
