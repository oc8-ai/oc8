"""Turn a tenant's per-department tool setups into shared logins (unified
connections design, "Migration").

Before the one-login model every department that set up a login tool got its
own `McpConnection` with the login's values copied into its env, and every
`POST /mcp/logins` an extra row pinned to one credential. `consolidate_tenant`
folds them, per tool, into the model the resolver now reads:

1. Department rows that carry copied login values: grouped by equal values
   (plain values and secret refs); each group reuses an equal organisation
   login or becomes a new one named "<Integration> — <first department>",
   whose `secret_refs` point at the secrets already stored (no re-encryption),
   and is enabled for the group's departments. Incomplete values are kept --
   the login then shows as incomplete instead of failing silently.
2. Rows pinned to a login become an agent's fixed access (`AgentToolAccess`)
   for every agent pinning them, written as a system change.
3. The oldest row stays as the tool's one row (no department, no login, no
   copied values); narrowing pins and department defaults that named another
   row now name it, and the other rows are deleted.
4. A row some login's `consolidated_from` already names is left alone, so a
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
from oc8.connections.access import binding_fingerprint, trigger_fingerprint
from oc8.connections.access_write import write_tool_access
from oc8.connections.credential_env import credential_env_targets
from oc8.connections.integrations import Integration, integration_for_connection
from oc8.credentials.enablement import enable_for_department, enabled_credentials
from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type
from oc8.secrets.service import store_secret

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
    tools: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.created or self.enabled or self.collapsed or self.pins_rewritten or self.fixed
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
    taken = set(
        (
            await db.execute(
                select(m.Credential.name).where(
                    m.Credential.owner_member_id.is_(None), m.Credential.name.like(f"{wanted}%")
                )
            )
        ).scalars()
    )
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted} ({n})" in taken:
        n += 1
    return f"{wanted} ({n})"


class _Stamps:
    """Confirmations valid before the pass, stamped again after it."""

    def __init__(self) -> None:
        self.triggers: list[m.Trigger] = []
        self.bindings: list[m.AgentToolAccess] = []

    async def collect(self, db: AsyncSession) -> None:
        agents = {a.id: a for a in (await db.execute(select(m.Agent))).scalars()}
        for trigger in (
            await db.execute(select(m.Trigger).where(m.Trigger.access_fingerprint.is_not(None)))
        ).scalars():
            agent = agents.get(trigger.agent_id)
            if agent is None:
                continue
            accepted = {
                await trigger_fingerprint(db, trigger),
                # Stamped before consolidated tools were part of the digest.
                await binding_fingerprint(
                    db, agent, None, version_id=agent.current_version_id, legacy=True
                ),
            }
            if trigger.access_fingerprint in accepted:
                self.triggers.append(trigger)
        for row in (
            await db.execute(
                select(m.AgentToolAccess).where(m.AgentToolAccess.binding_fingerprint.is_not(None))
            )
        ).scalars():
            agent = agents.get(row.agent_id)
            if agent is None:
                continue
            accepted = {
                await binding_fingerprint(
                    db, agent, row.tool_key, version_id=agent.current_version_id, legacy=legacy
                )
                for legacy in (False, True)
            }
            if row.binding_fingerprint in accepted:
                self.bindings.append(row)

    async def restamp(self, db: AsyncSession, extra: list[m.AgentToolAccess]) -> int:
        count = 0
        for trigger in self.triggers:
            current = await trigger_fingerprint(db, trigger)
            if current is not None and current != trigger.access_fingerprint:
                trigger.access_fingerprint = current
                count += 1
        for row in [*self.bindings, *extra]:
            agent = await db.get(m.Agent, row.agent_id)
            if agent is None:
                continue
            current = await binding_fingerprint(
                db, agent, row.tool_key, version_id=agent.current_version_id
            )
            if current != row.binding_fingerprint:
                row.binding_fingerprint = current
                count += 1
        await db.flush()
        return count


class _Pass:
    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID, report: ConsolidationReport):
        self.db = db
        self.tenant_id = tenant_id
        self.report = report
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

    async def fold_department_rows(
        self,
        integration: Integration,
        rows: list[m.McpConnection],
        departments: dict[uuid.UUID, m.Department],
    ) -> None:
        """Step 1."""
        cred_type = await self.cred_type(integration)
        groups: dict[_Copied, list[m.McpConnection]] = defaultdict(list)
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
            cred = next(
                (
                    c
                    for c in org
                    if not copied.plain_secrets
                    and dict(c.field_values or {}) == dict(copied.plain)
                    and dict(c.secret_refs or {}) == dict(copied.refs)
                ),
                None,
            )
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
                    secret_refs=dict(copied.refs),
                    consolidated_from=[],
                )
                self.db.add(cred)
                await self.db.flush()
                if copied.plain_secrets:
                    refs = dict(cred.secret_refs)
                    for key, value in copied.plain_secrets:
                        ref = f"credential/{cred.id}/{key}"
                        await store_secret(
                            self.db,
                            tenant_id=self.tenant_id,
                            name=ref,
                            value=value,
                            kind="credential",
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
        await self.db.flush()

    async def bind_login_rows(
        self,
        integration: Integration,
        rows: list[m.McpConnection],
        agents: list[m.Agent],
        current_pins: dict[uuid.UUID, dict[str, str]],
        departments: dict[uuid.UUID, m.Department],
    ) -> None:
        """Step 2."""
        tool_key = integration.tool_key
        for row in rows:
            assert row.credential_id is not None
            cred = await self.db.get(m.Credential, row.credential_id)
            if cred is None or cred.credential_type != integration.credential_type:
                continue
            row_id = str(row.id)
            pinned_by: list[m.Agent] = []
            for agent in agents:
                own = current_pins.get(agent.id, {})
                if own.get(tool_key) == row_id:
                    pinned_by.append(agent)
                    continue
                if own.get(tool_key) or agent.department_id is None:
                    continue
                dept = departments.get(agent.department_id)
                default = _frame_default(dept, tool_key)
                if default == row_id and _enables(agent, tool_key):
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
        agents: list[m.Agent],
        departments: dict[uuid.UUID, m.Department],
    ) -> None:
        """Step 3."""
        shared = [r for r in rows if r.department_id is None and r.credential_id is None]
        keep = min(shared or rows, key=lambda r: (r.created_at, r.id))
        cred_type = await self.cred_type(integration)
        keep.department_id = None
        keep.credential_id = None
        keep.config = _stripped_config(keep, cred_type, integration)
        keep.connected = any(r.connected for r in rows)
        moved = {str(r.id): str(keep.id) for r in rows if r.id != keep.id}
        for row in rows:
            if row.id != keep.id:
                await self.db.delete(row)
                self.report.collapsed += 1
        await self.db.flush()
        if not moved:
            return
        for agent in agents:
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
        await self.db.flush()


def _frame_default(dept: m.Department | None, tool_key: str) -> str | None:
    frame = dept.frame if dept is not None and isinstance(dept.frame, dict) else {}
    tools = frame.get("tools") if isinstance(frame.get("tools"), dict) else {}
    entry = (tools or {}).get(tool_key)
    value = entry.get("default_connection_id") if isinstance(entry, dict) else None
    return str(value) if value else None


def _enables(agent: m.Agent, tool_key: str) -> bool:
    tools = (agent.narrowing or {}).get("tools") if isinstance(agent.narrowing, dict) else None
    policy = tools.get(tool_key) if isinstance(tools, dict) else None
    return isinstance(policy, dict) and bool(policy.get("enabled"))


async def _current_pins(db: AsyncSession, agents: list[m.Agent]) -> dict[uuid.UUID, dict[str, str]]:
    """Each agent's pins: its working copy's, and its published version's
    where the working copy has none for a key (the run uses the version)."""
    out: dict[uuid.UUID, dict[str, str]] = {}
    for agent in agents:
        pins = _pins(agent.narrowing)
        if agent.current_version_id is not None:
            version = await db.get(m.AgentVersion, agent.current_version_id)
            if version is not None and isinstance(version.payload, dict):
                for key, value in _pins(version.payload.get("narrowing")).items():
                    pins.setdefault(key, value)
        out[agent.id] = pins
    return out


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

    stamps = _Stamps()
    await stamps.collect(db)

    rows = list(
        (
            await db.execute(
                select(m.McpConnection)
                .where(m.McpConnection.tenant_id == tenant_id)
                .order_by(m.McpConnection.created_at, m.McpConnection.id)
            )
        ).scalars()
    )
    by_name: dict[str, list[m.McpConnection]] = defaultdict(list)
    for row in rows:
        by_name[row.name].append(row)

    agents = list((await db.execute(select(m.Agent))).scalars())
    departments = {d.id: d for d in (await db.execute(select(m.Department))).scalars()}
    current_pins = await _current_pins(db, agents)
    work = _Pass(db, tenant_id, report)

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
        fresh = [r for r in candidates if str(r.id) not in done]
        report.skipped += len(candidates) - len(fresh)
        if not fresh:
            continue
        report.tools.append(name)
        department_rows = [r for r in fresh if r.credential_id is None]
        login_rows = [r for r in fresh if r.credential_id is not None]
        await work.fold_department_rows(integration, department_rows, departments)
        await work.bind_login_rows(integration, login_rows, agents, current_pins, departments)
        shared = [r for r in group if r.department_id is None and r.credential_id is None]
        await work.collapse(integration, [*shared, *fresh], agents, departments)

    report.restamped = await stamps.restamp(db, work.new_access)
    if report.changed:
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
