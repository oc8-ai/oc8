"""What waits for a personal department's owner, and their confirmation of it.

An administrator's change to a department with an owner never widens what
the owner confirmed on their own: a loosening of the frame tools waits in
`pending_frame_tools` (`personal.apply_held_tools`), a skill added or changed
since waits per agent (`connections.confirmation.held_skill_entries`), and a
change to an agent (its version, its department's goal, a setup, its
approval threshold) leaves the owner's confirmations of it unconfirmed until
they confirm again.

`pending_confirmation` shows the owner the full current state of whatever
changed (Ruling T9-I2) -- only digests of what they confirmed before are
kept, so the current state is what can be shown -- with a token over
everything one confirmation accepts. `confirm_pending` accepts it all in one
step, against that token, and checks after stamping that the stamps recorded
what the token pinned (Ruling T9-I1).

Each held part is replaced, never merged: a later administrator's write that
only tightens clears an earlier held loosening (`pending_frame_tools` None),
and the owner is then asked about nothing for it.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import payload_hash
from oc8.audit import append_event
from oc8.authz.pdp import ToolPolicy
from oc8.authz.tightening import grant_loosenings, grant_tool_entry, policy_loosenings
from oc8.connections.access import _setup_parts
from oc8.connections.confirmation import (
    SplitFingerprint,
    agent_threshold,
    confirmation_holds,
    grant_tools_at,
    held_skill_entries,
    owner_confirmations,
    split_fingerprint,
    stamp_binding,
    stamp_trigger,
    stored_split,
    trigger_confirmed,
)
from oc8.departments.frames import validate_frame_tools
from oc8.departments.personal import _apply_frame, _live_agents, lock_owned_frame

#: The audit action of an owner's confirmation.
CONFIRMED_ACTION = "department.confirmation.confirmed"

#: The approval fields of a grant tool entry, where the agent's own
#: threshold is folded in (`connections.confirmation._tool_entry`).
_APPROVAL_FIELDS = frozenset({"approval_eur", "agent_approval_eur"})


class ConfirmationChanged(Exception):
    """What waits for the owner changed since they were shown it."""


class NotTheOwner(Exception):
    """Only a department's owner confirms what waits for them."""


@dataclass(frozen=True)
class ToolChange:
    """A frame tool whose held policy loosens the applied one. Both
    policies as the runtime reads them (`ToolPolicy.to_json`); `applied`
    None when the department grants nothing for the key now."""

    key: str
    applied: dict[str, Any] | None
    pending: dict[str, Any] | None


@dataclass(frozen=True)
class SkillChange:
    """A skill held back from the department's agents. `status`: "new" (not
    confirmed at all) or "changed" (a version, overrides, name or
    description the owner has not confirmed)."""

    name: str
    description: str | None
    status: str


@dataclass(frozen=True)
class LoginView:
    """A login a setup uses: its name, never a secret."""

    name: str
    #: Whether the department's owner owns it (their own login).
    owned_by_owner: bool


@dataclass(frozen=True)
class SetupView:
    """A tool's setup as the agent's runs use it, without secrets: the
    `config` shows its keys only, a login its name and whose it is."""

    tool: str
    server_url: str | None
    transport: str | None
    scopes: Any
    config_keys: list[str]
    logins: list[LoginView]


@dataclass(frozen=True)
class AgentChange:
    """A live agent with an owner confirmation that no longer holds.
    `changes` (sorted): "core" (its version, a setup, or the department's
    goal or capa defaults), "frame" (the department's frame but its tools),
    "tools", "skills", "threshold" (its own approval threshold)."""

    agent_id: uuid.UUID
    name: str
    changes: list[str]
    approval_threshold: Any
    #: With "tools" or "threshold": every tool it may use now, as the grant
    #: records it (`grant_tool_entry`, with `frame_extra`/`agent_extra`, the
    #: fields the policy does not read, such as a default login).
    tools: dict[str, Any] | None = None
    #: With "core": the setups its runs use now.
    setup: list[SetupView] | None = None


@dataclass(frozen=True)
class DepartmentView:
    """The department as the owner would confirm it: its goal and every
    frame key but the tools."""

    goal: str
    frame: dict[str, Any]
    #: What the department's tools reset to (`frame_capa_defaults`).
    frame_capa_defaults: dict[str, Any] | None


@dataclass(frozen=True)
class PendingConfirmation:
    tools: list[ToolChange]
    skills: list[SkillChange]
    agents: list[AgentChange]
    #: Shown when an agent's change may come from the department ("core"
    #: or "frame"): a digest cannot tell which part of `core` moved.
    department: DepartmentView | None
    #: Pins the summary to everything a confirmation would accept now.
    token: str

    @property
    def needed(self) -> bool:
        return bool(self.tools or self.skills or self.agents)


@dataclass
class _Pin:
    """What a stamp of one agent must record, as the token pinned it: the
    `core` per tool key the owner's bindings scope (None: a trigger's,
    every tool), and the grant's skills and frame."""

    cores: dict[str | None, str] = field(default_factory=dict)
    skills: Any = None
    frame: Any = None
    tools: Any = None


def _readable(policy: Any) -> dict[str, Any] | None:
    """A stored policy as the runtime reads it, plus the fields it does not
    read (a default login, say) as stored; None when it does not read."""
    if not isinstance(policy, Mapping):
        return None
    try:
        read = ToolPolicy.from_json(dict(policy), strict=True).to_json()
    except (KeyError, TypeError, ValueError):
        return None
    extras = {k: v for k, v in policy.items() if k not in read and v is not None}
    return {**read, **extras}


def _ids(values: Any) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for value in values if isinstance(values, list) else []:
        try:
            out.append(uuid.UUID(str(value)))
        except ValueError:
            continue  # a mode, or "None"
    return out


async def _logins(
    db: AsyncSession, ids: list[uuid.UUID], owner: uuid.UUID | None
) -> list[LoginView]:
    out: list[LoginView] = []
    for login_id in ids:
        credential = await db.get(m.Credential, login_id)
        if credential is not None:
            out.append(
                LoginView(
                    credential.name, owner is not None and credential.owner_member_id == owner
                )
            )
            continue
        oauth = await db.get(m.OAuthConnection, login_id)
        if oauth is not None:
            label = f"{oauth.provider}: {oauth.account_label}"
            out.append(LoginView(label, owner is not None and oauth.owner_member_id == owner))
    return out


def _safe_url(url: Any) -> str | None:
    """A setup's URL as the owner may see it (Ruling T9-L1): scheme, host,
    port and path only -- never a userinfo, query or fragment, which may
    carry a secret. None for anything that does not parse as such."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    if ":" in host:  # an IPv6 address
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path}"


def _config_keys(config: Any) -> list[str]:
    return sorted(str(key) for key in config) if isinstance(config, Mapping) else []


async def _setup_view(db: AsyncSession, agent: m.Agent, dept: m.Department) -> list[SetupView]:
    """What `core` hashes of the setups (`connections.access._setup_parts`,
    every tool), shown without secrets."""
    setup, shared = await _setup_parts(db, agent, dept, None)
    owner = dept.owner_member_id
    out: list[SetupView] = []
    for conn_id, credential_id, server_url, transport, scopes, config in setup:
        conn = await db.get(m.McpConnection, uuid.UUID(conn_id))
        out.append(
            SetupView(
                tool=conn.name if conn is not None else conn_id,
                server_url=_safe_url(server_url),
                transport=transport,
                scopes=scopes,
                config_keys=_config_keys(config),
                logins=await _logins(db, _ids([credential_id]), owner),
            )
        )
    for name, definition, used in shared:
        server_url, transport, scopes, config = definition or [None, None, None, None]
        out.append(
            SetupView(
                tool=name,
                server_url=_safe_url(server_url),
                transport=transport,
                scopes=scopes,
                config_keys=_config_keys(config),
                logins=await _logins(db, _ids(used), owner),
            )
        )
    return sorted(out, key=lambda view: view.tool)


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
    that loosens no key (`held_frame_tools` holds only one that does; stored
    data may differ) lists the keys whose policy differs, so it is never
    left waiting unseen. A key the write removes is a tightening and is
    never listed."""
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
    return sorted(loosened)


def _number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


async def _tool_changes(
    db: AsyncSession, agent: m.Agent, stored: Mapping[str, Any], current: Mapping[str, Any]
) -> set[str]:
    """Whether the tool loosenings of `current` over `stored` come from the
    policies ("tools") or the agent's own approval threshold ("threshold").
    The threshold is folded into each entry's `approval_eur` as the stricter
    of the two, so the confirmed value cannot be taken apart again: an
    approval loosening is attributed to whichever of the two now sets the
    value (both when neither does)."""
    out: set[str] = set()
    own: dict[str, Any] | None = None
    mine = _number(agent_threshold(agent))
    for key in sorted(set(stored) | set(current)):
        fields = set(policy_loosenings(stored.get(key), current.get(key)))
        if not fields:
            continue
        if fields - _APPROVAL_FIELDS:
            out.add("tools")
        if "agent_approval_eur" in fields:
            out.add("threshold")
        if "approval_eur" not in fields:
            continue
        if own is None:
            own = await grant_tools_at(
                db, agent, version_id=agent.current_version_id, threshold=None
            )
        policy = _number((own.get(key) or {}).get("approval_eur"))
        if policy is None and mine is None:
            out |= {"tools", "threshold"}
        elif policy is None or (mine is not None and mine <= policy):
            out.add("threshold")
        else:
            out.add("tools")
    return out


async def _changes(
    db: AsyncSession, agent: m.Agent, stored: SplitFingerprint | None, current: SplitFingerprint
) -> set[str]:
    """The parts in which `current` no longer holds `stored`. A confirmation
    stamped before the split keeps only a digest: its change is "core"."""
    if stored is None:
        return {"core"}
    out: set[str] = set()
    if stored.core != current.core:
        out.add("core")
    loosened = grant_loosenings(stored.grant, current.grant)
    if any(item.startswith("frame:") for item in loosened):
        out.add("frame")
    if any(item.startswith("skill:") for item in loosened):
        out.add("skills")
    if any(item.startswith(("tool:", "grant:")) for item in loosened):
        then, now = stored.grant.get("tools"), current.grant.get("tools")
        found: set[str] = set()
        if isinstance(then, Mapping) and isinstance(now, Mapping):
            found = await _tool_changes(db, agent, then, now)
        out |= found or {"tools"}
    return out


async def _agent_survey(
    db: AsyncSession, agent: m.Agent, owner: uuid.UUID | None
) -> tuple[_Pin, dict[str, Any], set[str]]:
    """One agent's pin, its whole grant (every tool's), and the parts in
    which an owner confirmation of it no longer holds (empty: all hold)."""
    version_id = agent.current_version_id
    whole = await split_fingerprint(db, agent, None, version_id=version_id)
    pin = _Pin(
        skills=whole.grant.get("skills"),
        frame=whole.grant.get("frame"),
        tools=whole.grant.get("tools"),
    )
    pin.cores[None] = whole.core
    changes: set[str] = set()
    if owner is None:
        return pin, whole.grant, changes
    bindings, triggers = await owner_confirmations(db, agent, owner)
    for row in bindings:
        if row.tool_key not in pin.cores:
            scoped = await split_fingerprint(db, agent, row.tool_key, version_id=version_id)
            pin.cores[row.tool_key] = scoped.core
        if not await confirmation_holds(
            db,
            agent,
            row.tool_key,
            version_id=version_id,
            digest=row.binding_fingerprint,
            split=row.binding_split,
        ):
            now = await split_fingerprint(
                db, agent, row.tool_key, version_id=version_id, held_back=True
            )
            changes |= await _changes(db, agent, stored_split(row.binding_split), now) or {"core"}
    for trigger in triggers:
        if not await trigger_confirmed(db, trigger):
            now = await split_fingerprint(db, agent, None, version_id=version_id, held_back=True)
            changes |= await _changes(db, agent, stored_split(trigger.access_split), now) or {
                "core"
            }
    return pin, whole.grant, changes


def _skill_changes(raw: Any, held: dict[str, list[Any]] | None) -> set[SkillChange]:
    """The skills of `raw` (an agent's assignments as a stamp records them)
    that `held` (what it runs on) holds back. None held: nothing is."""
    if held is None or not isinstance(raw, Mapping):
        return set()
    out: set[SkillChange] = set()
    for aid, entry in raw.items():
        if held.get(aid) == entry or not isinstance(entry, list) or len(entry) != 4:
            continue
        name, description = entry[2], entry[3]
        if name is None:  # the skill is gone: nothing of it runs
            continue
        status = "changed" if aid in held else "new"
        out.add(SkillChange(str(name), None if description is None else str(description), status))
    return out


async def _survey(
    db: AsyncSession, dept: m.Department
) -> tuple[PendingConfirmation, dict[uuid.UUID, _Pin]]:
    owner = dept.owner_member_id
    frame = dict(dept.frame or {})
    raw_tools = frame.get("tools")
    applied: Mapping[str, Any] = raw_tools if isinstance(raw_tools, Mapping) else {}
    pending = dept.pending_frame_tools
    tools = [
        ToolChange(key, _readable(applied.get(key)), _readable((pending or {}).get(key)))
        for key in _loosened_tools(applied, pending)
    ]
    skills: set[SkillChange] = set()
    agents: list[AgentChange] = []
    pins: dict[uuid.UUID, _Pin] = {}
    pinned: list[list[Any]] = []
    live = await _live_agents(db, tenant_id=dept.tenant_id, department_ids=[dept.id])
    for agent in sorted(live, key=lambda a: str(a.id)):
        pin, grant, changes = await _agent_survey(db, agent, owner)
        held = await held_skill_entries(db, agent)
        skills |= _skill_changes(pin.skills, held)
        if changes:
            agents.append(
                AgentChange(
                    agent.id,
                    agent.name,
                    sorted(changes),
                    agent_threshold(agent),
                    tools=dict(pin.tools or {}) if changes & {"tools", "threshold"} else None,
                    setup=await _setup_view(db, agent, dept) if "core" in changes else None,
                )
            )
        pins[agent.id] = pin
        cores = sorted((str(key), core) for key, core in pin.cores.items())
        pinned.append([str(agent.id), str(agent.current_version_id), cores, grant, held])
    department = None
    if any({"core", "frame"} & set(agent.changes) for agent in agents):
        department = DepartmentView(
            goal=dept.goal or "",
            frame={k: v for k, v in frame.items() if k != "tools"},
            frame_capa_defaults=dept.frame_capa_defaults,
        )
    token = payload_hash(
        {
            "department": [dept.goal, dept.frame_capa_defaults, dept.frame],
            "pending_frame_tools": pending,
            "agents": pinned,
        }
    ).hex()
    summary = PendingConfirmation(
        tools=tools,
        skills=sorted(skills, key=lambda s: (s.name, s.description or "", s.status)),
        agents=agents,
        department=department,
        token=token,
    )
    return summary, pins


async def pending_confirmation(db: AsyncSession, dept: m.Department) -> PendingConfirmation:
    """What waits for `dept`'s owner. The token is the sha256 of the
    canonical JSON of everything one confirmation accepts (Ruling F2): the
    department's goal, capa defaults and frame (every key), the held frame
    tools, and per live agent its published version, the strict cores the
    owner's stamps would record (every tool's and each bound tool's: setup,
    shared setup), the grant (tools, frame, skills) and the skills it runs
    on meanwhile. Any change to one of them -- one the summary names or
    not -- changes the token."""
    summary, _pins = await _survey(db, dept)
    return summary


def _other_tools(tools: Any, applied: set[str]) -> dict[str, Any]:
    return {
        k: v for k, v in (tools if isinstance(tools, Mapping) else {}).items() if k not in applied
    }


def _stamped_as_pinned(
    split: Mapping[str, Any] | None, pin: _Pin, key: str | None, applied: set[str]
) -> bool:
    """Whether a stamp recorded what the token pinned: its `core`, skills,
    frame, and every tool but those the held tools just changed (`applied`:
    they change on purpose, and the token pinned them itself)."""
    stamped = stored_split(split)
    return (
        stamped is not None
        and key in pin.cores
        and stamped.core == pin.cores[key]
        and stamped.grant.get("skills") == pin.skills
        and stamped.grant.get("frame") == pin.frame
        and _other_tools(stamped.grant.get("tools"), applied) == _other_tools(pin.tools, applied)
    )


async def confirm_pending(
    db: AsyncSession, dept: m.Department, *, member: m.OrgMember, token: str
) -> PendingConfirmation:
    """The owner (`member`, else `NotTheOwner`) confirms what
    `pending_confirmation` showed them with `token`. Under the department's
    row lock the summary is computed again against the frame as last
    committed, and a token that no longer matches raises
    `ConfirmationChanged` with nothing written. Otherwise the held frame
    tools apply (validated again: a login they name may be gone; a refusal
    raises `FrameChangeRefused`) and nothing waits any more; then every
    confirmation of the owner on each live agent of the department
    (`owner_confirmations`: their own bindings and the triggers they
    created) is stamped again as theirs, which releases the held skills and
    re-confirms a changed agent.

    The stamps read live state, and not all of it is under the department
    lock (a tenant-wide skill assignment, a skill's text, a setup, the
    agent's threshold): each stamp is compared with what the token pinned
    (Rulings T9-I1, T9-I3), and any difference raises `ConfirmationChanged`
    -- the caller's transaction then rolls back. The agents are read again
    before they are stamped. Rows the session already holds (an assignment,
    a setup) may be read as the check read them rather than as committed
    since: a stamp then records what the token pinned, never anything
    newer, and the newer state shows as unconfirmed afterwards -- it can
    only fail safe. Audited without content; nothing to confirm writes
    nothing. Returns the summary as it is afterwards."""
    if dept.owner_member_id != member.id:
        raise NotTheOwner
    await lock_owned_frame(db, dept)
    current, pins = await _survey(db, dept)
    if not hmac.compare_digest(current.token, token):
        raise ConfirmationChanged
    if not current.needed:
        return current
    applied_now = dict((dept.frame or {}).get("tools") or {})
    pending_now = dict(dept.pending_frame_tools or {})
    changed = {
        key
        for key in set(applied_now) | set(pending_now)
        if dept.pending_frame_tools is not None and applied_now.get(key) != pending_now.get(key)
    }
    if dept.pending_frame_tools is not None:
        tools = await validate_frame_tools(
            db, tenant_id=dept.tenant_id, tools=dept.pending_frame_tools
        )
        await _apply_frame(db, dept=dept, new_tools=tools, hold=False)
        await db.flush()
    for agent in await _live_agents(db, tenant_id=dept.tenant_id, department_ids=[dept.id]):
        pin = pins.get(agent.id)
        if pin is None:  # an agent hired since the check: not what was shown
            raise ConfirmationChanged
        await db.refresh(agent)
        bindings, triggers = await owner_confirmations(db, agent, member.id)
        for row in bindings:
            await stamp_binding(db, row, agent, confirmed_by=member.id)
            if not _stamped_as_pinned(row.binding_split, pin, row.tool_key, changed):
                raise ConfirmationChanged
        for trigger in triggers:
            await stamp_trigger(db, trigger, confirmed_by=member.id)
            if not _stamped_as_pinned(trigger.access_split, pin, None, changed):
                raise ConfirmationChanged
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
            "agent_ids": [str(agent.agent_id) for agent in current.agents],
            "tools": [tool.key for tool in current.tools],
            "skills_count": len(current.skills),
        },
    )
    return await pending_confirmation(db, dept)
