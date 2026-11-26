"""Tenant-wide skills reach personal agents; per-agent ones stay the owner's.

N1: publishing a new skill version moves every live assignment the caller may
change (`skills_write._move_assignments`). A tenant-wide assignment is judged
against every department it reaches, and a personal department refuses an
admin at `authorize_agent_write` -- so once one existed, the company-wide
assignment silently stayed on its old version. The ruling: tenant-wide skills
DO reach personal agents, and moving a tenant-wide assignment is tenant skill
governance, not an agent content write. A per-agent assignment on a personal
agent is still the owner's alone.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from oc8 import models as m
from tests.api.test_personal_agent_content import _headers, _http, _world
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_SKILL: dict[str, Any] = {
    "name": "Polite replies",
    "description": "Keeps replies polite.",
    "category": "writing",
    "instructions": "Be polite.",
    "tools": [],
    "guardrails": [],
}


async def _skill(http: AsyncClient, tenant: uuid.UUID) -> dict[str, Any]:
    r = await http.post(
        "/api/v1/skills", json=_SKILL, headers=_headers(tenant, "admin", "org_admin")
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def _publish(http: AsyncClient, tenant: uuid.UUID, skill_id: str) -> dict[str, Any]:
    r = await http.post(
        f"/api/v1/skills/{skill_id}/versions",
        json={"instructions": "Be polite, always.", "updateAssignments": True},
        headers=_headers(tenant, "admin", "org_admin"),
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def test_a_tenant_wide_assignment_moves_with_a_personal_department_present(
    app_session: AppSessionFactory,
) -> None:
    w = await _world(app_session)
    async with _http() as http:
        skill = await _skill(http, w.tenant)
        async with app_session(w.tenant) as db:
            tenant_wide = m.SkillAssignment(
                tenant_id=w.tenant, skill_version_id=uuid.UUID(skill["currentVersionId"])
            )
            db.add(tenant_wide)
            await db.flush()
            assignment_id = tenant_wide.id
        published = await _publish(http, w.tenant, skill["id"])
    assert published["assignmentsNotAllowed"] == 0
    assert published["assignmentsUpdated"] == 1
    async with app_session(w.tenant) as db:
        row = await db.get(m.SkillAssignment, assignment_id)
        assert row is not None
        assert str(row.skill_version_id) == published["currentVersionId"]


async def test_a_personal_agents_own_assignment_stays_the_owners(
    app_session: AppSessionFactory,
) -> None:
    """The per-agent assignment on a personal agent is an agent write: an admin
    publishing a new version leaves it where the owner put it."""
    w = await _world(app_session)
    async with _http() as http:
        skill = await _skill(http, w.tenant)
        async with app_session(w.tenant) as db:
            db.add(
                m.SkillAssignment(
                    tenant_id=w.tenant,
                    agent_id=w.personal_agent,
                    skill_version_id=uuid.UUID(skill["currentVersionId"]),
                )
            )
        published = await _publish(http, w.tenant, skill["id"])
    assert published["assignmentsNotAllowed"] == 1
    assert published["assignmentsUpdated"] == 0
    async with app_session(w.tenant) as db:
        row = (
            await db.execute(
                select(m.SkillAssignment).where(m.SkillAssignment.agent_id == w.personal_agent)
            )
        ).scalar_one()
        assert str(row.skill_version_id) == skill["currentVersionId"]
