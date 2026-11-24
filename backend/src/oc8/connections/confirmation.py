"""A person's confirmation of a binding or a trigger, split (plan Rulings 1, 2).

The old digest (`connections.access.binding_fingerprint`) stays exactly as it
was, so every confirmation stamped before the split keeps holding unchanged.
Next to it a stamp now stores a split (`agent_tool_access.binding_split`,
`trigger.access_split`): `{"v": 1, "core": <sha256 hex>, "grant": {...}}`.

- `core` is strict: any change breaks the confirmation. It hashes the agent
  version's payload without the narrowing's `tools`,
  `narrowing_overridden_keys`, `skill_assignments` and `_meta` (the frame
  cascade publishes a new version for each `enabled` change, and those parts
  are judged in `grant`), plus the department's `goal` and
  `frame_capa_defaults` and its setup of the tool (`setup`, `shared`).
- `grant` may only tighten (`authz.tightening.grant_loosenings`): `tools`
  holds one entry per enabled tool key of the effective policies -- every
  one, for a binding too (a new tool next to the bound one is an addition
  the member confirms) -- `frame` the frame without `tools`,
  `skills` `{assignment id: [skill_version_id, overrides]}`.

The agent's own approval threshold (`presentation["approval_value_eur"]`,
which the live gate adds as an extra threshold) is folded into each tool
entry's `approval_eur` as the stricter of the two, exactly as the gate takes
it: lowering it tightens, raising or removing it loosens.

A row without a split (stamped before it existed) holds while the stored
digest equals the current one: strict, as before. A row with a split holds
while its `core` matches and the current grant only tightens the stored one.
The split decides even when the digest matches, because only the grant
covers the agent's own threshold.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import META_KEY, payload_hash, resolved_payload, snapshot_agent
from oc8.authz.pdp import ToolPolicy, effective_tool_policies
from oc8.authz.tightening import grant_loosenings, grant_tool_entry
from oc8.connections.access import _setup_parts, binding_fingerprint

SPLIT_VERSION: Final = 1

#: The fields `ToolPolicy` reads from a stored tool policy. Any other field of
#: a frame or narrowing policy (a department's default connection, say) is
#: kept in the grant entry as is, and must stay equal.
_POLICY_KEYS: Final = frozenset(
    {
        "enabled",
        "read",
        "modify",
        "approval_eur",
        "approval_actions",
        "only",
        "connection_id",
        "conditions",
        "limits",
    }
)

#: Version payload keys judged in `grant` (or not consent at all), not `core`.
_OUT_OF_CORE: Final = frozenset({"narrowing_overridden_keys", "skill_assignments", META_KEY})


@dataclass(frozen=True)
class SplitFingerprint:
    core: str
    grant: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"v": SPLIT_VERSION, "core": self.core, "grant": self.grant}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


async def _version_view(
    db: AsyncSession, agent: m.Agent, version_id: uuid.UUID | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The payload a run of `version_id` executes (`resolved_payload`, as
    `resolve_version` reads it) and the parts of `core` that say which one
    it is."""
    if version_id is not None:
        payload = await resolved_payload(db, agent, version_id)
        if payload is not None:
            return payload, {}
        # The runtime falls back to the live row; the id stays in `core`, so
        # a version that cannot be read is confirmed for itself only.
        return await snapshot_agent(db, agent), {"unresolved_version": str(version_id)}
    return await snapshot_agent(db, agent), {}


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _agent_threshold(agent: m.Agent) -> Any:
    raw = _dict(agent.presentation).get("approval_value_eur")
    if raw is None or _is_number(raw):
        return raw
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw


def _extras(policy: Any) -> dict[str, Any]:
    return {k: v for k, v in _dict(policy).items() if k not in _POLICY_KEYS and v is not None}


def _tool_entry(
    frame_policy: Any, narrowing_policy: Any, effective: ToolPolicy, threshold: Any
) -> dict[str, Any]:
    """One `grant.tools` entry. Built through `grant_tool_entry` only (an
    empty surface must never be stored as every tool), from policies that
    pass the strict read; one that does not is recorded unreadable, so any
    change to it loosens."""
    frame = _dict(frame_policy) if frame_policy is not None else None
    narrow = _dict(narrowing_policy) if narrowing_policy is not None else None
    try:
        ToolPolicy.from_json(frame, strict=True)
        ToolPolicy.from_json(narrow, strict=True)
        entry = grant_tool_entry(frame, narrow, effective)
    except (KeyError, TypeError, ValueError):
        raw = json.dumps([frame, narrow], sort_keys=True, default=str)
        return {"enabled": True, "unreadable": raw}
    if _extras(frame):
        entry["frame_extra"] = _extras(frame)
    if _extras(narrow):
        entry["agent_extra"] = _extras(narrow)
    if threshold is not None:
        own = entry.get("approval_eur")
        if _is_number(threshold) and own is None:
            entry["approval_eur"] = threshold
        elif _is_number(threshold) and _is_number(own):
            entry["approval_eur"] = min(own, threshold)
        else:
            entry["agent_approval_eur"] = threshold
    return entry


def _grant_tools(
    frame: dict[str, Any], narrowing: dict[str, Any], threshold: Any
) -> dict[str, Any]:
    frame_tools, narrow_tools = _dict(frame.get("tools")), _dict(narrowing.get("tools"))
    try:
        effective = effective_tool_policies(frame, narrowing)
    except (KeyError, TypeError, ValueError):
        raw = json.dumps([frame_tools, narrow_tools], sort_keys=True, default=str)
        return {"*": {"enabled": True, "unreadable": raw}}
    tools: dict[str, Any] = {}
    for key in sorted(effective):
        policy = effective.get(key)
        if policy is None or not policy.enabled:
            continue
        entry = _tool_entry(frame_tools.get(key), narrow_tools.get(key), policy, threshold)
        if entry.get("enabled"):
            tools[key] = entry
    return tools


async def _grant_skills(db: AsyncSession, agent: m.Agent) -> dict[str, Any]:
    """The assignments `binding_fingerprint` reads, enabled ones only."""
    assignments = (
        await db.execute(
            select(m.SkillAssignment).where(
                m.SkillAssignment.deleted_at.is_(None),
                (m.SkillAssignment.agent_id == agent.id)
                | (m.SkillAssignment.department_id == agent.department_id)
                | (
                    m.SkillAssignment.agent_id.is_(None) & m.SkillAssignment.department_id.is_(None)
                ),
            )
        )
    ).scalars()
    return {
        str(a.id): [str(a.skill_version_id), a.overrides] for a in assignments if bool(a.enabled)
    }


async def split_fingerprint(
    db: AsyncSession, agent: m.Agent, tool_key: str | None, *, version_id: uuid.UUID | None
) -> SplitFingerprint:
    """What a person confirms, split into a strict `core` and a `grant` that
    may only tighten (module docstring). `tool_key` scopes the setup in
    `core` (None: every tool's); `grant.tools` always covers every tool."""
    dept = await db.get(m.Department, agent.department_id)
    payload, which = await _version_view(db, agent, version_id)
    narrowing = _dict(payload.get("narrowing"))
    version = {k: v for k, v in payload.items() if k not in _OUT_OF_CORE}
    if "narrowing" in version and isinstance(version["narrowing"], Mapping):
        version["narrowing"] = {k: v for k, v in narrowing.items() if k != "tools"}
    setup, shared = await _setup_parts(db, agent, dept, tool_key)
    core = payload_hash(
        {
            **which,
            "version": version,
            "department_id": str(agent.department_id),
            "department": None if dept is None else [dept.goal, dept.frame_capa_defaults],
            "setup": setup,
            "shared": shared,
        }
    ).hex()
    frame = _dict(dept.frame) if dept is not None else {}
    grant = {
        # Every tool, also for a binding (Ruling T3-C1); `setup` and
        # `shared` above stay scoped to `tool_key`.
        "tools": _grant_tools(frame, narrowing, _agent_threshold(agent)),
        "frame": {k: v for k, v in frame.items() if k != "tools"},
        "skills": await _grant_skills(db, agent),
    }
    # Stored as JSONB: round-trip now, so a stored grant and a fresh one
    # compare in the same shape.
    return SplitFingerprint(core, json.loads(json.dumps(grant, default=str)))


def stored_split(split: Mapping[str, Any] | None) -> SplitFingerprint | None:
    """A stored split this code reads, or None (none, or another version)."""
    if not isinstance(split, Mapping) or split.get("v") != SPLIT_VERSION:
        return None
    core, grant = split.get("core"), split.get("grant")
    if not isinstance(core, str) or not isinstance(grant, Mapping):
        return None
    return SplitFingerprint(core, dict(grant))


async def confirmation_holds(
    db: AsyncSession,
    agent: m.Agent,
    tool_key: str | None,
    *,
    version_id: uuid.UUID | None,
    digest: str | None,
    split: Mapping[str, Any] | None,
) -> bool:
    """Whether a stamped confirmation still holds. No digest: never. No
    split (stamped before it existed): the stored digest equals the current
    one. With a split: its `core` matches and the current grant only
    tightens its grant -- even when the digest matches, because the digest
    does not cover the agent's own approval threshold, which the grant does
    (raising it must break the confirmation)."""
    if digest is None:
        return False
    stored = stored_split(split)
    if stored is None:
        return digest == await binding_fingerprint(db, agent, tool_key, version_id=version_id)
    current = await split_fingerprint(db, agent, tool_key, version_id=version_id)
    return stored.core == current.core and not grant_loosenings(stored.grant, current.grant)


async def grant_holds(
    db: AsyncSession,
    agent: m.Agent,
    tool_key: str | None,
    *,
    version_id: uuid.UUID | None,
    split: Mapping[str, Any] | None,
) -> bool:
    """Whether the current grant only tightens a stored split's grant, its
    `core` aside. For a caller that proved `core` another way (the
    consolidation pass, whose own folding changes the setup in `core`)."""
    stored = stored_split(split)
    if stored is None:
        return False
    current = await split_fingerprint(db, agent, tool_key, version_id=version_id)
    return not grant_loosenings(stored.grant, current.grant)


async def stamp_binding(db: AsyncSession, row: m.AgentToolAccess, agent: m.Agent) -> None:
    """Confirm the binding against the agent's published version as it is now."""
    version_id = agent.current_version_id
    row.binding_fingerprint = await binding_fingerprint(
        db, agent, row.tool_key, version_id=version_id
    )
    split = await split_fingerprint(db, agent, row.tool_key, version_id=version_id)
    row.binding_split = split.to_json()


async def _trigger_agent(db: AsyncSession, trigger: m.Trigger) -> m.Agent | None:
    agent = await db.get(m.Agent, trigger.agent_id)
    if agent is None or agent.tenant_id != trigger.tenant_id:
        return None
    return agent


async def stamp_trigger(db: AsyncSession, trigger: m.Trigger) -> None:
    """Confirm the trigger's agent (every tool) as it is now; both None when
    the agent is gone."""
    agent = await _trigger_agent(db, trigger)
    if agent is None:
        trigger.access_fingerprint = None
        trigger.access_split = None
        return
    version_id = agent.current_version_id
    trigger.access_fingerprint = await binding_fingerprint(db, agent, None, version_id=version_id)
    trigger.access_split = (
        await split_fingerprint(db, agent, None, version_id=version_id)
    ).to_json()


async def trigger_confirmed(
    db: AsyncSession, trigger: m.Trigger, *, version_id: uuid.UUID | None = None
) -> bool:
    """Whether the trigger's creator confirmed its agent as a run of
    `version_id` (default: the agent's published one) executes it. Never
    without a stamp or an agent."""
    if trigger.access_fingerprint is None:
        return False
    agent = await _trigger_agent(db, trigger)
    if agent is None:
        return False
    return await confirmation_holds(
        db,
        agent,
        None,
        version_id=version_id or agent.current_version_id,
        digest=trigger.access_fingerprint,
        split=trigger.access_split,
    )
