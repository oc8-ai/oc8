"""The admin routes of personal departments: the on/off switch (with the agent
cap), the default frame, the default budget, the "Personal" list and
follow-default.

Every test mints its own tenant. The world holds four personal departments:
`following` (follows both defaults), `overridden` (both stamps set),
`orphaned` (its owner's member row is soft-deleted) and `deleted` (the
department itself is soft-deleted). Re-sync must touch `following` only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.auth import get_identity_provider
from oc8.authz.permissions import (
    BUDGET,
    BUILTIN_ROLE_PERMISSIONS,
    DEPARTMENT,
    MANAGE,
    perm,
)
from oc8.authz.scope import subject_uuid_for
from oc8.config import get_settings
from oc8.departments.personal import (
    BUDGET_KEY,
    ENABLED_KEY,
    FRAME_KEY,
    MAX_AGENTS_KEY,
    personal_defaults,
    personal_frame,
)
from oc8.main import create_app
from oc8.metering.budget import get_budget, set_budget
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_CAPAS_DIR = Path(__file__).resolve().parents[3] / "capas"

_OLD_TOOLS: dict[str, Any] = {"x": {"enabled": True}, "y": {"enabled": False}}
_OVERRIDDEN_TOOLS: dict[str, Any] = {"z": {"enabled": True}}


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="admin", role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@dataclass
class _World:
    tenant: uuid.UUID
    following: uuid.UUID
    overridden: uuid.UUID
    orphaned: uuid.UUID
    deleted: uuid.UUID
    standard: uuid.UUID
    a1: uuid.UUID  # in `following`, overrode "x"
    a2: uuid.UUID  # in `following`, overrode nothing
    b1: uuid.UUID  # in `overridden`
    o1: uuid.UUID  # in `orphaned`


async def _world(app_session: AppSessionFactory, settings: dict[str, Any] | None = None) -> _World:
    tenant = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t-{tenant.hex[:8]}",
                name="T",
                settings={
                    ENABLED_KEY: True,
                    FRAME_KEY: _OLD_TOOLS,
                    "unrelated": "kept",
                    **(settings or {}),
                },
            )
        )
        members: list[m.OrgMember] = []
        for name in ("Ada", "Bob", "Cid", "Dee"):
            subject = f"user-{uuid.uuid4().hex[:8]}"
            member = m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=subject_uuid_for(subject),
                display_name=name,
            )
            db.add(member)
            members.append(member)
        await db.flush()
        members[2].deleted_at = now

        def personal(owner: m.OrgMember, tools: dict[str, Any]) -> m.Department:
            dept = m.Department(
                tenant_id=tenant,
                name=owner.display_name,
                goal="",
                kind=DEPARTMENT_KIND_PERSONAL,
                owner_member_id=owner.id,
                frame=personal_frame(tools),
                presentation={"icon": "user"},
            )
            db.add(dept)
            return dept

        following = personal(members[0], _OLD_TOOLS)
        overridden = personal(members[1], _OVERRIDDEN_TOOLS)
        overridden.frame_overridden_at = now
        overridden.budget_overridden_at = now
        orphaned = personal(members[2], _OLD_TOOLS)
        deleted = personal(members[3], _OLD_TOOLS)
        deleted.deleted_at = now
        standard = m.Department(
            tenant_id=tenant, name="Sales", goal="", frame={"tools": _OLD_TOOLS}
        )
        db.add(standard)
        await db.flush()

        a1 = m.Agent(
            tenant_id=tenant,
            department_id=following.id,
            name="A1",
            narrowing={"tools": {"x": {"enabled": True}}},
            narrowing_overridden_keys=["x"],
        )
        a2 = m.Agent(
            tenant_id=tenant,
            department_id=following.id,
            name="A2",
            narrowing={"tools": {"x": {"enabled": True}, "y": {"enabled": False}}},
        )
        b1 = m.Agent(
            tenant_id=tenant,
            department_id=overridden.id,
            name="B1",
            narrowing={"tools": {"z": {"enabled": True}}},
        )
        o1 = m.Agent(
            tenant_id=tenant,
            department_id=orphaned.id,
            name="O1",
            narrowing={"tools": {"x": {"enabled": True}, "y": {"enabled": False}}},
        )
        db.add_all([a1, a2, b1, o1])
        await db.flush()
        for agent in (a1, a2, b1, o1):
            await publish_version(db, agent, note="v1")
        await set_budget(
            db,
            tenant_id=tenant,
            department_id=overridden.id,
            soft_limit_tokens=5,
            hard_limit_tokens=6,
        )
        return _World(
            tenant=tenant,
            following=following.id,
            overridden=overridden.id,
            orphaned=orphaned.id,
            deleted=deleted.id,
            standard=standard.id,
            a1=a1.id,
            a2=a2.id,
            b1=b1.id,
            o1=o1.id,
        )


async def _dept(app_session: AppSessionFactory, w: _World, dept_id: uuid.UUID) -> m.Department:
    async with app_session(w.tenant) as db:
        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        db.expunge(dept)
        return dept


async def _agent(app_session: AppSessionFactory, w: _World, agent_id: uuid.UUID) -> m.Agent:
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        db.expunge(agent)
        return agent


async def _versions(app_session: AppSessionFactory, w: _World, agent_id: uuid.UUID) -> int:
    async with app_session(w.tenant) as db:
        return int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(m.AgentVersion)
                    .where(m.AgentVersion.agent_id == agent_id)
                )
            ).scalar_one()
        )


async def _budget(
    app_session: AppSessionFactory, w: _World, dept_id: uuid.UUID
) -> tuple[int | None, int | None] | None:
    async with app_session(w.tenant) as db:
        row = await get_budget(db, tenant_id=w.tenant, department_id=dept_id)
        return None if row is None else (row.soft_limit_tokens, row.hard_limit_tokens)


async def _settings(app_session: AppSessionFactory, w: _World) -> dict[str, Any]:
    async with app_session(w.tenant) as db:
        org = await db.get(m.Organization, w.tenant)
        assert org is not None
        return dict(org.settings)


async def _audits(app_session: AppSessionFactory, w: _World, action: str) -> list[dict[str, Any]]:
    async with app_session(w.tenant) as db:
        rows = await db.execute(
            select(m.AuditEvent)
            .where(m.AuditEvent.tenant_id == w.tenant, m.AuditEvent.action == action)
            .order_by(m.AuditEvent.seq)
        )
        return [dict(e.resource) for e in rows.scalars().all()]


async def test_settings_switch_read_write_and_gate(app_session: AppSessionFactory) -> None:
    w = await _world(app_session, settings={ENABLED_KEY: False})
    url = "/api/v1/settings/personal-departments"
    async with _http() as c:
        got = await c.get(url, headers=_headers(w.tenant, "operator"))
        assert got.status_code == 200, got.text
        assert got.json() == {"enabled": False, "maxAgents": 10}

        forbidden = await c.put(url, json={"enabled": True}, headers=_headers(w.tenant, "operator"))
        assert forbidden.status_code == 403

        for bad in (0, 1001, "many"):
            r = await c.put(
                url, json={"enabled": True, "maxAgents": bad}, headers=_headers(w.tenant)
            )
            assert r.status_code == 422, (bad, r.text)

        put = await c.put(url, json={"enabled": True, "maxAgents": 3}, headers=_headers(w.tenant))
        assert put.status_code == 200, put.text
        assert put.json() == {"enabled": True, "maxAgents": 3}
        assert (await c.get(url, headers=_headers(w.tenant))).json() == {
            "enabled": True,
            "maxAgents": 3,
        }

        # `maxAgents` is optional: leaving it out keeps the stored cap.
        put = await c.put(url, json={"enabled": False}, headers=_headers(w.tenant))
        assert put.status_code == 200, put.text
        assert put.json() == {"enabled": False, "maxAgents": 3}

    settings = await _settings(app_session, w)
    assert settings[ENABLED_KEY] is False
    assert settings[MAX_AGENTS_KEY] == 3
    assert settings["unrelated"] == "kept"
    audits = await _audits(app_session, w, "organization.settings.updated")
    assert [(a["enabled"], a["max_agents"]) for a in audits] == [(True, 3), (False, 3)]


@pytest.fixture
def _capas_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The limit check reads a connection's tool names from its manifest on disk."""
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_CAPAS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.usefixtures("_capas_path")
async def test_default_frame_is_validated_like_a_department_frame(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        db.add(
            m.McpConnection(
                tenant_id=w.tenant,
                name="odoo",
                server_url="",
                transport="stdio",
                config={"_plugin_name": "odoo_mcp", "_connection_key": "primary"},
            )
        )
    async with _http() as c:
        r = await c.put(
            "/api/v1/personal-departments/default-frame",
            json={
                "tools": {
                    "odoo": {
                        "enabled": True,
                        "limits": [{"tools": ["no_such_tool"], "max": 1, "per": "day"}],
                    }
                }
            },
            headers=_headers(w.tenant),
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["error"] == "limit_names_unknown_tools"

        forbidden = await c.put(
            "/api/v1/personal-departments/default-frame",
            json={"tools": {}},
            headers=_headers(w.tenant, "operator"),
        )
        assert forbidden.status_code == 403
        forbidden = await c.get(
            "/api/v1/personal-departments/default-frame", headers=_headers(w.tenant, "operator")
        )
        assert forbidden.status_code == 403

    # Nothing was stored, nothing re-synced.
    assert (await _settings(app_session, w))[FRAME_KEY] == _OLD_TOOLS
    assert (await _dept(app_session, w, w.following)).frame["tools"] == _OLD_TOOLS


async def test_default_frame_resync_skips_overrides(app_session: AppSessionFactory) -> None:
    w = await _world(app_session)
    overridden_before = (await _dept(app_session, w, w.overridden)).frame
    orphaned_before = (await _dept(app_session, w, w.orphaned)).frame
    standard_before = (await _dept(app_session, w, w.standard)).frame

    async with _http() as c:
        r = await c.put(
            "/api/v1/personal-departments/default-frame",
            json={"tools": {"x": {"enabled": False}, "y": {"enabled": True}}},
            headers=_headers(w.tenant),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        new_tools = body["tools"]
        assert new_tools["x"]["enabled"] is False and new_tools["y"]["enabled"] is True
        # Agents in following departments: a1 and a2 only.
        assert body["agentCount"] == 2
        # a1's own override of x (enabled) now widens the frame, which disables
        # x: a personal frame is a ceiling, so it is clipped and no longer deviates.
        assert body["deviationCounts"] == {"x": 0, "y": 0}

        got = await c.get("/api/v1/personal-departments/default-frame", headers=_headers(w.tenant))
        assert got.status_code == 200, got.text
        assert got.json() == body

    assert (await _settings(app_session, w))[FRAME_KEY] == new_tools
    following = await _dept(app_session, w, w.following)
    # Switching "x" off tightens and applies at once; switching "y" on loosens,
    # so with live agents it waits for the owner (`apply_held_tools`).
    assert following.frame["tools"]["x"] == new_tools["x"]
    assert following.frame["tools"]["y"]["enabled"] is False
    assert following.pending_frame_tools == new_tools
    assert following.frame_overridden_at is None
    assert (await _dept(app_session, w, w.overridden)).frame == overridden_before
    assert (await _dept(app_session, w, w.orphaned)).frame == orphaned_before
    assert (await _dept(app_session, w, w.standard)).frame == standard_before

    a1 = await _agent(app_session, w, w.a1)
    a2 = await _agent(app_session, w, w.a2)
    assert "x" not in a1.narrowing["tools"]
    assert a1.narrowing_overridden_keys == []
    assert await _versions(app_session, w, w.a1) == 2
    assert a2.narrowing["tools"]["x"]["enabled"] is False
    assert a2.narrowing["tools"]["y"]["enabled"] is False  # held, not cascaded
    assert await _versions(app_session, w, w.a2) == 2
    # Untouched agents keep their single version.
    assert await _versions(app_session, w, w.b1) == 1
    assert await _versions(app_session, w, w.o1) == 1
    o1 = await _agent(app_session, w, w.o1)
    assert o1.narrowing["tools"]["x"]["enabled"] is True

    audits = await _audits(app_session, w, "organization.settings.updated")
    assert audits and audits[-1]["setting"] == FRAME_KEY and audits[-1]["resynced"] == 1


async def test_default_budget_resync_skips_overrides_and_validates(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    url = "/api/v1/personal-departments/default-budget"
    async with _http() as c:
        got = await c.get(url, headers=_headers(w.tenant, "operator"))
        assert got.status_code == 200, got.text
        assert got.json() == {"softLimitTokens": None, "hardLimitTokens": None}

        for bad in (
            {"softLimitTokens": 300, "hardLimitTokens": 200},
            {"softLimitTokens": -1, "hardLimitTokens": None},
            {"softLimitTokens": None, "hardLimitTokens": -5},
        ):
            r = await c.put(url, json=bad, headers=_headers(w.tenant))
            assert r.status_code == 422, (bad, r.text)

        forbidden = await c.put(
            url,
            json={"softLimitTokens": 1, "hardLimitTokens": 2},
            headers=_headers(w.tenant, "operator"),
        )
        assert forbidden.status_code == 403

        put = await c.put(
            url, json={"softLimitTokens": 100, "hardLimitTokens": 200}, headers=_headers(w.tenant)
        )
        assert put.status_code == 200, put.text
        assert put.json() == {"softLimitTokens": 100, "hardLimitTokens": 200}
        assert (await c.get(url, headers=_headers(w.tenant))).json() == put.json()

    assert (await _settings(app_session, w))[BUDGET_KEY] == {
        "soft_limit_tokens": 100,
        "hard_limit_tokens": 200,
    }
    assert await _budget(app_session, w, w.following) == (100, 200)
    assert await _budget(app_session, w, w.overridden) == (5, 6)
    assert await _budget(app_session, w, w.orphaned) is None
    assert await _budget(app_session, w, w.deleted) is None
    assert await _budget(app_session, w, w.standard) is None
    assert (await _dept(app_session, w, w.following)).budget_overridden_at is None

    # A default with no limits removes the following department's budget row.
    async with _http() as c:
        put = await c.put(
            url, json={"softLimitTokens": None, "hardLimitTokens": None}, headers=_headers(w.tenant)
        )
        assert put.status_code == 200, put.text
    assert await _budget(app_session, w, w.following) is None
    assert await _budget(app_session, w, w.overridden) == (5, 6)

    audits = await _audits(app_session, w, "organization.settings.updated")
    assert [a["setting"] for a in audits] == [BUDGET_KEY, BUDGET_KEY]


async def test_personal_list_is_metadata_only_and_admin_only(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as c:
        r = await c.get("/api/v1/personal-departments", headers=_headers(w.tenant))
        assert r.status_code == 200, r.text
        rows = r.json()
        forbidden = await c.get(
            "/api/v1/personal-departments", headers=_headers(w.tenant, "operator")
        )
        assert forbidden.status_code == 403

    by_id = {row["id"]: row for row in rows}
    # Live personal departments only: the soft-deleted one and the standard one
    # are absent. The orphaned one stays visible -- admins still see its metadata.
    assert set(by_id) == {str(w.following), str(w.overridden), str(w.orphaned)}
    for row in rows:
        assert set(row) == {
            "id",
            "ownerMemberId",
            "ownerName",
            "ownerActive",
            "agentCount",
            "followsDefaultFrame",
            "followsDefaultBudget",
            "createdAt",
            "pendingConfirmation",
        }
        assert row["pendingConfirmation"] is False
    following = by_id[str(w.following)]
    assert following["ownerName"] == "Ada"
    assert following["ownerActive"] is True
    assert following["agentCount"] == 2
    assert following["followsDefaultFrame"] is True
    assert following["followsDefaultBudget"] is True
    overridden = by_id[str(w.overridden)]
    assert overridden["ownerName"] == "Bob"
    assert overridden["agentCount"] == 1
    assert overridden["followsDefaultFrame"] is False
    assert overridden["followsDefaultBudget"] is False
    # The owner of the orphaned department has left: the name stays, but the
    # flag tells the UI not to present them as a current member.
    orphaned = by_id[str(w.orphaned)]
    assert orphaned["ownerName"] == "Cid"
    assert orphaned["ownerActive"] is False


async def test_follow_default_clears_stamps_and_reapplies(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neither manage right is delegatable, so no tenant role can hold just one
    # of them; two probe roles stand in to prove BOTH gates are on the route.
    monkeypatch.setitem(
        BUILTIN_ROLE_PERMISSIONS, "probe_department_only", frozenset({perm(DEPARTMENT, MANAGE)})
    )
    monkeypatch.setitem(
        BUILTIN_ROLE_PERMISSIONS, "probe_budget_only", frozenset({perm(BUDGET, MANAGE)})
    )
    w = await _world(
        app_session,
        settings={BUDGET_KEY: {"soft_limit_tokens": 100, "hard_limit_tokens": 200}},
    )
    async with _http() as c:
        # The probes are live: each one passes the gate it holds.
        probe = await c.get(
            "/api/v1/personal-departments", headers=_headers(w.tenant, "probe_department_only")
        )
        assert probe.status_code == 200, probe.text
        probe = await c.put(
            "/api/v1/personal-departments/default-budget",
            json={"softLimitTokens": 100, "hardLimitTokens": 200},
            headers=_headers(w.tenant, "probe_budget_only"),
        )
        assert probe.status_code == 200, probe.text
        for role in ("operator", "probe_department_only", "probe_budget_only"):
            r = await c.post(
                f"/api/v1/personal-departments/{w.overridden}/follow-default",
                headers=_headers(w.tenant, role),
            )
            assert r.status_code == 403, (role, r.text)
        for missing in (w.standard, w.deleted, uuid.uuid4()):
            r = await c.post(
                f"/api/v1/personal-departments/{missing}/follow-default",
                headers=_headers(w.tenant),
            )
            assert r.status_code == 404, (missing, r.text)

        r = await c.post(
            f"/api/v1/personal-departments/{w.overridden}/follow-default",
            headers=_headers(w.tenant),
        )
        assert r.status_code == 200, r.text
        row = r.json()
        assert row["id"] == str(w.overridden)
        assert row["followsDefaultFrame"] is True and row["followsDefaultBudget"] is True
        assert row["agentCount"] == 1

    dept = await _dept(app_session, w, w.overridden)
    assert dept.frame_overridden_at is None and dept.budget_overridden_at is None
    # Dropping "z" tightens and applies; "x" comes back enabled, a loosening
    # that waits for the owner while the department has a live agent.
    assert dept.frame == personal_frame({"y": _OLD_TOOLS["y"]})
    assert dept.pending_frame_tools == _OLD_TOOLS
    b1 = await _agent(app_session, w, w.b1)
    # "z" left the frame: a personal frame is a ceiling, so the key is dropped.
    assert "z" not in b1.narrowing["tools"]
    assert "x" not in b1.narrowing["tools"]
    assert await _versions(app_session, w, w.b1) == 2
    assert await _budget(app_session, w, w.overridden) == (100, 200)
    audits = await _audits(app_session, w, "department.personal.follow_default")
    assert audits and audits[-1]["department_id"] == str(w.overridden)


async def test_follow_default_without_default_limits_drops_the_budget(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as c:
        r = await c.post(
            f"/api/v1/personal-departments/{w.overridden}/follow-default",
            headers=_headers(w.tenant),
        )
        assert r.status_code == 200, r.text
    assert await _budget(app_session, w, w.overridden) is None


async def test_defaults_reader_ignores_a_stored_soft_above_hard(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(
        app_session,
        settings={BUDGET_KEY: {"soft_limit_tokens": 300, "hard_limit_tokens": 200}},
    )
    async with app_session(w.tenant) as db:
        defaults = await personal_defaults(db, tenant_id=w.tenant)
    assert defaults.soft_limit_tokens is None and defaults.hard_limit_tokens is None


async def test_follow_default_on_a_department_whose_owner_left_is_a_404(
    app_session: AppSessionFactory,
) -> None:
    """The re-sync scope (`_following_departments`) skips an owner who has left;
    follow-default must agree rather than re-apply to such a department."""
    w = await _world(app_session)
    before = await _dept(app_session, w, w.orphaned)
    async with _http() as c:
        r = await c.post(
            f"/api/v1/personal-departments/{w.orphaned}/follow-default",
            headers=_headers(w.tenant),
        )
    assert r.status_code == 404, r.text
    assert (await _dept(app_session, w, w.orphaned)).frame == before.frame


async def test_the_owner_name_never_falls_back_to_the_idp_subject(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        owner = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.display_name == "Ada"))
        ).scalar_one()
        owner.display_name = ""
        subject = owner.subject
    async with _http() as c:
        r = await c.get("/api/v1/personal-departments", headers=_headers(w.tenant))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["id"] == str(w.following))
    assert row["ownerName"] == ""
    # Still a current member -- just one without a display name.
    assert row["ownerActive"] is True
    assert subject not in r.text


async def test_resync_publishes_neither_a_pending_approval_nor_a_dirty_agent(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with app_session(w.tenant) as db:
        pending = m.Agent(
            tenant_id=w.tenant,
            department_id=w.following,
            name="Pending",
            status="pending_approval",
            narrowing={"tools": {"x": {"enabled": True}, "y": {"enabled": False}}},
        )
        dirty = m.Agent(
            tenant_id=w.tenant,
            department_id=w.following,
            name="Dirty",
            narrowing={"tools": {"x": {"enabled": True}, "y": {"enabled": False}}},
        )
        db.add_all([pending, dirty])
        await db.flush()
        await publish_version(db, pending, note="v1", at_hire=True)
        await publish_version(db, dirty, note="v1")
        dirty.mission = "an unpublished edit"  # a pending draft
        pending_id, dirty_id = pending.id, dirty.id
    async with _http() as c:
        r = await c.put(
            "/api/v1/personal-departments/default-frame",
            json={"tools": {"x": {"enabled": False}, "y": {"enabled": True}}},
            headers=_headers(w.tenant),
        )
        assert r.status_code == 200, r.text
    assert await _versions(app_session, w, pending_id) == 1
    assert await _versions(app_session, w, dirty_id) == 1
    # The cascade still reached their working copies.
    assert (await _agent(app_session, w, dirty_id)).narrowing["tools"]["x"]["enabled"] is False
    assert (await _agent(app_session, w, pending_id)).narrowing["tools"]["x"]["enabled"] is False
