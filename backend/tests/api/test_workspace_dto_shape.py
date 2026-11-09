"""What the two queue endpoints actually put on the wire.

The first test here is the important one and it is a confidentiality test, not a
cosmetic one. `ApprovalRequest.payload` is a free-form JSONB blob that every
raiser writes into and that `approvals/service.py::_announce` writes
`channel_handles` into -- which messenger accounts were told about this approval,
i.e. whose private chat id. §5 says the DTO takes two named keys out of it and
never serializes it wholesale, and "never" is only true if something checks.

The rest pin the additive fields the workspace screen is built on. Each one was
absent before this slice, so a serializer that silently dropped them would leave
the detail pane -- the whole reason the screen exists -- showing an id and a
title.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

#: A chat id is exactly the sort of thing that must not travel to a browser, and
#: it is written into `payload` by the announce path on every approval that
#: reaches a messenger.
SECRET_HANDLE = "telegram:770099123"


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
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


class _Office:
    tenant: uuid.UUID
    sales: uuid.UUID
    approval: uuid.UUID
    task: uuid.UUID
    clarification: uuid.UUID


async def _office(app_session: AppSessionFactory) -> _Office:
    office = _Office()
    office.tenant = uuid.uuid4()
    async with app_session(office.tenant) as db:
        sales = m.Department(tenant_id=office.tenant, name="Vertrieb")
        db.add(sales)
        await db.flush()
        agent = m.Agent(tenant_id=office.tenant, department_id=sales.id, name="Nora")
        db.add(agent)
        await db.flush()
        task = m.Task(
            tenant_id=office.tenant,
            department_id=sales.id,
            title="Angebot Gartenholz GmbH vorbereiten",
            state="waiting_for_approval",
        )
        db.add(task)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=office.tenant,
            agent_id=agent.id,
            department_id=sales.id,
            task_id=task.id,
            action_type="tool_send",
            status="pending",
            title="Angebot Gartenholz GmbH",
            detail="4 Positionen",
            amount_text="4.320,00 EUR",
            payload={
                "tool": "odoo.send_quotation",
                "arguments": {"partner": "Gartenholz GmbH", "total": "4320.00"},
                # Written by `_announce`. Nobody's business on a screen.
                "channel_handles": [SECRET_HANDLE],
                "internal_note": "raised by the executor, step 7",
            },
        )
        db.add(approval)

        run = m.AgentRun(
            tenant_id=office.tenant,
            agent_id=agent.id,
            state="waiting_for_input",
            context={"task": "t", "pending_question": "Welche Rabattstufe?"},
        )
        db.add(run)
        await db.flush()
        clar = m.Clarification(
            tenant_id=office.tenant,
            run_id=run.id,
            agent_id=agent.id,
            question="Welche Rabattstufe?",
            status="open",
        )
        db.add(clar)

        member = m.OrgMember(
            tenant_id=office.tenant,
            subject="hos",
            subject_uuid=_subject_uuid("hos"),
            display_name="Head of Sales",
        )
        db.add(member)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=office.tenant,
                member_id=member.id,
                department_id=sales.id,
                seat_role=SEAT_APPROVER,
            )
        )
        await db.flush()
        office.sales, office.approval = sales.id, approval.id
        office.task, office.clarification = task.id, clar.id
    return office


async def test_the_approval_dto_never_carries_the_payload_wholesale(
    app_session: AppSessionFactory,
) -> None:
    """Two named keys come out of `payload`. Nothing else does.

    Asserted against the RAW response text rather than against the parsed body,
    because a leak would arrive as a new key nobody thought to look up -- and the
    check that only inspects the keys it already knows about is the check that
    misses exactly that.
    """
    office = await _office(app_session)

    async with _http() as http:
        got = await http.get("/api/v1/approvals", headers=_headers(office.tenant, "hos", "member"))
    assert got.status_code == 200, got.text

    assert SECRET_HANDLE not in got.text, (
        "the messenger handles `_announce` writes into payload reached the browser"
    )
    assert "channel_handles" not in got.text and "channelHandles" not in got.text
    assert "internal_note" not in got.text and "internalNote" not in got.text

    row: dict[str, Any] = got.json()[0]
    # And the two that DO travel are really there, so the assertions above are
    # not passing because the serializer dropped the payload entirely.
    assert row["toolName"] == "odoo.send_quotation"
    assert row["toolArguments"] == {"partner": "Gartenholz GmbH", "total": "4320.00"}


async def test_the_approval_dto_names_what_the_detail_pane_has_to_show(
    app_session: AppSessionFactory,
) -> None:
    """Agent, department, task and age -- none of which had a field before.

    Resolved server-side and batched, because the alternative is the screen
    fetching four things per row.
    """
    office = await _office(app_session)

    async with _http() as http:
        got = await http.get("/api/v1/approvals", headers=_headers(office.tenant, "hos", "member"))
    row = got.json()[0]

    assert row["departmentId"] == str(office.sales)
    assert row["departmentName"] == "Vertrieb"
    assert row["agentName"] == "Nora"
    assert row["taskId"] == str(office.task)
    assert row["taskTitle"] == "Angebot Gartenholz GmbH vorbereiten"
    assert row["createdAt"], "there is no timestamp, so the row cannot say how long it has waited"
    assert row["decidedByName"] == ""
    # `time` is untouched by this slice: it comes from `payload["time"]`, which
    # only the demo seed writes, and a client reading it must not see it change
    # in the same release that gives it `createdAt`.
    assert row["time"] == ""


async def test_a_decided_approval_names_the_person_who_decided_it(
    app_session: AppSessionFactory,
) -> None:
    """`decided_by` is declared in migration 0001 and was assigned nowhere in
    `src/` until this slice, so "entschieden von" had nothing behind it."""
    office = await _office(app_session)
    headers = _headers(office.tenant, "hos", "member")

    async with _http() as http:
        decided = await http.post(
            f"/api/v1/approvals/{office.approval}/decision",
            json={"decision": "approve"},
            headers=headers,
        )
        assert decided.status_code == 200, decided.text

        listed = await http.get("/api/v1/approvals?status=approved", headers=headers)
    row = listed.json()[0]
    assert row["decidedByName"] == "Head of Sales"


async def test_the_clarification_dto_carries_the_agents_face_and_its_department(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)

    async with _http() as http:
        got = await http.get(
            "/api/v1/clarifications", headers=_headers(office.tenant, "hos", "member")
        )
    assert got.status_code == 200, got.text
    row = got.json()[0]
    assert row["id"] == str(office.clarification)
    assert row["agentName"] == "Nora"
    assert row["departmentId"] == str(office.sales)
    assert row["departmentName"] == "Vertrieb"
    assert row["status"] == "open"
    assert row["createdAt"]


async def test_asking_for_answered_questions_is_refused_not_quietly_ignored(
    app_session: AppSessionFactory,
) -> None:
    """`open_clarifications` is the OPEN queue by name. Serving it for
    `?status=answered` would put a list on screen that contradicts the filter
    above it, and nobody ever looks at the endpoint again."""
    office = await _office(app_session)

    async with _http() as http:
        got = await http.get(
            "/api/v1/clarifications?status=answered",
            headers=_headers(office.tenant, "hos", "member"),
        )
    assert got.status_code == 400, got.text
    assert "open" in got.text


async def test_me_does_not_500_for_a_principal_that_cannot_hold_a_seat(
    app_session: AppSessionFactory,
) -> None:
    """`scope_for_principal` refuses anything that is not an operator, by KIND.

    `/me` is how a caller finds out what it is, so it has to answer a plugin token
    rather than raise at it -- and it has to answer honestly: no member, no seats,
    and not "sees every department".
    """
    tenant = uuid.uuid4()
    token = get_identity_provider().mint(
        tenant_id=tenant, subject="plugin-1", role="plugin", kind="plugin"
    )
    async with _http() as http:
        got = await http.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["kind"] == "plugin"
    assert body["memberId"] is None
    assert body["seats"] == []
    assert body["viewsAllDepartments"] is False


async def test_governance_explains_a_refusal_without_minting_anybody(
    app_session: AppSessionFactory,
) -> None:
    """It carries no permission because it exists for the caller it just refused.

    It also must not WRITE: a diagnostic read that mints a person would make
    "everybody this tenant knows" include everybody who ever loaded the governance
    screen, which is the list an administrator uses to decide who to offboard.
    """
    office = await _office(app_session)

    async with _http() as http:
        seated = await http.get(
            "/api/v1/governance", headers=_headers(office.tenant, "hos", "member")
        )
        assert seated.status_code == 200, seated.text
        assert [s["departmentName"] for s in seated.json()["seats"]] == ["Vertrieb"]
        assert seated.json()["callerPermissions"] == ["connection:own", "copilot:use"], (
            "a seat is authority somewhere, not tenant-wide; callerPermissions is "
            "what the ROLE grants and must not quietly gain the seat's four"
        )
        assert set(seated.json()["seatRoles"]) == {"dept_viewer", "dept_approver"}

        stranger = await http.get(
            "/api/v1/governance", headers=_headers(office.tenant, "never-seen", "menber")
        )
        assert stranger.status_code == 200, stranger.text
        assert stranger.json()["seats"] == []
        assert stranger.json()["callerRoleIsKnown"] is False

        listed = await http.get(
            "/api/v1/members", headers=_headers(office.tenant, "boss", "org_admin")
        )
    assert "never-seen" not in {r["subject"] for r in listed.json()["items"]}, (
        "the governance screen minted a member row for somebody who only read it"
    )
