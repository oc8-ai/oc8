"""oc8 as an outward MCP server: the same closed capability surface the
in-app Copilot uses (`copilot/proposals.py`), reachable from OUTSIDE oc8 by
whoever holds a member's own API key (Settings -> API keys).

Deliberately narrow: this router wraps `create_proposal`/`apply_proposal`/
`reject_proposal` directly rather than re-implementing capability logic, so
"everything an API key can do" is defined in exactly one place (`copilot/
capabilities.py`'s closed `Operation` union) and stays that way as new
operations are added there.

Mounted at its own top-level prefix in `main.py` (`/mcp/external`), the same
way `/mcp` and `/llm` are -- NOT under `/api/v1` -- because an API key is not
an operator session: it is verified by its own hash lookup below, never by
`get_principal`'s JWT flow, and it must not gain the rest of the operator
REST API by accident. The minted `Principal` carries `kind="operator"`
(matching the key's owning member exactly) so `authority_for_principal`
resolves that member's REAL, LIVE permissions on every call -- a key is never
more powerful than the person who created it, and disabling that person's
access disables every key they hold too.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.api.v1._serializers import agent_to_dto, department_to_dto, integration_to_dto
from oc8.api.v1.catalog import _agent_slug_to_id
from oc8.apikeys.service import API_KEY_PREFIX, find_enabled_by_token, touch_last_used
from oc8.auth import Principal
from oc8.authz.authority import authority_for_principal
from oc8.authz.permissions import COPILOT, MANAGE, MEMBER_ROLE, VIEW, perm
from oc8.copilot.capabilities import InvalidOperation, _connection_tool_names, operation_references
from oc8.copilot.models import CopilotOperation, CopilotProposal
from oc8.copilot.proposals import (
    ProposalNotRejectable,
    ProposalRejected,
    apply_proposal,
    create_proposal,
    reject_proposal,
)
from oc8.db.session import tenant_session
from oc8.tenants.provision import get_singleton_organization

logger = logging.getLogger(__name__)
router = APIRouter()

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "oc8-external-copilot-gateway"

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603

_bearer = HTTPBearer(auto_error=True)


# --------------------------------------------------------------------- auth


async def _verify_api_key(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
) -> Principal:
    """Resolves an `oc8_ak_...` bearer token to the operator `Principal` of
    its owning member -- never a JWT, never `get_identity_provider().verify()`.

    A token carries no tenant hint of its own, so the singleton Organization
    is resolved first (Community: exactly one), the same way `password_login`
    resolves it (`tenants/provision.py`'s `get_singleton_organization`).

    `role=MEMBER_ROLE`, not `""`: `authority_for_principal` falls back to this
    token floor (`permissions_for(principal.role)`) whenever the member has no
    `role_id` override, which -- per `authz/authority.py` -- is virtually every
    member on every live tenant today. `MEMBER_ROLE` is the exact floor
    `password_login` mints for that same member, so a self-service key stays
    "never more powerful than the person who created it" for the common case
    too, not just for members an admin has explicitly assigned a role to.
    """
    token = creds.credentials
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "invalid or disabled API key",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token.startswith(API_KEY_PREFIX):
        raise unauthorized

    async with tenant_session(None) as unbound_db:
        org = await get_singleton_organization(unbound_db)

    async with tenant_session(org.id) as db:
        api_key = await find_enabled_by_token(db, token=token)
        if api_key is None:
            raise unauthorized
        if api_key.allowed_origins:
            origin = request.headers.get("origin")
            if origin not in api_key.allowed_origins:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, "origin not allowed for this API key"
                )
        member = await db.get(m.OrgMember, api_key.member_id)
        if member is None or member.deleted_at is not None:
            raise unauthorized
        await touch_last_used(db, api_key)
        subject = member.subject

    return Principal(subject=subject, tenant_id=org.id, role=MEMBER_ROLE, kind="operator")


async def _api_key_db(
    principal: Annotated[Principal, Depends(_verify_api_key)],
) -> AsyncIterator[AsyncSession]:
    async with tenant_session(principal.tenant_id) as session:
        yield session


ApiKeyPrincipal = Annotated[Principal, Depends(_verify_api_key)]
ApiKeyDbSession = Annotated[AsyncSession, Depends(_api_key_db)]


async def _require(
    request: Request, db: AsyncSession, principal: Principal, permission: str
) -> None:
    authority = await authority_for_principal(request, db, principal)
    if permission not in authority.tenant_wide:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission: {permission}")


# ------------------------------------------------------------------ JSON-RPC


def _result(rpc_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": payload}


def _error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _proposal_payload(db: AsyncSession, proposal: CopilotProposal) -> dict[str, Any]:
    operations = (
        await db.execute(
            select(CopilotOperation)
            .where(CopilotOperation.proposal_id == proposal.id)
            .order_by(CopilotOperation.ordinal)
        )
    ).scalars().all()
    return {
        "id": str(proposal.id),
        "status": proposal.status,
        "revision": proposal.revision,
        "operations": [
            {"label": op.operation_type, "references": operation_references(op.configuration)}
            for op in operations
        ],
    }


_TOOLS: list[dict[str, Any]] = [
    {
        "name": "copilot_propose",
        "description": (
            "Propose one or more changes to this oc8 tenant -- the same closed "
            "operation set the in-app Copilot proposes (agent mission, trigger "
            "creation, plugin enablement, integration prepare, department/agent "
            "creation, agent guardrails). Returns a DRAFT proposal that changes "
            "nothing until copilot_apply_proposal is called."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "One or more operation objects, each shaped like "
                        "{type, ...}. See oc8's copilot/capabilities.py for the "
                        "closed set of `type` values and their fields."
                    ),
                },
                "idempotencyKey": {
                    "type": "string",
                    "description": "Optional UUID; retrying with the same key is safe.",
                },
            },
            "required": ["operations"],
        },
    },
    {
        "name": "copilot_list_proposals",
        "description": "List this tenant's Copilot proposals by status (default: draft).",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "default": "draft"}},
        },
    },
    {
        "name": "copilot_review_proposal",
        "description": "Read one Copilot proposal by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"proposalId": {"type": "string"}},
            "required": ["proposalId"],
        },
    },
    {
        "name": "copilot_apply_proposal",
        "description": "Apply a draft Copilot proposal, executing its operations now.",
        "inputSchema": {
            "type": "object",
            "properties": {"proposalId": {"type": "string"}},
            "required": ["proposalId"],
        },
    },
    {
        "name": "copilot_reject_proposal",
        "description": "Reject a draft Copilot proposal without applying it.",
        "inputSchema": {
            "type": "object",
            "properties": {"proposalId": {"type": "string"}},
            "required": ["proposalId"],
        },
    },
    {
        "name": "copilot_list_departments",
        "description": (
            "List every department in this tenant (id, name, goal, icon), including archived ones."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "copilot_get_department",
        "description": "Get one department by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"departmentId": {"type": "string"}},
            "required": ["departmentId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "copilot_list_agents",
        "description": "List agents in this tenant, optionally filtered by department.",
        "inputSchema": {
            "type": "object",
            "properties": {"departmentId": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "copilot_get_agent",
        "description": "Get one agent by id, including its narrowing and runtime assignment.",
        "inputSchema": {
            "type": "object",
            "properties": {"agentId": {"type": "string"}},
            "required": ["agentId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "copilot_list_plugins",
        "description": (
            "List every installed capa (plugin) in this tenant, with its id, name, type, and "
            "trust level."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "copilot_get_plugin",
        "description": "Get one installed capa by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"pluginId": {"type": "string"}},
            "required": ["pluginId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "copilot_list_integrations",
        "description": (
            "List every catalog integration available to this tenant (id, name, category, "
            "connected)."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "copilot_list_connection_tools",
        "description": (
            "List the real function names a named MCP connection exposes -- use this before "
            "agent.guardrail.set to find a valid `function` value for a given `connectionName`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"connectionName": {"type": "string"}},
            "required": ["connectionName"],
            "additionalProperties": False,
        },
    },
]


async def _call_tool(
    request: Request,
    db: AsyncSession,
    principal: Principal,
    *,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if name == "copilot_propose":
        await _require(request, db, principal, perm(COPILOT, MANAGE))
        key = arguments.get("idempotencyKey")
        try:
            proposal = await create_proposal(
                db,
                principal,
                arguments.get("operations"),
                idempotency_key=uuid.UUID(key) if isinstance(key, str) else None,
            )
        except (InvalidOperation, ValueError, TypeError):
            return _tool_result("invalid copilot proposal", is_error=True)
        return _tool_result(json.dumps(await _proposal_payload(db, proposal)))

    if name == "copilot_list_proposals":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        status_filter = arguments.get("status") or "draft"
        rows = (
            await db.execute(
                select(CopilotProposal)
                .where(
                    CopilotProposal.tenant_id == principal.tenant_id,
                    CopilotProposal.status == status_filter,
                )
                .order_by(CopilotProposal.created_at.desc())
            )
        ).scalars().all()
        payload = [await _proposal_payload(db, row) for row in rows]
        return _tool_result(json.dumps(payload))

    if name == "copilot_review_proposal":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        proposal_id = _parse_uuid(arguments.get("proposalId"))
        if proposal_id is None:
            return _tool_result("proposalId is required", is_error=True)
        found = await db.get(CopilotProposal, proposal_id)
        if found is None:
            return _tool_result("proposal not found", is_error=True)
        return _tool_result(json.dumps(await _proposal_payload(db, found)))

    if name == "copilot_apply_proposal":
        await _require(request, db, principal, perm(COPILOT, MANAGE))
        proposal_id = _parse_uuid(arguments.get("proposalId"))
        if proposal_id is None:
            return _tool_result("proposalId is required", is_error=True)
        try:
            await apply_proposal(db, proposal_id, principal)
        except ProposalRejected:
            return _tool_result("copilot proposal cannot be applied", is_error=True)
        applied = await db.get(CopilotProposal, proposal_id)
        assert applied is not None
        return _tool_result(json.dumps(await _proposal_payload(db, applied)))

    if name == "copilot_reject_proposal":
        await _require(request, db, principal, perm(COPILOT, MANAGE))
        proposal_id = _parse_uuid(arguments.get("proposalId"))
        if proposal_id is None:
            return _tool_result("proposalId is required", is_error=True)
        try:
            proposal = await reject_proposal(db, proposal_id, principal)
        except ProposalNotRejectable:
            return _tool_result("copilot proposal cannot be rejected", is_error=True)
        return _tool_result(json.dumps(await _proposal_payload(db, proposal)))

    if name == "copilot_list_departments":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        rows = (
            await db.execute(
                select(m.Department).where(m.Department.tenant_id == principal.tenant_id)
            )
        ).scalars().all()
        payload = [department_to_dto(d).model_dump(mode="json") for d in rows]
        return _tool_result(json.dumps(payload))

    if name == "copilot_get_department":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        department_id = _parse_uuid(arguments.get("departmentId"))
        if department_id is None:
            return _tool_result("departmentId is required", is_error=True)
        department = await db.get(m.Department, department_id)
        if department is None or department.tenant_id != principal.tenant_id:
            return _tool_result("department not found", is_error=True)
        return _tool_result(json.dumps(department_to_dto(department).model_dump(mode="json")))

    if name == "copilot_list_agents":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        statement = select(m.Agent).where(m.Agent.tenant_id == principal.tenant_id)
        raw_department_id = arguments.get("departmentId")
        if raw_department_id:
            department_id = _parse_uuid(raw_department_id)
            if department_id is None:
                return _tool_result("departmentId is invalid", is_error=True)
            statement = statement.where(m.Agent.department_id == department_id)
        rows = (await db.execute(statement)).scalars().all()
        return _tool_result(json.dumps([agent_to_dto(a).model_dump(mode="json") for a in rows]))

    if name == "copilot_get_agent":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        agent_id = _parse_uuid(arguments.get("agentId"))
        if agent_id is None:
            return _tool_result("agentId is required", is_error=True)
        agent = await db.get(m.Agent, agent_id)
        if agent is None or agent.tenant_id != principal.tenant_id:
            return _tool_result("agent not found", is_error=True)
        return _tool_result(json.dumps(agent_to_dto(agent).model_dump(mode="json")))

    if name == "copilot_list_plugins":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        rows = (
            await db.execute(select(m.Capa).where(m.Capa.tenant_id == principal.tenant_id))
        ).scalars().all()
        return _tool_result(
            json.dumps(
                [
                    {
                        "id": str(p.id),
                        "name": p.name,
                        "type": p.type,
                        "trustLevel": p.trust_level,
                        "currentVersionId": (
                            str(p.current_version_id) if p.current_version_id else None
                        ),
                    }
                    for p in rows
                ]
            )
        )

    if name == "copilot_get_plugin":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        plugin_id = _parse_uuid(arguments.get("pluginId"))
        if plugin_id is None:
            return _tool_result("pluginId is required", is_error=True)
        plugin = await db.get(m.Capa, plugin_id)
        if plugin is None or plugin.tenant_id != principal.tenant_id:
            return _tool_result("plugin not found", is_error=True)
        return _tool_result(
            json.dumps(
                {
                    "id": str(plugin.id),
                    "name": plugin.name,
                    "type": plugin.type,
                    "trustLevel": plugin.trust_level,
                    "currentVersionId": (
                        str(plugin.current_version_id) if plugin.current_version_id else None
                    ),
                }
            )
        )

    if name == "copilot_list_integrations":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        rows = (
            await db.execute(select(m.Integration).order_by(m.Integration.created_at))
        ).scalars().all()
        slug_to_id = await _agent_slug_to_id(db)
        return _tool_result(
            json.dumps([integration_to_dto(i, slug_to_id).model_dump(mode="json") for i in rows])
        )

    if name == "copilot_list_connection_tools":
        await _require(request, db, principal, perm(COPILOT, VIEW))
        connection_name = str(arguments.get("connectionName") or "")
        if not connection_name:
            return _tool_result("connectionName is required", is_error=True)
        # `credential_id.is_(None)` picks the manifest row -- same pattern as
        # `copilot/capabilities.py`'s `_apply_guardrail_set`.
        conn = (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.tenant_id == principal.tenant_id,
                    m.McpConnection.name == connection_name,
                    m.McpConnection.credential_id.is_(None),
                )
                .order_by(m.McpConnection.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if conn is None:
            return _tool_result("connection not found", is_error=True)
        payload = {"connectionName": connection_name, "tools": sorted(_connection_tool_names(conn))}
        return _tool_result(json.dumps(payload))

    raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown tool: {name}")


@router.post("")
@router.post("/")
async def mcp_external_endpoint(
    request: Request, db: ApiKeyDbSession, principal: ApiKeyPrincipal
) -> Any:
    try:
        body = await request.json()
    except Exception:
        return _error(None, _PARSE_ERROR, "invalid JSON")
    if not isinstance(body, dict):
        return _error(None, _INVALID_REQUEST, "expected a JSON-RPC object")

    method = str(body.get("method", ""))
    rpc_id = body.get("id")
    params = body.get("params") or {}

    if rpc_id is None:
        return Response(status_code=status.HTTP_202_ACCEPTED)

    if method == "initialize":
        return _result(
            rpc_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            },
        )
    if method == "ping":
        return _result(rpc_id, {})
    if method == "tools/list":
        return _result(rpc_id, {"tools": _TOOLS})
    if method == "tools/call":
        name = str(params.get("name", ""))
        if not name:
            return _error(rpc_id, _INVALID_REQUEST, "tools/call requires a name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(rpc_id, _INVALID_REQUEST, "arguments must be an object")
        try:
            payload = await _call_tool(
                request, db, principal, name=name, arguments=arguments
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("mcp_external tools/call failed for %r", name)
            return _error(rpc_id, _INTERNAL_ERROR, str(exc))
        return _result(rpc_id, payload)

    return _error(rpc_id, _METHOD_NOT_FOUND, f"unknown method: {method}")
