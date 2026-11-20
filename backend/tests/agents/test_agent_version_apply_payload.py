"""`apply_payload` -- the inverse of `snapshot_agent`, used only by rollback.

The scalar half is boring. The two collection fields are not: an
`apply_payload` that set the nine columns and left `skill_assignment` and
`knowledge_grant` alone would restore an agent's instructions while leaving it
holding the skills somebody added afterwards -- a configuration that never
existed at any point in time, which is worse than either the old one or the new
one.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.versioning import (
    apply_payload,
    missing_references,
    publish_version,
    snapshot_agent,
    version_payload,
)
from oc8.constants import ACME_TENANT_ID
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _make_agent(db: AsyncSession, tenant: uuid.UUID) -> m.Agent:
    department = m.Department(tenant_id=tenant, name=f"D-{uuid.uuid4().hex}", frame={})
    db.add(department)
    await db.flush()
    agent = m.Agent(
        tenant_id=tenant,
        department_id=department.id,
        name=f"A-{uuid.uuid4().hex[:8]}",
        mission="original",
    )
    db.add(agent)
    await db.flush()
    return agent


async def _skill_version(db: AsyncSession, tenant: uuid.UUID) -> m.SkillVersion:
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
    return version


async def test_apply_payload_restores_every_scalar_column(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        agent.mission = "v1 mission"
        agent.role_title = "v1 role"
        agent.narrowing = {"tools": {"odoo": {"enabled": True, "read": True}}}
        agent.narrowing_overridden_keys = ["odoo"]
        agent.is_team_lead = True
        agent.runtime_ref = "nanoclaw"
        await db.flush()
        v1 = await publish_version(db, agent)

        agent.mission = "v2 mission"
        agent.role_title = "v2 role"
        agent.narrowing = {}
        agent.narrowing_overridden_keys = []
        agent.is_team_lead = False
        agent.runtime_ref = None
        await db.flush()

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()

        assert agent.mission == "v1 mission"
        assert agent.role_title == "v1 role"
        assert agent.narrowing == {"tools": {"odoo": {"enabled": True, "read": True}}}
        assert agent.narrowing_overridden_keys == ["odoo"]
        assert agent.is_team_lead is True
        assert agent.runtime_ref == "nanoclaw"


async def test_apply_payload_restores_a_uuid_column_and_a_null_one(
    app_session: AppSessionFactory,
) -> None:
    """`model_config_id` and `role_id` are UUID columns and the payload holds
    them as STRINGS (`snapshot_agent` stringifies). Assigning the string back
    would give the ORM a `str` where it expects a `UUID`, which Postgres accepts
    on an INSERT and then compares unequal on the next read."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        model = m.ModelConfig(
            tenant_id=tenant, provider="opaas_ai", model="odoo-gpt", locality="cloud"
        )
        db.add(model)
        await db.flush()
        agent.model_config_id = model.id
        await db.flush()
        v1 = await publish_version(db, agent)

        agent.model_config_id = None
        await db.flush()

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()
        assert agent.model_config_id == model.id
        assert isinstance(agent.model_config_id, uuid.UUID)

        # And the other direction: a version with a null must clear it.
        v2_payload = dict(version_payload(v1))
        v2_payload["model_config_id"] = None
        await apply_payload(db, agent, v2_payload)
        await db.flush()
        assert agent.model_config_id is None


async def test_apply_payload_restores_the_pinned_skill_versions(
    app_session: AppSessionFactory,
) -> None:
    """Spec §7's skill-assignment round trip. `skill_assignments` in the payload
    is a list of `{skill_version_id}`, already version-pinned, so a rollback
    restores the exact SKILL versions too -- not merely "this skill is
    assigned"."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        first = await _skill_version(db, tenant)
        second = await _skill_version(db, tenant)

        db.add(
            m.SkillAssignment(
                tenant_id=tenant,
                agent_id=agent.id,
                skill_version_id=first.id,
                enabled=True,
            )
        )
        await db.flush()
        v1 = await publish_version(db, agent)

        # Drop the first, add the second, publish v2.
        rows = (
            (
                await db.execute(
                    select(m.SkillAssignment).where(m.SkillAssignment.agent_id == agent.id)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.enabled = False
        db.add(
            m.SkillAssignment(
                tenant_id=tenant,
                agent_id=agent.id,
                skill_version_id=second.id,
                enabled=True,
            )
        )
        agent.mission = "v2"
        await db.flush()
        await publish_version(db, agent)

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()

        snap = await snapshot_agent(db, agent)
        pinned = {row["skill_version_id"] for row in snap["skill_assignments"]}
        assert pinned == {str(first.id)}


async def test_apply_payload_restores_knowledge_grants_both_ways(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        kept = m.KnowledgeBase(tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e")
        added_later = m.KnowledgeBase(
            tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e"
        )
        db.add_all([kept, added_later])
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant,
                kb_id=kept.id,
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        await db.flush()
        v1 = await publish_version(db, agent)

        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant,
                kb_id=added_later.id,
                grantee_type="agent",
                grantee_id=agent.id,
            )
        )
        await db.flush()

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()

        snap = await snapshot_agent(db, agent)
        assert snap["knowledge_grants"] == [str(kept.id)]


async def test_apply_payload_never_touches_a_department_grant(
    app_session: AppSessionFactory,
) -> None:
    """`knowledge_grant` holds department-wide rows too, keyed by the same
    `grantee_id` column. A rollback that deleted by `grantee_id` alone would
    revoke a whole department's knowledge base because one agent rolled back."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e")
        db.add(kb)
        await db.flush()
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant,
                kb_id=kb.id,
                grantee_type="department",
                grantee_id=agent.department_id,
            )
        )
        await db.flush()
        v1 = await publish_version(db, agent)

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()

        surviving = (
            (
                await db.execute(
                    select(m.KnowledgeGrant).where(
                        m.KnowledgeGrant.grantee_type == "department",
                        m.KnowledgeGrant.grantee_id == agent.department_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(surviving) == 1


async def test_apply_payload_round_trips_to_an_identical_snapshot(
    app_session: AppSessionFactory,
) -> None:
    """The property that makes rollback correct: applying a payload and then
    snapshotting must reproduce that payload exactly. If it does not, the
    version published by the rollback is not the version that was rolled back
    to, and no test of the individual fields would notice a field nobody
    thought to assert on."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        version = await _skill_version(db, tenant)
        kb = m.KnowledgeBase(tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e")
        db.add(kb)
        await db.flush()
        agent.mission = "m"
        agent.narrowing = {"tools": {"odoo": {"enabled": True}}}
        agent.definition = {"oc8_agent": 1, "max_steps": 7}
        db.add_all(
            [
                m.SkillAssignment(
                    tenant_id=tenant,
                    agent_id=agent.id,
                    skill_version_id=version.id,
                    enabled=True,
                ),
                m.KnowledgeGrant(
                    tenant_id=tenant,
                    kb_id=kb.id,
                    grantee_type="agent",
                    grantee_id=agent.id,
                ),
            ]
        )
        await db.flush()
        original = await snapshot_agent(db, agent)

        agent.mission = "different"
        agent.definition = {}
        await db.flush()

        await apply_payload(db, agent, original)
        await db.flush()
        assert await snapshot_agent(db, agent) == original


async def test_apply_payload_disables_a_skill_assigned_after_the_target(
    app_session: AppSessionFactory,
) -> None:
    """The destructive case the round trip hides: a skill added AFTER the
    target version must stop being live, and must be DISABLED rather than
    deleted -- `assign_skill` re-enables an existing row, so the row has to
    survive for a later re-assign (or roll-forward) to find it."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        v1 = await publish_version(db, agent)

        later = await _skill_version(db, tenant)
        row = m.SkillAssignment(
            tenant_id=tenant, agent_id=agent.id, skill_version_id=later.id, enabled=True
        )
        db.add(row)
        await db.flush()

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()

        assert (await snapshot_agent(db, agent))["skill_assignments"] == []
        await db.refresh(row)
        assert row.enabled is False
        assert row.deleted_at is None


async def test_apply_payload_revives_a_soft_deleted_assignment_instead_of_inserting(
    app_session: AppSessionFactory,
) -> None:
    """`uq_skill_assignment_agent` is UNIQUE (agent_id, skill_version_id) WHERE
    agent_id IS NOT NULL -- it does NOT exclude soft-deleted rows. Inserting a
    fresh row next to a soft-deleted one for the same pair would be an
    IntegrityError, i.e. a rollback that 500s."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        version = await _skill_version(db, tenant)
        row = m.SkillAssignment(
            tenant_id=tenant, agent_id=agent.id, skill_version_id=version.id, enabled=True
        )
        db.add(row)
        await db.flush()
        v1 = await publish_version(db, agent)

        row.deleted_at = dt.datetime.now(tz=dt.UTC)
        row.enabled = False
        await db.flush()

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()

        snap = await snapshot_agent(db, agent)
        assert snap["skill_assignments"] == [{"skill_version_id": str(version.id)}]
        count = (
            await db.execute(
                select(func.count())
                .select_from(m.SkillAssignment)
                .where(m.SkillAssignment.agent_id == agent.id)
            )
        ).scalar_one()
        assert count == 1


async def test_apply_payload_never_touches_department_or_tenant_skill_assignments(
    app_session: AppSessionFactory,
) -> None:
    """Inherited assignments are policy, not this agent's configuration, and
    `snapshot_agent` excludes them -- so the inverse must leave them enabled.
    Disabling a department-wide row would strip a skill from every colleague."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        dept_skill = await _skill_version(db, tenant)
        tenant_skill = await _skill_version(db, tenant)
        dept_row = m.SkillAssignment(
            tenant_id=tenant,
            department_id=agent.department_id,
            skill_version_id=dept_skill.id,
            enabled=True,
        )
        tenant_row = m.SkillAssignment(
            tenant_id=tenant, skill_version_id=tenant_skill.id, enabled=True
        )
        db.add_all([dept_row, tenant_row])
        await db.flush()
        v1 = await publish_version(db, agent)

        await apply_payload(db, agent, version_payload(v1))
        await db.flush()
        await db.refresh(dept_row)
        await db.refresh(tenant_row)
        assert dept_row.enabled is True
        assert tenant_row.enabled is True


async def test_apply_payload_ignores_the_meta_key(
    app_session: AppSessionFactory,
) -> None:
    """A raw stored payload (with `_meta`) must not leak provenance onto the
    agent row or trip over the reserved key."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        original = await snapshot_agent(db, agent)
        raw = dict(original)
        raw["_meta"] = {"rolled_back_from": 1}
        agent.mission = "different"
        await db.flush()
        await apply_payload(db, agent, raw)
        await db.flush()
        assert await snapshot_agent(db, agent) == original


async def test_missing_references_names_every_dangling_id(
    app_session: AppSessionFactory,
) -> None:
    """A payload pinned to things that have since been deleted must be
    detectable BEFORE anything is written. Re-creating a knowledge grant on a
    deleted base is the exact hazard `delete_base` removes grants to prevent."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        live_kb = m.KnowledgeBase(
            tenant_id=tenant, name=f"KB-{uuid.uuid4().hex}", embedding_model="e"
        )
        dead_kb = m.KnowledgeBase(
            tenant_id=tenant,
            name=f"KB-{uuid.uuid4().hex}",
            embedding_model="e",
            deleted_at=dt.datetime.now(tz=dt.UTC),
        )
        db.add_all([live_kb, dead_kb])
        live_skill = await _skill_version(db, tenant)
        await db.flush()
        payload = await snapshot_agent(db, agent)
        gone_kb = uuid.uuid4()
        gone_skill = uuid.uuid4()
        gone_model = uuid.uuid4()
        payload["knowledge_grants"] = sorted([str(live_kb.id), str(dead_kb.id), str(gone_kb)])
        payload["skill_assignments"] = [
            {"skill_version_id": str(live_skill.id)},
            {"skill_version_id": str(gone_skill)},
        ]
        payload["model_config_id"] = str(gone_model)

        missing = await missing_references(db, payload)
        assert missing == {
            "knowledge_grants": sorted([str(dead_kb.id), str(gone_kb)]),
            "skill_assignments": [str(gone_skill)],
            "model_config_id": [str(gone_model)],
        }

        payload["knowledge_grants"] = [str(live_kb.id)]
        payload["skill_assignments"] = [{"skill_version_id": str(live_skill.id)}]
        payload["model_config_id"] = None
        assert await missing_references(db, payload) == {}


async def test_missing_references_treats_a_hard_deleted_skill_as_missing(
    app_session: AppSessionFactory,
) -> None:
    """`delete_skill` hard-deletes a skill with no live assignment and leaves
    its `skill_version` rows orphaned; the version row alone is not proof."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        agent = await _make_agent(db, tenant)
        orphan = await _skill_version(db, tenant)
        skill = await db.get(m.Skill, orphan.skill_id)
        assert skill is not None
        await db.delete(skill)
        await db.flush()
        payload = await snapshot_agent(db, agent)
        payload["skill_assignments"] = [{"skill_version_id": str(orphan.id)}]
        assert await missing_references(db, payload) == {"skill_assignments": [str(orphan.id)]}
