"""Whether artifact previews in the chat may run their scripts (tenant-wide).

The chat shows an answer's HTML artifacts in a sandboxed frame; with this
off, every preview is static (no scripts). Kept in `Organization.settings`
under `preview_scripts`, default on. Every signed-in person may read it --
the chat needs it -- and only a settings manager may change it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.audit import append_event
from oc8.authz.permissions import MANAGE, SETTINGS, perm
from oc8.schemas.base import CamelModel

router = APIRouter()

SETTINGS_KEY = "preview_scripts"


class PreviewSettings(CamelModel):
    run_scripts: bool


async def _organization(db: DbSession, principal: CurrentPrincipal) -> m.Organization:
    organization = await db.get(m.Organization, principal.tenant_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return organization


@router.get("/settings/previews", response_model=PreviewSettings)
async def get_preview_settings(db: DbSession, principal: CurrentPrincipal) -> PreviewSettings:
    organization = await _organization(db, principal)
    return PreviewSettings(
        run_scripts=(organization.settings or {}).get(SETTINGS_KEY, True) is not False
    )


@router.put(
    "/settings/previews",
    response_model=PreviewSettings,
    dependencies=[Depends(require_permission(perm(SETTINGS, MANAGE)))],
)
async def put_preview_settings(
    body: PreviewSettings, db: DbSession, principal: CurrentPrincipal
) -> PreviewSettings:
    organization = await _organization(db, principal)
    organization.settings = {**(organization.settings or {}), SETTINGS_KEY: body.run_scripts}
    await append_event(
        db,
        tenant_id=principal.tenant_id,
        actor_type="operator",
        actor_id=None,
        category="admin",
        action="organization.settings.updated",
        resource={
            "organization_id": str(organization.id),
            "by": principal.subject,
            "preview_scripts": body.run_scripts,
        },
        principal=principal,
    )
    await db.commit()
    return body
