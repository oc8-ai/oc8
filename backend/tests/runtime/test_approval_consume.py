"""One approval covers exactly one run of the call it approved (E2E N1,
security review). `Decisions` is what every dispatch path reads."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.runtime.approval_resume import Decisions, claim_approval, live_decisions
from tests.conftest import AppSessionFactory


def _approve(sig: str, approval_id: str | None = None) -> dict[str, str]:
    entry = {"sig": sig, "decision": "approve"}
    if approval_id is not None:
        entry["approval_id"] = approval_id
    return entry


def test_an_approval_its_call_used_up_approves_nothing_more() -> None:
    ctx = {
        "resolved_tool_approvals": [_approve("s", "a1")],
        "consumed_tool_approvals": [{"sig": "s", "key": "a1"}],
    }
    decisions = Decisions(ctx)
    assert decisions.get("s") is None
    assert not decisions.approved("s")
    assert decisions  # still a resume leg: somebody decided


def test_two_approvals_of_the_same_call_cover_two_runs() -> None:
    ctx = {"resolved_tool_approvals": [_approve("s", "a1"), _approve("s", "a2")]}
    assert Decisions(ctx).open["s"] == ["a1", "a2"]
    assert Decisions(ctx).available("s") == 2
    used = {**ctx, "consumed_tool_approvals": [{"sig": "s", "key": "a1"}]}
    assert Decisions(used).open["s"] == ["a2"]
    both = {**ctx, "consumed_tool_approvals": [{"sig": "s", "key": k} for k in ("a1", "a2")]}
    assert Decisions(both).get("s") is None


def test_an_approval_without_an_id_is_counted() -> None:
    ctx = {
        "resolved_tool_approvals": [_approve("s"), _approve("s")],
        "consumed_tool_approvals": [{"sig": "s", "key": "s#1"}],
    }
    assert Decisions(ctx).open["s"] == ["s#2"]


def test_a_rejection_is_never_used_up() -> None:
    ctx = {
        "resolved_tool_approvals": [{"sig": "s", "decision": "reject"}],
        "consumed_tool_approvals": [{"sig": "s", "key": "s#1"}],
    }
    assert Decisions(ctx).get("s") == "reject"


def test_dropping_forgets_the_approval_in_this_process() -> None:
    decisions = Decisions({"resolved_tool_approvals": [_approve("s", "a1")]})
    assert decisions.drop("s") == "a1"
    assert decisions.get("s") is None
    assert decisions.drop("s") is None


def test_a_plain_map_reads_as_unused_decisions() -> None:
    decisions = Decisions.of({"s": "approve", "t": "reject"})
    assert decisions is not None
    assert (decisions.get("s"), decisions.get("t")) == ("approve", "reject")


def test_a_call_id_an_approval_was_claimed_for_is_known() -> None:
    ctx = {
        "resolved_tool_approvals": [_approve("s", "a1")],
        "consumed_tool_approvals": [{"sig": "s", "key": "a1", "call_id": "c1"}],
    }
    decisions = Decisions(ctx)
    assert decisions.claimed_call("c1") == "a1"
    assert decisions.claimed_call("c2") is None
    assert not decisions.approved("s")


# ------------------------------------------- claimed before the call (review)


async def _run(db: Any, tenant: uuid.UUID, context: dict[str, Any]) -> m.AgentRun:
    dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(dept)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name="Nora",
        status="running",
        narrowing={},
        definition={},
        presentation={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(tenant_id=tenant, department_id=dept.id, title="t", state="in_progress")
    db.add(task)
    await db.flush()
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, task_id=task.id, context=context)
    db.add(run)
    await db.flush()
    return run


@pytest.mark.asyncio
async def test_one_approval_is_claimed_once(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _run(db, tenant, {"resolved_tool_approvals": [_approve("s", "a1")]})
        decisions = Decisions(run.context)
        assert await claim_approval(db, run, decisions, "s", call_id="c1") == "a1"
        assert await claim_approval(db, run, decisions, "s", call_id="c2") is None
        assert run.context["consumed_tool_approvals"] == [
            {"sig": "s", "key": "a1", "call_id": "c1"}
        ]
        # A fresh read -- another leg, a redelivered request -- sees it used.
        fresh = await live_decisions(
            db,
            context={"resolved_tool_approvals": [_approve("s", "a1")]},
            tenant_id=tenant,
            task_id=run.task_id,
        )
        assert not fresh.approved("s")


@pytest.mark.asyncio
async def test_two_approvals_cover_two_claims(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        run = await _run(
            db, tenant, {"resolved_tool_approvals": [_approve("s", "a1"), _approve("s", "a2")]}
        )
        decisions = Decisions(run.context)
        assert await claim_approval(db, run, decisions, "s") == "a1"
        assert await claim_approval(db, run, decisions, "s") == "a2"
        assert await claim_approval(db, run, decisions, "s") is None


@pytest.mark.asyncio
async def test_two_concurrent_claims_of_one_approval_let_exactly_one_win(
    app_session: AppSessionFactory,
) -> None:
    """Two requests that both read the approval as unused before either
    committed: the marker insert decides, and the loser waits, then loses."""
    tenant = uuid.uuid4()
    context = {"resolved_tool_approvals": [_approve("s", "a1")]}
    async with app_session(tenant) as db:
        run_id = (await _run(db, tenant, context)).id

    async def claim(pause: float) -> str | None:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, run_id)
            assert run is not None
            decisions = await live_decisions(
                db, context=run.context, tenant_id=tenant, task_id=run.task_id
            )
            assert decisions.approved("s")  # both see it unused
            await asyncio.sleep(pause)
            return await claim_approval(db, run, decisions, "s")

    results = await asyncio.gather(claim(0.0), claim(0.05))
    assert sorted(r or "" for r in results) == ["", "a1"]
