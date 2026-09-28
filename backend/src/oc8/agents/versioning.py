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

import copy
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


async def pinned_version_no(db: AsyncSession, run: m.AgentRun) -> int | None:
    """The version number `run.agent_version_id` points at, or `None`.

    Two routes serialize a `RunDTO` for the same frontend query-cache entry
    (`["run", runId]`): `GET /runs/{id}` (run.py) and the chat surface's
    `GET /chat/sessions/{sessionId}/runs/{runId}` (chat.py, used by
    `useCopilotRunActivity`). Both call this so `agentVersionNo` is filled
    identically no matter which query last wrote the cache -- otherwise the
    one that leaves it null would intermittently overwrite the other's value.

    `None` for a run pinned to nothing (a historical row from before version
    pinning existed) -- see `resolve_version`'s own None-safe fallback.
    """
    if run.agent_version_id is None:
        return None
    version = await db.get(m.AgentVersion, run.agent_version_id)
    return version.version_no if version is not None else None


def pinned_model_config_id(cfg: Mapping[str, Any]) -> uuid.UUID | None:
    """`cfg["model_config_id"]` as a UUID. A payload stores it as a string
    (it is JSON), so every reader would otherwise re-parse it by hand."""
    raw = cfg.get("model_config_id")
    if raw is None or raw == "":
        return None
    return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))


#: The two versioned columns that are UUIDs. `snapshot_agent` stringifies them
#: (JSONB has no UUID type), so the inverse has to parse them back -- assigning
#: the string would give SQLAlchemy a `str` where the column is `Uuid`.
_UUID_COLUMNS: Final[frozenset[str]] = frozenset({"model_config_id", "role_id"})


async def missing_references(db: AsyncSession, payload: dict[str, Any]) -> dict[str, list[str]]:
    """Ids `payload` points at that no longer exist (or are soft-deleted), by
    payload field. Empty dict when everything resolves.

    Rollback's pre-flight, run before anything is written. A version is
    immutable but what it references is not: a knowledge base deleted since
    (its grants were removed WITH it -- `knowledge/tombstone.py` calls a
    dangling grant an authz hazard), a model config deleted after the agent
    was moved off it, a skill version removed. Restoring any of those would
    either re-create exactly the dangling row a deletion was careful to remove,
    or publish a version whose payload silently differs from its target if the
    reference were dropped instead. Refusing is the only answer that is neither.

    `role_id` is deliberately not checked: nothing in the codebase reads or
    writes `agent.role_id` other than versioning itself, so there is no table
    whose absence would mean anything.
    """
    missing: dict[str, list[str]] = {}

    kb_ids = {uuid.UUID(str(k)) for k in payload.get("knowledge_grants") or []}
    if kb_ids:
        live = set(
            (
                await db.execute(
                    select(m.KnowledgeBase.id).where(
                        m.KnowledgeBase.id.in_(kb_ids), m.KnowledgeBase.deleted_at.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        if kb_ids - live:
            missing["knowledge_grants"] = sorted(str(k) for k in kb_ids - live)

    sv_ids = {
        uuid.UUID(str(row["skill_version_id"])) for row in payload.get("skill_assignments") or []
    }
    if sv_ids:
        live = set(
            (
                await db.execute(
                    # Joined to `skill`: `delete_skill` HARD-deletes a skill
                    # nobody is assigned to and leaves its versions behind, so
                    # a version row alone does not prove the skill exists. An
                    # ARCHIVED skill (`skill.deleted_at` set) does count as
                    # live -- archiving exists precisely so assignments keep
                    # resolving.
                    select(m.SkillVersion.id)
                    .join(m.Skill, m.Skill.id == m.SkillVersion.skill_id)
                    .where(m.SkillVersion.id.in_(sv_ids), m.SkillVersion.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        if sv_ids - live:
            missing["skill_assignments"] = sorted(str(s) for s in sv_ids - live)

    model_id = pinned_model_config_id(payload)
    if model_id is not None and await db.get(m.ModelConfig, model_id) is None:
        missing["model_config_id"] = [str(model_id)]

    return missing


async def apply_payload(db: AsyncSession, agent: m.Agent, payload: dict[str, Any]) -> None:
    """Copy a version's payload back onto the working copy: `snapshot_agent`
    inverted. `apply_payload` then `snapshot_agent` reproduces `payload`
    exactly (minus `_meta`) -- `test_apply_payload_round_trips_to_an_identical_
    snapshot` pins that, and it is the property that makes a rollback publish
    the version it claims to restore.

    Used ONLY by rollback. A rollback that set the scalar columns and left
    `skill_assignment` and `knowledge_grant` alone would restore an agent's
    instructions while leaving it holding whatever skills and knowledge somebody
    added afterwards -- a configuration that never existed at any point in time.

    A key ABSENT from the payload is left alone rather than cleared. Absent is
    not empty: a version stored before a field joined the snapshot never
    recorded it, and `resolve_version` already serves such a version with the
    row's current value -- so leaving it is what that version has actually been
    running with, whereas clearing it would, for the two collections, silently
    strip every skill and grant the agent holds.

    Does NOT publish, validate against the department frame or any other live
    constraint, check `missing_references`, or audit. The caller owns all of
    those, in the order `api/v1/agents_write.py`'s module docstring requires.
    """
    for col in _VERSIONED_COLUMNS:
        if col not in payload:
            continue
        value = payload[col]
        if col in _UUID_COLUMNS:
            setattr(agent, col, uuid.UUID(str(value)) if value else None)
        else:
            # Deep-copied: the payload is usually the version row's own JSONB,
            # and aliasing it onto the agent would let a later in-place edit of
            # the working copy reach into an immutable version.
            setattr(agent, col, copy.deepcopy(value))

    if "skill_assignments" in payload:
        await _apply_skill_assignments(db, agent, payload["skill_assignments"] or [])
    if "knowledge_grants" in payload:
        await _apply_knowledge_grants(db, agent, payload["knowledge_grants"] or [])


async def _apply_skill_assignments(
    db: AsyncSession, agent: m.Agent, wanted: list[dict[str, Any]]
) -> None:
    """Make this agent's OWN live assignments exactly `wanted`.

    Disable, never delete: `assign_skill` re-enables an existing row rather
    than inserting a second one, so `enabled` is the live/not-live term the
    rest of the codebase already agrees on, and the row has to survive for a
    later re-assign to find it.

    Soft-deleted rows are loaded too, and REVIVED if wanted rather than joined
    by a fresh insert: `uq_skill_assignment_agent` (migration 0040) is UNIQUE
    over (agent_id, skill_version_id) with no `deleted_at` term, so an insert
    beside a soft-deleted row for the same pair is an IntegrityError.

    `agent_id == agent.id` only. Department- and tenant-wide assignments
    (agent_id NULL) are inherited policy that `snapshot_agent` excludes, and
    disabling one would strip a skill from every colleague.
    """
    target = {uuid.UUID(str(row["skill_version_id"])) for row in wanted}
    existing = (
        (await db.execute(select(m.SkillAssignment).where(m.SkillAssignment.agent_id == agent.id)))
        .scalars()
        .all()
    )
    seen: set[uuid.UUID] = set()
    for row in existing:
        seen.add(row.skill_version_id)
        if row.skill_version_id in target:
            row.enabled = True
            row.deleted_at = None
        elif row.deleted_at is None:
            row.enabled = False
    for version_id in sorted(target - seen):
        db.add(
            m.SkillAssignment(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                skill_version_id=version_id,
                enabled=True,
            )
        )


async def _apply_knowledge_grants(db: AsyncSession, agent: m.Agent, wanted: list[str]) -> None:
    """Make this agent's knowledge grants exactly `wanted`.

    Filtered on `grantee_type == "agent"` as well as `grantee_id`, and that is
    not belt-and-braces: `knowledge_grant` holds department-wide rows in the
    same two columns, so a delete keyed on `grantee_id` alone could revoke a
    whole department's knowledge base.

    Hard delete rather than soft: `KnowledgeGrant` has no `SoftDeleteMixin` and
    a UNIQUE over (tenant, kb, grantee_type, grantee_id), so a disabled row
    would block the grant ever being made again. A removed grant's `scope` is
    not versioned (the payload holds kb ids only) -- re-created grants get the
    column default, which is what every grant writer in the codebase uses.
    """
    target = {uuid.UUID(str(kb_id)) for kb_id in wanted}
    existing = (
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
    seen: set[uuid.UUID] = set()
    for row in existing:
        if row.kb_id in target:
            seen.add(row.kb_id)
        else:
            await db.delete(row)
    for kb_id in sorted(target - seen):
        db.add(
            m.KnowledgeGrant(
                tenant_id=agent.tenant_id,
                kb_id=kb_id,
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
