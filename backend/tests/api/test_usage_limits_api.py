"""Usage limits through the admin API: written with a department's tools or an
agent's narrowing (a malformed one is a 422, never a stored policy that later
breaks every run), read back on the agent, and today's usage shown per agent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.agent.usage_limits import take
from oc8.auth import get_identity_provider
from oc8.authz.pdp import UsageLimit
from oc8.main import create_app
from tests.conftest import AppSessionFactory

SENDING = {"tools": ["mail_send", "mail_reply"], "max": 50, "per": "day"}
BAD_LIMITS = [
    {"tools": [], "max": 5},
    {"tools": ["mail_send"], "max": -1},
    {"tools": ["mail_send"], "max": 5, "per": "week"},
    {"tools": ["mail_send"]},
]


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _seed(app_session: AppSessionFactory) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="Postfach",
            frame={"tools": {"email": {"enabled": True, "read": True, "modify": True}}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Mila")
        db.add(agent)
        await db.flush()
        return tenant, dept.id, agent.id


def _email(**extra: Any) -> dict[str, Any]:
    return {"tools": {"email": {"enabled": True, "read": True, "modify": True, **extra}}}


async def test_a_department_limit_is_stored_and_echoed(app_session: AppSessionFactory) -> None:
    tenant, dept_id, _agent = await _seed(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/departments/{dept_id}/tools",
            json=_email(limits=[SENDING]),
            headers=_headers(tenant),
        )
    assert r.status_code == 200, r.text
    assert r.json()["tools"]["email"]["limits"] == [SENDING]


@pytest.mark.parametrize("bad", BAD_LIMITS)
async def test_a_malformed_department_limit_is_a_422(
    app_session: AppSessionFactory, bad: dict[str, Any]
) -> None:
    tenant, dept_id, _agent = await _seed(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/departments/{dept_id}/tools",
            json=_email(limits=[bad]),
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text


async def test_an_agent_limit_is_stored_and_shown_on_the_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant, _dept, agent_id = await _seed(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{agent_id}/narrowing",
            json={"narrowing": _email(limits=[SENDING])},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["narrowingTools"]["email"]["limits"] == [SENDING]
    assert body["effectiveTools"]["email"]["limits"] == [SENDING]


@pytest.mark.parametrize("bad", [*BAD_LIMITS, "fifty", [1]])
async def test_a_malformed_agent_limit_is_a_422_not_a_500(
    app_session: AppSessionFactory, bad: Any
) -> None:
    tenant, _dept, agent_id = await _seed(app_session)
    limits = bad if isinstance(bad, str) else [bad] if isinstance(bad, dict) else bad
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{agent_id}/narrowing",
            json={"narrowing": _email(limits=limits)},
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text


async def test_todays_usage_is_listed_per_agent(app_session: AppSessionFactory) -> None:
    tenant, _dept, agent_id = await _seed(app_session)
    limit = UsageLimit.from_json(SENDING)
    async with app_session(tenant) as db:
        for _ in range(3):
            assert await take(
                db,
                tenant_id=tenant,
                agent_id=agent_id,
                tool_key="email",
                limit=limit,
                now=dt.datetime.now(dt.UTC),
            )
    async with _http() as http:
        r = await http.get(f"/api/v1/agents/{agent_id}/tool-usage", headers=_headers(tenant))
    assert r.status_code == 200, r.text
    [row] = r.json()
    assert row["toolKey"] == "email"
    assert row["tools"] == ["mail_reply", "mail_send"]
    assert (row["per"], row["used"]) == ("day", 3)
    assert row["resetsAt"].endswith(("+00:00", "Z"))


async def test_an_agent_limit_above_the_departments_is_a_422(
    app_session: AppSessionFactory,
) -> None:
    tenant, dept_id, agent_id = await _seed(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/departments/{dept_id}/tools",
            json=_email(limits=[SENDING]),
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text
        r = await http.put(
            f"/api/v1/agents/{agent_id}/narrowing",
            json={"narrowing": _email(limits=[{**SENDING, "max": 80}])},
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text
    assert "The department allows at most 50 per day" in r.text


@pytest.fixture
def _capas(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pathlib import Path

    from oc8.config import get_settings

    monkeypatch.setenv("OC8_CAPAS_PATH", str(Path(__file__).resolve().parents[3] / "capas"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _with_email_connection(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    async with app_session(tenant) as db:
        db.add(
            m.McpConnection(
                tenant_id=tenant,
                name="email",
                server_url="",
                transport="stdio",
                config={"_plugin_name": "email", "_connection_key": "primary"},
            )
        )


TYPO = {"tools": ["mail_sned"], "max": 5}


async def test_a_department_limit_on_an_unknown_tool_is_a_422(
    app_session: AppSessionFactory, _capas: None
) -> None:
    tenant, dept_id, _agent = await _seed(app_session)
    await _with_email_connection(app_session, tenant)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/departments/{dept_id}/tools",
            json=_email(limits=[TYPO]),
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text
    assert "mail_sned" in r.text


async def test_an_agent_limit_on_an_unknown_tool_is_a_422(
    app_session: AppSessionFactory, _capas: None
) -> None:
    tenant, _dept, agent_id = await _seed(app_session)
    await _with_email_connection(app_session, tenant)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{agent_id}/narrowing",
            json={"narrowing": _email(limits=[TYPO])},
            headers=_headers(tenant),
        )
    assert r.status_code == 422, r.text
    assert "mail_sned" in r.text


async def test_usage_is_not_shown_outside_what_the_caller_may_see(
    app_session: AppSessionFactory,
) -> None:
    """Another department's agent (for a caller seated elsewhere) and another
    tenant's agent are a 404, never their counts."""
    from tests.api.test_department_reads_department_scoped import _headers as seat_headers
    from tests.api.test_department_reads_department_scoped import _office

    office = await _office(app_session)
    limit = UsageLimit.from_json(SENDING)
    now = dt.datetime.now(dt.UTC)
    for agent_id in (office.sales_agent, office.engineering_agent):
        async with app_session(office.tenant) as db:
            await take(
                db,
                tenant_id=office.tenant,
                agent_id=agent_id,
                tool_key="email",
                limit=limit,
                now=now,
            )
    seated = seat_headers(office.tenant, office.seated_subject)
    async with _http() as http:
        own = await http.get(f"/api/v1/agents/{office.sales_agent}/tool-usage", headers=seated)
        other_dept = await http.get(
            f"/api/v1/agents/{office.engineering_agent}/tool-usage", headers=seated
        )
        other_tenant = await http.get(
            f"/api/v1/agents/{office.sales_agent}/tool-usage",
            headers=_headers(uuid.uuid4()),
        )
    assert own.status_code == 200, own.text
    assert [row["used"] for row in own.json()] == [1]
    assert other_dept.status_code == 404, other_dept.text
    assert other_tenant.status_code == 404, other_tenant.text
