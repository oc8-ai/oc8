"""`oc8.agents.versioning`: the snapshot/publish functions behind agent config
versioning (Package A, tasks A1/A2). `snapshot_agent`'s payload boundary is
the most important thing tested here -- it is the only defence against
someone adding a behavioural column to `Agent` and forgetting to version it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import (
    META_KEY,
    NoChangesToPublish,
    draft_status,
    payload_hash,
    publish_version,
    resolve_version,
    snapshot_agent,
    version_meta,
    version_payload,
)
from tests.conftest import AppSessionFactory
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio


async def _draft_agent(app_session: AppSessionFactory, **overrides: Any) -> m.Agent:
    """Create a fresh tenant + department + agent WITHOUT publishing it.

    Session-level counterpart to `tests.factories._make_agent`, which
    auto-publishes v1 -- several tests in this file need to call
    `publish_version` themselves and see the result land as v1, which
    `_make_agent`'s auto-publish would collide with (`NoChangesToPublish` on a
    second, unchanged publish, or an unexpected v2). Per Ruling C2, this is
    its own differently-named helper rather than a change to `_make_agent`.
    """
    tenant_id = overrides.pop("tenant_id", None) or uuid.uuid4()
    name = overrides.pop("name", "Draft Agent")
    async with app_session(tenant_id) as db:
        dept = m.Department(tenant_id=tenant_id, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant_id, department_id=dept.id, name=name, **overrides)
        db.add(agent)
        await db.flush()
    return agent


async def _make_two_skill_versions(
    db: AsyncSession, tenant: uuid.UUID
) -> tuple[m.SkillVersion, m.SkillVersion]:
    """Two versions of two distinct skills, so a test can keep one assignment
    live and drop the other without tripping `uq_skill_version`."""
    out: list[m.SkillVersion] = []
    for _ in range(2):
        skill = m.Skill(tenant_id=tenant, name=f"S-{uuid.uuid4().hex}")
        db.add(skill)
        await db.flush()
        version = m.SkillVersion(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={},
            artifact_hash=b"\x00" * 32,
        )
        db.add(version)
        await db.flush()
        out.append(version)
    return out[0], out[1]


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


# --- Task 0 regressions (verified a no-op on this branch, kept anyway) -----


async def test_snapshot_reads_knowledge_grants_by_grantee_not_by_agent_id(
    app_session: AppSessionFactory,
) -> None:
    """`KnowledgeGrant` has no `agent_id`: it is (`grantee_type`, `grantee_id`).

    A snapshot that filtered on a non-existent column would either fail to
    import or silently return nothing, and a rollback built on it would drop
    every knowledge grant the agent had.
    """
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        kb = m.KnowledgeBase(
            tenant_id=agent.tenant_id, name=f"KB-{uuid.uuid4().hex}", embedding_model="text-embed"
        )
        db.add(kb)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=agent.tenant_id,
                kb_id=kb.id,
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        await db.flush()

        a = await db.get(m.Agent, agent.id)
        assert a is not None
        snap = await snapshot_agent(db, a)
        assert snap["knowledge_grants"] == [str(kb.id)]


async def test_snapshot_ignores_an_unassigned_skill(app_session: AppSessionFactory) -> None:
    """`SkillAssignment` is soft-deleted and re-enabled, never hard-deleted.

    Without the `deleted_at`/`enabled` terms an unassigned skill stays in every
    later snapshot, and a rollback to that version resurrects an assignment
    the operator removed on purpose.
    """
    agent = await _make_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        live, dropped = await _make_two_skill_versions(db, agent.tenant_id)
        db.add_all(
            [
                m.SkillAssignment(
                    tenant_id=agent.tenant_id,
                    agent_id=agent.id,
                    skill_version_id=live.id,
                    enabled=True,
                ),
                m.SkillAssignment(
                    tenant_id=agent.tenant_id,
                    agent_id=agent.id,
                    skill_version_id=dropped.id,
                    enabled=False,
                ),
            ]
        )
        await db.flush()

        a = await db.get(m.Agent, agent.id)
        assert a is not None
        snap = await snapshot_agent(db, a)
        pinned = [row["skill_version_id"] for row in snap["skill_assignments"]]
        assert pinned == [str(live.id)]


# --- Task 2: draft status, publish metadata -------------------------------


async def test_a_freshly_published_agent_is_not_dirty(app_session: AppSessionFactory) -> None:
    agent = await _draft_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        await publish_version(db, a, note="v1")
        status = await draft_status(db, a)
        assert status.dirty is False
        assert status.changed_fields == ()
        assert status.current_version_no == 1


async def test_editing_the_working_copy_makes_it_dirty_and_names_the_field(
    app_session: AppSessionFactory,
) -> None:
    agent = await _draft_agent(app_session, mission="original")
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        await publish_version(db, a, note="v1")
        a.mission = "changed"
        await db.flush()
        status = await draft_status(db, a)
        assert status.dirty is True
        assert status.changed_fields == ("mission",)
        assert status.current_version_no == 1


async def test_renaming_an_agent_never_makes_it_dirty(app_session: AppSessionFactory) -> None:
    """Decision 5: `name` is not versioned. A rename must not consume a version
    number, trigger a compliance re-check, or light up a publish bar -- it is a
    display change, and the `agent.renamed` audit event already covers it."""
    agent = await _draft_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        await publish_version(db, a, note="v1")
        a.name = f"Renamed-{uuid.uuid4().hex[:8]}"
        await db.flush()
        status = await draft_status(db, a)
        assert status.dirty is False


async def test_an_agent_with_no_current_version_is_dirty_and_has_no_number(
    app_session: AppSessionFactory,
) -> None:
    """Only reachable for a row written outside `create_agent` -- a fixture, a
    restore, a psql insert. It must read as "everything is unpublished" rather
    than crash or claim to be clean."""
    agent = await _draft_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        assert a.current_version_id is None
        status = await draft_status(db, a)
        assert status.dirty is True
        assert status.current_version_no is None
        assert "mission" in status.changed_fields


async def test_publish_meta_is_stored_but_excluded_from_the_hash(
    app_session: AppSessionFactory,
) -> None:
    """The reason `_meta` exists at all.

    A version published WITH metadata must hash identically to the same
    configuration published without it -- otherwise the no-op-publish 409 stops
    firing for every agent that has ever been rolled back.
    """
    agent = await _draft_agent(app_session, mission="m")
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        v1 = await publish_version(db, a, note="v1")
        a.mission = "m2"
        await db.flush()
        v2 = await publish_version(db, a, note="v2", meta={"rolled_back_from": 1})

        assert v2.payload[META_KEY] == {"rolled_back_from": 1}
        assert version_meta(v2) == {"rolled_back_from": 1}
        assert META_KEY not in version_payload(v2)
        assert v2.payload_hash == payload_hash(await snapshot_agent(db, a))
        assert v1.payload_hash != v2.payload_hash


async def test_a_version_payload_still_holds_exactly_the_versioned_fields(
    app_session: AppSessionFactory,
) -> None:
    """The payload boundary, asserted on a STORED version rather than on a
    fresh snapshot -- the same guard as
    `test_snapshot_contains_exactly_the_versioned_fields`, one layer out, so
    that adding `_meta` did not quietly widen what a runtime reads.
    """
    agent = await _draft_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        version = await publish_version(db, a, meta={"rolled_back_from": 1})
        assert set(version_payload(version)) == VERSIONED_FIELDS


async def test_resolve_version_hands_a_runtime_no_metadata(
    app_session: AppSessionFactory,
) -> None:
    """A runtime reads configuration. `_meta` is not configuration, and a key a
    runtime has no meaning for is a key some future preamble builder will
    eventually put in a prompt."""
    agent = await _draft_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        a = await db.get(m.Agent, agent.id)
        assert a is not None
        version = await publish_version(db, a, meta={"rolled_back_from": 1})
        run = m.AgentRun(
            tenant_id=agent.tenant_id,
            agent_id=a.id,
            state="queued",
            agent_version_id=version.id,
        )
        db.add(run)
        await db.flush()
        resolved = await resolve_version(db, run, a)
        assert META_KEY not in resolved
        assert set(resolved) == VERSIONED_FIELDS
