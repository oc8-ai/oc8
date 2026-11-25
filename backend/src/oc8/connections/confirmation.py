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
  `frame_capa_defaults` and its setup of the tool (`setup`, `shared`) --
  of a fixed binding in `shared` only its mode: the login it acts through
  is judged in `grant.logins`.
- `grant` may only tighten (`authz.tightening.grant_loosenings`): `tools`
  holds one entry per enabled tool key of the effective policies -- every
  one, for a binding too (a new tool next to the bound one is an addition
  the member confirms) -- `frame` the frame without `tools`,
  `skills` `{assignment id: [skill_version_id, overrides]}`, and in a
  department with an owner also the skill's name and description as the
  model reads them in the catalogue (`[..., name, description]`), and
  `logins` (only when there is one) `{tool key: [credential id, oauth
  connection id]}` for each fixed binding in `shared`: a login deleted since
  only takes away, a new or different one never holds (Ruling T12-N2).

The agent's own approval threshold (`presentation["approval_value_eur"]`,
which the live gate adds as an extra threshold) is folded into each tool
entry's `approval_eur` as the stricter of the two, exactly as the gate takes
it: lowering it tightens, raising or removing it loosens.

A row without a split (stamped before it existed) holds while the stored
digest equals the current one: strict, as before. A row with a split holds
while its `core` matches and the current grant only tightens the stored one.
The split decides even when the digest matches, because only the grant
covers the agent's own threshold.

In a department with an owner only the owner's confirmation card
(`departments.owner_confirmation.confirm_pending`) confirms what an
administrator changed, so only it stamps the raw skill set (Ruling FR-C1).
Every other stamp is held back (`held_back=True`, the default), and an
ordinary save -- a trigger created or edited, the owner's own tool access --
stamps only where `save_may_stamp` allows it: a row whose confirmation held
before the save (an owner editing a confirmed row consents to their own
change), or a new row while every owner confirmation of the agent holds.
Otherwise the save applies and the row stays unconfirmed, for the card.
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


def agent_threshold(agent: m.Agent) -> Any:
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


async def _grant_skills(db: AsyncSession, agent: m.Agent, *, owned: bool) -> dict[str, Any]:
    """The assignments `binding_fingerprint` reads, enabled ones only, as
    `[skill_version_id, overrides]`. `owned` (a department with an owner):
    each also carries the text the model reads of its skill (name,
    description: None when the version or skill is gone), so a change to
    that text loosens too and waits for the owner (`held_skill_entries`).
    A standard department leaves the text out: a rename there must not
    break every confirmation in the tenant."""
    rows = (
        await db.execute(
            select(m.SkillAssignment, m.Skill.name, m.Skill.description)
            .outerjoin(m.SkillVersion, m.SkillVersion.id == m.SkillAssignment.skill_version_id)
            .outerjoin(m.Skill, m.Skill.id == m.SkillVersion.skill_id)
            .where(
                m.SkillAssignment.deleted_at.is_(None),
                (m.SkillAssignment.agent_id == agent.id)
                | (m.SkillAssignment.department_id == agent.department_id)
                | (
                    m.SkillAssignment.agent_id.is_(None) & m.SkillAssignment.department_id.is_(None)
                ),
            )
        )
    ).tuples()
    return {
        str(a.id): [str(a.skill_version_id), a.overrides] + ([name, description] if owned else [])
        for a, name, description in rows
        if bool(a.enabled)
    }


def _skill_entry(entry: Any) -> list[Any] | None:
    """A stored `grant.skills` entry read strictly: `[skill_version_id,
    overrides, name, description]` with a version id that parses and a name
    and description that are text or None. None for anything else."""
    if not isinstance(entry, list) or len(entry) != 4 or not isinstance(entry[0], str):
        return None
    if not all(part is None or isinstance(part, str) for part in entry[2:]):
        return None
    try:
        uuid.UUID(entry[0])
    except ValueError:
        return None
    return entry


def _skill_entries(raw: Any) -> dict[str, list[Any]]:
    """A stored skill set read strictly; a malformed one holds nothing."""
    entries: dict[str, list[Any]] = {}
    if isinstance(raw, Mapping):
        for aid, entry in raw.items():
            read = _skill_entry(entry)
            if isinstance(aid, str) and read is not None:
                entries[aid] = read
    return entries


async def _department_owner(db: AsyncSession, agent: m.Agent) -> uuid.UUID | None:
    dept = await db.get(m.Department, agent.department_id)
    return dept.owner_member_id if dept is not None else None


async def _owner_binding(db: AsyncSession, row: m.AgentToolAccess, owner: uuid.UUID) -> bool:
    """Whether `row` is the owner's own confirmation: fixed to a login or
    connection of the owner, and set by the owner. A row the system wrote
    (`set_by_member_id` None) is nobody's confirmation."""
    if row.mode != "fixed" or row.set_by_member_id != owner:
        return False
    target: m.Credential | m.OAuthConnection | None = None
    if row.credential_id is not None:
        target = await db.get(m.Credential, row.credential_id)
    elif row.oauth_connection_id is not None:
        target = await db.get(m.OAuthConnection, row.oauth_connection_id)
    return target is not None and target.owner_member_id == owner


async def owner_confirmations(
    db: AsyncSession, agent: m.Agent, owner: uuid.UUID
) -> tuple[list[m.AgentToolAccess], list[m.Trigger]]:
    """The owner's own confirmations of `agent`: its fixed rows the owner
    bound to their own login or connection (`_owner_binding`), and the
    triggers the owner created. What `held_skill_entries` reads, and what
    the owner's confirmation (`departments.owner_confirmation`) stamps."""
    bindings = [
        row
        for row in (
            await db.execute(
                select(m.AgentToolAccess).where(
                    m.AgentToolAccess.tenant_id == agent.tenant_id,
                    m.AgentToolAccess.agent_id == agent.id,
                    m.AgentToolAccess.mode == "fixed",
                )
            )
        ).scalars()
        if await _owner_binding(db, row, owner)
    ]
    triggers = list(
        (
            await db.execute(
                select(m.Trigger).where(
                    m.Trigger.tenant_id == agent.tenant_id,
                    m.Trigger.agent_id == agent.id,
                    m.Trigger.created_by_member_id == owner,
                )
            )
        ).scalars()
    )
    return bindings, triggers


async def _owner_splits(
    db: AsyncSession, agent: m.Agent, owner: uuid.UUID
) -> list[SplitFingerprint]:
    """The stored splits of the owner's own confirmations of `agent`
    (`owner_confirmations`). Rows stamped before the split are not among
    them."""
    bindings, triggers = await owner_confirmations(db, agent, owner)
    stored = [row.binding_split for row in bindings] + [t.access_split for t in triggers]
    return [split for split in map(stored_split, stored) if split is not None]


async def held_skill_entries(db: AsyncSession, agent: m.Agent) -> dict[str, list[Any]] | None:
    """The skill assignments an agent in a department with an owner runs on
    while the owner has not confirmed newer ones: `{assignment id:
    [skill_version_id, overrides, name, description]}` as every split
    confirmation of the owner holds it identically; with none left (a
    trigger deleted, a binding switched to `user`, its login deleted), the
    set the owner last confirmed (`agent.confirmed_skills`; the set it was
    created with until a stamp records one, `record_created_skills`). None
    (every enabled assignment runs, as stored live) only for a department
    without an owner, or a legacy agent with neither a split nor a recorded
    set.

    A skill added since, a moved version, changed overrides or a changed
    name or description would loosen the owner's confirmation
    (`authz.tightening`), from whichever path made the change (an admin's
    tenant-wide assignment, a publish that moves assignments, a skill
    edit, a capa install): it waits here until the owner confirms again,
    and the confirmation keeps holding meanwhile
    (`split_fingerprint(held_back=True)`). A removal or disable is a
    tightening and applies at once. Stored entries are read strictly: a
    malformed set or entry holds nothing."""
    owner = await _department_owner(db, agent)
    if owner is None:
        return None
    splits = await _owner_splits(db, agent, owner)
    if not splits:
        if agent.confirmed_skills is None:
            return None
        return _skill_entries(agent.confirmed_skills)
    held: dict[str, list[Any]] | None = None
    for split in splits:
        entries = _skill_entries(split.grant.get("skills"))
        held = (
            entries
            if held is None
            else {aid: entry for aid, entry in held.items() if entries.get(aid) == entry}
        )
    return held


async def split_fingerprint(
    db: AsyncSession,
    agent: m.Agent,
    tool_key: str | None,
    *,
    version_id: uuid.UUID | None,
    held_back: bool = False,
) -> SplitFingerprint:
    """What a person confirms, split into a strict `core` and a `grant` that
    may only tighten (module docstring). `tool_key` scopes the setup in
    `core` (None: every tool's); `grant.tools` always covers every tool.

    `held_back`: the skills as the agent runs them (`held_skill_entries`),
    for judging a stamp and for every stamp but the owner's confirmation
    card, which alone takes the raw set: that is how the owner confirms
    what waited (Ruling FR-C1)."""
    dept = await db.get(m.Department, agent.department_id)
    payload, which = await _version_view(db, agent, version_id)
    narrowing = _dict(payload.get("narrowing"))
    version = {k: v for k, v in payload.items() if k not in _OUT_OF_CORE}
    if "narrowing" in version and isinstance(version["narrowing"], Mapping):
        version["narrowing"] = {k: v for k, v in narrowing.items() if k != "tools"}
    setup, shared = await _setup_parts(db, agent, dept, tool_key)
    shared, logins = await _fixed_logins(db, agent, shared)
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
    skills = await _grant_skills(
        db, agent, owned=dept is not None and dept.owner_member_id is not None
    )
    if held_back:
        entries = await held_skill_entries(db, agent)
        if entries is not None:
            skills = {aid: entries[aid] for aid in skills if aid in entries}
    grant = {
        # Every tool, also for a binding (Ruling T3-C1); `setup` and
        # `shared` above stay scoped to `tool_key`.
        "tools": _grant_tools(frame, narrowing, agent_threshold(agent)),
        "frame": {k: v for k, v in frame.items() if k != "tools"},
        "skills": skills,
    }
    if logins:
        grant["logins"] = logins
    # Stored as JSONB: round-trip now, so a stored grant and a fresh one
    # compare in the same shape.
    return SplitFingerprint(core, json.loads(json.dumps(grant, default=str)))


async def _fixed_logins(
    db: AsyncSession, agent: m.Agent, shared: list[Any]
) -> tuple[list[Any], dict[str, list[str]]]:
    """`shared` with each fixed binding's login ids taken out (its mode
    stays), and those ids by tool key. A `shared` entry's login part is the
    agent's access row whenever it has one (`_shared_setup`)."""
    rows = {
        row.tool_key: row
        for row in (
            await db.execute(
                select(m.AgentToolAccess).where(
                    m.AgentToolAccess.agent_id == agent.id,
                    m.AgentToolAccess.mode == "fixed",
                )
            )
        ).scalars()
    }
    out: list[Any] = []
    logins: dict[str, list[str]] = {}
    for entry in shared:
        name, definition, _used = entry
        row = rows.get(name)
        if row is None:
            out.append(entry)
            continue
        out.append([name, definition, [row.mode]])
        logins[name] = [str(row.credential_id), str(row.oauth_connection_id)]
    return out, logins


async def grant_tools_at(
    db: AsyncSession, agent: m.Agent, *, version_id: uuid.UUID | None, threshold: Any
) -> dict[str, Any]:
    """`grant.tools` as `split_fingerprint` builds it, with `threshold` in
    place of the agent's own approval threshold (None: no threshold), so a
    caller can tell a loosening of the policies from one of the threshold."""
    dept = await db.get(m.Department, agent.department_id)
    payload, _which = await _version_view(db, agent, version_id)
    frame = _dict(dept.frame) if dept is not None else {}
    return _grant_tools(frame, _dict(payload.get("narrowing")), threshold)


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
    current = await split_fingerprint(db, agent, tool_key, version_id=version_id, held_back=True)
    return stored.core == current.core and not grant_loosenings(stored.grant, current.grant)


async def _owner_confirmations_hold(db: AsyncSession, agent: m.Agent, owner: uuid.UUID) -> bool:
    """Whether every confirmation of `owner` on `agent` still holds (none:
    nothing is waiting for it)."""
    version_id = agent.current_version_id
    bindings, triggers = await owner_confirmations(db, agent, owner)
    for row in bindings:
        if not await confirmation_holds(
            db,
            agent,
            row.tool_key,
            version_id=version_id,
            digest=row.binding_fingerprint,
            split=row.binding_split,
        ):
            return False
    for trigger in triggers:
        if not await trigger_confirmed(db, trigger, version_id=version_id):
            return False
    return True


async def save_may_stamp(db: AsyncSession, agent: m.Agent, *, held: bool | None) -> bool:
    """Whether an ordinary save (not the owner's confirmation card) may stamp
    its row as confirmed (Ruling FR-C1). Called before the save changes
    anything. `held`: whether the saved row's own confirmation held before
    the save; None for a row that is no confirmation yet (a new trigger, a
    binding that becomes the owner's).

    Outside a department with an owner, always: there the person saving is
    the one who confirms. In one, a row that held may be stamped again (its
    owner editing it consents to their own change), one that did not stays
    unconfirmed: only the card confirms what changed meanwhile. A new row
    may be stamped while every owner confirmation of the agent holds --
    then nothing waits that the stamp could confirm unseen."""
    owner = await _department_owner(db, agent)
    if owner is None:
        return True
    if held is not None:
        return held
    return await _owner_confirmations_hold(db, agent, owner)


async def binding_held(
    db: AsyncSession, row: m.AgentToolAccess | None, agent: m.Agent
) -> bool | None:
    """`save_may_stamp`'s `held` for a save of `row` (None: not written yet):
    whether it is the owner's own confirmation and holds; None when it is
    no owner confirmation (no row, a department without an owner, a row
    somebody else set)."""
    owner = await _department_owner(db, agent)
    if row is None or owner is None or not await _owner_binding(db, row, owner):
        return None
    return await confirmation_holds(
        db,
        agent,
        row.tool_key,
        version_id=agent.current_version_id,
        digest=row.binding_fingerprint,
        split=row.binding_split,
    )


async def record_created_skills(db: AsyncSession, agent: m.Agent) -> None:
    """Ruling FR-I2: an agent created in a department with an owner (the
    member's hire, an agent the owner creates) starts from the skill set it
    was created with, so a skill an administrator adds later waits for the
    owner even before any stamp recorded a confirmation. Leaves a recorded
    set alone; nothing outside an owned department."""
    if agent.confirmed_skills is not None or await _department_owner(db, agent) is None:
        return
    skills = await _grant_skills(db, agent, owned=True)
    agent.confirmed_skills = json.loads(json.dumps(skills, default=str))


def _record_confirmed(agent: m.Agent, split: SplitFingerprint) -> None:
    """Keep the skills of the owner's newest confirmation on the agent, for
    when every confirmation is gone (`held_skill_entries`). A held-back
    stamp records the held set, so it never records a waiting skill."""
    skills = split.grant.get("skills")
    agent.confirmed_skills = dict(skills) if isinstance(skills, Mapping) else {}


async def stamp_binding(
    db: AsyncSession,
    row: m.AgentToolAccess,
    agent: m.Agent,
    *,
    confirmed_by: uuid.UUID | None,
    held_back: bool = True,
) -> None:
    """Confirm the binding against the agent's published version as it is now.
    `confirmed_by`: the member who confirms (None: the system). Only the
    department owner confirming their own binding records the skills they
    confirmed (`agent.confirmed_skills`). `held_back` (the default): the
    skills that wait for the owner keep waiting (`held_skill_entries`);
    only the owner's confirmation card (`confirm_pending`) passes False and
    takes them all (Ruling FR-C1). Outside a department with an owner
    nothing is held back, so it changes nothing there."""
    version_id = agent.current_version_id
    row.binding_fingerprint = await binding_fingerprint(
        db, agent, row.tool_key, version_id=version_id
    )
    split = await split_fingerprint(
        db, agent, row.tool_key, version_id=version_id, held_back=held_back
    )
    row.binding_split = split.to_json()
    owner = await _department_owner(db, agent)
    if owner is not None and confirmed_by == owner and await _owner_binding(db, row, owner):
        _record_confirmed(agent, split)


async def _trigger_agent(db: AsyncSession, trigger: m.Trigger) -> m.Agent | None:
    agent = await db.get(m.Agent, trigger.agent_id)
    if agent is None or agent.tenant_id != trigger.tenant_id:
        return None
    return agent


async def stamp_trigger(
    db: AsyncSession,
    trigger: m.Trigger,
    *,
    confirmed_by: uuid.UUID | None,
    held_back: bool = True,
) -> None:
    """Confirm the trigger's agent (every tool) as it is now; both None when
    the agent is gone. `confirmed_by` and `held_back` as for `stamp_binding`;
    only the owner confirming a trigger they created records the skills."""
    agent = await _trigger_agent(db, trigger)
    if agent is None:
        trigger.access_fingerprint = None
        trigger.access_split = None
        return
    version_id = agent.current_version_id
    trigger.access_fingerprint = await binding_fingerprint(db, agent, None, version_id=version_id)
    split = await split_fingerprint(db, agent, None, version_id=version_id, held_back=held_back)
    trigger.access_split = split.to_json()
    owner = await _department_owner(db, agent)
    if owner is not None and confirmed_by == owner and trigger.created_by_member_id == owner:
        _record_confirmed(agent, split)


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
