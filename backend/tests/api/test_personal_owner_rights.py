"""A `member` who owns a personal department runs and ships their own agent.

The owner of a personal department is usually a plain `member`, a role that
holds no `run:start`, `run:view`, `run:control`, `agent_version:view`,
`agent_version:publish`, `trigger:*` or `skill:*` tenant-wide. Their seat in
their own personal department carries exactly those, for that department
alone (`authz.permissions.PERSONAL_OWNER_PERMISSIONS`): the agent page works
end to end for the owner, and nothing changes for the same member anywhere
else -- a standard department (seated there or not), another member's
personal department, or a member with no personal department at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.authz.permissions import SEAT_APPROVER, SEAT_VIEWER
from oc8.runtime.states import RunState
from tests.api.test_personal_agent_actions import _FakeQueue
from tests.api.test_personal_agent_content import _headers, _http, _subject_uuid, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()
    monkeypatch.setattr("oc8.runtime.intake.get_run_queue", lambda: fake)
    return fake


@dataclass(frozen=True)
class _Agent:
    """One agent with published v1 and v2, a dirty draft, a parked run with an
    open question, an autonomous (scheduled) running run, a trigger and a
    skill assignment."""

    id: uuid.UUID
    waiting_run: uuid.UUID
    running_run: uuid.UUID
    trigger: uuid.UUID
    assignment: uuid.UUID


@dataclass(frozen=True)
class _Office:
    tenant: uuid.UUID
    standard_department: uuid.UUID
    mine: _Agent
    standard: _Agent
    theirs: _Agent


async def _equip(app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID) -> _Agent:
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        await publish_version(db, agent)
        agent.mission = "The mission of v2."
        await publish_version(db, agent)
        agent.mission = "A changed mission, not yet published."
        waiting = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent_id,
            state=RunState.WAITING_FOR_INPUT.value,
            context={"task": "read the mail"},
        )
        running = m.AgentRun(
            tenant_id=tenant,
            agent_id=agent_id,
            state=RunState.RUNNING.value,
            source="cron",
            context={},
        )
        db.add_all([waiting, running])
        await db.flush()
        db.add(
            m.Clarification(
                tenant_id=tenant, run_id=waiting.id, agent_id=agent_id, question="Which?"
            )
        )
        trigger = m.Trigger(
            tenant_id=tenant,
            agent_id=agent_id,
            kind="cron",
            cron_expression="0 7 * * *",
            task_text="Morning mail",
        )
        db.add(trigger)
        skill = m.Skill(tenant_id=tenant, name=f"skill-{agent_id.hex[:8]}")
        db.add(skill)
        await db.flush()
        version = m.SkillVersion(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={"instructions": "Be kind."},
            artifact_hash=b"\x00" * 32,
        )
        db.add(version)
        await db.flush()
        assignment = m.SkillAssignment(
            tenant_id=tenant, agent_id=agent_id, skill_version_id=version.id
        )
        db.add(assignment)
        await db.flush()
        return _Agent(
            id=agent_id,
            waiting_run=waiting.id,
            running_run=running.id,
            trigger=trigger.id,
            assignment=assignment.id,
        )


async def _office(app_session: AppSessionFactory) -> _Office:
    """`_world` (owner's Pia, the standard Nora), plus `other`'s own personal
    department with Otto, and `stranger`, a member with none."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        other = m.OrgMember(
            tenant_id=w.tenant,
            subject="other",
            subject_uuid=_subject_uuid("other"),
            display_name="Other",
        )
        stranger = m.OrgMember(
            tenant_id=w.tenant,
            subject="stranger",
            subject_uuid=_subject_uuid("stranger"),
            display_name="Stranger",
        )
        db.add_all([other, stranger])
        await db.flush()
        theirs = m.Department(
            tenant_id=w.tenant,
            name="Other",
            frame={"closed": True},
            kind="personal",
            owner_member_id=other.id,
        )
        db.add(theirs)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=w.tenant,
                member_id=other.id,
                department_id=theirs.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            )
        )
        otto = m.Agent(tenant_id=w.tenant, department_id=theirs.id, name="Otto", role_title="Mail")
        db.add(otto)
        await db.flush()
        nora = await db.get(m.Agent, w.standard_agent)
        assert nora is not None and nora.department_id is not None
        standard_department = nora.department_id
        otto_id = otto.id
    return _Office(
        tenant=w.tenant,
        standard_department=standard_department,
        mine=await _equip(app_session, w.tenant, w.personal_agent),
        standard=await _equip(app_session, w.tenant, w.standard_agent),
        theirs=await _equip(app_session, w.tenant, otto_id),
    )


#: Every call the agent page (and its Live and History tabs) makes that needs
#: more than `agent:view`, as `(name, call)`. Each answers 2xx for the owner.
_Call = Callable[[AsyncClient, _Agent, dict[str, str]], Awaitable[Response]]


def _calls() -> list[tuple[str, _Call]]:
    def get(path: str) -> _Call:
        async def call(http: AsyncClient, a: _Agent, h: dict[str, str]) -> Response:
            return await http.get(f"/api/v1{path.format(a=a)}", headers=h)

        return call

    def post(path: str, body: dict[str, Any] | None = None) -> _Call:
        async def call(http: AsyncClient, a: _Agent, h: dict[str, str]) -> Response:
            return await http.post(f"/api/v1{path.format(a=a)}", json=body, headers=h)

        return call

    async def patch_trigger(http: AsyncClient, a: _Agent, h: dict[str, str]) -> Response:
        return await http.patch(f"/api/v1/triggers/{a.trigger}", json={"enabled": False}, headers=h)

    async def delete_trigger(http: AsyncClient, a: _Agent, h: dict[str, str]) -> Response:
        return await http.delete(f"/api/v1/triggers/{a.trigger}", headers=h)

    async def unassign_skill(http: AsyncClient, a: _Agent, h: dict[str, str]) -> Response:
        return await http.delete(f"/api/v1/agents/{a.id}/skills/{a.assignment}", headers=h)

    return [
        ("run start", post("/agents/{a.id}/run", {"task": "Read my mail"})),
        ("versions list", get("/agents/{a.id}/versions?limit=20&offset=0")),
        ("version", get("/agents/{a.id}/versions/1")),
        ("version diff", get("/agents/{a.id}/versions/diff?from=1")),
        ("draft status", get("/agents/{a.id}/draft-status")),
        ("publish", post("/agents/{a.id}/versions", {"expectedCurrentVersionNo": 2})),
        ("rollback", post("/agents/{a.id}/versions/1/rollback")),
        ("history", get("/agents/{a.id}/runs?limit=20&offset=0")),
        ("run", get("/runs/{a.waiting_run}")),
        ("output files", get("/agents/{a.id}/workspace/files")),
        ("answer", post("/runs/{a.waiting_run}/answer", {"answer": "The landlord"})),
        ("message", post("/runs/{a.running_run}/message", {"body": "Be brief"})),
        ("steering", get("/runs/{a.running_run}/steering")),
        ("cancel", post("/runs/{a.running_run}/cancel")),
        ("triggers", get("/agents/{a.id}/triggers")),
        (
            "trigger create",
            post(
                "/agents/{a.id}/triggers",
                {"kind": "cron", "cronExpression": "0 8 * * *", "taskText": "Evening mail"},
            ),
        ),
        ("trigger update", patch_trigger),
        ("trigger delete", delete_trigger),
        ("skills", get("/agents/{a.id}/skills")),
        ("skill unassign", unassign_skill),
    ]


_CALLS = _calls()
_IDS = [name for name, _call in _CALLS]


@pytest.mark.parametrize("call", [c for _n, c in _CALLS], ids=_IDS)
async def test_a_member_owner_uses_their_own_agent(
    app_session: AppSessionFactory, fake_queue: _FakeQueue, call: _Call
) -> None:
    o = await _office(app_session)
    async with _http() as http:
        r = await call(http, o.mine, _headers(o.tenant, "owner", "member"))
    assert 200 <= r.status_code < 300, r.text


async def test_a_member_owner_starts_a_queued_run(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    o = await _office(app_session)
    async with _http() as http:
        r = await http.post(
            f"/api/v1/agents/{o.mine.id}/run",
            json={"task": "Read my mail"},
            headers=_headers(o.tenant, "owner", "member"),
        )
    assert r.status_code == 200, r.text
    run_id = uuid.UUID(r.json()["id"])
    assert fake_queue.enqueued == [run_id]
    async with app_session(o.tenant) as db:
        run = await db.get(m.AgentRun, run_id)
    assert run is not None and run.agent_id == o.mine.id
    assert run.state == RunState.QUEUED.value


async def test_a_member_owner_publishes_and_rolls_back(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    o = await _office(app_session)
    h = _headers(o.tenant, "owner", "member")
    async with _http() as http:
        published = await http.post(
            f"/api/v1/agents/{o.mine.id}/versions",
            json={"expectedCurrentVersionNo": 2, "note": "new mission"},
            headers=h,
        )
        assert published.status_code in (200, 201), published.text
        assert published.json()["versionNo"] == 3
        status = await http.get(f"/api/v1/agents/{o.mine.id}/draft-status", headers=h)
        assert status.status_code == 200, status.text
        rolled = await http.post(f"/api/v1/agents/{o.mine.id}/versions/1/rollback", headers=h)
        assert rolled.status_code in (200, 201), rolled.text
        assert rolled.json()["versionNo"] == 4
        versions = await http.get(f"/api/v1/agents/{o.mine.id}/versions", headers=h)
    assert versions.status_code == 200, versions.text


async def test_a_member_owner_lists_their_runs_and_nobody_elses(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    """`GET /runs`: the owner's seat reaches their own agent's runs, and the
    run rule still hides every other agent's."""
    o = await _office(app_session)
    async with _http() as http:
        r = await http.get("/api/v1/runs", headers=_headers(o.tenant, "owner", "member"))
    assert r.status_code == 200, r.text
    agents = {row["agentId"] for row in r.json()}
    assert agents == {str(o.mine.id)}


async def _refused_everywhere_else(
    app_session: AppSessionFactory, o: _Office, subject: str, targets: list[_Agent]
) -> None:
    h = _headers(o.tenant, subject, "member")
    async with _http() as http:
        for target in targets:
            for name, call in _CALLS:
                r = await call(http, target, h)
                assert r.status_code in (403, 404), (subject, name, r.status_code, r.text)
    async with app_session(o.tenant) as db:
        runs = await db.scalar(select(func.count()).select_from(m.AgentRun))
        versions = await db.scalar(select(func.count()).select_from(m.AgentVersion))
        triggers = await db.scalar(select(func.count()).select_from(m.Trigger))
        assignments = await db.scalar(select(func.count()).select_from(m.SkillAssignment))
        messages = await db.scalar(select(func.count()).select_from(m.RunMessage))
    # Nothing was started, published, scheduled, unassigned or sent (`_world`
    # gives Pia one trigger of its own).
    assert (runs, versions, triggers, assignments, messages) == (6, 6, 4, 3, 0)


async def test_the_owner_gains_nothing_outside_their_personal_department(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    """A standard department they hold no seat in, and another member's
    personal department: every call is still refused."""
    o = await _office(app_session)
    await _refused_everywhere_else(app_session, o, "owner", [o.standard, o.theirs])
    assert fake_queue.enqueued == []


async def test_the_owner_gains_nothing_where_they_hold_a_plain_seat(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    """A viewer seat in a standard department shows its agents; owning a
    personal department elsewhere must not turn it into run, version, trigger
    or skill rights there."""
    o = await _office(app_session)
    async with app_session(o.tenant) as db:
        owner = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "owner"))
        ).scalar_one()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=o.tenant,
                member_id=owner.id,
                department_id=o.standard_department,
                seat_role=SEAT_VIEWER,
                agent_manage=True,
            )
        )
    await _refused_everywhere_else(app_session, o, "owner", [o.standard])
    assert fake_queue.enqueued == []


async def test_a_member_without_a_personal_department_gains_nothing(
    app_session: AppSessionFactory, fake_queue: _FakeQueue
) -> None:
    o = await _office(app_session)
    await _refused_everywhere_else(app_session, o, "stranger", [o.mine, o.standard, o.theirs])
    assert fake_queue.enqueued == []


async def test_the_governance_seat_names_what_the_owner_holds_there(
    app_session: AppSessionFactory,
) -> None:
    """The screen gates Run now and Publish per department: the owner's seat
    lists what it carries in their personal department, every other seat
    nothing beyond its role."""
    o = await _office(app_session)
    async with _http() as http:
        r = await http.get("/api/v1/governance", headers=_headers(o.tenant, "owner", "member"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "run:start" not in body["callerPermissions"]
    [seat] = body["seats"]
    assert {"run:start", "agent_version:view", "agent_version:publish"} <= set(
        seat["ownerPermissions"]
    )
