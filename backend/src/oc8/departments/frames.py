"""Department frames: the checks a frame's tools pass before they are stored, and
the cascade of a changed frame into the agents that live under it.

Shared by `api/v1/departments.py` (`PUT/reset /departments/{id}/tools`), the
personal departments' default frame (`api/v1/personal_departments.py`) and the
re-sync in `departments/personal.py`, so none of them imports a route module.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.publish_hooks import PublishHookFailed
from oc8.agents.versioning import (
    AwaitingHireApproval,
    NoChangesToPublish,
    draft_status,
    publish_version,
)
from oc8.authz.pdp import narrowing_within_frame
from oc8.capas.discovery import connection_supports_value_spec, resolve_tool_pack_connection
from oc8.connections.limit_tools import unknown_limit_tools
from oc8.connections.tool_connection import not_retired
from oc8.connections.value_spec import has_value_spec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from oc8.api.v1.departments import DepartmentToolsWriteRequest

logger = logging.getLogger(__name__)


def clip_narrowing_to_frame(
    frame_tools: dict[str, Any], narrowing: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The narrowing with every tool the frame no longer allows dropped (a key
    absent from `frame_tools`, a widening of it, or a malformed policy -- the
    keys `enforce_personal_narrowing` refuses), or `None` when nothing changes.
    A dropped key falls back to what the frame grants."""
    raw_tools = (narrowing or {}).get("tools")
    if not isinstance(raw_tools, dict):
        return None
    frame = {"tools": frame_tools}
    kept: dict[str, Any] = {}
    for key, raw in raw_tools.items():
        if key not in frame_tools:
            continue
        try:
            if narrowing_within_frame(frame, {"tools": {key: raw}}):
                continue
        except ValueError:
            continue
        kept[key] = raw
    if len(kept) == len(raw_tools):
        return None
    return {**(narrowing or {}), "tools": kept}


def deviation_counts(tools: dict[str, Any], agents: Sequence[m.Agent]) -> dict[str, int]:
    counts: dict[str, int] = {key: 0 for key in tools}
    for agent in agents:
        for key in agent.narrowing_overridden_keys or []:
            if key in counts:
                counts[key] += 1
    return counts


async def cascade_department_tools_change(
    db: AsyncSession,
    *,
    old_tools: dict[str, Any],
    new_tools: dict[str, Any],
    agents: Sequence[m.Agent],
    clip_to_frame: bool = False,
) -> None:
    """Shared by `set_department_tools` and `reset_department_tool`: cascade
    each tool's new `enabled` default to every CURRENT agent in this
    department that has never *deliberately* touched this tool key. See
    `set_department_tools`'s own long comment (unchanged, kept there rather
    than duplicated here) for why this reads `narrowing_overridden_keys` and
    never writes to it, and why the whole policy dict is written rather than
    a sparse `{"enabled": ...}` patch.

    Over the UNION of old/new keys rather than just `new_tools`, so a reset
    that removes a key from the frame entirely (no CAPA default to restore
    it to) still cascades that removal to agents who never overrode it --
    `new_tools.get(key, {})` there is an empty `ToolPolicy`, i.e. "disabled,
    no rights", the correct meaning of "this tool no longer exists here".

    Every touched agent is then PUBLISHED, because runs execute an agent's
    published version and not its row: without it, re-enabling a tool at the
    department would never reach a single agent (the frame alone cannot widen
    a pinned narrowing that says "disabled"). Only an agent with no pending
    draft is published, so the new version holds the cascade and nothing else;
    for one mid-edit, publishing would push that operator's unfinished draft
    live, so the cascade joins the draft instead and shows in its publish bar.
    `clip_to_frame` is for a personal department, whose frame is a ceiling no
    agent may exceed (`departments.personal.enforce_personal_narrowing`): beyond
    the cascade, every agent's narrowing then loses the tools `new_tools` no
    longer allows -- an overridden key too, which the cascade leaves alone -- and
    those agents are published like any other touched one.

    A publish hook refusing rejects the whole department change (422, and
    `get_db` rolls every write back) rather than leave agents half-published.
    """
    changed_keys = {
        key
        for key in set(old_tools) | set(new_tools)
        if bool(new_tools.get(key, {}).get("enabled", False))
        != bool(old_tools.get(key, {}).get("enabled", False))
    }
    touched = [
        agent
        for agent in agents
        if any(key not in (agent.narrowing_overridden_keys or []) for key in changed_keys)
    ]
    clipped_ids: set[uuid.UUID] = set()
    if clip_to_frame:
        clipped_ids = {
            agent.id
            for agent in agents
            if clip_narrowing_to_frame(new_tools, agent.narrowing) is not None
        }
        touched = touched + [
            agent for agent in agents if agent.id in clipped_ids and agent not in touched
        ]
    # Read BEFORE the write below, which would make every one of them dirty.
    publishable = {agent.id for agent in touched if not (await draft_status(db, agent)).dirty}
    for key in changed_keys:
        new_policy = dict(new_tools.get(key, {}))
        eur = new_policy.get("approval_eur")
        if (
            isinstance(eur, int | float)
            and eur > 0
            and agents
            and not await has_value_spec(db, tenant_id=agents[0].tenant_id, key=key)
        ):
            # A positive threshold on a connection that reports no amount never
            # fires, and an agent copy goes stale when the department changes
            # its value; the frame's own applies anyway. €0 is enforced and
            # copied (`connections.value_spec`).
            del new_policy["approval_eur"]
        for agent in agents:
            if key in (agent.narrowing_overridden_keys or []):
                continue  # a genuine, deliberate operator override -- never touch it
            narrowing = dict(agent.narrowing or {})
            agent_tools = dict(narrowing.get("tools", {}))
            previous = agent_tools.get(key)
            agent_tools[key] = dict(new_policy)
            # The agent's login stays its own: a department change is about
            # what may be done, never whose account does it.
            if isinstance(previous, dict) and previous.get("connection_id"):
                agent_tools[key]["connection_id"] = previous["connection_id"]
            narrowing["tools"] = agent_tools
            agent.narrowing = narrowing
    if clip_to_frame:
        for agent in touched:
            clipped = clip_narrowing_to_frame(new_tools, agent.narrowing)
            if clipped is not None:
                agent.narrowing = clipped
                agent.narrowing_overridden_keys = sorted(
                    set(agent.narrowing_overridden_keys or []) & set(clipped.get("tools", {}))
                )
    await db.flush()
    for agent in touched:
        if agent.id not in publishable:
            logger.info(
                "department tool cascade deferred to existing draft: agent=%s",
                agent.id,
            )
            continue
        try:
            await publish_version(db, agent, note="department tool access changed")
        except AwaitingHireApproval:
            # An agent still awaiting its hire approval is not live: the
            # approver approves the hire's v1, and the change waits in the
            # working copy instead of failing the department's cascade.
            continue
        except NoChangesToPublish:
            # The cascade wrote what the version already had.
            continue
        except PublishHookFailed as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "error": "publish_hook_rejected",
                    "agentId": str(agent.id),
                    "hook": exc.hook_name,
                    "reason": str(exc.cause),
                },
            ) from exc


async def validate_frame_tools(
    db: AsyncSession, *, tenant_id: uuid.UUID, body: DepartmentToolsWriteRequest
) -> dict[str, Any]:
    """The checks a department frame's tools must pass before they are stored,
    shared by `PUT /departments/{id}/tools` and the personal departments'
    default frame. Returns the tools as the frame stores them; raises 422
    (`limit_names_unknown_tools`, `value_spec_not_supported`) otherwise."""
    new_tools = {k: v.model_dump() for k, v in body.tools.items()}

    unknown = await unknown_limit_tools(db, tenant_id=tenant_id, tools=new_tools)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "limit_names_unknown_tools", "violations": unknown},
        )

    value_spec_violations: list[dict[str, str]] = []
    for key, policy in body.tools.items():
        # `only` is a plain tool-name allowlist -- meaningful for ANY
        # connection that has tools to restrict, with no dependency on a
        # `value_spec` (github_mcp/jira_mcp/microsoft365/google_workspace all
        # ship real `only`-based guardrail presets and declare no value_spec
        # at all; the frontend's own drawer already gates `only`'s visibility
        # on the connection having tool names, not on `hasValueSpec` --
        # `tool-guardrail-editor-drawer.tsx`). Only `approval_eur` (a
        # monetary threshold) genuinely needs to know a value_spec exists,
        # since that's where the threshold gets compared against.
        #
        # `only` is a list, not a nullable scalar like `approval_eur` -- the
        # frontend's GuardrailValue always sends `only: []` for a tool that
        # never had an allowlist, never `null`, so an `is not None` check
        # (matching approval_eur's real absent/present distinction) treats
        # every save as "wants an allowlist". Only a genuinely non-empty list
        # means the operator actually wants one.
        wants_eur = policy.approval_eur is not None
        if not wants_eur:
            continue
        # `credential_id.is_(None)` picks the manifest row, never another
        # login sharing this same tenant-global name -- `POST /mcp/logins`
        # creates a SECOND `McpConnection` row with the same `name` (see its
        # own docstring), and a bare `.where(name == ...)` here would raise
        # `MultipleResultsFound` for any tenant that has pinned a login on
        # exactly the connections that have a value_spec.
        mcp_conn = (
            await db.execute(
                select(m.McpConnection)
                .where(
                    m.McpConnection.tenant_id == tenant_id,
                    m.McpConnection.name == key,
                    m.McpConnection.credential_id.is_(None),
                    not_retired(),
                )
                .order_by(m.McpConnection.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        _cfg = mcp_conn.config if mcp_conn is not None and isinstance(mcp_conn.config, dict) else {}
        manifest_conn = resolve_tool_pack_connection(
            str(_cfg.get("_plugin_name", "")), str(_cfg.get("_connection_key", ""))
        )
        if not connection_supports_value_spec(manifest_conn):
            value_spec_violations.append({"connection": key, "field": "approval_eur"})
    if value_spec_violations:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "value_spec_not_supported", "violations": value_spec_violations},
        )

    return new_tools
