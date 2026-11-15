"""Built-in runtime_adapter plugins auto-enabled for every tenant (§8.7).

Auto-enabled rather than surfaced through a plugin-marketplace/consent UI
(neither exists yet), mirroring `seed_builtin_model_providers`: these are
first-party runtimes shipping with oc8 itself, not a third-party plugin a
tenant needs to review before granting `sandbox:run`. Without this, `GET
/runtimes` -- and therefore every runtime picker, including at hire time --
would show only the two built-in defaults until an operator separately
installs each one via the Plugins page. Silently skipped (not an error) on a
deployment whose plugins path does not carry a given folder at all.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.discovery import find_plugin
from oc8.capas.lifecycle import consent_required, enable_plugin
from oc8.capas.service import install_plugin

BUILTIN_RUNTIME_ADAPTER_PLUGINS: tuple[str, ...] = (
    "claude_code_runtime",
    "codex_runtime",
    "opencode_runtime",
    "nanoclaw_runtime",
)


async def seed_runtime_adapters(db: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Install + enable each plugin in `BUILTIN_RUNTIME_ADAPTER_PLUGINS` for
    this tenant. Idempotent per tenant (skips a plugin already installed for
    it), mirroring `seed_builtin_model_providers`. Add/flush only -- never
    commits; the caller owns the commit."""
    for name in BUILTIN_RUNTIME_ADAPTER_PLUGINS:
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
