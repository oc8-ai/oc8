"""Copilot: existing agent first -- the agents that fit a person's request.

Before the Copilot does a tool task itself it offers an agent that already
does it. Three filters, in this order, and nothing is offered that fails one:

1. the agent has the tool -- enabled in its department's frame intersected
   with its PUBLISHED narrowing (`effective_tool_policies`);
2. the person may run it -- exactly what `POST /agents/{id}/run` would decide
   for them (`run_reach` + `agent_runnable_in`), resolved off-request from the
   stored member, and never what the Copilot itself may reach: the Copilot
   crosses departments, the person it acts for may not;
3. its published name, role title and mission are close to the request
   (embedding cosine >= `MATCH_THRESHOLD`), best first, at most `MATCH_LIMIT`.

No embeddings, no matches: the Copilot falls through to its direct offer
rather than guess an unranked agent.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.repo import RunReach, agent_runnable_in, run_reach
from oc8.agents.versioning import resolve_version
from oc8.authz.authority import authority_for_member
from oc8.authz.pdp import effective_tool_policies
from oc8.authz.permissions import RUN_START
from oc8.authz.scope import scope_for_member
from oc8.config import get_settings
from oc8.knowledge.embedding_policy import LOCAL_AGENT_REASON, agent_runs_locally
from oc8.knowledge.retrieval import cosine_similarity
from oc8.modelrouter import EmbeddingUnavailable, get_model_router
from oc8.modelrouter.router import embedding_local_tag

logger = logging.getLogger(__name__)

#: The lowest cosine similarity at which an agent counts as fitting.
MATCH_THRESHOLD: float = 0.6
#: How many fitting agents are offered at most.
MATCH_LIMIT: int = 3

_CACHE_SIZE = 512


@dataclass(frozen=True)
class AgentMatch:
    agent_id: uuid.UUID
    name: str
    department_id: uuid.UUID
    department_name: str
    score: float


class _VectorCache:
    """Process-local LRU of agent-text vectors, keyed by (model, sha256(text)).

    Agent texts change only on a publish or a rename, and then hash
    differently; the request text is never cached."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._rows: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    @staticmethod
    def key(model: str, text: str) -> tuple[str, str]:
        return model, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, key: tuple[str, str]) -> list[float] | None:
        vector = self._rows.get(key)
        if vector is not None:
            self._rows.move_to_end(key)
        return vector

    def put(self, key: tuple[str, str], vector: list[float]) -> None:
        self._rows[key] = vector
        self._rows.move_to_end(key)
        while len(self._rows) > self._size:
            self._rows.popitem(last=False)

    def clear(self) -> None:
        self._rows.clear()


_vectors = _VectorCache(_CACHE_SIZE)


def clear_embedding_cache() -> None:
    """Empty the agent-text vector cache (tests)."""
    _vectors.clear()


# ------------------------------------------------------------------ may run


def _paused(agent: m.Agent) -> bool:
    return agent.status == "paused" or agent.pause_reason is not None


async def _member_run_reach(
    db: AsyncSession, *, member_id: uuid.UUID, tenant_id: uuid.UUID, token_role: str | None
) -> RunReach | None:
    """The person's `run:start` reach, built from the stored member alone --
    the same authority and scope `POST /agents/{id}/run` reads off a request.
    None for no such (live) person in this tenant."""
    member = await db.get(m.OrgMember, member_id)
    if member is None or member.deleted_at is not None or member.tenant_id != tenant_id:
        return None
    authority = await authority_for_member(db, member, token_role=token_role)
    scope = await scope_for_member(db, member, token_role=token_role)
    return run_reach(authority=authority, scope=scope, permission=RUN_START)


def _runnable(reach: RunReach | None, agent: m.Agent) -> bool:
    if reach is None or agent.is_tenant_assistant or _paused(agent):
        return False
    return agent_runnable_in(reach, agent)


async def member_may_run_agent(
    db: AsyncSession, *, member_id: uuid.UUID, agent: m.Agent, token_role: str | None = None
) -> bool:
    """Whether this person may start `agent` now: never a deleted, paused,
    hire-pending or tenant-assistant agent; otherwise `run:start` must reach
    the agent's department for them.

    `token_role` is the role claim of the token behind the chat, when the
    caller has one (a member with no assigned role is authorised by it);
    without it the stored row decides alone, which fails closed."""
    if agent.deleted_at is not None or agent.is_tenant_assistant or _paused(agent):
        return False
    reach = await _member_run_reach(
        db, member_id=member_id, tenant_id=agent.tenant_id, token_role=token_role
    )
    return _runnable(reach, agent)


# ------------------------------------------------------------------ has tool


async def _frame(db: AsyncSession, agent: m.Agent) -> m.Department | None:
    return await db.get(m.Department, agent.department_id)


def _tool_enabled(
    department: m.Department | None, narrowing: dict[str, object] | None, tool_key: str
) -> bool:
    frame = (department.frame or {}) if department is not None else {}
    policy = effective_tool_policies(frame, dict(narrowing or {})).get(tool_key)
    return policy is not None and policy.enabled


async def agent_has_tool(db: AsyncSession, agent: m.Agent, tool_key: str) -> bool:
    """Whether `tool_key` is enabled for `agent`: its department's frame
    intersected with its PUBLISHED narrowing (a draft grants nothing)."""
    published = await resolve_version(db, None, agent)
    return _tool_enabled(await _frame(db, agent), published.get("narrowing"), tool_key)


# ------------------------------------------------------------------ embedding


async def _embed(
    db: AsyncSession, *, text: str, copilot: m.Agent, cache: bool = False
) -> list[float] | None:
    """`text`'s vector with the deployment's default embedding model -- or the
    local fallback when a cloud default is refused for this tenant, and only a
    local model when the Copilot runs on one (`memory/router._embed_memory`).
    None when no embedding can be had."""
    settings = get_settings()
    router = get_model_router()
    require_local = LOCAL_AGENT_REASON if await agent_runs_locally(db, copilot, None) else None
    model = settings.default_embedding_model
    try:
        if embedding_local_tag(model) is None:
            refusal = await router.cloud_embedding_refusal(
                model, tenant_id=copilot.tenant_id, db=db, require_local=require_local
            )
            if refusal is not None:
                model = settings.local_embedding_model
        key = _VectorCache.key(model, text)
        if cache and (hit := _vectors.get(key)) is not None:
            return hit
        vector = list(
            await router.embed(
                text, model=model, tenant_id=copilot.tenant_id, db=db, require_local=require_local
            )
        )
    except EmbeddingUnavailable as exc:
        logger.info("no agent match: embeddings are unavailable (%s)", exc)
        return None
    if cache:
        _vectors.put(key, vector)
    return vector


# ------------------------------------------------------------------ match


async def matching_agents(
    db: AsyncSession,
    *,
    member_id: uuid.UUID,
    tool_key: str,
    request: str,
    copilot: m.Agent,
    token_role: str | None = None,
) -> list[AgentMatch]:
    """The agents of the Copilot's tenant that have `tool_key`, that this
    person may run, and whose published name, role title and mission fit
    `request` -- best first, at most `MATCH_LIMIT`, each at least
    `MATCH_THRESHOLD`. Empty when embeddings are unavailable."""
    agents = (
        (
            await db.execute(
                select(m.Agent)
                .where(
                    m.Agent.tenant_id == copilot.tenant_id,
                    m.Agent.deleted_at.is_(None),
                    m.Agent.is_tenant_assistant.is_(False),
                    m.Agent.id != copilot.id,
                )
                .order_by(m.Agent.name, m.Agent.id)
            )
        )
        .scalars()
        .all()
    )
    reach = await _member_run_reach(
        db, member_id=member_id, tenant_id=copilot.tenant_id, token_role=token_role
    )
    if reach is None:
        return []

    candidates: list[tuple[m.Agent, m.Department | None, str]] = []
    for agent in agents:
        published = await resolve_version(db, None, agent)
        department = await _frame(db, agent)
        if not _tool_enabled(department, published.get("narrowing"), tool_key):
            continue
        if not _runnable(reach, agent):
            continue
        role_title = published.get("role_title") or ""
        mission = published.get("mission") or ""
        text = f"{agent.name}\n{role_title}\n{mission}"
        candidates.append((agent, department, text))
    if not candidates:
        return []

    wanted = await _embed(db, text=request, copilot=copilot)
    if wanted is None:
        return []
    matches: list[AgentMatch] = []
    for agent, department, text in candidates:
        vector = await _embed(db, text=text, copilot=copilot, cache=True)
        if vector is None:
            return []
        score = cosine_similarity(wanted, vector)
        if score < MATCH_THRESHOLD:
            continue
        matches.append(
            AgentMatch(
                agent_id=agent.id,
                name=agent.name,
                department_id=agent.department_id,
                department_name=department.name if department is not None else "",
                score=score,
            )
        )
    matches.sort(key=lambda match: match.score, reverse=True)
    return matches[:MATCH_LIMIT]
