"""What a personal agent produced, at run time and in the trail (spec A4).

Two halves of the same rule. An agent reads another run's file with
`read_run_file`: a file of a personal department's run is read only by an
agent of that same department, every other agent gets the tool's plain "no
such file" (no oracle). And what a personal agent looks up or forgets is not
written into the rows an administrator reads -- the activity line of
`search_memory` / `search_knowledge`, the `knowledge.searched` audit query, the
`agent.memory.deleted` snippet -- while the audit reader strips an event's
`reason` too, as a backstop beside the content keys. A standard agent's rows
stay exactly as they were.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from tests.api.test_personal_agent_content import _headers, _http
from tests.api.test_personal_agent_content import _world as _api_world
from tests.audit.helpers import flush_audit
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.agent.control_tools import (
    SEARCH_KNOWLEDGE,
    SEARCH_MEMORY,
    execute_control_tool,
)
from oc8.audit.chain import append_event
from oc8.authz.pdp import Decision, Effect
from oc8.authz.permissions import SEAT_APPROVER
from oc8.modelrouter import ToolCall

pytestmark = pytest.mark.asyncio

_SECRET_FILE = "Dear landlord, about the rent dispute ..."
_QUERY = "rent dispute with the landlord"
_RECALLED = "The landlord promised to fix the heating by March."


@dataclass(frozen=True)
class _Seat:
    agent: m.Agent
    task: m.Task
    run: m.AgentRun


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    #: The owner's personal agent whose run produced the file.
    pia: _Seat
    #: A second agent in the same personal department.
    paul: _Seat
    #: An agent of a standard department.
    nora: _Seat
    #: An agent of another member's personal department.
    olga: _Seat


async def _seat(db: Any, tenant: uuid.UUID, dept: m.Department, name: str) -> _Seat:
    agent = m.Agent(
        tenant_id=tenant,
        department_id=dept.id,
        name=name,
        status="running",
        definition={},
        presentation={},
        narrowing={},
    )
    db.add(agent)
    await db.flush()
    task = m.Task(
        tenant_id=tenant,
        department_id=dept.id,
        assigned_agent_id=agent.id,
        title="Work",
        state="in_progress",
        delegation_depth=0,
    )
    run = m.AgentRun(tenant_id=tenant, agent_id=agent.id, state="running")
    db.add_all([task, run])
    await db.flush()
    return _Seat(agent=agent, task=task, run=run)


async def _personal(db: Any, tenant: uuid.UUID, subject: str) -> m.Department:
    member = m.OrgMember(
        tenant_id=tenant,
        subject=subject,
        subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}"),
        display_name=subject.title(),
    )
    db.add(member)
    await db.flush()
    dept = m.Department(
        tenant_id=tenant,
        name=subject.title(),
        frame={"closed": True},
        kind="personal",
        owner_member_id=member.id,
    )
    db.add(dept)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=member.id,
            department_id=dept.id,
            seat_role=SEAT_APPROVER,
            agent_manage=True,
        )
    )
    await db.flush()
    return dept


async def _world(db: Any, tenant: uuid.UUID) -> _World:
    mine = await _personal(db, tenant, "owner")
    theirs = await _personal(db, tenant, "colleague")
    shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
    db.add(shared)
    await db.flush()
    return _World(
        tenant=tenant,
        pia=await _seat(db, tenant, mine, "Pia"),
        paul=await _seat(db, tenant, mine, "Paul"),
        nora=await _seat(db, tenant, shared, "Nora"),
        olga=await _seat(db, tenant, theirs, "Olga"),
    )


def _file(tenant: uuid.UUID, run: m.AgentRun, filename: str, text: str) -> m.FileAttachment:
    return m.FileAttachment(
        tenant_id=tenant,
        owner_type="agent_run",
        owner_id=run.id,
        bucket_key=f"run-{uuid.uuid4().hex}",
        filename=filename,
        content_type="text/plain",
        size_bytes=len(text),
        extracted_text=text,
        is_image=False,
    )


async def _call(db: Any, tenant: uuid.UUID, seat: _Seat, name: str, args: dict[str, Any]) -> str:
    outcome = await execute_control_tool(
        db,
        tenant_id=tenant,
        agent=seat.agent,
        task=seat.task,
        tc=ToolCall(id="1", name=name, arguments=args),
        decision=Decision(Effect.ALLOW),
        assigned_skills=[],
        active_skills=[],
        mcp_conn=None,
        originating_operator=None,
        run_id=seat.run.id,
    )
    assert outcome is not None
    return outcome.output


# ------------------------------------------------------------------ read_run_file


@pytest.mark.parametrize("reader", ["nora", "olga"])
@pytest.mark.parametrize("with_run_id", [False, True])
async def test_another_departments_agent_cannot_read_a_personal_run_file(
    app_session: AppSessionFactory, reader: str, with_run_id: bool
) -> None:
    """A standard department's agent and another member's personal agent both
    get the plain miss, with or without the producing run's id."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        db.add(_file(tenant, w.pia.run, "letter.txt", _SECRET_FILE))
        await db.flush()
        args: dict[str, Any] = {"filename": "letter.txt"}
        if with_run_id:
            args["run_id"] = str(w.pia.run.id)
        out = await _call(db, tenant, getattr(w, reader), "read_run_file", args)
    assert out == "ERROR: no such file: letter.txt"
    assert _SECRET_FILE not in out


async def test_the_owners_personal_agent_reads_its_own_run_file(
    app_session: AppSessionFactory,
) -> None:
    """Pia reads what her own run wrote, and Paul -- another agent of the same
    personal department -- reads it too."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        db.add(_file(tenant, w.pia.run, "letter.txt", _SECRET_FILE))
        await db.flush()
        own = await _call(db, tenant, w.pia, "read_run_file", {"filename": "letter.txt"})
        sibling = await _call(
            db,
            tenant,
            w.paul,
            "read_run_file",
            {"filename": "letter.txt", "run_id": str(w.pia.run.id)},
        )
    assert _SECRET_FILE in own
    assert _SECRET_FILE in sibling


async def test_a_hidden_newer_file_does_not_shadow_a_readable_older_one(
    app_session: AppSessionFactory,
) -> None:
    """Filenames are not unique, and the fallback picks the newest match: a
    personal run's newer `report.txt` must not turn the standard run's older
    one into a miss for Nora -- the refusal sits in the statement."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        producer = await _seat(
            db,
            tenant,
            await db.get(m.Department, w.nora.agent.department_id),
            "Nils",
        )
        db.add(_file(tenant, producer.run, "report.txt", "Q3 numbers look good."))
        await db.flush()
        db.add(_file(tenant, w.pia.run, "report.txt", _SECRET_FILE))
        await db.flush()
        out = await _call(db, tenant, w.nora, "read_run_file", {"filename": "report.txt"})
        # A personal agent still reads a standard run's file.
        pia_out = await _call(
            db,
            tenant,
            w.pia,
            "read_run_file",
            {"filename": "report.txt", "run_id": str(producer.run.id)},
        )
    assert "Q3 numbers look good." in out
    assert _SECRET_FILE not in out
    assert "Q3 numbers look good." in pia_out


# ------------------------------------------------------------------ activity and audit


def _fake_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_retrieve_context(*_a: Any, **_kw: Any) -> str:
        return _RECALLED

    monkeypatch.setattr("oc8.agent.control_tools.retrieve_context", fake_retrieve_context)


def _fake_knowledge(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_retrieve_kb_context(*_a: Any, **_kw: Any) -> tuple[str, bool]:
        return _RECALLED, False

    monkeypatch.setattr("oc8.agent.control_tools.retrieve_kb_context", fake_retrieve_kb_context)


async def _activity(db: Any, agent: m.Agent) -> list[m.ActivityEvent]:
    return list(
        (await db.execute(select(m.ActivityEvent).where(m.ActivityEvent.agent_id == agent.id)))
        .scalars()
        .all()
    )


async def _audit(db: Any, action: str) -> list[m.AuditEvent]:
    await flush_audit(db)
    return list(
        (await db.execute(select(m.AuditEvent).where(m.AuditEvent.action == action)))
        .scalars()
        .all()
    )


async def test_search_memory_by_a_personal_agent_writes_no_recalled_text(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_recall(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        out = await _call(db, tenant, w.pia, SEARCH_MEMORY.name, {"query": _QUERY})
        rows = await _activity(db, w.pia.agent)
    # The agent itself still gets what it recalled.
    assert out == _RECALLED
    assert len(rows) == 1
    assert not rows[0].detail
    assert _QUERY not in rows[0].message
    assert _RECALLED not in rows[0].message


async def test_search_knowledge_by_a_personal_agent_writes_no_query(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_knowledge(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        out = await _call(db, tenant, w.pia, SEARCH_KNOWLEDGE.name, {"query": _QUERY})
        rows = await _activity(db, w.pia.agent)
        audit = await _audit(db, "knowledge.searched")
    assert _RECALLED in out
    assert len(rows) == 1
    assert _QUERY not in rows[0].message
    assert not rows[0].detail
    assert len(audit) == 1
    resource = audit[0].resource
    assert "query" not in resource
    assert _QUERY not in str(resource)
    # The metadata stays: which agent, which task, whether anything was found.
    assert resource["agent_id"] == str(w.pia.agent.id)
    assert resource["task_id"] == str(w.pia.task.id)
    assert resource["found"] is True


async def test_a_standard_agents_rows_stay_exactly_as_they_were(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard changes nothing for an agent of a standard department."""
    _fake_recall(monkeypatch)
    _fake_knowledge(monkeypatch)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        w = await _world(db, tenant)
        await _call(db, tenant, w.nora, SEARCH_MEMORY.name, {"query": _QUERY})
        await _call(db, tenant, w.nora, SEARCH_KNOWLEDGE.name, {"query": _QUERY})
        rows = await _activity(db, w.nora.agent)
        audit = await _audit(db, "knowledge.searched")
    by_message = {r.message: r.detail for r in rows}
    assert by_message == {
        f"Erinnert sich an: {_QUERY}": _RECALLED,
        f"Schlägt nach: {_QUERY}": _RECALLED,
    }
    assert len(audit) == 1
    assert audit[0].resource == {
        "agent_id": str(w.nora.agent.id),
        "task_id": str(w.nora.task.id),
        "query": _QUERY,
        "found": True,
        "narrowed_to": [],
        "released": [],
    }


async def _memory(app_session: AppSessionFactory, tenant: uuid.UUID, agent: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        store = m.MemoryStore(tenant_id=tenant, tier="agent", owner_id=agent)
        db.add(store)
        await db.flush()
        record = m.MemoryRecord(
            tenant_id=tenant, store_id=store.id, content=_RECALLED, written_by=agent
        )
        db.add(record)
        await db.flush()
        return record.id


async def _deleted_events(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[m.AuditEvent]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "agent.memory.deleted")
                )
            )
            .scalars()
            .all()
        )


async def test_deleting_a_personal_memory_records_no_snippet(
    app_session: AppSessionFactory,
) -> None:
    """The row itself carries no snippet -- not only the reader's view of it --
    so no export path, present or future, can repeat it."""
    w = await _api_world(app_session)
    record = await _memory(app_session, w.tenant, w.personal_agent)
    async with _http() as http:
        r = await http.delete(
            f"/api/v1/agents/{w.personal_agent}/memory/{record}",
            headers=_headers(w.tenant, "owner", "member"),
        )
    assert r.status_code == 204, r.text
    events = await _deleted_events(app_session, w.tenant)
    assert len(events) == 1
    assert "content_snippet" not in events[0].resource
    assert _RECALLED not in str(events[0].resource)
    assert events[0].resource["record_id"] == str(record)


async def test_deleting_a_standard_memory_keeps_its_snippet(
    app_session: AppSessionFactory,
) -> None:
    w = await _api_world(app_session)
    record = await _memory(app_session, w.tenant, w.standard_agent)
    async with _http() as http:
        r = await http.delete(
            f"/api/v1/agents/{w.standard_agent}/memory/{record}",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 204, r.text
    events = await _deleted_events(app_session, w.tenant)
    assert [e.resource["content_snippet"] for e in events] == [_RECALLED]


async def test_audit_reason_is_stripped_for_non_owners(app_session: AppSessionFactory) -> None:
    """A `reason` is free text -- a denial can quote what the agent tried. For a
    personal agent's event only its owner reads it; a standard agent's event
    keeps it for everybody."""
    w = await _api_world(app_session)
    reason = "denied: wanted to forward the divorce papers"
    async with app_session(w.tenant) as db:
        for agent in (w.personal_agent, w.standard_agent):
            await append_event(
                db,
                tenant_id=w.tenant,
                actor_type="agent",
                actor_id=agent,
                category="tool_action",
                action="tool.denied",
                resource={"tool": "mail_send"},
                decision="deny",
                reason=reason,
            )
    async with _http() as http:
        admin = await http.get(
            "/api/v1/audit?action=tool.denied",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
        export = await http.get(
            "/api/v1/audit/export?format=csv&action=tool.denied",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
        owner = await http.get(
            "/api/v1/audit?action=tool.denied",
            headers=_headers(w.tenant, "owner", "org_admin"),
        )
    assert admin.status_code == 200, admin.text
    by_actor = {e["actorId"]: e for e in admin.json()["events"]}
    assert by_actor[str(w.personal_agent)]["reason"] is None
    assert by_actor[str(w.personal_agent)]["decision"] == "deny"
    assert by_actor[str(w.standard_agent)]["reason"] == reason
    assert export.status_code == 200, export.text
    assert export.text.count(reason) == 1
    owner_by_actor = {e["actorId"]: e for e in owner.json()["events"]}
    assert owner_by_actor[str(w.personal_agent)]["reason"] == reason
