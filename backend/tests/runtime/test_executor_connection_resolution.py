"""Which McpConnection a run reaches (agent tool login selection design, Task 6).

Three branches, and the third is the one that must not move: an agent pins its
own login; an agent that needs a login but pinned none fails loudly rather than
borrowing someone else's; and an agent whose tools are the pre-existing,
department-scoped OAuth kind resolves exactly as it did before this design
existed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from oc8 import models as m
from oc8.agent.control_tools import _delegate
from oc8.agent.engine import RunResult
from oc8.constants import ACME_TENANT_ID
from oc8.modelrouter.types import ToolCall
from oc8.runtime.executor import _maybe_wake_parent, _resolve_mcp_connection, execute_run
from oc8.runtime.queue import RunMessage
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FnRuntime:
    def __init__(self, fn: Callable[..., Awaitable[RunResult]]) -> None:
        self._fn = fn

    async def execute(self, db: Any, **kw: Any) -> RunResult:
        return await self._fn(db, **kw)


def _login_connection(tenant: uuid.UUID, name: str) -> tuple[m.Credential, m.McpConnection]:
    """A login exactly as `POST /mcp/logins` builds it: a Credential paired with
    a TENANT-GLOBAL connection -- department_id deliberately unset."""
    cred = m.Credential(
        tenant_id=tenant, name=f"{name} login {uuid.uuid4().hex[:8]}", credential_type="odoo_login"
    )
    conn = m.McpConnection(
        tenant_id=tenant,
        name=name,
        server_url="",
        transport="stdio",
        connected=True,
    )
    return cred, conn


def _legacy_connection(
    tenant: uuid.UUID, dept_id: uuid.UUID, name: str, *, age_days: int = 0
) -> m.McpConnection:
    """A pre-existing, department-scoped OAuth connection: no credential_id."""
    return m.McpConnection(
        tenant_id=tenant,
        department_id=dept_id,
        name=name,
        server_url="https://example.invalid",
        transport="http",
        connected=True,
        created_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=age_days),
    )


async def _agent(
    db: Any, tenant: uuid.UUID, *, dept_id: uuid.UUID, narrowing: dict[str, Any] | None = None
) -> m.Agent:
    agent = m.Agent(tenant_id=tenant, department_id=dept_id, name="Nora", narrowing=narrowing or {})
    db.add(agent)
    await db.flush()
    return agent


# --------------------------------------------------------- (1) the agent's pin


async def test_resolves_the_agents_pinned_connection(app_session: AppSessionFactory) -> None:
    """A pinned login wins -- including over a department connection that the
    legacy lookup would otherwise have handed this agent."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        cred, conn = _login_connection(tenant, "Odoo")
        db.add(cred)
        await db.flush()
        conn.credential_id = cred.id
        db.add(conn)
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth", age_days=5))
        await db.flush()
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(conn.id)}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert error is None
        assert resolved is not None
        assert resolved.id == conn.id
        assert resolved.name == "Odoo"


async def test_both_pinned_logins_are_resolved(
    app_session: AppSessionFactory,
) -> None:
    """An agent assigned two logins must reach both. Returning only the first
    key alphabetically left the other system unreachable for the whole run."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        narrowing_tools: dict[str, Any] = {}
        for name in ("Zulip", "Aardvark"):
            cred, conn = _login_connection(tenant, name)
            db.add(cred)
            await db.flush()
            conn.credential_id = cred.id
            db.add(conn)
            await db.flush()
            narrowing_tools[name] = {"enabled": True, "connection_id": str(conn.id)}
        agent = await _agent(db, tenant, dept_id=dept_id, narrowing={"tools": narrowing_tools})

        choice = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert choice.error is None
        assert {c.name for c in choice.connections} == {"Aardvark", "Zulip"}
        # The single-connection slot stays the first key alphabetically so
        # callers that still read `.connection` (control tools, inheritance)
        # keep a stable primary.
        assert choice.connection is not None
        assert choice.connection.name == "Aardvark"
        assert choice.pinned is True


async def test_pinned_connection_that_no_longer_exists_is_a_hard_error(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    gone = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth"))
        await db.flush()
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(gone)}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is not None
        assert "Odoo" in error
        assert str(gone) in error


async def test_an_unreadable_pin_fails_the_run_instead_of_raising(
    app_session: AppSessionFactory,
) -> None:
    """narrowing is operator-supplied JSONB; garbage in it must not escape as a
    ValueError from a section of execute_run that has no exception handling."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(
            db,
            tenant,
            dept_id=uuid.uuid4(),
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": "not-a-uuid"}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is not None
        assert "Odoo" in error


# ------------------------------------- (2) needs a login, has none: hard error


async def test_login_backed_tool_enabled_with_no_pin_is_a_hard_error(
    app_session: AppSessionFactory,
) -> None:
    """No silent fallback: a login-needing tool with no connection_id must never
    resolve to some OTHER connection by accident."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        cred, conn = _login_connection(tenant, "Odoo")
        db.add(cred)
        await db.flush()
        conn.credential_id = cred.id
        db.add(conn)
        # A department connection the legacy path WOULD have handed over.
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth"))
        await db.flush()
        agent = await _agent(
            db, tenant, dept_id=dept_id, narrowing={"tools": {"Odoo": {"enabled": True}}}
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None, "must not borrow the department's connection"
        assert error is not None
        assert "Odoo" in error
        assert "login" in error


async def test_missing_pin_surfaces_as_a_failed_run_with_an_activity_event(
    app_session: AppSessionFactory,
) -> None:
    """The hard error is loud: the run FAILS and the operator sees why."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        cred, conn = _login_connection(tenant, "Odoo")
        db.add(cred)
        await db.flush()
        conn.credential_id = cred.id
        db.add(conn)
        await db.flush()
        agent = await _agent(
            db, tenant, dept_id=dept_id, narrowing={"tools": {"Odoo": {"enabled": True}}}
        )
        run_id = (
            await RunRepository(db).create(
                tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
            )
        ).id
        agent_id = agent.id

    async def unused_runner(db: Any, **kw: Any) -> RunResult:
        raise AssertionError("runner must not be called when no login is pinned")

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(unused_runner),
    )

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.state == RunState.FAILED.value
        assert "Odoo" in run.context["error"]
        reloaded = await db.get(m.Agent, agent_id)
        assert reloaded is not None
        assert reloaded.status == "idle"


async def test_disabled_login_tool_does_not_block_the_legacy_path(
    app_session: AppSessionFactory,
) -> None:
    """A tool the agent has switched OFF asks nothing of the runtime -- it is not
    a missing pin, so the department lookup still applies."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        cred, conn = _login_connection(tenant, "Odoo")
        db.add(cred)
        await db.flush()
        conn.credential_id = cred.id
        db.add(conn)
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth"))
        await db.flush()
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            # Disabled, yet pinned -- neither branch may fire.
            narrowing={"tools": {"Odoo": {"enabled": False, "connection_id": str(conn.id)}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert error is None
        assert resolved is not None
        assert resolved.name == "LegacyOAuth"


# ------------------------------------------- (3) the legacy path, untouched


async def test_legacy_department_lookup_still_takes_the_oldest_connected(
    app_session: AppSessionFactory,
) -> None:
    """Zero behaviour change for an agent whose tools predate this design."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(_legacy_connection(tenant, dept_id, "Newer", age_days=1))
        db.add(_legacy_connection(tenant, dept_id, "Older", age_days=9))
        # Disconnected and other-department rows stay invisible, as before.
        other = _legacy_connection(tenant, uuid.uuid4(), "OtherDept", age_days=30)
        db.add(other)
        stale = _legacy_connection(tenant, dept_id, "Disconnected", age_days=40)
        stale.connected = False
        db.add(stale)
        await db.flush()
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            narrowing={"tools": {"Newer": {"enabled": True, "read": True}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert error is None
        assert resolved is not None
        assert resolved.name == "Older"


async def test_legacy_lookup_never_substitutes_a_credential_backed_connection(
    app_session: AppSessionFactory,
) -> None:
    """Defence in depth: a login is tenant-global today, so the department
    lookup cannot see one -- but should one ever carry a department_id, it must
    still not be handed to an agent that never pinned it."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        cred, conn = _login_connection(tenant, "Odoo")
        db.add(cred)
        await db.flush()
        conn.credential_id = cred.id
        conn.department_id = dept_id
        conn.created_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=99)
        db.add(conn)
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth", age_days=1))
        await db.flush()
        # Nothing enabled at all: the agent asks for no tool by name.
        agent = await _agent(db, tenant, dept_id=dept_id, narrowing={})

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert error is None
        assert resolved is not None
        assert resolved.name == "LegacyOAuth"


async def test_no_connection_anywhere_is_not_an_error(app_session: AppSessionFactory) -> None:
    """An agent with no tools at all runs; it simply reaches nothing."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, dept_id=uuid.uuid4(), narrowing={})

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is None


# ------------------------------------------ run-context override, unchanged


async def test_explicit_run_context_override_still_wins(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        pinned = _legacy_connection(tenant, uuid.uuid4(), "Delegated", age_days=2)
        db.add(pinned)
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth", age_days=8))
        await db.flush()
        cred, conn = _login_connection(tenant, "Odoo")
        db.add(cred)
        await db.flush()
        conn.credential_id = cred.id
        db.add(conn)
        await db.flush()
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            # Even an unpinned login-needing tool does not override an explicit
            # "run against THIS connection".
            narrowing={"tools": {"Odoo": {"enabled": True}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(
            db, agent=agent, run_context={"mcp_connection_id": str(pinned.id)}
        )
        assert error is None
        assert resolved is not None
        assert resolved.name == "Delegated"


async def test_unknown_run_context_override_keeps_its_own_error(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    unknown = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _agent(db, tenant, dept_id=uuid.uuid4(), narrowing={})

        resolved, error, *_ = await _resolve_mcp_connection(
            db, agent=agent, run_context={"mcp_connection_id": str(unknown)}
        )
        assert resolved is None
        assert error is not None
        assert "unknown MCP connection" in error


# =====================================================================
# Review round 2 -- the "no silent borrowing" constraint on paths the
# first commit made newly reachable.
# =====================================================================


async def _pin(db: Any, tenant: uuid.UUID, name: str) -> m.McpConnection:
    cred, conn = _login_connection(tenant, name)
    db.add(cred)
    await db.flush()
    conn.credential_id = cred.id
    db.add(conn)
    await db.flush()
    return conn


# ------------------------------- Fix 1: a login is never inherited across agents


async def test_delegation_does_not_hand_a_lead_login_to_the_sub_agent(
    app_session: AppSessionFactory,
) -> None:
    """The lead's PERSONAL login must not land in the sub-agent's context, where
    it would outrank the sub-agent's own pin."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        lead_conn = await _pin(db, tenant, "Odoo")
        sub_conn = await _pin(db, tenant, "Odoo")
        lead = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lead")
        sub = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="Ada",
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(sub_conn.id)}}},
        )
        db.add_all([lead, sub])
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(task)
        await db.flush()

        _out, sub_run_id = await _delegate(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="1",
                name="delegate_task",
                arguments={"agent_id": str(sub.id), "task_text": "do the thing"},
            ),
            mcp_conn=lead_conn,
            run_id=None,
        )
        assert sub_run_id is not None
        sub_run = await db.get(m.AgentRun, sub_run_id)
        assert sub_run is not None
        assert "mcp_connection_id" not in sub_run.context, "a login must never be inherited"

        # ...and the sub-agent then resolves its OWN login, not the lead's.
        resolved, error, *_ = await _resolve_mcp_connection(
            db, agent=sub, run_context=sub_run.context
        )
        assert error is None
        assert resolved is not None
        assert resolved.id == sub_conn.id
        assert resolved.id != lead_conn.id


async def test_delegation_to_an_unpinned_sub_agent_fails_loudly_not_silently(
    app_session: AppSessionFactory,
) -> None:
    """The reverse: no pin of its own, no department fallback -- the sub-agent
    must NOT quietly inherit the lead's login."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        lead_conn = await _pin(db, tenant, "Odoo")
        lead = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lead")
        sub = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="Ada",
            narrowing={"tools": {"Odoo": {"enabled": True}}},
        )
        db.add_all([lead, sub])
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(task)
        await db.flush()

        _out, sub_run_id = await _delegate(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="1",
                name="delegate_task",
                arguments={"agent_id": str(sub.id), "task_text": "do the thing"},
            ),
            mcp_conn=lead_conn,
            run_id=None,
        )
        assert sub_run_id is not None
        sub_run = await db.get(m.AgentRun, sub_run_id)
        assert sub_run is not None
        assert "mcp_connection_id" not in sub_run.context

        resolved, error, *_ = await _resolve_mcp_connection(
            db, agent=sub, run_context=sub_run.context
        )
        assert resolved is None, "must not fall back to the lead's login"
        assert error is not None
        assert "Odoo" in error


async def test_delegation_still_inherits_a_shared_department_connection(
    app_session: AppSessionFactory,
) -> None:
    """Unchanged for the legacy path: a department connection is shared by
    construction, so a sub-agent still inherits it and keeps its tools."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        shared = _legacy_connection(tenant, dept_id, "LegacyOAuth")
        db.add(shared)
        lead = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lead")
        sub = m.Agent(tenant_id=tenant, department_id=dept_id, name="Ada")
        db.add_all([lead, sub])
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(task)
        await db.flush()

        _out, sub_run_id = await _delegate(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="1",
                name="delegate_task",
                arguments={"agent_id": str(sub.id), "task_text": "do the thing"},
            ),
            mcp_conn=shared,
            run_id=None,
        )
        assert sub_run_id is not None
        sub_run = await db.get(m.AgentRun, sub_run_id)
        assert sub_run is not None
        assert sub_run.context["mcp_connection_id"] == str(shared.id)


async def test_wakeup_does_not_hand_a_sub_agent_login_to_the_lead(
    app_session: AppSessionFactory,
) -> None:
    """The other propagation site: a finished sub-run's login must not land in
    the delegating lead's wake-up run."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        sub_conn = await _pin(db, tenant, "Odoo")
        lead = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lead")
        sub = m.Agent(tenant_id=tenant, department_id=dept_id, name="Ada")
        db.add_all([lead, sub])
        await db.flush()
        lead_task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(lead_task)
        await db.flush()

        wake_id = await _maybe_wake_parent(
            db,
            repo=RunRepository(db),
            tenant_id=tenant,
            parent_task_id=lead_task.id,
            delegation_depth=1,
            finished_agent_id=sub.id,
            sub_task_label="the sub work",
            output="done",
            succeeded=True,
            mcp_conn=sub_conn,
        )
        assert wake_id is not None
        wake = await db.get(m.AgentRun, wake_id)
        assert wake is not None
        assert "mcp_connection_id" not in wake.context

        # A shared department connection still rides along, as before.
        shared = _legacy_connection(tenant, dept_id, "LegacyOAuth")
        db.add(shared)
        await db.flush()
        wake2_id = await _maybe_wake_parent(
            db,
            repo=RunRepository(db),
            tenant_id=tenant,
            parent_task_id=lead_task.id,
            delegation_depth=1,
            finished_agent_id=sub.id,
            sub_task_label="the sub work",
            output="done",
            succeeded=True,
            mcp_conn=shared,
        )
        assert wake2_id is not None
        wake2 = await db.get(m.AgentRun, wake2_id)
        assert wake2 is not None
        assert wake2.context["mcp_connection_id"] == str(shared.id)


# --- Fix 1b: a DEPARTMENT connection is withheld too when the target claims the key


async def test_delegation_still_inherits_a_department_connection_the_sub_agent_only_enabled(
    app_session: AppSessionFactory,
) -> None:
    """Regression guard for the fix below: a sub-agent that merely ENABLES a tool
    key -- no pin, and no login exists behind that name -- claims nothing, so the
    department connection still rides along and it keeps its tools."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        shared = _legacy_connection(tenant, dept_id, "LegacyOAuth")
        db.add(shared)
        lead = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lead")
        sub = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="Ada",
            narrowing={"tools": {"LegacyOAuth": {"enabled": True}}},
        )
        db.add_all([lead, sub])
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(task)
        await db.flush()

        _out, sub_run_id = await _delegate(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="1",
                name="delegate_task",
                arguments={"agent_id": str(sub.id), "task_text": "do the thing"},
            ),
            mcp_conn=shared,
            run_id=None,
        )
        assert sub_run_id is not None
        sub_run = await db.get(m.AgentRun, sub_run_id)
        assert sub_run is not None
        assert sub_run.context["mcp_connection_id"] == str(shared.id)


async def test_delegation_withholds_a_department_connection_from_a_login_owed_sub_agent(
    app_session: AppSessionFactory,
) -> None:
    """The lead resolved through a legacy DEPARTMENT connection, so nothing
    personal is being handed over -- but the sub-agent has that same tool key
    enabled and a login exists behind it that nobody pinned to this agent.
    Propagating the department id would be read first at resolution and returned,
    swallowing the missing-pin error the sub-agent is owed."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        shared = _legacy_connection(tenant, dept_id, "Odoo")
        db.add(shared)
        await _pin(db, tenant, "Odoo")  # a login-backed "Odoo" exists tenant-wide
        lead = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lead")
        sub = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="Ada",
            narrowing={"tools": {"Odoo": {"enabled": True}}},
        )
        db.add_all([lead, sub])
        await db.flush()
        task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(task)
        await db.flush()

        _out, sub_run_id = await _delegate(
            db,
            tenant_id=tenant,
            agent=lead,
            task=task,
            tc=ToolCall(
                id="1",
                name="delegate_task",
                arguments={"agent_id": str(sub.id), "task_text": "do the thing"},
            ),
            mcp_conn=shared,
            run_id=None,
        )
        assert sub_run_id is not None
        sub_run = await db.get(m.AgentRun, sub_run_id)
        assert sub_run is not None
        assert "mcp_connection_id" not in sub_run.context

        # ...so the sub-agent's own resolution still fails loudly, as designed.
        resolved, error, *_ = await _resolve_mcp_connection(
            db, agent=sub, run_context=sub_run.context
        )
        assert resolved is None, "must not silently borrow the department connection"
        assert error is not None
        assert "Odoo" in error


async def test_wakeup_still_inherits_a_department_connection_the_lead_only_enabled(
    app_session: AppSessionFactory,
) -> None:
    """Regression guard on the other site: a lead claiming no key of its own
    keeps inheriting the shared connection."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        shared = _legacy_connection(tenant, dept_id, "LegacyOAuth")
        db.add(shared)
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="Lead",
            narrowing={"tools": {"LegacyOAuth": {"enabled": True}}},
        )
        sub = m.Agent(tenant_id=tenant, department_id=dept_id, name="Ada")
        db.add_all([lead, sub])
        await db.flush()
        lead_task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(lead_task)
        await db.flush()

        wake_id = await _maybe_wake_parent(
            db,
            repo=RunRepository(db),
            tenant_id=tenant,
            parent_task_id=lead_task.id,
            delegation_depth=1,
            finished_agent_id=sub.id,
            sub_task_label="the sub work",
            output="done",
            succeeded=True,
            mcp_conn=shared,
        )
        assert wake_id is not None
        wake = await db.get(m.AgentRun, wake_id)
        assert wake is not None
        assert wake.context["mcp_connection_id"] == str(shared.id)


async def test_wakeup_withholds_a_department_connection_from_a_lead_with_its_own_pin(
    app_session: AppSessionFactory,
) -> None:
    """The pinned half of the same rule: the lead pinned its OWN login for that
    tool key, so an inherited department id -- which outranks a narrowing pin --
    would make the wake-up run act through the shared connection instead."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        shared = _legacy_connection(tenant, dept_id, "Odoo")
        db.add(shared)
        lead_conn = await _pin(db, tenant, "Odoo")
        lead = m.Agent(
            tenant_id=tenant,
            department_id=dept_id,
            name="Lead",
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(lead_conn.id)}}},
        )
        sub = m.Agent(tenant_id=tenant, department_id=dept_id, name="Ada")
        db.add_all([lead, sub])
        await db.flush()
        lead_task = m.Task(
            tenant_id=tenant,
            department_id=dept_id,
            assigned_agent_id=lead.id,
            title="big job",
            state="in_progress",
            delegation_depth=0,
        )
        db.add(lead_task)
        await db.flush()

        wake_id = await _maybe_wake_parent(
            db,
            repo=RunRepository(db),
            tenant_id=tenant,
            parent_task_id=lead_task.id,
            delegation_depth=1,
            finished_agent_id=sub.id,
            sub_task_label="the sub work",
            output="done",
            succeeded=True,
            mcp_conn=shared,
        )
        assert wake_id is not None
        wake = await db.get(m.AgentRun, wake_id)
        assert wake is not None
        assert "mcp_connection_id" not in wake.context

        # ...and the lead's wake-up run then resolves to its OWN pinned login.
        resolved, error, *_ = await _resolve_mcp_connection(
            db, agent=lead, run_context=wake.context
        )
        assert error is None
        assert resolved is not None
        assert resolved.id == lead_conn.id


# --------------------- Fix 2: the pin reaches the container runtime too


async def test_a_resolved_pin_is_stamped_onto_the_run_context(
    app_session: AppSessionFactory,
) -> None:
    """The container resolves connections for itself and honours only the run
    context, so a pin has to be written there or isolation silently borrows the
    department's connection."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = await _pin(db, tenant, "Odoo")
        # A department connection the container would otherwise have taken.
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth"))
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(conn.id)}}},
        )
        run_id = (
            await RunRepository(db).create(
                tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
            )
        ).id
        conn_id = conn.id

    async def ok(db: Any, **kw: Any) -> RunResult:
        return RunResult(uuid.uuid4(), kw["agent"].id, "done", "ok", [], 1)

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(ok),
    )

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert run.context["mcp_connection_id"] == str(conn_id)

        # ...and that is exactly what the container's own resolver then reads.
        from oc8.api.mcp_gateway import _connections

        dept = m.Department(tenant_id=tenant, name="Sales", frame={"tools": {}})
        dept.id = dept_id
        conns = await _connections(db, run, dept)
        assert [c.id for c in conns] == [conn_id]


async def test_two_resolved_pins_are_both_stamped_onto_the_run_context(
    app_session: AppSessionFactory,
) -> None:
    """A single mcp_connection_id would collapse the gateway back to one
    system. Two pins have to travel as a list so both logins stay reachable
    under isolation."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        narrowing_tools: dict[str, Any] = {}
        pinned_ids: dict[str, uuid.UUID] = {}
        for name in ("Zulip", "Aardvark"):
            conn = await _pin(db, tenant, name)
            narrowing_tools[name] = {"enabled": True, "connection_id": str(conn.id)}
            pinned_ids[name] = conn.id
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth"))
        agent = await _agent(db, tenant, dept_id=dept_id, narrowing={"tools": narrowing_tools})
        run_id = (
            await RunRepository(db).create(
                tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
            )
        ).id

    async def ok(db: Any, **kw: Any) -> RunResult:
        return RunResult(uuid.uuid4(), kw["agent"].id, "done", "ok", [], 1)

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(ok),
    )

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        stamped = {uuid.UUID(str(i)) for i in run.context["mcp_connection_ids"]}
        assert stamped == set(pinned_ids.values())
        # Primary slot stays first-key-alphabetically for callers that still
        # read a single id (control tools). It must not be the only signal.
        assert run.context["mcp_connection_id"] == str(pinned_ids["Aardvark"])

        from oc8.api.mcp_gateway import _connections

        dept = m.Department(tenant_id=tenant, name="Sales", frame={"tools": {}})
        dept.id = dept_id
        conns = await _connections(db, run, dept)
        assert {c.id for c in conns} == set(pinned_ids.values())


async def test_the_department_fallback_is_not_stamped(app_session: AppSessionFactory) -> None:
    """Only a pin is stamped. Stamping a department connection would collapse
    the gateway's multi-connection list back to one."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(_legacy_connection(tenant, dept_id, "LegacyOAuth"))
        agent = await _agent(db, tenant, dept_id=dept_id, narrowing={})
        run_id = (
            await RunRepository(db).create(
                tenant_id=tenant, agent_id=agent.id, context={"task": "do it"}
            )
        ).id

    async def ok(db: Any, **kw: Any) -> RunResult:
        return RunResult(uuid.uuid4(), kw["agent"].id, "done", "ok", [], 1)

    await execute_run(
        RunMessage(run_id=str(run_id), tenant_id=str(tenant), entry_id="0-0", redelivered=False),
        runtime=_FnRuntime(ok),
    )

    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        assert "mcp_connection_id" not in run.context
        assert "mcp_connection_ids" not in run.context


# ------------- Fix 3: one correct pin must not mask another tool's missing pin


async def test_a_correct_pin_does_not_mask_a_missing_pin_on_another_tool(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        good = await _pin(db, tenant, "Aardvark")
        await _pin(db, tenant, "Odoo")  # a login exists for Odoo...
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            narrowing={
                "tools": {
                    # ...but the agent was enabled for it without one.
                    "Aardvark": {"enabled": True, "connection_id": str(good.id)},
                    "Odoo": {"enabled": True},
                }
            },
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None, "Aardvark's correct pin must not stand in for Odoo's missing one"
        assert error is not None
        assert "Odoo" in error


# ------------------------- Fix 4: a pin must point at the tool it is pinned under


async def test_a_pin_to_a_different_tools_connection_is_a_hard_error(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        other = await _pin(db, tenant, "Aardvark")
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            # "Odoo" pinned to the Aardvark login.
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(other.id)}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is not None
        assert "Odoo" in error
        assert "Aardvark" in error


async def test_a_login_tool_pinned_to_a_shared_connection_is_a_hard_error(
    app_session: AppSessionFactory,
) -> None:
    """Right name, wrong kind: a tool that HAS logins must be pinned to one of
    them, not to the department's shared connection."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _pin(db, tenant, "Odoo")  # a login for "Odoo" exists
        shared = _legacy_connection(tenant, dept_id, "Odoo")  # ...and so does a shared one
        db.add(shared)
        await db.flush()
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(shared.id)}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is not None
        assert "Odoo" in error


# ============================================================
# The department's own default login (default_connection_id)
# ============================================================


async def _department(
    db: Any, dept_id: uuid.UUID, tenant: uuid.UUID, *, tools: dict[str, Any]
) -> None:
    dept = m.Department(tenant_id=tenant, name="Sales", frame={"tools": tools})
    dept.id = dept_id
    db.add(dept)
    await db.flush()


async def test_department_default_satisfies_a_login_needing_tool_with_no_pin(
    app_session: AppSessionFactory,
) -> None:
    """The department named a login for this tool; an agent with no pin of its
    own uses it instead of erroring."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        default_conn = await _pin(db, tenant, "Odoo")
        await _department(
            db, dept_id, tenant, tools={"Odoo": {"default_connection_id": str(default_conn.id)}}
        )
        agent = await _agent(
            db, tenant, dept_id=dept_id, narrowing={"tools": {"Odoo": {"enabled": True}}}
        )

        resolved, error, pinned, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert error is None
        assert resolved is not None
        assert resolved.id == default_conn.id
        assert pinned is True, "an explicit department default is stamped, like a pin"


async def test_agents_own_pin_still_wins_over_the_department_default(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        dept_default = await _pin(db, tenant, "Odoo")
        agent_pin = await _pin(db, tenant, "Odoo")
        await _department(
            db, dept_id, tenant, tools={"Odoo": {"default_connection_id": str(dept_default.id)}}
        )
        agent = await _agent(
            db,
            tenant,
            dept_id=dept_id,
            narrowing={"tools": {"Odoo": {"enabled": True, "connection_id": str(agent_pin.id)}}},
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert error is None
        assert resolved is not None
        assert resolved.id == agent_pin.id


async def test_no_department_default_still_hard_errors_as_before(
    app_session: AppSessionFactory,
) -> None:
    """A department that never set a default keeps today's exact behaviour:
    no silent guess."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _pin(db, tenant, "Odoo")
        await _department(db, dept_id, tenant, tools={"Odoo": {"enabled": True}})
        agent = await _agent(
            db, tenant, dept_id=dept_id, narrowing={"tools": {"Odoo": {"enabled": True}}}
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is not None
        assert "Odoo" in error


async def test_department_default_overrides_the_untargeted_legacy_lookup(
    app_session: AppSessionFactory,
) -> None:
    """A department can point its default at a DIFFERENT department's own
    (non-login) connection -- e.g. reusing another team's Odoo setup -- and
    that explicit choice wins over this department's own implicit lookup."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    other_dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        own = _legacy_connection(tenant, dept_id, "Odoo", age_days=1)
        other = _legacy_connection(tenant, other_dept_id, "Odoo", age_days=30)
        db.add_all([own, other])
        await db.flush()
        await _department(
            db, dept_id, tenant, tools={"Odoo": {"default_connection_id": str(other.id)}}
        )
        agent = await _agent(
            db, tenant, dept_id=dept_id, narrowing={"tools": {"Odoo": {"enabled": True}}}
        )

        resolved, error, pinned, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert error is None
        assert resolved is not None
        assert resolved.id == other.id, "the explicit default beats this department's own lookup"
        assert pinned is True


async def test_department_default_pointing_at_the_wrong_tool_name_is_a_hard_error(
    app_session: AppSessionFactory,
) -> None:
    """Same by-name guard as an agent's own pin: a default must actually name a
    connection FOR this tool key."""
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    async with app_session(tenant) as db:
        wrong = await _pin(db, tenant, "Aardvark")
        await _department(
            db, dept_id, tenant, tools={"Odoo": {"default_connection_id": str(wrong.id)}}
        )
        agent = await _agent(
            db, tenant, dept_id=dept_id, narrowing={"tools": {"Odoo": {"enabled": True}}}
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is not None
        assert "Odoo" in error


async def test_department_default_that_no_longer_exists_is_a_hard_error(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    dept_id = uuid.uuid4()
    gone = uuid.uuid4()
    async with app_session(tenant) as db:
        await _department(db, dept_id, tenant, tools={"Odoo": {"default_connection_id": str(gone)}})
        agent = await _agent(
            db, tenant, dept_id=dept_id, narrowing={"tools": {"Odoo": {"enabled": True}}}
        )

        resolved, error, *_ = await _resolve_mcp_connection(db, agent=agent, run_context={})
        assert resolved is None
        assert error is not None
        assert "Odoo" in error
