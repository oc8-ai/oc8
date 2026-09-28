"""`oc8.agents.versioning`: the snapshot/publish functions behind agent config
versioning (Package A, tasks A1/A2). `snapshot_agent`'s payload boundary is
the most important thing tested here -- it is the only defence against
someone adding a behavioural column to `Agent` and forgetting to version it.
"""

from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.agents.versioning import (
    NoChangesToPublish,
    payload_hash,
    publish_version,
    resolve_version,
    snapshot_agent,
)
from tests.conftest import AppSessionFactory
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio

VERSIONED_FIELDS = {
    "mission",
    "role_title",
    "definition",
    "model_config_id",
    "narrowing",
    "narrowing_overridden_keys",
    "role_id",
    "runtime_ref",
    "is_team_lead",
    "skill_assignments",
    "knowledge_grants",
}


async def test_snapshot_contains_exactly_the_versioned_fields(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        snap = await snapshot_agent(db, a)
    assert set(snap) == VERSIONED_FIELDS


async def test_snapshot_excludes_operational_state(app_session: AppSessionFactory) -> None:
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        snap = await snapshot_agent(db, a)
    for forbidden in (
        "status",
        "pause_reason",
        "paused_at",
        "presentation",
        "trust_level",
        "is_tenant_assistant",
        "config_revision",
        "name",
        "id",
        "tenant_id",
        "department_id",
    ):
        assert forbidden not in snap


async def test_snapshot_scopes_skill_assignments_to_this_agent_enabled_only(
    app_session: AppSessionFactory,
) -> None:
    """Only enabled, agent-scoped assignments count. Department/tenant-wide
    assignments are inherited policy, not this agent's own configuration, and
    a disabled (soft-deleted-by-flag) assignment must not resurrect on a
    rollback."""
    agent = await _make_agent(app_session)
    other_agent = await _make_agent(app_session, tenant_id=agent.tenant_id)
    async with app_session(agent.tenant_id) as db:
        mine = m.SkillAssignment(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            skill_version_id=uuid.uuid4(),
            enabled=True,
        )
        disabled = m.SkillAssignment(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            skill_version_id=uuid.uuid4(),
            enabled=False,
        )
        dept_wide = m.SkillAssignment(
            tenant_id=agent.tenant_id,
            department_id=other_agent.department_id,
            skill_version_id=uuid.uuid4(),
            enabled=True,
        )
        other = m.SkillAssignment(
            tenant_id=agent.tenant_id,
            agent_id=other_agent.id,
            skill_version_id=uuid.uuid4(),
            enabled=True,
        )
        db.add_all([mine, disabled, dept_wide, other])
        await db.flush()

        a = await db.get(m.Agent, agent.id)
        assert a is not None
        snap = await snapshot_agent(db, a)

    assert snap["skill_assignments"] == [{"skill_version_id": str(mine.skill_version_id)}]


async def test_snapshot_scopes_knowledge_grants_to_this_agent(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)
    other_agent = await _make_agent(app_session, tenant_id=agent.tenant_id)
    async with app_session(agent.tenant_id) as db:
        mine = m.KnowledgeGrant(
            tenant_id=agent.tenant_id,
            kb_id=uuid.uuid4(),
            grantee_type="agent",
            grantee_id=agent.id,
        )
        dept_grant = m.KnowledgeGrant(
            tenant_id=agent.tenant_id,
            kb_id=uuid.uuid4(),
            grantee_type="department",
            grantee_id=agent.department_id,
        )
        other_grant = m.KnowledgeGrant(
            tenant_id=agent.tenant_id,
            kb_id=uuid.uuid4(),
            grantee_type="agent",
            grantee_id=other_agent.id,
        )
        db.add_all([mine, dept_grant, other_grant])
        await db.flush()

        a = await db.get(m.Agent, agent.id)
        assert a is not None
        snap = await snapshot_agent(db, a)

    assert snap["knowledge_grants"] == [str(mine.kb_id)]


def test_payload_hash_is_stable_regardless_of_key_order() -> None:
    a = payload_hash({"mission": "x", "role_title": "y"})
    b = payload_hash({"role_title": "y", "mission": "x"})
    assert a == b


def test_payload_hash_differs_when_a_list_field_order_differs() -> None:
    """Sorting happens inside `snapshot_agent`, not `payload_hash` -- an
    unsorted list here is a genuinely different payload and must hash
    differently, or the no-op-publish check downstream would be defeated by
    something that merely LOOKS unchanged."""
    a = payload_hash({"skill_assignments": ["1", "2"]})
    b = payload_hash({"skill_assignments": ["2", "1"]})
    assert a != b


async def test_publish_version_creates_v1_and_points_current_version_id_at_it(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Fresh")
        db.add(agent)
        await db.flush()
        assert agent.current_version_id is None

        version = await publish_version(db, agent)
        assert version.version_no == 1
        assert version.agent_id == agent.id
        assert agent.current_version_id == version.id


async def test_publish_version_increments_version_no_on_a_real_change(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)  # already v1
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        a.mission = "a brand new mission"
        v2 = await publish_version(db, a)
        assert v2.version_no == 2
        assert a.current_version_id == v2.id


async def test_publish_version_raises_when_nothing_changed(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session)  # already v1, unchanged
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        with pytest.raises(NoChangesToPublish):
            await publish_version(db, a)


async def test_publish_version_writes_note_and_published_by(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    publisher = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Fresh")
        db.add(agent)
        await db.flush()

        version = await publish_version(db, agent, note="initial hire", published_by=publisher)
        assert version.note == "initial hire"
        assert version.published_by == publisher


async def test_publish_version_audits_the_publish(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Fresh")
        db.add(agent)
        await db.flush()
        await publish_version(db, agent)
        agent_id = agent.id

    async with app_session(tenant) as db:
        import sqlalchemy as sa

        rows = (
            (
                await db.execute(
                    sa.select(m.AuditEvent.action).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "agent.version.published",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == ["agent.version.published"], (agent_id, rows)


async def test_resolve_version_uses_the_runs_pinned_version_when_set(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session, mission="v1 mission")
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        v1_id = a.current_version_id
        a.mission = "v2 mission"
        await publish_version(db, a)  # now current_version_id points at v2

        run = m.AgentRun(tenant_id=agent.tenant_id, agent_id=agent.id, agent_version_id=v1_id)
        db.add(run)
        await db.flush()

        payload = await resolve_version(db, run, a)
    assert payload["mission"] == "v1 mission"


async def test_resolve_version_falls_back_to_current_when_run_has_no_pin(
    app_session: AppSessionFactory,
) -> None:
    agent = await _make_agent(app_session, mission="only mission")
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        run = m.AgentRun(tenant_id=agent.tenant_id, agent_id=agent.id)
        db.add(run)
        await db.flush()

        payload = await resolve_version(db, run, a)
    assert payload["mission"] == "only mission"


async def test_resolve_version_fills_a_column_an_older_payload_predates(
    app_session: AppSessionFactory,
) -> None:
    """Every runtime indexes the resolved payload directly, so a version
    published before a column was versioned must not KeyError a run."""
    agent = await _make_agent(app_session, is_team_lead=True)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None and a.current_version_id is not None
        version = await db.get(m.AgentVersion, a.current_version_id)
        assert version is not None
        version.payload = {k: v for k, v in version.payload.items() if k != "is_team_lead"}
        await db.flush()
        run = m.AgentRun(tenant_id=agent.tenant_id, agent_id=agent.id, agent_version_id=version.id)
        db.add(run)
        await db.flush()

        payload = await resolve_version(db, run, a)
    assert payload["is_team_lead"] is True
