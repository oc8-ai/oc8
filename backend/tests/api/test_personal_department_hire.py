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
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.hire import HIRED_STATUS, set_require_hire_approval
from oc8.auth import get_identity_provider
from oc8.authz.permissions import COPILOT_USE
from oc8.authz.scope import subject_uuid_for
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.departments.personal import ENABLED_KEY, MAX_AGENTS_KEY
from oc8.main import create_app
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SUBJECT = "ada"


def _headers(tenant: uuid.UUID, subject: str = _SUBJECT, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


async def _token(
    http: AsyncClient, tenant: uuid.UUID, cid: uuid.UUID | str, subject: str = _SUBJECT
) -> str:
    """The `hireToken` the templates list shows for `cid`; a placeholder for
    a template the list does not show (the hire refuses it before the token)."""
    listed = await http.get(
        "/api/v1/personal-department/templates", headers=_headers(tenant, subject)
    )
    if listed.status_code != 200:
        return "unlisted"
    found = [t["hireToken"] for t in listed.json() if t["capaId"] == str(cid)]
    return str(found[0]) if found else "unlisted"


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _template(
    name: str, *, scope: str | None = "personal", version: str = "1.0.0", **extra: Any
) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": "Mia", "mission": "Keep the owner's day in order."}
    if scope is not None:
        spec["scope"] = scope
    return {
        "name": name,
        "version": version,
        "type": "agent_template",
        "trust": "first_party",
        "summary": f"{name} summary",
        "agent_template": spec,
        **extra,
    }


async def _tenant(
    app_session: AppSessionFactory,
    *,
    enabled: bool = True,
    hire_approval: bool = False,
    settings: dict[str, Any] | None = None,
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
                settings={ENABLED_KEY: enabled, **(settings or {})},
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
    still starts ready (`HIRED_STATUS`, like any ungated template hire), never
    `pending_approval`, with no hire request behind it: a personal
    department is never gated (`agents.hire.hire_is_gated`)."""
    tenant = await _tenant(app_session, hire_approval=True)
    cid = await _capa(app_session, tenant, _template("mail_helper"))

    async with _http() as http:
        r = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid)},
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
        assert agent.status == HIRED_STATUS
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
            json={
                "capaId": str(cid),
                "hireToken": await _token(http, tenant, cid),
                "name": "  Max  ",
            },
            headers=_headers(tenant),
        )
        assert named.status_code == 201, named.text
        blank = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid), "name": "   "},
            headers=_headers(tenant),
        )
        assert blank.status_code == 201, blank.text
        too_long = await http.post(
            "/api/v1/personal-department/agents",
            json={
                "capaId": str(cid),
                "hireToken": await _token(http, tenant, cid),
                "name": "x" * 201,
            },
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
            json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid)},
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
            json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid)},
            headers=_headers(tenant),
        )
        assert r.status_code == 404, r.text
        missing = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(uuid.uuid4()), "hireToken": "unlisted"},
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
            json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid)},
            headers=_headers(tenant),
        )
        assert first.status_code == 201, first.text

        await _set_enabled(app_session, tenant, False)

        again = await http.post(
            "/api/v1/personal-department/agents",
            json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid)},
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
            json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid)},
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
    (listed,) = r.json()
    assert listed.pop("hireToken")
    assert listed == {
        "capaId": str(personal),
        "name": "mail_helper",
        "label": "Mail Helper",
        "summary": "mail_helper summary",
        "grants": {"tools": [], "skills": []},
    }


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
            ("POST", "/api/v1/personal-department/agents", {"capaId": str(cid), "hireToken": "x"}),
        ):
            r = await http.request(method, path, json=body, headers=_headers(tenant))
            assert r.status_code == 403, (path, r.text)
    assert await _personal_departments(app_session, tenant) == []


async def _hire(
    http: AsyncClient, tenant: uuid.UUID, cid: uuid.UUID, subject: str = _SUBJECT
) -> Any:
    return await http.post(
        "/api/v1/personal-department/agents",
        json={"capaId": str(cid), "hireToken": await _token(http, tenant, cid, subject)},
        headers=_headers(tenant, subject),
    )


async def test_only_the_enabled_version_counts_not_a_newer_installed_one(
    app_session: AppSessionFactory,
) -> None:
    """v1 (department scope) is enabled; v2 (personal) is only installed, so
    it is `Capa.current_version_id` but not what the admin enabled."""
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("helper", scope=None))
    await _capa(app_session, tenant, _template("helper", version="2.0.0"), enable=False)

    async with _http() as http:
        listed = await http.get("/api/v1/personal-department/templates", headers=_headers(tenant))
        assert listed.status_code == 200, listed.text
        assert listed.json() == []
        r = await _hire(http, tenant, cid)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "template_not_personal"}
    assert await _personal_departments(app_session, tenant) == []


async def test_the_enabled_personal_version_stays_hireable_under_a_newer_install(
    app_session: AppSessionFactory,
) -> None:
    """The reverse: v1 is personal and enabled, v2 is not personal and only
    installed. The member still gets v1."""
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("helper"))
    await _capa(app_session, tenant, _template("helper", scope=None, version="2.0.0"), enable=False)

    async with _http() as http:
        listed = await http.get("/api/v1/personal-department/templates", headers=_headers(tenant))
        assert [t["capaId"] for t in listed.json()] == [str(cid)]
        r = await _hire(http, tenant, cid)
    assert r.status_code == 201, r.text
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, uuid.UUID(r.json()["agentId"]))
        assert agent is not None and agent.definition["version"] == "1.0.0"


async def test_a_member_hires_at_most_the_tenant_cap(app_session: AppSessionFactory) -> None:
    tenant = await _tenant(app_session, settings={MAX_AGENTS_KEY: 2})
    cid = await _capa(app_session, tenant, _template("helper"))

    async with _http() as http:
        assert (await _hire(http, tenant, cid)).status_code == 201
        second = await _hire(http, tenant, cid)
        assert second.status_code == 201, second.text
        third = await _hire(http, tenant, cid)
        assert third.status_code == 409, third.text
        assert third.json()["detail"] == {"error": "personal_agent_limit"}

    async with app_session(tenant) as db:
        # A deleted agent frees its place.
        agent = await db.get(m.Agent, uuid.UUID(second.json()["agentId"]))
        assert agent is not None
        agent.deleted_at = func.now()

    async with _http() as http:
        assert (await _hire(http, tenant, cid)).status_code == 201


async def test_two_members_each_get_their_own_department(app_session: AppSessionFactory) -> None:
    tenant = await _tenant(app_session)
    cid = await _capa(app_session, tenant, _template("helper"))

    async with _http() as http:
        a = await _hire(http, tenant, cid, "ada")
        b = await _hire(http, tenant, cid, "bob")
        assert a.status_code == 201, a.text
        assert b.status_code == 201, b.text
        assert a.json()["departmentId"] != b.json()["departmentId"]

        mine_a = await http.get("/api/v1/personal-department", headers=_headers(tenant, "ada"))
        mine_b = await http.get("/api/v1/personal-department", headers=_headers(tenant, "bob"))
        assert mine_a.json()["department"]["id"] == a.json()["departmentId"]
        assert mine_b.json()["department"]["id"] == b.json()["departmentId"]

        listed_a = await http.get(
            f"/api/v1/agents?departmentId={a.json()['departmentId']}",
            headers=_headers(tenant, "ada"),
        )
        assert [x["id"] for x in listed_a.json()["items"]] == [a.json()["agentId"]]

    async with app_session(tenant) as db:
        agent_a = await db.get(m.Agent, uuid.UUID(a.json()["agentId"]))
        agent_b = await db.get(m.Agent, uuid.UUID(b.json()["agentId"]))
        assert agent_a is not None and str(agent_a.department_id) == a.json()["departmentId"]
        assert agent_b is not None and str(agent_b.department_id) == b.json()["departmentId"]


async def test_a_template_outside_the_personal_frame_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _tenant(app_session)
    manifest = _template("helper")
    manifest["agent_template"]["narrowing"] = {"tools": {"crm.write": {"enabled": True}}}
    cid = await _capa(app_session, tenant, manifest)

    async with _http() as http:
        r = await _hire(http, tenant, cid)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "the template grants tools the personal department does not allow"
    async with app_session(tenant) as db:
        agents = (await db.execute(select(m.Agent).where(m.Agent.tenant_id == tenant))).all()
        assert agents == []
