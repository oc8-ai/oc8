"""A personal hire binds the member's own login and creates the template's
triggers as the member's own (plan Rulings 9, 10 and 11).

`POST /personal-department/agents` takes the login the member chose for one of
the template's `connections_any` (and any of its `connections_optional`), with
`confirmShare`. The binding is the owner's own confirmation; the template's
cron triggers, and the event triggers of the bound capa, are created by the
member acting for themselves. A refused hire leaves nothing behind.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.scope import subject_uuid_for
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.service import install_plugin
from oc8.connections.access import unconfirmed_reason
from oc8.connections.confirmation import trigger_confirmed
from oc8.departments.personal import ENABLED_KEY, FRAME_KEY, MAX_AGENTS_KEY
from oc8.events import sources
from oc8.events.sources import poll_tick, register_poller
from oc8.main import create_app
from oc8.models.core import DEPARTMENT_KIND_PERSONAL
from tests.conftest import AppSessionFactory
from tests.events.polled import (
    CREDENTIAL_TYPE,
    EVENT_TYPE,
    FakePoller,
    baseline_then,
    install_capa,
    install_oauth_capa,
    login,
    oauth_login,
    use_kek,
)

pytestmark = pytest.mark.asyncio

_SUBJECT = "ada"
_TOOLS = {"enabled": True, "read": True, "modify": True}


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    use_kek(monkeypatch)


@pytest.fixture(autouse=True)
def _own_pollers() -> Iterator[None]:
    saved = dict(sources._POLLERS)
    yield
    sources._POLLERS.clear()
    sources._POLLERS.update(saved)


def _headers(tenant: uuid.UUID, subject: str = _SUBJECT) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="member")
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@dataclass
class World:
    tenant: uuid.UUID
    member_id: uuid.UUID
    #: The polled test capa the template's `connections_any` names.
    source: str
    tool_key: str
    #: The member's own login of that capa's type.
    credential_id: uuid.UUID
    fake: FakePoller


async def _member(app_session: AppSessionFactory, tenant: uuid.UUID, subject: str) -> uuid.UUID:
    async with app_session(tenant) as db:
        row = m.OrgMember(
            tenant_id=tenant,
            subject=subject,
            subject_uuid=subject_uuid_for(subject),
            display_name=subject.title(),
        )
        db.add(row)
        await db.flush()
        return row.id


async def _world(
    app_session: AppSessionFactory,
    *,
    in_frame: bool = True,
    settings: dict[str, Any] | None = None,
) -> World:
    """A tenant with personal departments on, the member, the polled test capa
    (its tools in the personal default frame unless `in_frame` is False), the
    member's own login of it, and a poller that finds nothing."""
    tenant = uuid.uuid4()
    source = f"ev_{uuid.uuid4().hex[:10]}"
    tool_key = f"{source}_tools"
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t-{tenant.hex[:8]}",
                name="T",
                settings={
                    ENABLED_KEY: True,
                    FRAME_KEY: {tool_key: dict(_TOOLS)} if in_frame else {},
                    **(settings or {}),
                },
            )
        )
    member_id = await _member(app_session, tenant, _SUBJECT)
    await install_capa(app_session, tenant, source, tool_key)
    credential_id = await login(app_session, tenant, owner=member_id)
    fake = FakePoller(baseline_then(lambda _c: []))
    register_poller(source, EVENT_TYPE, fake, credential_type=CREDENTIAL_TYPE)
    return World(tenant, member_id, source, tool_key, credential_id, fake)


def _template(name: str, **agent: Any) -> dict[str, Any]:
    return {
        "name": name,
        "version": "1.0.0",
        "type": "agent_template",
        "trust": "first_party",
        "summary": f"{name} summary",
        "agent_template": {
            "name": "Mia",
            "mission": "Keep the owner's day in order.",
            "scope": "personal",
            **agent,
        },
    }


def _cron(task: str = "Plan the day", expression: str = "0 7 * * *") -> dict[str, Any]:
    return {"kind": "cron", "cron_expression": expression, "task_text": task}


def _event(source: str, *, config: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "kind": "event",
        "source": source,
        "event_type": EVENT_TYPE,
        "task_text": "Sort what came in",
        "config": config or {},
    }


async def _capa(app_session: AppSessionFactory, tenant: uuid.UUID, manifest: dict[str, Any]) -> str:
    async with app_session(tenant) as db:
        version = await install_plugin(db, tenant_id=tenant, manifest_data=manifest)
        await enable_plugin(db, tenant_id=tenant, capa_id=version.capa_id, granted_permissions=[])
        return str(version.capa_id)


async def _hire(tenant: uuid.UUID, body: dict[str, Any]) -> Any:
    async with _http() as http:
        return await http.post(
            "/api/v1/personal-department/agents", json=body, headers=_headers(tenant)
        )


def _own(world: World, credential_id: uuid.UUID | None = None) -> dict[str, Any]:
    return {"integration": world.source, "credentialId": str(credential_id or world.credential_id)}


async def _nothing_left(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    """A refused hire left no department, agent, binding or trigger."""
    async with app_session(tenant) as db:
        for model in (m.Agent, m.AgentToolAccess, m.Trigger):
            assert (await db.execute(select(model.id))).first() is None, model.__name__
        assert (
            await db.execute(
                select(m.Department.id).where(m.Department.kind == DEPARTMENT_KIND_PERSONAL)
            )
        ).first() is None


async def _triggers(
    app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID
) -> list[m.Trigger]:
    async with app_session(tenant) as db:
        return list(
            (await db.execute(select(m.Trigger).where(m.Trigger.agent_id == agent_id))).scalars()
        )


# --- the hire --------------------------------------------------------------------------


async def test_hire_binds_the_members_own_login_and_creates_their_triggers(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    cid = await _capa(
        app_session,
        world.tenant,
        _template(
            "helper",
            connections_any=[world.source],
            triggers=[_cron(), _event(world.source, config={"folder": "today"})],
        ),
    )

    r = await _hire(world.tenant, {"capaId": cid, "connection": _own(world), "confirmShare": True})

    assert r.status_code == 201, r.text
    agent_id = uuid.UUID(r.json()["agentId"])
    async with app_session(world.tenant) as db:
        agent = await db.get(m.Agent, agent_id)
        assert agent is not None and agent.current_version_id is not None
        row = await db.scalar(
            select(m.AgentToolAccess).where(m.AgentToolAccess.agent_id == agent_id)
        )
        assert row is not None
        assert row.tool_key == world.tool_key
        assert row.mode == "fixed"
        assert row.credential_id == world.credential_id
        assert row.oauth_connection_id is None
        assert row.set_by_member_id == world.member_id
        assert row.set_with_manage is False
        assert row.binding_split is not None and row.binding_split["v"] == 1
        assert (
            await unconfirmed_reason(
                db,
                row,
                agent,
                world.tool_key,
                world.credential_id,
                world.member_id,
                version_id=agent.current_version_id,
            )
            is None
        )
        # The binding was the owner's own confirmation.
        assert agent.confirmed_skills is not None
        audit = (
            await db.execute(
                select(m.AuditEvent).where(m.AuditEvent.action == "agent.tool_access.updated")
            )
        ).scalar_one()
        assert audit.resource["consent"] == "owner"
        assert audit.resource["confirm_share"] is True
        triggers = list(
            (await db.execute(select(m.Trigger).where(m.Trigger.agent_id == agent_id))).scalars()
        )
        assert {t.kind for t in triggers} == {"cron", "event"}
        for trigger in triggers:
            assert trigger.created_by_member_id == world.member_id
            assert trigger.acts_for_creator is True
            assert trigger.enabled is True
            assert await trigger_confirmed(db, trigger), trigger.kind
        (event_trigger,) = [t for t in triggers if t.kind == "event"]
        assert event_trigger.event_source == world.source
        assert event_trigger.event_type == EVENT_TYPE
        assert event_trigger.event_target_credential_id == world.credential_id
        assert event_trigger.event_config == {"folder": "today"}

    # It listens on the member's own login from the first tick on.
    await poll_tick()
    assert len(world.fake.calls) == 1
    assert world.fake.calls[0][0]["username"] == "anna"


async def test_re_saving_the_binding_after_hire_keeps_the_owned_trigger_enabled(
    app_session: AppSessionFactory,
) -> None:
    """Ruling F12, at hire level: the member saves the binding the hire wrote
    once more; their event trigger stays on and confirmed."""
    world = await _world(app_session)
    cid = await _capa(
        app_session,
        world.tenant,
        _template("helper", connections_any=[world.source], triggers=[_event(world.source)]),
    )
    r = await _hire(world.tenant, {"capaId": cid, "connection": _own(world), "confirmShare": True})
    assert r.status_code == 201, r.text
    agent_id = r.json()["agentId"]

    async with _http() as http:
        saved = await http.put(
            f"/api/v1/agents/{agent_id}/tool-access/{world.tool_key}",
            json={
                "mode": "fixed",
                "credentialId": str(world.credential_id),
                "confirmShare": True,
            },
            headers=_headers(world.tenant),
        )
    assert saved.status_code == 200, saved.text

    (trigger,) = await _triggers(app_session, world.tenant, uuid.UUID(agent_id))
    assert trigger.enabled is True and trigger.last_skip_reason is None
    async with app_session(world.tenant) as db:
        live = await db.get(m.Trigger, trigger.id)
        assert live is not None and await trigger_confirmed(db, live)


async def test_an_event_trigger_of_another_alternative_is_not_created(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    other = f"ev_{uuid.uuid4().hex[:10]}"
    await install_capa(app_session, world.tenant, other, f"{other}_tools")
    cid = await _capa(
        app_session,
        world.tenant,
        _template(
            "helper",
            connections_any=[world.source, other],
            triggers=[_event(world.source), _event(other), _cron()],
        ),
    )

    r = await _hire(world.tenant, {"capaId": cid, "connection": _own(world), "confirmShare": True})

    assert r.status_code == 201, r.text
    triggers = await _triggers(app_session, world.tenant, uuid.UUID(r.json()["agentId"]))
    assert sorted((t.kind, t.event_source) for t in triggers) == [
        ("cron", None),
        ("event", world.source),
    ]


async def test_an_optional_connection_is_bound_too(app_session: AppSessionFactory) -> None:
    world = await _world(app_session)
    extra = f"ev_{uuid.uuid4().hex[:10]}"
    extra_tools = f"{extra}_tools"
    await install_capa(app_session, world.tenant, extra, extra_tools)
    async with app_session(world.tenant) as db:
        org = await db.get(m.Organization, world.tenant)
        assert org is not None
        org.settings = {
            **org.settings,
            FRAME_KEY: {world.tool_key: dict(_TOOLS), extra_tools: dict(_TOOLS)},
        }
    second = await login(app_session, world.tenant, owner=world.member_id, username="bob")
    cid = await _capa(
        app_session,
        world.tenant,
        _template("helper", connections_any=[world.source], connections_optional=[extra]),
    )

    r = await _hire(
        world.tenant,
        {
            "capaId": cid,
            "connection": _own(world),
            "optionalConnections": [{"integration": extra, "credentialId": str(second)}],
            "confirmShare": True,
        },
    )

    assert r.status_code == 201, r.text
    async with app_session(world.tenant) as db:
        rows = (
            await db.execute(
                select(m.AgentToolAccess).where(
                    m.AgentToolAccess.agent_id == uuid.UUID(r.json()["agentId"])
                )
            )
        ).scalars()
        assert {(row.tool_key, row.credential_id) for row in rows} == {
            (world.tool_key, world.credential_id),
            (extra_tools, second),
        }


async def test_a_template_without_connections_hires_as_before(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    cid = await _capa(app_session, world.tenant, _template("helper", triggers=[_cron()]))

    r = await _hire(world.tenant, {"capaId": cid})

    assert r.status_code == 201, r.text
    agent_id = uuid.UUID(r.json()["agentId"])
    async with app_session(world.tenant) as db:
        found = await db.execute(
            select(m.AgentToolAccess.id).where(m.AgentToolAccess.agent_id == agent_id)
        )
        assert found.first() is None
    (cron,) = await _triggers(app_session, world.tenant, agent_id)
    assert cron.kind == "cron"
    assert cron.created_by_member_id == world.member_id and cron.acts_for_creator is True


# --- refusals --------------------------------------------------------------------------


async def _offered(
    app_session: AppSessionFactory, world: World, name: str = "helper", **agent: Any
) -> str:
    agent.setdefault("connections_any", [world.source])
    agent.setdefault("triggers", [_event(world.source)])
    return await _capa(app_session, world.tenant, _template(name, **agent))


async def test_a_colleagues_login_is_refused(app_session: AppSessionFactory) -> None:
    world = await _world(app_session)
    colleague = await _member(app_session, world.tenant, "bea")
    theirs = await login(app_session, world.tenant, owner=colleague, username="bea")
    cid = await _offered(app_session, world)

    r = await _hire(
        world.tenant, {"capaId": cid, "connection": _own(world, theirs), "confirmShare": True}
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "login_not_usable"}
    await _nothing_left(app_session, world.tenant)


async def test_an_organisation_login_is_refused(app_session: AppSessionFactory) -> None:
    world = await _world(app_session)
    shared = await login(app_session, world.tenant, owner=None, username="team")
    cid = await _offered(app_session, world)

    r = await _hire(
        world.tenant, {"capaId": cid, "connection": _own(world, shared), "confirmShare": True}
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "login_not_usable"}
    await _nothing_left(app_session, world.tenant)


async def test_a_login_of_another_type_or_none_at_all_is_refused(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    other_type = await login(
        app_session, world.tenant, owner=world.member_id, credential_type="other_login"
    )
    cid = await _offered(app_session, world)

    for connection in (
        _own(world, other_type),
        _own(world, uuid.uuid4()),
        {"integration": world.source},
    ):
        r = await _hire(
            world.tenant, {"capaId": cid, "connection": connection, "confirmShare": True}
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == {"error": "login_not_usable"}
    await _nothing_left(app_session, world.tenant)


async def test_an_alternative_outside_the_frame_is_refused(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session, in_frame=False)
    cid = await _offered(app_session, world)

    r = await _hire(world.tenant, {"capaId": cid, "connection": _own(world), "confirmShare": True})

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "connection_unavailable"}
    await _nothing_left(app_session, world.tenant)


async def test_an_alternative_that_is_not_installed_is_unavailable(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    cid = await _capa(app_session, world.tenant, _template("helper", connections_any=["nowhere"]))

    r = await _hire(
        world.tenant,
        {
            "capaId": cid,
            "connection": {"integration": "nowhere", "credentialId": str(world.credential_id)},
            "confirmShare": True,
        },
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "connection_unavailable"}


async def test_no_choice_for_a_template_with_alternatives_is_refused(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    cid = await _offered(app_session, world)

    r = await _hire(world.tenant, {"capaId": cid, "confirmShare": True})

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "connection_required"}
    await _nothing_left(app_session, world.tenant)


async def test_a_choice_the_template_does_not_offer_is_refused(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    bare = await _capa(app_session, world.tenant, _template("bare"))
    offered = await _offered(app_session, world)

    declared = await _hire(
        world.tenant, {"capaId": bare, "connection": _own(world), "confirmShare": True}
    )
    elsewhere = await _hire(
        world.tenant,
        {
            "capaId": offered,
            "connection": _own(world),
            "optionalConnections": [_own(world)],
            "confirmShare": True,
        },
    )

    assert declared.status_code == 422, declared.text
    assert declared.json()["detail"] == {"error": "connections_not_declared"}
    assert elsewhere.status_code == 422, elsewhere.text
    assert elsewhere.json()["detail"] == {"error": "connection_not_offered"}
    await _nothing_left(app_session, world.tenant)


async def test_binding_needs_confirm_share(app_session: AppSessionFactory) -> None:
    world = await _world(app_session)
    cid = await _offered(app_session, world)

    r = await _hire(world.tenant, {"capaId": cid, "connection": _own(world)})

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": "confirm_share_required"}
    await _nothing_left(app_session, world.tenant)


@pytest.mark.parametrize(
    ("status", "grant_type", "error"),
    [
        ("needs_reauth", "authorization_code", "login_needs_reauth"),
        ("active", "device_code", "login_not_usable"),
    ],
)
async def test_an_oauth_connection_must_be_usable(
    app_session: AppSessionFactory, status: str, grant_type: str, error: str
) -> None:
    world = await _world(app_session)
    source = f"oa_{uuid.uuid4().hex[:10]}"
    tool_key = f"{source}_tools"
    await install_oauth_capa(app_session, world.tenant, source, tool_key)
    async with app_session(world.tenant) as db:
        org = await db.get(m.Organization, world.tenant)
        assert org is not None
        org.settings = {**org.settings, FRAME_KEY: {tool_key: dict(_TOOLS)}}
    conn = await oauth_login(
        app_session, world.tenant, owner=world.member_id, status=status, grant_type=grant_type
    )
    cid = await _capa(app_session, world.tenant, _template("helper", connections_any=[source]))

    r = await _hire(
        world.tenant,
        {
            "capaId": cid,
            "connection": {"integration": source, "oauthConnectionId": str(conn)},
            "confirmShare": True,
        },
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {"error": error}
    await _nothing_left(app_session, world.tenant)


async def test_a_failing_trigger_leaves_nothing_and_does_not_use_up_the_cap(
    app_session: AppSessionFactory,
) -> None:
    """The hire is one transaction: a template trigger that is refused after
    the binding was written takes the agent, its binding, its department and
    the cap's slot back with it."""
    world = await _world(app_session, settings={MAX_AGENTS_KEY: 1})
    broken = await _offered(
        app_session,
        world,
        "broken",
        triggers=[_event(world.source), _cron(expression="not a cron")],
    )
    fine = await _offered(app_session, world, "fine")

    refused = await _hire(
        world.tenant, {"capaId": broken, "connection": _own(world), "confirmShare": True}
    )

    assert refused.status_code == 400, refused.text
    await _nothing_left(app_session, world.tenant)
    hired = await _hire(
        world.tenant, {"capaId": fine, "connection": _own(world), "confirmShare": True}
    )
    assert hired.status_code == 201, hired.text


# --- the templates list ----------------------------------------------------------------


async def test_templates_list_their_alternatives_with_availability(
    app_session: AppSessionFactory,
) -> None:
    world = await _world(app_session)
    off_frame = f"ev_{uuid.uuid4().hex[:10]}"
    await install_capa(app_session, world.tenant, off_frame, f"{off_frame}_tools")
    await _capa(
        app_session,
        world.tenant,
        _template(
            "helper",
            connections_any=[world.source, off_frame],
            connections_optional=["nowhere"],
        ),
    )

    async with _http() as http:
        listed = await http.get(
            "/api/v1/personal-department/templates", headers=_headers(world.tenant)
        )

    assert listed.status_code == 200, listed.text
    (template,) = listed.json()
    assert template["connectionsAny"] == [
        {
            "integration": world.source,
            "label": world.source,
            "kind": "credential",
            "provider": None,
            "credentialType": CREDENTIAL_TYPE,
            "available": True,
        },
        {
            "integration": off_frame,
            "label": off_frame,
            "kind": "credential",
            "provider": None,
            "credentialType": CREDENTIAL_TYPE,
            "available": False,
        },
    ]
    (nowhere,) = template["connectionsOptional"]
    assert nowhere["integration"] == "nowhere" and nowhere["available"] is False
