"""The tenant's two speech models: one for dictation (speech to text), one for
reading answers aloud (text to speech). Either may be unset; the chat hides
the button then.

Next to them, the per-member daily allowance that bounds speech spend (speech
costs show 0, so the token budgets do not see it): minutes of dictation and
characters read aloud per member per day; 0 switches the feature off for
members. PUT replaces the whole setting: an omitted model id is cleared, an
omitted allowance goes back to its default.

Kept in `Organization.settings["speech"]` (no migration), as the preview
setting is. Only a settings manager reads or changes it here; the chat learns
what it may show from `GET /chat/audio/capabilities`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.authz.permissions import MANAGE, SETTINGS, VIEW, perm
from oc8.modelrouter.speech import (
    DAILY_LIMIT_FOR,
    DEFAULT_DAILY_LIMIT,
    SETTING_FOR,
    SETTINGS_KEY,
    SPEECH_TO_TEXT,
    TEXT_TO_SPEECH,
    capability_of,
    daily_limit,
)
from oc8.schemas.base import CamelModel

router = APIRouter()


class SpeechSettings(CamelModel):
    speech_to_text_model_id: str | None = None
    text_to_speech_model_id: str | None = None
    #: Per member per day, on the organisation's clock; 0 = off for members.
    daily_transcription_minutes: int = Field(
        default=DEFAULT_DAILY_LIMIT[SPEECH_TO_TEXT], ge=0, le=24 * 60
    )
    daily_read_aloud_characters: int = Field(
        default=DEFAULT_DAILY_LIMIT[TEXT_TO_SPEECH], ge=0, le=10_000_000
    )


async def _organization(db: DbSession, principal: CurrentPrincipal) -> m.Organization:
    organization = await db.get(m.Organization, principal.tenant_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return organization


def _stored(organization: m.Organization) -> SpeechSettings:
    picks = (organization.settings or {}).get(SETTINGS_KEY)
    picks = picks if isinstance(picks, dict) else {}
    return SpeechSettings(
        speech_to_text_model_id=picks.get(SETTING_FOR[SPEECH_TO_TEXT]) or None,
        text_to_speech_model_id=picks.get(SETTING_FOR[TEXT_TO_SPEECH]) or None,
        daily_transcription_minutes=daily_limit(organization, SPEECH_TO_TEXT),
        daily_read_aloud_characters=daily_limit(organization, TEXT_TO_SPEECH),
    )


async def _checked(
    db: DbSession, tenant_id: uuid.UUID, raw: str | None, capability: str
) -> str | None:
    """`raw` as a model id of this tenant with `capability`, or 422."""
    if raw is None:
        return None
    try:
        cfg = await db.get(m.ModelConfig, uuid.UUID(raw))
    except ValueError:
        cfg = None
    if cfg is None or cfg.tenant_id != tenant_id or capability_of(cfg) != capability:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"not a {capability.replace('_', '-')} model of this organization",
        )
    return str(cfg.id)


@router.get(
    "/settings/speech",
    response_model=SpeechSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, VIEW)))],
)
async def get_speech_settings(db: DbSession, principal: CurrentPrincipal) -> SpeechSettings:
    return _stored(await _organization(db, principal))


@router.put(
    "/settings/speech",
    response_model=SpeechSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, MANAGE)))],
)
async def put_speech_settings(
    body: SpeechSettings, db: DbSession, principal: CurrentPrincipal
) -> SpeechSettings:
    organization = await _organization(db, principal)
    picks = {
        SETTING_FOR[SPEECH_TO_TEXT]: await _checked(
            db, principal.tenant_id, body.speech_to_text_model_id, SPEECH_TO_TEXT
        ),
        SETTING_FOR[TEXT_TO_SPEECH]: await _checked(
            db, principal.tenant_id, body.text_to_speech_model_id, TEXT_TO_SPEECH
        ),
        DAILY_LIMIT_FOR[SPEECH_TO_TEXT]: body.daily_transcription_minutes,
        DAILY_LIMIT_FOR[TEXT_TO_SPEECH]: body.daily_read_aloud_characters,
    }
    organization.settings = {**(organization.settings or {}), SETTINGS_KEY: picks}
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="organization.settings.updated",
        resource={"organization_id": str(organization.id), "by": principal.subject, **picks},
        principal=principal,
    )
    await db.commit()
    return _stored(organization)
