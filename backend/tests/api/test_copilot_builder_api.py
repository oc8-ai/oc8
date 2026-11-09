"""The card's routes: read, "Anlegen", "Zur Freigabe senden". Owner-only
writes; the approver reads once the proposal is in their queue (Ruling A4)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.authority import authority_for_member, tenant_wide_read
from oc8.authz.permissions import DEPARTMENT, SEAT_APPROVER, SEAT_VIEWER, VIEW, perm
from oc8.authz.scope import scope_for_member
from oc8.copilot.builder import create_builder_proposal
from oc8.copilot.models import CopilotProposal
from oc8.edition.runtime import RuntimeComposition
from oc8.main import create_app
from oc8.runtime.supervision_hook import NOOP_SUPERVISION_RUN_HOOK
from tests.conftest import AppSessionFactory
from tests.copilot.builder_helpers import BuilderOffice, builder_office, invoice_checker_ops
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio

_NAME = "Rechnungsprüfer"


def _headers(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


async def _seed(
    app_session: AppSessionFactory,
    owner: Literal["admin", "employee"],
    *,
    now: dt.datetime | None = None,
) -> tuple[BuilderOffice, uuid.UUID]:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        office = await builder_office(db, tenant)
        seat = office.admin if owner == "admin" else office.employee
        member = await db.get(m.OrgMember, seat.member_id)
        assert member is not None
        role = "org_admin" if owner == "admin" else "member"
        scope = await scope_for_member(db, member, token_role=role)
        authority = await authority_for_member(db, member, token_role=role)
        proposal = await create_builder_proposal(
            db,
            tenant_id=tenant,
            member=member,
            scope=scope,
            tenant_wide=tenant_wide_read(authority, perm(DEPARTMENT, VIEW)),
            session_id=seat.session_id,
            operations=invoice_checker_ops(office),
            now=now or dt.datetime.now(tz=dt.UTC),
        )
        return office, proposal.id


async def _agents_named(db: AsyncSession, name: str = _NAME) -> int:
    return int(
        (
            await db.execute(select(func.count()).select_from(m.Agent).where(m.Agent.name == name))
        ).scalar_one()
    )


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_the_owner_with_agent_manage_creates_the_agent_once(
    app_session: AppSessionFactory,
) -> None:
    office, pid = await _seed(app_session, "admin")
    admin = _headers(office.tenant, "admin", "org_admin")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        card = await c.get(f"/api/v1/copilot/builder/{pid}", headers=admin)
        assert card.status_code == 200, card.text
        body = card.json()
        assert body["mayCreate"] is True and body["status"] == "draft"
        assert body["readOnly"] is False
        assert body["name"] == _NAME and body["departmentName"] == "Buchhaltung"
        assert body["mission"].startswith("Prüft jeden Morgen")
        assert body["model"]["label"] == "stub"
        assert body["skills"][0]["label"] == "Rechnungen prüfen"
        assert body["triggers"][0]["cronExpression"] == "0 7 * * 1-5"

        applied = await c.post(f"/api/v1/copilot/builder/{pid}/apply", headers=admin)
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"
        assert applied.json()["agent"]["name"] == _NAME
        assert applied.json()["mayCreate"] is False

        # A second "Anlegen" (double click, second tab) creates nothing.
        again = await c.post(f"/api/v1/copilot/builder/{pid}/apply", headers=admin)
        assert again.status_code == 409
        assert again.json()["detail"] == "agent_builder:already_applied"
    async with app_session(office.tenant) as db:
        assert await _agents_named(db) == 1


async def test_an_employee_requests_instead_and_the_lead_reads_it(
    app_session: AppSessionFactory,
) -> None:
    office, pid = await _seed(app_session, "employee")
    emp = _headers(office.tenant, "emp")
    lead = _headers(office.tenant, "lead")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        card = (await c.get(f"/api/v1/copilot/builder/{pid}", headers=emp)).json()
        assert card["mayCreate"] is False and card["mayRequest"] is True

        refused = await c.post(f"/api/v1/copilot/builder/{pid}/apply", headers=emp)
        assert refused.status_code == 403

        hidden = await c.get(f"/api/v1/copilot/builder/{pid}", headers=lead)
        assert hidden.status_code == 404, "a draft is the owner's alone"
        not_theirs = await c.post(f"/api/v1/copilot/builder/{pid}/request", headers=lead)
        assert not_theirs.status_code == 404

        requested = await c.post(f"/api/v1/copilot/builder/{pid}/request", headers=emp)
        assert requested.status_code == 200, requested.text
        assert requested.json()["status"] == "awaiting_approval"
        assert requested.json()["mayRequest"] is False

        twice = await c.post(f"/api/v1/copilot/builder/{pid}/request", headers=emp)
        assert twice.status_code == 409
        assert twice.json()["detail"] == "agent_builder:not_open"

        read = await c.get(f"/api/v1/copilot/builder/{pid}", headers=lead)
        assert read.status_code == 200
        assert read.json()["readOnly"] is True and read.json()["mayCreate"] is False
        assert read.json()["mayRequest"] is False
        assert read.json()["mission"].startswith("Prüft jeden Morgen")
        lead_apply = await c.post(f"/api/v1/copilot/builder/{pid}/apply", headers=lead)
        assert lead_apply.status_code == 404, "the approver applies through the approval"

        approvals = (await c.get("/api/v1/approvals?status=pending", headers=lead)).json()
        mine = [a for a in approvals if a.get("proposalId") == str(pid)]
        assert mine and mine[0]["actionType"] == "copilot.agent_builder"


async def test_a_department_viewer_reads_a_request_without_its_mission(
    app_session: AppSessionFactory,
) -> None:
    office, pid = await _seed(app_session, "employee")
    async with app_session(office.tenant) as db:
        viewer = await copilot_seat(db, office.tenant, "viewer")
        await copilot_seat(db, office.tenant, "outsider")
        db.add(
            m.OrgMemberDepartment(
                tenant_id=office.tenant,
                member_id=viewer.member_id,
                department_id=office.department_id,
                seat_role=SEAT_VIEWER,
                agent_manage=False,
            )
        )
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        url = f"/api/v1/copilot/builder/{pid}"
        draft = await c.get(url, headers=_headers(office.tenant, "viewer"))
        assert draft.status_code == 404, "viewers never see a draft"
        sent = await c.post(f"{url}/request", headers=_headers(office.tenant, "emp"))
        assert sent.status_code == 200, sent.text

        read = await c.get(url, headers=_headers(office.tenant, "viewer"))
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["readOnly"] is True and body["name"] == _NAME
        assert body["mission"] == "", "the mission is the owner's and the approver's"
        assert body["triggers"][0]["taskText"] == ""

        elsewhere = await c.get(url, headers=_headers(office.tenant, "outsider"))
        assert elsewhere.status_code == 404, "no view of the department, no card"


async def test_an_approver_through_tenant_wide_agent_manage_reads_the_mission(
    app_session: AppSessionFactory,
) -> None:
    """The approver whose right to create agents comes from tenant-wide
    agent:manage (a dept_manager), not the seat's toggle, decides the request
    and so reads what they decide on: the mission and the trigger's task."""
    office, pid = await _seed(app_session, "employee")
    async with app_session(office.tenant) as db:
        boss = await copilot_seat(db, office.tenant, "boss", with_role=False)
        db.add(
            m.OrgMemberDepartment(
                tenant_id=office.tenant,
                member_id=boss.member_id,
                department_id=office.department_id,
                seat_role=SEAT_APPROVER,
                agent_manage=False,
            )
        )
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        url = f"/api/v1/copilot/builder/{pid}"
        sent = await c.post(f"{url}/request", headers=_headers(office.tenant, "emp"))
        assert sent.status_code == 200, sent.text
        read = await c.get(url, headers=_headers(office.tenant, "boss", "dept_manager"))
        assert read.status_code == 200, read.text
        body = read.json()
        assert body["readOnly"] is True
        assert body["mission"].startswith("Prüft jeden Morgen")
        assert body["triggers"][0]["taskText"] == "Prüfe die offenen Rechnungen."


async def test_a_reader_of_the_shared_chat_cannot_touch_the_card(
    app_session: AppSessionFactory,
) -> None:
    """Ruling 8/45/47: a shared chat is read-only; its card's writes stay the
    owner's, and a reader without a view of the department never sees it."""
    office, pid = await _seed(app_session, "employee")
    async with app_session(office.tenant) as db:
        reader = await copilot_seat(db, office.tenant, "reader")

    async def readers(_db: AsyncSession, session: m.ChatSession) -> list[uuid.UUID]:
        return [reader.member_id] if session.id == office.employee.session_id else []

    app = create_app(
        runtime_composition=RuntimeComposition(NOOP_SUPERVISION_RUN_HOOK, extra_readers=readers)
    )
    h = _headers(office.tenant, "reader", "org_admin")
    async with LifespanManager(app), _client(app) as c:
        chat = await c.get(f"/api/v1/chat/sessions/{office.employee.session_id}", headers=h)
        assert chat.status_code == 200 and chat.json()["readOnly"] is True
        url = f"/api/v1/copilot/builder/{pid}"
        assert (await c.get(url, headers=h)).status_code == 404
        assert (await c.post(f"{url}/apply", headers=h)).status_code == 404
        assert (await c.post(f"{url}/request", headers=h)).status_code == 404
    async with app_session(office.tenant) as db:
        proposal = await db.get(CopilotProposal, pid)
        assert proposal is not None and proposal.status == "draft"
        assert await _agents_named(db) == 0


async def test_an_approval_whose_apply_is_refused_answers_409_and_stays_pending(
    app_session: AppSessionFactory,
) -> None:
    office, pid = await _seed(app_session, "employee")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        sent = await c.post(
            f"/api/v1/copilot/builder/{pid}/request", headers=_headers(office.tenant, "emp")
        )
        assert sent.status_code == 200, sent.text
        async with app_session(office.tenant) as db:
            # A colleague took the name meanwhile.
            db.add(m.Agent(tenant_id=office.tenant, department_id=office.department_id, name=_NAME))
            proposal = await db.get(CopilotProposal, pid)
            assert proposal is not None and proposal.result is not None
            approval_id = proposal.result["approvalId"]
        decided = await c.post(
            f"/api/v1/approvals/{approval_id}/decision",
            json={"decision": "approve"},
            headers=_headers(office.tenant, "lead"),
        )
        assert decided.status_code == 409, decided.text
        assert decided.json()["detail"] == "agent_builder:name_taken"
    async with app_session(office.tenant) as db:
        approval = await db.get(m.ApprovalRequest, uuid.UUID(approval_id))
        proposal = await db.get(CopilotProposal, pid)
        assert approval is not None and approval.status == "pending"
        assert proposal is not None and proposal.status == "awaiting_approval"
        assert await _agents_named(db) == 1, "only the colleague's"


async def test_an_expired_proposal_reads_as_expired_and_refuses_with_409(
    app_session: AppSessionFactory,
) -> None:
    office, pid = await _seed(app_session, "admin", now=dt.datetime.now(tz=dt.UTC))
    async with app_session(office.tenant) as db:
        proposal = await db.get(CopilotProposal, pid)
        assert proposal is not None
        proposal.expires_at = dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=1)
    admin = _headers(office.tenant, "admin", "org_admin")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        card = await c.get(f"/api/v1/copilot/builder/{pid}", headers=admin)
        assert card.status_code == 200, card.text
        assert card.json()["status"] == "expired" and card.json()["mayCreate"] is False
        applied = await c.post(f"/api/v1/copilot/builder/{pid}/apply", headers=admin)
        assert applied.status_code == 409
        assert applied.json()["detail"] == "agent_builder:not_open"
        sent = await c.post(f"/api/v1/copilot/builder/{pid}/request", headers=admin)
        assert sent.status_code == 409
    async with app_session(office.tenant) as db:
        proposal = await db.get(CopilotProposal, pid)
        assert proposal is not None and proposal.status == "expired", "the read persisted it"
        assert await _agents_named(db) == 0


async def test_an_unknown_proposal_is_not_found(app_session: AppSessionFactory) -> None:
    office, _pid = await _seed(app_session, "admin")
    admin = _headers(office.tenant, "admin", "org_admin")
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        url = f"/api/v1/copilot/builder/{uuid.uuid4()}"
        assert (await c.get(url, headers=admin)).status_code == 404
        assert (await c.post(f"{url}/apply", headers=admin)).status_code == 404
        assert (await c.post(f"{url}/request", headers=admin)).status_code == 404
