"""Which knowledge content may be embedded by a cloud model.

Chat never sends `restricted` material to a non-local model
(`ModelRouter._guard_and_resolve`'s ClassificationViolation, and
`classification_rule` at retrieval). An embedding sends the same text to the
same kind of provider, so it follows the same line: content labelled
`restricted` -- by its base, its source or the chunk itself -- or held in a
local-only base is embedded locally or not at all. The reason returned here is
passed to `ModelRouter.embed(require_local=...)`, which refuses any cloud
provider with it.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.modelrouter import locality_for_provider

#: Least to most sensitive -- the order of `ck_source_classification`.
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")

LOCAL_AGENT_REASON = (
    "the agent runs on a local model, so what it reads and writes may hold restricted "
    "material that chat keeps off cloud models"
)


async def live_model_locality(db: AsyncSession, agent: m.Agent) -> str:
    """Where the agent's live row says its model runs: "local" or "cloud"."""
    if agent.model_config_id is not None:
        config = await db.get(m.ModelConfig, agent.model_config_id)
        if config is not None:
            return "local" if config.locality == "local" else "cloud"
    provider = (agent.presentation or {}).get("provider", get_settings().default_model_provider)
    return locality_for_provider(str(provider))


async def agent_runs_locally(db: AsyncSession, agent: m.Agent, model_locality: str | None) -> bool:
    """For what the agent SENDS (an embedding of its question or note): true if
    its model is local by the run's pinned locality (`model_locality`) or by the
    live row. Either one being local is enough -- a local model is what lets
    restricted material into an agent's context."""
    if model_locality == "local":
        return True
    return await live_model_locality(db, agent) == "local"


async def model_receives_local_content(
    db: AsyncSession, agent: m.Agent, model_locality: str | None
) -> bool:
    """For what the agent RECEIVES: true only if the model this run talks to is
    local -- the pinned locality when the caller has one, else the live row. A
    run pinned to a cloud model gets nothing local-only, whatever the live row
    has been switched to since."""
    if model_locality is not None:
        return model_locality == "local"
    return await live_model_locality(db, agent) == "local"


def strictest(*labels: str | None) -> str:
    """The most sensitive of `labels`; an unknown label counts as `restricted`
    (an unrecognised label is no reason to loosen anything)."""
    rank = 0
    for label in labels:
        if label is None:
            continue
        rank = max(
            rank,
            CLASSIFICATIONS.index(label) if label in CLASSIFICATIONS else len(CLASSIFICATIONS) - 1,
        )
    return CLASSIFICATIONS[rank]


def require_local_reason(
    *, kb: m.KnowledgeBase | None = None, classification: str | None = None
) -> str | None:
    """Why this content may only be embedded locally, or None."""
    if kb is not None and kb.local_only:
        return "the knowledge base is local-only"
    label = strictest(kb.classification if kb is not None else None, classification)
    if label == "restricted":
        return "restricted classification requires a local model"
    return None
