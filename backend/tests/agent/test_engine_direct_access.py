"""The in-process engine runs the Copilot's direct access: granted tools only,
reads run, every write parks for the person (§3, §6, §10)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.agent.test_engine_parallel_reads import (
    _RecordingServer,
    _ScriptedStream,
    _StubSession,
    _turn,
)
from tests.conftest import AppSessionFactory
from tests.copilot.direct_access_helpers import (
    READ_ONLY,
    READ_WRITE,
    SCOPES,
    DirectOffice,
    chat_run,
    direct_office,
    grant,
)

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.agents.versioning import publish_version
from oc8.connections.direct_access import DIRECT_ACTION_NUDGE
from oc8.modelrouter import ToolCall

pytestmark = pytest.mark.asyncio

TOOLS = ["search_records", "update_record", "get_record"]


class _SpyBus:
    """Every event the run would publish, by kind -- no Redis needed."""

    def __init__(self) -> None:
        self.tenant: list[tuple[str, dict[str, Any]]] = []
        self.member: list[tuple[list[Any], str, dict[str, Any]]] = []

    async def publish_event(
        self, tenant_id: Any, type_: str, data: dict[str, Any], **_kw: Any
    ) -> None:
        self.tenant.append((type_, data))

    async def publish_member_event(
        self, tenant_id: Any, members: list[Any], type_: str, data: dict[str, Any], **_kw: Any
    ) -> None:
        self.member.append((list(members), type_, data))


class _Stream(_ScriptedStream):
    """Scripted turns, remembering what each completion was sent."""

    def __init__(self, script: list[Any]) -> None:
        super().__init__(script)
        self.sent: list[str] = []

    def __call__(self, *args: Any, **kw: Any) -> Any:
        self.sent.append(repr(args) + repr(kw))
        return super().__call__(*args, **kw)


def _patch(
    monkeypatch: pytest.MonkeyPatch, recorder: _RecordingServer, script: list[Any]
) -> tuple[list[str], _SpyBus, _Stream]:
    opened: list[str] = []

    async def _open(*_a: Any, mcp_conn: m.McpConnection, **_kw: Any) -> _StubSession:
        opened.append(mcp_conn.name)
        return _StubSession(recorder, TOOLS)

    bus = _SpyBus()
    stream = _Stream(script)
    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    monkeypatch.setattr("oc8.agent.engine.stream_completion_with_fallback", stream)
    monkeypatch.setattr("oc8.realtime.bus.get_event_bus", lambda: bus)
    return opened, bus, stream


async def _copilot(
    db: AsyncSession,
    office: DirectOffice,
    *,
    autonomy: str = "autonomous",
    parallel: bool = False,
    narrowing: dict[str, Any] | None = None,
) -> m.Agent:
    cfg = m.ModelConfig(
        tenant_id=office.tenant,
        provider="fake",
        model="fake",
        params={"parallel_tool_calls": True} if parallel else {},
    )
    db.add(cfg)
    await db.flush()
    agent = await db.get(m.Agent, office.copilot_id)
    assert agent is not None
    agent.model_config_id = cfg.id
    agent.definition = {**(agent.definition or {}), "autonomy": autonomy}
    await db.flush()
    if narrowing is not None:
        # Runs execute the published version: pin the narrowing there.
        agent.narrowing = narrowing
        await db.flush()
        await publish_version(db, agent, note="narrowed")
    return agent


async def _run(db: AsyncSession, office: DirectOffice, agent: m.Agent, run: m.AgentRun) -> Any:
    return await run_agent(
        db, agent=agent, task_text="find it", tenant_id=office.tenant, run_id=run.id
    )


async def _held(db: AsyncSession) -> m.ApprovalRequest:
    return (
        await db.execute(
            select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "tool_send")
        )
    ).scalar_one()


async def test_a_granted_read_runs_and_a_write_parks_for_the_person(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer(delay_s=0)
    _opened, bus, _stream = _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "XYZ"})),
            _turn("", ToolCall(id="c2", name="update_record", arguments={"id": 7})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        result = await _run(db, office, agent, run)
        held = await _held(db)
    assert result.status == "waiting_for_approval"
    assert [name for name, _s, _e in recorder.windows] == ["search_records"]
    info = held.payload["direct_access"]
    assert info["member_id"] == str(office.member_id) and info["decider"] == "member"
    assert info["session_id"] == str(office.session_id)
    assert held.payload["run_id"] == str(run.id)
    assert held.title.startswith("Copilot on behalf of Anna: ")
    assert "Copilot im Auftrag von Anna" in held.payload["i18n"]["de"]["title"]
    # The person's own card (R21/R22): no department, announced to them alone.
    assert held.department_id is None
    assert [ev for ev in bus.tenant if ev[0] == "approval.created"] == []
    assert [(who, kind) for who, kind, _d in bus.member] == [
        ([office.member_id], "approval.created")
    ]
    traced = [e for e in result.tool_calls if e["tool"] == "search_records"]
    assert traced and traced[0]["actingMember"] == str(office.member_id)
    parked = [e for e in result.tool_calls if e["tool"] == "update_record"]
    assert parked and parked[0]["actingMember"] == str(office.member_id)


async def test_without_a_grant_the_copilot_reaches_nothing(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer(delay_s=0)
    opened, _bus, _stream = _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={})),
            _turn("I cannot."),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
    assert opened == [] and recorder.windows == []
    assert result.status == "done"


async def test_a_followup_turn_never_sees_granted_tools(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer(delay_s=0)
    opened, _bus, _stream = _patch(monkeypatch, recorder, [_turn("ok")])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(
            db, office, door="followup", on_behalf=False, extra={"followup": {"trigger_id": "t"}}
        )
        await _run(db, office, agent, run)
    assert opened == []


async def test_a_department_gate_asks_the_person_first(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A department's own policy only ADDS a gate (§1): the person's card comes
    first, naming the department that decides after them."""
    recorder = _RecordingServer(delay_s=0)
    _opened, bus, _stream = _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="update_record", arguments={"id": 7})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(
            db,
            tenant,
            policy={"enabled": True, "read": True, "modify": True, "approval_actions": ["modify"]},
        )
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        await _run(db, office, agent, run)
        held = await _held(db)
    assert held.department_id is None
    info = held.payload["direct_access"]
    assert info["decider"] == "member" and info["member_id"] == str(office.member_id)
    assert info["department_id"] == str(office.department_id)
    assert held.payload["direct_access"]["session_id"] == str(office.session_id)
    assert held.payload["run_id"] == str(run.id)
    # The person's card: announced to them alone.
    assert [ev for ev in bus.tenant if ev[0] == "approval.created"] == []
    assert [(who, kind) for who, kind, _d in bus.member] == [
        ([office.member_id], "approval.created")
    ]


class _FlakyServer(_RecordingServer):
    """A read that fails for one argument -- a real failing call through the grant."""

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        out = await super().call(name, arguments)
        if arguments.get("q") == "boom":
            raise RuntimeError("the system is down")
        return out


async def test_two_direct_actions_are_counted_and_a_failing_call_keeps_the_grant(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _FlakyServer(delay_s=0)
    _opened, _bus, stream = _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "a"})),
            _turn("", ToolCall(id="c2", name="search_records", arguments={"q": "boom"})),
            _turn("", ToolCall(id="c3", name="search_records", arguments={"q": "b"})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        row = await grant(db, office)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
        await db.refresh(row)
    async with app_session(tenant) as db:
        # Staged audit rows reach the chain on commit.
        audited = (
            (
                await db.execute(
                    select(m.AuditEvent)
                    .where(m.AuditEvent.action == "direct_access.call")
                    .order_by(m.AuditEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    assert [name for name, _s, _e in recorder.windows] == ["search_records"] * 3
    states = [e["state"] for e in result.tool_calls if e["tool"] == "search_records"]
    assert states[1] == "failed"
    assert row.actions == 2 and row.agent_suggested_at is not None and row.revoked_at is None
    assert [a.resource["succeeded"] for a in audited] == [True, False, True]
    # The nudge goes to the model once, after the second successful action.
    assert DIRECT_ACTION_NUDGE not in stream.sent[2]
    assert DIRECT_ACTION_NUDGE in stream.sent[3]


@pytest.mark.parametrize("lost", ["revoked", "seat"])
async def test_access_lost_between_two_calls_blocks_the_very_next_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, lost: str
) -> None:
    """The view is loaded per call, never cached for the run (Review Focus 4)."""
    holder: dict[str, Any] = {}

    class _LosingServer(_RecordingServer):
        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            out = await super().call(name, arguments)
            db: AsyncSession = holder["db"]
            office: DirectOffice = holder["office"]
            if lost == "revoked":
                row: m.DirectAccessGrant = holder["grant"]
                row.revoked_at = dt.datetime.now(dt.UTC)
            else:
                await db.execute(
                    delete(m.OrgMemberDepartment).where(
                        m.OrgMemberDepartment.member_id == office.member_id
                    )
                )
            await db.flush()
            return out

    recorder = _LosingServer(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "a"})),
            _turn("", ToolCall(id="c2", name="search_records", arguments={"q": "b"})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        holder.update(db=db, office=office, grant=await grant(db, office))
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
    assert len(recorder.windows) == 1
    second = [e for e in result.tool_calls if e["tool"] == "search_records"][1]
    assert second["state"] == "denied"


async def test_copilot_direct_reads_stay_sequential_even_with_parallel_caps(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R12: the parallel-read pre-pass is fail-closed for direct reads -- they
    run one at a time through the per-call PEP."""
    recorder = _RecordingServer(delay_s=0.05)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn(
                "",
                ToolCall(id="c1", name="search_records", arguments={"q": "a"}),
                ToolCall(id="c2", name="search_records", arguments={"q": "b"}),
            ),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        row = await grant(db, office)
        agent = await _copilot(db, office, parallel=True)
        await _run(db, office, agent, await chat_run(db, office))
        await db.refresh(row)
    assert len(recorder.windows) == 2
    assert recorder.overlapping_pairs() == 0
    assert row.actions == 2


async def test_the_copilots_own_narrowing_binds_a_direct_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R14: the pinned narrowing reaches the offer and the per-call decision."""
    recorder = _RecordingServer(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="update_record", arguments={"id": 7})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        agent = await _copilot(
            db,
            office,
            narrowing={"tools": {"things": {"enabled": True, "read": True, "modify": False}}},
        )
        result = await _run(db, office, agent, await chat_run(db, office))
        held = (
            (
                await db.execute(
                    select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "tool_send")
                )
            )
            .scalars()
            .all()
        )
    assert recorder.windows == [] and held == []
    assert result.status == "done"
    (entry,) = [e for e in result.tool_calls if e["tool"] == "update_record"]
    assert entry["state"] == "denied"


async def _two_departments(db: AsyncSession, tenant: uuid.UUID) -> tuple[DirectOffice, uuid.UUID]:
    """Anna reads through Sales (the older row) and may also change through
    Ops (read + modify, a newer row)."""
    office = await direct_office(db, tenant, policy=READ_ONLY)
    ops = m.Department(tenant_id=tenant, name="Ops", frame={"tools": {"things": READ_WRITE}})
    db.add(ops)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=office.member_id,
            department_id=ops.id,
            seat_role="dept_viewer",
        )
    )
    db.add(
        m.McpConnection(
            tenant_id=tenant,
            department_id=ops.id,
            name="things",
            transport="stdio",
            server_url="stdio://things-ops",
            connected=True,
            config={"command": "y", "args": []},
            scopes=SCOPES,
            created_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1),
        )
    )
    await db.flush()
    await grant(db, office)
    return office, ops.id


async def test_a_key_opens_on_the_department_that_may_also_change_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R23: read-only Sales and read+modify Ops -- the run opens Ops's row, so
    reads run and a write reaches the person's hold instead of a refusal."""
    recorder = _RecordingServer(delay_s=0)
    opened_rows: list[str] = []

    async def _open(*_a: Any, mcp_conn: m.McpConnection, **_kw: Any) -> _StubSession:
        opened_rows.append(mcp_conn.server_url)
        return _StubSession(recorder, TOOLS)

    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "a"})),
            _turn("", ToolCall(id="c2", name="update_record", arguments={"id": 7})),
            _turn("done"),
        ],
    )
    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office, _ops = await _two_departments(db, tenant)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        result = await _run(db, office, agent, run)
        held = await _held(db)
    assert opened_rows == ["stdio://things-ops"]
    assert [name for name, _s, _e in recorder.windows] == ["search_records"]
    assert result.status == "waiting_for_approval"
    assert held.payload["direct_access"]["decider"] == "member"
    assert held.payload["run_id"] == str(run.id)


async def test_a_seat_lost_in_the_opened_department_refuses_even_a_read(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R17: the opened row is Ops's. Losing that seat mid-run leaves Sales
    granting the read -- but not on Ops's identity: refused, not run there."""
    holder: dict[str, Any] = {}

    class _LosingOps(_RecordingServer):
        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            out = await super().call(name, arguments)
            db: AsyncSession = holder["db"]
            await db.execute(
                delete(m.OrgMemberDepartment).where(
                    m.OrgMemberDepartment.department_id == holder["ops"]
                )
            )
            await db.flush()
            return out

    recorder = _LosingOps(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "a"})),
            _turn("", ToolCall(id="c2", name="search_records", arguments={"q": "b"})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office, ops = await _two_departments(db, tenant)
        holder.update(db=db, ops=ops)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
    assert len(recorder.windows) == 1
    second = [e for e in result.tool_calls if e["tool"] == "search_records"][1]
    assert second["state"] == "denied"
    assert "through another department" in second["reason"]


async def _org_wide_office(db: AsyncSession, tenant: uuid.UUID) -> tuple[DirectOffice, uuid.UUID]:
    """Sales: read-only, its own row. Ops: read + modify, NO row of its own.
    A connected organisation-wide (department-less) row exists."""
    office = await direct_office(db, tenant, policy=READ_ONLY)
    ops = m.Department(tenant_id=tenant, name="Ops", frame={"tools": {"things": READ_WRITE}})
    db.add(ops)
    await db.flush()
    db.add(
        m.OrgMemberDepartment(
            tenant_id=tenant,
            member_id=office.member_id,
            department_id=ops.id,
            seat_role="dept_viewer",
        )
    )
    db.add(
        m.McpConnection(
            tenant_id=tenant,
            department_id=None,
            name="things",
            transport="stdio",
            server_url="stdio://things-org",
            connected=True,
            config={"command": "z", "args": []},
            scopes=SCOPES,
        )
    )
    await db.flush()
    await grant(db, office)
    return office, ops.id


async def test_an_org_wide_row_opened_for_a_rowless_department_reads_and_holds_writes(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ops grants read + modify but has no row: the key opens on the
    organisation-wide row. Ops alone would pick that row for a read and for a
    change, so reads run and the write reaches the person's hold (R17/R23)."""
    recorder = _RecordingServer(delay_s=0)
    opened_rows: list[str] = []

    async def _open(*_a: Any, mcp_conn: m.McpConnection, **_kw: Any) -> _StubSession:
        opened_rows.append(mcp_conn.server_url)
        return _StubSession(recorder, TOOLS)

    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "a"})),
            _turn("", ToolCall(id="c2", name="search_records", arguments={"q": "b"})),
            _turn("", ToolCall(id="c3", name="update_record", arguments={"id": 7})),
            _turn("done"),
        ],
    )
    monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office, _ops = await _org_wide_office(db, tenant)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        result = await _run(db, office, agent, run)
        held = await _held(db)
    assert opened_rows == ["stdio://things-org"]
    assert [name for name, _s, _e in recorder.windows] == ["search_records"] * 2
    assert result.status == "waiting_for_approval"
    assert held.payload["direct_access"]["decider"] == "member"


async def test_an_org_wide_row_is_refused_a_change_once_the_granting_department_has_its_own(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict R17: the read runs on the organisation-wide row (Ops alone picks
    it). Then Ops -- the only department granting modify -- gets a row of its
    own: Ops alone no longer picks the organisation-wide row, so a change on it
    is refused, not held to run there. The next run opens Ops's row."""
    holder: dict[str, Any] = {}

    class _OpsGetsARow(_RecordingServer):
        async def call(self, name: str, arguments: dict[str, Any]) -> str:
            out = await super().call(name, arguments)
            db: AsyncSession = holder["db"]
            db.add(
                m.McpConnection(
                    tenant_id=holder["tenant"],
                    department_id=holder["ops"],
                    name="things",
                    transport="stdio",
                    server_url="stdio://things-ops",
                    connected=True,
                    config={"command": "y", "args": []},
                    scopes=SCOPES,
                )
            )
            await db.flush()
            return out

    recorder = _OpsGetsARow(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "a"})),
            _turn("", ToolCall(id="c2", name="update_record", arguments={"id": 7})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office, ops = await _org_wide_office(db, tenant)
        holder.update(db=db, ops=ops, tenant=tenant)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
        held = (
            (
                await db.execute(
                    select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "tool_send")
                )
            )
            .scalars()
            .all()
        )
    assert len(recorder.windows) == 1 and held == []
    (write,) = [e for e in result.tool_calls if e["tool"] == "update_record"]
    assert write["state"] == "denied"
    assert "through another department" in write["reason"]


async def test_an_elicited_direct_write_parks_for_the_person(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: an answered elicitation's re-run is a direct write like any other --
    decided against a freshly loaded view, held for the person, carrying
    direct_access and run_id."""
    recorder = _RecordingServer(delay_s=0)
    _opened, bus, _stream = _patch(monkeypatch, recorder, [_turn("done")])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(
            db,
            office,
            extra={
                "pending_elicitation": {
                    "connection": "things",
                    "tool": "update_record",
                    "arguments": {"id": 7},
                    "question": "Which customer?",
                },
                "clarifications": [{"question": "Which customer?", "answer": "ACME"}],
            },
        )
        result = await _run(db, office, agent, run)
        held = await _held(db)
    assert result.status == "waiting_for_approval"
    assert recorder.windows == []
    assert held.payload["direct_access"]["decider"] == "member"
    assert held.payload["direct_access"]["session_id"] == str(office.session_id)
    assert held.payload["run_id"] == str(run.id)
    assert held.title.startswith("Copilot on behalf of Anna: ")
    assert held.department_id is None
    assert [ev for ev in bus.tenant if ev[0] == "approval.created"] == []
    assert [who for who, _k, _d in bus.member] == [[office.member_id]]


async def test_an_elicited_department_held_write_needs_both_approvals_and_runs_once(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held-elicitation path raises the same two stages: the person's card,
    then the department's. The answered call runs only after both, once."""
    recorder = _RecordingServer(delay_s=0)
    _patch(monkeypatch, recorder, [_turn("done") for _ in range(6)])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(
            db,
            tenant,
            policy={"enabled": True, "read": True, "modify": True, "approval_actions": ["modify"]},
        )
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(
            db,
            office,
            extra={
                "pending_elicitation": {
                    "connection": "things",
                    "tool": "update_record",
                    "arguments": {"id": 7},
                    "question": "Which customer?",
                },
                "clarifications": [{"question": "Which customer?", "answer": "ACME"}],
            },
        )
        leg1 = await _run(db, office, agent, run)
        assert leg1.status == "waiting_for_approval"
        held = await _held(db)
        assert held.department_id is None
        assert held.payload["direct_access"]["decider"] == "member"
        assert held.payload["direct_access"]["department_id"] == str(office.department_id)
        pre = await _decide_both_stages(db, office, run)
        assert recorder.windows == []
        leg2 = await run_agent(
            db,
            agent=agent,
            task_text="resume",
            tenant_id=office.tenant,
            run_id=run.id,
            pre_decided=pre,
        )
    assert leg2.status == "done"
    assert [name for name, _s, _e in recorder.windows] == ["update_record"]


async def test_an_elicited_direct_write_after_the_grant_was_revoked_is_refused(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: the held re-run is decided against the view loaded NOW, not the
    one the run started with."""
    recorder = _RecordingServer(delay_s=0)
    _patch(monkeypatch, recorder, [_turn("done")])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        row = await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(
            db,
            office,
            extra={
                "pending_elicitation": {
                    "connection": "things",
                    "tool": "search_records",
                    "arguments": {"q": "x"},
                    "question": "Which?",
                },
                "clarifications": [{"question": "Which?", "answer": "ACME"}],
            },
        )

        async def _open_then_revoke(
            *_a: Any, mcp_conn: m.McpConnection, **_kw: Any
        ) -> _StubSession:
            # The run has loaded its first view and opened the connection;
            # the grant goes now, before the held call is decided.
            row.revoked_at = dt.datetime.now(dt.UTC)
            await db.flush()
            return _StubSession(recorder, TOOLS)

        monkeypatch.setattr("oc8.agent.engine._open_mcp_session", _open_then_revoke)
        result = await _run(db, office, agent, run)
    async with app_session(tenant) as db:
        decided = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "tool.call:search_records")
                )
            )
            .scalars()
            .all()
        )
    assert recorder.windows == []
    assert result.status == "done"
    assert [a.decision for a in decided] == ["deny"]
    (entry,) = [e for e in result.tool_calls if e["tool"] == "search_records"]
    assert "offer it with offer_direct_access first" in entry["result"]


async def test_an_elicited_direct_action_can_be_the_one_that_suggests_an_agent(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held re-run counts like any direct call -- and when it is the
    second action, the model is nudged after it as well."""
    recorder = _RecordingServer(delay_s=0)
    _opened, _bus, stream = _patch(monkeypatch, recorder, [_turn("done")])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        row = await grant(db, office)
        row.actions = 1
        await db.flush()
        agent = await _copilot(db, office)
        run = await chat_run(
            db,
            office,
            extra={
                "pending_elicitation": {
                    "connection": "things",
                    "tool": "search_records",
                    "arguments": {"q": "x"},
                    "question": "Which?",
                },
                "clarifications": [{"question": "Which?", "answer": "ACME"}],
            },
        )
        result = await _run(db, office, agent, run)
        await db.refresh(row)
    assert [name for name, _s, _e in recorder.windows] == ["search_records"]
    assert row.actions == 2 and row.agent_suggested_at is not None
    assert DIRECT_ACTION_NUDGE in stream.sent[0]
    (entry,) = [e for e in result.tool_calls if e["tool"] == "search_records"]
    assert entry["actingMember"] == str(office.member_id)


async def test_an_offer_parks_the_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn(
                "",
                ToolCall(
                    id="c1",
                    name="offer_direct_access",
                    arguments={"connection": "things", "purpose": "find the quotation"},
                ),
            ),
            _turn("never reached"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        result = await _run(db, office, agent, run)
        offer = (
            await db.execute(
                select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "direct_access")
            )
        ).scalar_one()
    assert result.status == "waiting_for_approval"
    assert offer.payload["run_id"] == str(run.id)
    assert recorder.windows == []
    (entry,) = [e for e in result.tool_calls if e["tool"] == "offer_direct_access"]
    assert entry["state"] == "awaiting_approval"


def _stub_read_resource(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    reads: list[dict[str, Any]] = []

    async def _read(connection_id: Any, **kw: Any) -> str:
        reads.append({"connection_id": connection_id, **kw})
        return "BODY-OF-RECORD"

    monkeypatch.setattr("oc8.agent.mcp_pool.read_resource", _read)
    return reads


async def test_a_granted_copilot_reads_a_resource_through_the_opened_connection(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _stub_read_resource(monkeypatch)
    _opened, _bus, _stream = _patch(
        monkeypatch,
        _RecordingServer(delay_s=0),
        [
            _turn(
                "",
                ToolCall(
                    id="c1",
                    name="read_resource",
                    arguments={"connection": "things", "uri": "order/115"},
                ),
            ),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
    assert [r["uri"] for r in reads] == ["order/115"]
    assert reads[0]["connection_id"] == office.connection_id
    assert result.status == "done"


async def test_without_a_grant_the_copilot_cannot_read_a_resource(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _stub_read_resource(monkeypatch)
    _patch(
        monkeypatch,
        _RecordingServer(delay_s=0),
        [
            _turn(
                "",
                ToolCall(
                    id="c1",
                    name="read_resource",
                    arguments={"connection": "things", "uri": "order/115"},
                ),
            ),
            _turn("I cannot."),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        agent = await _copilot(db, office)
        await _run(db, office, agent, await chat_run(db, office))
    assert reads == []


async def test_a_granted_read_resource_is_audited_on_behalf_and_counted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_read_resource(monkeypatch)
    _patch(
        monkeypatch,
        _RecordingServer(delay_s=0),
        [
            _turn(
                "",
                ToolCall(
                    id="c1",
                    name="read_resource",
                    arguments={"connection": "things", "uri": "order/115"},
                ),
            ),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        row = await grant(db, office)
        agent = await _copilot(db, office)
        await _run(db, office, agent, await chat_run(db, office))
        await db.refresh(row)
    async with app_session(tenant) as db:
        audited = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "direct_access.call")
                )
            )
            .scalars()
            .all()
        )
    assert row.actions == 1
    assert [(a.resource["tool"], a.resource["succeeded"]) for a in audited] == [
        ("read_resource", True)
    ]
    assert audited[0].resource["on_behalf_of"] == str(office.member_id)
    assert audited[0].resource["connection"] == "things"


async def test_a_denied_read_resource_is_not_audited_as_a_direct_call(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_read_resource(monkeypatch)
    _patch(
        monkeypatch,
        _RecordingServer(delay_s=0),
        [
            _turn(
                "",
                ToolCall(
                    id="c1",
                    name="read_resource",
                    arguments={"connection": "things", "uri": "order/115"},
                ),
            ),
            _turn("no"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await grant(db, office)
        agent = await _copilot(
            db,
            office,
            narrowing={"tools": {"things": {"enabled": True, "read": False, "modify": False}}},
        )
        await _run(db, office, agent, await chat_run(db, office))
    async with app_session(tenant) as db:
        audited = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "direct_access.call")
                )
            )
            .scalars()
            .all()
        )
    assert audited == []


async def _focused(db: AsyncSession, office: DirectOffice) -> None:
    """Give the granted connection a focus spec, so a single-record call has an
    identity and read-before-write applies to it."""
    conn = await db.get(m.McpConnection, office.connection_id)
    assert conn is not None
    conn.config = {
        "command": "x",
        "args": [],
        "focus_spec": {
            "entity_field": "model",
            "id_fields": ["id"],
            "search_tools": ["search_records"],
            "labels": {"sale.order": "Order"},
        },
    }
    conn.scopes = {"read": ["search_records", "get_record"], "modify": ["update_record"]}
    await db.flush()


_ORDER = {"model": "sale.order", "id": 115}


async def test_a_granted_copilot_may_write_a_record_it_read(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(
        monkeypatch,
        _RecordingServer(delay_s=0),
        [
            _turn("", ToolCall(id="c1", name="get_record", arguments=dict(_ORDER))),
            _turn("", ToolCall(id="c2", name="update_record", arguments=dict(_ORDER))),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
        held = await _held(db)
    assert result.status == "waiting_for_approval"
    assert held.payload["direct_access"]["member_id"] == str(office.member_id)


async def test_a_granted_copilot_may_not_write_a_record_it_did_not_read(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c2", name="update_record", arguments=dict(_ORDER))),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
        held = (
            (
                await db.execute(
                    select(m.ApprovalRequest).where(m.ApprovalRequest.action_type == "tool_send")
                )
            )
            .scalars()
            .all()
        )
    assert held == [] and recorder.windows == []
    (entry,) = [e for e in result.tool_calls if e["tool"] == "update_record"]
    assert "have not read" in str(entry["result"])


async def _two_legs(
    db: AsyncSession,
    office: DirectOffice,
    agent: m.Agent,
    *,
    record_approval: bool,
) -> Any:

    return await _two_legs_on(
        db, office, agent, await chat_run(db, office), record_approval=record_approval
    )


async def _two_legs_on(
    db: AsyncSession,
    office: DirectOffice,
    agent: m.Agent,
    run: m.AgentRun,
    *,
    record_approval: bool = True,
) -> Any:
    from oc8.agent.harness.calls import call_sig
    from oc8.runtime.approval_resume import resolve_tool_approval

    leg1 = await _run(db, office, agent, run)
    assert leg1.status == "waiting_for_approval"
    held = await _held(db)
    if record_approval:
        # What the person's approval writes into the run's context.
        await resolve_tool_approval(db, approval=held, decision="approve")
        await db.refresh(run)
    approved = call_sig(ToolCall(id="x", name="update_record", arguments=dict(_ORDER)))
    return await run_agent(
        db,
        agent=agent,
        task_text="resume",
        tenant_id=office.tenant,
        run_id=run.id,
        pre_decided={approved: {"decision": "approve"}},
    )


def _resume_script(*updates: str) -> list[Any]:
    turns = [
        _turn("", ToolCall(id="c1", name="get_record", arguments=dict(_ORDER))),
        _turn("", ToolCall(id="c2", name="update_record", arguments=dict(_ORDER))),
    ]
    turns += [
        _turn("", ToolCall(id=u, name="update_record", arguments=dict(_ORDER))) for u in updates
    ]
    return turns + [_turn("done") for _ in range(8)]


async def test_an_approved_write_runs_once_after_the_run_resumes(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leg 2 starts with a fresh ledger: the read happened in leg 1 and the
    held write passed read-before-write before it parked. The person approved
    this exact call, so read-before-write must not refuse it."""
    recorder = _RecordingServer(delay_s=0)
    _patch(monkeypatch, recorder, _resume_script("c3"))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        result = await _two_legs(db, office, agent, record_approval=True)
    assert [name for name, _s, _e in recorder.windows] == ["get_record", "update_record"]
    assert result.status == "done"


async def test_an_approval_without_proof_does_not_waive_read_before_write(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer(delay_s=0)
    _patch(monkeypatch, recorder, _resume_script("c3"))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        await _two_legs(db, office, agent, record_approval=False)
    assert "update_record" not in [n for n, _s, _e in recorder.windows]


async def test_the_waiver_is_single_use_within_a_leg(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingServer(delay_s=0)
    _patch(monkeypatch, recorder, _resume_script("c3", "c4"))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        await _two_legs(db, office, agent, record_approval=True)
    assert [n for n, _s, _e in recorder.windows].count("update_record") == 1


async def test_a_waiver_is_spent_once_per_approval_across_legs(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leg 2 runs the approved write. In leg 3 the model repeats it without
    reading: the in-memory context of leg 3 is rebuilt, the approval is not."""
    from oc8.agent.harness.calls import call_sig

    recorder = _RecordingServer(delay_s=0)
    _opened, _bus, stream = _patch(monkeypatch, recorder, _resume_script("c3"))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        await _two_legs_on(db, office, agent, run)
        approved = call_sig(ToolCall(id="x", name="update_record", arguments=dict(_ORDER)))
        # Leg 3: a fresh leg for the same run, the approval still on record.
        await db.refresh(run)
        assert [c["sig"] for c in run.context["consumed_tool_approvals"]] == [approved]
        # Leg 3's own script: its first call is the same write, unread.
        stream.script[:] = [
            _turn("", ToolCall(id="c4", name="update_record", arguments=dict(_ORDER))),
            *[_turn("done") for _ in range(8)],
        ]
        leg3 = await run_agent(
            db,
            agent=agent,
            task_text="resume again",
            tenant_id=office.tenant,
            run_id=run.id,
            pre_decided={approved: {"decision": "approve"}},
        )
    (entry,) = [e for e in leg3.tool_calls if e["tool"] == "update_record"]
    assert "have not read" in str(entry["result"])
    assert [n for n, _s, _e in recorder.windows].count("update_record") == 1


async def _decide_both_stages(
    db: AsyncSession, office: DirectOffice, run: m.AgentRun
) -> dict[str, dict[str, Any]]:
    """The person approves their card, then the holding department approves
    the one that raises; returns what the resumed leg is pre-decided with."""
    from tests.copilot.test_direct_access_department_stage import _approver, _human

    from oc8.approvals import decide_approval
    from oc8.runtime.approval_resume import pre_decided_map

    card = await _held(db)
    anna = await _human(db, office.tenant, office.subject, "member")
    first = await decide_approval(db, card, decision="approve", tenant_id=office.tenant, actor=anna)
    assert first.resumed_run_id is None
    await db.refresh(run)
    assert not (run.context or {}).get("resolved_tool_approvals")
    (dept,) = [
        r
        for r in (
            await db.execute(
                select(m.ApprovalRequest).where(
                    m.ApprovalRequest.action_type == "tool_send",
                    m.ApprovalRequest.id != card.id,
                )
            )
        ).scalars()
    ]
    assert dept.status == "pending" and dept.department_id == office.department_id
    hos = await _approver(db, office)
    await decide_approval(db, dept, decision="approve", tenant_id=office.tenant, actor=hos)
    await db.refresh(run)
    return pre_decided_map(run.context["resolved_tool_approvals"])


async def test_a_pdp_hold_leaves_no_proof_so_the_resumed_write_is_refused(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The department holds every write itself, before the B gate runs: the
    approvals are of a call that was never read-checked."""
    recorder = _RecordingServer(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="update_record", arguments=dict(_ORDER))),
            _turn("", ToolCall(id="c2", name="update_record", arguments=dict(_ORDER))),
            *[_turn("done")] * 6,
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(
            db,
            tenant,
            policy={"enabled": True, "read": True, "modify": True, "approval_actions": ["modify"]},
        )
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        leg1 = await _run(db, office, agent, run)
        assert leg1.status == "waiting_for_approval"
        held = await _held(db)
        assert "read_before_write_ok" not in held.payload
        pre = await _decide_both_stages(db, office, run)
        (entry,) = run.context["resolved_tool_approvals"]
        assert "read_before_write_ok" not in entry
        await run_agent(
            db,
            agent=agent,
            task_text="resume",
            tenant_id=office.tenant,
            run_id=run.id,
            pre_decided=pre,
        )
    assert recorder.windows == []


async def test_a_department_held_write_that_was_read_runs_once_after_both_approve(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record was read, then the department's policy held the write: the
    person approves, then the department. Nothing runs before both; the
    resumed leg runs the write once without re-reading (the proof travelled
    to the department's card); the next leg's repeat is asked again."""
    from oc8.agent.harness.calls import call_sig

    recorder = _RecordingServer(delay_s=0)
    _opened, _bus, stream = _patch(monkeypatch, recorder, _resume_script("c3"))
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(
            db,
            tenant,
            policy={"enabled": True, "read": True, "modify": True, "approval_actions": ["modify"]},
        )
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        leg1 = await _run(db, office, agent, run)
        assert leg1.status == "waiting_for_approval"
        held = await _held(db)
        assert held.payload["direct_access"]["decider"] == "member"
        assert held.payload["read_before_write_ok"] is True
        pre = await _decide_both_stages(db, office, run)
        # Nothing ran on the person's approval alone.
        assert [n for n, _s, _e in recorder.windows] == ["get_record"]
        approved = call_sig(ToolCall(id="x", name="update_record", arguments=dict(_ORDER)))
        assert pre == {approved: "approve"}
        (entry,) = run.context["resolved_tool_approvals"]
        assert entry["read_before_write_ok"] is True
        leg2 = await run_agent(
            db,
            agent=agent,
            task_text="resume",
            tenant_id=office.tenant,
            run_id=run.id,
            pre_decided=pre,
        )
        assert leg2.status == "done"
        assert [n for n, _s, _e in recorder.windows] == ["get_record", "update_record"]
        await db.refresh(run)
        assert [c["sig"] for c in run.context["consumed_tool_approvals"]] == [approved]
        stream.script[:] = [
            _turn("", ToolCall(id="c4", name="update_record", arguments=dict(_ORDER))),
            *[_turn("done") for _ in range(8)],
        ]
        leg3 = await run_agent(
            db,
            agent=agent,
            task_text="resume again",
            tenant_id=office.tenant,
            run_id=run.id,
            pre_decided=pre,
        )
    # The approval covered leg 2's run, so the repeat is asked again --
    # never run on it a second time.
    assert leg3.status == "waiting_for_approval"
    assert [n for n, _s, _e in recorder.windows].count("update_record") == 1


async def test_the_repeated_call_is_denied_with_the_reason(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person rejected the held write and said why: the resumed leg's
    repeat of exactly that call is refused with who rejected it and why, so
    the model changes course instead of retrying."""
    from oc8.runtime.approval_resume import Decisions, resolve_tool_approval

    recorder = _RecordingServer(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="get_record", arguments=dict(_ORDER))),
            _turn("", ToolCall(id="c2", name="update_record", arguments=dict(_ORDER))),
            # The resumed leg reads again and retries the very call.
            _turn("", ToolCall(id="c3", name="get_record", arguments=dict(_ORDER))),
            _turn("", ToolCall(id="c4", name="update_record", arguments=dict(_ORDER))),
            *[_turn("done") for _ in range(8)],
        ],
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant)
        await _focused(db, office)
        await grant(db, office)
        agent = await _copilot(db, office)
        run = await chat_run(db, office)
        leg1 = await _run(db, office, agent, run)
        assert leg1.status == "waiting_for_approval"
        ada = m.OrgMember(
            tenant_id=tenant, subject="ada", subject_uuid=uuid.uuid4(), display_name="Ada Admin"
        )
        db.add(ada)
        await db.flush()
        held = await _held(db)
        held.status, held.reason, held.decided_by = "rejected", "wrong customer", ada.id
        await db.flush()
        await resolve_tool_approval(db, approval=held, decision="reject")
        await db.refresh(run)
        leg2 = await run_agent(
            db,
            agent=agent,
            task_text="resume",
            tenant_id=office.tenant,
            run_id=run.id,
            pre_decided=Decisions(run.context),
        )
    (repeat,) = [e for e in leg2.tool_calls if e["tool"] == "update_record"]
    assert repeat["state"] == "denied"
    assert (
        "ERROR from oc8 (update_record): rejected by Ada Admin: wrong customer. "
        "Adjust your approach instead of retrying the same call."
    ) in str(repeat["result"])
    assert [n for n, _s, _e in recorder.windows] == ["get_record", "get_record"]


async def test_a_person_who_may_only_read_lets_the_copilot_load_a_skill(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading a skill only hands the model a procedure: the PEP decides it as
    a read and an early ALLOW, and it reaches no connection. It used to fall
    back to the run's opened connection and count as a direct call, whose
    per-call check judged it a change -- refused for a person who may only
    read, so the Copilot could not load its own skill."""
    from oc8.skills.runtime import load_assigned_skills

    recorder = _RecordingServer(delay_s=0)
    _opened, _bus, stream = _patch(monkeypatch, recorder, [])
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, policy=READ_ONLY)
        await grant(db, office)
        agent = await _copilot(db, office)
        skill = m.Skill(
            id=uuid.uuid4(), tenant_id=tenant, name="Refunds", description="d", author="t"
        )
        db.add(skill)
        await db.flush()
        version = m.SkillVersion(
            id=uuid.uuid4(),
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={
                "oc8_skill": 1,
                "id": "sk-refunds",
                "version": "1.0.0",
                "instruction": "STEP ONE: check the refund policy.",
                "requires": {"tools": [], "kbs": []},
                "guardrails": [],
            },
            artifact_hash=b"\x00" * 32,
        )
        db.add(version)
        await db.flush()
        db.add(
            m.SkillAssignment(
                id=uuid.uuid4(),
                tenant_id=tenant,
                agent_id=agent.id,
                skill_version_id=version.id,
                enabled=True,
            )
        )
        await db.flush()
        (loaded,) = await load_assigned_skills(db, agent=agent, tenant_id=tenant)
        stream.script.extend(
            [_turn("", ToolCall(id="c1", name=loaded.tool_name, arguments={})), _turn("done")]
        )
        run = await chat_run(db, office)
        result = await _run(db, office, agent, run)
    # Read back once the run's session committed, like every audit test.
    async with app_session(tenant) as db:
        events = list(
            (
                await db.execute(
                    select(m.AuditEvent)
                    .where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action.in_(
                            [f"tool.call:{loaded.tool_name}", f"tool.result:{loaded.tool_name}"]
                        ),
                    )
                    .order_by(m.AuditEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    assert result.status == "done"
    (entry,) = [e for e in result.tool_calls if e["tool"] == loaded.tool_name]
    assert "STEP ONE" in entry["result"], entry
    # Not on the person's behalf: no connection was reached.
    assert "actingMember" not in entry
    call, outcome = events
    assert call.action == f"tool.call:{loaded.tool_name}"
    assert call.decision == "allow"
    assert "basis" not in call.resource
    assert outcome.action == f"tool.result:{loaded.tool_name}"
    assert outcome.resource["outcome"] == "ok"
    assert recorder.windows == []


async def test_a_direct_call_over_the_departments_usage_limit_is_refused(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through a grant, the department's policy decides the call -- and its
    usage limit counts it, against the Copilot."""
    recorder = _RecordingServer(delay_s=0)
    _patch(
        monkeypatch,
        recorder,
        [
            _turn("", ToolCall(id="c1", name="search_records", arguments={"q": "a"})),
            _turn("", ToolCall(id="c2", name="search_records", arguments={"q": "b"})),
            _turn("done"),
        ],
    )
    tenant = uuid.uuid4()
    limited = {**READ_WRITE, "limits": [{"tools": ["search_records"], "max": 1}]}
    async with app_session(tenant) as db:
        office = await direct_office(db, tenant, policy=limited)
        await grant(db, office)
        agent = await _copilot(db, office)
        result = await _run(db, office, agent, await chat_run(db, office))
    assert [name for name, _s, _e in recorder.windows] == ["search_records"]
    refused = [e for e in result.tool_calls if e["tool"] == "search_records"][1]
    assert "Daily limit of 1 reached for search_records" in str(refused["result"])
