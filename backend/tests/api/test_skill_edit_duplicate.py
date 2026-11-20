"""Editing a skill's content as a new version, and duplicating a skill."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app

pytestmark = pytest.mark.asyncio

TOOL_FRAME = {"tools": {"erp.read": {"enabled": True, "read": True, "modify": False}}}


def _h(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    tok = get_identity_provider().mint(tenant_id=tenant, subject="author", role=role)
    return {"Authorization": f"Bearer {tok}"}


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Invoice check",
        "description": "Checks invoices.",
        "category": "finance",
        "instructions": "Compare the invoice total to the purchase order.",
        "tools": [],
        "guardrails": ["Never approve above 10k."],
    }
    base.update(over)
    return base


async def _agent(app_session: Any, tenant: uuid.UUID, frame: dict[str, Any]) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex[:6]}", frame=frame)
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Clerk",
            status="stopped",
            narrowing={},
            definition={},
        )
        db.add(agent)
        await db.flush()
        return agent.id


async def _assign(c: AsyncClient, tenant: uuid.UUID, agent_id: uuid.UUID, version_id: str) -> None:
    r = await c.post(
        f"/api/v1/agents/{agent_id}/skills",
        json={"skillVersionId": version_id},
        headers=_h(tenant),
    )
    assert r.status_code == 201, r.text


async def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---------------------------------------------------------------- versions


async def test_a_new_version_without_semver_bumps_the_patch() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            sid = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()["id"]
            r = await c.post(
                f"/api/v1/skills/{sid}/versions",
                json={"instructions": "Changed."},
                headers=_h(tenant),
            )
            assert r.status_code == 201, r.text
            assert r.json()["version"] == "0.1.1"
            assert r.json()["assignmentsUpdated"] == 0
            assert r.json()["assignmentsSkipped"] == 0
            r2 = await c.post(
                f"/api/v1/skills/{sid}/versions",
                json={"instructions": "Changed again."},
                headers=_h(tenant),
            )
            assert r2.json()["version"] == "0.1.2"


async def test_the_next_version_copes_with_a_suffixed_current_semver() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            sid = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()["id"]
            await c.post(
                f"/api/v1/skills/{sid}/versions",
                json={"semver": "0.1.1", "instructions": "x"},
                headers=_h(tenant),
            )
            await c.post(
                f"/api/v1/skills/{sid}/versions",
                json={"semver": "0.1.1-old", "instructions": "x"},
                headers=_h(tenant),
            )
            r = await c.post(
                f"/api/v1/skills/{sid}/versions", json={"instructions": "y"}, headers=_h(tenant)
            )
            assert r.status_code == 201, r.text
            assert r.json()["version"] == "0.1.2"


async def test_update_assignments_moves_live_ones_and_skips_those_failing_the_check(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    good = await _agent(app_session, tenant, TOOL_FRAME)
    bad = await _agent(app_session, tenant, {})
    gone = await _agent(app_session, tenant, TOOL_FRAME)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            skill = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()
            v1 = skill["currentVersionId"]
            for agent in (good, bad, gone):
                await _assign(c, tenant, agent, v1)
            async with app_session(tenant) as db:
                row = (
                    await db.execute(
                        select(m.SkillAssignment).where(m.SkillAssignment.agent_id == gone)
                    )
                ).scalar_one()
                row.deleted_at = dt.datetime.now(tz=dt.UTC)

            r = await c.post(
                f"/api/v1/skills/{skill['id']}/versions",
                json={"instructions": "New.", "tools": ["erp.read"], "updateAssignments": True},
                headers=_h(tenant),
            )
            assert r.status_code == 201, r.text
            assert r.json()["assignmentsUpdated"] == 1
            assert r.json()["assignmentsSkipped"] == 1
            v2 = r.json()["currentVersionId"]

    async with app_session(tenant) as db:
        rows = {
            a.agent_id: a for a in (await db.execute(select(m.SkillAssignment))).scalars().all()
        }
        assert str(rows[good].skill_version_id) == v2
        assert str(rows[bad].skill_version_id) == v1
        assert str(rows[gone].skill_version_id) == v1


async def test_assignments_stay_pinned_unless_asked() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            r = await c.post(
                "/api/v1/skills/" + str(uuid.uuid4()) + "/versions",
                json={"instructions": "x", "updateAssignments": True},
                headers=_h(tenant),
            )
            assert r.status_code == 404


# ---------------------------------------------------------------- duplicate


async def test_duplicate_copies_the_current_version_into_a_new_local_skill(
    app_session: Any,
) -> None:
    tenant = uuid.uuid4()
    agent = await _agent(app_session, tenant, TOOL_FRAME)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            src = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()
            await _assign(c, tenant, agent, src["currentVersionId"])
            async with app_session(tenant) as db:
                skill = await db.get(m.Skill, uuid.UUID(src["id"]))
                assert skill is not None
                skill.trust_level = "community"
                skill.origin = "store"
                version = await db.get(m.SkillVersion, skill.current_version_id)
                assert version is not None
                version.definition = {**version.definition, "reference_root": "imported:abc"}

            r = await c.post(f"/api/v1/skills/{src['id']}/duplicate", json={}, headers=_h(tenant))
            assert r.status_code == 201, r.text
            copy = r.json()
            assert copy["name"] == "Invoice check (copy)"
            assert copy["origin"] == "local"
            assert copy["version"] == "0.1.0"
            assert copy["instructions"] == src["instructions"]
            assert copy["guardrails"] == src["guardrails"]
            assert copy["id"] != src["id"]

            again = await c.post(
                f"/api/v1/skills/{src['id']}/duplicate", json={}, headers=_h(tenant)
            )
            assert again.json()["name"] == "Invoice check (copy) 2"
            third = await c.post(
                f"/api/v1/skills/{src['id']}/duplicate", json={}, headers=_h(tenant)
            )
            assert third.json()["name"] == "Invoice check (copy) 3"
            named = await c.post(
                f"/api/v1/skills/{src['id']}/duplicate",
                json={"name": "Invoice check (Kopie)"},
                headers=_h(tenant),
            )
            assert named.json()["name"] == "Invoice check (Kopie)"

    async with app_session(tenant) as db:
        new = await db.get(m.Skill, uuid.UUID(copy["id"]))
        assert new is not None
        assert new.trust_level == "community"
        assert new.author == "author"
        version = await db.get(m.SkillVersion, new.current_version_id)
        assert version is not None
        assert version.definition["reference_root"] == "imported:abc"
        assert version.definition["slug"] == "invoice-check-copy"
        assert len(version.artifact_hash) == 32
        count = (
            (
                await db.execute(
                    select(m.SkillAssignment).where(
                        m.SkillAssignment.skill_version_id == version.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert count == []


async def test_duplicate_404s_for_unknown_foreign_and_archived_sources() -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            src = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()
            unknown = await c.post(
                f"/api/v1/skills/{uuid.uuid4()}/duplicate", json={}, headers=_h(tenant)
            )
            assert unknown.status_code == 404
            foreign = await c.post(
                f"/api/v1/skills/{src['id']}/duplicate", json={}, headers=_h(other)
            )
            assert foreign.status_code == 404
            deleted = await c.delete(f"/api/v1/skills/{src['id']}", headers=_h(tenant))
            assert deleted.status_code == 200
            again = await c.post(
                f"/api/v1/skills/{src['id']}/duplicate", json={}, headers=_h(tenant)
            )
            assert again.status_code == 404


async def test_duplicate_404s_for_an_archived_source(app_session: Any) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            src = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()
            async with app_session(tenant) as db:
                skill = await db.get(m.Skill, uuid.UUID(src["id"]))
                assert skill is not None
                skill.deleted_at = dt.datetime.now(tz=dt.UTC)
            r = await c.post(f"/api/v1/skills/{src['id']}/duplicate", json={}, headers=_h(tenant))
            assert r.status_code == 404


async def test_duplicate_requires_skill_manage() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            src = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()
            r = await c.post(
                f"/api/v1/skills/{src['id']}/duplicate", json={}, headers=_h(tenant, "member")
            )
            assert r.status_code == 403


async def test_the_skill_list_counts_live_assignments(app_session: Any) -> None:
    tenant = uuid.uuid4()
    one = await _agent(app_session, tenant, {})
    two = await _agent(app_session, tenant, {})
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            skill = (await c.post("/api/v1/skills", json=_body(), headers=_h(tenant))).json()
            await _assign(c, tenant, one, skill["currentVersionId"])
            await _assign(c, tenant, two, skill["currentVersionId"])
            rows = (await c.get("/api/v1/skills", headers=_h(tenant))).json()["items"]
            assert next(s for s in rows if s["id"] == skill["id"])["assignmentCount"] == 2
