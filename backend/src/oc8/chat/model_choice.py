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
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.modelrouter.levels import CHAT_LEVEL_KEY, DEFAULT_LEVEL, as_level, default_display
from oc8.modelrouter.sampling import configured_effort

_AUTO_PROVIDER = "auto"


@dataclass(frozen=True)
class ModelOption:
    id: str | None
    label: str
    provider: str
    locality: str
    is_default: bool
    #: Whether the composer shows the reasoning-level control on this model.
    supports_reasoning: bool = False
    #: The level that applies without a chat pick, or "custom".
    default_effort: str = DEFAULT_LEVEL


async def _chat_enabled_configs(db: AsyncSession, *, tenant_id: uuid.UUID) -> list[m.ModelConfig]:
    rows = await db.execute(
        select(m.ModelConfig).where(
            m.ModelConfig.tenant_id == tenant_id,
            m.ModelConfig.provider != _AUTO_PROVIDER,
            m.ModelConfig.chat_enabled.is_(True),
        )
    )
    return list(rows.scalars())


async def chat_enabled_model_ids(db: AsyncSession, *, tenant_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """Ids of every chat-enabled model of the tenant (Ruling U1). The agent
    builder offers and accepts only these."""
    configs = await _chat_enabled_configs(db, tenant_id=tenant_id)
    return frozenset(c.id for c in configs)


async def allowed_model_ids(db: AsyncSession, *, agent: m.Agent) -> frozenset[uuid.UUID]:
    """Ids of every chat-enabled model of the agent's tenant."""
    return await chat_enabled_model_ids(db, tenant_id=agent.tenant_id)


async def is_allowed_override(
    db: AsyncSession, *, agent: m.Agent, model_config_id: uuid.UUID
) -> bool:
    return model_config_id in await allowed_model_ids(db, agent=agent)


def _reasoning_of(
    config: m.ModelConfig | None, definition: dict[str, Any] | None
) -> tuple[bool, str]:
    """(can it think, the level that applies without a chat pick). An Auto
    row only routes, so whether a level applies is decided per routed model."""
    if config is None or config.provider == _AUTO_PROVIDER:
        return False, DEFAULT_LEVEL
    return bool(config.supports_reasoning), default_display(configured_effort(config, definition))


async def model_options(
    db: AsyncSession,
    *,
    agent: m.Agent,
    default_model_id: uuid.UUID | None,
    definition: dict[str, Any] | None = None,
) -> list[ModelOption]:
    """The picker's entries: "Auto" first, then the agent's default model (if
    chat-enabled), then the other chat-enabled models by label. `definition`
    is the agent's pinned definition, whose `model_params.effort` is part of
    what each option's default level is."""
    configs = await _chat_enabled_configs(db, tenant_id=agent.tenant_id)
    configs.sort(key=lambda c: (c.id != default_model_id, (c.display_name or c.model).lower()))
    default_cfg = next((c for c in configs if c.id == default_model_id), None)
    if default_cfg is None and default_model_id is not None:
        # "Auto" runs on the agent's own model even when that one is not
        # offered for a pick.
        default_cfg = await db.get(m.ModelConfig, default_model_id)
    auto_thinks, auto_default = _reasoning_of(default_cfg, definition)
    options = [
        ModelOption(
            id=None,
            label="Auto",
            provider="",
            locality="",
            is_default=False,
            supports_reasoning=auto_thinks,
            default_effort=auto_default,
        )
    ]
    for c in configs:
        thinks, default = _reasoning_of(c, definition)
        options.append(
            ModelOption(
                id=str(c.id),
                label=c.display_name or c.model,
                provider=c.provider,
                locality=c.locality,
                is_default=c.id == default_model_id,
                supports_reasoning=thinks,
                default_effort=default,
            )
        )
    return options


def _requested_override(run: m.AgentRun | None) -> uuid.UUID | None:
    raw = (run.context or {}).get("model_override_id") if run is not None else None
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


async def apply_model_override(
    db: AsyncSession, *, run: m.AgentRun | None, agent: m.Agent, payload: dict[str, Any]
) -> dict[str, Any]:
    """The run's behavioural payload with the chat's model in place of the
    pinned one -- only when that model is still a chat-enabled model of the
    tenant. Every runtime reads its model through `resolve_version`, so this one
    seam is what makes the in-process engine, the isolated control plane and
    both gateways agree (and what `control_tools._model_locality` judges
    retrieval by)."""
    requested = _requested_override(run)
    if requested is None:
        return payload
    if not await is_allowed_override(db, agent=agent, model_config_id=requested):
        return payload
    return {**payload, "model_config_id": str(requested)}


async def audit_model_override(db: AsyncSession, *, run: m.AgentRun, agent: m.Agent) -> None:
    """One audit line when a run's requested model was refused. Called once at
    run start by the executor; the context flag keeps a resumed leg silent."""
    from oc8.audit import append_event
    from oc8.runtime.run_context import merge_context

    requested = _requested_override(run)
    if requested is None or (run.context or {}).get("model_override_checked"):
        return
    allowed = await is_allowed_override(db, agent=agent, model_config_id=requested)
    await merge_context(db, run, {"model_override_checked": True})
    if allowed:
        return
    await append_event(
        db,
        tenant_id=run.tenant_id,
        actor_type="agent",
        actor_id=agent.id,
        category="run",
        action="chat.model_override_rejected",
        resource={"run_id": str(run.id), "requested_model_config_id": str(requested)},
        decision="deny",
        reason="model is not a chat-enabled model; the agent's default model was used",
        originating_operator=(run.context or {}).get("originating_operator"),
    )


def apply_effort_override(*, run: m.AgentRun | None, payload: dict[str, Any]) -> dict[str, Any]:
    """The run's behavioural payload with the chat's reasoning level in the
    pinned definition's `model_params`, where `resolve_params` reads it
    (spec 2026-11-11 §7). Same seam as `apply_model_override`, so the engine,
    the isolated control plane and the gateways all agree. Copies, never
    mutates: the payload may be a version row's own dict."""
    level = as_level((run.context or {}).get("effort_override")) if run is not None else None
    if level is None:
        return payload
    definition = dict(payload.get("definition") or {})
    raw = definition.get("model_params")
    model_params = dict(raw) if isinstance(raw, dict) else {}
    model_params[CHAT_LEVEL_KEY] = level
    definition["model_params"] = model_params
    return {**payload, "definition": definition}
