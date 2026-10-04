"""The Copilot's dot tools (design §7a.3-§7a.4). Every call resolves the
member behind the run's task and scopes everything to them; a model naming
another member's responsibility gets "not found"."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.control_tools import (
    CANCEL_FOLLOWUP,
    COPILOT_TOOL_NAMES,
    COPILOT_TOOLS,
    RESPONSIBILITY_CLOSE,
    RESPONSIBILITY_OPEN,
    RESPONSIBILITY_UPDATE,
    SCHEDULE_FOLLOWUP,
    ControlOutcome,
    _resolve_agent_actor,
)
from oc8.copilot.door import door_of
from oc8.copilot.followups import cancel_followup, list_followups, schedule_followup
from oc8.copilot.responsibilities import (
    ResponsibilityError,
    close_responsibility,
    get_owned,
    open_responsibility,
    update_responsibility,
)
from oc8.copilot.schedule import FollowupRejected, validate_followup
from oc8.modelrouter import ToolCall

__all__ = ["COPILOT_TOOLS", "execute_copilot_tool"]


class _BadArgument(ValueError):
    """A tool argument of the wrong type; becomes an ERROR string, never a raise."""


def _text(args: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    raw = args.get(key)
    if raw is None:
        if required:
            raise _BadArgument(f"{key} is required")
        return None
    if not isinstance(raw, str):
        raise _BadArgument(f"{key} must be a string")
    return raw


def _flag(args: dict[str, Any], key: str) -> bool:
    raw = args.get(key)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ("true", "false"):
        return raw.strip().lower() == "true"
    raise _BadArgument(f"{key} must be true or false")


def _uuid(raw: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _card(r: m.Responsibility) -> dict[str, Any]:
    return {
        "component_key": "responsibility_card",
        "props": {
            "id": str(r.id),
            "title": r.title,
            "goal": r.goal,
            "state": r.state,
            "nextStep": r.next_step,
        },
    }


async def execute_copilot_tool(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    task: m.Task,
    tc: ToolCall,
    run_id: uuid.UUID | None,
) -> ControlOutcome | None:
    """Run one dot tool, or return None for a name this module does not own."""
    if tc.name not in COPILOT_TOOL_NAMES:
        return None
    if not agent.is_tenant_assistant:
        return ControlOutcome(output="ERROR: only the Copilot keeps responsibilities")
    # Same seam as the other Copilot write tools: None when nobody is behind the
    # task, or when the run was posted by someone other than the session's own
    # member (the copilot:manage oversight carve-out). Their dots must never be
    # created as -- and later fire with the authority of -- the colleague.
    actor = await _resolve_agent_actor(db, tenant_id=tenant_id, task=task, run_id=run_id)
    if actor is None:
        return ControlOutcome(
            output="ERROR: only the person this conversation belongs to can set up "
            "their responsibilities"
        )
    member = actor.member
    args = tc.arguments if isinstance(tc.arguments, dict) else {}
    run = await db.get(m.AgentRun, run_id) if run_id else None
    context: dict[str, Any] = (run.context or {}) if run is not None else {}
    door = door_of(context)
    try:
        return await _dispatch(
            db,
            tenant_id=tenant_id,
            agent=agent,
            member=member,
            tc=tc,
            args=args,
            context=context,
            door=door,
            run_id=run_id,
        )
    except _BadArgument as exc:
        return ControlOutcome(output=f"ERROR: {exc}")
    except (ResponsibilityError, FollowupRejected) as exc:
        return ControlOutcome(output=f"ERROR: {exc}")


async def _dispatch(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent: m.Agent,
    member: m.OrgMember,
    tc: ToolCall,
    args: dict[str, Any],
    context: dict[str, Any],
    door: str,
    run_id: uuid.UUID | None,
) -> ControlOutcome:
    if tc.name == RESPONSIBILITY_OPEN.name:
        if door == "followup":
            return ControlOutcome(
                output="ERROR: a follow-up cannot start new responsibilities; ask the person"
            )
        session_id = _uuid(context.get("chat_session_id"))
        if session_id is None:
            return ControlOutcome(output="ERROR: responsibilities start in a conversation")
        r = await open_responsibility(
            db,
            tenant_id=tenant_id,
            member_id=member.id,
            chat_session_id=session_id,
            title=_text(args, "title", required=True) or "",
            goal=_text(args, "goal", required=True) or "",
            notify_rule=_text(args, "notify_rule") or "risks_and_decisions",
            origin_channel=(str(context["chat_channel"]) if door == "telegram" else None),
            run_id=run_id,
            actor_agent_id=agent.id,
            member_subject=member.subject,
        )
        return ControlOutcome(
            output=f"Responsibility opened ({r.id}).", rendered_component=_card(r)
        )
    if tc.name == CANCEL_FOLLOWUP.name:
        fid = _uuid(args.get("followup_id"))
        if fid is None or not await cancel_followup(
            db, tenant_id=tenant_id, member_id=member.id, trigger_id=fid
        ):
            return ControlOutcome(output="ERROR: follow-up not found")
        return ControlOutcome(output="Follow-up ended.")
    rid = _uuid(args.get("responsibility_id"))
    if rid is None:
        return ControlOutcome(output="ERROR: responsibility_id is required")
    if tc.name == RESPONSIBILITY_UPDATE.name:
        state = _text(args, "state")
        if state is not None and state not in ("active", "paused"):
            return ControlOutcome(output="ERROR: state must be active or paused")
        r = await update_responsibility(
            db,
            tenant_id=tenant_id,
            member_id=member.id,
            responsibility_id=rid,
            next_step=_text(args, "next_step"),
            state=state,
            report=_flag(args, "report"),
            run_id=run_id,
        )
        return ControlOutcome(output=f"Updated. Next step: {r.next_step or '-'}")
    if tc.name == RESPONSIBILITY_CLOSE.name:
        close_state = _text(args, "state")
        if close_state == "done":
            final: Literal["done", "cancelled"] = "done"
        elif close_state == "cancelled":
            final = "cancelled"
        else:
            return ControlOutcome(output="ERROR: state must be done or cancelled")
        r = await close_responsibility(
            db,
            tenant_id=tenant_id,
            member_id=member.id,
            responsibility_id=rid,
            state=final,
            reason=_text(args, "reason") or "",
            actor_agent_id=agent.id,
            member_subject=member.subject,
        )
        return ControlOutcome(output=f"Responsibility {final}.", rendered_component=_card(r))
    if tc.name != SCHEDULE_FOLLOWUP.name:
        return ControlOutcome(output="ERROR: unknown tool")
    kind = _text(args, "kind", required=True) or ""
    if door == "followup":
        # A follow-up turn reads content the person may not have written (a
        # mail, a page). It must not be able to persist a recurring instruction
        # from it: only a single re-check of the SAME responsibility, and only
        # when nothing else is already scheduled for it.
        fu = context.get("followup")
        own = _uuid(fu.get("responsibility_id")) if isinstance(fu, dict) else None
        firing = _uuid(fu.get("trigger_id")) if isinstance(fu, dict) else None
        if kind != "once":
            return ControlOutcome(
                output="ERROR: a follow-up can only schedule one more single check; "
                "recurring follow-ups are set up in a conversation with the person"
            )
        if own is None or own != rid:
            return ControlOutcome(
                output="ERROR: a follow-up can only reschedule its own responsibility"
            )
        others = [
            t
            for t in await list_followups(db, tenant_id=tenant_id, member_id=member.id)
            if t.responsibility_id == rid and t.enabled and t.id != firing
        ]
        if others:
            return ControlOutcome(
                output="ERROR: this responsibility already has another follow-up scheduled"
            )
    prompt = _text(args, "prompt") or ""
    spec = validate_followup(
        kind=kind,
        timezone=_text(args, "timezone"),
        run_at=_text(args, "run_at"),
        cron_expression=_text(args, "cron_expression"),
        ends_at=_text(args, "ends_at"),
        now=dt.datetime.now(tz=dt.UTC),
    )
    t = await schedule_followup(
        db,
        tenant_id=tenant_id,
        member_id=member.id,
        responsibility_id=rid,
        assistant_id=agent.id,
        spec=spec,
        prompt=prompt,
        member_subject=member.subject,
    )
    owned = await get_owned(db, tenant_id=tenant_id, member_id=member.id, responsibility_id=rid)
    # The next fire in the trigger's own zone, so "09:00" reads as 09:00.
    when = t.next_run_at.astimezone(ZoneInfo(spec.timezone)).isoformat() if t.next_run_at else None
    return ControlOutcome(
        output=f"Follow-up saved ({t.id}); next at {when} ({spec.timezone}).",
        rendered_component={
            "component_key": "followup_card",
            "props": {
                "id": str(t.id),
                "responsibilityTitle": owned.title if owned else "",
                "kind": t.kind,
                "when": when,
                "timezone": t.timezone,
                "endsAt": t.ends_at.isoformat() if t.ends_at else None,
            },
        },
    )
