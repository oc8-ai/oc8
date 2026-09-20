"""MCP connection management (create/list). Attaching an MCP server to a
department makes its tools available to that department's agents."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.mcp_client import open_tool_session, resolve_auth_header
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.agent.mcp_requirements import wrap_with_requirements
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import INTEGRATION, MANAGE, VIEW, perm
from oc8.capas.discovery import find_plugin
from oc8.capas.guardrails import GuardrailLibrary
from oc8.capas.i18n import translations_for
from oc8.capas.manifest import ManifestError, ToolPackConnection, parse_manifest
from oc8.schemas.dto import (
    ConnectionToolNamesDTO,
    GuardrailAdjustableDTO,
    GuardrailAttributeDTO,
    GuardrailDTO,
    GuardrailPresetDTO,
    McpConnectionDTO,
)
from oc8.schemas.requests import CreateMcpConnectionRequest, UpdateMcpConnectionRequest

router = APIRouter()

_TEST_TIMEOUT_S = 15.0


def _manifest_connection(
    c: m.McpConnection,
) -> tuple[ToolPackConnection, GuardrailLibrary | None, dict[str, dict[str, str]]] | None:
    """The manifest connection this row was materialised from, paired with its
    plugin's optional `guardrails/*.toml` library and its `i18n/*.po`
    catalogs, or None.

    `config` holds INSTANCE data (server URL, env, secret refs); the presets,
    `value_spec` and guardrail library are PLUGIN data that lives on disk,
    never copied onto the row. `materialise.py` stamps `_plugin_name`/
    `_connection_key` onto every row it creates -- the same pair
    `api/v1/capas.py`'s setup endpoint already matches on -- so this reuses
    that identifying pair rather than a second way of answering "which
    manifest connection is this row", which would eventually disagree with it.
    The guardrail library rides along on the same `find_plugin` lookup rather
    than a second one, since this is the only caller.

    A connection with no such stamp, whose plugin ships no tool pack, or whose
    plugin folder is no longer on disk returns None: an operator's connection
    list must not break because a plugin was removed.
    """
    cfg = c.config or {}
    plugin_name = cfg.get("_plugin_name")
    connection_key = cfg.get("_connection_key")
    if not isinstance(plugin_name, str) or not plugin_name:
        return None
    discovered = find_plugin(plugin_name)
    if discovered is None or not discovered.valid or discovered.manifest is None:
        return None
    try:
        manifest = parse_manifest(discovered.manifest)
    except ManifestError:
        return None
    if manifest.tool_pack is None:
        return None
    conn = next(
        (conn for conn in manifest.tool_pack.connections if conn.key == connection_key),
        None,
    )
    if conn is None:
        return None
    return conn, discovered.guardrail_library, discovered.i18n


async def _connection_env(db: DbSession, conn: m.McpConnection) -> dict[str, str]:
    """Build a subprocess environment without ever serialising secret values.

    ``secret_env`` maps an environment-variable name to a tenant Secret name (or
    to an ``oauth:`` ref minted fresh per launch -- see agent/mcp_env.py). It is
    intentionally resolved only immediately before launching the MCP subprocess,
    not when the connection is created or returned to the browser.
    """
    return await resolve_mcp_env(
        db, tenant_id=conn.tenant_id, cfg=conn.config or {}, connection_name=conn.name
    )


def _utcnow_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _to_dto(c: m.McpConnection) -> McpConnectionDTO:
    cfg = c.config or {}
    # Older seed data used a policy object here, before MCP scopes were
    # standardised as a list. A single legacy row must not make every MCP
    # connection unavailable to the UI.
    scopes = c.scopes if isinstance(c.scopes, list) else []
    resolved = _manifest_connection(c)
    manifest_conn, guardrail_library, i18n = (
        resolved if resolved is not None else (None, None, {})
    )
    presets = (
        [
            GuardrailPresetDTO(
                key=p.key,
                label=p.label,
                label_translations=translations_for(i18n, p.label),
                summary=p.summary,
                summary_translations=translations_for(i18n, p.summary),
                recommended=p.recommended,
                read=p.read,
                modify=p.modify,
                approval_actions=p.approval_actions,
                approval_eur=p.approval_eur,
                only=p.only,
            )
            for p in manifest_conn.guardrail_presets
        ]
        if manifest_conn is not None
        else []
    )
    library = (
        [
            GuardrailDTO(
                key=g.key,
                label=g.label,
                label_translations=translations_for(i18n, g.label),
                summary=g.summary,
                summary_translations=translations_for(i18n, g.summary),
                use_case=g.use_case,
                read=g.read,
                modify=g.modify,
                approval_eur=g.approval_eur,
                approval_actions=sorted(g.approval_actions),
                only=list(g.only),
                adjustable=[
                    GuardrailAdjustableDTO(
                        field=a.field,
                        label=a.label,
                        label_translations=translations_for(i18n, a.label),
                        unit=a.unit,
                        min=a.min,
                        max=a.max,
                    )
                    for a in g.adjustable
                ],
            )
            for g in guardrail_library.guardrail
        ]
        if guardrail_library is not None
        else None
    )
    attributes = (
        [
            GuardrailAttributeDTO(
                key=a.key,
                label=a.label,
                label_translations=translations_for(i18n, a.label),
                datatype=a.datatype,
                enum_values=a.enum_values,
                tools=a.tools,
            )
            for a in manifest_conn.guardrail_attributes
        ]
        if manifest_conn is not None
        else []
    )
    has_value_spec = manifest_conn is not None and "value_spec" in manifest_conn.config
    # The same `_plugin_name` stamp `_manifest_connection` above reads to find
    # the manifest connection -- read directly off `cfg` rather than through
    # that resolution, so a connection whose plugin was later removed from
    # disk (or ships no tool pack) still reports which plugin created it,
    # rather than silently falling back to None like the guardrail fields do.
    plugin_name = cfg.get("_plugin_name")
    return McpConnectionDTO(
        id=str(c.id),
        name=c.name,
        transport=c.transport,
        server_url=c.server_url,
        command=cfg.get("command", ""),
        args=cfg.get("args", []),
        department_id=str(c.department_id) if c.department_id else None,
        connected=c.connected,
        scopes=scopes,
        health=c.health or {},
        guardrail_presets=presets,
        guardrail_library=library,
        guardrail_attributes=attributes,
        has_value_spec=has_value_spec,
        plugin_name=plugin_name if isinstance(plugin_name, str) and plugin_name else None,
        credential_type=manifest_conn.credential_type or None if manifest_conn else None,
    )


@router.get(
    "/mcp/connections",
    response_model=list[McpConnectionDTO],
    dependencies=[Depends(require_permission(perm(INTEGRATION, VIEW)))],
)
async def list_connections(db: DbSession) -> list[McpConnectionDTO]:
    rows = (
        (await db.execute(select(m.McpConnection).order_by(m.McpConnection.created_at)))
        .scalars()
        .all()
    )
    return [_to_dto(c) for c in rows]


@router.get(
    "/mcp/connections/{name}/tool-names",
    response_model=ConnectionToolNamesDTO,
    dependencies=[Depends(require_permission(perm(INTEGRATION, VIEW)))],
)
async def get_connection_tool_names(name: str, db: DbSession) -> ConnectionToolNamesDTO:
    # `credential_id.is_(None)` picks the manifest row, never another login
    # sharing this same tenant-global name -- `POST /mcp/logins` creates a
    # SECOND `McpConnection` row with the same `name` (see its own docstring),
    # and a bare `.where(name == ...)` here would raise `MultipleResultsFound`
    # for any tenant that has pinned a login. Same pattern as `mcp_logins.py`'s
    # own `source_conn` lookup.
    conn = (
        await db.execute(
            select(m.McpConnection)
            .where(m.McpConnection.name == name, m.McpConnection.credential_id.is_(None))
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if conn is None:
        return ConnectionToolNamesDTO(names=[])
    resolved = _manifest_connection(conn)
    if resolved is None:
        return ConnectionToolNamesDTO(names=[])
    manifest_conn, _guardrail_library, _i18n = resolved
    scopes = manifest_conn.scopes if isinstance(manifest_conn.scopes, dict) else {}
    read = sorted(scopes.get("read", []))
    modify = sorted(scopes.get("modify", []))
    names = sorted({*read, *modify})
    return ConnectionToolNamesDTO(names=names, read=read, modify=modify)


@router.post(
    "/mcp/connections",
    response_model=McpConnectionDTO,
    status_code=201,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def create_connection(
    body: CreateMcpConnectionRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> McpConnectionDTO:
    conn = m.McpConnection(
        id=uuid.uuid4(),
        tenant_id=principal.tenant_id,
        department_id=body.department_id,
        name=body.name,
        transport=body.transport,
        server_url=body.server_url,
        scopes=body.scopes,
        config={
            "command": body.command,
            "args": body.args,
            "env": body.env,
            "secret_env": body.secret_env,
        },
        connected=False,
    )
    db.add(conn)
    await db.flush()
    return _to_dto(conn)


@router.patch(
    "/mcp/connections/{conn_id}",
    response_model=McpConnectionDTO,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def update_connection(
    conn_id: uuid.UUID,
    body: UpdateMcpConnectionRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> McpConnectionDTO:
    conn = await db.get(m.McpConnection, conn_id)
    if conn is None or conn.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    if body.name is not None:
        conn.name = body.name
    if body.department_id is not None:
        conn.department_id = body.department_id
    if body.scopes is not None:
        conn.scopes = body.scopes
    cfg = dict(conn.config or {})
    if body.command is not None:
        cfg["command"] = body.command
    if body.args is not None:
        cfg["args"] = body.args
    if body.env is not None:
        cfg["env"] = body.env
    if body.secret_env is not None:
        cfg["secret_env"] = body.secret_env
    conn.config = cfg
    # A changed command/config invalidates the previous health assertion.
    conn.connected = False
    conn.health = {}
    # Flush, don't commit: the RLS GUC is transaction-local, so committing here
    # leaves the session unbound and the reload that followed ran with
    # app.tenant_id = '' and failed the uuid cast -- a 500 for a change that had
    # already been written. `tenant_session` commits when the request ends.
    await db.flush()
    return _to_dto(conn)


@router.delete(
    "/mcp/connections/{conn_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def delete_connection(
    conn_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> None:
    """Drop one connection row -- e.g. a stale duplicate left behind by a
    failed setup attempt (`configure_plugin`, api/v1/capas.py, makes a new
    row per distinct department target rather than overwriting one that
    already exists). Safe to delete outright: an Agent/Department narrowing
    references a connection by NAME, not by this row's id (capas/export.py's
    own docstring confirms the convention), so nothing holds a foreign key to
    it -- at worst a stale name reference resolves to "no connection", the
    same already-handled case as a name that was never configured."""
    conn = await db.get(m.McpConnection, conn_id)
    if conn is None or conn.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    await db.delete(conn)
    await db.commit()


@router.post(
    "/mcp/connections/{conn_id}/test",
    response_model=McpConnectionDTO,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def test_connection(
    conn_id: uuid.UUID,
    db: DbSession,
    principal: CurrentPrincipal,
) -> McpConnectionDTO:
    conn = await db.get(m.McpConnection, conn_id)
    if conn is None or conn.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    cfg = conn.config or {}
    now = _utcnow_iso()
    try:
        async with asyncio.timeout(_TEST_TIMEOUT_S):
            env = await _connection_env(db, conn)
            headers = resolve_auth_header(cfg, env)
            if conn.transport == "manual_http":
                http_tools = list(cfg.get("http_tools", []))
                if not http_tools:
                    raise ValueError("connection has no described HTTP tools")
                tool_session = await open_tool_session(
                    transport="manual_http",
                    server_url=conn.server_url,
                    http_tools=http_tools,
                    headers=headers,
                )
            else:
                command, args = wrap_with_requirements(
                    cfg.get("command", ""), cfg.get("args", []), cfg
                )
                tool_session = await open_tool_session(
                    transport=conn.transport,
                    command=command,
                    args=args,
                    server_url=conn.server_url,
                    headers=headers,
                    env=env,
                )
            async with tool_session as session:
                names = [t.name for t in session.tools]
        conn.connected = True
        conn.health = {
            "status": "ok",
            "checkedAt": now,
            "toolCount": len(names),
            "tools": names,
        }
    except Exception as exc:  # any bring-up failure is an operator-visible error health, not a 500
        conn.connected = False
        conn.health = {"status": "error", "checkedAt": now, "error": str(exc)[:500]}
    await db.commit()
    return _to_dto(conn)
