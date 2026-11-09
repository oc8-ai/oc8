"""CRUD for cron/event Triggers (§8.4): backs the frontend schedule editor
and (future) event-trigger UI."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.authz.permissions import MANAGE, TRIGGER, VIEW, perm
from oc8.authz.scope import scope_for_principal
from oc8.config import get_settings
from oc8.connections.access import trigger_fingerprint
from oc8.modelrouter.subscription_guard import (
    SubscriptionModelNotManualOnly,
    assert_manual_only_compatible,
)
from oc8.schemas.dto import TriggerDTO
from oc8.schemas.requests import CreateTriggerRequest, UpdateTriggerRequest
from oc8.triggers.service import (
    InvalidTriggerConfig,
    create_trigger,
    delete_trigger,
    get_trigger,
    list_triggers_for_agent,
    update_trigger,
)

router = APIRouter()


def _webhook_url(token: str) -> str:
    base = get_settings().oauth_redirect_base_url.rstrip("/")
    return f"{base}/api/v1/webhooks/{token}"


async def _owner_names(db: DbSession, triggers: list[m.Trigger]) -> dict[uuid.UUID, str]:
    ids = {t.created_by_member_id for t in triggers if t.created_by_member_id is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(m.OrgMember.id, m.OrgMember.display_name).where(m.OrgMember.id.in_(ids))
        )
    ).all()
    return {r.id: r.display_name for r in rows}


async def _needs_confirmation(
    db: DbSession, triggers: list[m.Trigger], caller: uuid.UUID | None
) -> set[uuid.UUID]:
    """The caller's own triggers that act through their account and whose
    agent changed since they last saved them (the runtime rule:
    connections.access.schedule_confirmed). Nobody else is asked."""
    out: set[uuid.UUID] = set()
    for t in triggers:
        if caller is None or t.created_by_member_id != caller:
            continue
        if t.kind in ("webhook", "event") and not t.acts_for_creator:
            continue
        current = await trigger_fingerprint(db, t)
        if t.access_fingerprint is None or t.access_fingerprint != current:
            out.add(t.id)
    return out


async def _to_dto(db: DbSession, t: m.Trigger, caller: uuid.UUID | None) -> TriggerDTO:
    flagged = await _needs_confirmation(db, [t], caller)
    return _dto_with(t, await _owner_names(db, [t]), needs_confirmation=flagged)


def _dto_with(
    t: m.Trigger, names: dict[uuid.UUID, str], *, needs_confirmation: set[uuid.UUID]
) -> TriggerDTO:
    return TriggerDTO(
        id=str(t.id),
        agent_id=str(t.agent_id),
        kind=t.kind,
        task_text=t.task_text,
        enabled=t.enabled,
        cron_expression=t.cron_expression,
        next_run_at=t.next_run_at.isoformat() if t.next_run_at else None,
        last_run_at=t.last_run_at.isoformat() if t.last_run_at else None,
        event_source=t.event_source,
        event_type=t.event_type,
        webhook_url=_webhook_url(t.webhook_token) if t.webhook_token else None,
        created_by_member_id=str(t.created_by_member_id) if t.created_by_member_id else None,
        created_by_name=names.get(t.created_by_member_id) if t.created_by_member_id else None,
        acts_for_creator=t.acts_for_creator,
        needs_confirmation=t.id in needs_confirmation,
    )


async def _caller_member_id(db: DbSession, principal: CurrentPrincipal) -> uuid.UUID | None:
    """Whose account the trigger's runs act through in "user" mode."""
    try:
        member, _scope = await scope_for_principal(db, principal, upsert=True)
    except PermissionError:
        return None
    return member.id if member is not None else None


def _changes_what_runs(trigger: m.Trigger, body: UpdateTriggerRequest) -> bool:
    return (
        (body.task_text is not None and body.task_text != trigger.task_text)
        or (
            body.cron_expression is not None
            and trigger.kind == "cron"
            and body.cron_expression != trigger.cron_expression
        )
        or body.enabled is True
    )


@router.post(
    "/agents/{agent_id}/triggers",
    response_model=TriggerDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(TRIGGER, MANAGE)))],
)
async def create_agent_trigger(
    agent_id: uuid.UUID, body: CreateTriggerRequest, db: DbSession, principal: CurrentPrincipal
) -> TriggerDTO:
    agent = await db.get(m.Agent, agent_id)
    if agent is None or agent.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    caller = await _caller_member_id(db, principal)
    try:
        trigger = await create_trigger(
            db,
            tenant_id=principal.tenant_id,
            agent_id=agent_id,
            kind=body.kind,
            task_text=body.task_text,
            cron_expression=body.cron_expression,
            event_source=body.event_source,
            event_type=body.event_type,
            created_by_member_id=caller,
            acts_for_creator=body.acts_for_creator,
        )
    except InvalidTriggerConfig as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    # The manual-trigger-only guard is NOT called here: it lives inside
    # `create_trigger` itself, the one funnel every trigger creation shares
    # (this route and the Copilot's `trigger.create` operation). This route
    # only maps its exception onto the HTTP status, exactly as it does for
    # InvalidTriggerConfig. A raised HTTPException propagates out to
    # tenant_session's rollback-on-exception, so the rejected trigger the
    # service flushed before checking never reaches the database.
    except SubscriptionModelNotManualOnly as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _to_dto(db, trigger, caller)


@router.get(
    "/agents/{agent_id}/triggers",
    response_model=list[TriggerDTO],
    dependencies=[Depends(require_permission(perm(TRIGGER, VIEW)))],
)
async def list_agent_triggers(
    agent_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> list[TriggerDTO]:
    rows = list(await list_triggers_for_agent(db, agent_id=agent_id))
    names = await _owner_names(db, rows)
    flagged = await _needs_confirmation(db, rows, await _caller_member_id(db, principal))
    return [_dto_with(t, names, needs_confirmation=flagged) for t in rows]


@router.patch(
    "/triggers/{trigger_id}",
    response_model=TriggerDTO,
    dependencies=[Depends(require_permission(perm(TRIGGER, MANAGE)))],
)
async def update_agent_trigger(
    trigger_id: uuid.UUID, body: UpdateTriggerRequest, db: DbSession, principal: CurrentPrincipal
) -> TriggerDTO:
    trigger = await get_trigger(db, trigger_id=trigger_id)
    if trigger is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trigger not found")
    caller = await _caller_member_id(db, principal)
    owner = trigger.created_by_member_id
    if body.acts_for_creator is True and owner is not None and caller != owner:
        # Letting a trigger borrow the creator's account is the creator's
        # decision alone.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "only the trigger's creator can let it act for them"
        )
    if owner is None:
        # Re-saving a schedule made before triggers recorded a creator.
        trigger.created_by_member_id = caller
    elif caller != owner and _changes_what_runs(trigger, body):
        # Whoever changes what a trigger does becomes the person it acts for,
        # so an edit can never run under the previous owner's account.
        trigger.created_by_member_id = caller
        trigger.acts_for_creator = False
    if body.acts_for_creator is not None:
        trigger.acts_for_creator = (
            body.acts_for_creator and trigger.created_by_member_id is not None
        )
    try:
        updated = await update_trigger(
            db,
            trigger,
            task_text=body.task_text,
            enabled=body.enabled,
            cron_expression=body.cron_expression,
        )
    except InvalidTriggerConfig as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if caller is not None and updated.created_by_member_id == caller:
        # Its creator saved it: they confirm the agent as it is now.
        updated.access_fingerprint = await trigger_fingerprint(db, updated)
        await db.flush()
    # The guard only matters when this update is turning the trigger ON
    # (`body.enabled is True`, whether that flips a disabled row or
    # re-affirms an already-enabled one). Disabling it (`enabled=False`), or
    # leaving `enabled` unset entirely -- which never touches the column,
    # whatever its current value -- can never newly violate anything the row
    # doesn't already satisfy, so those cases skip the guard call outright
    # rather than relying on it to be a no-op. Checked after the flush above
    # (mirroring create_agent_trigger) so a fresh enable is visible to the
    # guard's own-agent lookup; a raised HTTPException propagates out to
    # tenant_session's rollback-on-exception, so a rejected enable is never
    # committed.
    if body.enabled is True:
        agent = await db.get(m.Agent, updated.agent_id)
        model_config_id = agent.model_config_id if agent is not None else None
        try:
            await assert_manual_only_compatible(
                db, agent_id=updated.agent_id, model_config_id=model_config_id
            )
        except SubscriptionModelNotManualOnly as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return await _to_dto(db, updated, caller)


@router.delete(
    "/triggers/{trigger_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(TRIGGER, MANAGE)))],
)
async def delete_agent_trigger(
    trigger_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal
) -> None:
    trigger = await get_trigger(db, trigger_id=trigger_id)
    if trigger is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trigger not found")
    await delete_trigger(db, trigger)
