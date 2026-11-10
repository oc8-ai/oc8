"""GET /agents/{id}/live -- the Live tab's timeline (spec §3.1).

The caller must reach the agent by the same predicate as every chat door
(`reachable_chat_agent`); "not yours" and "no such agent" are the same 404.
"All runs" -- colleagues' chat runs, as steps and results only -- needs the
right to manage the agent; asking for it without that right is a 403, never
a silent downgrade, so the client cannot mistake a partial timeline for the
whole one.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from oc8 import models as m
from oc8.api.deps import DbSession, require_departmental
from oc8.api.v1.chat import _message_dto
from oc8.authz.authority import authority_for_principal
from oc8.authz.permissions import AGENT, COPILOT_USE, VIEW, perm
from oc8.authz.scope import HumanActor
from oc8.chat.live import (
    LiveItem,
    get_or_create_live_session,
    live_timeline,
    trigger_names,
)
from oc8.chat.reach import reachable_chat_agent, reaches_agent_runs
from oc8.runtime.person import whose_runs
from oc8.runtime.run_inputs import may_manage_agent
from oc8.runtime.step_record import step_timing_dto
from oc8.schemas.dto import LiveItemDTO, LiveRunDTO, LiveTimelineDTO, LiveTriggerDTO

router = APIRouter()

_Actor = Annotated[
    HumanActor, Depends(require_departmental(perm(AGENT, VIEW), or_tenant_wide=COPILOT_USE))
]
_ALL_RUNS_REFUSED = "All runs needs the right to manage this agent"
#: Separates the time and the id in a paging cursor; URL-safe, never in either.
_CURSOR_SEP = "~"


def _cursor(item: LiveItem) -> str:
    return f"{item.at.isoformat()}{_CURSOR_SEP}{item.id}"


def _parse_cursor(raw: str) -> tuple[dt.datetime, uuid.UUID | None]:
    """`<iso time>~<id>` (what `next_before` hands out) or a bare ISO time."""
    at_raw, _, id_raw = raw.partition(_CURSOR_SEP)
    try:
        at = dt.datetime.fromisoformat(at_raw)
        item_id = uuid.UUID(id_raw) if id_raw else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid cursor") from exc
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.UTC)
    return at, item_id


#: Ruling 15: what a colleague's run may show of a step -- its label, never
#: its arguments, raw result, connection, or the model's reasoning.
_COLLEAGUE_CALL_KEYS: Final = ("tool", "step", "state", "durationMs")
_COLLEAGUE_TIMING_KEYS: Final = ("step", "stepWallMs")


def _only(entry: object, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        return {}
    return {k: entry[k] for k in keys if entry.get(k) is not None}


def _run_dto(run: m.AgentRun, trigger: dict[str, str | None], *, colleague: bool) -> LiveRunDTO:
    """`colleague`: somebody else's run, under "All runs". An ALLOWLIST, not a
    blocklist -- a key some runtime adds to the context tomorrow stays out."""
    ctx = run.context or {}
    output = ctx.get("output")
    calls: list[Any] = list(ctx.get("toolCalls") or [])
    timings = [step_timing_dto(e) for e in ctx.get("stepTimings") or []]
    components: list[Any] = list(ctx.get("rendered_components") or [])
    if colleague:
        calls = [_only(c, _COLLEAGUE_CALL_KEYS) for c in calls]
        timings = [_only(t, _COLLEAGUE_TIMING_KEYS) for t in timings]
        # A card may quote anything the run read -- none of it is a label.
        components = []
    return LiveRunDTO(
        id=str(run.id),
        state=run.state,
        source=run.source,
        created_at=run.created_at.isoformat(),
        trigger=LiveTriggerDTO(
            kind=str(trigger["kind"]), label=trigger["label"], timezone=trigger.get("timezone")
        ),
        output=str(output) if output else None,
        steps=int(ctx.get("steps") or 0),
        tool_calls=calls,
        step_timings=timings,
        started_by=trigger.get("started_by"),
        redacted=colleague,
        rendered_components=components,
    )


@router.get("/agents/{agent_id}/live", response_model=LiveTimelineDTO)
async def get_live(
    request: Request,
    agent_id: uuid.UUID,
    db: DbSession,
    actor: _Actor,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    all_runs: Annotated[bool, Query(alias="allRuns")] = False,
) -> LiveTimelineDTO:
    authority = await authority_for_principal(request, db, actor.principal)
    agent = await reachable_chat_agent(
        db, authority=authority, scope=actor.scope, agent_id=agent_id
    )
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    # Run reach (`run:view`, as on every run door): without it the timeline
    # holds only the caller's own conversation, and "All runs" -- a run read
    # of colleagues' runs -- is refused like a missing manager right.
    reaches_runs = await reaches_agent_runs(db, authority=authority, scope=actor.scope, agent=agent)
    manages = reaches_runs and await may_manage_agent(
        db, agent=agent, authority=authority, scope=actor.scope
    )
    if all_runs and not manages:
        raise HTTPException(status.HTTP_403_FORBIDDEN, _ALL_RUNS_REFUSED)
    before_at, before_id = _parse_cursor(before) if before else (None, None)
    session = await get_or_create_live_session(
        db, tenant_id=actor.principal.tenant_id, member_id=actor.member.id, agent_id=agent.id
    )
    # One more than asked: whether an older page exists, without a count.
    items = await live_timeline(
        db,
        member=actor.member,
        agent=agent,
        session=session,
        before=before_at,
        before_id=before_id,
        limit=limit + 1,
        all_runs=all_runs,
        reaches_runs=reaches_runs,
    )
    has_more = len(items) > limit
    items = items[-limit:]
    triggers = await trigger_names(
        db, [i.run for i in items if i.run is not None], tenant_id=agent.tenant_id
    )
    whose = await whose_runs(db, [i.run.id for i in items if i.run is not None])
    out: list[LiveItemDTO] = []
    for item in items:
        out.append(
            LiveItemDTO(
                kind=item.kind,
                at=item.at.isoformat(),
                message=(
                    await _message_dto(item.message, db, actor.principal.tenant_id)
                    if item.message is not None
                    else None
                ),
                run=(
                    _run_dto(
                        item.run,
                        triggers[item.run.id],
                        colleague=whose[item.run.id].is_colleagues(actor.member.subject),
                    )
                    if item.run is not None
                    else None
                ),
            )
        )
    session_id = str(session.id)
    await db.commit()
    return LiveTimelineDTO(
        session_id=session_id,
        items=out,
        next_before=_cursor(items[0]) if has_more and items else None,
        may_view_all_runs=manages,
    )
