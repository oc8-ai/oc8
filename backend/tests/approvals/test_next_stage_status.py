"""`nextStageStatus` on a member's own approval (D3a): how the department's
second stage stands, so a member without a seat in that department -- who is
never shown the department's row -- still learns how it was decided. Nothing
else of the department row reaches them."""

from __future__ import annotations

import uuid

import pytest

from oc8.api.v1._serializers import approval_to_dto, resolve_approval_names
from oc8.approvals import decide_approval, raise_approval
from oc8.auth import get_identity_provider
from tests.approvals.test_member_approval_routing import (
    _actor,
    _http,
    _office,
    _raise_member_stage,
    _rows,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _dto_status(db, row) -> str | None:  # type: ignore[no-untyped-def]
    names = await resolve_approval_names(db, [row])
    return approval_to_dto(row, names).next_stage_status


async def test_the_members_row_reports_the_department_stage(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        first = await _raise_member_stage(db, tenant, office, department_follows=True)
        anna = await _actor(db, tenant, office.anna, "member")
        dana = await _actor(db, tenant, office.dana, "member")

        assert await _dto_status(db, first) is None, "no stage before the member's yes"
        result = await decide_approval(db, first, decision="approve", tenant_id=tenant, actor=anna)
        assert await _dto_status(db, first) == "pending"

        second = next(r for r in await _rows(db, office.task) if r.id == result.next_stage_id)
        assert await _dto_status(db, second) is None, "a department row reports nothing"
        await decide_approval(db, second, decision="reject", tenant_id=tenant, actor=dana)
        assert await _dto_status(db, first) == "rejected"


async def test_a_department_row_never_reports_one(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        plain = await raise_approval(
            db,
            tenant_id=tenant,
            agent_id=office.agent,
            action_type="tool_send",
            title="Nora wants to call erp.cancel_order",
            payload={"tool": "erp.cancel_order", "arguments": {}},
        )
        assert await _dto_status(db, plain) is None


async def test_a_member_without_a_seat_learns_how_the_department_decided(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await _office(db, tenant)
        own = await _raise_member_stage(db, tenant, office, department_follows=True)
        own_id = own.id

    token = get_identity_provider().mint(tenant_id=tenant, subject="anna", role="member")
    hers = {"Authorization": f"Bearer {token}"}
    async with _http() as http:
        decided = await http.post(
            f"/api/v1/approvals/{own_id}/decision", json={"decision": "approve"}, headers=hers
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["nextStageStatus"] == "pending"

        listed = await http.get("/api/v1/approvals?status=approved", headers=hers)
        assert listed.status_code == 200, listed.text
        assert [(r["id"], r["nextStageStatus"]) for r in listed.json()] == [
            (str(own_id), "pending")
        ]

    async with app_session(tenant) as db:
        dana = await _actor(db, tenant, office.dana, "member")
        second = next(r for r in await _rows(db, office.task) if r.previous_approval_id == own_id)
        await decide_approval(db, second, decision="approve", tenant_id=tenant, actor=dana)

    async with _http() as http:
        listed = await http.get("/api/v1/approvals?status=approved", headers=hers)
        body = listed.json()
        assert [(r["id"], r["nextStageStatus"]) for r in body] == [(str(own_id), "approved")]
        assert set(body[0]) == set(listed.json()[0]), "same shape, nothing of the stage added"
