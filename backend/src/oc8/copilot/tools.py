"""The Copilot's dot tools (design §7a.3-§7a.4). Every call resolves the
member behind the run's task and scopes everything to them; a model naming
another member's responsibility gets "not found"."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
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
    _member_behind_task,
)
from oc8.copilot.door import door_of
from oc8.copilot.followups import cancel_followup, schedule_followup
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
    member = await _member_behind_task(db, tenant_id=tenant_id, task=task)
    if member is None:
        return ControlOutcome(output="ERROR: no person behind this conversation")
    args = tc.arguments or {}
    run = await db.get(m.AgentRun, run_id) if run_id else None
    context: dict[str, Any] = (run.context or {}) if run is not None else {}
    try:
        if tc.name == RESPONSIBILITY_OPEN.name:
            session_id = _uuid(context.get("chat_session_id"))
            if session_id is None:
                return ControlOutcome(output="ERROR: responsibilities start in a conversation")
            r = await open_responsibility(
                db,
                tenant_id=tenant_id,
                member_id=member.id,
                chat_session_id=session_id,
                title=str(args.get("title", "")),
                goal=str(args.get("goal", "")),
                notify_rule=str(args.get("notify_rule") or "risks_and_decisions"),
                origin_channel=(
                    str(context["chat_channel"]) if door_of(context) == "telegram" else None
                ),
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
            r = await update_responsibility(
                db,
                tenant_id=tenant_id,
                member_id=member.id,
                responsibility_id=rid,
                next_step=args.get("next_step"),
                state=args.get("state"),
                report=bool(args.get("report")),
                run_id=run_id,
            )
            return ControlOutcome(output=f"Updated. Next step: {r.next_step or '-'}")
        if tc.name == RESPONSIBILITY_CLOSE.name:
            state = args.get("state")
            if state not in ("done", "cancelled"):
                return ControlOutcome(output="ERROR: state must be done or cancelled")
            r = await close_responsibility(
                db,
                tenant_id=tenant_id,
                member_id=member.id,
                responsibility_id=rid,
                state=state,
                reason=str(args.get("reason", "")),
                actor_agent_id=agent.id,
                member_subject=member.subject,
            )
            return ControlOutcome(output=f"Responsibility {state}.", rendered_component=_card(r))
        assert tc.name == SCHEDULE_FOLLOWUP.name
        spec = validate_followup(
            kind=str(args.get("kind", "")),
            timezone=args.get("timezone"),
            run_at=args.get("run_at"),
            cron_expression=args.get("cron_expression"),
            ends_at=args.get("ends_at"),
            now=dt.datetime.now(tz=dt.UTC),
        )
        t = await schedule_followup(
            db,
            tenant_id=tenant_id,
            member_id=member.id,
            responsibility_id=rid,
            assistant_id=agent.id,
            spec=spec,
            prompt=str(args.get("prompt", "")),
            member_subject=member.subject,
        )
        owned = await get_owned(db, tenant_id=tenant_id, member_id=member.id, responsibility_id=rid)
        # The next fire in the trigger's own zone, so "09:00" reads as 09:00.
        when = (
            t.next_run_at.astimezone(ZoneInfo(spec.timezone)).isoformat() if t.next_run_at else None
        )
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
    except (ResponsibilityError, FollowupRejected) as exc:
        return ControlOutcome(output=f"ERROR: {exc}")
