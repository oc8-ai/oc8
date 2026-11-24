"""What waits for a personal department's owner, and their confirmation of it.

An administrator's change to a department with an owner never widens what
the owner confirmed on their own: a loosening of the frame tools waits in
`pending_frame_tools` (`personal.apply_held_tools`), a skill added or changed
since waits per agent (`connections.confirmation.held_skill_entries`), and a
change to an agent the owner made themselves (a new mission, say) leaves its
confirmations unconfirmed until they confirm again.

`pending_confirmation` sums that up for the owner, with a token over
everything one confirmation would accept; `confirm_pending` accepts it all in
one step, against that token. Each held part is replaced, never merged: a
later administrator's write that only tightens clears an earlier held
loosening (`pending_frame_tools` None), and the owner is then asked about
nothing for it.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import payload_hash
from oc8.audit import append_event
from oc8.authz.pdp import ToolPolicy
from oc8.authz.tightening import grant_loosenings, grant_tool_entry
from oc8.connections.confirmation import (
    confirmation_holds,
    held_skill_entries,
    owner_confirmations,
    split_fingerprint,
    stamp_binding,
    stamp_trigger,
    trigger_confirmed,
)
from oc8.departments.frames import validate_frame_tools
from oc8.departments.personal import _apply_frame, _live_agents, lock_owned_frame

#: The audit action of an owner's confirmation.
CONFIRMED_ACTION = "department.confirmation.confirmed"


class ConfirmationChanged(Exception):
    """What waits for the owner changed since they were shown it."""


@dataclass(frozen=True)
class PendingConfirmation:
    #: The frame tool keys whose held policy loosens the applied one.
    tools: list[str]
    #: The names of the skills held back from the department's agents.
    skills: list[str]
    #: The live agents (id, name) with an owner confirmation that no longer holds.
    agents: list[tuple[uuid.UUID, str]]
    #: Pins the summary to everything a confirmation would accept now.
    token: str

    @property
    def needed(self) -> bool:
        return bool(self.tools or self.skills or self.agents)


def _grant_entry(policy: Any) -> dict[str, Any] | None:
    """A frame policy as a grant tool entry, None when it does not read."""
    if not isinstance(policy, Mapping):
        return None
    try:
        return grant_tool_entry(policy, None, ToolPolicy.from_json(dict(policy), strict=True))
    except (KeyError, TypeError, ValueError):
        return None


def _loosened_tools(applied: Mapping[str, Any], pending: Mapping[str, Any] | None) -> list[str]:
    """The keys of `pending` that grant more than `applied`. A held write
    that differs without loosening any key (it cannot, as `held_frame_tools`
    holds one; but stored data may) lists the keys that differ, so it is
    never left waiting unseen."""
    if pending is None:
        return []
    current: dict[str, Any] = {}
    confirmed: dict[str, Any] = {}
    unreadable: list[str] = []
    for key, policy in pending.items():
        entry = _grant_entry(policy)
        if entry is None:
            unreadable.append(key)
            continue
        current[key] = entry
        held = _grant_entry(applied.get(key))
        if held is not None:
            confirmed[key] = held
    loosened = {
        item.removeprefix("tool:")
        for item in grant_loosenings({"tools": confirmed}, {"tools": current})
        if item.startswith("tool:")
    } | set(unreadable)
    if not loosened:
        loosened = {key for key in pending if pending.get(key) != applied.get(key)}
        loosened |= {key for key in applied if key not in pending}
    return sorted(loosened)


async def _broken(db: AsyncSession, agent: m.Agent, owner: uuid.UUID) -> bool:
    """Whether one of the owner's confirmations of `agent` no longer holds."""
    bindings, triggers = await owner_confirmations(db, agent, owner)
    for row in bindings:
        if not await confirmation_holds(
            db,
            agent,
            row.tool_key,
            version_id=agent.current_version_id,
            digest=row.binding_fingerprint,
            split=row.binding_split,
        ):
            return True
    for trigger in triggers:
        if not await trigger_confirmed(db, trigger):
            return True
    return False


async def pending_confirmation(db: AsyncSession, dept: m.Department) -> PendingConfirmation:
    """What waits for `dept`'s owner. The token is the sha256 of the
    canonical JSON of everything one confirmation accepts (Ruling F2): the
    department's goal, capa defaults and frame (every key), the held frame
    tools, and per live agent its published version, the strict core and
    the grant a stamp would record (setup, shared setup, tools, skills) and
    the skills it runs on meanwhile. Any change to one of them -- one the
    summary names or not -- changes the token."""
    owner = dept.owner_member_id
    applied = (dept.frame or {}).get("tools") or {}
    pending = dept.pending_frame_tools
    tools = _loosened_tools(applied if isinstance(applied, Mapping) else {}, pending)
    skills: set[str] = set()
    agents: list[tuple[uuid.UUID, str]] = []
    pinned: list[list[Any]] = []
    live = await _live_agents(db, tenant_id=dept.tenant_id, department_ids=[dept.id])
    for agent in sorted(live, key=lambda a: str(a.id)):
        split = await split_fingerprint(db, agent, None, version_id=agent.current_version_id)
        held = await held_skill_entries(db, agent)
        raw = split.grant.get("skills")
        if held is not None and isinstance(raw, Mapping):
            skills |= {
                str(entry[2])
                for aid, entry in raw.items()
                if held.get(aid) != entry and isinstance(entry, list) and entry[2:3] != [None]
            }
        if owner is not None and await _broken(db, agent, owner):
            agents.append((agent.id, agent.name))
        pinned.append([str(agent.id), str(agent.current_version_id), split.core, split.grant, held])
    token = payload_hash(
        {
            "department": [dept.goal, dept.frame_capa_defaults, dept.frame],
            "pending_frame_tools": pending,
            "agents": pinned,
        }
    ).hex()
    return PendingConfirmation(tools=tools, skills=sorted(skills), agents=agents, token=token)


async def confirm_pending(
    db: AsyncSession, dept: m.Department, *, member: m.OrgMember, token: str
) -> PendingConfirmation:
    """The owner (`member`) confirms what `pending_confirmation` showed them
    with `token`. Under the department's row lock the summary is computed
    again against the frame as last committed, and a token that no longer
    matches raises `ConfirmationChanged` with nothing written. Otherwise the
    held frame tools apply (validated again: a login they name may be gone)
    and nothing waits any more; then every confirmation of the owner on
    each live agent of the department (`owner_confirmations`: their own
    bindings and the triggers they created) is stamped again as theirs,
    which releases the held skills and re-confirms a changed agent. Audited
    without content; nothing to confirm writes nothing. Returns the summary
    as it is afterwards."""
    if dept.owner_member_id != member.id:
        raise ValueError("only the department's owner confirms what waits for them")
    await lock_owned_frame(db, dept)
    current = await pending_confirmation(db, dept)
    if not hmac.compare_digest(current.token, token):
        raise ConfirmationChanged
    if not current.needed:
        return current
    if dept.pending_frame_tools is not None:
        tools = await validate_frame_tools(
            db, tenant_id=dept.tenant_id, tools=dept.pending_frame_tools
        )
        await _apply_frame(db, dept=dept, new_tools=tools, hold=False)
        await db.flush()
    for agent in await _live_agents(db, tenant_id=dept.tenant_id, department_ids=[dept.id]):
        bindings, triggers = await owner_confirmations(db, agent, member.id)
        for row in bindings:
            await stamp_binding(db, row, agent, confirmed_by=member.id)
        for trigger in triggers:
            await stamp_trigger(db, trigger, confirmed_by=member.id)
    await db.flush()
    await append_event(
        db,
        tenant_id=dept.tenant_id,
        actor_type="operator",
        actor_id=member.id,
        category="authz",
        action=CONFIRMED_ACTION,
        resource={
            "department_id": str(dept.id),
            "agent_ids": [str(agent_id) for agent_id, _name in current.agents],
            "tools": current.tools,
            "skills_count": len(current.skills),
        },
    )
    return await pending_confirmation(db, dept)
