"""An agent in a department with an owner runs on the skills its owner
confirmed (`connections.confirmation.held_skill_entries`): a skill added since,
from any path (an admin's tenant-wide assignment, a moved version, a capa
install), waits until the owner confirms again, and the owner's confirmation
keeps holding meanwhile. A removal applies at once. A standard department
loads every assigned skill as before, and a change breaks the binding."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

# Loaded first: importing `oc8.skills.runtime` on its own runs into the
# `oc8.agent` package's import cycle.
import oc8.agent  # noqa: F401
from oc8 import models as m
from oc8.connections.access import unconfirmed_reason
from oc8.connections.confirmation import held_skill_entries, stamp_binding, stamp_trigger
from oc8.connections.consolidate import _Stamps
from oc8.skills.runtime import load_assigned_skills
from tests.conftest import AppSessionFactory
from tests.connections.test_split_confirmation import _bound
from tests.connections.test_split_confirmation import _reason as _standard_reason
from tests.departments.owned import OwnedWorld, owned_world

pytestmark = pytest.mark.asyncio

_RW: dict[str, Any] = {"enabled": True, "read": True, "modify": True}

DEFINITION: dict[str, Any] = {
    "schema_version": 1,
    "slug": "hausstil",
    "version": "1.0.0",
    "instruction": "So schreibst du.",
    "requires": {"tools": [], "kbs": []},
    "guardrails": [],
    "presentation": {},
}


async def _skill(
    app_session: AppSessionFactory, tenant: uuid.UUID, name: str = "Hausstil"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A skill with two versions; returns (v1, v2)."""
    async with app_session(tenant) as db:
        skill = m.Skill(tenant_id=tenant, name=name, origin="store", trust_level="first_party")
        db.add(skill)
        await db.flush()
        ids = []
        for semver in ("1.0.0", "1.1.0"):
            version = m.SkillVersion(
                tenant_id=tenant,
                skill_id=skill.id,
                semver=semver,
                definition={**DEFINITION, "slug": name.lower(), "version": semver},
                artifact_hash=b"x" * 32,
            )
            db.add(version)
            await db.flush()
            ids.append(version.id)
        skill.current_version_id = ids[0]
        return ids[0], ids[1]


async def _assign(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    version_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Assign `version_id` to `agent_id`, or tenant-wide when None."""
    async with app_session(tenant) as db:
        row = m.SkillAssignment(
            tenant_id=tenant,
            agent_id=agent_id,
            skill_version_id=version_id,
            enabled=True,
            overrides={},
        )
        db.add(row)
        await db.flush()
        return row.id


async def _update_assignment(
    app_session: AppSessionFactory, tenant: uuid.UUID, aid: uuid.UUID, **values: Any
) -> None:
    async with app_session(tenant) as db:
        row = await db.get(m.SkillAssignment, aid)
        assert row is not None
        for key, value in values.items():
            setattr(row, key, value)


async def _row(db: Any, w: OwnedWorld) -> m.AgentToolAccess:
    row = await db.scalar(select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == w.agent_id))
    assert row is not None
    return row  # type: ignore[no-any-return]


async def _confirm(app_session: AppSessionFactory, w: OwnedWorld) -> None:
    """The owner confirms the binding again, as it is now."""
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        await stamp_binding(db, await _row(db, w), agent)


async def _loaded(app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: Any) -> list[Any]:
    async with app_session(tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None
        return await load_assigned_skills(db, agent=agent, tenant_id=tenant)


async def _reason(app_session: AppSessionFactory, w: OwnedWorld) -> str | None:
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None and w.credential_id is not None
        return await unconfirmed_reason(
            db,
            await _row(db, w),
            agent,
            "a",
            w.credential_id,
            w.member_id,
            version_id=agent.current_version_id,
        )


async def test_a_new_tenant_skill_waits_for_the_owner(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)  # an admin, tenant-wide

    assert await _loaded(app_session, w.tenant, w.agent_id) == []
    assert await _reason(app_session, w) is None

    # Once the owner confirms again, it runs.
    await _confirm(app_session, w)
    assert [s.skill_version_id for s in await _loaded(app_session, w.tenant, w.agent_id)] == [v1]
    assert await _reason(app_session, w) is None


async def test_a_new_skill_version_runs_the_confirmed_one(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    v1, v2 = await _skill(app_session, w.tenant)
    aid = await _assign(app_session, w.tenant, v1, agent_id=w.agent_id)
    await _confirm(app_session, w)

    await _update_assignment(app_session, w.tenant, aid, skill_version_id=v2)
    loaded = await _loaded(app_session, w.tenant, w.agent_id)
    assert [s.skill_version_id for s in loaded] == [v1]
    assert loaded[0].definition.version == "1.0.0"
    assert await _reason(app_session, w) is None

    # Changed overrides are held the same way.
    await _update_assignment(app_session, w.tenant, aid, overrides={"tone": "loud"})
    assert await _reason(app_session, w) is None


async def test_a_removed_skill_is_gone_at_once(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    v1, _ = await _skill(app_session, w.tenant)
    aid = await _assign(app_session, w.tenant, v1, agent_id=w.agent_id)
    await _confirm(app_session, w)
    assert len(await _loaded(app_session, w.tenant, w.agent_id)) == 1

    await _update_assignment(app_session, w.tenant, aid, enabled=False)
    assert await _loaded(app_session, w.tenant, w.agent_id) == []
    assert await _reason(app_session, w) is None

    # Turned back on without a new confirmation: it was confirmed as it is.
    await _update_assignment(app_session, w.tenant, aid, enabled=True)
    assert len(await _loaded(app_session, w.tenant, w.agent_id)) == 1
    assert await _reason(app_session, w) is None


async def test_a_standard_department_loads_new_skills_and_breaks_the_binding(
    app_session: AppSessionFactory,
) -> None:
    b = await _bound(app_session)
    assert await _standard_reason(app_session, b) is None
    v1, _ = await _skill(app_session, b.tenant)
    await _assign(app_session, b.tenant, v1)

    async with app_session(b.tenant) as db:
        agent = await db.get(m.Agent, b.agent.id)
        assert agent is not None
        assert await held_skill_entries(db, agent) is None
    assert [s.skill_version_id for s in await _loaded(app_session, b.tenant, b.agent.id)] == [v1]
    assert await _standard_reason(app_session, b) == "agent_changed"


async def test_an_owned_agent_without_confirmations_loads_everything(
    app_session: AppSessionFactory,
) -> None:
    w = await owned_world(app_session, tools={"a": _RW})
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        assert await held_skill_entries(db, agent) is None
    assert [s.skill_version_id for s in await _loaded(app_session, w.tenant, w.agent_id)] == [v1]


async def _owner_trigger(
    app_session: AppSessionFactory, w: OwnedWorld, *, creator: uuid.UUID | None
) -> uuid.UUID:
    async with app_session(w.tenant) as db:
        trigger = m.Trigger(
            tenant_id=w.tenant,
            agent_id=w.agent_id,
            kind="cron",
            task_text="t",
            cron_expression="0 9 * * *",
            created_by_member_id=creator,
        )
        db.add(trigger)
        await db.flush()
        await stamp_trigger(db, trigger)
        return trigger.id


async def test_an_owner_trigger_alone_holds_the_skills(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW})
    await _owner_trigger(app_session, w, creator=w.member_id)
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)
    assert await _loaded(app_session, w.tenant, w.agent_id) == []


async def test_a_trigger_by_someone_else_holds_nothing(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW})
    await _owner_trigger(app_session, w, creator=None)
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)
    assert len(await _loaded(app_session, w.tenant, w.agent_id)) == 1


async def test_every_owner_confirmation_must_hold_a_skill(app_session: AppSessionFactory) -> None:
    """The binding is confirmed again with a new skill, the trigger is not:
    the skill still waits for the trigger's confirmation."""
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    await _owner_trigger(app_session, w, creator=w.member_id)
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)
    await _confirm(app_session, w)
    assert await _loaded(app_session, w.tenant, w.agent_id) == []
    assert await _reason(app_session, w) is None


async def test_a_malformed_stored_skill_set_holds_nothing(app_session: AppSessionFactory) -> None:
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1, agent_id=w.agent_id)
    await _confirm(app_session, w)
    async with app_session(w.tenant) as db:
        row = await _row(db, w)
        split = dict(row.binding_split or {})
        grant = dict(split["grant"])
        (aid,) = grant["skills"]
        grant["skills"] = {aid: ["not-a-uuid", {}], "x": "y"}
        row.binding_split = {**split, "grant": grant}
    assert await _loaded(app_session, w.tenant, w.agent_id) == []


async def test_the_consolidation_restamp_does_not_confirm_a_waiting_skill(
    app_session: AppSessionFactory,
) -> None:
    """The pass restamps a confirmation that holds; a skill that only waited
    must keep waiting, since nobody but the owner confirms it."""
    w = await owned_world(app_session, tools={"a": _RW}, tool_key="a")
    await _owner_trigger(app_session, w, creator=w.member_id)
    v1, _ = await _skill(app_session, w.tenant)
    await _assign(app_session, w.tenant, v1)
    async with app_session(w.tenant) as db:
        agent = await db.get(m.Agent, w.agent_id)
        assert agent is not None
        stamps = _Stamps()
        await stamps.collect(db, {agent.id: agent}, set())
        assert len(stamps.bindings) == 1 and len(stamps.triggers) == 1
        await stamps.restamp(db, w.tenant, [])
    assert await _loaded(app_session, w.tenant, w.agent_id) == []
    assert await _reason(app_session, w) is None
