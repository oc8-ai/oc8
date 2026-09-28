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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.publish_hooks import run_publish_hooks
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

#: A reserved payload key holding publish PROVENANCE, never configuration.
#:
#: Excluded from `payload_hash` and stripped by `version_payload`, and both of
#: those are load-bearing rather than tidy. A rollback records
#: `{"rolled_back_from": N}` here (spec §2.7). If that key entered the hash,
#: then a rolled-back agent whose working copy is byte-identical to its current
#: version would hash differently from it -- so the no-op-publish 409 would stop
#: firing the moment anybody rolled back, and an operator could publish an
#: empty version whose only effect is a pointless compliance re-check and eval
#: run. If it were not stripped on the way out, a runtime would receive a
#: configuration key it has no meaning for.
META_KEY: Final = "_meta"


def version_payload(version: m.AgentVersion) -> dict[str, Any]:
    """The CONFIGURATION half of a stored payload: `_meta` removed.

    Every consumer that compares a stored payload against a fresh snapshot --
    the diff, the draft status, the no-op check, both runtimes -- reads through
    here rather than touching `version.payload` directly, so there is exactly
    one place that knows the reserved key exists.
    """
    return {k: v for k, v in (version.payload or {}).items() if k != META_KEY}


def version_meta(version: m.AgentVersion) -> dict[str, Any]:
    """The provenance half, or `{}`. Never `None`, so a caller can `.get()`."""
    meta = (version.payload or {}).get(META_KEY)
    return dict(meta) if isinstance(meta, dict) else {}


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


def diff_payloads(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Structural diff of two version payloads, one level deep inside a dict.

    Returns `{field, before, after}` entries sorted by `field`, computed
    SERVER-side so the Review dialog, the Versions tab and any future
    compliance report render the same thing from the same code (spec §2.7).

    One level, and only when BOTH sides are dicts. `narrowing` is keyed by
    connection name and `definition` is a flat grab-bag, so `narrowing.odoo`
    and `definition.max_steps` are the granularity an operator reasons in. A
    whole-object comparison would say "narrowing changed" for a single
    threshold edit; a generic deep diff would emit paths nobody can review and
    would need an array-diff convention this does not have. Lists are compared
    whole for exactly that reason -- descending into one means diffing by
    index, which renders an insertion at the front as every element changing.

    `_meta` is skipped on both sides: it is provenance, not configuration, and
    a rollback would otherwise render as "the metadata changed" stacked on top
    of the real difference.
    """
    entries: list[dict[str, Any]] = []
    fields = (set(before) | set(after)) - {META_KEY}
    for field in fields:
        left = before.get(field)
        right = after.get(field)
        if left == right:
            continue
        if isinstance(left, dict) and isinstance(right, dict):
            for key in set(left) | set(right):
                if left.get(key) != right.get(key):
                    entries.append(
                        {
                            "field": f"{field}.{key}",
                            "before": left.get(key),
                            "after": right.get(key),
                        }
                    )
            continue
        entries.append({"field": field, "before": left, "after": right})
    entries.sort(key=lambda e: str(e["field"]))
    return entries


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Just the field names from `diff_payloads`, in the same order.

    This is what "N unpublished changes" counts, and it is deliberately
    granular: two threshold edits on two different connections read as two
    changes rather than as one "narrowing", which is the number an operator
    would otherwise have to open the diff to discover.
    """
    return [str(entry["field"]) for entry in diff_payloads(before, after)]


@dataclass(frozen=True)
class DraftStatus:
    """How the working copy differs from what is live.

    There is no `state` column and no second mutable row: the DIFFERENCE
    between the `agent` row and its current version IS the draft (spec §2.2).
    That is why this is computed rather than stored -- a stored flag is a third
    thing to keep in sync with the two that already disagree.
    """

    dirty: bool
    changed_fields: tuple[str, ...]
    current_version_no: int | None


async def draft_status(db: AsyncSession, agent: m.Agent) -> DraftStatus:
    """`dirty` from the HASH, `changed_fields` from the diff.

    Two different computations on purpose, and the hash is the authority. The
    409 that refuses a no-op publish compares hashes, so a `dirty` derived from
    the diff could disagree with it -- an operator would see a publish bar,
    click Publish, and get a 409 saying there is nothing to publish. Deriving
    the boolean from the same comparison the endpoint makes removes that class
    of bug entirely; the field list is display only.

    An agent with no `current_version_id` reads as dirty with no number. Only
    reachable for a row written outside `create_agent` (a fixture, a restore),
    and "everything is unpublished" is the honest answer for it.
    """
    snapshot = await snapshot_agent(db, agent)
    current = (
        await db.get(m.AgentVersion, agent.current_version_id)
        if agent.current_version_id is not None
        else None
    )
    if current is None:
        return DraftStatus(
            dirty=True,
            changed_fields=tuple(sorted(snapshot)),
            current_version_no=None,
        )
    published = version_payload(current)
    return DraftStatus(
        dirty=payload_hash(published) != payload_hash(snapshot),
        changed_fields=tuple(changed_fields(published, snapshot)),
        current_version_no=current.version_no,
    )


async def publish_version(
    db: AsyncSession,
    agent: m.Agent,
    *,
    note: str | None = None,
    published_by: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> m.AgentVersion:
    """Snapshot `agent`'s current configuration, and -- if it differs from the
    current version -- insert it as the next `version_no`, repoint
    `agent.current_version_id` at it, and audit the publish.

    Raises `NoChangesToPublish` if the hash is identical to the current
    version's; there is deliberately no way to force an empty publish.

    `meta`, when given, is merged into the STORED payload after the hash is
    computed -- see `META_KEY`'s own comment for why the hash must never see
    it.
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
    stored = dict(payload)
    if meta:
        # Merged after hashing, never before. See META_KEY's own comment: a
        # `_meta` inside the hash breaks the no-op check for every agent that
        # has ever been rolled back.
        stored[META_KEY] = dict(meta)
    version = m.AgentVersion(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        version_no=(max_no or 0) + 1,
        payload=stored,
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
    # Inside this transaction, and last: the hooks may read the version row and
    # the audit event, and a refusal must be able to undo both. Every publish
    # path reaches here -- `create_agent`'s v1, the publish endpoint, and the
    # rollback endpoint -- which is what makes a rollback "re-enter the publish
    # gate" (spec §2.7) rather than quietly skip it the way a `current_version_
    # id` repoint would have.
    await run_publish_hooks(db, version)
    return version


async def resolve_version(
    db: AsyncSession, run: m.AgentRun | None, agent: m.Agent
) -> dict[str, Any]:
    """The behavioural payload `run` should execute with: the version it was
    pinned to at intake, falling back to `agent`'s current version, falling
    back to a live snapshot for the rare row that has neither (e.g. a
    pre-migration agent that predates any publish). Never raises -- a reader
    always gets a usable payload.

    Every runtime reads an agent's behavioural fields (narrowing, mission,
    definition, model_config_id, is_team_lead, ...) through this, never off
    the live `Agent` row: the row is the operator's working draft, and a
    change to it must not land between two tool calls of a run already in
    flight. `department.frame` is deliberately NOT part of the payload -- the
    tenant's policy ceiling bites immediately, mid-run included.

    `run` may be None for a caller with no run behind it (a direct
    `run_agent` call in a test); that resolves exactly like an unpinned run.
    """
    pinned = run.agent_version_id if run is not None else None
    version_id = pinned or agent.current_version_id
    if version_id is not None:
        version = await db.get(m.AgentVersion, version_id)
        if version is not None:
            payload = version_payload(version)
            # A version published before a column joined `_VERSIONED_COLUMNS`
            # does not carry it. Readers index the payload directly, so fill
            # the gap from the row -- the only value that field has ever had
            # for this agent, since it was never versioned before.
            for col in _VERSIONED_COLUMNS:
                if col not in payload:
                    val = getattr(agent, col)
                    payload[col] = str(val) if isinstance(val, uuid.UUID) else val
            return payload
    return await snapshot_agent(db, agent)


def pinned_model_config_id(cfg: Mapping[str, Any]) -> uuid.UUID | None:
    """`cfg["model_config_id"]` as a UUID. A payload stores it as a string
    (it is JSON), so every reader would otherwise re-parse it by hand."""
    raw = cfg.get("model_config_id")
    if raw is None or raw == "":
        return None
    return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
