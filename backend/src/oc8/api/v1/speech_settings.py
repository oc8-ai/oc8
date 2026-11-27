"""The tenant's two speech models: one for dictation (speech to text), one for
reading answers aloud (text to speech). Either may be unset; the chat hides
the button then.

Kept in `Organization.settings["speech"]` (no migration), as the preview
setting is. Only a settings manager reads or changes it here; the chat learns
what it may show from `GET /chat/audio/capabilities`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.authz.permissions import MANAGE, SETTINGS, VIEW, perm
from oc8.modelrouter.speech import (
    SETTING_FOR,
    SETTINGS_KEY,
    SPEECH_TO_TEXT,
    TEXT_TO_SPEECH,
    capability_of,
)
from oc8.schemas.base import CamelModel

router = APIRouter()


class SpeechSettings(CamelModel):
    speech_to_text_model_id: str | None = None
    text_to_speech_model_id: str | None = None


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
