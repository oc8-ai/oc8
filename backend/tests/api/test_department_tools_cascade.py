from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.pdp import effective_tool_policies
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_enabling_a_department_tool_cascades_to_agents_with_no_override(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        never_touched = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", narrowing={})
        # Provenance is tracked in `narrowing_overridden_keys`, NOT inferred
        # from `narrowing` itself (see `set_department_tools`'s own code
        # comment) -- this fixture sets it directly to stand in for "an
        # operator previously saved this via `PUT /agents/{id}/narrowing`",
        # which is exactly what `set_narrowing` would have recorded for real.
        overridden = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Max",
            narrowing={"tools": {"Odoo": {"enabled": False}}},
            narrowing_overridden_keys=["Odoo"],
        )
        db.add_all([never_touched, overridden])
        await db.flush()
        dept_id, never_touched_id, overridden_id = dept.id, never_touched.id, overridden.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": True}}},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        reloaded_never_touched = await db.get(m.Agent, never_touched_id)
        reloaded_overridden = await db.get(m.Agent, overridden_id)
        assert reloaded_never_touched is not None and reloaded_overridden is not None
        assert reloaded_never_touched.narrowing["tools"]["Odoo"]["enabled"] is True
        assert reloaded_overridden.narrowing["tools"]["Odoo"]["enabled"] is False


async def test_disabling_a_department_tool_cascades_off_for_a_never_touched_agent(
    app_session: AppSessionFactory,
) -> None:
    """Symmetric OFF case: the department default flips to False and the
    never-touched agent follows it down."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant, name="Sales", frame={"tools": {"Odoo": {"enabled": True}}}
        )
        db.add(dept)
        await db.flush()
        never_touched = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", narrowing={})
        db.add(never_touched)
        await db.flush()
        dept_id, never_touched_id = dept.id, never_touched.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": False}}},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        reloaded_never_touched = await db.get(m.Agent, never_touched_id)
        assert reloaded_never_touched is not None
        assert reloaded_never_touched.narrowing["tools"]["Odoo"]["enabled"] is False


async def test_cascade_preserves_read_write_send_not_just_enabled(
    app_session: AppSessionFactory,
) -> None:
    """A sparse `{"enabled": ...}` cascade write is still a non-None
    narrowing term for `effective_tool_policies`'s intersection, so every
    OTHER field (read/write/send) would default to False there instead of
    falling through to the frame's own grant -- discovered live (agent tool
    login selection design, Task 10 final verification) with a department
    that grants read/write beyond a bare `enabled` toggle."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        never_touched = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", narrowing={})
        db.add(never_touched)
        await db.flush()
        dept_id, never_touched_id = dept.id, never_touched.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": True, "read": True, "modify": True}}},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        reloaded_dept = await db.get(m.Department, dept_id)
        reloaded_agent = await db.get(m.Agent, never_touched_id)
        assert reloaded_dept is not None and reloaded_agent is not None
        effective = effective_tool_policies(
            reloaded_dept.frame or {}, reloaded_agent.narrowing or {}
        )
        assert effective["Odoo"].enabled is True
        assert effective["Odoo"].read is True
        assert effective["Odoo"].modify is True


async def test_new_agent_created_after_the_toggle_inherits_the_current_default(
    app_session: AppSessionFactory,
) -> None:
    """An agent hired AFTER the department toggle was already flipped never
    gets its own `narrowing.tools` entry written by hire -- it inherits the
    frame's current default purely through `effective_tool_policies`'s
    frame-narrowing intersection (no stored narrowing entry at all), which is
    exactly the same "no entry -- inherit" contract the cascade itself relies
    on."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        dept_id = dept.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": True}}},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        new_agent = m.Agent(tenant_id=tenant, department_id=dept_id, name="Lena", narrowing={})
        db.add(new_agent)
        await db.flush()
        new_agent_id = new_agent.id

    async with app_session(tenant) as db:
        reloaded_dept = await db.get(m.Department, dept_id)
        reloaded_agent = await db.get(m.Agent, new_agent_id)
        assert reloaded_dept is not None and reloaded_agent is not None
        # No cascade touched this agent -- it was created after the PUT, so
        # its own `narrowing` is still `{}`. It must nonetheless resolve to
        # the department's live default via the frame/narrowing intersection.
        assert reloaded_agent.narrowing == {}
        effective = effective_tool_policies(reloaded_dept.frame, reloaded_agent.narrowing)
        assert effective["Odoo"].enabled is True


async def test_a_repeated_put_re_cascades_an_untouched_agent_instead_of_freezing_it(
    app_session: AppSessionFactory,
) -> None:
    """Regression for the original Critical bug found in review: `PUT
    .../tools` is a full-map REPLACE and the frontend always re-emits the
    whole tools map on every save. Before the first fix, PUT #1 (Odoo
    enabled=True) materialized a concrete `narrowing["tools"]["Odoo"]` entry
    on the never-touched agent -- which then made that entry indistinguishable
    from a real operator override, so PUT #2 (Odoo enabled=False) skipped the
    agent and it stayed stuck at enabled=True forever. Provenance is now
    tracked via `Agent.narrowing_overridden_keys`, which this never-touched
    agent never enters (only `PUT /agents/{id}/narrowing` adds to it) -- so
    the cascade is free to keep rewriting its `narrowing["tools"]["Odoo"]`
    entry on every department toggle, however many times."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales", frame={"tools": {}})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Nora", narrowing={})
        db.add(agent)
        await db.flush()
        dept_id, agent_id = dept.id, agent.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r1 = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": True}}},
                headers=_headers(tenant),
            )
            assert r1.status_code == 200, r1.text

    async with app_session(tenant) as db:
        reloaded_agent = await db.get(m.Agent, agent_id)
        assert reloaded_agent is not None
        # First PUT correctly cascades the never-touched agent up to True --
        # true regardless of the bug, checked here for setup sanity.
        assert reloaded_agent.narrowing["tools"]["Odoo"]["enabled"] is True
        assert reloaded_agent.narrowing_overridden_keys == []

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            # Same tool, flipped off, nothing else changed -- exactly what the
            # full-map-replace frontend sends on a real second save.
            r2 = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": False}}},
                headers=_headers(tenant),
            )
            assert r2.status_code == 200, r2.text

    async with app_session(tenant) as db:
        reloaded_agent = await db.get(m.Agent, agent_id)
        assert reloaded_agent is not None
        # The cascade must fire again on the second PUT -- checked directly
        # against the agent's OWN stored narrowing, not through the frame/
        # narrowing AND-intersection (which happens to collapse to the right
        # answer on a True->False transition regardless of whether the
        # cascade actually ran, masking the bug at the effective-policy
        # level). The regression is that the raw stored value never gets
        # rewritten at all -- this is the direct, unmasked check for that.
        assert reloaded_agent.narrowing["tools"]["Odoo"]["enabled"] is False


async def test_an_unchanged_key_does_not_re_cascade_on_a_later_put_that_changes_a_different_key(
    app_session: AppSessionFactory,
) -> None:
    """Per-key diffing, not a whole-PUT "did anything change" flag: PUT #1
    changes tool A only; PUT #2 (full-map replace, so it re-submits BOTH A and
    B) changes tool B only. Tool A's value is identical across both PUTs, so
    it must not re-cascade on PUT #2 -- confirmed here by having tool A's
    never-touched agent already carry an explicit override from a prior save,
    which a real per-key diff leaves alone but a whole-PUT flag (cascading
    every key whenever ANYTHING in the payload changed) would have no reason
    to skip either, so this alone does not fully separate the two behaviors;
    the two tests above (which look at a single key across sequential PUTs)
    are what actually pin down per-key diffing -- this test additionally
    guards that a second, unrelated key changing in the same full-map PUT
    does not disturb tool A's already-settled state at all."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant, name="Sales", frame={"tools": {"A": {"enabled": True}}}
        )
        db.add(dept)
        await db.flush()
        # This agent explicitly overrode tool A to False already -- a real
        # per-key diff must never touch it again, on any future PUT, since A
        # never changes value from here on.
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Max",
            narrowing={"tools": {"A": {"enabled": False}}},
            narrowing_overridden_keys=["A"],
        )
        db.add(agent)
        await db.flush()
        dept_id, agent_id = dept.id, agent.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            # Full-map replace: A stays enabled=True (unchanged), B is newly
            # introduced and enabled=True (changed from absent/False).
            r = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"A": {"enabled": True}, "B": {"enabled": True}}},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        reloaded_agent = await db.get(m.Agent, agent_id)
        assert reloaded_agent is not None
        # Tool A's override survives untouched -- its value never changed.
        assert reloaded_agent.narrowing["tools"]["A"]["enabled"] is False
        # Tool B is new/changed, so it DOES cascade to this never-touched-on-B agent.
        assert reloaded_agent.narrowing["tools"]["B"]["enabled"] is True


async def test_a_real_operator_override_survives_every_department_toggle_even_when_values_coincide(
    app_session: AppSessionFactory,
) -> None:
    """Closes the residual gap a previous, value-comparison-based fix left
    open (flagged by review as Critical, not a narrow edge case): with only
    `agent.narrowing["tools"][key]`'s stored VALUE to go on, "the agent's
    value happens to equal the department's OLD default at the time of some
    later PUT" is a near-inevitable eventual state for any standing operator
    override on a two-valued boolean field, given enough department-level
    toggles -- and a value-comparison heuristic has no way to tell that state
    apart from cascade residue. It would have silently swept the override
    below, with no error and no audit trail.

    This test sets up a REAL operator override via an actual
    `PUT /agents/{id}/narrowing` call (Task 4's endpoint, the only writer of
    `narrowing_overridden_keys`) -- chosen, deliberately, to equal the
    department's CURRENT default at the moment it's set (`True` while the
    department is `True`), which is exactly the shape that used to be
    indistinguishable from cascade residue. The department then toggles the
    same key through three more PUTs, including one whose OLD value (`True`)
    transiently coincides with the agent's own stored value again. The
    agent's operator-set narrowing must never move, at any point, regardless
    of what the department's value does -- because provenance is now tracked
    by WHICH ENDPOINT wrote it, not inferred from value equality."""
    tenant = uuid.uuid4()
    # `PUT /agents/{id}/narrowing`'s own response serialization
    # (`_agent_detail_dto`) builds a `ToolPolicyDTO` per frame tool key and
    # requires the full read/write/send shape (a pre-existing constraint,
    # unrelated to this fix -- see test_agent_narrowing_connection_required.py's
    # identically-shaped `_FRAME_TOOL_POLICY`), so this fixture seeds the
    # frame fully-shaped rather than the bare `{"enabled": True}}` the
    # department-PUT-only tests above use (those never hit that DTO, since
    # `set_department_tools` never calls it).
    _frame_tool_policy = {"enabled": True, "read": True, "modify": False}
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant, name="Sales", frame={"tools": {"Odoo": dict(_frame_tool_policy)}}
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Max", narrowing={})
        db.add(agent)
        await db.flush()
        dept_id, agent_id = dept.id, agent.id

    # A REAL operator override, via the real endpoint -- value (True)
    # deliberately equal to the department's CURRENT default (True) right now.
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r0 = await c.put(
                f"/api/v1/agents/{agent_id}/narrowing",
                json={"narrowing": {"tools": {"Odoo": {"enabled": True}}}},
                headers=_headers(tenant),
            )
            assert r0.status_code == 200, r0.text

    async with app_session(tenant) as db:
        reloaded_agent = await db.get(m.Agent, agent_id)
        assert reloaded_agent is not None
        assert reloaded_agent.narrowing_overridden_keys == ["Odoo"]
        assert reloaded_agent.narrowing["tools"]["Odoo"]["enabled"] is True

    # Department PUT #1: True -> False. OLD value (True) equals the agent's
    # own stored value -- exactly the coincidence a value-comparison heuristic
    # would have mistaken for cascade residue and swept.
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r1 = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": False}}},
                headers=_headers(tenant),
            )
            assert r1.status_code == 200, r1.text

    async with app_session(tenant) as db:
        reloaded_agent = await db.get(m.Agent, agent_id)
        assert reloaded_agent is not None
        assert reloaded_agent.narrowing["tools"]["Odoo"]["enabled"] is True

    # Department PUT #2: False -> True.
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r2 = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": True}}},
                headers=_headers(tenant),
            )
            assert r2.status_code == 200, r2.text

    async with app_session(tenant) as db:
        reloaded_agent = await db.get(m.Agent, agent_id)
        assert reloaded_agent is not None
        assert reloaded_agent.narrowing["tools"]["Odoo"]["enabled"] is True

    # Department PUT #3: True -> False again. OLD value (True) transiently
    # coincides with the agent's own stored value a SECOND time.
    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r3 = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": False}}},
                headers=_headers(tenant),
            )
            assert r3.status_code == 200, r3.text

    async with app_session(tenant) as db:
        reloaded_agent = await db.get(m.Agent, agent_id)
        assert reloaded_agent is not None
        # The operator's choice never moved, through any of the three
        # department toggles, however many times the department's OLD value
        # happened to coincide with it.
        assert reloaded_agent.narrowing["tools"]["Odoo"]["enabled"] is True
        assert reloaded_agent.narrowing_overridden_keys == ["Odoo"]


async def test_a_cascade_is_published_so_the_next_run_actually_gets_the_tool(
    app_session: AppSessionFactory,
) -> None:
    """Runs execute an agent's PUBLISHED version. A cascade that only wrote the
    live row would re-enable a tool that no run of this agent ever sees -- the
    frame cannot widen a pinned narrowing that still says "disabled". An agent
    with an operator draft in progress is not published behind that operator's
    back: the cascade joins the draft instead."""
    from oc8.agents.versioning import draft_status, publish_version, resolve_version

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant, name="Sales", frame={"tools": {"Odoo": {"enabled": False}}}
        )
        db.add(dept)
        await db.flush()
        clean = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Nora",
            narrowing={"tools": {"Odoo": {"enabled": False}}},
        )
        mid_edit = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Max",
            mission="published",
            narrowing={"tools": {"Odoo": {"enabled": False}}},
        )
        db.add_all([clean, mid_edit])
        await db.flush()
        await publish_version(db, clean, note="v1")
        await publish_version(db, mid_edit, note="v1")
        mid_edit.mission = "an unfinished edit"
        await db.flush()
        dept_id, clean_id, mid_edit_id = dept.id, clean.id, mid_edit.id

    app = create_app()
    async with LifespanManager(app):
        async with _client(app) as c:
            r = await c.put(
                f"/api/v1/departments/{dept_id}/tools",
                json={"tools": {"Odoo": {"enabled": True}}},
                headers=_headers(tenant),
            )
            assert r.status_code == 200, r.text

    async with app_session(tenant) as db:
        reloaded_clean = await db.get(m.Agent, clean_id)
        reloaded_mid_edit = await db.get(m.Agent, mid_edit_id)
        assert reloaded_clean is not None and reloaded_mid_edit is not None

        pinned = await resolve_version(db, None, reloaded_clean)
        assert pinned["narrowing"]["tools"]["Odoo"]["enabled"] is True
        assert (await draft_status(db, reloaded_clean)).dirty is False

        pinned = await resolve_version(db, None, reloaded_mid_edit)
        assert pinned["mission"] == "published", "the operator's draft was not pushed live"
        assert pinned["narrowing"]["tools"]["Odoo"]["enabled"] is False
        status = await draft_status(db, reloaded_mid_edit)
        assert status.dirty is True
        assert "narrowing.tools" in status.changed_fields
