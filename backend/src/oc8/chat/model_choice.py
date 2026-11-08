"""Which models a chat may run on (spec §2.3).

The allowed set is the tenant's concrete, chat-enabled `ModelConfig` rows: an
Auto row is a policy, not a model -- choosing "Auto" means "no override" -- and
a model an admin switched off for chat ("Im Chat verfügbar") is never offered
nor honoured. The flag is read live, so switching a model off bites mid-chat.

This module only ever SUBTRACTS. Classification and locality are still decided
by the PDP and the router for whatever model is finally used.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m

_AUTO_PROVIDER = "auto"


@dataclass(frozen=True)
class ModelOption:
    id: str | None
    label: str
    provider: str
    locality: str
    is_default: bool


async def _chat_enabled_configs(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[m.ModelConfig]:
    rows = await db.execute(
        select(m.ModelConfig).where(
            m.ModelConfig.tenant_id == tenant_id,
            m.ModelConfig.provider != _AUTO_PROVIDER,
            m.ModelConfig.chat_enabled.is_(True),
        )
    )
    return list(rows.scalars())


async def allowed_model_ids(db: AsyncSession, *, agent: m.Agent) -> frozenset[uuid.UUID]:
    """Ids of every chat-enabled model of the agent's tenant."""
    configs = await _chat_enabled_configs(db, tenant_id=agent.tenant_id)
    return frozenset(c.id for c in configs)


async def is_allowed_override(
    db: AsyncSession, *, agent: m.Agent, model_config_id: uuid.UUID
) -> bool:
    return model_config_id in await allowed_model_ids(db, agent=agent)


async def model_options(
    db: AsyncSession, *, agent: m.Agent, default_model_id: uuid.UUID | None
) -> list[ModelOption]:
    """The picker's entries: "Auto" first, then the agent's default model (if
    chat-enabled), then the other chat-enabled models by label."""
    configs = await _chat_enabled_configs(db, tenant_id=agent.tenant_id)
    configs.sort(key=lambda c: (c.id != default_model_id, (c.display_name or c.model).lower()))
    options = [ModelOption(id=None, label="Auto", provider="", locality="", is_default=False)]
    options.extend(
        ModelOption(
            id=str(c.id),
            label=c.display_name or c.model,
            provider=c.provider,
            locality=c.locality,
            is_default=c.id == default_model_id,
        )
        for c in configs
    )
    return options
