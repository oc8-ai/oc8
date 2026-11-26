"""The named, disclosed residual: a department-scoped `agent_manage` toggle
holder can CREATE a gated agent, but cannot resolve the `hire_agent` approval
that creation raised.

`EFFECT_PERMISSIONS['hire_agent'] = perm(AGENT, MANAGE)` (`approvals/service.py`)
is untouched by this design -- deciding a `hire_agent` approval stays gated on
the RESOLVED, tenant-wide permission set (`granted_for_member`), which the
seat-native `org_member_department.agent_manage` column never enters, because it
is never a `resource:action` string. This is the workspace design's own
residual 5 ("a messenger cannot resolve a `budget_incident` or a `hire_agent`"),
inherited exactly as it exists today -- not introduced or worsened by this
slice, only reachable by a wider population now that the toggle exists at all.
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
from oc8.agents.hire import set_require_hire_approval
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _subject_uuid(subject: str) -> uuid.UUID:
    try:
        return uuid.UUID(subject)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


async def _office(app_session: AppSessionFactory) -> tuple[uuid.UUID, uuid.UUID]:
    """One department, the hire gate turned on, and a member holding a live
    `dept_approver` seat there with `agent_manage=True` -- an approver AND a
    write-toggle holder at once, so a refusal below can only be about THIS one
    action type, never about missing `approval:decide` or a missing toggle."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:6]}",
                name="T",
                tier="standard",
                region="eu",
                settings={},
            )
        )
        await db.flush()
        await set_require_hire_approval(db, tenant_id=tenant, enabled=True)

        dept = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(dept)
        await db.flush()

        holder = m.OrgMember(tenant_id=tenant, subject="hos", subject_uuid=_subject_uuid("hos"))
        db.add(holder)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=holder.id,
                department_id=dept.id,
                seat_role=SEAT_APPROVER,
                agent_manage=True,
            )
        )
        await db.flush()
        return tenant, dept.id


async def test_department_write_holder_cannot_resolve_own_hire_request(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id = await _office(app_session)

    async with _http() as http:
        # 1. She CAN create the gated agent -- the write half of this slice,
        #    admitted through her own department's toggle, no tenant-wide grant.
        created = await http.post(
            "/api/v1/agents",
            json={"name": "Neu", "departmentId": str(dept_id)},
            headers=_headers(tenant, "hos"),
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]

    async with app_session(tenant) as db:
        agent_row = await db.get(m.Agent, uuid.UUID(agent_id))
        assert agent_row is not None and agent_row.status == "pending_approval", (
            "the hire gate did not fire -- the residual this test names cannot be "
            "exercised without a pending hire_agent approval"
        )
        approval = (
            await db.execute(
                select(m.ApprovalRequest).where(
                    m.ApprovalRequest.tenant_id == tenant,
                    m.ApprovalRequest.agent_id == agent_row.id,
                    m.ApprovalRequest.action_type == "hire_agent",
                )
            )
        ).scalar_one()
        approval_id = approval.id

    async with _http() as http:
        # 2. She CANNOT resolve it -- deciding a hire_agent approval costs
        #    agent:manage RESOLVED tenant-wide, which her seat's toggle never
        #    grants because it is never a permission string.
        decided = await http.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "approve"},
            headers=_headers(tenant, "hos"),
        )
        assert decided.status_code == 403, decided.text
        assert "agent:manage" in decided.text

    async with app_session(tenant) as db:
        still_pending = await db.get(m.Agent, uuid.UUID(agent_id))
        assert still_pending is not None and still_pending.status == "pending_approval", (
            "the refused decision still activated the agent"
        )


async def test_a_tenant_wide_agent_manage_holder_can_resolve_it(
    app_session: AppSessionFactory,
) -> None:
    """The control: the SAME approval, decided by `org_admin`, succeeds -- so the
    403 above is the residual and not a route that refuses everybody."""
    tenant, dept_id = await _office(app_session)
    async with _http() as http:
        created = await http.post(
            "/api/v1/agents",
            json={"name": "Neu", "departmentId": str(dept_id)},
            headers=_headers(tenant, "hos"),
        )
        assert created.status_code == 201, created.text
        agent_id = created.json()["id"]

    async with app_session(tenant) as db:
        approval = (
            await db.execute(
                select(m.ApprovalRequest).where(
                    m.ApprovalRequest.tenant_id == tenant,
                    m.ApprovalRequest.agent_id == uuid.UUID(agent_id),
                    m.ApprovalRequest.action_type == "hire_agent",
                )
            )
        ).scalar_one()
        approval_id = approval.id

    async with _http() as http:
        decided = await http.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "approve"},
            headers=_headers(tenant, "boss", "org_admin"),
        )
        assert decided.status_code == 200, decided.text

    async with app_session(tenant) as db:
        activated: Any = await db.get(m.Agent, uuid.UUID(agent_id))
        assert activated is not None and activated.status == "idle"
