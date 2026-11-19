"""The Copilot's direct access (copilot direct access design §3): the offer,
what the Copilot is told it may offer, and the approval's effect."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import (
    OFFER_DIRECT_ACCESS,
    ControlOutcome,
    DelegationDepthExceeded,
    _acting_token_role,
    start_delegation,
)
from oc8.agent.harness.stages.b_direct_access import DirectAccessView, offers_direct
from oc8.approvals import AgentNotOffered, AgentNotRunnable, EffectRefused, raise_approval
from oc8.audit import append_event
from oc8.connections.access import (
    ConnectionRequired,
    ToolAccess,
    account_of,
    resolve_direct_access,
)
from oc8.connections.direct_access import DirectAccessRun, active_grant, direct_access_run
from oc8.connections.usable import (
    direct_connection,
    member_departments_for,
    member_may_use,
    member_usable_tool_keys,
    tool_key_label,
)
from oc8.copilot.agent_match import AgentMatch, matching_agents, member_may_run_agent
from oc8.copilot.tools import uuid_or_none
from oc8.modelrouter import ToolCall
from oc8.runtime.fencing import one_line
from oc8.runtime.repository import RunRepository
from oc8.runtime.run_context import record_direct_access_answer
from oc8.runtime.states import RunState

logger = logging.getLogger(__name__)

DIRECT_ACCESS_ACTION: Final = "direct_access"

#: How much of the request is matched against the agents (and handed to the
#: one the person chooses).
_REQUEST_LIMIT: Final = 2000

_ACCOUNT_TEXT: Final = {"own": "their own login", "organisation": "the organisation account"}


def _declined_in_this_run(context: dict[str, Any], tool_key: str) -> bool:
    entry = context.get("resolved_direct_access")
    if (
        isinstance(entry, dict)
        and entry.get("tool_key") == tool_key
        and entry.get("decision") == "reject"
    ):
        return True
    # Every decline of the run, not just the latest answer
    # (`record_direct_access_answer`).
    declined = context.get("declined_direct_access")
    return isinstance(declined, list) and tool_key in declined


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
    """Keys whose LATEST decided offer was rejected: one allowed afterwards
    (and maybe revoked since) is not declined."""
    rows = (
        await db.execute(
            select(m.ApprovalRequest)
            .where(
                m.ApprovalRequest.action_type == DIRECT_ACCESS_ACTION,
                m.ApprovalRequest.status.in_(("approved", "rejected")),
                m.ApprovalRequest.payload["direct_access"]["session_id"].astext == str(session_id),
            )
            .order_by(m.ApprovalRequest.decided_at, m.ApprovalRequest.created_at)
        )
    ).scalars()
    latest: dict[str, str] = {}
    for r in rows:
        latest[str(r.payload.get("direct_access", {}).get("tool_key") or "")] = r.status
    return {key for key, status in latest.items() if status == "rejected"}


async def _copilot_may_read(
    db: AsyncSession,
    *,
    direct: DirectAccessRun,
    tool_key: str,
    narrowing: Mapping[str, Any] | None,
    scopes: Mapping[str, Any] | None = None,
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
        tool=None,
        scopes=scopes,
        narrowing=narrowing,
    )


async def _own_request(db: AsyncSession, *, direct: DirectAccessRun, run: m.AgentRun) -> str:
    """The person's own words this run answers: the chat message its turn
    replies to (`reply_to_message_id`, written only for a person's turn),
    in this conversation, written by this person. Never the newest message
    of the conversation -- in a shared chat it may be someone else's -- and
    never a wake-up's text, which carries another agent's result. Empty when
    there is no such turn: then no agent is offered."""
    message_id = uuid_or_none((run.context or {}).get("reply_to_message_id"))
    if message_id is None:
        return ""
    message = await db.get(m.ChatMessage, message_id)
    if (
        message is None
        or message.session_id != direct.session_id
        or message.role != "user"
        or message.author_member_id != direct.member_id
    ):
        return ""
    return message.content.strip()


def _request_text(purpose: str, request: str) -> str:
    return "\n".join(part for part in (purpose, request) if part)[:_REQUEST_LIMIT]


async def _fitting_agents(
    db: AsyncSession,
    *,
    copilot: m.Agent,
    member_id: uuid.UUID,
    tool_key: str,
    request: str,
    run_id: uuid.UUID,
) -> list[AgentMatch]:
    """The agents to offer instead (Copilot: existing agent first). Never
    fails the offer: anything going wrong here costs the matches only, in a
    savepoint so the offer's own transaction is untouched."""
    try:
        async with db.begin_nested():
            return await matching_agents(
                db,
                member_id=member_id,
                tool_key=tool_key,
                request=request,
                copilot=copilot,
                token_role=await _acting_token_role(db, tenant_id=copilot.tenant_id, run_id=run_id),
            )
    except Exception:
        logger.warning("no agent match for a direct-access offer", exc_info=True)
        return []


async def _login_name(db: AsyncSession, access: ToolAccess, tool_key: str) -> str:
    """The login the direct path would use, as the card names it: the
    credential's name, an OAuth account's integration label, else the
    connection's own label."""
    if access.credential is not None and access.credential.name:
        return one_line(access.credential.name, 120)
    if access.integration is not None and access.integration.label:
        return one_line(access.integration.label, 120)
    return one_line(await tool_key_label(db, tool_key), 120)


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
    if not await member_may_use(db, member_id=direct.member_id, tool_key=key, right="read"):
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
    if not await _copilot_may_read(
        db,
        direct=direct,
        tool_key=key,
        narrowing=narrowing,
        scopes=conn.scopes if isinstance(conn.scopes, dict) else None,
    ):
        return ControlOutcome(
            output=f"ERROR: your own configuration leaves you no read through {key}; "
            "offer only a connection from your direct-access list"
        )
    try:
        access = await resolve_direct_access(db, member_id=direct.member_id, conn=conn)
    except (ConnectionRequired, PermissionError) as exc:
        # ConnectionRequired's text tells the person to connect under /connections.
        return ControlOutcome(output=f"ERROR: {exc}")
    member = await db.get(m.OrgMember, direct.member_id)
    name = one_line((member.display_name or member.subject) if member else "", 120)
    label = one_line(await tool_key_label(db, key), 120)
    account = account_of(access)
    # Last, once every guard above has passed: a refused offer costs no
    # embedding call.
    request = (await _own_request(db, direct=direct, run=run))[:_REQUEST_LIMIT]
    matches = (
        await _fitting_agents(
            db,
            copilot=agent,
            member_id=direct.member_id,
            tool_key=key,
            request=_request_text(purpose, request),
            run_id=run.id,
        )
        if request
        else []
    )
    # Neutral even when agents fit: the row's title is what every other door
    # shows (the approvals inbox, the realtime event) next to a plain Approve,
    # which grants direct access and starts no agent. Only the chat card, which
    # can start one, asks "run it?" -- built from `direct_access.agents`.
    title = f"Copilot on behalf of {name}: read {label}"
    title_de = f"Copilot im Auftrag von {name}: {label} lesen"
    approval = await raise_approval(
        db,
        tenant_id=tenant_id,
        agent_id=agent.id,
        task_id=task.id,
        action_type=DIRECT_ACCESS_ACTION,
        # The person's own card, in no department's queue (R21): only they
        # see and decide it (`approvals.repo.is_member_owned`).
        department_id=None,
        announce_to_member=direct.member_id,
        title=title,
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
                # What the chosen agent is handed (with the purpose), and
                # what the card shows it will be handed.
                "request": request,
                "login_name": await _login_name(db, access, key),
                "agents": [
                    {
                        "id": str(match.agent_id),
                        "name": one_line(match.name, 120),
                        "department": one_line(match.department_name, 120),
                        "score": round(match.score, 4),
                    }
                    for match in matches
                ],
            },
            "i18n": {"de": {"title": title_de}},
        },
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
            "agent_ids": [str(match.agent_id) for match in matches],
        },
    )
    offered = (
        " The card also offers to run "
        + ", ".join(one_line(match.name, 120) for match in matches)
        + " instead."
        if matches
        else ""
    )
    return ControlOutcome(
        output=f"Asked {name} to allow reading {label} through {_ACCOUNT_TEXT[account]}."
        f"{offered} Wait for their decision.",
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


async def _choose_agent(
    db: AsyncSession,
    *,
    approval: m.ApprovalRequest,
    info: Mapping[str, Any],
    session: m.ChatSession,
    member_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> dict[str, str]:
    """The person ran an agent the offer named instead of letting the Copilot
    do it: their run right re-checked now, with the role claim of the
    Copilot run that made the offer, and the agent started as that run's
    delegation -- it waits on the sub-run like after `delegate_task`. No
    grant. Returns the `chosen_agent` record."""
    offered = info.get("agents")
    offered_ids = {
        str(entry.get("id"))
        for entry in (offered if isinstance(offered, list) else [])
        if isinstance(entry, dict)
    }
    if str(agent_id) not in offered_ids:
        raise AgentNotOffered()
    run_id = uuid_or_none((approval.payload or {}).get("run_id"))
    parent_run = await db.get(m.AgentRun, run_id) if run_id is not None else None
    copilot = await db.get(m.Agent, approval.agent_id)
    target = await db.get(m.Agent, agent_id)
    if parent_run is None or parent_run.task_id != approval.task_id or copilot is None:
        raise EffectRefused("direct_access_run_gone")
    if (
        target is None
        or target.tenant_id != approval.tenant_id
        or not await member_may_run_agent(
            db,
            member_id=member_id,
            agent=target,
            token_role=await _acting_token_role(
                db, tenant_id=approval.tenant_id, run_id=parent_run.id
            ),
        )
    ):
        name = one_line(target.name, 120) if target is not None else ""
        raise AgentNotRunnable(name)
    purpose = str(info.get("purpose") or "")
    request = str(info.get("request") or "")
    task_text = "\n\n".join(part for part in (request, purpose) if part) or purpose
    try:
        sub_run = await start_delegation(
            db, parent_run=parent_run, delegator=copilot, target=target, task_text=task_text
        )
    except DelegationDepthExceeded as exc:
        raise EffectRefused("delegation_depth_limit") from exc
    chosen = {"id": str(target.id), "name": one_line(target.name, 120), "run_id": str(sub_run.id)}
    approval.payload = {
        **(approval.payload or {}),
        "direct_access": {**info, "chosen_agent": chosen},
    }
    await db.flush()
    await append_event(
        db,
        tenant_id=approval.tenant_id,
        actor_type="operator",
        actor_id=member_id,
        category="copilot",
        action="direct_access.agent_chosen",
        resource={
            "approval_id": str(approval.id),
            "chat_session_id": str(session.id),
            "agent_id": str(target.id),
            "run_id": str(sub_run.id),
        },
    )
    return chosen


async def resolve_direct_access_approval(
    db: AsyncSession,
    *,
    approval: m.ApprovalRequest,
    decision: str,
    agent_id: uuid.UUID | None = None,
) -> m.AgentRun | None:
    """The offer's effect: on approve, the grant (§5) -- its conversation and
    the person's right re-checked now -- or, with `agent_id`, the agent the
    person chose to run instead (`_choose_agent`); either way, the parked run
    is woken and told. Raises EffectRefused (the caller rolls back) when the
    grant can no longer be given or the agent no longer be started.

    One grant at most: `decide_approval` holds the approval row locked and
    refuses a second decision, the conversation row is locked here like the
    offer locks it, and the partial unique index is the last word."""
    payload = approval.payload or {}
    raw_info = payload.get("direct_access")
    info: Mapping[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    session_id = uuid_or_none(info.get("session_id"))
    member_id = uuid_or_none(info.get("member_id"))
    key = str(info.get("tool_key") or "")
    chosen: dict[str, str] | None = None
    if decision == "approve":
        session = (
            await db.execute(
                select(m.ChatSession).where(m.ChatSession.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        if session is None or member_id is None or session.member_id != member_id or not key:
            raise EffectRefused("direct_access_conversation_gone")
        if agent_id is not None:
            chosen = await _choose_agent(
                db,
                approval=approval,
                info=info,
                session=session,
                member_id=member_id,
                agent_id=agent_id,
            )
        else:
            if not await member_may_use(db, member_id=member_id, tool_key=key, right="read"):
                raise EffectRefused("direct_access_not_allowed")
            if await active_grant(db, session_id=session.id, tool_key=key) is None:
                db.add(
                    m.DirectAccessGrant(
                        tenant_id=approval.tenant_id,
                        chat_session_id=session.id,
                        member_id=member_id,
                        tool_key=key,
                        approval_id=approval.id,
                    )
                )
                await db.flush()
            await append_event(
                db,
                tenant_id=approval.tenant_id,
                actor_type="operator",
                actor_id=member_id,
                category="copilot",
                action="direct_access.granted",
                resource={
                    "approval_id": str(approval.id),
                    "chat_session_id": str(session.id),
                    "tool_key": key,
                },
            )
    run_id = uuid_or_none(payload.get("run_id"))
    run = await db.get(m.AgentRun, run_id) if run_id else None
    if run is None or run.task_id != approval.task_id:
        return None
    entry: dict[str, Any] = {
        "tool_key": key,
        "label": str(info.get("label") or key),
        "decision": decision,
    }
    if chosen is not None:
        entry["chosen_agent"] = {"name": chosen["name"], "run_id": chosen["run_id"]}
    await record_direct_access_answer(db, run, entry)
    # Re-read, not the identity-mapped copy: see `resolve_tool_approval`.
    run = await db.get(m.AgentRun, run.id, populate_existing=True)
    if run is None or run.state != RunState.WAITING_FOR_APPROVAL.value:
        return None
    await RunRepository(db).transition(run, RunState.QUEUED)
    await db.flush()
    return run


def direct_access_resume_text(task_text: str, entry: Mapping[str, Any]) -> str:
    """The resumed run's task: the original request plus the person's answer
    (§3 step 4/6). Unlike a held write's resume, the run carries on with the
    request -- that is what the access was asked for."""
    label = one_line(str(entry.get("label") or entry.get("tool_key") or ""), 120)
    key = one_line(str(entry.get("tool_key") or ""), 200)
    chosen = entry.get("chosen_agent")
    if isinstance(chosen, dict):
        agent = one_line(str(chosen.get("name") or ""), 120)
        run_id = one_line(str(chosen.get("run_id") or ""), 64)
        note = (
            f"[oc8] The person chose to run {agent} instead (run {run_id}). Wait for "
            "its result; do not do the task yourself."
        )
    elif entry.get("decision") == "approve":
        note = (
            f"[oc8] The person approved reading {label} ({key}) in this conversation. "
            "Its tools are now available: carry on with their request. Every change "
            "still asks them."
        )
    else:
        note = (
            f"[oc8] The person declined direct access to {label} ({key}). Answer without "
            "it, and do not offer it again in this conversation unless they ask."
        )
    return f"{task_text}\n\n{note}"
