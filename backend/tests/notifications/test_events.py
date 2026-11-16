"""The three event hooks: a board created, a run failed, an approval decided.

Each one is called from its business funnel (`_apply_board_create`,
`RunRepository.transition`, `decide_approval`) inside that funnel's own
transaction, so these tests drive the funnels rather than the hooks alone.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.approvals import decide_approval
from oc8.auth.principal import Principal
from oc8.authz.permissions import SEAT_APPROVER, SEAT_VIEWER
from oc8.authz.scope import HumanActor, scope_for_principal, subject_uuid_for
from oc8.copilot.capabilities import BoardCreate, _apply_board_create
from oc8.notifications import delivery, events, service
from oc8.runtime import repository as run_repository
from oc8.runtime.repository import RunRepository
from oc8.runtime.states import RunState
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

SPEC: dict[str, Any] = {
    "datasets": [
        {
            "key": "sales",
            "label": "Sales",
            "grain": "day",
            "fields": [{"key": "amount", "label": "Amount", "type": "money", "currency": "EUR"}],
        }
    ],
    "blocks": [{"type": "kpi", "dataset": "sales", "field": "amount"}],
}


async def _drain() -> None:
    while service._background_tasks:
        await asyncio.gather(*list(service._background_tasks), return_exceptions=True)


@pytest.fixture(autouse=True)
def _no_run_state_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal transition deletes the run's saved state after commit, which
    reaches Redis and object storage; neither exists here and neither is what
    these tests are about."""
    monkeypatch.setattr(run_repository, "_delete_state_after_commit", lambda *a, **k: None)


@pytest.fixture
async def delivered(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    async def _fake_deliver(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(delivery, "deliver", _fake_deliver)
    yield calls
    await _drain()


async def _rows(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.MemberNotification]:
    async with app_session(tenant) as db:
        return list((await db.execute(select(m.MemberNotification))).scalars().all())


async def _member(
    db: Any,
    tenant: uuid.UUID,
    *,
    seat_in: uuid.UUID | None = None,
    seat_role: str = SEAT_VIEWER,
    agent_manage: bool = False,
    all_departments: bool = False,
    name: str = "",
) -> m.OrgMember:
    subject = f"u-{uuid.uuid4().hex}"
    member = m.OrgMember(
        tenant_id=tenant,
        subject=subject,
        subject_uuid=subject_uuid_for(subject),
        display_name=name,
        all_departments=all_departments,
    )
    db.add(member)
    await db.flush()
    if seat_in is not None:
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=member.id,
                department_id=seat_in,
                seat_role=seat_role,
                agent_manage=agent_manage,
            )
        )
        await db.flush()
    return member


async def _office(db: Any, tenant: uuid.UUID, *, copilot: bool = False) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name=f"Vertrieb {uuid.uuid4().hex[:4]}")
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="idle",
        definition={},
        presentation={},
        is_tenant_assistant=copilot,
    )
    db.add(agent)
    await db.flush()
    return agent


async def _run(
    db: Any,
    agent: m.Agent,
    context: dict[str, Any],
    *,
    state: RunState = RunState.RUNNING,
    task_id: uuid.UUID | None = None,
) -> m.AgentRun:
    run = m.AgentRun(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        state=state.value,
        context=context,
        source="manual",
        task_id=task_id,
    )
    db.add(run)
    await db.flush()
    return run


# --- board created -----------------------------------------------------------


async def test_board_create_notifies_the_departments_seats_but_not_the_actor(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        actor = await _member(db, tenant, seat_in=agent.department_id)
        seated = await _member(db, tenant, seat_in=agent.department_id)
        await _member(db, tenant, all_departments=True)
        other = await _office(db, tenant)
        await _member(db, tenant, seat_in=other.department_id)
        board_id = await _apply_board_create(
            db,
            tenant_id=tenant,
            operation=BoardCreate.model_validate(
                {"type": "board.create", "agentId": agent.id, "title": "Umsatz", "spec": SPEC}
            ),
            actor_member_id=actor.id,
        )
    await _drain()
    rows = await _rows(app_session, tenant)
    assert [r.member_id for r in rows] == [seated.id]
    row = rows[0]
    assert row.kind == "board.available"
    assert row.subject_id == board_id
    assert row.title == {"en": "New dashboard", "de": "Neues Dashboard"}
    assert row.body == {"en": "Umsatz · Nora", "de": "Umsatz · Nora"}
    assert row.link == "/workspace"
    assert len(delivered) == 1


# --- run failed ---------------------------------------------------------------


async def _fail(app_session: AppSessionFactory, tenant: uuid.UUID, run_id: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        run = await db.get(m.AgentRun, run_id)
        assert run is not None
        await RunRepository(db).transition(run, RunState.FAILED)


async def test_a_failed_manual_run_notifies_its_starter(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        starter = await _member(db, tenant)
        await _member(db, tenant, seat_in=agent.department_id, agent_manage=True)
        run = await _run(
            db,
            agent,
            {"originating_operator": starter.subject, "error": "agent awaiting hire approval"},
        )
        agent_id, run_id = agent.id, run.id
    await _fail(app_session, tenant, run_id)
    await _drain()
    rows = await _rows(app_session, tenant)
    assert [r.member_id for r in rows] == [starter.id]
    row = rows[0]
    assert row.kind == "run.failed"
    assert row.subject_id == agent_id
    assert row.title == {"en": "Agent 'Nora' failed", "de": "Agent ‚Nora‘ fehlgeschlagen"}  # noqa: RUF001
    assert row.body == dict(zip(("en", "de"), events.CATEGORY_TEXT["error"], strict=True))
    assert row.link == f"/agents/{agent_id}?tab=live"


async def test_a_failed_scheduled_run_for_nobody_notifies_the_agent_managers(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        manager = await _member(db, tenant, seat_in=agent.department_id, agent_manage=True)
        await _member(db, tenant, seat_in=agent.department_id)  # a seat, but no agent_manage
        await _member(db, tenant, all_departments=True)
        run = await _run(db, agent, {"acting_person_withheld": "schedule_without_owner"})
        run_id = run.id
    await _fail(app_session, tenant, run_id)
    await _drain()
    assert [r.member_id for r in await _rows(app_session, tenant)] == [manager.id]


async def test_a_failed_copilot_run_notifies_nobody(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant, copilot=True)
        starter = await _member(db, tenant)
        await _member(db, tenant, seat_in=agent.department_id, agent_manage=True)
        run = await _run(db, agent, {"originating_operator": starter.subject})
        run_id = run.id
    await _fail(app_session, tenant, run_id)
    await _drain()
    assert await _rows(app_session, tenant) == []
    assert delivered == []


async def test_interrupted_and_already_failed_runs_do_not_notify(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        starter = await _member(db, tenant)
        ctx = {"originating_operator": starter.subject}
        interrupted = await _run(db, agent, ctx)
        failed = await _run(db, agent, ctx, state=RunState.FAILED)
        ids = (interrupted.id, failed.id)
    async with app_session(tenant) as db:
        repo = RunRepository(db)
        run = await db.get(m.AgentRun, ids[0])
        assert run is not None
        await repo.transition(run, RunState.INTERRUPTED)
        again = await db.get(m.AgentRun, ids[1])
        assert again is not None
        await repo.transition(again, RunState.FAILED)  # already terminal: left alone
    await _drain()
    assert await _rows(app_session, tenant) == []


async def test_two_failures_of_one_agent_collapse_into_one_unread_row(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        starter = await _member(db, tenant)
        ctx = {"originating_operator": starter.subject}
        first, second = await _run(db, agent, ctx), await _run(db, agent, ctx)
        ids = (first.id, second.id)
    await _fail(app_session, tenant, ids[0])
    await _fail(app_session, tenant, ids[1])
    await _drain()
    rows = await _rows(app_session, tenant)
    assert len(rows) == 1
    assert rows[0].read_at is None


async def test_two_concurrent_failures_of_one_agent_collapse_into_one_unread_row(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        starter = await _member(db, tenant)
        ctx = {"originating_operator": starter.subject}
        runs = [await _run(db, agent, ctx) for _ in range(2)]
        ids = [r.id for r in runs]
    await asyncio.gather(*(_fail(app_session, tenant, run_id) for run_id in ids))
    await _drain()
    rows = await _rows(app_session, tenant)
    assert len(rows) == 1
    assert rows[0].member_id == starter.id
    assert len(delivered) == 2


async def test_the_body_never_carries_the_raw_error(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    secret = "RuntimeError('IBAN DE89 3704 0044 0532 0130 00 for Frau Müller')"
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        starter = await _member(db, tenant)
        run = await _run(db, agent, {"originating_operator": starter.subject, "error": secret})
        run_id = run.id
    await _fail(app_session, tenant, run_id)
    await _drain()
    (row,) = await _rows(app_session, tenant)
    assert "IBAN" not in str(row.body) and "IBAN" not in str(row.title)
    assert all("IBAN" not in str(call) for call in delivered)


# Every FAILED site the executor (and the two closers beside it) writes, with
# the `context` it leaves behind. See the mapping table in `events.py`.
@pytest.mark.parametrize(
    ("context", "expected"),
    [
        # executor ~846: the agent row is gone
        ({"error": "agent not found"}, "error"),
        # executor ~894: the hire gate
        ({"error": "agent awaiting hire approval"}, "error"),
        # executor ~1025: no usable login for a tool (`_resolve_mcp_connection`)
        (
            {
                "error": "tool 'odoo' needs a login but none is assigned to this agent: "
                "set connection_id on the agent's narrowing for this tool"
            },
            "connection_required",
        ),
        ({"error": "tool 'odoo' has an unreadable pinned login: 'x'"}, "connection_required"),
        (
            {"error": "tool 'odoo' is pinned to a login that no longer exists: 1"},
            "connection_required",
        ),
        ({"error": "unknown MCP connection: 7"}, "connection_required"),
        # runtime_env: a connection the person has to make
        ({"connection_required": {"integration": "odoo"}}, "connection_required"),
        # executor ~1272: repr() of whatever the runtime raised
        ({"error": "TimeoutError()"}, "step_limit"),
        ({"error": "ProviderBudgetExceeded('openai')"}, "model_error"),
        ({"error": "HTTPStatusError('Client error 400')"}, "model_error"),
        ({"error": "AutoRouterError('no candidates')"}, "model_error"),
        ({"error": "TenantKeyRequired('anthropic')"}, "model_error"),
        ({"error": "ValueError('boom')"}, "error"),
        # executor ~1431: the same question asked twice
        ({"error": "the agent asked the same question twice"}, "error"),
        # executor ~1464: a status the runtime does not know
        ({"error": "unknown run status: 'weird'"}, "error"),
        # executor ~1464: the runtime's own `failed` verdict (output, no error)
        ({"output": "Auto model router misconfigured: no rule"}, "model_error"),
        ({"output": "The model used its whole answer budget (8,000 tokens)"}, "model_error"),
        (
            {"output": "The monthly token quota for openai is used up until 2026-12-01"},
            "model_error",
        ),
        (
            {"output": "The model provider is rate-limiting requests"},
            "model_error",
        ),
        ({"output": "Reached step limit."}, "step_limit"),
        ({"output": "I could not finish."}, "error"),
        # reclaim / reconciler: nobody reported in
        ({"error": "lease lost: worker died mid-run"}, "error"),
        ({}, "error"),
    ],
)
def test_failure_category(context: dict[str, Any], expected: str) -> None:
    run = m.AgentRun(context=context, state=RunState.FAILED.value)
    assert events.failure_category(run) == expected


# --- approval decided -----------------------------------------------------------


async def _actor(db: Any, member: m.OrgMember) -> HumanActor:
    principal = Principal(subject=member.subject, tenant_id=member.tenant_id, role="member")
    resolved, scope = await scope_for_principal(db, principal)
    assert resolved is not None
    return HumanActor(principal=principal, member=resolved, scope=scope)


async def _held_call(
    db: Any,
    tenant: uuid.UUID,
    *,
    agent: m.Agent | None = None,
    person: m.OrgMember | None = None,
    classification: str = "",
) -> tuple[m.Agent, m.ApprovalRequest, m.OrgMember]:
    """An agent whose run, started by `person` (or a new member), waits on a
    held tool call in its department; returns (agent, approval, person)."""
    if agent is None:
        agent = await _office(db, tenant)
    if person is None:
        person = await _member(db, tenant)
    task = m.Task(
        tenant_id=tenant,
        department_id=agent.department_id,
        title="t",
        state="waiting_for_approval",
    )
    db.add(task)
    await db.flush()
    await _run(
        db,
        agent,
        {"originating_operator": person.subject},
        state=RunState.WAITING_FOR_APPROVAL,
        task_id=task.id,
    )
    approval = m.ApprovalRequest(
        tenant_id=tenant,
        agent_id=agent.id,
        department_id=agent.department_id,
        task_id=task.id,
        action_type="tool_send",
        status="pending",
        title="Angebot senden",
        detail="",
        payload={"tool": "send", "arguments": {}}
        | ({"classification": classification} if classification else {}),
    )
    db.add(approval)
    await db.flush()
    return agent, approval, person


async def test_a_decision_notifies_the_runs_person_not_the_decider(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, approval, person = await _held_call(db, tenant, classification="confidential")
        decider = await _member(
            db, tenant, seat_in=agent.department_id, seat_role=SEAT_APPROVER, name="Grete"
        )
        await decide_approval(
            db, approval, decision="reject", tenant_id=tenant, actor=await _actor(db, decider)
        )
        approval_id, agent_id = approval.id, agent.id
    await _drain()
    (row,) = await _rows(app_session, tenant)
    assert row.member_id == person.id
    assert row.kind == "approval.decided"
    assert row.subject_id == approval_id
    assert row.title == {"en": "Rejected: Angebot senden", "de": "Abgelehnt: Angebot senden"}
    assert row.body == {"en": "Grete", "de": "Grete"}
    assert row.link == f"/agents/{agent_id}?tab=live"
    (call,) = delivered
    assert call["classification"] == "confidential"


async def test_an_approval_by_the_runs_own_person_notifies_nobody(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        person = await _member(db, tenant, seat_in=agent.department_id, seat_role=SEAT_APPROVER)
        _, approval, _ = await _held_call(db, tenant, agent=agent, person=person)
        await decide_approval(
            db, approval, decision="approve", tenant_id=tenant, actor=await _actor(db, person)
        )
    await _drain()
    assert await _rows(app_session, tenant) == []
    assert delivered == []


async def test_a_rolled_back_decision_delivers_nothing(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, approval, _person = await _held_call(db, tenant)
        decider = await _member(db, tenant, seat_in=agent.department_id, seat_role=SEAT_APPROVER)
        approval_id, decider_id = approval.id, decider.id
    with pytest.raises(RuntimeError):
        async with app_session(tenant) as db:
            row = await db.get(m.ApprovalRequest, approval_id)
            member = await db.get(m.OrgMember, decider_id)
            assert row is not None and member is not None
            await decide_approval(
                db, row, decision="approve", tenant_id=tenant, actor=await _actor(db, member)
            )
            raise RuntimeError("the effect failed after the notice was written")
    await _drain()
    assert delivered == []
    assert await _rows(app_session, tenant) == []


async def test_a_raising_push_still_commits_the_decision(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("push service down")

    monkeypatch.setattr(delivery, "send_push_to_members", _boom)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, approval, person = await _held_call(db, tenant)
        decider = await _member(db, tenant, seat_in=agent.department_id, seat_role=SEAT_APPROVER)
        await decide_approval(
            db, approval, decision="approve", tenant_id=tenant, actor=await _actor(db, decider)
        )
        approval_id = approval.id
    await _drain()
    async with app_session(tenant) as db:
        row = await db.get(m.ApprovalRequest, approval_id)
        assert row is not None and row.status == "approved"
    assert [r.member_id for r in await _rows(app_session, tenant)] == [person.id]


# --- the guard ------------------------------------------------------------------


async def _boom(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("notification store down")


async def test_a_failing_hook_still_fails_the_run_and_tells_nobody(
    app_session: AppSessionFactory,
    delivered: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        starter = await _member(db, tenant)
        run = await _run(db, agent, {"originating_operator": starter.subject})
        run_id = run.id
    monkeypatch.setattr(events, "notify", _boom)
    await _fail(app_session, tenant, run_id)
    await _drain()
    async with app_session(tenant) as db:
        row = await db.get(m.AgentRun, run_id)
        assert row is not None and row.state == RunState.FAILED.value
    assert await _rows(app_session, tenant) == []
    assert delivered == []


async def test_a_failing_hook_still_commits_the_decision_and_tells_nobody(
    app_session: AppSessionFactory,
    delivered: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent, approval, _person = await _held_call(db, tenant)
        decider = await _member(db, tenant, seat_in=agent.department_id, seat_role=SEAT_APPROVER)
        approval_id = approval.id
        monkeypatch.setattr(events, "resolve_acting_person", _boom)
        await decide_approval(
            db, approval, decision="approve", tenant_id=tenant, actor=await _actor(db, decider)
        )
    await _drain()
    async with app_session(tenant) as db:
        row = await db.get(m.ApprovalRequest, approval_id)
        assert row is not None and row.status == "approved"
    assert await _rows(app_session, tenant) == []
    assert delivered == []


async def test_a_business_flush_error_is_not_swallowed_by_the_guard(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    """The guard covers the notification only. A pending business row that
    cannot be written must raise from the caller, not be logged as a notice."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        run = await _run(db, agent, {})
        run_id = run.id
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            # Pending, unflushed, and NOT NULL-violating: the business write.
            db.add(m.Task(tenant_id=tenant, department_id=agent.department_id, title=None))
            await events.on_run_failed(db, run)
    assert delivered == []


async def test_a_decision_on_a_chat_run_links_to_the_chat(
    app_session: AppSessionFactory, delivered: list[dict[str, Any]]
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        agent = await _office(db, tenant)
        person = await _member(db, tenant)
        session = m.ChatSession(tenant_id=tenant, agent_id=agent.id, member_id=person.id)
        db.add(session)
        await db.flush()
        _, approval, _ = await _held_call(db, tenant, agent=agent, person=person)
        run = (
            await db.execute(select(m.AgentRun).where(m.AgentRun.task_id == approval.task_id))
        ).scalar_one()
        run.context = {"chat_session_id": str(session.id)}
        decider = await _member(db, tenant, seat_in=agent.department_id, seat_role=SEAT_APPROVER)
        await decide_approval(
            db, approval, decision="approve", tenant_id=tenant, actor=await _actor(db, decider)
        )
        session_id = session.id
    await _drain()
    (row,) = await _rows(app_session, tenant)
    assert row.member_id == person.id
    assert row.link == f"/chat/{session_id}"
