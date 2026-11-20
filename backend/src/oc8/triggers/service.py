"""Trigger Service (§8.4): CRUD + cron-schedule computation."""

from __future__ import annotations

import random
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import CroniterBadCronError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.connections.access import trigger_fingerprint
from oc8.modelrouter.subscription_guard import assert_manual_only_compatible
from oc8.models.core import Agent
from oc8.models.credentials import Credential
from oc8.models.triggers import Trigger
from oc8.triggers.zones import (
    UnknownTimezone,
    next_fire_utc,
    organisation_timezone,
    valid_timezone,
)

#: 256 bits of CSPRNG entropy, URL-safe -- for a kind='webhook' row this
#: token is the ENTIRE auth boundary (no signature, no header, per the
#: n8n-Webhook-node model this trigger kind copies), so it needs to be
#: unguessable on its own, not just unique.
_WEBHOOK_TOKEN_BYTES = 32


_ONE_POLLED_TRIGGER = "the agent already has a trigger on this polled event"
_CAPA_NOT_ENABLED = "the event's capa is not enabled"


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """Whether `exc` is a violation of the constraint or index `constraint`
    (asyncpg names it on the driver error; its message names it too)."""
    driver = getattr(exc.orig, "__cause__", None) or exc.orig
    if getattr(driver, "constraint_name", None) == constraint:
        return True
    return f'"{constraint}"' in str(exc.orig)


class InvalidTriggerConfig(ValueError):
    """Raised for a bad cron expression, an unknown kind, or a kind whose
    required fields (cron_expression, or event_source+event_type) are
    missing -- always raised before any row reaches the database, so the
    caller never sees a raw IntegrityError from ck_trigger_kind_fields."""


def compute_next_run(
    cron_expression: str,
    *,
    after: datetime,
    timezone: str | None = None,
    jitter_seconds: int = 60,
) -> datetime:
    """Next fire time for `cron_expression` after `after`, read in `timezone`
    (an IANA name; None = UTC, what every trigger saved before triggers carried
    a zone fires in), plus a random 0..jitter_seconds offset to avoid a
    thundering herd across tenants sharing common cron patterns (e.g. every
    hour on the hour). Returned in UTC. DST edges: `triggers.zones`."""
    try:
        base = next_fire_utc(cron_expression, timezone=timezone, after=after)
    except UnknownTimezone as exc:
        raise InvalidTriggerConfig(str(exc)) from exc
    except (CroniterBadCronError, ValueError) as exc:
        raise InvalidTriggerConfig(f"invalid cron expression: {exc}") from exc
    return base + timedelta(seconds=random.randint(0, jitter_seconds))


async def _cron_timezone(db: AsyncSession, tenant_id: uuid.UUID, timezone: str | None) -> str:
    """The zone a new schedule is read in: the one given, else the
    organisation's own."""
    if timezone:
        try:
            return valid_timezone(timezone)
        except UnknownTimezone as exc:
            raise InvalidTriggerConfig(str(exc)) from exc
    return await organisation_timezone(db, tenant_id)


async def _check_polled_target(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    event_source: str,
    event_type: str,
    target: uuid.UUID | None,
    existing: bool = False,
) -> bool:
    """For an event this tenant's capas declare as polled
    (`oc8.events.sources`): refuse a target that is not the organisation login
    the agent is fixed to for the capa's tools. Returns whether the event is
    polled; a pushed event takes no target. `existing`: re-checking a polled
    trigger already saved, whose capa may have been switched off since."""
    from oc8.events.sources import fixed_login, polled_event_spec

    spec = await polled_event_spec(db, event_source, event_type)
    if spec is None:
        if existing and target is not None:
            raise InvalidTriggerConfig(_CAPA_NOT_ENABLED)
        if target is not None:
            raise InvalidTriggerConfig("only a polled event listens on a login")
        return False
    if spec.registered is None:
        raise InvalidTriggerConfig("no poller is available for this event")
    if spec.tool_key is None:
        raise InvalidTriggerConfig("the event's capa has no login tool")
    if target is None:
        raise InvalidTriggerConfig("a polled event needs the login it listens on")
    credential = await db.get(Credential, target)
    if credential is None:
        raise InvalidTriggerConfig("the login it listens on does not exist")
    if credential.owner_member_id is not None:
        raise InvalidTriggerConfig("a polled event listens on an organisation login only")
    if credential.credential_type != spec.registered.credential_type:
        raise InvalidTriggerConfig(
            f"a polled event of this kind listens on a {spec.registered.credential_type} login"
        )
    if await fixed_login(db, agent_id, spec.tool_key) != target:
        raise InvalidTriggerConfig(
            "a polled event listens on the login the agent is fixed to for its tools"
        )
    return True


async def create_trigger(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    kind: str,
    task_text: str,
    cron_expression: str | None = None,
    timezone: str | None = None,
    event_source: str | None = None,
    event_type: str | None = None,
    declared_events: set[tuple[str, str]] | None = None,
    jitter_seconds: int = 60,
    created_by_member_id: uuid.UUID | None = None,
    acts_for_creator: bool = False,
    event_target_credential_id: uuid.UUID | None = None,
    event_config: dict[str, Any] | None = None,
) -> Trigger:
    """`event_target_credential_id` and `event_config` are for a polled event
    only (`oc8.events.sources`): the organisation login the agent is fixed to
    for the event capa's tools, required; and the poller's settings. An agent
    has at most one trigger per polled event."""
    next_run_at = None
    polled = False
    zone: str | None = None
    if kind == "cron":
        if cron_expression is None:
            raise InvalidTriggerConfig("cron_expression is required for kind='cron'")
        # Without a zone named, the organisation's: a person writes "7:00"
        # meaning their own clock, not UTC.
        zone = await _cron_timezone(db, tenant_id, timezone)
        next_run_at = compute_next_run(
            cron_expression,
            after=datetime.now(tz=UTC),
            timezone=zone,
            jitter_seconds=jitter_seconds,
        )
    elif kind == "event":
        if event_source is None or event_type is None:
            raise InvalidTriggerConfig("event_source and event_type are required for kind='event'")
        if declared_events is not None and (event_source, event_type) not in declared_events:
            raise InvalidTriggerConfig("event_source and event_type must name a declared event")
        polled = await _check_polled_target(
            db,
            agent_id=agent_id,
            event_source=event_source,
            event_type=event_type,
            target=event_target_credential_id,
        )
        if polled and await db.scalar(
            select(Trigger.id).where(
                Trigger.agent_id == agent_id,
                Trigger.kind == "event",
                Trigger.event_source == event_source,
                Trigger.event_type == event_type,
            )
        ):
            raise InvalidTriggerConfig(_ONE_POLLED_TRIGGER)
    elif kind != "webhook":
        raise InvalidTriggerConfig(f"unknown trigger kind: {kind!r}")
    if not polled and (event_target_credential_id is not None or event_config):
        raise InvalidTriggerConfig("only a polled event listens on a login")

    # Generated here, never client-supplied -- a caller naming its own token
    # would defeat the "unguessable" half of the auth model. Collision odds
    # at 256 bits are cryptographically negligible; retrying past the
    # unique constraint on IntegrityError isn't worth the complexity.
    webhook_token = secrets.token_urlsafe(_WEBHOOK_TOKEN_BYTES) if kind == "webhook" else None

    trigger = Trigger(
        tenant_id=tenant_id,
        agent_id=agent_id,
        created_by_member_id=created_by_member_id,
        acts_for_creator=acts_for_creator and created_by_member_id is not None,
        kind=kind,
        task_text=task_text,
        cron_expression=cron_expression,
        timezone=zone,
        next_run_at=next_run_at,
        event_source=event_source,
        event_type=event_type,
        webhook_token=webhook_token,
        event_target_credential_id=event_target_credential_id if polled else None,
        event_config=dict(event_config or {}) if polled else {},
    )
    if polled:
        # Two creates at once both pass the check above; the partial unique
        # index (uq_trigger_polled_event) refuses the second. Inside a
        # savepoint so the caller's transaction survives the refusal.
        try:
            async with db.begin_nested():
                db.add(trigger)
                await db.flush()
        except IntegrityError as exc:
            if _violates(exc, "uq_trigger_polled_event"):
                raise InvalidTriggerConfig(_ONE_POLLED_TRIGGER) from exc
            raise
    else:
        db.add(trigger)
        await db.flush()
    if polled:
        from oc8.events.sources import reset_cursor

        # The first poll only records where the source is now.
        await reset_cursor(db, trigger)
    if created_by_member_id is not None:
        # Its creator saves it: their own account may serve it while the
        # agent stays as they see it now (connections.access.schedule_confirmed).
        trigger.access_fingerprint = await trigger_fingerprint(db, trigger)
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
    timezone: str | None = None,
    jitter_seconds: int = 60,
) -> Trigger:
    """`timezone` None keeps the zone the trigger has -- a row saved before
    triggers carried one stays in UTC until someone names a zone."""
    if task_text is not None:
        trigger.task_text = task_text
    if (
        enabled is True
        and not trigger.enabled
        and trigger.kind == "event"
        and trigger.event_source is not None
        and trigger.event_type is not None
        and await _check_polled_target(
            db,
            agent_id=trigger.agent_id,
            event_source=trigger.event_source,
            event_type=trigger.event_type,
            target=trigger.event_target_credential_id,
            existing=True,
        )
    ):
        # Re-enabling a polled trigger: its login is still the agent's (checked
        # above), and it starts from a fresh baseline instead of catching up.
        from oc8.events.sources import EVENT_TARGET_CHANGED, reset_cursor

        await reset_cursor(db, trigger)
        if trigger.last_skip_reason == EVENT_TARGET_CHANGED:
            trigger.last_skip_reason = None
    if enabled is not None:
        trigger.enabled = enabled
    if trigger.kind == "cron" and (cron_expression is not None or timezone):
        expression = cron_expression if cron_expression is not None else trigger.cron_expression
        zone = (
            await _cron_timezone(db, trigger.tenant_id, timezone) if timezone else trigger.timezone
        )
        assert expression is not None  # ck_trigger_kind_fields
        # Computed before either column changes: a refused value leaves the row as it was.
        next_run_at = compute_next_run(
            expression, after=datetime.now(tz=UTC), timezone=zone, jitter_seconds=jitter_seconds
        )
        trigger.cron_expression = expression
        trigger.timezone = zone
        trigger.next_run_at = next_run_at
    await db.flush()
    return trigger


async def delete_trigger(db: AsyncSession, trigger: Trigger) -> None:
    await db.delete(trigger)
    await db.flush()
