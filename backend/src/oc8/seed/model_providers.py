"""Built-in model_adapter plugins auto-enabled for every tenant (§9.2 BYOK).

Auto-enabled rather than surfaced through a plugin-marketplace/consent UI
(neither exists yet): a first-party model_adapter plugin's only surface is
contributing a `ProviderEntry` (see modelrouter/registry.py's own docstring)
-- it runs no hook code and needs no scoped grant, so there is nothing here
for a tenant to meaningfully consent to. Silently skipped (not an error) on a
deployment whose plugins path does not carry the folder at all.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.discovery import find_plugin
from oc8.capas.lifecycle import consent_required, enable_plugin
from oc8.capas.service import install_plugin

BUILTIN_MODEL_PROVIDER_PLUGINS: tuple[str, ...] = ("opaas_ai_provider",)


async def seed_builtin_model_providers(db: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Install + enable each plugin in `BUILTIN_MODEL_PROVIDER_PLUGINS` for
    this tenant. Idempotent per tenant (skips a plugin already installed for
    it), mirroring `seed_department_templates`. Add/flush only -- never
    commits; the caller owns the commit."""
    for name in BUILTIN_MODEL_PROVIDER_PLUGINS:
        existing = (
            await db.execute(
                select(m.Capa).where(m.Capa.name == name, m.Capa.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        discovered = find_plugin(name)
        if discovered is None or not discovered.valid or discovered.manifest is None:
            continue
        version = await install_plugin(db, tenant_id=tenant_id, manifest_data=discovered.manifest)
        await enable_plugin(
            db,
            tenant_id=tenant_id,
            capa_id=version.capa_id,
            granted_permissions=consent_required(version),
        )
