"""Copilot follow-ups (design §7a.2, §7a.4): trigger rows that fire as a turn
in the member's own conversation, never as a free-standing run -- that is the
only way a follow-up inherits the member's authority without a new grant path.

A follow-up carries no token: no `operator_role`, and `originating_operator`
is the member's own subject. Authority is the member's stored terms only,
exactly as a messenger turn resolves (§7a.2)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.assistant import _load_assistant
from oc8.authz.authority import member_holds_assigned_permission
from oc8.authz.permissions import COPILOT_USE
from oc8.chat.modes import RESEARCH
from oc8.copilot.profile import get_or_create_profile
from oc8.copilot.responsibilities import get_owned
from oc8.copilot.schedule import MAX_ACTIVE_FOLLOWUPS, FollowupRejected, FollowupSpec, next_fire
from oc8.modelrouter.subscription_guard import (
    SubscriptionModelNotManualOnly,
    assert_manual_only_compatible,
)
from oc8.runtime.states import RunState

logger = logging.getLogger(__name__)


async def max_active_followups(db: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """The tenant's per-member cap on active follow-ups: the admin-set
    `organization.settings["copilot_max_active_followups"]` (1..100), else the
    default. A missing org row or an invalid stored value reads as the default."""
    org = await db.get(m.Organization, tenant_id)
    stored = org.settings.get("copilot_max_active_followups") if org is not None else None
    if isinstance(stored, int) and not isinstance(stored, bool) and 1 <= stored <= 100:
        return stored
    return MAX_ACTIVE_FOLLOWUPS


async def _active_count(db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID) -> int:
    owned = select(m.Responsibility.id).where(
        m.Responsibility.tenant_id == tenant_id, m.Responsibility.member_id == member_id
    )
    return int(
        await db.scalar(
            select(func.count())
            .select_from(m.Trigger)
            .where(
                m.Trigger.tenant_id == tenant_id,
                m.Trigger.enabled.is_(True),
                m.Trigger.responsibility_id.in_(owned),
            )
        )
        or 0
    )


async def schedule_followup(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    member_id: uuid.UUID,
    responsibility_id: uuid.UUID,
    assistant_id: uuid.UUID,
    spec: FollowupSpec,
    prompt: str,
    member_subject: str,
) -> m.Trigger:
    r = await get_owned(
        db, tenant_id=tenant_id, member_id=member_id, responsibility_id=responsibility_id
    )
    if r is None:
        raise FollowupRejected("responsibility not found")
    if r.state not in ("active", "waiting"):
        raise FollowupRejected(f"the responsibility is {r.state} -- resume it first")
    # fire_followup skips a member without it; refusing here keeps a follow-up
    # from being saved that could never fire.
    member = await db.get(m.OrgMember, member_id)
    if member is None or not await member_holds_assigned_permission(
        db, member=member, permission=COPILOT_USE
    ):
        raise FollowupRejected(
            "follow-ups need an assigned role with Copilot access -- ask an administrator"
        )
    cap = await max_active_followups(db, tenant_id=tenant_id)
    if await _active_count(db, tenant_id=tenant_id, member_id=member_id) >= cap:
        raise FollowupRejected(f"you already have {cap} active follow-ups -- end one first")
    now = dt.datetime.now(tz=dt.UTC)
    trigger = m.Trigger(
        tenant_id=tenant_id,
        agent_id=assistant_id,
        kind=spec.kind,
        task_text=prompt.strip()[:2000] or r.title,
        cron_expression=spec.cron_expression,
        next_run_at=spec.run_at
        if spec.kind == "once"
        else next_fire(spec.cron_expression or "", timezone=spec.timezone, after=now),
        chat_session_id=r.chat_session_id,
        responsibility_id=r.id,
        timezone=spec.timezone,
        ends_at=spec.ends_at,
        followup_purpose=spec.purpose,
    )
    db.add(trigger)
    await db.flush()
    # Same guard and order as triggers.service.create_trigger: checked after
    # the flush, so this follow-up itself counts as the enabled trigger. A tool
    # call turns the rejection into an ERROR and the run carries on, so the
    # flushed row is removed here rather than left to a rollback.
    assistant = await _load_assistant(db, tenant_id=tenant_id)
    try:
        await assert_manual_only_compatible(
            db,
            agent_id=assistant_id,
            model_config_id=(
                assistant.model_config_id
                if assistant is not None and assistant.id == assistant_id
                else None
            ),
        )
    except SubscriptionModelNotManualOnly as exc:
        await db.delete(trigger)
        await db.flush()
        raise FollowupRejected(
            "the Copilot's model signs in with a personal ChatGPT subscription, which is "
            "licensed for manual use only, so it cannot run follow-ups -- ask an "
            "administrator to connect the Copilot model with an API key"
        ) from exc
    from oc8.audit import append_event

    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="agent",
        actor_id=assistant_id,
        category="copilot",
        action="copilot.followup_scheduled",
        resource={
            "trigger_id": str(trigger.id),
            "responsibility_id": str(r.id),
            "member_id": str(member_id),
        },
        originating_operator=member_subject,
    )
    return trigger


async def list_followups(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID
) -> list[m.Trigger]:
    owned = select(m.Responsibility.id).where(
        m.Responsibility.tenant_id == tenant_id, m.Responsibility.member_id == member_id
    )
    rows = await db.execute(
        select(m.Trigger)
        .where(m.Trigger.tenant_id == tenant_id, m.Trigger.responsibility_id.in_(owned))
        .order_by(m.Trigger.enabled.desc(), m.Trigger.next_run_at.asc().nulls_last())
    )
    return list(rows.scalars())


async def cancel_followup(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, trigger_id: uuid.UUID
) -> bool:
    for t in await list_followups(db, tenant_id=tenant_id, member_id=member_id):
        if t.id == trigger_id:
            t.enabled = False
            t.last_skip_reason = None
            await db.flush()
            return True
    return False


async def _session_busy(db: AsyncSession, *, tenant_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(m.AgentRun.id)
        .where(
            m.AgentRun.tenant_id == tenant_id,
            m.AgentRun.source == "chat",
            m.AgentRun.state.in_(
                [
                    RunState.QUEUED.value,
                    RunState.RUNNING.value,
                    RunState.WAITING_FOR_INPUT.value,
                    RunState.WAITING_FOR_APPROVAL.value,
                ]
            ),
            m.AgentRun.context["chat_session_id"].astext == str(session_id),
        )
        .limit(1)
    )
    return found is not None


async def live_binding_external_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID, channel: str
) -> str | None:
    return await db.scalar(
        select(m.ApprovalChannelBinding.external_id)
        .where(
            m.ApprovalChannelBinding.tenant_id == tenant_id,
            m.ApprovalChannelBinding.channel == channel,
            m.ApprovalChannelBinding.member_id == member_id,
            m.ApprovalChannelBinding.revoked_at.is_(None),
            m.ApprovalChannelBinding.external_id.is_not(None),
        )
        .limit(1)
    )


def _advance(trigger: m.Trigger, now: dt.datetime) -> None:
    trigger.last_run_at = now
    if trigger.kind == "once":
        trigger.enabled = False
        return
    nxt = next_fire(trigger.cron_expression or "", timezone=trigger.timezone or "UTC", after=now)
    if trigger.ends_at is not None and nxt > trigger.ends_at:
        trigger.enabled = False
    trigger.next_run_at = nxt


async def fire_followup(db: AsyncSession, trigger: m.Trigger, *, tenant_id: uuid.UUID) -> str:
    """Fire one follow-up. Every non-"fired" outcome advances the schedule and
    records why, so a skipped follow-up never fires every tick."""
    now = dt.datetime.now(tz=dt.UTC)
    if trigger.ends_at is not None and now > trigger.ends_at:
        trigger.enabled = False
        trigger.last_skip_reason = "ended"
        await db.commit()
        return "ended"
    session = (
        await db.get(m.ChatSession, trigger.chat_session_id) if trigger.chat_session_id else None
    )
    if session is None:
        trigger.enabled = False
        trigger.last_skip_reason = "no_session"
        await db.commit()
        return "no_session"
    member = await db.get(m.OrgMember, session.member_id)
    if member is None or member.deleted_at is not None:
        trigger.enabled = False
        trigger.last_skip_reason = "no_member"
        await db.commit()
        return "no_member"
    outcome: str | None = None
    r: m.Responsibility | None = None
    if not await member_holds_assigned_permission(db, member=member, permission=COPILOT_USE):
        outcome = "no_permission"
    elif (
        await get_or_create_profile(db, tenant_id=tenant_id, member_id=member.id)
    ).paused_at is not None:
        outcome = "paused"
    else:
        r = (
            await get_owned(
                db,
                tenant_id=tenant_id,
                member_id=member.id,
                responsibility_id=trigger.responsibility_id,
            )
            if trigger.responsibility_id
            else None
        )
        if r is None or r.state != "active":
            outcome = "not_active"
        elif await _session_busy(db, tenant_id=tenant_id, session_id=session.id):
            outcome = "busy"
    if outcome is not None:
        trigger.last_skip_reason = outcome
        # A one-shot stays scheduled and is re-checked next tick; only a
        # recurring schedule moves on (spec: skipped with a recorded reason).
        if trigger.kind != "once":
            _advance(trigger, now)
        logger.info("follow-up %s skipped: %s", trigger.id, outcome)
        await db.commit()
        return outcome

    assert r is not None
    channel = external_id = None
    if r.origin_channel:
        external_id = await live_binding_external_id(
            db, tenant_id=tenant_id, member_id=member.id, channel=r.origin_channel
        )
        channel = r.origin_channel if external_id else None
        if external_id is None:
            logger.info("follow-up %s: no live %s binding, web only", trigger.id, r.origin_channel)
    trigger.last_skip_reason = None
    _advance(trigger, now)
    from oc8.chat.service import send_message

    await send_message(
        db,
        session=session,
        tenant_id=tenant_id,
        message=f"Follow-up for “{r.title}”: {trigger.task_text}",
        originating_operator=member.subject,
        operator_role=None,
        chat_channel=channel,
        chat_channel_external_id=external_id,
        role="followup",
        extra_context={
            "followup": {
                "responsibility_id": str(r.id),
                "trigger_id": str(trigger.id),
                # Shared by this turn and every wake-up after it (it rides
                # along with "followup"): the research delegation cap counts
                # per turn, not per run.
                "turn_id": str(uuid.uuid4()),
            }
        },
        # NULL and "check_in" are ordinary follow-ups; anything else -- research
        # or a value this release does not know -- fires read-only.
        followup_mode=None if trigger.followup_purpose in (None, "check_in") else RESEARCH,
    )  # commits via enqueue_run
    return "fired"


async def catch_up(db: AsyncSession, *, tenant_id: uuid.UUID, member_id: uuid.UUID) -> int:
    """After resume: one catch-up turn per responsibility that had a follow-up
    skipped because of the pause -- never one per missed occurrence."""
    fired = 0
    done: set[uuid.UUID] = set()
    for t in await list_followups(db, tenant_id=tenant_id, member_id=member_id):
        # Never re-enable: a follow-up the member ended, or whose responsibility
        # was closed, stays ended (cancel/close also clear last_skip_reason).
        if not t.enabled or t.last_skip_reason != "paused" or t.responsibility_id is None:
            continue
        if t.responsibility_id in done:
            t.last_skip_reason = None  # covered by this responsibility's one catch-up
            continue
        done.add(t.responsibility_id)
        t.last_skip_reason = None
        if await fire_followup(db, t, tenant_id=tenant_id) == "fired":
            fired += 1
        # fire_followup commits, which ends the transaction-local tenant binding.
        await db.execute(
            text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
        )
    await db.commit()
    await db.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    return fired


def is_quiet_followup(run: m.AgentRun, responsibility: m.Responsibility | None) -> bool:
    if not isinstance((run.context or {}).get("followup"), dict):
        return False
    if run.state != RunState.DONE.value:
        return False  # a failed follow-up must always tell the member
    return responsibility is None or responsibility.last_report_run_id != run.id


async def load_responsibility_for_run(
    db: AsyncSession, *, run: m.AgentRun
) -> m.Responsibility | None:
    raw = ((run.context or {}).get("followup") or {}).get("responsibility_id")
    if not raw:
        return None
    return await db.get(m.Responsibility, uuid.UUID(str(raw)))
