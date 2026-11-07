"""A member's own guardrails per agent (D3a): the `me` routes, the read-only
view for whoever manages the agent, the Copilot overview, and pruning a member
entry once the agent's own guardrails cover it (plan Review Focus 5)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.agents.versioning import publish_version
from oc8.auth import get_identity_provider
from oc8.authz.member_guardrail_store import _adds_nothing, prune_redundant, set_member_tool
from oc8.authz.pdp import ToolPolicy
from oc8.authz.permissions import SEAT_VIEWER
from oc8.authz.scope import subject_uuid_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

GMAIL_FULL: dict[str, Any] = {"enabled": True, "read": True, "modify": True}


@dataclass
class Office:
    tenant: uuid.UUID
    sales: uuid.UUID
    agent: uuid.UUID
    anna: uuid.UUID
    olaf: uuid.UUID


def _member(tenant: uuid.UUID, subject: str, **kw: Any) -> m.OrgMember:
    return m.OrgMember(
        tenant_id=tenant, subject=subject, subject_uuid=subject_uuid_for(subject), **kw
    )


async def _office(app_session: AppSessionFactory) -> Office:
    """Sales grants gmail in full; Nora works there. Anna holds a viewer seat
    in Sales, Olaf holds no seat at all, the admin holds everything."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sales = m.Department(tenant_id=tenant, name="Sales", frame={"tools": {"gmail": GMAIL_FULL}})
        db.add(sales)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant, department_id=sales.id, name="Nora", narrowing={}, definition={}
        )
        anna = _member(tenant, "anna", display_name="Anna")
        olaf = _member(tenant, "olaf", display_name="Olaf")
        admin = _member(tenant, "admin", display_name="Admin")
        db.add_all([agent, anna, olaf, admin])
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant, member_id=anna.id, department_id=sales.id, seat_role=SEAT_VIEWER
            )
        )
        await db.flush()
        return Office(tenant, sales.id, agent.id, anna.id, olaf.id)


def _headers(tenant: uuid.UUID, subject: str, role: str = "member") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _rows(app_session: AppSessionFactory, office: Office) -> list[m.MemberGuardrail]:
    async with app_session(office.tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.MemberGuardrail).where(m.MemberGuardrail.agent_id == office.agent)
                )
            )
            .scalars()
            .all()
        )


def _tool(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(t for t in body["tools"] if t["toolKey"] == key)


# ------------------------------------------------------------------ unit


def test_a_rule_the_agent_already_has_adds_nothing() -> None:
    base = {"gmail": ToolPolicy(enabled=True, read=True, modify=False)}
    read_only = {"enabled": True, "read": True, "modify": False}
    assert _adds_nothing(base, "gmail", read_only) is True


def test_asking_first_adds_something_when_the_agent_does_not_ask() -> None:
    base = {"gmail": ToolPolicy(enabled=True, read=True, modify=True)}
    ask_first = {"enabled": True, "read": True, "modify": True, "approval_actions": ["modify"]}
    assert _adds_nothing(base, "gmail", ask_first) is False


# ------------------------------------------------------------------ me


async def test_my_guardrails_show_the_agents_own_level(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    async with _http() as http:
        r = await http.get(
            f"/api/v1/agents/{office.agent}/guardrails/me", headers=_headers(office.tenant, "anna")
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agentId"] == str(office.agent)
    assert body["agentName"] == "Nora"
    gmail = _tool(body, "gmail")
    assert gmail["baseLevel"] == "allowed"
    assert gmail["level"] == "allowed"
    assert gmail["mine"] is None
    assert gmail["hasLimits"] is False
    assert gmail["connectionName"] == "gmail"
    lines = {line["code"]: line["params"] for line in body["summary"]}
    assert lines["may_modify"]["tools"] == ["gmail"]


async def test_picking_ask_first_stores_my_rule(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "ask_first"},
            headers=_headers(office.tenant, "anna"),
        )
    assert r.status_code == 200, r.text
    gmail = _tool(r.json(), "gmail")
    assert gmail["level"] == "ask_first"
    assert gmail["baseLevel"] == "allowed"
    assert gmail["source"] == "member"
    assert gmail["mine"]["approvalActions"] == ["modify"]
    lines = {line["code"]: line["params"] for line in r.json()["summary"]}
    assert lines["asks_you"]["tools"] == ["gmail"]
    rows = await _rows(app_session, office)
    assert len(rows) == 1
    assert rows[0].member_id == office.anna
    assert rows[0].policy["tools"]["gmail"]["approval_actions"] == ["modify"]


async def test_picking_allowed_removes_my_rule(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    anna = _headers(office.tenant, "anna")
    url = f"/api/v1/agents/{office.agent}/guardrails/me/gmail"
    async with _http() as http:
        assert (await http.put(url, json={"level": "read_only"}, headers=anna)).status_code == 200
        r = await http.put(url, json={"level": "allowed"}, headers=anna)
    assert r.status_code == 200, r.text
    assert _tool(r.json(), "gmail")["mine"] is None
    assert await _rows(app_session, office) == []


async def test_deleting_my_rule_removes_it(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    anna = _headers(office.tenant, "anna")
    url = f"/api/v1/agents/{office.agent}/guardrails/me/gmail"
    async with _http() as http:
        assert (await http.put(url, json={"level": "off"}, headers=anna)).status_code == 200
        r = await http.delete(url, headers=anna)
    assert r.status_code == 200, r.text
    assert _tool(r.json(), "gmail")["level"] == "allowed"
    assert await _rows(app_session, office) == []


async def test_a_rule_that_would_widen_is_refused(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"policy": {**GMAIL_FULL, "connection_id": "x"}},
            headers=_headers(office.tenant, "anna"),
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "member_guardrail_widens"
    assert r.json()["detail"]["violations"]
    assert await _rows(app_session, office) == []


async def test_a_write_only_tool_asks_first_without_adding_read(
    app_session: AppSessionFactory,
) -> None:
    """Sales lets the agent send mail but not read it: "ask me first" stays
    write-only, and "read only" -- which would add read -- is refused."""
    office = await _office(app_session)
    async with app_session(office.tenant) as db:
        sales = await db.get(m.Department, office.sales)
        assert sales is not None
        sales.frame = {"tools": {"gmail": {"enabled": True, "read": False, "modify": True}}}
    anna = _headers(office.tenant, "anna")
    url = f"/api/v1/agents/{office.agent}/guardrails/me/gmail"
    async with _http() as http:
        r = await http.put(url, json={"level": "ask_first"}, headers=anna)
        assert r.status_code == 200, r.text
        assert _tool(r.json(), "gmail")["level"] == "ask_first"
        refused = await http.put(url, json={"level": "read_only"}, headers=anna)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["error"] == "member_guardrail_widens"
    rows = await _rows(app_session, office)
    assert rows[0].policy["tools"]["gmail"] == {
        "enabled": True,
        "read": False,
        "modify": True,
        "approval_actions": ["modify"],
    }


async def test_a_tool_the_agent_does_not_have_is_refused(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/calendar",
            json={"level": "read_only"},
            headers=_headers(office.tenant, "anna"),
        )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "member_guardrail_widens"


async def test_level_and_policy_are_exclusive(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    url = f"/api/v1/agents/{office.agent}/guardrails/me/gmail"
    anna = _headers(office.tenant, "anna")
    async with _http() as http:
        both = await http.put(
            url, json={"level": "off", "policy": {"enabled": False}}, headers=anna
        )
        neither = await http.put(url, json={}, headers=anna)
    assert both.status_code == 422
    assert neither.status_code == 422


async def test_an_agent_i_cannot_work_with_is_not_found(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    olaf = _headers(office.tenant, "olaf")
    async with _http() as http:
        hidden = await http.get(f"/api/v1/agents/{office.agent}/guardrails/me", headers=olaf)
        missing = await http.get(f"/api/v1/agents/{uuid.uuid4()}/guardrails/me", headers=olaf)
        write = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "off"},
            headers=olaf,
        )
    assert hidden.status_code == 404, hidden.text
    assert missing.status_code == 404
    assert write.status_code == 404
    assert await _rows(app_session, office) == []


async def test_a_member_without_a_seat_sets_rules_on_their_copilot(
    app_session: AppSessionFactory,
) -> None:
    """Olaf can chat with the tenant's Copilot without any seat, so he can set
    his own guardrails on it too -- the same gate chat uses."""
    office = await _office(app_session)
    async with app_session(office.tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=office.tenant)
        assistant_id = assistant.id
    async with _http() as http:
        r = await http.get(
            f"/api/v1/agents/{assistant_id}/guardrails/me", headers=_headers(office.tenant, "olaf")
        )
    assert r.status_code == 200, r.text
    assert r.json()["agentId"] == str(assistant_id)


# ------------------------------------------------------------------ members


async def test_only_whoever_manages_the_agent_sees_members_rules(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    async with _http() as http:
        set_ = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "read_only"},
            headers=_headers(office.tenant, "anna"),
        )
        assert set_.status_code == 200, set_.text
        url = f"/api/v1/agents/{office.agent}/guardrails/members"
        refused = await http.get(url, headers=_headers(office.tenant, "anna"))
        listed = await http.get(url, headers=_headers(office.tenant, "admin", "org_admin"))
        no_write = await http.put(
            f"{url}/{office.anna}/gmail",
            json={"level": "off"},
            headers=_headers(office.tenant, "admin", "org_admin"),
        )
    assert refused.status_code == 403, refused.text
    assert listed.status_code == 200, listed.text
    [entry] = listed.json()
    assert entry["memberId"] == str(office.anna)
    assert entry["memberName"] == "Anna"
    assert list(entry["tools"]) == ["gmail"]
    assert entry["tools"]["gmail"]["modify"] is False
    assert no_write.status_code in (404, 405)


# ------------------------------------------------------------------ pruning


async def test_a_rule_the_agent_takes_over_is_pruned_for_good(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    admin = _headers(office.tenant, "admin", "org_admin")
    narrowing_url = f"/api/v1/agents/{office.agent}/narrowing"
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "read_only"},
            headers=_headers(office.tenant, "anna"),
        )
        assert r.status_code == 200, r.text
        tightened = await http.put(
            narrowing_url,
            json={"narrowing": {"tools": {"gmail": {**GMAIL_FULL, "modify": False}}}},
            headers=admin,
        )
        assert tightened.status_code == 200, tightened.text
        assert await _rows(app_session, office) == []
        loosened = await http.put(
            narrowing_url, json={"narrowing": {"tools": {"gmail": GMAIL_FULL}}}, headers=admin
        )
        assert loosened.status_code == 200, loosened.text
    assert await _rows(app_session, office) == []
    async with app_session(office.tenant) as db:
        lines = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.action == "member_guardrail.pruned")
                )
            )
            .scalars()
            .all()
        )
    assert len(lines) == 1
    assert lines[0].resource["member_id"] == str(office.anna)
    assert lines[0].resource["tools"] == ["gmail"]
    assert lines[0].resource["agent_id"] == str(office.agent)


async def test_a_department_change_prunes_too(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "off"},
            headers=_headers(office.tenant, "anna"),
        )
        assert r.status_code == 200, r.text
        dept = await http.put(
            f"/api/v1/departments/{office.sales}/tools",
            json={"tools": {"gmail": {"enabled": False, "read": False, "modify": False}}},
            headers=_headers(office.tenant, "admin", "org_admin"),
        )
        assert dept.status_code == 200, dept.text
    assert await _rows(app_session, office) == []


async def test_a_rule_for_a_withdrawn_tool_waits_for_it_to_return(
    app_session: AppSessionFactory,
) -> None:
    """Ruling 9: a withdrawn tool is not "redundant". The member's entry stays
    (inert while neither department nor agent grants the tool) and protects
    again once the department grants the tool back."""
    office = await _office(app_session)
    anna = _headers(office.tenant, "anna")
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "ask_first"},
            headers=anna,
        )
        assert r.status_code == 200, r.text
    async with app_session(office.tenant) as db:
        sales = await db.get(m.Department, office.sales)
        agent = await db.get(m.Agent, office.agent)
        assert sales is not None and agent is not None
        sales.frame = {"tools": {}}
        agent.narrowing = {}
        await db.flush()
        assert await prune_redundant(db, tenant_id=office.tenant, agent=agent) == []
    rows = await _rows(app_session, office)
    assert len(rows) == 1
    assert rows[0].policy["tools"]["gmail"]["approval_actions"] == ["modify"]
    async with app_session(office.tenant) as db:
        sales = await db.get(m.Department, office.sales)
        assert sales is not None
        sales.frame = {"tools": {"gmail": GMAIL_FULL}}
    async with _http() as http:
        mine = await http.get(f"/api/v1/agents/{office.agent}/guardrails/me", headers=anna)
    assert mine.status_code == 200, mine.text
    gmail = _tool(mine.json(), "gmail")
    assert gmail["level"] == "ask_first"
    assert gmail["source"] == "member"


async def test_a_rule_the_agent_still_lacks_survives(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "ask_first"},
            headers=_headers(office.tenant, "anna"),
        )
        assert r.status_code == 200, r.text
        tightened = await http.put(
            f"/api/v1/agents/{office.agent}/narrowing",
            json={"narrowing": {"tools": {"gmail": GMAIL_FULL}}},
            headers=_headers(office.tenant, "admin", "org_admin"),
        )
        assert tightened.status_code == 200, tightened.text
    rows = await _rows(app_session, office)
    assert len(rows) == 1


# ------------------------------------------------------------------ overview


async def test_the_overview_lists_only_agents_i_have_rules_on(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    async with app_session(office.tenant) as db:
        other = m.Agent(
            tenant_id=office.tenant,
            department_id=office.sales,
            name="Otto",
            narrowing={},
            definition={},
        )
        db.add(other)
    anna = _headers(office.tenant, "anna")
    async with _http() as http:
        empty = await http.get("/api/v1/copilot/guardrails", headers=anna)
        assert empty.status_code == 200, empty.text
        assert empty.json() == []
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "read_only"},
            headers=anna,
        )
        assert r.status_code == 200, r.text
        listed = await http.get("/api/v1/copilot/guardrails", headers=anna)
        olaf_sees = await http.get(
            "/api/v1/copilot/guardrails", headers=_headers(office.tenant, "olaf")
        )
    assert listed.status_code == 200, listed.text
    assert [a["agentId"] for a in listed.json()] == [str(office.agent)]
    assert olaf_sees.json() == []


# ------------------------------------------------------------------ concurrency

READ_ONLY: dict[str, Any] = {"enabled": True, "read": True, "modify": False}


async def _two_tools(app_session: AppSessionFactory, office: Office) -> None:
    async with app_session(office.tenant) as db:
        dept = await db.get(m.Department, office.sales)
        assert dept is not None
        dept.frame = {"tools": {"gmail": GMAIL_FULL, "calendar": GMAIL_FULL}}


async def _set_in_own_session(
    app_session: AppSessionFactory, office: Office, tool_key: str
) -> None:
    async with app_session(office.tenant) as db:
        member = await db.get(m.OrgMember, office.anna)
        agent = await db.get(m.Agent, office.agent)
        assert member is not None and agent is not None
        await set_member_tool(
            db,
            tenant_id=office.tenant,
            member=member,
            agent=agent,
            tool_key=tool_key,
            entry=READ_ONLY,
        )


async def test_two_first_rules_at_once_both_land(app_session: AppSessionFactory) -> None:
    """Two tabs, two tools, no row yet: the second insert must merge into the
    first, not die on the unique constraint."""
    office = await _office(app_session)
    await _two_tools(app_session, office)
    async with app_session(office.tenant) as first:
        member = await first.get(m.OrgMember, office.anna)
        agent = await first.get(m.Agent, office.agent)
        assert member is not None and agent is not None
        await set_member_tool(
            first,
            tenant_id=office.tenant,
            member=member,
            agent=agent,
            tool_key="gmail",
            entry=READ_ONLY,
        )
        second = asyncio.create_task(_set_in_own_session(app_session, office, "calendar"))
        await asyncio.sleep(0.3)
    await second
    [row] = await _rows(app_session, office)
    assert sorted(row.policy["tools"]) == ["calendar", "gmail"]


async def test_two_rules_at_once_over_http_both_land(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    await _two_tools(app_session, office)
    anna = _headers(office.tenant, "anna")
    base = f"/api/v1/agents/{office.agent}/guardrails/me"
    async with _http() as http:
        results = await asyncio.gather(
            http.put(f"{base}/gmail", json={"level": "read_only"}, headers=anna),
            http.put(f"{base}/calendar", json={"level": "read_only"}, headers=anna),
        )
    assert [r.status_code for r in results] == [200, 200], [r.text for r in results]
    [row] = await _rows(app_session, office)
    assert sorted(row.policy["tools"]) == ["calendar", "gmail"]


async def test_a_prune_never_drops_a_rule_set_meanwhile(app_session: AppSessionFactory) -> None:
    """The admin's save prunes gmail while Anna adds calendar: the prune holds
    the row, Anna's write waits for it and then merges into what is left."""
    office = await _office(app_session)
    await _two_tools(app_session, office)
    await _set_in_own_session(app_session, office, "gmail")
    async with app_session(office.tenant) as admin:
        agent = await admin.get(m.Agent, office.agent)
        assert agent is not None
        agent.narrowing = {"tools": {"gmail": READ_ONLY}}
        await admin.flush()
        removed = await prune_redundant(admin, tenant_id=office.tenant, agent=agent)
        assert removed == [(office.anna, "gmail")]
        anna = asyncio.create_task(_set_in_own_session(app_session, office, "calendar"))
        await asyncio.sleep(0.3)
    await anna
    [row] = await _rows(app_session, office)
    assert sorted(row.policy["tools"]) == ["calendar"]


# ------------------------------------------------------------------ rollback


async def test_rolling_back_to_a_tighter_version_prunes(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    async with app_session(office.tenant) as db:
        agent = await db.get(m.Agent, office.agent)
        assert agent is not None
        agent.narrowing = {"tools": {"gmail": READ_ONLY}}
        await db.flush()
        tight = await publish_version(db, agent, note="read only")
        agent.narrowing = {"tools": {"gmail": GMAIL_FULL}}
        await db.flush()
        await publish_version(db, agent, note="full")
        tight_no = tight.version_no
    async with _http() as http:
        r = await http.put(
            f"/api/v1/agents/{office.agent}/guardrails/me/gmail",
            json={"level": "read_only"},
            headers=_headers(office.tenant, "anna"),
        )
        assert r.status_code == 200, r.text
        assert len(await _rows(app_session, office)) == 1
        rolled = await http.post(
            f"/api/v1/agents/{office.agent}/versions/{tight_no}/rollback",
            headers=_headers(office.tenant, "admin", "org_admin"),
        )
        assert rolled.status_code == 201, rolled.text
    assert await _rows(app_session, office) == []
