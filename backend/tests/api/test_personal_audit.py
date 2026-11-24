"""The audit trail keeps every event of a personal agent, never its content.

`PATCH /agents/{id}/instructions` writes the whole mission into the event's
`resource` as `before`/`after`, and `GET /audit` and `/audit/export` returned
`resource` verbatim. The ruling: the entry itself stays (who, when, action,
ids), but for an event whose agent or department is personal, the content
fields are stripped for every reader except that agent's owner. Done at read
time, so rows written before this change are covered too.

A plain `member` holds no `audit:view`, so an owner reads the trail only with
a role that does; here the owner carries `org_admin`.
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import AsyncClient

from oc8.audit.chain import append_event
from tests.api.test_personal_agent_content import _headers, _http, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_NEW_MISSION = "Answer the landlord, and never mention the rent dispute."
_ACTION = "agent.instructions.updated"


async def _edit(
    http: AsyncClient, tenant: uuid.UUID, agent: uuid.UUID, subject: str, role: str
) -> None:
    r = await http.patch(
        f"/api/v1/agents/{agent}/instructions",
        json={"instructions": _NEW_MISSION},
        headers=_headers(tenant, subject, role),
    )
    assert r.status_code == 200, r.text


async def test_an_admin_sees_a_personal_agents_event_without_its_content(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        await _edit(http, w.tenant, w.personal_agent, "owner", "member")
        r = await http.get(
            f"/api/v1/audit?action={_ACTION}",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert len(events) == 1
    event = events[0]
    assert event["action"] == _ACTION
    assert event["resource"]["agent_id"] == str(w.personal_agent)
    assert event["resource"]["by"] == "owner"
    assert "before" not in event["resource"]
    assert "after" not in event["resource"]
    assert _NEW_MISSION not in r.text
    assert "Answer my private mail" not in r.text


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
async def test_the_export_strips_a_personal_agents_content_too(
    app_session: AppSessionFactory, fmt: str
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        await _edit(http, w.tenant, w.personal_agent, "owner", "member")
        r = await http.get(
            f"/api/v1/audit/export?format={fmt}&action={_ACTION}",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 200, r.text
    assert str(w.personal_agent) in r.text
    assert _NEW_MISSION not in r.text
    assert "Answer my private mail" not in r.text


async def test_the_owner_sees_the_full_event(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    async with _http() as http:
        await _edit(http, w.tenant, w.personal_agent, "owner", "member")
        r = await http.get(
            f"/api/v1/audit?action={_ACTION}",
            headers=_headers(w.tenant, "owner", "org_admin"),
        )
    assert r.status_code == 200, r.text
    resource = r.json()["events"][0]["resource"]
    assert resource["after"] == _NEW_MISSION
    assert resource["before"] == "Answer my private mail in my voice."


async def test_a_standard_agents_event_is_unchanged(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    async with _http() as http:
        await _edit(http, w.tenant, w.standard_agent, "admin", "org_admin")
        r = await http.get(
            f"/api/v1/audit?action={_ACTION}",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
        export = await http.get(
            f"/api/v1/audit/export?format=jsonl&action={_ACTION}",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 200, r.text
    resource = r.json()["events"][0]["resource"]
    assert resource["after"] == _NEW_MISSION
    lines = [json.loads(line) for line in export.text.splitlines() if line]
    assert lines[0]["resource"]["after"] == _NEW_MISSION


async def test_a_personal_agents_tool_call_keeps_its_arguments_from_an_admin(
    app_session: AppSessionFactory,
) -> None:
    """A tool call names its agent as the actor, not in `resource`; the rule
    follows the actor too. The same call by a standard agent is unchanged."""
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        for agent in (w.personal_agent, w.standard_agent):
            await append_event(
                db,
                tenant_id=w.tenant,
                actor_type="agent",
                actor_id=agent,
                category="tool_action",
                action="tool.call:mail_send",
                resource={"tool": "mail_send", "arguments": {"to": "landlord@example.org"}},
            )
    async with _http() as http:
        r = await http.get(
            "/api/v1/audit?action=tool.call:mail_send",
            headers=_headers(w.tenant, "admin", "org_admin"),
        )
    assert r.status_code == 200, r.text
    by_actor = {e["actorId"]: e["resource"] for e in r.json()["events"]}
    assert by_actor[str(w.personal_agent)] == {"tool": "mail_send"}
    assert by_actor[str(w.standard_agent)]["arguments"] == {"to": "landlord@example.org"}
