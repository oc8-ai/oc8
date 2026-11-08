"""The other half of the queue: the question an agent parked mid-run.

There is no endpoint for these at all today. A run that stopped to ask something
sits in `waiting_for_input` and the only way to answer it is
`POST /runs/{run_id}/answer`, which is gated on `run:control` -- a permission in
no seat vocabulary, held only by tenant-wide roles. So the employee whose answer
the agent is waiting for is precisely the person who cannot give it.

That admin door is deliberately left exactly as it is (§4). These tests are about
the new one, which is scoped by the agent's department -- `Clarification.agent_id`
is NOT NULL (`models/run.py:71`), so the department is one join away and the row
needs no column of its own.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


class _FakeQueue:
    """Records what would have gone onto the run stream.

    Patched at `oc8.runtime.intake.get_run_queue`, which `publish_run` looks up
    as a module global at CALL time -- so this works however the route imports
    `publish_run`, and needs no Redis.
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def enqueue(self, *, run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        self.enqueued.append((run_id, tenant_id))


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    q = _FakeQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: q)
    return q


def _subject_uuid(subject: str) -> uuid.UUID:
    try:
        return uuid.UUID(subject)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


async def _seat_for(
    db: Any, tenant: uuid.UUID, subject: str, department_id: uuid.UUID, seat_role: str
) -> m.OrgMember:
    member: m.OrgMember | None = (
        await db.execute(
            select(m.OrgMember).where(
                m.OrgMember.subject == subject, m.OrgMember.deleted_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if member is None:
        member = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=_subject_uuid(subject),
            display_name=subject,
        )
        db.add(member)
        await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=member.id,
            department_id=department_id,
            seat_role=seat_role,
        )
    )
    await db.flush()
    return member


async def _parked_run(
    db: Any, tenant: uuid.UUID, *, department: uuid.UUID, agent_name: str, question: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """An agent that stopped to ask something. Returns (run_id, clarification_id)."""
    agent = m.Agent(tenant_id=tenant, department_id=department, name=agent_name)
    db.add(agent)
    await db.flush()
    run = m.AgentRun(
        tenant_id=tenant,
        agent_id=agent.id,
        state="waiting_for_input",
        context={"task": "t", "pending_question": question},
    )
    db.add(run)
    # Flushed before the Clarification is built: run.id is a Python-side uuid7
    # default applied at flush, and clarification.run_id is NOT NULL.
    await db.flush()
    clar = m.Clarification(
        tenant_id=tenant, run_id=run.id, agent_id=agent.id, question=question, status="open"
    )
    db.add(clar)
    await db.flush()
    return run.id, clar.id


class _Office:
    tenant: uuid.UUID
    sales: uuid.UUID
    engineering: uuid.UUID
    sales_run: uuid.UUID
    sales_clar: uuid.UUID
    engineering_run: uuid.UUID
    engineering_clar: uuid.UUID


async def _office(app_session: AppSessionFactory) -> _Office:
    office = _Office()
    office.tenant = uuid.uuid4()
    async with app_session(office.tenant) as db:
        sales = m.Department(tenant_id=office.tenant, name="Vertrieb")
        engineering = m.Department(tenant_id=office.tenant, name="Entwicklung")
        db.add_all([sales, engineering])
        await db.flush()
        office.sales, office.engineering = sales.id, engineering.id
        office.sales_run, office.sales_clar = await _parked_run(
            db,
            office.tenant,
            department=office.sales,
            agent_name="Nora",
            question="Welche Rabattstufe für Gartenholz?",
        )
        office.engineering_run, office.engineering_clar = await _parked_run(
            db,
            office.tenant,
            department=office.engineering,
            agent_name="Ada",
            question="Darf ich die Migration nachts fahren?",
        )
        await _seat_for(db, office.tenant, "hos", office.sales, SEAT_APPROVER)
    return office


# ----------------------------------------------------------------------- 15


async def test_a_sales_approver_sees_only_his_departments_open_questions(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)

    async with _http() as http:
        got = await http.get(
            "/api/v1/clarifications?status=open",
            headers=_headers(office.tenant, "hos", "member"),
        )
    assert got.status_code == 200, got.text
    rows = got.json()
    assert [r["id"] for r in rows] == [str(office.sales_clar)]
    assert rows[0]["question"] == "Welche Rabattstufe für Gartenholz?"


# ----------------------------------------------------------------------- 16


async def test_a_sales_approver_cannot_answer_an_engineering_clarification(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    """404 -- the same answer as "no such clarification", for the same reason
    the approvals door gives one: an id that answers differently is an oracle."""
    office = await _office(app_session)
    headers = _headers(office.tenant, "hos", "member")

    async with _http() as http:
        refused = await http.post(
            f"/api/v1/clarifications/{office.engineering_clar}/answer",
            json={"answer": "ja, nachts ist ok"},
            headers=headers,
        )
        assert refused.status_code == 404, refused.text

        missing = await http.post(
            f"/api/v1/clarifications/{uuid.uuid4()}/answer",
            json={"answer": "x"},
            headers=headers,
        )
        assert missing.status_code == 404
        assert missing.json() == refused.json()

        # Control: his own is answerable, so a 404 above cannot be the endpoint
        # simply not working.
        allowed = await http.post(
            f"/api/v1/clarifications/{office.sales_clar}/answer",
            json={"answer": "Stufe 2"},
            headers=headers,
        )
        assert allowed.status_code == 200, allowed.text

    async with app_session(office.tenant) as db:
        foreign = await db.get(m.Clarification, office.engineering_clar)
        assert foreign is not None
        assert foreign.status == "open" and foreign.answer is None
        parked = await db.get(m.AgentRun, office.engineering_run)
        assert parked is not None and parked.state == "waiting_for_input"

    assert [rid for rid, _ in queue.enqueued] == [office.sales_run], (
        "answering somebody else's question must not re-queue their run either"
    )


# ----------------------------------------------------------------------- 17


async def test_answering_requeues_the_parked_run(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    """The whole point of the screen: a person answers, and the agent carries on.

    `publish_run` fires AFTER the commit -- a stream entry whose run row is not
    yet visible is a run a worker picks up and cannot find.
    """
    office = await _office(app_session)

    async with _http() as http:
        got = await http.post(
            f"/api/v1/clarifications/{office.sales_clar}/answer",
            json={"answer": "Stufe 2, wie im letzten Quartal"},
            headers=_headers(office.tenant, "hos", "member"),
        )
        assert got.status_code == 200, got.text

    async with app_session(office.tenant) as db:
        clar = await db.get(m.Clarification, office.sales_clar)
        assert clar is not None
        assert clar.status == "answered"
        assert clar.answer == "Stufe 2, wie im letzten Quartal"
        run = await db.get(m.AgentRun, office.sales_run)
        assert run is not None
        assert run.state == "queued"
        # The answer has to be where the agent will actually look for it.
        assert {"question": clar.question, "answer": clar.answer} in run.context["clarifications"]
        assert "pending_question" not in run.context

    assert queue.enqueued == [(office.sales_run, office.tenant)]

    # And it has left the queue, so the screen does not offer it twice.
    async with _http() as http:
        again = await http.get(
            "/api/v1/clarifications?status=open",
            headers=_headers(office.tenant, "hos", "member"),
        )
    assert again.json() == []


async def test_the_admin_answer_door_is_untouched_and_still_refuses_a_seat_holder(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    """Not in §8's list, and cheap insurance: §4 says `POST /runs/{id}/answer`
    stays exactly as it is, gated on `run:control`. If this slice widened it to
    admit a seat, the department term would have a hole beside it that nobody
    was looking at."""
    office = await _office(app_session)

    async with _http() as http:
        seat_holder = await http.post(
            f"/api/v1/runs/{office.sales_run}/answer",
            json={"answer": "Stufe 2"},
            headers=_headers(office.tenant, "hos", "member"),
        )
        assert seat_holder.status_code == 403, seat_holder.text

        admin = await http.post(
            f"/api/v1/runs/{office.engineering_run}/answer",
            json={"answer": "ja"},
            headers=_headers(office.tenant, "op-1", "operator"),
        )
        assert admin.status_code == 200, admin.text


# --------------------------------------------------- who unblocked the run


async def test_answering_names_who_did_it(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    """There was no trail at all.

    `grep -c append_event workspace/queue.py api/v1/clarifications.py` returned
    `0` and `0`: `Clarification` has no `answered_by` column, and neither this
    door nor `POST /runs/{id}/answer` wrote an audit event. A sentence that went
    into a live run and changed what an agent did next was attributable to
    nobody -- while `decide_approval`, one door along, records `decided_by` plus a
    `member_id`/`department_id` resource for exactly this reason. This slice is
    what makes unblocking a run reachable by somebody who is not an
    administrator, so the gap became this slice's.

    The answer TEXT is deliberately NOT in the audit resource: it is the person's
    own words, it is already on the clarification row, and the trail is read by
    roles (`auditor`) that hold no seat in this department.
    """
    office = await _office(app_session)

    async with _http() as http:
        got = await http.post(
            f"/api/v1/clarifications/{office.sales_clar}/answer",
            json={"answer": "Stufe 2, wie im letzten Quartal"},
            headers=_headers(office.tenant, "hos", "member"),
        )
        assert got.status_code == 200, got.text

    async with app_session(office.tenant) as db:
        member = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "hos"))
        ).scalar_one()
        event = (
            await db.execute(
                select(m.AuditEvent).where(m.AuditEvent.action == "clarification.answered")
            )
        ).scalar_one()

    assert event.actor_id == member.id
    assert event.actor_type == "operator"
    assert event.resource["clarification_id"] == str(office.sales_clar)
    assert event.resource["run_id"] == str(office.sales_run)
    assert event.resource["department_id"] == str(office.sales)
    assert event.resource["member_id"] == str(member.id)
    assert event.resource["via"] == "inbox"
    assert "Stufe 2" not in str(event.resource)


async def test_a_refused_answer_leaves_no_audit_line(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    """The append happens after the scope check and after the run state check, so
    a 404 or a 409 must leave nothing behind. An audit trail that records attempts
    somebody was refused is a trail that says a run was unblocked when it was
    not."""
    office = await _office(app_session)

    async with _http() as http:
        outside = await http.post(
            f"/api/v1/clarifications/{office.engineering_clar}/answer",
            json={"answer": "ja"},
            headers=_headers(office.tenant, "hos", "member"),
        )
        assert outside.status_code == 404

    async with app_session(office.tenant) as db:
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "clarification.answered")
                )
            )
            .scalars()
            .all()
        )
    assert events == []


async def test_a_structured_question_is_listed_with_its_options_and_answered_by_number(
    app_session: AppSessionFactory, queue: _FakeQueue
) -> None:
    """Spec §4.1 through the inbox door: the row carries the structured
    questions, and a written answer is read like a messenger reply -- one that
    fits nothing is a 422 and leaves the question open."""
    office = await _office(app_session)
    async with app_session(office.tenant) as db:
        run = await db.get(m.AgentRun, office.sales_run)
        assert run is not None
        run.context = {
            **run.context,
            "pending_questions": [
                {
                    "id": "tier",
                    "prompt": "Rabattstufe?",
                    "kind": "single",
                    "options": [
                        {"key": "s1", "label": "Stufe 1"},
                        {"key": "s2", "label": "Stufe 2"},
                    ],
                }
            ],
        }
    headers = _headers(office.tenant, "hos", "member")

    async with _http() as http:
        rows = (await http.get("/api/v1/clarifications?status=open", headers=headers)).json()
        assert [q["id"] for q in rows[0]["questions"]] == ["tier"]
        assert rows[0]["questions"][0]["options"][1]["label"] == "Stufe 2"

        misfit = await http.post(
            f"/api/v1/clarifications/{office.sales_clar}/answer",
            json={"answer": "irgendwas"},
            headers=headers,
        )
        assert misfit.status_code == 422, misfit.text

        ok = await http.post(
            f"/api/v1/clarifications/{office.sales_clar}/answer",
            json={"answer": "2"},
            headers=headers,
        )
        assert ok.status_code == 200, ok.text

    async with app_session(office.tenant) as db:
        run = await db.get(m.AgentRun, office.sales_run)
        assert run is not None and run.state == "queued"
        assert run.context["clarifications"][-1]["answers"] == {"tier": ["s2"]}
