"""Turn a tenant's per-department tool setups into shared logins (unified
connections design, "Migration").

Before the one-login model every department that set up a login tool got its
own `McpConnection` with the login's values copied into its env, and every
`POST /mcp/logins` an extra row pinned to one credential. `consolidate_tenant`
folds them, per tool, into the model the resolver now reads:

1. Department rows that carry copied login values: grouped by equal values
   (plain values and secret refs); each group reuses an equal organisation
   login or becomes a new one named "<Integration> — <first department>".
   A new login stores its own copy of each secret under its own ref (the
   old setup's secret stays as it was, never shared or deleted). The login
   is enabled for the group's departments. Incomplete values are kept --
   the login then shows as incomplete instead of failing silently, and is
   never replaced by another login.
2. The organisation's single login, which the resolver handed out while no
   login of the type was enabled anywhere, is enabled for every department
   whose frame enables the tool -- except a department that had its own
   copied login (step 1, folded first) or whose default names a login row.
3. Rows pinned to a login become an agent's fixed access (`AgentToolAccess`)
   for every agent pinning them, written as a system change.
4. The oldest row stays as the tool's one row (no department, no login, no
   copied values); narrowing pins and department defaults that named another
   row now name it. The other rows are deleted -- or, while an unfinished
   run still names one, retired (kept, disconnected, pointing at the tool
   row) and removed by a later pass.
5. A row some login's `consolidated_from` already names is left alone, so a
   second pass changes nothing.

Confirmations of a person's account (a trigger's `access_fingerprint`, a
binding's `binding_fingerprint`) that were valid before are stamped again
afterwards, so a confirmed schedule keeps running. Generic over each tool
pack's `credential_env_fields`; no vendor is named here.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import META_KEY, payload_hash
from oc8.audit import append_event
from oc8.capas.manifest import CredentialTypeSpec
from oc8.connections.access import (
    binding_fingerprint,
    single_organisation_credential,
    trigger_fingerprint,
)
from oc8.connections.access_write import write_tool_access
from oc8.connections.confirmation import (
    confirmation_holds,
    stamp_binding,
    stamp_trigger,
    stored_split,
    trigger_confirmed,
)
from oc8.connections.credential_env import credential_env_targets
from oc8.connections.integrations import Integration, integration_for_connection
from oc8.connections.login import enabled_anywhere
from oc8.connections.tool_connection import FOLDED_KEY, RETIRED_KEY
from oc8.credentials.enablement import enable_for_department, enabled_credentials
from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type
from oc8.credentials.service import secret_ref_for
from oc8.runtime.states import TERMINAL
from oc8.secrets.service import SecretNotFound, resolve_secret, store_secret

logger = logging.getLogger(__name__)


@dataclass
class ConsolidationReport:
    """What one pass did (or, `dry_run`, would do). Ids and counts only."""

    tenant_id: uuid.UUID
    dry_run: bool = False
    #: Organisation logins created from copied values.
    created: int = 0
    #: Department enablements added (one per login and department).
    enabled: int = 0
    #: Rows removed because the tool's one row took their place.
    collapsed: int = 0
    #: Narrowing pins and department defaults now naming the tool row.
    pins_rewritten: int = 0
    #: Rows left alone (already consolidated, or no login tool).
    skipped: int = 0
    #: Fixed accesses written for agents that pinned a login row.
    fixed: int = 0
    #: Stored confirmations stamped again.
    restamped: int = 0
    #: Rows kept (disconnected) because an unfinished run still names them.
    retired: int = 0
    #: Rows retired by an earlier pass, removed now that no run names them.
    purged: int = 0
    #: Departments given the organisation's single login they already used.
    kept_single: int = 0
    #: Agents whose unpublished pin differed from the published one (ignored).
    draft_pins_ignored: int = 0
    tools: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.created
            or self.enabled
            or self.collapsed
            or self.pins_rewritten
            or self.fixed
            or self.retired
            or self.purged
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "dry_run": self.dry_run,
            "created": self.created,
            "enabled": self.enabled,
            "collapsed": self.collapsed,
            "pins_rewritten": self.pins_rewritten,
            "skipped": self.skipped,
            "fixed": self.fixed,
            "restamped": self.restamped,
            "retired": self.retired,
            "purged": self.purged,
            "kept_single": self.kept_single,
            "draft_pins_ignored": self.draft_pins_ignored,
        }


def _cfg(conn: m.McpConnection) -> dict[str, Any]:
    return conn.config if isinstance(conn.config, dict) else {}


def _env_maps(conn: m.McpConnection) -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = _cfg(conn)
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    secret_env = cfg.get("secret_env") if isinstance(cfg.get("secret_env"), dict) else {}
    return dict(env or {}), dict(secret_env or {})


def _field_targets(
    cred_type: CredentialTypeSpec | None, credential_env_fields: dict[str, str]
) -> dict[str, list[str]]:
    """field key -> the env names carrying it (mapped ones; an unmapped field
    its own key)."""
    by_field: dict[str, list[str]] = defaultdict(list)
    for env_name, field_key in sorted(credential_env_fields.items()):
        by_field[field_key].append(env_name)
    if cred_type is not None:
        for f in cred_type.fields:
            by_field.setdefault(f.key, [f.key])
    return dict(by_field)


@dataclass(frozen=True)
class _Copied:
    """The login values copied into a department row."""

    plain: tuple[tuple[str, str], ...]
    refs: tuple[tuple[str, str], ...]
    #: Secret fields held as plain env (stored once the login exists).
    plain_secrets: tuple[tuple[str, str], ...]

    @property
    def empty(self) -> bool:
        return not (self.plain or self.refs or self.plain_secrets)


def _copied_login(
    conn: m.McpConnection, cred_type: CredentialTypeSpec | None, integration: Integration
) -> _Copied:
    env, secret_env = _env_maps(conn)
    secret_keys = (
        {f.key for f in cred_type.fields if f.kind == "password"}
        if cred_type is not None
        else set()
    )
    plain: dict[str, str] = {}
    refs: dict[str, str] = {}
    plain_secrets: dict[str, str] = {}
    for field_key, targets in _field_targets(cred_type, integration.credential_env_fields).items():
        for target in targets:
            ref = secret_env.get(target)
            value = env.get(target)
            if ref and field_key not in refs:
                refs[field_key] = str(ref)
            if (
                value not in (None, "")
                and field_key not in plain
                and field_key not in plain_secrets
            ):
                if field_key in secret_keys:
                    plain_secrets[field_key] = str(value)
                else:
                    plain[field_key] = str(value)
    for key in refs:
        plain_secrets.pop(key, None)
    return _Copied(
        plain=tuple(sorted(plain.items())),
        refs=tuple(sorted(refs.items())),
        plain_secrets=tuple(sorted(plain_secrets.items())),
    )


def _stripped_config(
    conn: m.McpConnection, cred_type: CredentialTypeSpec | None, integration: Integration
) -> dict[str, Any]:
    """The row's config without any env a login of the type sets."""
    if cred_type is not None:
        targets = credential_env_targets(cred_type, integration.credential_env_fields)
    else:
        targets = set(integration.credential_env_fields)
    cfg = copy.deepcopy(_cfg(conn))
    env, secret_env = _env_maps(conn)
    cfg["env"] = {k: v for k, v in env.items() if k not in targets}
    cfg["secret_env"] = {k: v for k, v in secret_env.items() if k not in targets}
    return cfg


def _pins(narrowing: Any) -> dict[str, str]:
    """tool key -> the connection id the narrowing pins it to."""
    tools = narrowing.get("tools") if isinstance(narrowing, dict) else None
    out: dict[str, str] = {}
    if isinstance(tools, dict):
        for key, policy in tools.items():
            if isinstance(policy, dict) and policy.get("connection_id"):
                out[key] = str(policy["connection_id"])
    return out


def _rewrite(tools_holder: Any, key_name: str, moved: dict[str, str]) -> tuple[Any, int]:
    """A copy of a narrowing (or frame) whose `tools[*][key_name]` naming a
    moved row names its successor; and how many it changed."""
    if not isinstance(tools_holder, dict) or not isinstance(tools_holder.get("tools"), dict):
        return tools_holder, 0
    out = copy.deepcopy(tools_holder)
    count = 0
    for policy in out["tools"].values():
        if isinstance(policy, dict):
            old = policy.get(key_name)
            if old is not None and str(old) in moved:
                policy[key_name] = moved[str(old)]
                count += 1
    return out, count


async def _unique_name(db: AsyncSession, wanted: str) -> str:
    """`wanted`, or `wanted (n)` when a login of any owner has that name."""
    taken = set((await db.execute(select(m.Credential.name))).scalars())
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted} ({n})" in taken:
        n += 1
    return f"{wanted} ({n})"


async def _secret_values(
    db: AsyncSession, tenant_id: uuid.UUID, refs: dict[str, str]
) -> dict[str, str]:
    """The values behind `refs` (field key -> secret ref); a secret that is
    gone is left out -- the login then shows that field as missing."""
    out: dict[str, str] = {}
    for key, ref in sorted(refs.items()):
        try:
            out[key] = await resolve_secret(db, tenant_id=tenant_id, ref=ref)
        except SecretNotFound:
            continue
    return out


async def _live_run_references(db: AsyncSession) -> set[str]:
    """Connection ids an unfinished run still names in its context."""
    out: set[str] = set()
    terminal = [state.value for state in TERMINAL]
    for context in (
        await db.execute(select(m.AgentRun.context).where(m.AgentRun.state.not_in(terminal)))
    ).scalars():
        if not isinstance(context, dict):
            continue
        if context.get("mcp_connection_id"):
            out.add(str(context["mcp_connection_id"]))
        ids = context.get("mcp_connection_ids")
        if isinstance(ids, list):
            out.update(str(i) for i in ids)
    return out


class _Stamps:
    """Confirmations valid before the pass, stamped again after it."""

    def __init__(self) -> None:
        self.triggers: list[m.Trigger] = []
        self.bindings: list[m.AgentToolAccess] = []

    async def collect(
        self,
        db: AsyncSession,
        agents: dict[uuid.UUID, m.Agent],
        legacy_departments: set[uuid.UUID],
    ) -> None:
        """A confirmation is valid when it equals the current digest; the
        digest as stored before consolidated tools were part of it (legacy)
        is accepted only for agents whose department's rows this pass folds
        -- the one pass that changes what their digest covers. Both only for
        a row without a split. A row with a split is valid only when it holds
        (`confirmation_holds` / `trigger_confirmed`, Ruling T3-C2): the
        digest misses parts of `core` (the payload as it runs) and the
        agent's own threshold, so a digest match never restamps one. This
        runs before the pass folds anything, so an unchanged split holds."""
        for trigger in (
            await db.execute(select(m.Trigger).where(m.Trigger.access_fingerprint.is_not(None)))
        ).scalars():
            agent = agents.get(trigger.agent_id)
            if agent is None:
                continue
            accepted = {await trigger_fingerprint(db, trigger)}
            if agent.department_id in legacy_departments:
                accepted.add(
                    await binding_fingerprint(
                        db, agent, None, version_id=agent.current_version_id, legacy=True
                    )
                )
            if stored_split(trigger.access_split) is None:
                valid = trigger.access_fingerprint in accepted
            else:
                valid = await trigger_confirmed(db, trigger)
            if valid:
                self.triggers.append(trigger)
        for row in (
            await db.execute(
                select(m.AgentToolAccess).where(m.AgentToolAccess.binding_fingerprint.is_not(None))
            )
        ).scalars():
            agent = agents.get(row.agent_id)
            if agent is None:
                continue
            legacy_too = (False, True) if agent.department_id in legacy_departments else (False,)
            accepted = {
                await binding_fingerprint(
                    db, agent, row.tool_key, version_id=agent.current_version_id, legacy=legacy
                )
                for legacy in legacy_too
            }
            if stored_split(row.binding_split) is None:
                valid = row.binding_fingerprint in accepted
            else:
                valid = await confirmation_holds(
                    db,
                    agent,
                    row.tool_key,
                    version_id=agent.current_version_id,
                    digest=row.binding_fingerprint,
                    split=row.binding_split,
                )
            if valid:
                self.bindings.append(row)

    async def restamp(
        self, db: AsyncSession, tenant_id: uuid.UUID, extra: list[m.AgentToolAccess]
    ) -> int:
        """Stamp the collected confirmations (and the accesses this pass
        wrote) again; each change is audited (system, ids only)."""
        count = 0
        for trigger in self.triggers:
            agent = await db.get(m.Agent, trigger.agent_id)
            if agent is None or agent.tenant_id != trigger.tenant_id:
                continue  # its agent is gone: leave the stamp as it was
            before = trigger.access_fingerprint
            await stamp_trigger(db, trigger)
            if trigger.access_fingerprint != before:
                count += 1
                await _audit_restamp(
                    db,
                    tenant_id,
                    {"trigger_id": str(trigger.id), "agent_id": str(trigger.agent_id)},
                )
        for row in [*self.bindings, *extra]:
            agent = await db.get(m.Agent, row.agent_id)
            if agent is None:
                continue
            before = row.binding_fingerprint
            await stamp_binding(db, row, agent)
            if row.binding_fingerprint != before:
                count += 1
                await _audit_restamp(
                    db,
                    tenant_id,
                    {
                        "agent_tool_access_id": str(row.id),
                        "agent_id": str(row.agent_id),
                        "tool": row.tool_key,
                    },
                )
        await db.flush()
        return count


async def _audit_restamp(db: AsyncSession, tenant_id: uuid.UUID, resource: dict[str, Any]) -> None:
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id=None,
        category="connection",
        action="connections.confirmation_restamped",
        resource=resource,
    )


@dataclass
class _Agents:
    """Each agent with what its runs execute: the published version's
    narrowing (the working copy's when it has none published)."""

    by_id: dict[uuid.UUID, m.Agent]
    narrowing: dict[uuid.UUID, Any]
    has_version: set[uuid.UUID]

    def pins(self, agent_id: uuid.UUID) -> dict[str, str]:
        return _pins(self.narrowing.get(agent_id))

    def draft_pins(self, agent_id: uuid.UUID) -> dict[str, str]:
        agent = self.by_id[agent_id]
        return _pins(agent.narrowing)

    def enables(self, agent_id: uuid.UUID, tool_key: str) -> bool:
        narrowing = self.narrowing.get(agent_id)
        tools = narrowing.get("tools") if isinstance(narrowing, dict) else None
        policy = tools.get(tool_key) if isinstance(tools, dict) else None
        return isinstance(policy, dict) and bool(policy.get("enabled"))


async def _load_agents(db: AsyncSession) -> _Agents:
    by_id = {a.id: a for a in (await db.execute(select(m.Agent))).scalars()}
    narrowing: dict[uuid.UUID, Any] = {}
    has_version: set[uuid.UUID] = set()
    for agent in by_id.values():
        narrowing[agent.id] = agent.narrowing
        if agent.current_version_id is not None:
            version = await db.get(m.AgentVersion, agent.current_version_id)
            if version is not None and isinstance(version.payload, dict):
                narrowing[agent.id] = version.payload.get("narrowing")
                has_version.add(agent.id)
    return _Agents(by_id=by_id, narrowing=narrowing, has_version=has_version)


def _frame_enables(dept: m.Department, tool_key: str) -> bool:
    frame = dept.frame if isinstance(dept.frame, dict) else {}
    tools = frame.get("tools") if isinstance(frame.get("tools"), dict) else {}
    entry = (tools or {}).get(tool_key)
    return isinstance(entry, dict) and bool(entry.get("enabled"))


class _Pass:
    def __init__(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        report: ConsolidationReport,
        live_refs: set[str],
    ):
        self.db = db
        self.tenant_id = tenant_id
        self.report = report
        self.live_refs = live_refs
        self.new_access: list[m.AgentToolAccess] = []

    async def cred_type(self, integration: Integration) -> CredentialTypeSpec | None:
        assert integration.credential_type is not None
        try:
            return await get_credential_type(
                self.db, tenant_id=self.tenant_id, name=integration.credential_type
            )
        except CredentialTypeNotFound:
            return None

    async def enable(self, cred: m.Credential, department_id: uuid.UUID) -> None:
        """Enable `cred` for the department -- unless the department already
        uses another login of the type, which keeps winning as it did before
        (enabling a second one would make it ambiguous)."""
        if cred.owner_member_id is not None:
            return
        assert cred.credential_type
        current = await enabled_credentials(
            self.db, credential_type=cred.credential_type, department_id=department_id
        )
        if current:
            return
        await enable_for_department(self.db, cred, department_id)
        self.report.enabled += 1

    async def single_login_in_use(self, integration: Integration) -> m.Credential | None:
        """The organisation's single login of the type, while resolver step 5
        (no login of the type enabled anywhere) still hands it out; else
        None. Read before anything is folded: the first enablement ends it."""
        assert integration.credential_type is not None
        if await enabled_anywhere(self.db, integration.credential_type):
            return None
        return await single_organisation_credential(self.db, integration.credential_type)

    async def keep_single_login(
        self,
        integration: Integration,
        only: m.Credential | None,
        departments: dict[uuid.UUID, m.Department],
        login_rows: list[m.McpConnection],
        own: set[uuid.UUID],
    ) -> None:
        """Every department that reached the tool through the organisation's
        single login `only` (its frame enables the tool) keeps it: enabled
        for each of them, not for all departments, so a copied login enabled
        for another department cannot make anyone ambiguous. A department
        with its own copied login (`own`, folded first) keeps that one, and
        one whose default names a login row keeps that row's login."""
        if only is None:
            return
        # A department whose default names a login row keeps that login
        # (`bind_login_rows` enables it), not the single one.
        defaulted = {str(r.id) for r in login_rows}
        for dept in sorted(departments.values(), key=lambda d: d.id):
            if dept.id in own or _frame_default(dept, integration.tool_key) in defaulted:
                continue
            if dept.deleted_at is None and _frame_enables(dept, integration.tool_key):
                await enable_for_department(self.db, only, dept.id)
                self.report.enabled += 1
                self.report.kept_single += 1

    async def fold_department_rows(
        self,
        integration: Integration,
        rows: list[m.McpConnection],
        departments: dict[uuid.UUID, m.Department],
    ) -> set[uuid.UUID]:
        """Step 1. A login made here owns a copy of each secret under its own
        name: the old setup's secret stays as it was. Returns the departments
        whose own copied login was folded."""
        cred_type = await self.cred_type(integration)
        groups: dict[_Copied, list[m.McpConnection]] = defaultdict(list)
        own: set[uuid.UUID] = set()
        for row in rows:
            copied = _copied_login(row, cred_type, integration)
            if not copied.empty:
                groups[copied].append(row)
        org = list(
            (
                await self.db.execute(
                    select(m.Credential)
                    .where(
                        m.Credential.credential_type == integration.credential_type,
                        m.Credential.owner_member_id.is_(None),
                    )
                    .order_by(m.Credential.created_at, m.Credential.id)
                )
            ).scalars()
        )
        for copied, members in groups.items():
            members.sort(key=lambda r: (r.created_at, r.id))
            secrets = {
                **await _secret_values(self.db, self.tenant_id, dict(copied.refs)),
                **dict(copied.plain_secrets),
            }
            cred = None
            for candidate in org:
                if dict(candidate.field_values or {}) != dict(copied.plain):
                    continue
                if set((candidate.secret_refs or {}).keys()) != set(secrets):
                    continue
                if (
                    await _secret_values(self.db, self.tenant_id, dict(candidate.secret_refs))
                    == secrets
                ):
                    cred = candidate
                    break
            if cred is None:
                first = members[0].department_id
                dept = departments.get(first) if first is not None else None
                label = (
                    f"{integration.label} — {dept.name}" if dept is not None else integration.label
                )
                assert integration.credential_type is not None
                cred = m.Credential(
                    tenant_id=self.tenant_id,
                    name=await _unique_name(self.db, label),
                    credential_type=integration.credential_type,
                    field_values=dict(copied.plain),
                    secret_refs={},
                    consolidated_from=[],
                )
                self.db.add(cred)
                await self.db.flush()
                refs: dict[str, str] = {}
                for key, value in sorted(secrets.items()):
                    ref = secret_ref_for(cred.id, key)
                    await store_secret(
                        self.db, tenant_id=self.tenant_id, name=ref, value=value, kind="credential"
                    )
                    refs[key] = ref
                cred.secret_refs = refs
                org.append(cred)
                self.report.created += 1
            cred.consolidated_from = sorted(
                {*(cred.consolidated_from or []), *(str(r.id) for r in members)}
            )
            for department_id in dict.fromkeys(r.department_id for r in members):
                if department_id is not None:
                    await self.enable(cred, department_id)
                    own.add(department_id)
        await self.db.flush()
        return own

    async def bind_login_rows(
        self,
        integration: Integration,
        rows: list[m.McpConnection],
        agents: _Agents,
        departments: dict[uuid.UUID, m.Department],
    ) -> None:
        """Step 2. What a run executes decides: the published version's pin
        (a different pin in the unpublished working copy is only rewritten,
        never made the agent's access), else the department default."""
        tool_key = integration.tool_key
        for row in rows:
            assert row.credential_id is not None
            cred = await self.db.get(m.Credential, row.credential_id)
            assert cred is not None  # rows without a usable login are skipped earlier
            row_id = str(row.id)
            pinned_by: list[m.Agent] = []
            for agent_id, agent in agents.by_id.items():
                pin = agents.pins(agent_id).get(tool_key)
                draft = agents.draft_pins(agent_id).get(tool_key)
                if agent_id in agents.has_version and draft == row_id and pin != row_id:
                    self.report.draft_pins_ignored += 1
                    logger.info(
                        "connection consolidation: agent %s draft pin of %s not made its access",
                        agent_id,
                        row_id,
                    )
                if pin == row_id:
                    pinned_by.append(agent)
                    continue
                if pin or agent.department_id is None:
                    continue
                default = _frame_default(departments.get(agent.department_id), tool_key)
                if default == row_id and agents.enables(agent_id, tool_key):
                    pinned_by.append(agent)
            for dept in departments.values():
                if _frame_default(dept, tool_key) == row_id:
                    await self.enable(cred, dept.id)
            for agent in pinned_by:
                if agent.deleted_at is not None:
                    continue
                existing = await self.db.scalar(
                    select(m.AgentToolAccess.id).where(
                        m.AgentToolAccess.agent_id == agent.id,
                        m.AgentToolAccess.tool_key == tool_key,
                    )
                )
                if existing is not None:
                    # The agent's explicit access already won over the pin.
                    continue
                access = await write_tool_access(
                    self.db,
                    agent=agent,
                    tool_key=tool_key,
                    mode="fixed",
                    oauth_connection_id=None,
                    credential_id=cred.id,
                    set_by_member_id=None,
                    # The operator's earlier pin, carried over: it never
                    # needed another confirmation, and does not now.
                    set_with_manage=True,
                    actor_type="system",
                    actor_id=None,
                    audit={"consent": "consolidation", "connection_id": row_id},
                )
                self.new_access.append(access)
                self.report.fixed += 1
            cred.consolidated_from = sorted({*(cred.consolidated_from or []), row_id})
        await self.db.flush()

    async def collapse(
        self,
        integration: Integration,
        rows: list[m.McpConnection],
        agents: _Agents,
        departments: dict[uuid.UUID, m.Department],
    ) -> None:
        """Step 3. A row an unfinished run still names is retired (kept,
        disconnected, pointing at the tool row) and removed by a later pass."""
        shared = [r for r in rows if r.department_id is None and r.credential_id is None]
        keep = min(shared or rows, key=lambda r: (r.created_at, r.id))
        cred_type = await self.cred_type(integration)
        moved = {str(r.id): str(keep.id) for r in rows if r.id != keep.id}
        keep.department_id = None
        keep.credential_id = None
        cfg = _stripped_config(keep, cred_type, integration)
        folded = cfg.get(FOLDED_KEY) if isinstance(cfg.get(FOLDED_KEY), list) else []
        if moved:
            cfg[FOLDED_KEY] = sorted({*(str(i) for i in folded or []), *moved})
        keep.config = cfg
        keep.connected = any(r.connected for r in rows)
        for row in rows:
            if row.id == keep.id:
                continue
            if str(row.id) in self.live_refs:
                row.department_id = None
                row.credential_id = None
                row.connected = False
                row.config = {
                    **_stripped_config(row, cred_type, integration),
                    RETIRED_KEY: str(keep.id),
                }
                self.report.retired += 1
            else:
                await self.db.delete(row)
                self.report.collapsed += 1
        await self.db.flush()
        if not moved:
            return
        for agent in agents.by_id.values():
            narrowing, count = _rewrite(agent.narrowing, "connection_id", moved)
            if count:
                agent.narrowing = narrowing
                self.report.pins_rewritten += count
        for version in (await self.db.execute(select(m.AgentVersion))).scalars():
            payload = version.payload if isinstance(version.payload, dict) else {}
            narrowing, count = _rewrite(payload.get("narrowing"), "connection_id", moved)
            if count:
                # Only a removed row's id is replaced by its successor's: the
                # version still means what it meant, and stays comparable.
                stored = {**payload, "narrowing": narrowing}
                version.payload = stored
                version.payload_hash = payload_hash(
                    {k: v for k, v in stored.items() if k != META_KEY}
                )
                self.report.pins_rewritten += count
        for dept in departments.values():
            frame, count = _rewrite(dept.frame, "default_connection_id", moved)
            if count:
                dept.frame = frame
                self.report.pins_rewritten += count
            # A change held for the owner too: confirming it later must not
            # bring back a removed row.
            held, count = _rewrite(
                {"tools": dept.pending_frame_tools}, "default_connection_id", moved
            )
            if count:
                dept.pending_frame_tools = held["tools"]
                self.report.pins_rewritten += count
        await self.db.flush()


def _frame_default(dept: m.Department | None, tool_key: str) -> str | None:
    frame = dept.frame if dept is not None and isinstance(dept.frame, dict) else {}
    tools = frame.get("tools") if isinstance(frame.get("tools"), dict) else {}
    entry = (tools or {}).get(tool_key)
    value = entry.get("default_connection_id") if isinstance(entry, dict) else None
    return str(value) if value else None


@dataclass
class _Tool:
    name: str
    integration: Integration
    group: list[m.McpConnection]
    fresh: list[m.McpConnection]


async def _consolidate(db: AsyncSession, tenant_id: uuid.UUID, report: ConsolidationReport) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"consolidate:{tenant_id}"},
    )
    done: set[str] = set()
    for ids in (
        await db.execute(
            select(m.Credential.consolidated_from).where(
                m.Credential.consolidated_from.is_not(None)
            )
        )
    ).scalars():
        done.update(str(i) for i in ids or [])
    live_refs = await _live_run_references(db)

    rows = list(
        (
            await db.execute(
                select(m.McpConnection)
                .where(m.McpConnection.tenant_id == tenant_id)
                .order_by(m.McpConnection.created_at, m.McpConnection.id)
            )
        ).scalars()
    )
    # Rows an earlier pass retired: removed once no unfinished run names them.
    by_name: dict[str, list[m.McpConnection]] = defaultdict(list)
    for row in rows:
        if RETIRED_KEY in _cfg(row):
            if str(row.id) not in live_refs:
                await db.delete(row)
                report.purged += 1
            continue
        by_name[row.name].append(row)

    plan: list[_Tool] = []
    for name in sorted(by_name):
        group = by_name[name]
        candidates = [
            r for r in group if r.department_id is not None or r.credential_id is not None
        ]
        if not candidates:
            continue
        integration = next(
            (i for i in (integration_for_connection(r) for r in group) if i is not None), None
        )
        if integration is None or integration.kind != "credential":
            # A sign-in tool keeps its department's app-only row; a custom
            # server its department.
            report.skipped += len(candidates)
            continue
        fresh: list[m.McpConnection] = []
        for row in candidates:
            if str(row.id) in done:
                report.skipped += 1
                continue
            if row.credential_id is not None:
                cred = await db.get(m.Credential, row.credential_id)
                if cred is None or cred.credential_type != integration.credential_type:
                    # No usable login behind the pin: left as it is, so no
                    # agent pinning it loses that pin without a replacement.
                    report.skipped += 1
                    continue
            fresh.append(row)
        if fresh:
            plan.append(_Tool(name, integration, group, fresh))

    if not plan:
        # Nothing folded: nothing the digests cover changed, so nothing is
        # stamped again.
        await db.flush()
        if report.purged:
            await _audit_report(db, tenant_id, report)
        return

    agents = await _load_agents(db)
    departments = {d.id: d for d in (await db.execute(select(m.Department))).scalars()}
    folded_departments = {
        r.department_id for tool in plan for r in tool.fresh if r.department_id is not None
    }
    stamps = _Stamps()
    await stamps.collect(db, agents.by_id, folded_departments)
    work = _Pass(db, tenant_id, report, live_refs)

    for tool in plan:
        report.tools.append(tool.name)
        department_rows = [r for r in tool.fresh if r.credential_id is None]
        login_rows = [r for r in tool.fresh if r.credential_id is not None]
        # Folded first: a department's own copied login (complete or not)
        # is what it used, never the organisation's single one.
        only = await work.single_login_in_use(tool.integration)
        own = await work.fold_department_rows(tool.integration, department_rows, departments)
        await work.keep_single_login(tool.integration, only, departments, login_rows, own)
        await work.bind_login_rows(tool.integration, login_rows, agents, departments)
        shared = [r for r in tool.group if r.department_id is None and r.credential_id is None]
        await work.collapse(tool.integration, [*shared, *tool.fresh], agents, departments)

    report.restamped = await stamps.restamp(db, tenant_id, work.new_access)
    if report.changed:
        await _audit_report(db, tenant_id, report)


async def _audit_report(
    db: AsyncSession, tenant_id: uuid.UUID, report: ConsolidationReport
) -> None:
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id=None,
        category="connection",
        action="connections.consolidated",
        resource=report.as_dict(),
    )


async def consolidate_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, *, dry_run: bool = False
) -> ConsolidationReport:
    """Consolidate one tenant's tool setups (module docstring). `db` is bound
    to the tenant; flushes, never commits. `dry_run`: the same pass inside a
    savepoint that is rolled back -- the report says what would change."""
    report = ConsolidationReport(tenant_id=tenant_id, dry_run=dry_run)
    if not dry_run:
        await _consolidate(db, tenant_id, report)
        return report
    savepoint = await db.begin_nested()
    try:
        await _consolidate(db, tenant_id, report)
    finally:
        await savepoint.rollback()
        db.expire_all()
    return report


async def consolidate_all_tenants(*, dry_run: bool = False) -> list[ConsolidationReport]:
    """`consolidate_tenant` for every tenant, each in its own tenant session.
    A tenant that fails is logged (ids only) and skipped; never raises."""
    from oc8.db.session import tenant_session
    from oc8.triggers.scheduler import list_active_tenant_ids

    reports: list[ConsolidationReport] = []
    try:
        tenant_ids = await list_active_tenant_ids()
    except Exception:
        logger.exception("connection consolidation: listing tenants failed")
        return reports
    for tenant_id in tenant_ids:
        try:
            async with tenant_session(tenant_id) as db:
                report = await consolidate_tenant(db, tenant_id, dry_run=dry_run)
        except Exception as exc:
            logger.error(
                "connection consolidation failed for tenant %s (%s)",
                tenant_id,
                type(exc).__name__,
            )
            continue
        reports.append(report)
        if report.changed or report.restamped:
            logger.info("connection consolidation: %s", report.as_dict())
    return reports
