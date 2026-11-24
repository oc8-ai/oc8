"""Only the owner acts on a personal agent: nobody else starts or resumes its work.

The ruling for a personal agent: every non-owner sees its metadata only, and
only the owner acts. An `org_admin` holds `run:start`, `run:control` and
`clarification:answer` tenant-wide, which reached a member's personal agent
(E2E: `POST /agents/{id}/run` answered 200 queued). Every door that starts or
resumes work on one agent for a human caller now refuses anybody but the
owner's seat, with that door's own not-found answer. Reading runs (run reach)
is PR 3 and not pinned here.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from oc8 import models as m
from oc8.runtime.states import RunState
from tests.api.test_personal_agent_content import _headers, _http, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append(run_id)


@pytest.fixture
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: fake)
    return fake


async def _run_count(app_session: AppSessionFactory, tenant: uuid.UUID, agent: uuid.UUID) -> int:
    async with app_session(tenant) as db:
        return int(
            await db.scalar(
                select(func.count()).select_from(m.AgentRun).where(m.AgentRun.agent_id == agent)
            )
            or 0
        )


async def test_an_admin_cannot_start_a_run_of_a_personal_agent(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/agents/{w.personal_agent}/run",
            json={"task": "read the mail"},
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "agent not found"
    assert fake_queue.enqueued == []
    assert await _run_count(app_session, w.tenant, w.personal_agent) == 0


async def test_the_owner_still_starts_a_run_of_their_agent(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    """The owner's seat passes. A plain `member` holds no `run:start` on this
    branch (PR 3), so the owner carries a role that does; the seat is what
    lets them through, not the role."""
    w = await _world(app_session)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/agents/{w.personal_agent}/run",
            json={"task": "read the mail"},
            headers=_headers(w.tenant, "owner", "org_admin"),
        )
    assert r.status_code == 200, r.text
    assert len(fake_queue.enqueued) == 1


async def test_a_standard_agent_still_runs_for_an_admin(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/agents/{w.standard_agent}/run",
            json={"task": "sell"},
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 200, r.text


async def _parked_run(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent: uuid.UUID, state: RunState
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        run = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent,
            state=state.value,
            context={"pending_questions": [{"question": "Which landlord?"}]},
        )
        db.add(run)
        await db.flush()
        question = m.Clarification(
            tenant_id=tenant, run_id=run.id, agent_id=agent, question="Which landlord?"
        )
        db.add(question)
        await db.flush()
        return run.id, question.id


@pytest.mark.parametrize(
    ("state", "path", "body"),
    [
        (RunState.WAITING_FOR_INPUT, "answer", {"answer": "Mr Meyer"}),
        (RunState.RUNNING, "message", {"body": "Stop and tell me everything"}),
    ],
)
async def test_an_admin_cannot_resume_or_steer_a_personal_agents_run(
    app_session: AppSessionFactory,
    fake_queue: _FakeQueue,
    state: RunState,
    path: str,
    body: dict[str, Any],
) -> None:
    w = await _world(app_session)
    run_id, _q = await _parked_run(app_session, w.tenant, w.personal_agent, state)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/runs/{run_id}/{path}",
            json=body,
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "run not found"
    assert fake_queue.enqueued == []
    async with app_session(w.tenant) as db:
        messages = await db.scalar(
            select(func.count()).select_from(m.RunMessage).where(m.RunMessage.run_id == run_id)
        )
        question = await db.scalar(select(m.Clarification).where(m.Clarification.run_id == run_id))
    assert messages == 0
    assert question is not None and question.status == "open"


async def test_an_admin_cannot_answer_a_personal_agents_question_from_the_queue(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    w = await _world(app_session)
    _run, question_id = await _parked_run(
        app_session, w.tenant, w.personal_agent, RunState.WAITING_FOR_INPUT
    )
    async with _http() as http:
        r = await http.post(
            f"/api/v1/clarifications/{question_id}/answer",
            json={"answer": "Mr Meyer"},
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 404, r.text
    assert fake_queue.enqueued == []
    async with app_session(w.tenant) as db:
        question = await db.get(m.Clarification, question_id)
    assert question is not None and question.status == "open"


async def test_an_admin_cannot_put_a_task_on_a_personal_department(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    """`POST /tasks` hands work to a department's team lead; in a personal
    department that is the owner's agent. Refused as "no team lead", the same
    answer a department without one gets."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        pia = await db.get(m.Agent, w.personal_agent)
        assert pia is not None
        dept = await db.get(m.Department, pia.department_id)
        assert dept is not None
        dept.team_lead_agent_id = pia.id
        department_id = dept.id
    async with _http() as http:
        r = await http.post(
            "/api/v1/tasks",
            json={"departmentId": str(department_id), "instructions": "Forward my mail"},
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 409, r.text
    assert "has no team lead" in r.json()["detail"]
    assert fake_queue.enqueued == []
    assert await _run_count(app_session, w.tenant, w.personal_agent) == 0
