"""Trigger Service (§8.4): CRUD + cron-schedule computation."""

from __future__ import annotations

import random
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from croniter import CroniterBadCronError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.connections.confirmation import stamp_trigger
from oc8.modelrouter.subscription_guard import assert_manual_only_compatible
from oc8.models.core import Agent
from oc8.models.credentials import Credential
from oc8.models.oauth import OAuthConnection
from oc8.models.triggers import Trigger
from oc8.triggers.zones import (
    UnknownTimezone,
    next_fire_utc,
    organisation_timezone,
    valid_timezone,
)

if TYPE_CHECKING:
    from oc8.events.sources import LoginTarget, PolledEventSpec

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


class PolledTargetMissing(InvalidTriggerConfig):
    """A polled event listens on the login the agent is fixed to for the
    event capa's tools, and the agent has none (`polled_event_target`)."""


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
    target: LoginTarget | None,
    existing: bool = False,
) -> PolledEventSpec | None:
    """For an event this tenant's capas declare as polled
    (`oc8.events.sources`): refuse a target that is not the organisation login
    the agent is fixed to for the capa's tools -- a credential of the poller's
    type, or an OAuth connection of the capa's provider. Returns the polled
    event, None for a pushed one, which takes no target. `existing`:
    re-checking a polled trigger already saved, whose capa may have been
    switched off since."""
    from oc8.events.sources import fixed_login, polled_event_spec

    spec = await polled_event_spec(db, event_source, event_type)
    if spec is None:
        if existing and target is not None:
            raise InvalidTriggerConfig(_CAPA_NOT_ENABLED)
        if target is not None:
            raise InvalidTriggerConfig("only a polled event listens on a login")
        return None
    if spec.registered is None:
        raise InvalidTriggerConfig("no poller is available for this event")
    if spec.tool_key is None:
        raise InvalidTriggerConfig("the event's capa has no login tool")
    if target is None:
        raise InvalidTriggerConfig("a polled event needs the login it listens on")
    if target.kind == "credential":
        credential = await db.get(Credential, target.id)
        if credential is None:
            raise InvalidTriggerConfig("the login it listens on does not exist")
        if credential.owner_member_id is not None:
            raise InvalidTriggerConfig("a polled event listens on an organisation login only")
        wanted = spec.registered.credential_type
        if wanted is None or credential.credential_type != wanted:
            raise InvalidTriggerConfig(
                f"a polled event of this kind listens on a {wanted} login"
                if wanted
                else "a polled event of this kind listens on an OAuth connection"
            )
    else:
        conn = await db.get(OAuthConnection, target.id)
        if conn is None:
            raise InvalidTriggerConfig("the login it listens on does not exist")
        if conn.owner_member_id is not None:
            raise InvalidTriggerConfig("a polled event listens on an organisation login only")
        if spec.provider is None or conn.provider != spec.provider:
            raise InvalidTriggerConfig(
                f"a polled event of this kind listens on a {spec.provider} connection"
                if spec.provider
                else "a polled event of this kind listens on a login, not an OAuth connection"
            )
    if await fixed_login(db, agent_id, spec.tool_key) != target:
        raise InvalidTriggerConfig(
            "a polled event listens on the login the agent is fixed to for its tools"
        )
    return spec


def _target(
    credential_id: uuid.UUID | None, oauth_connection_id: uuid.UUID | None
) -> LoginTarget | None:
    from oc8.events.sources import LoginTarget

    try:
        return LoginTarget.of(credential_id=credential_id, oauth_connection_id=oauth_connection_id)
    except ValueError as exc:
        raise InvalidTriggerConfig("a polled event listens on one login, not two") from exc


async def _event_config(
    db: AsyncSession,
    spec: PolledEventSpec,
    *,
    agent_id: uuid.UUID,
    target: LoginTarget,
    given: dict[str, Any] | None,
) -> dict[str, str]:
    """The trigger's settings for `spec`: only the keys its event declares,
    each a string of at most `TRIGGER_CONFIG_VALUE_MAX` characters; one left
    out or blank is filled from its `default`, or from its `default_from` key
    of the login's connection setup (`connection_setup_value`). A setting with
    `allowed_from` only takes a value of that setup list (`setting_refusal`)."""
    from oc8.capas.manifest import TRIGGER_CONFIG_VALUE_MAX
    from oc8.events.sources import connection_setup_value, setting_refusal

    given = dict(given or {})
    declared = {field.key: field for field in spec.config}
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise InvalidTriggerConfig(f"the event takes no setting {unknown[0]!r}")
    out: dict[str, str] = {}
    for key, field in declared.items():
        value = given.get(key)
        if value is not None and not isinstance(value, str):
            raise InvalidTriggerConfig(f"the setting {key!r} must be text")
        value = (value or "").strip()
        if len(value) > TRIGGER_CONFIG_VALUE_MAX:
            raise InvalidTriggerConfig(
                f"the setting {key!r} is longer than {TRIGGER_CONFIG_VALUE_MAX} characters"
            )
        if not value and field.default_from:
            value = await connection_setup_value(
                db, spec, agent_id=agent_id, target=target, key=field.default_from
            )
        out[key] = value or field.default
    _allowed, refused = await setting_refusal(
        db, spec, agent_id=agent_id, target=target, config=out
    )
    if refused is not None:
        raise InvalidTriggerConfig(refused)
    return out


async def polled_event_target(
    db: AsyncSession, *, agent_id: uuid.UUID, event_source: str, event_type: str
) -> LoginTarget | None:
    """The login a trigger of the agent on this event listens on: the
    organisation login the agent is fixed to for the event capa's tools. None
    for an event that is not polled; `PolledTargetMissing` when the agent has
    no such login. For callers that do not name the target themselves (the
    Copilot, and a trigger API request without one)."""
    from oc8.events.sources import fixed_login, polled_event_spec

    spec = await polled_event_spec(db, event_source, event_type)
    if spec is None or spec.tool_key is None:
        return None
    target = await fixed_login(db, agent_id, spec.tool_key)
    if target is None:
        raise PolledTargetMissing(
            "this event listens on the login the agent is fixed to for the "
            f"{spec.tool_key!r} tools, and the agent has none: choose one in its tool settings"
        )
    return target


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
    event_target_oauth_connection_id: uuid.UUID | None = None,
    event_target: LoginTarget | None = None,
    event_config: dict[str, Any] | None = None,
) -> Trigger:
    """The event target and `event_config` are for a polled event only: the
    organisation login the agent is fixed to for the event capa's tools,
    required -- named by its id (`event_target_credential_id` or
    `event_target_oauth_connection_id`) or as `event_target` -- and the
    poller's settings. An agent has at most one trigger per polled event."""
    target = event_target or _target(event_target_credential_id, event_target_oauth_connection_id)
    next_run_at = None
    polled = False
    config: dict[str, str] = {}
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
        spec = await _check_polled_target(
            db,
            agent_id=agent_id,
            event_source=event_source,
            event_type=event_type,
            target=target,
        )
        polled = spec is not None
        if spec is not None:
            assert target is not None  # _check_polled_target
            config = await _event_config(
                db, spec, agent_id=agent_id, target=target, given=event_config
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
    if not polled and (target is not None or event_config):
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
        event_config=config,
    )
    if polled and target is not None:
        target.put_on(trigger)
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
        await stamp_trigger(db, trigger)
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
    event_config: dict[str, Any] | None = None,
    event_target_credential_id: uuid.UUID | None = None,
    event_target_oauth_connection_id: uuid.UUID | None = None,
) -> Trigger:
    """`timezone` None keeps the zone the trigger has -- a row saved before
    triggers carried one stays in UTC until someone names a zone.

    `event_config` and the target ids are for a polled trigger only: new
    settings (checked and filled as on create), and a new target, which must
    be the login the agent is fixed to now. A change to either
    starts a fresh baseline, so a different folder or login never replays its
    backlog."""
    if task_text is not None:
        trigger.task_text = task_text
    target = _target(event_target_credential_id, event_target_oauth_connection_id)
    if event_config is not None or target is not None:
        await _update_polled_settings(db, trigger, event_config=event_config, target=target)
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
            target=_saved_target(trigger),
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


def _saved_target(trigger: Trigger) -> LoginTarget | None:
    from oc8.events.sources import LoginTarget

    return LoginTarget.of_trigger(trigger)


async def polled_settings(
    db: AsyncSession,
    trigger: Trigger,
    *,
    event_config: dict[str, Any] | None,
    target: LoginTarget | None,
) -> tuple[LoginTarget, dict[str, str]]:
    """What a polled trigger's target and settings would be after an update
    naming `event_config` and `target` (None keeps each): checked and filled
    as on create, without changing the trigger. Lets a caller tell a real
    change from the same settings written differently."""
    saved = _saved_target(trigger)
    if trigger.kind != "event" or trigger.event_source is None or trigger.event_type is None:
        raise InvalidTriggerConfig("only a polled event listens on a login")
    if saved is None:
        # Its login was deleted or revoked (ON DELETE SET NULL): a polled
        # trigger takes the agent's new login, checked below; any other
        # event trigger never names one.
        from oc8.events.sources import polled_event_spec

        if await polled_event_spec(db, trigger.event_source, trigger.event_type) is None:
            raise InvalidTriggerConfig("only a polled event listens on a login")
        if target is None:
            raise InvalidTriggerConfig("a polled event needs the login it listens on")
    new_target = target if target is not None else saved
    assert new_target is not None
    spec = await _check_polled_target(
        db,
        agent_id=trigger.agent_id,
        event_source=trigger.event_source,
        event_type=trigger.event_type,
        target=new_target,
        existing=True,
    )
    assert spec is not None  # a saved target on an event no capa polls raised
    config = await _event_config(
        db,
        spec,
        agent_id=trigger.agent_id,
        target=new_target,
        given=event_config if event_config is not None else trigger.event_config,
    )
    return new_target, config


async def _update_polled_settings(
    db: AsyncSession,
    trigger: Trigger,
    *,
    event_config: dict[str, Any] | None,
    target: LoginTarget | None,
) -> None:
    from oc8.events.sources import EVENT_TARGET_CHANGED, reset_cursor

    new_target, config = await polled_settings(
        db, trigger, event_config=event_config, target=target
    )
    if new_target == _saved_target(trigger) and config == trigger.event_config:
        return
    if new_target != _saved_target(trigger) and trigger.last_skip_reason == EVENT_TARGET_CHANGED:
        trigger.last_skip_reason = None  # it listens on the agent's login again
    new_target.put_on(trigger)
    trigger.event_config = config
    await reset_cursor(db, trigger)


async def delete_trigger(db: AsyncSession, trigger: Trigger) -> None:
    await db.delete(trigger)
    await db.flush()
