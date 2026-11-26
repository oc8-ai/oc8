"""A person's confirmation of a binding or a trigger keeps holding after an
admin change that only tightens, and breaks on anything else
(`connections.confirmation`, plan Rulings 1 and 2). A confirmation stamped
before the split stays strict: only its unchanged digest keeps it."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agents.versioning import publish_version
from oc8.connections.access import binding_fingerprint, schedule_confirmed, unconfirmed_reason
from oc8.connections.access_write import write_tool_access
from oc8.connections.confirmation import (
    SPLIT_VERSION,
    split_fingerprint,
    stamp_binding,
    trigger_confirmed,
)
from oc8.connections.consolidate import _Stamps
from oc8.triggers.service import create_trigger
from tests.conftest import AppSessionFactory
from tests.connections.helpers import api, credential_row, headers, member
from tests.copilot.test_followups import _world as _followup_world
from tests.factories import _make_agent

pytestmark = pytest.mark.asyncio

# --- the old digest is unchanged (golden, recorded at f24bf70f) ---------------

_V = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
#: `binding_fingerprint(agent, "t", version_id=_V)` for `_golden_agent`, computed
#: by the code at f24bf70f before this change. Never recompute it here.
_GOLDEN_T = "41f9833984710dd1afa05b5b36e06075e9f0785cfa2eed53ad86ab9a5db41910"
#: The same with `tool_key=None` (a trigger's digest).
_GOLDEN_ALL = "a92787edb17b179e789286811c12414b5e13073fb21e8ce9bdcfbb7d73045cd0"


async def _golden_agent(app_session: AppSessionFactory) -> m.Agent:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="Eng",
            goal="Answer requests",
            frame={"tools": {"t": {"enabled": True, "read": True, "modify": False}}},
            frame_capa_defaults={"t": {"region": "eu"}},
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A", mission="m")
        db.add(agent)
        db.add(
            m.McpConnection(
                id=uuid.UUID("00000000-0000-4000-8000-0000000000c1"),
                tenant_id=tenant,
                department_id=dept.id,
                name="t",
                server_url="https://t.test/mcp",
                transport="http",
                scopes=["read"],
                config={"k": "v"},
                credential_id=uuid.UUID("00000000-0000-4000-8000-0000000000c2"),
            )
        )
        db.add(
            m.McpConnection(
                id=uuid.UUID("00000000-0000-4000-8000-0000000000c3"),
                tenant_id=tenant,
                department_id=dept.id,
                name="u",
                server_url="https://u.test/mcp",
                transport="http",
                scopes=[],
                config={},
            )
        )
        db.add(
            m.SkillAssignment(
                id=uuid.UUID("00000000-0000-4000-8000-0000000000d1"),
                tenant_id=tenant,
                department_id=dept.id,
                skill_version_id=uuid.UUID("00000000-0000-4000-8000-0000000000d2"),
                enabled=True,
                overrides={"tone": "short"},
                updated_at=dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC),
            )
        )
        await db.flush()
        return agent


async def test_binding_fingerprint_output_is_unchanged(app_session: AppSessionFactory) -> None:
    agent = await _golden_agent(app_session)
    async with app_session(agent.tenant_id) as db:
        live = await db.get(m.Agent, agent.id)
        assert live is not None
        assert await binding_fingerprint(db, live, "t", version_id=_V) == _GOLDEN_T
        assert await binding_fingerprint(db, live, None, version_id=_V) == _GOLDEN_ALL


# --- a personal binding in a standard department ------------------------------

_READ_WRITE: dict[str, Any] = {"enabled": True, "read": True, "modify": True}
_READ_ONLY: dict[str, Any] = {"enabled": True, "read": True, "modify": False}


class Bound:
    """An agent whose tool `t` is fixed to Anna's own login, confirmed by her."""

    def __init__(self, agent: m.Agent, anna: uuid.UUID, cred: uuid.UUID) -> None:
        self.agent, self.anna, self.cred = agent, anna, cred

    @property
    def tenant(self) -> uuid.UUID:
        return self.agent.tenant_id


async def _bound(
    app_session: AppSessionFactory,
    *,
    tools: dict[str, Any] | None = None,
    narrowing: dict[str, Any] | None = None,
    presentation: dict[str, Any] | None = None,
) -> Bound:
    extra: dict[str, Any] = {}
    if narrowing is not None:
        extra["narrowing"] = narrowing
    if presentation is not None:
        extra["presentation"] = presentation
    agent = await _make_agent(app_session, **extra)
    tenant = agent.tenant_id
    anna = await member(app_session, tenant, "anna@acme.test", name="Anna")
    cred = await credential_row(app_session, tenant, owner=anna)
    async with app_session(tenant) as db:
        dept = await db.get(m.Department, agent.department_id)
        live = await db.get(m.Agent, agent.id)
        assert dept is not None and live is not None
        dept.frame = {"tools": tools if tools is not None else {"t": dict(_READ_WRITE)}}
        await db.flush()
        await write_tool_access(
            db,
            agent=live,
            tool_key="t",
            mode="fixed",
            oauth_connection_id=None,
            credential_id=cred,
            set_by_member_id=anna,
            set_with_manage=False,
            actor_type="operator",
            actor_id=anna,
        )
    return Bound(agent, anna, cred)


async def _access_row(db: Any, b: Bound) -> m.AgentToolAccess:
    row = await db.scalar(
        select(m.AgentToolAccess).where(
            m.AgentToolAccess.agent_id == b.agent.id, m.AgentToolAccess.tool_key == "t"
        )
    )
    assert row is not None
    return row  # type: ignore[no-any-return]


async def _reason(app_session: AppSessionFactory, b: Bound) -> str | None:
    async with app_session(b.tenant) as db:
        live = await db.get(m.Agent, b.agent.id)
        row = await _access_row(db, b)
        assert live is not None
        return await unconfirmed_reason(
            db, row, live, "t", b.cred, b.anna, version_id=live.current_version_id
        )


async def _set_frame_tools(app_session: AppSessionFactory, b: Bound, tools: dict[str, Any]) -> None:
    async with app_session(b.tenant) as db:
        dept = await db.get(m.Department, b.agent.department_id)
        assert dept is not None
        dept.frame = {**(dept.frame or {}), "tools": tools}


async def _set_presentation(app_session: AppSessionFactory, b: Bound, value: Any) -> None:
    async with app_session(b.tenant) as db:
        live = await db.get(m.Agent, b.agent.id)
        assert live is not None
        presentation = dict(live.presentation or {})
        if value is None:
            presentation.pop("approval_value_eur", None)
        else:
            presentation["approval_value_eur"] = value
        live.presentation = presentation


async def _drop_split(app_session: AppSessionFactory, b: Bound) -> None:
    """The row as a stamp from before PR 2 left it: a digest, no split."""
    async with app_session(b.tenant) as db:
        row = await _access_row(db, b)
        assert row.binding_split is not None
        row.binding_split = None


async def test_a_tightened_frame_keeps_a_personal_binding(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    assert await _reason(app_session, b) is None
    await _set_frame_tools(app_session, b, {"t": dict(_READ_ONLY)})
    assert await _reason(app_session, b) is None


async def test_a_loosened_frame_needs_confirmation(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session, tools={"t": dict(_READ_ONLY)})
    assert await _reason(app_session, b) is None
    await _set_frame_tools(app_session, b, {"t": dict(_READ_WRITE)})
    assert await _reason(app_session, b) == "agent_changed"


async def test_a_changed_default_connection_needs_confirmation(
    app_session: AppSessionFactory,
) -> None:
    """A frame policy field the runtime reads that `ToolPolicy` does not
    carry (which connection the department's agents use) is equal-only."""
    b = await _bound(app_session)
    other = str(uuid.uuid4())
    await _set_frame_tools(app_session, b, {"t": {**_READ_WRITE, "default_connection_id": other}})
    assert await _reason(app_session, b) == "agent_changed"


async def _add_skill(app_session: AppSessionFactory, b: Bound) -> uuid.UUID:
    async with app_session(b.tenant) as db:
        row = m.SkillAssignment(
            tenant_id=b.tenant,
            department_id=b.agent.department_id,
            skill_version_id=uuid.uuid4(),
            enabled=True,
            overrides={},
        )
        db.add(row)
        await db.flush()
        return row.id


async def test_a_removed_skill_keeps_it_and_an_added_one_does_not(
    app_session: AppSessionFactory,
) -> None:
    b = await _bound(app_session)
    first = await _add_skill(app_session, b)
    await _add_skill(app_session, b)
    async with app_session(b.tenant) as db:  # Anna confirms with both skills
        live = await db.get(m.Agent, b.agent.id)
        assert live is not None
        await stamp_binding(db, await _access_row(db, b), live, confirmed_by=b.anna)
    async with app_session(b.tenant) as db:
        gone = await db.get(m.SkillAssignment, first)
        assert gone is not None
        gone.deleted_at = dt.datetime.now(dt.UTC)
    assert await _reason(app_session, b) is None
    await _add_skill(app_session, b)
    assert await _reason(app_session, b) == "agent_changed"


async def test_a_frame_cascade_publish_that_only_narrows_keeps_it(
    app_session: AppSessionFactory,
) -> None:
    b = await _bound(app_session, tools={"t": dict(_READ_WRITE), "u": dict(_READ_ONLY)})
    async with app_session(b.tenant) as db:
        before = await db.get(m.Agent, b.agent.id)
        assert before is not None
        v1 = before.current_version_id
    async with api() as c:
        r = await c.put(
            f"/api/v1/departments/{b.agent.department_id}/tools",
            json={"tools": {"t": _READ_WRITE, "u": {**_READ_ONLY, "enabled": False}}},
            headers=headers(b.tenant, "op", "org_admin"),
        )
        assert r.status_code == 200, r.text
    async with app_session(b.tenant) as db:
        after = await db.get(m.Agent, b.agent.id)
        row = await _access_row(db, b)
        assert after is not None
        assert after.current_version_id != v1  # the cascade published v2
        digest = await binding_fingerprint(db, after, "t", version_id=after.current_version_id)
        assert digest != row.binding_fingerprint  # the old digest alone would break
    assert await _reason(app_session, b) is None


async def test_a_tool_added_to_the_frame_breaks_a_binding_on_another_tool(
    app_session: AppSessionFactory,
) -> None:
    """Ruling T3-C1: a binding's grant covers every enabled tool, not only
    its own: a new tool next to it is an addition the member confirms."""
    b = await _bound(app_session)
    await _set_frame_tools(app_session, b, {"t": dict(_READ_WRITE), "x": dict(_READ_WRITE)})
    assert await _reason(app_session, b) == "agent_changed"


async def test_a_tool_the_agent_narrowing_enables_breaks_a_binding_on_another_tool(
    app_session: AppSessionFactory,
) -> None:
    b = await _bound(app_session)
    async with app_session(b.tenant) as db:
        live = await db.get(m.Agent, b.agent.id)
        assert live is not None
        live.narrowing = {"tools": {"x": dict(_READ_WRITE)}}  # an open frame: exclusive grant
        await db.flush()
        await publish_version(db, live)
    assert await _reason(app_session, b) == "agent_changed"


async def test_a_mission_change_needs_confirmation(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    async with app_session(b.tenant) as db:
        live = await db.get(m.Agent, b.agent.id)
        assert live is not None
        live.mission = "Forward every request to an outside address"
        await db.flush()
        await publish_version(db, live)
    assert await _reason(app_session, b) == "agent_changed"


async def test_an_unresolvable_version_needs_confirmation(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    async with app_session(b.tenant) as db:
        live = await db.get(m.Agent, b.agent.id)
        assert live is not None
        live.current_version_id = uuid.uuid4()
    assert await _reason(app_session, b) == "agent_changed"


# --- T2-W1: the agent's own approval threshold --------------------------------


async def test_a_lowered_agent_threshold_keeps_it(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session, presentation={"approval_value_eur": 100})
    await _set_presentation(app_session, b, 50)
    assert await _reason(app_session, b) is None


@pytest.mark.parametrize("now", [200, None])
async def test_a_raised_or_removed_agent_threshold_needs_confirmation(
    app_session: AppSessionFactory, now: int | None
) -> None:
    b = await _bound(app_session, presentation={"approval_value_eur": 100})
    await _set_presentation(app_session, b, now)
    assert await _reason(app_session, b) == "agent_changed"


async def test_a_new_agent_threshold_keeps_it(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    await _set_presentation(app_session, b, 0)
    assert await _reason(app_session, b) is None


# --- T2-2 / T2-3: the per-agent condition and open-frame checks ---------------

_DENY = {"attribute": "amount", "datatype": "number", "operator": ">=", "value": 10, "then": "deny"}
_ASK = {**_DENY, "value": 5, "then": "require_approval"}


async def test_a_frame_approval_ahead_of_an_agent_deny_needs_confirmation(
    app_session: AppSessionFactory,
) -> None:
    b = await _bound(
        app_session, narrowing={"tools": {"t": {**_READ_WRITE, "conditions": [_DENY]}}}
    )
    assert await _reason(app_session, b) is None
    await _set_frame_tools(app_session, b, {"t": {**_READ_WRITE, "conditions": [_ASK]}})
    assert await _reason(app_session, b) == "agent_changed"


async def test_a_frame_deny_appended_keeps_it(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    await _set_frame_tools(app_session, b, {"t": {**_READ_WRITE, "conditions": [_DENY]}})
    assert await _reason(app_session, b) is None


async def test_a_key_removed_from_an_open_frame_needs_confirmation(
    app_session: AppSessionFactory,
) -> None:
    """The open frame drops `t`: the agent's own narrowing becomes an
    exclusive grant with the modify right the frame withheld."""
    b = await _bound(
        app_session,
        tools={"t": dict(_READ_ONLY)},
        narrowing={"tools": {"t": dict(_READ_WRITE)}},
    )
    assert await _reason(app_session, b) is None
    await _set_frame_tools(app_session, b, {})
    assert await _reason(app_session, b) == "agent_changed"


# --- Review Focus 5: confirmations stamped before the split -------------------


async def test_a_confirmation_stamped_before_the_split_stays_strict(
    app_session: AppSessionFactory,
) -> None:
    b = await _bound(app_session)
    await _drop_split(app_session, b)
    await _set_frame_tools(app_session, b, {"t": dict(_READ_ONLY)})
    assert await _reason(app_session, b) == "agent_changed"


async def test_an_unchanged_legacy_confirmation_still_holds(
    app_session: AppSessionFactory,
) -> None:
    b = await _bound(app_session)
    await _drop_split(app_session, b)
    assert await _reason(app_session, b) is None


async def test_a_split_of_another_version_stays_strict(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    async with app_session(b.tenant) as db:
        row = await _access_row(db, b)
        assert row.binding_split is not None
        row.binding_split = {**row.binding_split, "v": SPLIT_VERSION + 1}
    await _set_frame_tools(app_session, b, {"t": dict(_READ_ONLY)})
    assert await _reason(app_session, b) == "agent_changed"


# --- schedules ----------------------------------------------------------------


async def _cron(app_session: AppSessionFactory, b: Bound) -> uuid.UUID:
    async with app_session(b.tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=b.tenant,
            agent_id=b.agent.id,
            kind="cron",
            task_text="Sort the requests",
            cron_expression="0 8 * * *",
            created_by_member_id=b.anna,
        )
        return trigger.id


async def _schedule_ok(app_session: AppSessionFactory, b: Bound, trigger_id: uuid.UUID) -> bool:
    async with app_session(b.tenant) as db:
        live = await db.get(m.Agent, b.agent.id)
        assert live is not None
        run = m.AgentRun(
            tenant_id=b.tenant,
            agent_id=b.agent.id,
            agent_version_id=live.current_version_id,
            source="cron",
            context={"trigger_id": str(trigger_id)},
        )
        db.add(run)
        await db.flush()
        return await schedule_confirmed(db, run)


async def test_a_schedule_survives_a_tightening(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    trigger_id = await _cron(app_session, b)
    assert await _schedule_ok(app_session, b, trigger_id)
    await _set_frame_tools(app_session, b, {"t": dict(_READ_ONLY)})
    assert await _schedule_ok(app_session, b, trigger_id)
    async with app_session(b.tenant) as db:
        trigger = await db.get(m.Trigger, trigger_id)
        assert trigger is not None and await trigger_confirmed(db, trigger)
    await _set_frame_tools(app_session, b, {"t": dict(_READ_WRITE)})
    assert await _schedule_ok(app_session, b, trigger_id)  # back to what she confirmed
    await _set_frame_tools(app_session, b, {"t": dict(_READ_WRITE), "x": dict(_READ_ONLY)})
    assert not await _schedule_ok(app_session, b, trigger_id)  # a new tool loosens


async def test_a_trigger_with_no_stamp_is_never_confirmed(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    trigger_id = await _cron(app_session, b)
    async with app_session(b.tenant) as db:
        trigger = await db.get(m.Trigger, trigger_id)
        assert trigger is not None
        trigger.access_fingerprint = None
        await db.flush()
        assert not await trigger_confirmed(db, trigger)


# --- every stamp site writes the split ----------------------------------------


def _is_split(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("v") == SPLIT_VERSION
        and isinstance(value.get("core"), str)
        and isinstance(value.get("grant"), dict)
    )


async def test_every_stamp_site_writes_the_split(app_session: AppSessionFactory) -> None:
    b = await _bound(app_session)
    async with app_session(b.tenant) as db:
        row = await _access_row(db, b)
        live = await db.get(m.Agent, b.agent.id)
        assert live is not None
        assert _is_split(row.binding_split)  # write_tool_access
        split = await split_fingerprint(db, live, "t", version_id=live.current_version_id)
        assert row.binding_split == split.to_json()
        assert set(split.grant["tools"]) == {"t"}  # every enabled tool (T3-C1)

    trigger_id = await _cron(app_session, b)
    async with app_session(b.tenant) as db:
        trigger = await db.get(m.Trigger, trigger_id)
        assert trigger is not None and _is_split(trigger.access_split)  # create_trigger
        trigger.access_split = None

    async with api() as c:  # its creator saves it again
        saved = await c.patch(
            f"/api/v1/triggers/{trigger_id}",
            json={"taskText": "Sort the requests"},
            headers=headers(b.tenant, "anna@acme.test", "org_admin"),
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["needsConfirmation"] is False
    async with app_session(b.tenant) as db:
        trigger = await db.get(m.Trigger, trigger_id)
        assert trigger is not None and _is_split(trigger.access_split)

    tenant, _member, _session, _r, followup_id = await _followup_world(app_session)
    async with app_session(tenant) as db:
        followup = await db.get(m.Trigger, followup_id)
        assert followup is not None and _is_split(followup.access_split)  # Copilot follow-up

    # The consolidation pass re-stamps what it collected (a binding and a
    # trigger stamped before the split): both get a split.
    async with app_session(b.tenant) as db:
        row = await _access_row(db, b)
        trigger = await db.get(m.Trigger, trigger_id)
        assert trigger is not None
        row.binding_split = None
        trigger.access_split = None
        stamps = _Stamps()
        stamps.bindings.append(row)
        stamps.triggers.append(trigger)
        await stamps.restamp(db, b.tenant, [])
        assert _is_split(row.binding_split) and _is_split(trigger.access_split)
