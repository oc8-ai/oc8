"""Which account a run's tool call acts through (connections design).

`legacy`: the McpConnection's own configuration, unchanged (a department's
app-only setup, a pinned login, an unconsolidated department row, a tool that
is no connectable integration). `oauth` / `credential`: one OAuth connection
or credential, chosen in one order for agents and the Copilot
(`connections.login.resolve_login`). Anything else raises ConnectionRequired:
there is no fallback to another person's account.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import snapshot_agent
from oc8.connections.acting import ACTING_PERSON_WITHHELD, ActingPerson, resolve_acting_person
from oc8.connections.integrations import (
    Integration,
    integration_enabled,
    integration_for_connection,
)

if TYPE_CHECKING:
    from oc8.connections.login import LoginChoice


class ConnectionRequired(Exception):
    """The call needs an account that is not available. `str()` is for the
    model; `as_context()` is what the run records for the UI."""

    def __init__(
        self, integration: Integration, reason: str, *, missing: Sequence[str] = ()
    ) -> None:
        self.integration = integration
        self.reason = reason
        #: `incomplete`: the required field keys the login leaves empty.
        self.missing = list(missing)
        super().__init__(self._message())

    def _message(self) -> str:
        what = (
            f"the person's {self.integration.label} account"
            if self.integration.kind == "oauth"
            else f"a {self.integration.label} login"
        )
        if self.reason == "schedule_without_owner":
            return (
                f"connection_required: this schedule has no owner, "
                f"so it cannot act through {what}. "
                "It must be saved once again by the person whose account it should use."
            )
        if self.reason == "schedule_needs_confirmation":
            return (
                f"connection_required: this agent changed since the schedule's creator last "
                f"saved it, so it cannot act through {what}. "
                "The creator must open and save the schedule again."
            )
        if self.reason == "trigger_without_person":
            return (
                f"connection_required: this trigger may act through {what} of its creator "
                "only if the creator allows it in the trigger's settings."
            )
        if self.reason == "no_acting_person":
            return (
                f"connection_required: nobody is working with this run, "
                f"so {what} cannot be chosen. "
                "An administrator can give this agent a fixed access."
            )
        if self.reason == "personal_account_withheld":
            return (
                "connection_required: this run acts for nobody because the run it came from "
                "withheld its person (for example a decision with someone else's instruction), "
                f"so it may not use the personal {self.integration.label} account of this "
                "agent's fixed access. Only an organisation account can be used here."
            )
        if self.reason == "needs_reauth":
            return (
                f"connection_required: {what} must be connected again. "
                "Tell the user to reconnect it under Connections."
            )
        if self.reason == "binding_needs_confirmation":
            return (
                f"connection_required: this agent's access to {self.integration.label} uses a "
                "person's account and was not confirmed since the agent last changed. "
                "The account's owner or an administrator must save the access setting again."
            )
        if self.reason == "incomplete":
            return (
                f"connection_required: The {self.integration.label} login is incomplete: "
                f"{', '.join(self.missing)} missing. "
                "Ask an admin to complete it under Settings → Connections."
            )
        if self.reason == "read_only":
            return (
                f"connection_required: the {self.integration.label} login this conversation "
                "uses was chosen for reading; no department of the person grants writing "
                "through it."
            )
        if self.reason == "ambiguous":
            return (
                f"connection_required: Several {self.integration.label} logins are enabled "
                "for this department and the agent has none chosen."
            )
        if self.reason == "not_configured":
            return (
                f"connection_required: no access to {self.integration.label} "
                "is set up for this agent."
            )
        return (
            f"connection_required: this task needs {what}, which is not connected. "
            "Tell the user to connect it under Connections."
        )

    def as_context(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "integration": self.integration.name,
            "provider": self.integration.provider,
            "label": self.integration.label,
            "reason": self.reason,
        }
        if self.reason == "incomplete":
            out["missing"] = list(self.missing)
        return out


@dataclass(frozen=True)
class ToolAccess:
    kind: str  # "legacy" | "oauth" | "credential"
    integration: Integration | None
    on_behalf_of: uuid.UUID | None
    oauth_connection: m.OAuthConnection | None = None
    credential: m.Credential | None = None
    #: The right the account was resolved for. "read": a Copilot login lent by
    #: a department granting only read -- no call needing more may use it. An
    #: agent's account is not chosen per right ("modify").
    right: Literal["read", "modify"] = "modify"


async def single_organisation_credential(
    db: AsyncSession, credential_type: str
) -> m.Credential | None:
    rows = (
        (
            await db.execute(
                select(m.Credential)
                .where(
                    m.Credential.credential_type == credential_type,
                    m.Credential.owner_member_id.is_(None),
                )
                .limit(2)
            )
        )
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


async def resolve_tool_access(
    db: AsyncSession,
    *,
    run: m.AgentRun,
    agent: m.Agent,
    conn: m.McpConnection,
    tool: str | None = None,
    right: Literal["read", "modify"] | None = None,
) -> ToolAccess:
    """`tool`: the call this access serves -- a Copilot direct-access write
    needs a department granting `modify`. `"*"`: a bridge serving every call
    of the run (`whole_bridge_access`). None: a listing or an env lookup
    (`read`). `right` overrides what `tool` implies (a core tool reading
    through the connection)."""
    # Deferred: direct_access -> usable -> access would be a cycle at import;
    # login imports this module.
    from oc8.connections.direct_access import active_grant, direct_access_run

    direct = direct_access_run(run, agent)
    grant = (
        await active_grant(db, session_id=direct.session_id, tool_key=conn.name)
        if direct is not None
        else None
    )
    # The grant must be this person's own (fail closed): a grant row of another
    # member never lends its account.
    if direct is not None and grant is not None and grant.member_id == direct.member_id:
        if right is None and tool == "*":
            return await whole_bridge_access(db, member_id=direct.member_id, conn=conn)
        return await resolve_direct_access(
            db, member_id=direct.member_id, conn=conn, right=right or call_right(conn, tool)
        )
    acting, integration, choice = await agent_login(db, run=run, agent=agent, conn=conn)
    if integration is None or choice is None:
        return ToolAccess("legacy", None, acting.member_id)
    return _to_access(choice, integration, acting.member_id)


async def agent_login(
    db: AsyncSession, *, run: m.AgentRun, agent: m.Agent, conn: m.McpConnection
) -> tuple[ActingPerson, Integration | None, LoginChoice | None]:
    """The login an agent's run uses through `conn` (no Copilot direct
    access): who acts, the integration (None: not a connectable one, the
    row's own config applies) and the resolver's choice. Raises
    `ConnectionRequired` as the run would. The one path for the runtime and
    for showing which login a tool uses."""
    from oc8.connections.login import live_chat_member, resolve_login

    acting = await resolve_acting_person(db, run)
    integration = integration_for_connection(conn)
    if integration is None:
        return acting, None, None
    if not await integration_enabled(db, integration):
        # Disabling keeps the plugin's rows (there is no uninstall): its tools
        # act through nobody's account until it is enabled again.
        raise ConnectionRequired(integration, "not_configured")
    choice = await resolve_login(
        db,
        agent=agent,
        integration=integration,
        conn=conn,
        live_member_id=live_chat_member(run, acting),
        department_ids=[agent.department_id],
        right="read",
        run=run,
        acting=acting,
    )
    return acting, integration, choice


def declared_scopes(conn: m.McpConnection) -> dict[str, Any] | None:
    """The connection's read/modify classification: its manifest's, else a
    dict stored on the row."""
    from oc8.capas.discovery import resolve_tool_pack_connection

    cfg = conn.config if isinstance(conn.config, dict) else {}
    declared = resolve_tool_pack_connection(
        str(cfg.get("_plugin_name", "")), str(cfg.get("_connection_key", ""))
    )
    if declared is not None and isinstance(declared.scopes, dict):
        return declared.scopes
    return conn.scopes if isinstance(conn.scopes, dict) else None


def call_right(conn: m.McpConnection, tool: str | None) -> Literal["read", "modify"]:
    """The right a call of `tool` needs (the connection's declared scopes;
    unknown: modify). No tool (a listing, an env lookup): read."""
    from oc8.authz.pdp import required_right

    if tool is None or tool == "*":
        return "read"
    return "modify" if required_right(tool, declared_scopes(conn)) == "modify" else "read"


def can_write(conn: m.McpConnection) -> bool:
    """Whether the connection declares a tool needing more than read (no
    classification at all: every tool counts as modify)."""
    scopes = declared_scopes(conn)
    if not scopes:
        return True
    return any(right != "read" and names for right, names in scopes.items())


async def whole_bridge_access(
    db: AsyncSession, *, member_id: uuid.UUID, conn: m.McpConnection
) -> ToolAccess:
    """A Copilot bridge serving every call of the run: resolved for `modify`
    when the tool can write, else -- or when no department of the person
    grants modify -- for `read`. The access records the right; a call needing
    more is refused on that bridge."""
    if can_write(conn):
        try:
            return await resolve_direct_access(db, member_id=member_id, conn=conn, right="modify")
        except (ConnectionRequired, PermissionError):
            pass
    return await resolve_direct_access(db, member_id=member_id, conn=conn, right="read")


def _to_access(
    choice: LoginChoice,
    integration: Integration,
    on_behalf_of: uuid.UUID | None,
    right: Literal["read", "modify"] = "modify",
) -> ToolAccess:
    if choice.oauth_connection is not None:
        return ToolAccess(
            "oauth",
            integration,
            on_behalf_of,
            oauth_connection=choice.oauth_connection,
            right=right,
        )
    if choice.credential is not None:
        return ToolAccess(
            "credential", integration, on_behalf_of, credential=choice.credential, right=right
        )
    return ToolAccess("legacy", integration, on_behalf_of, right=right)


async def _require_schedule_confirmed(
    db: AsyncSession, run: m.AgentRun, integration: Integration, owner: uuid.UUID | None
) -> None:
    """A person's own account (`owner` set), in a run a trigger started, only
    for the agent the trigger's creator last saved. An organisation's account
    is not theirs to confirm."""
    if owner is not None and not await schedule_confirmed(db, run):
        raise ConnectionRequired(integration, "schedule_needs_confirmation")


async def department_targets(db: AsyncSession, agent: m.Agent, tool_key: str) -> list[uuid.UUID]:
    """The accounts the agent's own department already set up for this tool:
    the organisation logins enabled for it, and -- while unconsolidated --
    the OAuth connection or pinned login of a row it still owns."""
    from oc8.connections.integrations import enabled_tool_pack_connection
    from oc8.credentials.enablement import enabled_credentials

    out: list[uuid.UUID] = []
    if agent.department_id is None:
        return out
    owned = (
        await db.execute(
            select(m.McpConnection).where(
                m.McpConnection.name == tool_key,
                m.McpConnection.department_id == agent.department_id,
            )
        )
    ).scalars()
    integration: Integration | None = None
    for conn in owned:
        integration = integration or integration_for_connection(conn)
        cfg = conn.config if isinstance(conn.config, dict) else {}
        for raw in (cfg.get("oauth_connection_id"), conn.credential_id):
            if raw is None:
                continue
            try:
                out.append(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))
            except ValueError:
                continue
    if integration is None:
        template = await enabled_tool_pack_connection(db, tool_key)
        integration = template[2] if template is not None else None
    if integration is not None and integration.credential_type:
        out.extend(
            cred.id
            for cred in await enabled_credentials(
                db, credential_type=integration.credential_type, department_id=agent.department_id
            )
        )
    return out


async def binding_fingerprint(
    db: AsyncSession,
    agent: m.Agent,
    tool_key: str | None,
    *,
    version_id: uuid.UUID | None,
    legacy: bool = False,
) -> str:
    """What the person who confirmed a binding to their own account saw: the
    agent version (or, for an agent without one, its working copy), the
    skills the agent (or its department or the whole organisation) gets, the
    department's frame and the department's setup of this tool. Anything else
    (status, routing hints) does not change it. The setup is hashed whole: the
    result is a digest, and the config holds references, not secret values.
    `tool_key` None: the department's setup of every tool (a trigger's runs
    may use any of them).

    A tool the department owns no row of any more (consolidated: one shared
    row, logins enabled for departments) is hashed through what the agent
    reaches instead (`_shared_setup`): the shared row, the agent's access
    row and the logins the resolver could pick. Otherwise a change to the
    shared row or to the login used would leave a confirmation standing.
    `legacy`: the digest without that part, as stored before (consolidation
    accepts a confirmation stamped that way once, `connections.consolidate`)."""
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
    skills = sorted(
        [
            str(a.id),
            str(a.skill_version_id),
            bool(a.enabled),
            a.overrides,
            a.updated_at.isoformat() if a.updated_at else None,
        ]
        for a in assignments
    )
    dept = await db.get(m.Department, agent.department_id)
    # The rows the department owns, as before the one-row-per-tool change:
    # a stored confirmation (a trigger's, a binding's) must stay valid for
    # a tenant whose setups did not change. Consolidation re-stamps them.
    which = select(m.McpConnection).where(m.McpConnection.department_id == agent.department_id)
    if tool_key is not None:
        which = which.where(m.McpConnection.name == tool_key)
    conns = list((await db.execute(which)).scalars())
    setup = sorted(
        [str(c.id), str(c.credential_id), c.server_url, c.transport, c.scopes, c.config]
        for c in conns
    )
    payload: dict[str, Any] = {
        "version": str(version_id) if version_id else await snapshot_agent(db, agent),
        "skills": skills,
        "department": None if dept is None else [dept.goal, dept.frame, dept.frame_capa_defaults],
        "setup": setup,
    }
    if not legacy:
        # Only present when non-empty: an unconsolidated department's digest
        # stays exactly what it was.
        shared = await _shared_setup(db, agent, dept, tool_key, owned={c.name for c in conns})
        if shared:
            payload["shared"] = shared
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _frame_tools(dept: m.Department | None) -> list[str]:
    """The tool names the department's frame enables."""
    frame = dept.frame if dept is not None and isinstance(dept.frame, dict) else {}
    tools = frame.get("tools") if isinstance(frame.get("tools"), dict) else {}
    return sorted(
        key
        for key, policy in (tools or {}).items()
        if isinstance(policy, dict) and policy.get("enabled")
    )


async def _shared_setup(
    db: AsyncSession,
    agent: m.Agent,
    dept: m.Department | None,
    tool_key: str | None,
    *,
    owned: set[str],
) -> list[Any]:
    """What a consolidated tool's run uses, for each login tool the agent can
    reach (`tool_key`, or every tool its department's frame enables) that its
    department owns no row of: the tool's row (the shared one the run starts
    on -- or, before a first run created it, the manifest's, which is what
    that run creates) and the login the resolver uses (the agent's access
    row; else the organisation logins enabled for the department, else the
    organisation's single one). Definitions and ids only -- references,
    never secret values."""
    from oc8.capas.materialise import unbound_connection
    from oc8.connections.integrations import enabled_tool_pack_connection
    from oc8.connections.login import enabled_anywhere
    from oc8.connections.tool_connection import not_retired
    from oc8.credentials.enablement import enabled_credentials

    names = [tool_key] if tool_key is not None else _frame_tools(dept)
    out: list[Any] = []
    for name in names:
        if name in owned:
            continue
        row = await db.scalar(
            select(m.McpConnection)
            .where(
                m.McpConnection.name == name,
                m.McpConnection.department_id.is_(None),
                m.McpConnection.credential_id.is_(None),
                not_retired(),
            )
            .order_by(m.McpConnection.created_at, m.McpConnection.id)
            .limit(1)
        )
        integration = integration_for_connection(row) if row is not None else None
        if row is None or integration is None:
            template = await enabled_tool_pack_connection(db, name)
            if template is not None:
                manifest, pack_conn, integration = template
                if row is None:
                    row = unbound_connection(
                        tenant_id=agent.tenant_id, manifest=manifest, conn=pack_conn
                    )
        if integration is None or integration.kind != "credential":
            # Only login tools: a sign-in tool's account is the person's own
            # or its department's app-only row, hashed as before.
            continue
        assert integration.credential_type is not None
        access = await db.scalar(
            select(m.AgentToolAccess).where(
                m.AgentToolAccess.agent_id == agent.id, m.AgentToolAccess.tool_key == name
            )
        )
        used: list[Any]
        if access is not None:
            used = [access.mode, str(access.credential_id), str(access.oauth_connection_id)]
        else:
            used = []
            if agent.department_id is not None:
                used = [
                    str(c.id)
                    for c in await enabled_credentials(
                        db,
                        credential_type=integration.credential_type,
                        department_id=agent.department_id,
                    )
                ]
            if not used and not await enabled_anywhere(db, integration.credential_type):
                only = await single_organisation_credential(db, integration.credential_type)
                if only is not None:
                    used = [str(only.id)]
        definition = (
            None if row is None else [row.server_url, row.transport, row.scopes, row.config]
        )
        if definition is not None or used:
            out.append([name, definition, used])
    return out


async def trigger_fingerprint(
    db: AsyncSession, trigger: m.Trigger, *, version_id: uuid.UUID | None = None
) -> str | None:
    """What a trigger's creator saw of its agent when they last saved it (the
    binding fingerprint over every tool). `version_id`: the version a run
    executes; default the agent's published one. None: the agent is gone."""
    agent = await db.get(m.Agent, trigger.agent_id)
    if agent is None or agent.tenant_id != trigger.tenant_id:
        return None
    return await binding_fingerprint(
        db, agent, None, version_id=version_id or agent.current_version_id
    )


def _trigger_ids(run: m.AgentRun) -> list[object]:
    """The triggers a run descends from: its own (a schedule's run, a
    delegated sub-run or wake-up carrying it) and a Copilot follow-up's."""
    ctx = run.context if isinstance(run.context, dict) else {}
    raws: list[object] = [ctx.get("trigger_id")]
    followup = ctx.get("followup")
    if isinstance(followup, dict):
        raws.append(followup.get("trigger_id"))
    return [raw for raw in raws if raw is not None]


async def schedule_confirmed(db: AsyncSession, run: m.AgentRun) -> bool:
    """A run a trigger started may use a person's own account only when it is
    the trigger's own agent and that agent is what its creator last saved
    (fingerprint stored on the trigger). A trigger saved before fingerprints
    existed (NULL) never is; a delegated sub-run of another agent never is."""
    for raw in _trigger_ids(run):
        try:
            trigger = await db.get(m.Trigger, uuid.UUID(str(raw)))
        except ValueError:
            return False
        if trigger is None or trigger.tenant_id != run.tenant_id:
            return False
        if trigger.access_fingerprint is None:
            return False
        if run.agent_id != trigger.agent_id:
            # A delegate (or a decision follow-up's agent) is not what the
            # creator saw: their own account stays with the trigger's agent.
            return False
        current = await trigger_fingerprint(db, trigger, version_id=run.agent_version_id)
        if current != trigger.access_fingerprint:
            return False
    return True


async def _trigger_owner_ok(db: AsyncSession, run: m.AgentRun, owner: uuid.UUID) -> bool:
    """A run a trigger started (a schedule, a webhook, an event, a Copilot
    follow-up) acts through a person's account only if that person created
    the trigger. A webhook or event trigger, which anyone who reaches it can
    fire, additionally needs its creator's explicit `acts_for_creator`."""
    ctx = run.context if isinstance(run.context, dict) else {}
    followup = ctx.get("followup")
    raws = [ctx.get("trigger_id")]
    if isinstance(followup, dict):
        raws.append(followup.get("trigger_id"))
    for raw in raws:
        if raw is None:
            continue
        try:
            trigger = await db.get(m.Trigger, uuid.UUID(str(raw)))
        except ValueError:
            return False
        if trigger is None or trigger.created_by_member_id != owner:
            return False
        if trigger.kind in ("webhook", "event") and not trigger.acts_for_creator:
            return False
    return True


async def unconfirmed_reason(
    db: AsyncSession,
    row: m.AgentToolAccess,
    agent: m.Agent,
    tool_key: str,
    target_id: uuid.UUID,
    owner: uuid.UUID | None,
    *,
    version_id: uuid.UUID | None,
) -> str | None:
    """Why a fixed binding is not (or no longer) confirmed, or None when it is.
    `not_confirmed`: neither an administrator nor (for a person's account) its
    owner chose it. `agent_changed`: a person's account, and the agent changed
    since it was confirmed. A trigger's own conditions are per run, not here."""
    if owner is None:
        # An organisation account: an administrator chose it, or it still is
        # the department's own setup.
        if row.set_with_manage or target_id in await department_targets(db, agent, tool_key):
            return None
        return "not_confirmed"
    # A person's own account: that person (or an administrator) chose it and
    # the agent is unchanged since.
    if not (row.set_with_manage or row.set_by_member_id == owner):
        return "not_confirmed"
    current = await binding_fingerprint(db, agent, tool_key, version_id=version_id)
    if row.binding_fingerprint is None or row.binding_fingerprint != current:
        return "agent_changed"
    return None


async def _consented(
    db: AsyncSession,
    row: m.AgentToolAccess,
    agent: m.Agent,
    run: m.AgentRun,
    tool_key: str,
    target_id: uuid.UUID,
    owner: uuid.UUID | None,
) -> bool:
    reason = await unconfirmed_reason(
        db,
        row,
        agent,
        tool_key,
        target_id,
        owner,
        version_id=run.agent_version_id or agent.current_version_id,
    )
    if reason is not None:
        return False
    # A schedule or trigger acts through a person's account only if it is theirs.
    return owner is None or await _trigger_owner_ok(db, run, owner)


def _refuse_withheld_personal(
    run: m.AgentRun, integration: Integration, owner: uuid.UUID | None
) -> None:
    """A run whose person was withheld (an approver's own words, and every
    delegation or wake-up carried from it) acts for nobody on purpose: a fixed
    binding must not hand it a member's own account through the back door.
    An organisation account stays usable. A run that acts for nobody for
    another reason (a schedule, a webhook) keeps the binding's consent rules."""
    ctx = run.context if isinstance(run.context, dict) else {}
    if owner is not None and ctx.get(ACTING_PERSON_WITHHELD):
        raise ConnectionRequired(integration, "personal_account_withheld")


async def _fixed(
    db: AsyncSession,
    row: m.AgentToolAccess,
    agent: m.Agent,
    run: m.AgentRun,
    tool_key: str,
    integration: Integration,
    acting: ActingPerson,
) -> ToolAccess:
    if integration.kind == "oauth" and row.oauth_connection_id is not None:
        target = await db.get(m.OAuthConnection, row.oauth_connection_id)
        if target is not None and target.provider == integration.provider:
            _refuse_withheld_personal(run, integration, target.owner_member_id)
            if target.status != "active":
                raise ConnectionRequired(integration, "needs_reauth")
            if not await _consented(
                db, row, agent, run, tool_key, target.id, target.owner_member_id
            ):
                raise ConnectionRequired(integration, "binding_needs_confirmation")
            await _require_schedule_confirmed(db, run, integration, target.owner_member_id)
            return ToolAccess("oauth", integration, acting.member_id, oauth_connection=target)
    if integration.kind == "credential" and row.credential_id is not None:
        cred = await db.get(m.Credential, row.credential_id)
        if cred is not None and cred.credential_type == integration.credential_type:
            _refuse_withheld_personal(run, integration, cred.owner_member_id)
            if not await _consented(db, row, agent, run, tool_key, cred.id, cred.owner_member_id):
                raise ConnectionRequired(integration, "binding_needs_confirmation")
            await _require_schedule_confirmed(db, run, integration, cred.owner_member_id)
            return ToolAccess("credential", integration, acting.member_id, credential=cred)
    # The target is gone (ON DELETE SET NULL) or never matched: refuse, never re-choose.
    raise ConnectionRequired(integration, "not_connected")


def department_setup(conn: m.McpConnection, agent: m.Agent, integration: Integration) -> bool:
    """Whether `conn` is the agent's own department's setup of the integration
    (a login, or an app-only OAuth app) -- the default access when the agent
    has no access row."""
    cfg = conn.config if isinstance(conn.config, dict) else {}
    return (
        conn.department_id is not None
        and conn.department_id == agent.department_id
        and (integration.kind == "credential" or bool(cfg.get("oauth_connection_id")))
    )


async def _user(db: AsyncSession, integration: Integration, acting: ActingPerson) -> ToolAccess:
    if acting.member_id is None:
        raise ConnectionRequired(integration, acting.reason or "no_acting_person")
    if integration.kind == "oauth":
        rows = (
            (
                await db.execute(
                    select(m.OAuthConnection)
                    .where(
                        m.OAuthConnection.provider == integration.provider,
                        m.OAuthConnection.owner_member_id == acting.member_id,
                        m.OAuthConnection.grant_type == "authorization_code",
                    )
                    .order_by(m.OAuthConnection.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        active = next((r for r in rows if r.status == "active"), None)
        if active is None:
            raise ConnectionRequired(integration, "needs_reauth" if rows else "not_connected")
        return ToolAccess("oauth", integration, acting.member_id, oauth_connection=active)
    cred = await db.scalar(
        select(m.Credential)
        .where(
            m.Credential.credential_type == integration.credential_type,
            m.Credential.owner_member_id == acting.member_id,
        )
        .order_by(m.Credential.created_at.desc())
        .limit(1)
    )
    if cred is None:
        raise ConnectionRequired(integration, "not_connected")
    return ToolAccess("credential", integration, acting.member_id, credential=cred)


async def resolve_direct_access(
    db: AsyncSession,
    *,
    member_id: uuid.UUID,
    conn: m.McpConnection,
    right: Literal["read", "modify"] = "read",
) -> ToolAccess:
    """The account a Copilot direct-access call uses (§4, unified connections
    "Resolver order (Copilot direct access)"): the person's own login; else a
    login enabled for a department of theirs whose frame grants `right`
    ("modify" for a write); else the organisation's single login. Another
    member's account, never."""
    from oc8.connections.login import resolve_login
    from oc8.connections.usable import member_departments_for, member_may_use

    integration = integration_for_connection(conn)
    if integration is None:
        # Not a connectable integration: it has one account, the organisation's.
        if await member_may_use(db, member_id=member_id, tool_key=conn.name, right=right):
            return ToolAccess("legacy", None, member_id, right=right)
        raise PermissionError(f"{conn.name!r} is not usable by the person this run acts for")
    if not await integration_enabled(db, integration):
        raise ConnectionRequired(integration, "not_configured")
    # The departments of the person's whose frame grants `right` -- not
    # gated on `member_may_use`, which already asks whether a login resolves:
    # the resolver itself must say why not (`ambiguous`, ...).
    granting = [
        dept
        for dept, policy in await member_departments_for(
            db, member_id=member_id, tool_key=conn.name
        )
        if policy.has_right(right)
    ]
    choice = await resolve_login(
        db,
        agent=None,
        integration=integration,
        conn=conn,
        live_member_id=member_id,
        department_ids=granting,
        right=right,
    )
    return _to_access(choice, integration, member_id, right)


def account_of(access: ToolAccess) -> Literal["own", "organisation"]:
    """Whose account a resolved access is, for the approval card (§8)."""
    owner = (
        access.oauth_connection.owner_member_id
        if access.oauth_connection is not None
        else access.credential.owner_member_id
        if access.credential is not None
        else None
    )
    return "own" if owner is not None and owner == access.on_behalf_of else "organisation"


async def connected_connections(db: AsyncSession) -> list[m.McpConnection]:
    """Every live connection the session's tenant has, whatever department it
    sits in. Row-level security already scopes the session to the tenant."""
    rows = await db.execute(select(m.McpConnection).where(m.McpConnection.connected.is_(True)))
    return list(rows.scalars())
