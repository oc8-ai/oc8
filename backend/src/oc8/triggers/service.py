"""Trigger Service (§8.4): CRUD + cron-schedule computation."""

from __future__ import annotations

import random
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from croniter import CroniterBadCronError, croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.modelrouter.subscription_guard import assert_manual_only_compatible
from oc8.models.core import Agent
from oc8.models.triggers import Trigger

#: 256 bits of CSPRNG entropy, URL-safe -- for a kind='webhook' row this
#: token is the ENTIRE auth boundary (no signature, no header, per the
#: n8n-Webhook-node model this trigger kind copies), so it needs to be
#: unguessable on its own, not just unique.
_WEBHOOK_TOKEN_BYTES = 32


class InvalidTriggerConfig(ValueError):
    """Raised for a bad cron expression, an unknown kind, or a kind whose
    required fields (cron_expression, or event_source+event_type) are
    missing -- always raised before any row reaches the database, so the
    caller never sees a raw IntegrityError from ck_trigger_kind_fields."""


def compute_next_run(
    cron_expression: str, *, after: datetime, jitter_seconds: int = 60
) -> datetime:
    """Next fire time for `cron_expression` after `after`, plus a random
    0..jitter_seconds offset to avoid a thundering herd across tenants
    sharing common cron patterns (e.g. every hour on the hour)."""
    try:
        base = cast(datetime, croniter(cron_expression, after).get_next(datetime))
    except (CroniterBadCronError, ValueError) as exc:
        raise InvalidTriggerConfig(f"invalid cron expression: {exc}") from exc
    return base + timedelta(seconds=random.randint(0, jitter_seconds))


async def create_trigger(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    kind: str,
    task_text: str,
    cron_expression: str | None = None,
    event_source: str | None = None,
    event_type: str | None = None,
    declared_events: set[tuple[str, str]] | None = None,
    jitter_seconds: int = 60,
) -> Trigger:
    next_run_at = None
    if kind == "cron":
        if cron_expression is None:
            raise InvalidTriggerConfig("cron_expression is required for kind='cron'")
        next_run_at = compute_next_run(
            cron_expression, after=datetime.now(tz=UTC), jitter_seconds=jitter_seconds
        )
    elif kind == "event":
        if event_source is None or event_type is None:
            raise InvalidTriggerConfig(
                "event_source and event_type are required for kind='event'"
            )
        if declared_events is not None and (event_source, event_type) not in declared_events:
            raise InvalidTriggerConfig("event_source and event_type must name a declared event")
    elif kind != "webhook":
        raise InvalidTriggerConfig(f"unknown trigger kind: {kind!r}")

    # Generated here, never client-supplied -- a caller naming its own token
    # would defeat the "unguessable" half of the auth model. Collision odds
    # at 256 bits are cryptographically negligible; retrying past the
    # unique constraint on IntegrityError isn't worth the complexity.
    webhook_token = secrets.token_urlsafe(_WEBHOOK_TOKEN_BYTES) if kind == "webhook" else None

    trigger = Trigger(
        tenant_id=tenant_id,
        agent_id=agent_id,
        kind=kind,
        task_text=task_text,
        cron_expression=cron_expression,
        next_run_at=next_run_at,
        event_source=event_source,
        event_type=event_type,
        webhook_token=webhook_token,
    )
    db.add(trigger)
    await db.flush()
    # The manual-trigger-only check for ChatGPT-subscription-backed models
    # (`oc8.modelrouter.subscription_guard`) lives HERE, not in the HTTP route,
    # because this function is the one funnel every trigger creation goes
    # through -- `POST /agents/{id}/triggers` and the Copilot's
    # `trigger.create` operation (`oc8.copilot.capabilities.apply_operation`)
    # both land here, and the Copilot path had no guard at all while it lived
    # in the route. Same "one funnel" reasoning as `RunRepository.transition`
    # being the single place a run is closed.
    #
    # It runs AFTER the flush above, deliberately: a new Trigger is always
    # created enabled, so checking first would find zero enabled triggers on a
    # brand-new agent and wave a lone, freshly-created trigger straight
    # through. Every caller's transaction rolls back on the raised exception
    # (`tenant_session`), so a rejected trigger is flushed but never committed.
    agent = await db.get(Agent, agent_id)
    await assert_manual_only_compatible(
        db,
        agent_id=agent_id,
        model_config_id=agent.model_config_id if agent is not None else None,
    )
    return trigger


async def get_trigger(db: AsyncSession, *, trigger_id: uuid.UUID) -> Trigger | None:
    """None for a Copilot follow-up (chat_session_id set): those belong to one
    member and are managed only through /copilot/followups."""
    trigger = await db.get(Trigger, trigger_id)
    if trigger is None or trigger.chat_session_id is not None:
        return None
    return trigger


async def get_trigger_by_webhook_token(db: AsyncSession, *, token: str) -> Trigger | None:
    """Looked up within a session already bound to the right tenant -- the
    caller (POST /webhooks/{token}) has no tenant context yet and tries each
    tenant's session in turn, same all-tenant discovery loop
    handle_inbound_event already uses for kind='event' fan-out."""
    return (
        await db.execute(
            select(Trigger).where(Trigger.kind == "webhook", Trigger.webhook_token == token)
        )
    ).scalar_one_or_none()


async def list_triggers_for_agent(db: AsyncSession, *, agent_id: uuid.UUID) -> list[Trigger]:
    # Copilot follow-ups are a member's own and never listed here (get_trigger).
    result = await db.execute(
        select(Trigger).where(Trigger.agent_id == agent_id, Trigger.chat_session_id.is_(None))
    )
    return list(result.scalars().all())


async def update_trigger(
    db: AsyncSession,
    trigger: Trigger,
    *,
    task_text: str | None = None,
    enabled: bool | None = None,
    cron_expression: str | None = None,
    jitter_seconds: int = 60,
) -> Trigger:
    if task_text is not None:
        trigger.task_text = task_text
    if enabled is not None:
        trigger.enabled = enabled
    if cron_expression is not None and trigger.kind == "cron":
        trigger.cron_expression = cron_expression
        trigger.next_run_at = compute_next_run(
            cron_expression, after=datetime.now(tz=UTC), jitter_seconds=jitter_seconds
        )
    await db.flush()
    return trigger


async def delete_trigger(db: AsyncSession, trigger: Trigger) -> None:
    await db.delete(trigger)
    await db.flush()
