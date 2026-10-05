"""The member's own `/copilot/*` API (design §7a, §9): profile, pause/resume,
responsibilities, follow-ups, notes and delegations. Every route serves only
the caller; a colleague's id is 404, never 403."""

from __future__ import annotations

import uuid

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.assistant import get_or_create_assistant
from oc8.auth import get_identity_provider
from oc8.authz.permissions import COPILOT_USE
from oc8.copilot.followups import schedule_followup
from oc8.copilot.responsibilities import open_responsibility
from oc8.main import create_app
from tests.conftest import AppSessionFactory
from tests.copilot.helpers import once_in_an_hour


def _headers(tenant: uuid.UUID, subject: str = "op") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role="org_admin")
    return {"Authorization": f"Bearer {token}"}


def _client(app: object) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")  # type: ignore[arg-type]


async def _member_id(app_session: AppSessionFactory, tenant: uuid.UUID, subject: str) -> uuid.UUID:
    async with app_session(tenant) as db:
        found = await db.scalar(
            select(m.OrgMember.id).where(
                m.OrgMember.tenant_id == tenant, m.OrgMember.subject == subject
            )
        )
        assert found is not None
        return found


async def _seed_responsibility(
    app_session: AppSessionFactory, tenant: uuid.UUID, member_id: uuid.UUID, subject: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """A responsibility plus its Copilot chat session, for `member_id`."""
    async with app_session(tenant) as db:
        assistant = await get_or_create_assistant(db, tenant_id=tenant)
        session = m.ChatSession(tenant_id=tenant, agent_id=assistant.id, member_id=member_id)
        db.add(session)
        await db.flush()
        r = await open_responsibility(
            db,
            tenant_id=tenant,
            member_id=member_id,
            chat_session_id=session.id,
            title="Watch the invoice",
            goal="Invoice 42 is paid",
            origin_channel=None,
            run_id=None,
            actor_agent_id=assistant.id,
            member_subject=subject,
        )
        return r.id, session.id


async def test_profile_get_creates_and_patch_validates() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        r = await c.get("/api/v1/copilot/profile", headers=_headers(tenant))
        assert r.status_code == 200, r.text
        assert r.json()["displayName"] == "Copilot"
        assert r.json()["status"] == "ready"

        too_long = await c.patch(
            "/api/v1/copilot/profile",
            json={"displayName": "x" * 41},
            headers=_headers(tenant),
        )
        assert too_long.status_code == 422, too_long.text

        ok = await c.patch(
            "/api/v1/copilot/profile",
            json={"displayName": "Alfred", "avatar": {"shape": "star", "color": "teal"}},
            headers=_headers(tenant),
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["displayName"] == "Alfred"
        assert ok.json()["avatar"] == {"shape": "star", "color": "teal"}


async def test_pause_resume() -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        paused = await c.post("/api/v1/copilot/pause", headers=_headers(tenant))
        assert paused.status_code == 200, paused.text
        assert paused.json()["status"] == "paused"
        assert paused.json()["pausedAt"] is not None

        resumed = await c.post("/api/v1/copilot/resume", headers=_headers(tenant))
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["status"] == "ready"
        assert resumed.json()["pausedAt"] is None


async def test_responsibilities_are_member_scoped(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await c.get("/api/v1/copilot/profile", headers=_headers(tenant, "op"))
        member_a = await _member_id(app_session, tenant, "op")
        rid, _ = await _seed_responsibility(app_session, tenant, member_a, "op")

        mine = await c.get("/api/v1/copilot/responsibilities", headers=_headers(tenant, "op"))
        assert [r["id"] for r in mine.json()] == [str(rid)]
        assert mine.json()[0]["title"] == "Watch the invoice"

        other = _headers(tenant, "op2")
        theirs = await c.get("/api/v1/copilot/responsibilities", headers=other)
        assert theirs.status_code == 200, theirs.text
        assert theirs.json() == []
        patched = await c.patch(
            f"/api/v1/copilot/responsibilities/{rid}", json={"state": "paused"}, headers=other
        )
        assert patched.status_code == 404, patched.text
        closed = await c.patch(
            f"/api/v1/copilot/responsibilities/{rid}", json={"state": "done"}, headers=other
        )
        assert closed.status_code == 404, closed.text

        # The owner can still pause it, then close it; closing twice is a 422.
        ok = await c.patch(
            f"/api/v1/copilot/responsibilities/{rid}",
            json={"state": "paused"},
            headers=_headers(tenant, "op"),
        )
        assert ok.status_code == 200 and ok.json()["state"] == "paused", ok.text
        done = await c.patch(
            f"/api/v1/copilot/responsibilities/{rid}",
            json={"state": "done", "reason": "paid"},
            headers=_headers(tenant, "op"),
        )
        assert done.status_code == 200 and done.json()["state"] == "done", done.text
        again = await c.patch(
            f"/api/v1/copilot/responsibilities/{rid}",
            json={"state": "paused"},
            headers=_headers(tenant, "op"),
        )
        assert again.status_code == 422, again.text


async def test_end_schedule_disables_trigger(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await c.get("/api/v1/copilot/profile", headers=_headers(tenant))
        member = await _member_id(app_session, tenant, "op")
        rid, _ = await _seed_responsibility(app_session, tenant, member, "op")
        async with app_session(tenant) as db:
            # A follow-up is only saved for a member whose ASSIGNED role grants
            # copilot:use; the token role alone is not enough.
            role = m.Role(tenant_id=tenant, name="copilot-users", kind="human")
            db.add(role)
            await db.flush()
            db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=COPILOT_USE))
            row = await db.get(m.OrgMember, member)
            assert row is not None
            row.role_id = role.id
            await db.flush()
            assistant = await get_or_create_assistant(db, tenant_id=tenant)
            trigger = await schedule_followup(
                db,
                tenant_id=tenant,
                member_id=member,
                responsibility_id=rid,
                assistant_id=assistant.id,
                spec=once_in_an_hour(),
                prompt="check",
                member_subject="op",
            )
            trigger_id = trigger.id

        listed = await c.get("/api/v1/copilot/followups", headers=_headers(tenant))
        assert listed.status_code == 200, listed.text
        assert listed.json()[0]["id"] == str(trigger_id)
        assert listed.json()[0]["responsibilityTitle"] == "Watch the invoice"
        assert listed.json()[0]["purpose"] == "check_in"

        foreign = await c.delete(
            f"/api/v1/copilot/followups/{trigger_id}", headers=_headers(tenant, "op2")
        )
        assert foreign.status_code == 404, foreign.text

        r = await c.delete(f"/api/v1/copilot/followups/{trigger_id}", headers=_headers(tenant))
        assert r.status_code == 204, r.text
        async with app_session(tenant) as db:
            row = await db.get(m.Trigger, trigger_id)
            assert row is not None
            assert row.enabled is False
        missing = await c.delete(
            f"/api/v1/copilot/followups/{uuid.uuid4()}", headers=_headers(tenant)
        )
        assert missing.status_code == 404, missing.text


async def test_notes_list_and_delete(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await c.get("/api/v1/copilot/profile", headers=_headers(tenant, "op"))
        await c.get("/api/v1/copilot/profile", headers=_headers(tenant, "op2"))
        member_a = await _member_id(app_session, tenant, "op")
        member_b = await _member_id(app_session, tenant, "op2")
        async with app_session(tenant) as db:
            assistant = await get_or_create_assistant(db, tenant_id=tenant)
            store = m.MemoryStore(tenant_id=tenant, tier="agent", owner_id=assistant.id)
            db.add(store)
            await db.flush()
            note_ids = {}
            for who, mid in (("a", member_a), ("b", member_b)):
                note = m.MemoryRecord(
                    tenant_id=tenant,
                    store_id=store.id,
                    content=f"note of {who}",
                    record_metadata={"member_id": str(mid)},
                    written_by=assistant.id,
                )
                db.add(note)
                await db.flush()
                note_ids[who] = note.id

        mine = await c.get("/api/v1/copilot/notes", headers=_headers(tenant, "op"))
        assert mine.status_code == 200, mine.text
        assert [n["content"] for n in mine.json()] == ["note of a"]

        foreign = await c.delete(
            f"/api/v1/copilot/notes/{note_ids['b']}", headers=_headers(tenant, "op")
        )
        assert foreign.status_code == 404, foreign.text
        ok = await c.delete(f"/api/v1/copilot/notes/{note_ids['a']}", headers=_headers(tenant))
        assert ok.status_code == 204, ok.text
        after = await c.get("/api/v1/copilot/notes", headers=_headers(tenant, "op"))
        assert after.json() == []
        theirs = await c.get("/api/v1/copilot/notes", headers=_headers(tenant, "op2"))
        assert [n["content"] for n in theirs.json()] == ["note of b"]


async def test_delegations_lists_runs_from_my_sessions_only(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await c.get("/api/v1/copilot/profile", headers=_headers(tenant, "op"))
        member_a = await _member_id(app_session, tenant, "op")
        _, session_id = await _seed_responsibility(app_session, tenant, member_a, "op")
        async with app_session(tenant) as db:
            assistant = await get_or_create_assistant(db, tenant_id=tenant)
            dept = m.Department(tenant_id=tenant, name="Finance", frame={})
            db.add(dept)
            await db.flush()
            worker = m.Agent(tenant_id=tenant, department_id=dept.id, name="Ledger")
            db.add(worker)
            await db.flush()
            delegated = m.AgentRun(
                tenant_id=tenant,
                agent_id=worker.id,
                state="done",
                source="chat",
                context={"chat_session_id": str(session_id)},
            )
            copilot_own = m.AgentRun(
                tenant_id=tenant,
                agent_id=assistant.id,
                state="done",
                source="chat",
                context={"chat_session_id": str(session_id)},
            )
            db.add_all([delegated, copilot_own])
            await db.flush()
            delegated_id = delegated.id

        mine = await c.get("/api/v1/copilot/delegations", headers=_headers(tenant, "op"))
        assert mine.status_code == 200, mine.text
        assert [r["id"] for r in mine.json()] == [str(delegated_id)]

        theirs = await c.get("/api/v1/copilot/delegations", headers=_headers(tenant, "op2"))
        assert theirs.status_code == 200, theirs.text
        assert theirs.json() == []

        bad = await c.get("/api/v1/copilot/delegations?limit=0", headers=_headers(tenant))
        assert bad.status_code == 422


async def test_generic_agent_memory_delete_refuses_copilot_personal_notes(
    app_session: AppSessionFactory,
) -> None:
    """Personal notes go only through /copilot/notes; an admin's generic
    memory delete must not reach them, nor copy a Copilot note into the audit."""
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app), _client(app) as c:
        await c.get("/api/v1/copilot/profile", headers=_headers(tenant, "op2"))
        member_b = await _member_id(app_session, tenant, "op2")
        async with app_session(tenant) as db:
            assistant = await get_or_create_assistant(db, tenant_id=tenant)
            store = m.MemoryStore(tenant_id=tenant, tier="agent", owner_id=assistant.id)
            db.add(store)
            await db.flush()
            personal = m.MemoryRecord(
                tenant_id=tenant, store_id=store.id, content="private of b",
                record_metadata={"member_id": str(member_b)}, written_by=assistant.id,
            )  # fmt: skip
            legacy = m.MemoryRecord(
                tenant_id=tenant, store_id=store.id, content="legacy untagged",
                record_metadata={}, written_by=assistant.id,
            )  # fmt: skip
            db.add_all([personal, legacy])
            await db.flush()
            assistant_id, personal_id, legacy_id = assistant.id, personal.id, legacy.id

        refused = await c.delete(
            f"/api/v1/agents/{assistant_id}/memory/{personal_id}", headers=_headers(tenant)
        )
        assert refused.status_code == 404, refused.text
        ok = await c.delete(
            f"/api/v1/agents/{assistant_id}/memory/{legacy_id}", headers=_headers(tenant)
        )
        assert ok.status_code == 204, ok.text
        async with app_session(tenant) as db:
            assert await db.get(m.MemoryRecord, personal_id) is not None
            events = (
                (
                    await db.execute(
                        select(m.AuditEvent).where(m.AuditEvent.action == "agent.memory.deleted")
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 1
            assert "legacy untagged" not in str(events[0].resource)
