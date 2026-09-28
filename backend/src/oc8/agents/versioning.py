"""Agent configuration snapshots (Package A, tasks A1/A2 of the versioning
spec).

The `agent` row is the working copy an operator edits; an `AgentVersion` is
an immutable snapshot of the behavioural subset that actually runs. The
difference between the two IS the draft -- there is no second mutable copy
to keep in sync. `Agent.current_version_id` always points at the version
that is live; `AgentRun.agent_version_id` pins a run to the version it
started with, so a publish mid-run never changes what a running agent is
doing.

add/flush only -- callers own the commit, same convention as
`oc8.agents.hire`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event

#: The behavioural subset of `Agent` that a version snapshots. Anything NOT
#: named here (status, pause_reason, paused_at, presentation, trust_level,
#: is_tenant_assistant, config_revision, name, id, tenant_id, department_id,
#: ...) is operational state or identity, not behaviour, and must never end
#: up inside a payload -- see `test_snapshot_excludes_operational_state`.
#: `name` is deliberately absent: a rename must not consume a version number
#: (versioning spec §2.4, decision 5).
_VERSIONED_COLUMNS = (
    "mission",
    "role_title",
    "definition",
    "model_config_id",
    "narrowing",
    "narrowing_overridden_keys",
    "role_id",
    "runtime_ref",
    "is_team_lead",
)


class NoChangesToPublish(Exception):
    """Raised by `publish_version` when the agent's current configuration
    hashes identically to its current version. Publishing nothing is always a
    mistake -- an empty version would still trigger a pointless downstream
    compliance re-check -- so this is a hard stop, not a silent no-op."""


async def snapshot_agent(db: AsyncSession, agent: m.Agent) -> dict[str, Any]:
    """The exact payload a version stores for `agent`, computed fresh from its
    current row plus its own skill/knowledge bindings -- never read off a
    stale version."""
    payload: dict[str, Any] = {}
    for col in _VERSIONED_COLUMNS:
        val = getattr(agent, col)
        payload[col] = str(val) if isinstance(val, uuid.UUID) else val

    # SkillAssignment has NO `skill_id` column -- only `skill_version_id`
    # (models/skills.py). It also carries SoftDeleteMixin plus an `enabled`
    # flag, and `assign_skill` (agents_write.py) RE-ENABLES an existing row
    # rather than inserting a new one -- so omitting either filter keeps a
    # disabled/unassigned skill in every snapshot, and a rollback would
    # resurrect it.
    #
    # Scope: only assignments bound to THIS agent. Per the model's own
    # docstring, `agent_id` set means this agent, `department_id` set means
    # every agent in that department, and neither set means every agent in
    # the tenant. Department- and tenant-wide assignments are inherited
    # policy, not this agent's configuration, and must keep applying after a
    # rollback -- so they are excluded here.
    assignments = (
        (
            await db.execute(
                select(m.SkillAssignment).where(
                    m.SkillAssignment.agent_id == agent.id,
                    m.SkillAssignment.deleted_at.is_(None),
                    m.SkillAssignment.enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    payload["skill_assignments"] = sorted(
        ({"skill_version_id": str(a.skill_version_id)} for a in assignments),
        key=lambda d: d["skill_version_id"],
    )

    # KnowledgeGrant has NO `agent_id` either -- it is a polymorphic grant:
    # `grantee_type IN ('department','agent')` plus `grantee_id`
    # (models/knowledge.py).
    grants = (
        (
            await db.execute(
                select(m.KnowledgeGrant).where(
                    m.KnowledgeGrant.grantee_type == "agent",
                    m.KnowledgeGrant.grantee_id == agent.id,
                )
            )
        )
        .scalars()
        .all()
    )
    payload["knowledge_grants"] = sorted(str(g.kb_id) for g in grants)
    return payload


def payload_hash(payload: dict[str, Any]) -> bytes:
    """A stable digest of `payload`: sorted keys, no whitespace, so two
    structurally-identical payloads always hash identically regardless of
    insertion order. Sorting the CONTENTS of any list-valued field (e.g.
    `skill_assignments`) is `snapshot_agent`'s job, not this function's -- an
    unsorted list here is treated as a genuinely different payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).digest()


async def publish_version(
    db: AsyncSession,
    agent: m.Agent,
    *,
    note: str | None = None,
    published_by: uuid.UUID | None = None,
) -> m.AgentVersion:
    """Snapshot `agent`'s current configuration, and -- if it differs from the
    current version -- insert it as the next `version_no`, repoint
    `agent.current_version_id` at it, and audit the publish.

    Raises `NoChangesToPublish` if the hash is identical to the current
    version's; there is deliberately no way to force an empty publish.
    """
    payload = await snapshot_agent(db, agent)
    new_hash = payload_hash(payload)

    current: m.AgentVersion | None = None
    if agent.current_version_id is not None:
        current = await db.get(m.AgentVersion, agent.current_version_id)
    if current is not None and current.payload_hash == new_hash:
        raise NoChangesToPublish(f"agent {agent.id} has no changes to publish")

    max_no = (
        await db.execute(
            select(func.max(m.AgentVersion.version_no)).where(m.AgentVersion.agent_id == agent.id)
        )
    ).scalar()
    version = m.AgentVersion(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        version_no=(max_no or 0) + 1,
        payload=payload,
        payload_hash=new_hash,
        note=note,
        published_by=published_by,
    )
    db.add(version)
    await db.flush()

    agent.current_version_id = version.id
    await db.flush()

    await append_event(
        db,
        tenant_id=agent.tenant_id,
        actor_type="operator",
        actor_id=published_by,
        category="admin",
        action="agent.version.published",
        resource={"agent_id": str(agent.id), "version_no": version.version_no},
    )
    return version


async def resolve_version(db: AsyncSession, run: m.AgentRun, agent: m.Agent) -> dict[str, Any]:
    """The behavioural payload `run` should execute with: the version it was
    pinned to at start, falling back to `agent`'s current version, falling
    back to a live snapshot for the rare row that has neither (e.g. a
    pre-migration agent that predates any publish). Never raises -- a reader
    always gets a usable payload.

    Small helper pulled forward from task A4 (run pinning), since later
    tasks consume it by this exact name; A4 owns wiring it into the actual
    run-start/step path and may extend this if that turns up a need this
    slice didn't anticipate.
    """
    version_id = run.agent_version_id or agent.current_version_id
    if version_id is not None:
        version = await db.get(m.AgentVersion, version_id)
        if version is not None:
            return version.payload
    return await snapshot_agent(db, agent)
