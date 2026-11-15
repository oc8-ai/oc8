"""The closed Copilot operations `board.create` and `board.update`.

A board's department is always its producing agent's; a spec update is additive
and never touches records; `restoreVersion` is the undo (the old blocks over the
current datasets). Every refusal is a value-free `InvalidOperation`.
"""

from __future__ import annotations

import copy
import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from oc8 import models as m
from oc8.auth import Principal
from oc8.boards.records import write_batch
from oc8.boards.spec import parse_board_spec
from oc8.copilot.builder import _texts
from oc8.copilot.builder_ops import BuilderInvalid, parse_builder_operations
from oc8.copilot.capabilities import (
    BoardCreate,
    InvalidOperation,
    apply_operation,
    parse_operations,
    target_revision,
)
from oc8.copilot.models import CopilotOperation, CopilotProposal
from oc8.copilot.proposals import ProposalRejected, apply_proposal, create_proposal
from oc8.edition.tools import tenant_has_capability, use_edition_tools
from oc8.licensing.capabilities import ENTERPRISE_CANVAS, Capability
from oc8.licensing.contracts import Entitlements
from tests.conftest import AppSessionFactory

_SPEC: dict[str, Any] = {
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
_NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.UTC)
#: Planted in submitted keys and values; must never come back in a refusal.
_MARKER = "zz_hostile_marker"


def _norm(spec: dict[str, Any]) -> dict[str, Any]:
    """A spec in the normalized form the operations store."""
    return parse_board_spec(spec).model_dump(mode="json")


def _with_cost(spec: dict[str, Any]) -> dict[str, Any]:
    """`spec` plus a field `cost` and a chart over it: an additive change."""
    out = copy.deepcopy(spec)
    out["datasets"][0]["fields"].append(
        {"key": "cost", "label": "Cost", "type": "money", "currency": "EUR"}
    )
    out["blocks"].append({"type": "line", "dataset": "sales", "fields": ["amount", "cost"]})
    return out


class _Seed:
    tenant: uuid.UUID
    dept: uuid.UUID
    agent: uuid.UUID
    board: uuid.UUID


async def _seed(app_session: AppSessionFactory, *, records: bool = False) -> _Seed:
    s = _Seed()
    s.tenant = uuid.uuid4()
    async with app_session(s.tenant) as db:
        dept = m.Department(tenant_id=s.tenant, name="Finance", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=s.tenant, department_id=dept.id, name="Reporter", definition={})
        db.add(agent)
        await db.flush()
        board = m.DataBoard(
            tenant_id=s.tenant,
            department_id=dept.id,
            agent_id=agent.id,
            title="Revenue",
            spec=_SPEC,
        )
        db.add(board)
        await db.flush()
        if records:
            await write_batch(
                db,
                tenant_id=s.tenant,
                agent=agent,
                board_id=board.id,
                dataset="sales",
                as_of="2026-03-01",
                rows=[{"amount": 1}, {"amount": 2}],
                note=None,
                run_id=None,
                now=_NOW,
            )
        s.dept, s.agent, s.board = dept.id, agent.id, board.id
    return s


async def _board(app_session: AppSessionFactory, s: _Seed) -> m.DataBoard:
    async with app_session(s.tenant) as db:
        return (await db.execute(select(m.DataBoard).where(m.DataBoard.id == s.board))).scalar_one()


async def _update(app_session: AppSessionFactory, s: _Seed, **fields: Any) -> None:
    async with app_session(s.tenant) as db:
        result = await apply_operation(
            db,
            tenant_id=s.tenant,
            data={"type": "board.update", "boardId": str(s.board), **fields},
        )
        assert result is None


async def _refused_update(app_session: AppSessionFactory, s: _Seed, **fields: Any) -> str | None:
    async with app_session(s.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(
                db,
                tenant_id=s.tenant,
                data={"type": "board.update", "boardId": str(s.board), **fields},
            )
    return caught.value.reason


def _chain(exc: BaseException) -> list[BaseException]:
    """`exc` and every exception a traceback of it would print."""
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return seen


# -- board.create ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_create_pins_the_agents_department(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        created = await apply_operation(
            db,
            tenant_id=s.tenant,
            data={
                "type": "board.create",
                "agentId": str(s.agent),
                "title": "Margins",
                "description": "Monthly margins",
                "spec": _SPEC,
            },
        )
        assert isinstance(created, uuid.UUID)
        board = (
            await db.execute(select(m.DataBoard).where(m.DataBoard.id == created))
        ).scalar_one()
        assert board.department_id == s.dept
        assert board.agent_id == s.agent
        assert board.spec_version == 1
        assert (board.title, board.description) == ("Margins", "Monthly margins")
        assert board.spec == _norm(_SPEC)


@pytest.mark.asyncio
async def test_board_create_refuses_an_archived_or_unknown_agent(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        agent = await db.get(m.Agent, s.agent)
        assert agent is not None
        agent.deleted_at = _NOW
        await db.flush()
    for agent_id in (s.agent, uuid.uuid4()):
        async with app_session(s.tenant) as db:
            with pytest.raises(InvalidOperation) as caught:
                await apply_operation(
                    db,
                    tenant_id=s.tenant,
                    data={
                        "type": "board.create",
                        "agentId": str(agent_id),
                        "title": "Margins",
                        "spec": _SPEC,
                    },
                )
            assert caught.value.reason == "agent_unknown"
            count = await db.scalar(
                select(func.count()).select_from(m.DataBoard).where(m.DataBoard.title == "Margins")
            )
            assert count == 0


def test_invalid_spec_is_an_invalid_operation() -> None:
    with pytest.raises(InvalidOperation) as caught:
        parse_operations(
            [
                {
                    "type": "board.create",
                    "agentId": str(uuid.uuid4()),
                    "title": "Sales",
                    "spec": {"datasets": []},
                }
            ]
        )
    assert caught.value.reason == "board_spec_invalid"


@pytest.mark.parametrize(
    "spec",
    [
        # A hostile unknown key inside the spec.
        {**_SPEC, _MARKER: "x"},
        # A hostile dataset key a block refers to (the spec parser names it).
        {
            **_SPEC,
            "blocks": [{"type": "kpi", "dataset": _MARKER, "field": "amount"}],
        },
        # A hostile value of the wrong type.
        {**_SPEC, "datasets": _MARKER},
        # Not an object at all.
        _MARKER,
    ],
)
def test_spec_errors_carry_no_submitted_text(spec: object) -> None:
    raw = {"type": "board.create", "agentId": str(uuid.uuid4()), "title": "Sales", "spec": spec}
    with pytest.raises(InvalidOperation) as caught:
        parse_operations([raw])
    assert caught.value.reason == "board_spec_invalid"
    for exc in _chain(caught.value):
        assert _MARKER not in str(exc), type(exc).__name__
        assert _MARKER not in repr(exc.args), type(exc).__name__
    # The builder path reports the same fixed code, and nothing else.
    with pytest.raises(BuilderInvalid) as refused:
        parse_builder_operations([raw])
    assert (refused.value.code, refused.value.step) == ("board_spec_invalid", 0)
    for exc in _chain(refused.value):
        assert _MARKER not in str(exc), type(exc).__name__


def test_spec_and_restore_version_are_exclusive() -> None:
    with pytest.raises(InvalidOperation):
        parse_operations(
            [
                {
                    "type": "board.update",
                    "boardId": str(uuid.uuid4()),
                    "spec": _SPEC,
                    "restoreVersion": 1,
                }
            ]
        )
    with pytest.raises(InvalidOperation):
        parse_operations(
            [{"type": "board.update", "boardId": str(uuid.uuid4()), "restoreVersion": 0}]
        )


def test_the_secret_gate_reads_board_text() -> None:
    spec = copy.deepcopy(_SPEC)
    spec["blocks"][0]["title"] = "api_key=sk-test"
    operation = BoardCreate(
        type="board.create",
        agentId=uuid.uuid4(),
        title="Sales",
        description="Weekly view",
        spec=spec,
    )
    texts = _texts(operation)
    assert texts[:2] == ["Sales", "Weekly view"]
    assert "title=api_key=sk-test" in texts
    assert "amount" in texts


# -- board.update ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_update_keeps_the_previous_spec_and_records(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session, records=True)
    await _update(app_session, s, spec=_with_cost(_SPEC))
    board = await _board(app_session, s)
    assert board.spec_version == 2
    assert [f["key"] for f in board.spec["datasets"][0]["fields"]] == ["amount", "cost"]
    async with app_session(s.tenant) as db:
        versions = (
            (
                await db.execute(
                    select(m.DataBoardSpecVersion).where(m.DataBoardSpecVersion.board_id == s.board)
                )
            )
            .scalars()
            .all()
        )
        assert [(v.version, v.title, v.spec) for v in versions] == [(1, "Revenue", _SPEC)]
        records = await db.scalar(
            select(func.count())
            .select_from(m.DataBoardRecord)
            .where(m.DataBoardRecord.board_id == s.board)
        )
        assert records == 2


@pytest.mark.asyncio
async def test_board_update_refuses_a_field_rename(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    renamed = copy.deepcopy(_SPEC)
    renamed["datasets"][0]["fields"][0]["key"] = "revenue"
    renamed["blocks"][0]["field"] = "revenue"
    assert await _refused_update(app_session, s, spec=renamed) == "field_removed"
    assert (await _board(app_session, s)).spec_version == 1


@pytest.mark.asyncio
async def test_board_update_refuses_a_field_type_change(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    retyped = copy.deepcopy(_SPEC)
    retyped["datasets"][0]["fields"][0] = {"key": "amount", "label": "Amount", "type": "number"}
    assert await _refused_update(app_session, s, spec=retyped) == "field_type_changed"
    board = await _board(app_session, s)
    assert board.spec_version == 1 and board.spec == _SPEC


@pytest.mark.asyncio
async def test_board_update_changes_title_and_description(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    await _update(app_session, s, title="Umsatz", description="Täglich")
    board = await _board(app_session, s)
    assert (board.title, board.description, board.spec_version) == ("Umsatz", "Täglich", 1)


@pytest.mark.asyncio
async def test_replacement_agent_must_be_in_the_boards_department(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        other_dept = m.Department(tenant_id=s.tenant, name="Sales", frame={})
        db.add(other_dept)
        await db.flush()
        outsider = m.Agent(
            tenant_id=s.tenant, department_id=other_dept.id, name="Outsider", definition={}
        )
        colleague = m.Agent(
            tenant_id=s.tenant, department_id=s.dept, name="Colleague", definition={}
        )
        db.add_all([outsider, colleague])
        await db.flush()
        outsider_id, colleague_id = outsider.id, colleague.id
    assert (
        await _refused_update(app_session, s, agentId=str(outsider_id)) == "agent_other_department"
    )
    board = await _board(app_session, s)
    assert (board.agent_id, board.department_id) == (s.agent, s.dept)
    await _update(app_session, s, agentId=str(colleague_id))
    board = await _board(app_session, s)
    assert (board.agent_id, board.department_id) == (colleague_id, s.dept)


@pytest.mark.asyncio
async def test_board_update_of_an_unknown_or_removed_board(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(
                db,
                tenant_id=s.tenant,
                data={"type": "board.update", "boardId": str(uuid.uuid4()), "title": "X"},
            )
        assert caught.value.reason == "board_unknown"
        board = await db.get(m.DataBoard, s.board)
        assert board is not None
        board.removed_at = _NOW
        await db.flush()
    assert await _refused_update(app_session, s, title="X") == "board_unknown"


@pytest.mark.asyncio
async def test_stale_update_is_detected(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    operation = parse_operations([{"type": "board.update", "boardId": str(s.board), "title": "X"}])[
        0
    ]
    async with app_session(s.tenant) as db:
        assert await target_revision(db, operation) == "1"
    await _update(app_session, s, spec=_with_cost(_SPEC))
    async with app_session(s.tenant) as db:
        assert await target_revision(db, operation, lock_for_apply=True) == "2"
        create = parse_operations(
            [{"type": "board.create", "agentId": str(s.agent), "title": "T", "spec": _SPEC}]
        )[0]
        assert await target_revision(db, create) is None
        missing = parse_operations(
            [{"type": "board.update", "boardId": str(uuid.uuid4()), "title": "X"}]
        )[0]
        with pytest.raises(InvalidOperation):
            await target_revision(db, missing)


@pytest.mark.asyncio
async def test_restore_version_brings_back_the_old_blocks_and_keeps_datasets(
    app_session: AppSessionFactory,
) -> None:
    s = await _seed(app_session, records=True)
    v2 = _with_cost(_SPEC)
    await _update(app_session, s, spec=v2)
    await _update(app_session, s, restoreVersion=1)
    board = await _board(app_session, s)
    assert board.spec_version == 3
    assert board.spec["blocks"] == _norm(_SPEC)["blocks"]
    assert board.spec["datasets"] == _norm(v2)["datasets"]
    assert "cost" in [f["key"] for f in board.spec["datasets"][0]["fields"]]
    async with app_session(s.tenant) as db:
        versions = (
            await db.execute(
                select(m.DataBoardSpecVersion.version, m.DataBoardSpecVersion.spec)
                .where(m.DataBoardSpecVersion.board_id == s.board)
                .order_by(m.DataBoardSpecVersion.version)
            )
        ).all()
        assert [(v, spec) for v, spec in versions] == [(1, _SPEC), (2, _norm(v2))]
        records = await db.scalar(
            select(func.count())
            .select_from(m.DataBoardRecord)
            .where(m.DataBoardRecord.board_id == s.board)
        )
        assert records == 2


@pytest.mark.asyncio
async def test_restore_of_an_unknown_version_is_refused(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    assert await _refused_update(app_session, s, restoreVersion=7) == "board_version_unknown"
    assert (await _board(app_session, s)).spec_version == 1


@pytest.mark.asyncio
async def test_restore_that_no_longer_parses_is_refused(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    async with app_session(s.tenant) as db:
        broken = copy.deepcopy(_SPEC)
        broken["blocks"] = [{"type": "kpi", "dataset": _MARKER, "field": "amount"}]
        db.add(
            m.DataBoardSpecVersion(
                tenant_id=s.tenant, board_id=s.board, version=1, title="Revenue", spec=broken
            )
        )
        board = await db.get(m.DataBoard, s.board)
        assert board is not None
        board.spec_version = 2
        await db.flush()
    async with app_session(s.tenant) as db:
        with pytest.raises(InvalidOperation) as caught:
            await apply_operation(
                db,
                tenant_id=s.tenant,
                data={"type": "board.update", "boardId": str(s.board), "restoreVersion": 1},
            )
    assert caught.value.reason == "board_spec_invalid"
    assert all(_MARKER not in str(exc) for exc in _chain(caught.value))
    board_after = await _board(app_session, s)
    assert board_after.spec_version == 2 and board_after.spec == _SPEC


# -- only through builder proposals (F11) ---------------------------------------


@pytest.mark.asyncio
async def test_generic_proposals_refuse_board_operations(app_session: AppSessionFactory) -> None:
    s = await _seed(app_session)
    actor = Principal(subject="operator-1", tenant_id=s.tenant, role="org_admin")
    for operation in (
        {"type": "board.create", "agentId": str(s.agent), "title": "T", "spec": _SPEC},
        {"type": "board.update", "boardId": str(s.board), "title": "T"},
    ):
        async with app_session(s.tenant) as db:
            with pytest.raises(InvalidOperation) as caught:
                await create_proposal(db, actor, [operation])
            assert caught.value.reason == "board_builder_only"
            proposals = await db.scalar(
                select(func.count())
                .select_from(m.CopilotProposal)
                .where(m.CopilotProposal.tenant_id == s.tenant)
            )
            assert proposals == 0
    assert (await _board(app_session, s)).title == "Revenue"


@pytest.mark.asyncio
async def test_a_stored_generic_board_operation_is_never_applied(
    app_session: AppSessionFactory,
) -> None:
    """Defense in depth: `create_proposal` never stores one, but a row that does
    is rejected by `apply_proposal`, not applied."""
    s = await _seed(app_session)
    actor = Principal(subject="operator-1", tenant_id=s.tenant, role="org_admin")
    async with app_session(s.tenant) as db:
        proposal = CopilotProposal(
            tenant_id=s.tenant, created_by="operator-1", idempotency_key=uuid.uuid4()
        )
        db.add(proposal)
        await db.flush()
        db.add(
            CopilotOperation(
                tenant_id=s.tenant,
                proposal_id=proposal.id,
                ordinal=0,
                operation_type="board.update",
                configuration={"type": "board.update", "boardId": str(s.board), "title": "X"},
                target_revision="1",
            )
        )
        await db.flush()
        with pytest.raises(ProposalRejected):
            await apply_proposal(db, proposal.id, actor)
        assert proposal.status == "rejected"
    assert (await _board(app_session, s)).title == "Revenue"


# -- edition capability ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_has_capability_reads_the_editions_entitlements() -> None:
    tenant = uuid.uuid4()
    assert await tenant_has_capability(tenant, ENTERPRISE_CANVAS) is False

    class _Canvas:
        def has(self, capability: Capability) -> bool:
            return capability == ENTERPRISE_CANVAS

        def limit(self, name: str) -> int | None:
            return None

    async def entitlements(_tenant: uuid.UUID) -> Entitlements:
        return _Canvas()

    with use_edition_tools([], entitlements):
        assert await tenant_has_capability(tenant, ENTERPRISE_CANVAS) is True
        assert await tenant_has_capability(tenant, "enterprise.sso.saml") is False
