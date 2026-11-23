"""The member routes of a personal department: `GET /personal-department`,
`GET /personal-department/templates` and `POST /personal-department/agents`.

All three are gated by `department:own`, which the built-in `member` role
holds, so a bare member -- no seat, no `plugin:manage` -- can hire a personal
template into their own personal department. Every test mints its own tenant.
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
from oc8.authz.permissions import COPILOT_USE
from oc8.authz.scope import subject_uuid_for
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.departments.personal import ENABLED_KEY
from oc8.main import create_app
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SUBJECT = "ada"


def _headers(tenant: uuid.UUID, subject: str = _SUBJECT, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _template(name: str, *, scope: str | None = "personal", **extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": "Mia", "mission": "Keep the owner's day in order."}
    if scope is not None:
        spec["scope"] = scope
    return {
        "name": name,
        "version": "1.0.0",
        "type": "agent_template",
        "trust": "first_party",
        "summary": f"{name} summary",
        "agent_template": spec,
        **extra,
    }


async def _tenant(
    app_session: AppSessionFactory, *, enabled: bool = True, hire_approval: bool = False
) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:8]}",
                name="T",
                tier="standard",
                region="eu",
                settings={ENABLED_KEY: enabled},
            )
        )
        await db.flush()
        await set_require_hire_approval(db, tenant_id=tenant, enabled=hire_approval)
    return tenant


async def _capa(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    manifest: dict[str, Any],
    *,
    enable: bool = True,
) -> uuid.UUID:
    async with app_session(tenant) as db:
        version = await install_plugin(db, tenant_id=tenant, manifest_data=manifest)
        if enable:
            await enable_plugin(
                db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[]
            )
        return version.capa_id


async def _set_enabled(app_session: AppSessionFactory, tenant: uuid.UUID, enabled: bool) -> None:
    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        assert org is not None
        org.settings = {**org.settings, ENABLED_KEY: enabled}


async def _personal_departments(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> list[m.Department]:
    async with app_session(tenant) as db:
        rows = await db.execute(
            select(m.Department).where(
                m.Department.tenant_id == tenant,
                m.Department.kind == DEPARTMENT_KIND_PERSONAL,
            )
        )
        return list(rows.scalars().all())


async def test_a_bare_member_hires_a_personal_template(app_session: AppSessionFactory) -> None:
    """Ruling 4: a personal hire is exempt from `require_hire_approval` -- the
    admin enabling the capa is the approval. The flag is on here, and the agent
    is still a plain stopped agent with no hire request behind it."""
    tenant = await _tenant(app_session, hire_approval=True)
    cid = await _capa(app_session, tenant, _template("mail_helper"))

    async with _http() as http:
        r = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid)},
            headers=_headers(tenant),
        )
        assert r.status_code == 201, r.text
        agent_id = uuid.UUID(r.json()["agentId"])
        dept_id = uuid.UUID(r.json()["departmentId"])

        mine = await http.get("/api/v1/personal-department", headers=_headers(tenant))
        assert mine.status_code == 200, mine.text
        assert mine.json()["enabled"] is True
        assert mine.json()["department"]["id"] == str(dept_id)
        assert mine.json()["department"]["kind"] == "personal"

        listed = await http.get(f"/api/v1/agents?departmentId={dept_id}", headers=_headers(tenant))
        assert listed.status_code == 200, listed.text
        assert [a["id"] for a in listed.json()["items"]] == [str(agent_id)]

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        assert agent.department_id == dept_id
        assert agent.name == "Mia"
        assert agent.status == "stopped"
        member = (
            await db.execute(
                select(m.OrgMember).where(
                    m.OrgMember.tenant_id == tenant, m.OrgMember.subject == _SUBJECT
                )
            )
        ).scalar_one()
        dept = await db.get(m.Department, dept_id)
        assert dept is not None and dept.owner_member_id == member.id
        hire_requests = (
            (
                await db.execute(
                    select(m.ApprovalRequest).where(
                        m.ApprovalRequest.tenant_id == tenant,
                        m.ApprovalRequest.action_type == "hire_agent",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert hire_requests == []
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "agent.created",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].category == "admin"
        assert events[0].resource == {
            "agent_id": str(agent_id),
            "department_id": str(dept_id),
            "personal": True,
        }


async def test_the_member_names_the_agent_within_limits(app_session: AppSessionFactory) -> None:
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("mail_helper"))

    async with _http() as http:
        named = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid), "name": "  Max  "},
            headers=_headers(tenant),
        )
        assert named.status_code == 201, named.text
        blank = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid), "name": "   "},
            headers=_headers(tenant),
        )
        assert blank.status_code == 201, blank.text
        too_long = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid), "name": "x" * 201},
            headers=_headers(tenant),
        )
        assert too_long.status_code == 422, too_long.text

    async with app_session(tenant) as db:
        a = await db.get(m.Agent, uuid.UUID(named.json()["agentId"]))
        b = await db.get(m.Agent, uuid.UUID(blank.json()["agentId"]))
        assert a is not None and a.name == "Max"
        assert b is not None and b.name == "Mia"


async def test_a_non_personal_template_is_refused(app_session: AppSessionFactory) -> None:
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("shared_helper", scope=None))

    async with _http() as http:
        r = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid)},
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "template_not_personal"}
    assert await _personal_departments(app_session, tenant) == []


async def test_a_disabled_capa_is_not_offered_or_hireable(app_session: AppSessionFactory) -> None:
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("mail_helper"), enable=False)

    async with _http() as http:
        listed = await http.get("/api/v1/personal-department/templates", headers=_headers(tenant))
        assert listed.status_code == 200, listed.text
        assert listed.json() == []
        r = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid)},
            headers=_headers(tenant),
        )
        assert r.status_code == 404, r.text
        missing = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(uuid.uuid4())},
            headers=_headers(tenant),
        )
        assert missing.status_code == 404, missing.text
    assert await _personal_departments(app_session, tenant) == []


async def test_disabled_refuses_new_hires_but_keeps_existing(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("mail_helper"))

    async with _http() as http:
        first = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid)},
            headers=_headers(tenant),
        )
        assert first.status_code == 201, first.text

        await _set_enabled(app_session, tenant, False)

        again = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid)},
            headers=_headers(tenant),
        )
        assert again.status_code == 403, again.text
        assert again.json()["detail"] == {"error": "personal_departments_disabled"}

        mine = await http.get("/api/v1/personal-department", headers=_headers(tenant))
        assert mine.status_code == 200, mine.text
        assert mine.json()["enabled"] is False
        assert mine.json()["department"]["id"] == first.json()["departmentId"]

    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(first.json()["agentId"]))
        assert agent is not None and agent.deleted_at is None


async def test_disabled_refuses_a_first_hire(app_session: AppSessionFactory) -> None:
    tenant = await _tenant(app_session, enabled=False)
    cid = await _capa(app_session, tenant, _template("mail_helper"))

    async with _http() as http:
        r = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid)},
            headers=_headers(tenant),
        )
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == {"error": "personal_departments_disabled"}
        mine = await http.get("/api/v1/personal-department", headers=_headers(tenant))
        assert mine.json() == {"enabled": False, "department": None}
    assert await _personal_departments(app_session, tenant) == []


async def test_templates_lists_only_personal_enabled_agent_templates(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _tenant(app_session)
    personal = await _capa(app_session, tenant, _template("mail_helper", label="Mail Helper"))
    await _capa(app_session, tenant, _template("shared_helper", scope=None))
    await _capa(app_session, tenant, _template("off_helper"), enable=False)
    await _capa(
        app_session,
        tenant,
        {
            "name": "sales_dept",
            "version": "1.0.0",
            "type": "department_template",
            "trust": "first_party",
            "department_template": {"agents": [{"name": "A"}]},
        },
    )

    async with _http() as http:
        r = await http.get("/api/v1/personal-department/templates", headers=_headers(tenant))
    assert r.status_code == 200, r.text
    assert r.json() == [
        {
            "capaId": str(personal),
            "name": "mail_helper",
            "label": "Mail Helper",
            "summary": "mail_helper summary",
        }
    ]


async def test_a_role_without_department_own_is_refused(app_session: AppSessionFactory) -> None:
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("mail_helper"))
    async with app_session(tenant) as db:
        role = m.Role(tenant_id=tenant, name="chat-only", kind="human")
        db.add(role)
        await db.flush()
        db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=COPILOT_USE))
        db.add(
            m.OrgMember(
                tenant_id=tenant,
                subject=_SUBJECT,
                subject_uuid=subject_uuid_for(_SUBJECT),
                role_id=role.id,
            )
        )

    async with _http() as http:
        for method, path, body in (
            ("GET", "/api/v1/personal-department", None),
            ("GET", "/api/v1/personal-department/templates", None),
            ("POST", "/api/v1/personal-department/agents", {"capaId": str(cid)}),
        ):
            r = await http.request(method, path, json=body, headers=_headers(tenant))
            assert r.status_code == 403, (path, r.text)
    assert await _personal_departments(app_session, tenant) == []
