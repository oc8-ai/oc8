from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.knowledge.connectors.base import BrowseEntry
from oc8.main import create_app
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import copilot_seat

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


async def _agent_and_kb(
    app_session: AppSessionFactory, tenant: uuid.UUID, kb_name: str = "Prices"
) -> tuple[uuid.UUID, uuid.UUID]:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        kb = m.KnowledgeBase(tenant_id=tenant, name=kb_name, embedding_model="x")
        db.add_all([agent, kb])
        await db.flush()
        return agent.id, kb.id


async def test_member_cannot_pin_what_they_cannot_see(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "emp")
        kb = m.KnowledgeBase(tenant_id=tenant, name="Board minutes", embedding_model="x")
        db.add(kb)
        await db.flush()
        kb_id, sid = kb.id, seat.session_id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            sent = str(kb_id).upper()
            r = await c.put(
                f"/api/v1/chat/sessions/{sid}/pins",
                json={"refs": [{"kind": "knowledge_base", "id": sent}]},
                headers=_h(tenant, "emp", "member"),
            )
            assert r.status_code == 422 and "Board" not in r.text  # no label leak
            # Named by the id the client sent, nothing more.
            assert r.json()["detail"]["refused"] == [{"kind": "knowledge_base", "id": sent}]


async def test_a_missing_and_a_forbidden_ref_are_refused_alike(
    app_session: AppSessionFactory,
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    _agent, theirs = await _agent_and_kb(app_session, other, "Their prices")
    agent_id, _kb = await _agent_and_kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
            ).json()["id"]
            replies = [
                await c.put(
                    f"/api/v1/chat/sessions/{sid}/pins",
                    json={"refs": [{"kind": "knowledge_base", "id": ref}]},
                    headers=h,
                )
                for ref in (str(theirs), str(uuid.uuid4()))
            ]
    assert [r.status_code for r in replies] == [422, 422]
    assert {r.json()["detail"]["message"] for r in replies} == {"not available to you"}
    assert "Their" not in replies[0].text


async def test_admin_pins_and_projects_round_trip(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, kb_id = await _agent_and_kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
            ).json()["id"]
            pins = await c.put(
                f"/api/v1/chat/sessions/{sid}/pins",
                json={"refs": [{"kind": "knowledge_base", "id": str(kb_id)}]},
                headers=h,
            )
            assert pins.status_code == 200 and pins.json()["pinnedRefs"][0]["label"] == "Prices"
            assert pins.json()["pinnedRefs"][0]["available"] is True
            detail = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=h)).json()
            assert detail["pinnedRefs"][0]["id"] == str(kb_id)
            project = (
                await c.post(
                    "/api/v1/chat/projects",
                    json={"name": "Q4", "instructions": "Be brief."},
                    headers=h,
                )
            ).json()
            moved = await c.put(
                f"/api/v1/chat/sessions/{sid}/project",
                json={"projectId": project["id"]},
                headers=h,
            )
            assert moved.json()["projectId"] == project["id"]
            await c.put(f"/api/v1/chat/sessions/{sid}/pinned", json={"pinned": True}, headers=h)
            listed = (await c.get("/api/v1/chat/sessions", headers=h)).json()
            assert listed[0]["pinned"] is True
            found = (await c.get("/api/v1/chat/context/search?q=Pri", headers=h)).json()
            assert {"kind": "knowledge_base", "id": str(kb_id)} in [
                {"kind": i["kind"], "id": i["id"]} for i in found
            ]
            other = await c.patch(
                f"/api/v1/chat/projects/{project['id']}",
                json={"name": "x"},
                headers=_h(tenant, "bob", "org_admin"),
            )
            assert other.status_code == 404


async def test_pinned_chats_list_first(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, _kb = await _agent_and_kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            ids = [
                (
                    await c.post(
                        "/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h
                    )
                ).json()["id"]
                for _ in range(3)
            ]
            before = [s["id"] for s in (await c.get("/api/v1/chat/sessions", headers=h)).json()]
            await c.put(
                f"/api/v1/chat/sessions/{before[-1]}/pinned", json={"pinned": True}, headers=h
            )
            after = [s["id"] for s in (await c.get("/api/v1/chat/sessions", headers=h)).json()]
    assert set(ids) == set(before)
    # The pinned chat moves to the top; the rest keep their order.
    assert after == [before[-1], *before[:-1]]


async def test_only_the_owner_writes_a_chats_pins_project_and_pinned_state(
    app_session: AppSessionFactory,
) -> None:
    """An org_admin may READ a colleague's Assistant chat (`copilot:manage`),
    but never rearrange it."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        seat = await copilot_seat(db, tenant, "emp")
        sid = seat.session_id
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            assert (await c.get(f"/api/v1/chat/sessions/{sid}", headers=h)).status_code == 200
            replies = [
                await c.put(f"/api/v1/chat/sessions/{sid}/pins", json={"refs": []}, headers=h),
                await c.put(
                    f"/api/v1/chat/sessions/{sid}/project", json={"projectId": None}, headers=h
                ),
                await c.put(
                    f"/api/v1/chat/sessions/{sid}/pinned", json={"pinned": True}, headers=h
                ),
            ]
    assert [r.status_code for r in replies] == [404, 404, 404]


async def test_a_chat_moves_only_into_the_callers_own_project(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    agent_id, _kb = await _agent_and_kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h, bob = _h(tenant, "admin", "org_admin"), _h(tenant, "bob", "org_admin")
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
            ).json()["id"]
            bobs = (
                await c.post("/api/v1/chat/projects", json={"name": "Bob's"}, headers=bob)
            ).json()
            into_bobs = await c.put(
                f"/api/v1/chat/sessions/{sid}/project", json={"projectId": bobs["id"]}, headers=h
            )
            into_nothing = await c.put(
                f"/api/v1/chat/sessions/{sid}/project",
                json={"projectId": str(uuid.uuid4())},
                headers=h,
            )
            assert [into_bobs.status_code, into_nothing.status_code] == [404, 404]
            # Another member's project is invisible, even to an org_admin.
            assert (await c.get("/api/v1/chat/projects", headers=h)).json() == []
            assert (
                await c.delete(f"/api/v1/chat/projects/{bobs['id']}", headers=h)
            ).status_code == 404


async def test_deleting_a_project_detaches_its_chats(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    agent_id, kb_id = await _agent_and_kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
            ).json()["id"]
            created = await c.post(
                "/api/v1/chat/projects",
                json={"name": "Q4", "pinnedRefs": [{"kind": "knowledge_base", "id": str(kb_id)}]},
                headers=h,
            )
            assert created.status_code == 201
            project = created.json()
            assert project["pinnedRefs"][0]["label"] == "Prices"
            renamed = await c.patch(
                f"/api/v1/chat/projects/{project['id']}",
                json={"name": "Q4 plan", "instructions": "Short."},
                headers=h,
            )
            # What a PATCH does not send is kept.
            assert renamed.json()["name"] == "Q4 plan"
            assert renamed.json()["instructions"] == "Short."
            assert [r["id"] for r in renamed.json()["pinnedRefs"]] == [str(kb_id)]
            unpinned = await c.patch(
                f"/api/v1/chat/projects/{project['id']}",
                json={"name": "Q4 plan", "pinnedRefs": []},
                headers=h,
            )
            assert unpinned.json()["pinnedRefs"] == []
            assert unpinned.json()["instructions"] == "Short."
            await c.put(
                f"/api/v1/chat/sessions/{sid}/project", json={"projectId": project["id"]}, headers=h
            )
            gone = await c.delete(f"/api/v1/chat/projects/{project['id']}", headers=h)
            assert gone.status_code == 204
            detail = (await c.get(f"/api/v1/chat/sessions/{sid}", headers=h)).json()
            assert detail["projectId"] is None
            assert (await c.get("/api/v1/chat/projects", headers=h)).json() == []


async def test_context_search_is_bounded(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _agent_and_kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            too_long = await c.get(f"/api/v1/chat/context/search?q={'a' * 101}", headers=h)
            too_many = await c.get(
                "/api/v1/chat/context/search?kinds=" + ",".join(["agent"] * 21), headers=h
            )
            unknown = await c.get("/api/v1/chat/context/search?kinds=nonsense", headers=h)
            only_agents = (await c.get("/api/v1/chat/context/search?kinds=agent", headers=h)).json()
            wildcard = (
                await c.get(
                    "/api/v1/chat/context/search?q=%25&kinds=knowledge_base,data_source",
                    headers=h,
                )
            ).json()
    assert [too_long.status_code, too_many.status_code, unknown.status_code] == [422, 422, 422]
    assert only_agents and {i["kind"] for i in only_agents} == {"agent"}
    # `%` is a character to look for, not a wildcard.
    assert wildcard == []


class _Drive:
    type_id = "fake_drive"

    def __init__(self, entries: list[BrowseEntry] | None = None, fail: bool = False) -> None:
        self.entries = entries or [BrowseEntry(id="f1", name="Q3.xlsx", is_folder=False, size=10)]
        self.fail = fail

    async def browse(self, config: dict[str, Any], path: str, auth: Any) -> list[BrowseEntry]:
        if self.fail:
            raise RuntimeError("token=sk-secret at https://internal")
        return self.entries


async def _source(
    app_session: AppSessionFactory, tenant: uuid.UUID, connector_type: str = "fake_drive"
) -> uuid.UUID:
    async with app_session(tenant) as db:
        src = m.DataSource(tenant_id=tenant, connector_type=connector_type, name="Site", config={})
        db.add(src)
        await db.flush()
        return src.id


async def test_browse_degrades_without_connector_support(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant, "website")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(
                f"/api/v1/chat/data-sources/{sid}/browse?path=/",
                headers=_h(tenant, "admin", "org_admin"),
            )
            assert r.status_code == 409 and "pin it as a whole" in r.text


async def test_browse_lists_reports_truncation_and_scrubs_failures(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    sid = await _source(app_session, tenant)
    many = [BrowseEntry(id=f"f{i}", name=f"{i}.txt", is_folder=False) for i in range(501)]
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            url = f"/api/v1/chat/data-sources/{sid}/browse"
            with patch("oc8.knowledge.browse.resolve_connector", return_value=_Drive()):
                ok = await c.get(url, headers=h)
                bad_path = await c.get(url + "?path=/a/../b", headers=h)
            with patch("oc8.knowledge.browse.resolve_connector", return_value=_Drive(many)):
                long = await c.get(url, headers=h)
            with patch("oc8.knowledge.browse.resolve_connector", return_value=_Drive(fail=True)):
                failed = await c.get(url, headers=h)
    assert ok.status_code == 200
    assert ok.json() == {
        "entries": [
            {"id": "f1", "name": "Q3.xlsx", "isFolder": False, "size": 10, "contentType": None}
        ],
        "truncated": False,
    }
    assert long.json()["truncated"] is True and len(long.json()["entries"]) == 500
    assert bad_path.status_code == 422
    assert failed.status_code == 502 and "sk-secret" not in failed.text
    assert "RuntimeError" in failed.text


async def test_browse_is_held_to_knowledge_view_and_the_tenant(
    app_session: AppSessionFactory,
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    mine = await _source(app_session, tenant)
    theirs = await _source(app_session, other)
    async with app_session(tenant) as db:
        await copilot_seat(db, tenant, "emp")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            with patch("oc8.knowledge.browse.resolve_connector", return_value=_Drive()):
                as_emp = await c.get(
                    f"/api/v1/chat/data-sources/{mine}/browse",
                    headers=_h(tenant, "emp", "member"),
                )
                cross = await c.get(
                    f"/api/v1/chat/data-sources/{theirs}/browse",
                    headers=_h(tenant, "admin", "org_admin"),
                )
    # `emp` lacks knowledge:view: refused at the door, whatever the id.
    assert as_emp.status_code == 403
    assert cross.status_code == 404


async def test_rearranging_a_chat_keeps_its_run_watcher(app_session: AppSessionFactory) -> None:
    """The PUT replies are the chat as `GET /chat/sessions/{id}` returns it: a
    client writing one into its cache must not lose the run it watches."""
    tenant = uuid.uuid4()
    agent_id, _kb = await _agent_and_kb(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
            ).json()["id"]
            async with app_session(tenant) as db:
                run = m.AgentRun(
                    tenant_id=tenant,
                    agent_id=agent_id,
                    source="chat",
                    state="running",
                    context={"chat_session_id": sid, "reply_to_message_id": "m1"},
                )
                db.add(run)
                await db.flush()
                run_id = str(run.id)
                await db.commit()
            replies = [
                await c.put(
                    f"/api/v1/chat/sessions/{sid}/pinned", json={"pinned": True}, headers=h
                ),
                await c.put(f"/api/v1/chat/sessions/{sid}/pins", json={"refs": []}, headers=h),
                await c.put(
                    f"/api/v1/chat/sessions/{sid}/project", json={"projectId": None}, headers=h
                ),
            ]
    for r in replies:
        assert r.status_code == 200
        assert [a["runId"] for a in r.json()["activeRuns"]] == [run_id]
        assert "pendingQuestion" in r.json()


async def test_project_changes_are_audited_without_their_instructions(
    app_session: AppSessionFactory,
) -> None:
    from sqlalchemy import select

    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            project = (
                await c.post(
                    "/api/v1/chat/projects",
                    json={"name": "Q4", "instructions": "SECRET PLAN"},
                    headers=h,
                )
            ).json()
            await c.patch(
                f"/api/v1/chat/projects/{project['id']}",
                json={"name": "Q4b", "instructions": "SECRET TOO"},
                headers=h,
            )
            await c.delete(f"/api/v1/chat/projects/{project['id']}", headers=h)
    async with app_session(tenant) as db:
        rows = (
            (
                await db.execute(
                    select(m.AuditEvent)
                    .where(m.AuditEvent.action.like("chat.project.%"))
                    .order_by(m.AuditEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    assert [(r.action, r.resource) for r in rows] == [
        ("chat.project.created", {"project_id": project["id"], "name": "Q4"}),
        ("chat.project.updated", {"project_id": project["id"], "name": "Q4b"}),
        ("chat.project.deleted", {"project_id": project["id"], "name": "Q4b"}),
    ]
    assert "SECRET" not in repr([r.resource for r in rows])


async def test_unpinning_keeps_a_stored_pin_that_became_unavailable(
    app_session: AppSessionFactory,
) -> None:
    """Removing one pin never fails because another can no longer be seen;
    a NEW ref the caller may not see is still refused."""
    tenant = uuid.uuid4()
    agent_id, kb_id = await _agent_and_kb(app_session, tenant)
    gone, never = str(uuid.uuid4()), str(uuid.uuid4())
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            h = _h(tenant, "admin", "org_admin")
            sid = (
                await c.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
            ).json()["id"]
            async with app_session(tenant) as db:
                session = await db.get(m.ChatSession, uuid.UUID(sid))
                assert session is not None
                session.pinned_refs = [
                    {"kind": "knowledge_base", "id": str(kb_id), "label": "Prices"},
                    {"kind": "knowledge_base", "id": gone, "label": "Board minutes"},
                ]
                await db.commit()
            removed = await c.put(
                f"/api/v1/chat/sessions/{sid}/pins",
                json={"refs": [{"kind": "knowledge_base", "id": gone}]},
                headers=h,
            )
            added = await c.put(
                f"/api/v1/chat/sessions/{sid}/pins",
                json={
                    "refs": [
                        {"kind": "knowledge_base", "id": gone},
                        {"kind": "knowledge_base", "id": never},
                    ]
                },
                headers=h,
            )
    assert removed.status_code == 200
    assert removed.json()["pinnedRefs"] == [
        {"kind": "knowledge_base", "id": gone, "label": "Board minutes", "available": False}
    ]
    assert added.status_code == 422
    assert added.json()["detail"]["refused"] == [{"kind": "knowledge_base", "id": never}]
