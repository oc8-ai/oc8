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
    parse_builder_operations,
    resolve_step,
)
from oc8.copilot.capabilities import AgentCreate, apply_operation
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
