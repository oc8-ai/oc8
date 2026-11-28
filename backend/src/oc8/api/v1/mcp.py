"""MCP connection management (create/list). Attaching an MCP server to a
department makes its tools available to that department's agents."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agent import mcp_pool
from oc8.agent.http_tools import HttpToolError, HttpToolSession, resolve_http_login
from oc8.agent.mcp_client import open_tool_session, resolve_auth_header
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.agent.mcp_requirements import wrap_with_requirements
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import INTEGRATION, MANAGE, PLUGIN, VIEW, perm
from oc8.capas.discovery import find_plugin
from oc8.capas.guardrails import GuardrailLibrary
from oc8.capas.i18n import translations_for
from oc8.capas.manifest import ManifestError, ToolPackConnection, parse_manifest
from oc8.capas.service import _artifact_hash
from oc8.connections.checks import classify_check_error
from oc8.connections.credential_env import with_login
from oc8.connections.integrations import integration_for_connection
from oc8.connections.login import holds_copied_login
from oc8.connections.tool_connection import not_retired
from oc8.connections.tool_scopes import classified, known_tool_names, row_tool_scopes
from oc8.connections.usable import needs_department
from oc8.credentials.errors import CredentialCheckFailed
from oc8.credentials.registry import get_credential_type
from oc8.credentials.service import (
    CredentialFieldNotSet,
    CredentialNotFound,
    IncompleteCredential,
    get_credential,
    missing_required,
    test_credential,
)
from oc8.net.outbound import LOGIN_IN_BASE_URL, base_url_has_login
from oc8.realtime.emit import publish_mcp_test_log
from oc8.schemas.dto import (
    ConnectionToolLabelsDTO,
    ConnectionToolNamesDTO,
    ConnectionToolScopesDTO,
    GuardrailAdjustableDTO,
    GuardrailAttributeDTO,
    GuardrailDTO,
    GuardrailPresetDTO,
    HttpLoginShapeDTO,
    HttpTestCallResultDTO,
    HttpToolDTO,
    HttpToolScopesDTO,
    HttpToolsSnapshotDTO,
    ImportMetadataDTO,
    McpConnectionDTO,
    ModelLabelDTO,
    ToolLabelDTO,
    UsageLimitDTO,
)
from oc8.schemas.requests import (
    CreateMcpConnectionRequest,
    HttpTestCallRequest,
    UpdateHttpToolsRequest,
    UpdateMcpConnectionRequest,
    UpdateToolScopesRequest,
)

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
    manifest_conn, guardrail_library, i18n = resolved if resolved is not None else (None, None, {})
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
                limits=[UsageLimitDTO(**lim.to_json()) for lim in p.limits],
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
                limits=[UsageLimitDTO(**lim.to_json()) for lim in g.limits],
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
    # The same seam the risk tier reads (b_risk_tier): the tools that reach
    # people outside, which ask even an autonomous agent without a grant.
    raw_outward = cfg.get("outward_tools")
    if not isinstance(raw_outward, list) and manifest_conn is not None:
        raw_outward = manifest_conn.config.get("outward_tools")
    outward_tools = [str(t) for t in raw_outward] if isinstance(raw_outward, list) else []
    return McpConnectionDTO(
        id=str(c.id),
        name=c.name,
        transport=c.transport,
        server_url=c.server_url,
        command=cfg.get("command", ""),
        args=cfg.get("args", []),
        department_id=str(c.department_id) if c.department_id else None,
        needs_department=needs_department(c),
        connected=c.connected,
        scopes=scopes,
        health=c.health or {},
        guardrail_presets=presets,
        guardrail_library=library,
        guardrail_attributes=attributes,
        has_value_spec=has_value_spec,
        outward_tools=outward_tools,
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
        (
            await db.execute(
                select(m.McpConnection).where(not_retired()).order_by(m.McpConnection.created_at)
            )
        )
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
            .where(
                m.McpConnection.name == name,
                m.McpConnection.credential_id.is_(None),
                not_retired(),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if conn is None:
        return ConnectionToolNamesDTO(names=[])
    resolved = _manifest_connection(conn)
    if resolved is None or not isinstance(resolved[0].scopes, dict):
        # No shipped classification: the one a person gave the row, over the
        # tools its server offered (`connections.tool_scopes`).
        own = row_tool_scopes(conn) or {"read": [], "modify": []}
        return ConnectionToolNamesDTO(
            names=known_tool_names(conn), read=own["read"], modify=own["modify"]
        )
    manifest_conn, _guardrail_library, _i18n = resolved
    scopes = manifest_conn.scopes if isinstance(manifest_conn.scopes, dict) else {}
    read = sorted(scopes.get("read", []))
    modify = sorted(scopes.get("modify", []))
    names = sorted({*read, *modify})
    return ConnectionToolNamesDTO(names=names, read=read, modify=modify)


@router.get(
    "/mcp/connections/{name}/tool-labels",
    response_model=ConnectionToolLabelsDTO,
    dependencies=[Depends(require_permission(perm(INTEGRATION, VIEW)))],
)
async def get_connection_tool_labels(name: str, db: DbSession) -> ConnectionToolLabelsDTO:
    """How this connection's calls read to a person (see `ToolLabel`).

    Keyed by connection NAME, not id, for one reason: that is what a run's
    own call records carry (`toolCalls[].connection`, written by both
    runtimes), so a timeline holding a call can ask about it directly. The
    department frame keys its tool policies by the same name.

    An unknown name, a connection whose plugin ships no tool pack, or a
    plugin no longer on disk all return an EMPTY catalogue rather than 404:
    the caller's fallback for "no label" is already the right-derived
    sentence, and a 404 would turn a removed plugin into a red error on a
    screen whose job is to describe what already happened.

    Same `credential_id.is_(None)` filter as the sibling tool-names route --
    `POST /mcp/logins` creates a SECOND row with the same name, and a bare
    name match would raise MultipleResultsFound for any tenant that pinned a
    login.
    """
    conn = (
        await db.execute(
            select(m.McpConnection)
            .where(
                m.McpConnection.name == name,
                m.McpConnection.credential_id.is_(None),
                not_retired(),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if conn is None:
        return ConnectionToolLabelsDTO(connection=name)
    resolved = _manifest_connection(conn)
    if resolved is None:
        return ConnectionToolLabelsDTO(connection=name)
    manifest_conn, _guardrail_library, i18n = resolved
    scopes = manifest_conn.scopes if isinstance(manifest_conn.scopes, dict) else {}
    return ConnectionToolLabelsDTO(
        connection=name,
        labels=[
            ToolLabelDTO(
                tool=tool,
                verb=label.verb,
                object=label.object,
                running=label.running,
                verb_translations=translations_for(i18n, label.verb),
                object_translations=translations_for(i18n, label.object) if label.object else {},
                running_translations=(
                    translations_for(i18n, label.running) if label.running else {}
                ),
            )
            for tool, label in sorted(manifest_conn.tool_labels.items())
        ],
        model_labels=[
            ModelLabelDTO(
                key=key,
                label=label,
                label_translations=translations_for(i18n, label),
            )
            for key, label in sorted(manifest_conn.model_labels.items())
        ],
        read=sorted(scopes.get("read", [])),
        modify=sorted(scopes.get("modify", [])),
    )


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
    # Checked here, not on the request model: a validation error echoes the
    # input, and that input is the address with the password in it.
    if body.transport == "manual_http" and base_url_has_login(body.server_url):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, LOGIN_IN_BASE_URL)
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
        # The foreign key alone would accept another organisation's department
        # (RLS does not apply to it); the RLS-bound read below does not.
        dept = await db.get(m.Department, body.department_id)
        if dept is None or dept.deleted_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")
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
    # A changed command/config invalidates the previous health assertion.
    # Moving it to a department does not: it runs the same server either way.
    if body.model_fields_set - {"department_id"}:
        conn.connected = False
        conn.health = {}
    conn.config = cfg
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
    credential_id: uuid.UUID | None = Query(default=None, alias="credentialId"),
) -> McpConnectionDTO:
    """Start the connection's bridge once and list its tools. `credentialId`
    tests the tool's row with that organisation login -- the login a setup
    just chose (the row holds none of its values). Without it, a tool that
    takes a login is tested with the organisation login it would use when
    there is exactly one (the one enabled for departments, else the only
    one); with several the caller is asked which (422 `login_required`). A
    tool row is never tested without a login. A test with a login only marks
    the shared row connected: a failure is the login's, so the row keeps its
    state for every department and only the answer carries the error."""
    conn = await db.get(m.McpConnection, conn_id)
    if conn is None or conn.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    cfg = conn.config or {}
    if credential_id is None:
        credential_id = await _default_test_login(db, conn)
    if credential_id is not None:
        cfg = await _with_test_login(db, principal.tenant_id, conn, credential_id)
    now = _utcnow_iso()
    if credential_id is not None:
        # The login's own check first -- the one Connections -> Test runs. A
        # bridge comes up whatever the login holds, so without it a wrong
        # password passed here and failed there (E2E F8).
        refused = await _login_check_failure(db, principal.tenant_id, credential_id)
        if refused is not None:
            await publish_mcp_test_log(
                principal.tenant_id,
                connection_id=conn_id,
                step="result",
                message=f"Error: {refused}",
            )
            await db.commit()  # keeps the login's recorded test result
            return _to_dto(conn).model_copy(
                update={
                    "connected": False,
                    "health": {"status": "error", "checkedAt": now, "error": refused},
                }
            )

    _STEP_MESSAGES = {
        "spawn": "Starting session…",
        "handshake": "Handshake complete.",
        "list_tools": "Tool list received.",
    }

    async def on_step(step: str) -> None:
        await publish_mcp_test_log(
            principal.tenant_id, connection_id=conn_id, step=step, message=_STEP_MESSAGES[step]
        )

    try:
        async with asyncio.timeout(_TEST_TIMEOUT_S):
            env = await resolve_mcp_env(
                db, tenant_id=conn.tenant_id, cfg=cfg, connection_name=conn.name
            )
            headers = resolve_auth_header(cfg, env)
            if conn.transport == "manual_http":
                http_tools = list(cfg.get("http_tools", []))
                if not http_tools:
                    raise ValueError("connection has no described HTTP tools")
                tool_session = await open_tool_session(
                    transport="manual_http",
                    server_url=conn.server_url,
                    http_tools=http_tools,
                    login=resolve_http_login(cfg, env),
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
                    on_step=on_step,
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
        await publish_mcp_test_log(
            principal.tenant_id,
            connection_id=conn_id,
            step="result",
            message=f"Connected — {len(names)} tool(s) discovered.",
        )
    except Exception as exc:  # any bring-up failure is an operator-visible error health, not a 500
        error = str(exc)[:500]
        failed = {"status": "error", "checkedAt": now, "error": error}
        await publish_mcp_test_log(
            principal.tenant_id, connection_id=conn_id, step="result", message=f"Error: {error}"
        )
        if credential_id is not None:
            await db.commit()
            return _to_dto(conn).model_copy(update={"connected": False, "health": failed})
        conn.connected = False
        conn.health = failed
    await db.commit()
    return _to_dto(conn)


_CHECK_REASONS = {
    "auth": "The login was refused.",
    "unreachable": "The service could not be reached with this login.",
    "invalid": "The login is not valid.",
}


async def _login_check_failure(
    db: DbSession, tenant_id: uuid.UUID, credential_id: uuid.UUID
) -> str | None:
    """Why the login type's check refused `credential_id`, or None when it
    passed (or the type declares none). Records the result on the login, as
    Connections -> Test does. Only text a validator worded for people is
    shown; anything else is named by its class of failure."""
    try:
        await test_credential(
            db, tenant_id=tenant_id, credential_id=credential_id, owner="organisation"
        )
    except CredentialFieldNotSet as exc:
        return f"The login has no value for {exc.field_key!r}. Complete it first."
    except CredentialCheckFailed as exc:
        if exc.detail:
            return exc.detail[:300]
        reason = exc.reason or classify_check_error(exc.internal)
        return _CHECK_REASONS.get(reason, _CHECK_REASONS["invalid"])
    return None


async def _default_test_login(db: DbSession, conn: m.McpConnection) -> uuid.UUID | None:
    """The organisation login a test of `conn` runs with when none was named:
    None for a row that is no login tool's shared row (a custom server, a
    sign-in tool, an older row that still holds its own login); the only
    login enabled for departments, else the only organisation login; 422
    when the caller has to choose."""
    integration = integration_for_connection(conn)
    if integration is None or integration.kind != "credential":
        return None
    if conn.credential_id is not None or holds_copied_login(conn, integration):
        return None
    creds = (
        (
            await db.execute(
                select(m.Credential)
                .where(
                    m.Credential.credential_type == integration.credential_type,
                    m.Credential.owner_member_id.is_(None),
                )
                .order_by(m.Credential.created_at, m.Credential.id)
            )
        )
        .scalars()
        .all()
    )
    enabled_ids = set(
        (
            await db.execute(
                select(m.CredentialDepartment.credential_id).where(
                    m.CredentialDepartment.credential_id.in_([c.id for c in creds])
                )
            )
        )
        .scalars()
        .all()
    )
    enabled = [c for c in creds if c.all_departments or c.id in enabled_ids]
    pool = enabled or list(creds)
    if len(pool) == 1:
        return pool[0].id
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        {
            "code": "login_required",
            "message": "this tool takes a login: choose which one to test"
            if pool
            else "this tool takes a login and none is set up yet",
        },
    )


async def _with_test_login(
    db: DbSession, tenant_id: uuid.UUID, conn: m.McpConnection, credential_id: uuid.UUID
) -> dict[str, Any]:
    """The row's config with the organisation login `credential_id` in place
    of any login values it holds -- as a run that resolved this login would
    start the bridge. An incomplete login is refused naming its fields."""
    integration = integration_for_connection(conn)
    if integration is None or integration.kind != "credential":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "this connection's tool takes no login"
        )
    try:
        cred = await get_credential(
            db, tenant_id=tenant_id, credential_id=credential_id, owner="organisation"
        )
    except CredentialNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential") from exc
    if cred.credential_type != integration.credential_type:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "not a login of this connection's tool"
        )
    cred_type = await get_credential_type(db, tenant_id=tenant_id, name=cred.credential_type)
    missing = missing_required(cred_type, cred.field_values or {}, cred.secret_refs or {})
    if missing:
        raise IncompleteCredential(missing)
    return with_login(dict(conn.config or {}), cred, cred_type, integration.credential_env_fields)


@router.post(
    "/mcp/connections/{conn_id}/test-call",
    response_model=HttpTestCallResultDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def run_test_call(
    conn_id: uuid.UUID,
    body: HttpTestCallRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> HttpTestCallResultDTO:
    """One real call of one declared tool, for the person setting an HTTP API
    connection up. Nothing is stored. The answer comes back as an excerpt with
    the login's secrets removed, and a failure names its class: the login was
    refused (auth), no token could be fetched (token), the address is not
    allowed (blocked), nothing answered (unreachable), or the API answered
    with an error (http). plugin:manage, like editing the tools."""
    conn = await db.get(m.McpConnection, conn_id)
    if conn is None or conn.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    if conn.transport != "manual_http":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "only an HTTP API connection has declared tools to call"
        )
    cfg = conn.config or {}
    http_tools = [t for t in cfg.get("http_tools", []) if isinstance(t, dict) and t.get("name")]
    if body.tool not in {str(t["name"]) for t in http_tools}:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "tool not found")
    env = await _connection_env(db, conn)
    try:
        session = HttpToolSession(
            conn.server_url,
            http_tools,
            headers=resolve_auth_header(cfg, env),
            login=resolve_http_login(cfg, env),
            timeout_s=_TEST_TIMEOUT_S,
        )
    except HttpToolError as exc:  # a stored base URL the session refuses
        return HttpTestCallResultDTO(ok=False, error_class=exc.kind, message=str(exc))
    # Everything the call needs is loaded: end the transaction now, so its
    # pooled connection is not held for as long as the API takes to answer.
    await db.commit()
    try:
        async with session:
            outcome = await session.try_call(body.tool, body.arguments)
    except RuntimeError as exc:  # a missing path parameter: the caller's arguments
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return HttpTestCallResultDTO(**dataclasses.asdict(outcome))


_IMPORT_METADATA_KEYS = ("source_url", "fingerprint", "imported_at", "title", "version")
_LOGIN_SHAPE_KEYS = ("in", "name", "username", "token_url", "client_id", "scope")


def _text(value: object) -> str | None:
    """A stored scalar as text; anything else (a list, a dict, None) as None.
    Rows and custom manifests are written by hand too, so the read path never
    trusts their types."""
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_text(x) for x in value) if text is not None]


def _http_tools_snapshot(conn: m.McpConnection) -> HttpToolsSnapshotDTO:
    cfg = conn.config or {}
    tools: list[HttpToolDTO] = []
    raw_tools = cfg.get("http_tools", [])
    for raw in raw_tools if isinstance(raw_tools, list) else []:
        if not isinstance(raw, dict):
            continue
        schema = raw.get("param_schema")
        locations = raw.get("param_locations")
        body_param = raw.get("body_param")
        tools.append(
            HttpToolDTO(
                name=str(raw.get("name", "")),
                description=str(raw.get("description", "")),
                method=str(raw.get("method", "GET")).upper(),
                url_template=str(raw.get("url_template", "")),
                param_schema=schema if isinstance(schema, dict) else {},
                param_locations=(
                    {str(k): str(v) for k, v in locations.items()}
                    if isinstance(locations, dict)
                    else {}
                ),
                body_param=body_param if isinstance(body_param, str) else None,
                group=str(raw.get("group", "")),
            )
        )
    scopes = conn.scopes if isinstance(conn.scopes, dict) else {}
    auth = cfg.get("auth")
    login: HttpLoginShapeDTO | None = None
    if isinstance(auth, dict) and _text(auth.get("kind")):
        shape = {key: _text(auth.get(key)) for key in _LOGIN_SHAPE_KEYS}
        login = HttpLoginShapeDTO(
            kind=str(auth["kind"]),
            location=shape["in"],
            name=shape["name"],
            username=shape["username"],
            token_url=shape["token_url"],
            client_id=shape["client_id"],
            scope=shape["scope"],
        )
    elif cfg.get("auth_header_name"):
        # A connection from before logins existed: its one header is an API
        # key sent in a header, which is exactly what it does at runtime.
        login = HttpLoginShapeDTO(
            kind="api_key", location="header", name=str(cfg["auth_header_name"])
        )
    meta = cfg.get("openapi")
    openapi: ImportMetadataDTO | None = None
    if isinstance(meta, dict):
        found = {key: _text(meta.get(key)) for key in _IMPORT_METADATA_KEYS}
        openapi = ImportMetadataDTO(
            source_url=found["source_url"],
            fingerprint=found["fingerprint"] or "",
            imported_at=found["imported_at"] or "",
            title=found["title"] or "",
            version=found["version"] or "",
        )
    return HttpToolsSnapshotDTO(
        connection_id=str(conn.id),
        name=conn.name,
        base_url=conn.server_url,
        http_tools=tools,
        scopes=HttpToolScopesDTO(
            read=_names(scopes.get("read")), modify=_names(scopes.get("modify"))
        ),
        auth=login,
        openapi=openapi,
    )


async def _http_api_connection(
    db: DbSession, conn_id: uuid.UUID, principal: CurrentPrincipal
) -> m.McpConnection:
    conn = await db.get(m.McpConnection, conn_id)
    if conn is None or conn.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    if conn.transport != "manual_http":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "only an HTTP API connection has declared tools"
        )
    return conn


@router.get(
    "/mcp/connections/{conn_id}/http-tools",
    response_model=HttpToolsSnapshotDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def get_http_tools(
    conn_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> HttpToolsSnapshotDTO:
    """An HTTP API connection's endpoints, classification, login (never its
    secret) and import metadata, for the endpoints editor."""
    return _http_tools_snapshot(await _http_api_connection(db, conn_id, principal))


@router.patch(
    "/mcp/connections/{conn_id}/http-tools",
    response_model=HttpToolsSnapshotDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def update_http_tools(
    conn_id: uuid.UUID,
    body: UpdateHttpToolsRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> HttpToolsSnapshotDTO:
    """Replace the tools, their read/modify classification and the import
    metadata.

    Written to every row of the same plugin connection (one per department)
    AND to the custom capa's current manifest: enabling a plugin rewrites each
    row's `scopes` from the manifest (materialise._refresh_declared_seams) and
    a further department's row is seeded from the manifest's config, so an
    edit kept only on the row would silently revert. A plugin that is not a
    custom capa owns its tools: 409. So is a capa whose current manifest no
    longer has this connection: the rows alone would revert on the next enable.

    Locks, in this order: the capa row (the one mutex for all of a plugin's
    connections, and the order the Copilot's apply of an enable or setup
    takes: capa, then its rows), then only this plugin connection's rows,
    by id. An unstamped row has no capa and is locked alone. So an install of
    a new version, an enable or another edit of the same plugin waits instead
    of writing past this one, and unrelated connections are never held.
    """
    conn = await _http_api_connection(db, conn_id, principal)
    cfg = conn.config or {}
    tools = [
        {
            "name": t.name,
            "description": t.description,
            "method": t.method,
            "url_template": t.url_template,
            "param_schema": t.param_schema,
            "param_locations": dict(t.param_locations),
            "body_param": t.body_param,
            **({"group": t.group} if t.group else {}),
        }
        for t in body.http_tools
    ]
    scopes = {
        "read": sorted(set(body.scopes.read)),
        "modify": sorted(set(body.scopes.modify)),
    }
    replace_meta = "openapi" in body.model_fields_set
    meta = body.openapi.model_dump(mode="json") if body.openapi is not None else None

    capa, rows = await _lock_plugin_connection(db, conn, principal)
    if capa is not None:
        if capa.origin != "custom":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this connection's tools come from its plugin and cannot be edited here",
            )
        await _write_custom_manifest(
            db,
            capa,
            cfg.get("_connection_key", "default"),
            scopes,
            tools=tools,
            meta=meta,
            replace_meta=replace_meta,
        )

    names = [t["name"] for t in tools]
    for row in rows:
        row_cfg = dict(row.config or {})
        row_cfg["http_tools"] = tools
        if replace_meta:
            if meta is None:
                row_cfg.pop("openapi", None)
            else:
                row_cfg["openapi"] = meta
        row.config = row_cfg
        row.scopes = scopes
        health = dict(row.health or {})
        if health.get("status") == "ok":
            health["tools"] = names
            health["toolCount"] = len(names)
            row.health = health
    snapshot = _http_tools_snapshot(conn)
    row_ids = [row.id for row in rows]
    # Committed BEFORE the pooled sessions are closed: one reopened in between
    # would be built from the old committed tools and serve them until it ages
    # out. The pool is per process, so another replica keeps its session (and
    # the old tool list) until mcp_pool.MAX_AGE; nothing invalidates it sooner.
    await db.commit()
    for row_id in row_ids:
        await mcp_pool.close(row_id)
    return snapshot




async def _tool_scopes_connection(
    db: DbSession, conn_id: uuid.UUID, principal: CurrentPrincipal
) -> m.McpConnection:
    conn = await db.get(m.McpConnection, conn_id)
    if conn is None or conn.tenant_id != principal.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "connection not found")
    return conn


async def _plugin_classifies(db: DbSession, conn: m.McpConnection) -> bool:
    """Whether the connection's tools are classified by someone else: an HTTP
    API's endpoints editor, or a plugin that is not the tenant's own."""
    if conn.transport == "manual_http":
        return True
    resolved = _manifest_connection(conn)
    if resolved is not None and isinstance(resolved[0].scopes, dict):
        return True
    plugin_name = (conn.config or {}).get("_plugin_name")
    if isinstance(plugin_name, str) and plugin_name:
        capa = (
            await db.execute(
                select(m.Capa).where(
                    m.Capa.tenant_id == conn.tenant_id, m.Capa.name == plugin_name
                )
            )
        ).scalar_one_or_none()
        if capa is not None and capa.origin != "custom":
            return True
    return False


def _tool_scopes_snapshot(conn: m.McpConnection, *, editable: bool) -> ConnectionToolScopesDTO:
    own = row_tool_scopes(conn) or {"read": [], "modify": []}
    return ConnectionToolScopesDTO(
        connection_id=str(conn.id),
        name=conn.name,
        tools=known_tool_names(conn),
        read=own["read"],
        modify=own["modify"],
        editable=editable,
    )


@router.get(
    "/mcp/connections/{conn_id}/tool-scopes",
    response_model=ConnectionToolScopesDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def get_tool_scopes(
    conn_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> ConnectionToolScopesDTO:
    """A connection's tools and which of them only read, for the classification
    editor. The tools are the ones its server offered at the last successful
    connection test."""
    conn = await _tool_scopes_connection(db, conn_id, principal)
    return _tool_scopes_snapshot(conn, editable=not await _plugin_classifies(db, conn))


@router.put(
    "/mcp/connections/{conn_id}/tool-scopes",
    response_model=ConnectionToolScopesDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def update_tool_scopes(
    conn_id: uuid.UUID,
    body: UpdateToolScopesRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> ConnectionToolScopesDTO:
    """Classify a server's tools as read or modify. A tool left out, or one the
    server offers later, still requires modify.

    Written to every row of the same plugin connection (one per department)
    and, for the tenant's own plugin, to its current manifest: an enable
    rewrites each row from it and a further department is seeded from it, so
    an edit kept only on one row would revert. Same locks, in the same order,
    as the HTTP API endpoints editor (`_lock_plugin_connection`).
    """
    conn = await _tool_scopes_connection(db, conn_id, principal)
    if await _plugin_classifies(db, conn):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this connection's tools are classified by its plugin or its endpoints editor",
        )
    known = set(known_tool_names(conn))
    unknown = sorted({*body.read, *body.modify} - known)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"not a tool of this connection: {', '.join(unknown[:5])}",
        )
    scopes = classified(body.read, body.modify)

    capa, rows = await _lock_plugin_connection(db, conn, principal)
    if capa is not None:
        if capa.origin != "custom":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "this connection's tools are classified by its plugin or its endpoints editor",
            )
        await _write_custom_manifest(
            db, capa, (conn.config or {}).get("_connection_key", "default"), scopes
        )
    for row in rows:
        row.scopes = scopes
    snapshot = _tool_scopes_snapshot(conn, editable=True)
    await db.commit()
    return snapshot

async def _lock_plugin_connection(
    db: DbSession, conn: m.McpConnection, principal: CurrentPrincipal
) -> tuple[m.Capa | None, list[m.McpConnection]]:
    """Lock, in this order, the capa row and then every row of the same plugin
    connection (one per department), by id; an unstamped row is locked alone.

    The capa row is the one mutex for all of a plugin's connections and the
    order the Copilot's apply of an enable or setup takes (capa, then its
    rows), so an install of a new version, an enable or another edit of the
    same plugin waits instead of writing past this one, and unrelated
    connections are never held. 409 when the connection moved to another
    plugin connection, or was deleted, between the read and the lock.
    """
    cfg = conn.config or {}
    plugin_name = cfg.get("_plugin_name")
    connection_key = cfg.get("_connection_key", "default")
    if isinstance(plugin_name, str) and plugin_name:
        capa = (
            await db.execute(
                select(m.Capa)
                .where(m.Capa.tenant_id == principal.tenant_id, m.Capa.name == plugin_name)
                .with_for_update()
            )
        ).scalar_one_or_none()
        rows = list(
            (
                await db.execute(
                    select(m.McpConnection)
                    .where(
                        m.McpConnection.tenant_id == principal.tenant_id,
                        m.McpConnection.config["_plugin_name"].astext == plugin_name,
                        func.coalesce(m.McpConnection.config["_connection_key"].astext, "default")
                        == connection_key,
                    )
                    .order_by(m.McpConnection.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        if conn not in rows:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "this connection changed meanwhile; load it again"
            )
        return capa, rows
    locked = (
        await db.execute(
            select(m.McpConnection)
            .where(m.McpConnection.id == conn.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked is None or (locked.config or {}).get("_plugin_name"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "this connection changed meanwhile; load it again"
        )
    return None, [locked]

async def _write_custom_manifest(
    db: DbSession,
    capa: m.Capa,
    connection_key: str,
    scopes: dict[str, list[str]],
    *,
    tools: list[dict[str, object]] | None = None,
    meta: dict[str, object] | None = None,
    replace_meta: bool = False,
) -> None:
    """Keep an edit in the custom capa's current manifest, so an enable (which
    rewrites each row from it) or a further department (seeded from it) keeps
    it too. `tools=None` leaves the connection's HTTP tools as they are.

    An HTTP API's tools exist only in the manifest, so a connection missing
    from it is a 409. A server's own tools need no manifest entry: an entry
    that is missing is never rewritten from, so the rows alone hold.
    """
    version = (
        await db.get(m.CapaVersion, capa.current_version_id)
        if capa.current_version_id is not None
        else None
    )
    if version is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this connection's plugin has no installed manifest to keep the edit in",
        )
    manifest = copy.deepcopy(version.manifest)
    found = False
    for entry in (manifest.get("tool_pack") or {}).get("connections", []):
        if entry.get("key", "default") != connection_key:
            continue
        found = True
        entry_cfg = dict(entry.get("config") or {})
        if tools is not None:
            entry_cfg["http_tools"] = tools
        if replace_meta:
            if meta is None:
                entry_cfg.pop("openapi", None)
            else:
                entry_cfg["openapi"] = meta
        entry["config"] = entry_cfg
        entry["scopes"] = scopes
    if not found and tools is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this connection is no longer in its plugin's manifest; install it again",
        )
    # A setup form's own `scopes` (a flat list) wins over the connection's when
    # a further department is set up (api/v1/capas.py `_apply_plugin_setup`),
    # and it cannot hold a read/modify classification: clear it, so the edited
    # classification above is the one every department gets.
    setup_mcp = (manifest.get("setup") or {}).get("mcp")
    if isinstance(setup_mcp, dict) and setup_mcp.get("connection_key", "default") == connection_key:
        setup_mcp["scopes"] = []
    parsed = parse_manifest(manifest)
    version.manifest = parsed.model_dump(mode="json")
    version.artifact_hash = _artifact_hash(parsed)
