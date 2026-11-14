"""The Copilot's direct access (copilot direct access design §3): the offer,
what the Copilot is told it may offer, and the approval's effect."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import OFFER_DIRECT_ACCESS, ControlOutcome
from oc8.agent.harness.stages.b_direct_access import DirectAccessView, offers_direct
from oc8.approvals import raise_approval
from oc8.audit import append_event
from oc8.connections.access import ConnectionRequired, account_of, resolve_direct_access
from oc8.connections.direct_access import DirectAccessRun, active_grant, direct_access_run
from oc8.connections.usable import (
    direct_connection,
    member_departments_for,
    member_may_use,
    member_usable_tool_keys,
    tool_key_label,
)
from oc8.modelrouter import ToolCall
from oc8.runtime.fencing import one_line

DIRECT_ACCESS_ACTION: Final = "direct_access"

_ACCOUNT_TEXT: Final = {"own": "their own login", "organisation": "the organisation account"}

#: Stands for "a read through this connection" when asking the narrowing
#: whether the Copilot may read there at all.
_READ_PROBE: Final = "offer_direct_access.read"


def _declined_in_this_run(context: dict[str, Any], tool_key: str) -> bool:
    entry = context.get("resolved_direct_access")
    return (
        isinstance(entry, dict)
        and entry.get("tool_key") == tool_key
        and entry.get("decision") == "reject"
    )


def _offer_rows(session_id: uuid.UUID, status: str) -> Any:
    return select(m.ApprovalRequest).where(
        m.ApprovalRequest.action_type == DIRECT_ACCESS_ACTION,
        m.ApprovalRequest.status == status,
        m.ApprovalRequest.payload["direct_access"]["session_id"].astext == str(session_id),
    )


async def _waiting_offer(
    db: AsyncSession, *, session_id: uuid.UUID, tool_key: str
) -> m.ApprovalRequest | None:
    row: m.ApprovalRequest | None = await db.scalar(
        _offer_rows(session_id, "pending")
        .where(m.ApprovalRequest.payload["direct_access"]["tool_key"].astext == tool_key)
        .execution_options(populate_existing=True)
        .limit(1)
    )
    return row


async def _declined_keys(db: AsyncSession, *, session_id: uuid.UUID) -> set[str]:
    rows = (await db.execute(_offer_rows(session_id, "rejected"))).scalars()
    return {str(r.payload.get("direct_access", {}).get("tool_key") or "") for r in rows}


async def _copilot_may_read(
    db: AsyncSession,
    *,
    direct: DirectAccessRun,
    tool_key: str,
    narrowing: Mapping[str, Any] | None,
) -> bool:
    """The person may read through the key AND the Copilot's own pinned
    narrowing, a ceiling (R14), leaves it a read."""
    if not await member_may_use(db, member_id=direct.member_id, tool_key=tool_key, right="read"):
        return False
    departments = await member_departments_for(db, member_id=direct.member_id, tool_key=tool_key)
    view = DirectAccessView(member_id=direct.member_id, granted={tool_key: tuple(departments)})
    return offers_direct(
        view,
        connection_key=tool_key,
        tool=_READ_PROBE,
        scopes={"read": [_READ_PROBE]},
        narrowing=narrowing,
    )


async def execute_direct_access_tool(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    task: m.Task,
    tc: ToolCall,
    run_id: uuid.UUID | None,
    narrowing: Mapping[str, Any] | None = None,
) -> ControlOutcome | None:
    if tc.name != OFFER_DIRECT_ACCESS.name:
        return None
    if not agent.is_tenant_assistant:
        return ControlOutcome(output="ERROR: only the Copilot offers direct access")
    run = await db.get(m.AgentRun, run_id) if run_id is not None else None
    direct = direct_access_run(run, agent)
    if run is None or direct is None:
        return ControlOutcome(
            output="ERROR: direct access is offered only in the web chat, to the person "
            "whose conversation it is"
        )
    if narrowing is None:
        narrowing = agent.narrowing
    args = tc.arguments if isinstance(tc.arguments, dict) else {}
    key = one_line(str(args.get("connection") or ""), 200)
    purpose = one_line(str(args.get("purpose") or ""), 300)
    if not key or not purpose:
        return ControlOutcome(output="ERROR: connection and purpose are required")
    context = run.context if isinstance(run.context, dict) else {}
    if _declined_in_this_run(context, key):
        return ControlOutcome(
            output=f"ERROR: the person declined direct access to {key}; do not offer it "
            "again in this conversation unless they ask"
        )
    # Serialise the offers of one conversation: two parallel calls must not
    # both find nothing waiting and raise two cards.
    await db.execute(
        select(m.ChatSession.id).where(m.ChatSession.id == direct.session_id).with_for_update()
    )
    if await active_grant(db, session_id=direct.session_id, tool_key=key) is not None:
        return ControlOutcome(
            output=f"Direct access to {key} is already allowed in this conversation; use its tools."
        )
    waiting = await _waiting_offer(db, session_id=direct.session_id, tool_key=key)
    if waiting is not None:
        if waiting.payload.get("run_id") == str(run.id):
            return ControlOutcome(
                output=f"The person has not decided on direct access to {key} yet. Wait for them.",
                suspend="waiting_for_approval",
            )
        # Another run holds the offer: parking this one would hang it, nobody
        # wakes it (R4).
        return ControlOutcome(
            output=f"Direct access to {key} is already offered and waiting for the person; "
            "do not offer it again, tell them it is waiting for their decision."
        )
    if not await _copilot_may_read(db, direct=direct, tool_key=key, narrowing=narrowing):
        return ControlOutcome(
            output=f"ERROR: {key} is not a connection this person may use; offer only one "
            "from your direct-access list"
        )
    departments = await member_departments_for(db, member_id=direct.member_id, tool_key=key)
    conn = await direct_connection(
        db,
        tenant_id=tenant_id,
        tool_key=key,
        departments=departments,
        right="read",
    )
    if conn is None:
        return ControlOutcome(output=f"ERROR: {key} is not connected")
    try:
        access = await resolve_direct_access(db, member_id=direct.member_id, conn=conn)
    except (ConnectionRequired, PermissionError) as exc:
        # ConnectionRequired's text tells the person to connect under /connections.
        return ControlOutcome(output=f"ERROR: {exc}")
    member = await db.get(m.OrgMember, direct.member_id)
    name = one_line((member.display_name or member.subject) if member else "", 120)
    label = one_line(await tool_key_label(db, key), 120)
    account = account_of(access)
    approval = await raise_approval(
        db,
        tenant_id=tenant_id,
        agent_id=agent.id,
        task_id=task.id,
        action_type=DIRECT_ACCESS_ACTION,
        title=f"Copilot on behalf of {name}: read {label}",
        detail=purpose,
        payload={
            "tool": OFFER_DIRECT_ACCESS.name,
            "arguments": {"connection": key, "purpose": purpose},
            "run_id": str(run.id),
            "direct_access": {
                "member_id": str(direct.member_id),
                "member_name": name,
                "session_id": str(direct.session_id),
                "tool_key": key,
                "label": label,
                "account": account,
                "purpose": purpose,
                "decider": "member",
                "department_id": None,
            },
            "i18n": {"de": {"title": f"Copilot im Auftrag von {name}: {label} lesen"}},
        },
    )
    from oc8.realtime.bus import get_event_bus

    await get_event_bus().publish_event(
        tenant_id,
        "approval.created",
        {
            "approval_id": str(approval.id),
            "agent_id": str(agent.id),
            "title": approval.title,
            "detail": approval.detail,
        },
        source=f"oc8/approval/{approval.id}",
    )
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent",
        actor_id=agent.id,
        category="copilot",
        action="direct_access.offered",
        resource={
            "approval_id": str(approval.id),
            "tool_key": key,
            "chat_session_id": str(direct.session_id),
            "on_behalf_of": str(direct.member_id),
            "account": account,
        },
    )
    return ControlOutcome(
        output=f"Asked {name} to allow reading {label} through {_ACCOUNT_TEXT[account]}. "
        "Wait for their decision.",
        suspend="waiting_for_approval",
    )


async def direct_access_block(
    db: AsyncSession,
    *,
    run: m.AgentRun | None,
    agent: m.Agent,
    narrowing: Mapping[str, Any] | None = None,
) -> str | None:
    """The system note listing what the Copilot may offer (§3 step 1), or None."""
    direct = direct_access_run(run, agent)
    if direct is None:
        return None
    if narrowing is None:
        narrowing = agent.narrowing
    keys = [
        key
        for key in await member_usable_tool_keys(db, member_id=direct.member_id)
        if await _copilot_may_read(db, direct=direct, tool_key=key, narrowing=narrowing)
    ]
    if not keys:
        return None
    declined = await _declined_keys(db, session_id=direct.session_id)
    lines = [
        "Direct access: when no agent in the roster fits the person's request, you "
        "may offer to do it yourself through one of these connections with "
        "offer_direct_access(connection, purpose). Never call a connection's tool "
        "before the person allowed it. Reading is then allowed for the rest of this "
        "conversation; every change asks them again.",
        "Connections (key | name | status):",
    ]
    for key in keys:
        if await active_grant(db, session_id=direct.session_id, tool_key=key) is not None:
            status = "allowed in this conversation"
        elif key in declined:
            status = "declined — offer again only if the person asks"
        else:
            status = "not allowed yet"
        lines.append(f"- {key} | {one_line(await tool_key_label(db, key), 80)} | {status}")
    return "\n".join(lines)
