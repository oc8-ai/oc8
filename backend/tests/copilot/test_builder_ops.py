"""A builder proposal names the agent it creates with `ref` and every later
operation points at it with `agentRef` (spec §3.2). Validated before anything
is stored; resolved to the real id only at apply time."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as m
from oc8.copilot.builder_ops import (
    PLACEHOLDER_AGENT_ID,
    BuilderInvalid,
    builder_operation_data,
    is_board_only,
    parse_builder_operations,
    resolve_step,
)
from oc8.copilot.capabilities import AgentCreate, BoardCreate, BoardUpdate, apply_operation
from tests.conftest import AppSessionFactory

DEPT = str(uuid.uuid4())
MODEL = str(uuid.uuid4())


def _create(ref: str | None = "$new") -> dict[str, Any]:
    op: dict[str, Any] = {
        "type": "agent.create",
        "departmentId": DEPT,
        "name": "Rechnungsprüfer",
    }
    if ref is not None:
        op["ref"] = ref
    return op


def _model(agent_ref: str = "$new") -> dict[str, Any]:
    return {"type": "agent.model.switch", "agentRef": agent_ref, "modelConfigId": MODEL}


def _refused(raw: object) -> BuilderInvalid:
    with pytest.raises(BuilderInvalid) as caught:
        parse_builder_operations(raw)
    return caught.value


def test_a_valid_proposal_parses_with_a_placeholder_id() -> None:
    steps = parse_builder_operations([_create(), _model()])
    assert isinstance(steps[0].operation, AgentCreate) and steps[0].ref == "$new"
    assert steps[1].agent_ref == "$new"
    assert steps[1].operation.agentId == PLACEHOLDER_AGENT_ID  # type: ignore[union-attr]


_EXISTING_AGENT = str(uuid.uuid4())


@pytest.mark.parametrize(
    ("raw", "code", "step"),
    [
        ([_create(), _model("$other")], "unknown_ref", 1),
        ([_model(), _create()], "ref_before_create", 0),
        ([_create(), _create("$two")], "two_refs", 1),
        ([_create(ref=None), _model()], "missing_ref", 0),
        ([_model()], "missing_create", None),
        (
            [
                _create(),
                {"type": "agent.model.switch", "agentId": _EXISTING_AGENT, "modelConfigId": MODEL},
            ],
            "agent_id_not_allowed",
            1,
        ),
        (
            [_create(), {**_model(), "agentId": _EXISTING_AGENT}],
            "agent_id_not_allowed",
            1,
        ),
        # Neither agentRef nor agentId: the step points at no ref (Ruling A17).
        (
            [_create(), {"type": "agent.model.switch", "modelConfigId": MODEL}],
            "unknown_ref",
            1,
        ),
        # An agentRef that names an existing agent's id is not a ref.
        ([_create(), _model(_EXISTING_AGENT)], "unknown_ref", 1),
        ([_create(), {"type": "department.create", "name": "X"}], "type_not_allowed", 1),
        ([_create(), {**_model(), "secret": "x"}], "invalid", 1),
        ([_create("not-a-ref"), _model()], "invalid", 0),
        ([_create(), {**_model(), "ref": "$x"}], "invalid", 1),
        ([], "invalid", None),
        ("not a list", "invalid", None),
    ],
)
def test_bad_proposals_are_refused_with_a_code(raw: object, code: str, step: int | None) -> None:
    refused = _refused(raw)
    assert refused.code == code and refused.step == step
    assert "Rechnungsprüfer" not in str(refused), "value-free"


def test_the_storage_form_round_trips() -> None:
    steps = parse_builder_operations([_create(), _model()])
    stored = [builder_operation_data(s) for s in steps]
    assert stored[0]["ref"] == "$new" and "agentId" not in stored[1]
    assert stored[1]["agentRef"] == "$new"
    again = parse_builder_operations(stored)
    assert [builder_operation_data(s) for s in again] == stored


def test_resolve_step_names_the_created_agent() -> None:
    created = uuid.uuid4()
    steps = parse_builder_operations([_create(), _model()])
    data, ref = resolve_step(builder_operation_data(steps[0]), {})
    assert ref == "$new" and "ref" not in data
    data, ref = resolve_step(builder_operation_data(steps[1]), {"$new": created})
    assert ref is None and data["agentId"] == str(created) and "agentRef" not in data
    with pytest.raises(BuilderInvalid):
        resolve_step(builder_operation_data(steps[1]), {})


def test_resolve_step_never_lets_a_raw_agent_id_through() -> None:
    created = uuid.uuid4()
    smuggled = {"type": "agent.model.switch", "agentId": _EXISTING_AGENT, "modelConfigId": MODEL}
    with pytest.raises(BuilderInvalid) as caught:
        resolve_step(smuggled, {"$new": created})
    assert caught.value.code == "agent_id_not_allowed"
    with pytest.raises(BuilderInvalid) as caught:
        resolve_step({**smuggled, "agentRef": "$new"}, {"$new": created})
    assert caught.value.code == "agent_id_not_allowed"
    with pytest.raises(BuilderInvalid) as caught:
        resolve_step({"type": "agent.model.switch", "modelConfigId": MODEL}, {"$new": created})
    assert caught.value.code == "unknown_ref"


@pytest.mark.asyncio
async def test_apply_operation_reports_the_created_agent(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Buchhaltung", frame={})
        db.add(dept)
        await db.flush()
        created = await apply_operation(
            db,
            tenant_id=tenant,
            data={"type": "agent.create", "departmentId": str(dept.id), "name": "Nora"},
        )
        assert isinstance(created, uuid.UUID)
        agent = await db.get(m.Agent, created)
        assert agent is not None and agent.name == "Nora"
        other = await apply_operation(
            db,
            tenant_id=tenant,
            data={"type": "agent.rename", "agentId": str(created), "name": "Nora B."},
        )
        assert other is None


_BOARD_SPEC: dict[str, Any] = {
    "datasets": [
        {
            "key": "sales",
            "label": "Sales",
            "grain": "day",
            "fields": [{"key": "amount", "label": "Amount", "type": "number"}],
        }
    ],
    "blocks": [{"type": "kpi", "dataset": "sales", "field": "amount"}],
}
_BOARD = str(uuid.uuid4())


def _board_create(**where: Any) -> dict[str, Any]:
    return {"type": "board.create", "title": "Sales", "spec": _BOARD_SPEC, **where}


def test_board_create_may_follow_agent_create() -> None:
    steps = parse_builder_operations([_create(), _board_create(agentRef="$new")])
    assert isinstance(steps[1].operation, BoardCreate)
    assert steps[1].agent_ref == "$new"
    assert steps[1].operation.agentId == PLACEHOLDER_AGENT_ID
    assert not is_board_only(steps)
    stored = builder_operation_data(steps[1])
    assert stored["agentRef"] == "$new" and "agentId" not in stored
    created = uuid.uuid4()
    data, ref = resolve_step(stored, {"$new": created})
    assert ref is None and data["agentId"] == str(created)


def test_board_only_create_and_update_parse() -> None:
    steps = parse_builder_operations([_board_create(agentId=_EXISTING_AGENT)])
    assert len(steps) == 1 and isinstance(steps[0].operation, BoardCreate)
    assert str(steps[0].operation.agentId) == _EXISTING_AGENT
    assert steps[0].agent_ref is None and steps[0].ref is None
    assert is_board_only(steps)
    stored = builder_operation_data(steps[0])
    assert stored["agentId"] == _EXISTING_AGENT and "agentRef" not in stored
    assert resolve_step(stored, {}, board_only=True) == (stored, None)
    with pytest.raises(BuilderInvalid) as caught:
        resolve_step(stored, {})
    assert caught.value.code == "agent_id_not_allowed", "only a board-only apply passes it"

    update = parse_builder_operations([{"type": "board.update", "boardId": _BOARD}])
    assert isinstance(update[0].operation, BoardUpdate)
    assert str(update[0].operation.boardId) == _BOARD
    assert is_board_only(update)
    again = builder_operation_data(update[0])
    assert resolve_step(again, {}, board_only=True) == (again, None)


def test_board_only_resolve_refuses_anything_else() -> None:
    with pytest.raises(BuilderInvalid) as caught:
        resolve_step(_model(), {"$new": uuid.uuid4()}, board_only=True)
    assert caught.value.code == "invalid"
    with pytest.raises(BuilderInvalid) as caught:
        resolve_step(_board_create(agentRef="$new"), {"$new": uuid.uuid4()}, board_only=True)
    assert caught.value.code == "invalid"


@pytest.mark.parametrize(
    "raw",
    [
        # Two board steps.
        [_board_create(agentId=_EXISTING_AGENT), {"type": "board.update", "boardId": _BOARD}],
        # A board.create with no agent to produce it.
        [_board_create()],
        # A board-only step may not carry refs.
        [_board_create(agentId=_EXISTING_AGENT, ref="$new")],
        [_board_create(agentRef="$new")],
        [{"type": "board.update", "boardId": _BOARD, "agentRef": "$new"}],
        [{"type": "board.update", "boardId": _BOARD, "departmentRef": "$department"}],
    ],
)
def test_board_only_needs_exactly_one_step(raw: list[dict[str, Any]]) -> None:
    refused = _refused(raw)
    assert (refused.code, refused.step) == ("missing_create", None)


def test_other_ops_without_create_still_refused() -> None:
    refused = _refused(
        [{"type": "agent.mission.set", "agentId": _EXISTING_AGENT, "mission": "Report sales."}]
    )
    assert (refused.code, refused.step) == ("missing_create", None)
    assert not is_board_only([])


def test_a_board_only_step_with_a_bad_field_is_invalid() -> None:
    refused = _refused([{"type": "board.update", "boardId": _BOARD, "title": ""}])
    assert (refused.code, refused.step) == ("invalid", 0)
