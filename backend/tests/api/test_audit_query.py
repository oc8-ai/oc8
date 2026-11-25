from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from oc8.audit.chain import append_event
from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def _seed(app_session: AppSessionFactory, tenant: uuid.UUID, n: int) -> None:
    async with app_session(tenant) as s:
        for i in range(n):
            await append_event(
                s,
                tenant_id=tenant,
                actor_type="operator" if i % 2 else "agent",
                actor_id=None,
                category="approval" if i % 2 else "tool",
                action=f"a.{i}",
                decision="allow" if i % 2 else "deny",
            )


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_lists_newest_first_with_hashes(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant, 3)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            r = await c.get("/api/v1/audit", headers={"Authorization": f"Bearer {_token(tenant)}"})
    assert r.status_code == 200
    body = r.json()
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == sorted(seqs, reverse=True)
    assert all(len(e["hash"]) == 64 for e in body["events"])


async def test_filters_by_category_and_decision(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant, 4)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            r = await c.get(
                "/api/v1/audit?category=approval&decision=allow",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert r.status_code == 200
    events = r.json()["events"]
    assert events
    assert {e["category"] for e in events} == {"approval"}
    assert {e["decision"] for e in events} == {"allow"}


async def test_keyset_pages_do_not_overlap(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant, 5)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            h = {"Authorization": f"Bearer {_token(tenant)}"}
            p1 = (await c.get("/api/v1/audit?limit=2", headers=h)).json()
            p2 = (
                await c.get(f"/api/v1/audit?limit=2&before_seq={p1['nextBeforeSeq']}", headers=h)
            ).json()
    a = {e["seq"] for e in p1["events"]}
    b = {e["seq"] for e in p2["events"]}
    assert len(a) == 2 and len(b) == 2
    assert a.isdisjoint(b)


async def test_member_is_forbidden(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    await _seed(app_session, tenant, 1)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            r = await c.get(
                "/api/v1/audit",
                headers={"Authorization": f"Bearer {_token(tenant, role='member')}"},
            )
    assert r.status_code == 403


async def test_tenant_isolation(app_session: AppSessionFactory) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed(app_session, a, 2)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            r = await c.get("/api/v1/audit", headers={"Authorization": f"Bearer {_token(b)}"})
    assert r.status_code == 200
    assert r.json()["events"] == []


async def test_to_date_filter_includes_the_whole_selected_day(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """I3: the UI sends a date-only `to` (an <input type="date">). "2026-07-21"
    parses to 2026-07-21T00:00:00, so a `ts <= to` bound silently dropped
    everything that happened on the selected day."""
    import sqlalchemy as sa

    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        ev = await append_event(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="tool",
            action="midday",
        )
    seq = ev.seq  # assigned when the session commits

    # ts is not part of the hashed payload, so backdating it leaves the chain
    # intact. UPDATE needs the owning role -- oc8_app is REVOKEd (migration 0001).
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET ts = '2026-05-14 12:30:00+00' WHERE seq = :s"),
            {"s": seq},
        )
    engine.dispose()

    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            r = await c.get(
                "/api/v1/audit?from=2026-05-14&to=2026-05-14",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert r.status_code == 200
    assert [e["seq"] for e in r.json()["events"]] == [seq]


async def test_export_agrees_with_list_on_the_to_date_bound(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """I3: the export must not be short by up to a day relative to GET /audit."""
    import sqlalchemy as sa

    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        ev = await append_event(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="tool",
            action="midday",
        )
    seq = ev.seq  # assigned when the session commits

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET ts = '2026-05-14 23:15:00+00' WHERE seq = :s"),
            {"s": seq},
        )
    engine.dispose()

    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            listed = await c.get("/api/v1/audit?from=2026-05-14&to=2026-05-14", headers=headers)
            exported = await c.get(
                "/api/v1/audit/export?format=jsonl&from=2026-05-14&to=2026-05-14",
                headers=headers,
            )
    import json as _json

    assert [e["seq"] for e in listed.json()["events"]] == [seq]
    assert [_json.loads(ln)["seq"] for ln in exported.text.splitlines() if ln.strip()] == [seq]


async def test_explicit_to_timestamp_is_still_respected_exactly(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """The end-of-day expansion applies to date-ONLY values; a full timestamp
    must keep meaning exactly what it says."""
    import sqlalchemy as sa

    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        ev = await append_event(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="tool",
            action="late",
        )
    seq = ev.seq  # assigned when the session commits

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET ts = '2026-05-14 18:00:00+00' WHERE seq = :s"),
            {"s": seq},
        )
    engine.dispose()

    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            r = await c.get(
                "/api/v1/audit?from=2026-05-14&to=2026-05-14T09:00:00",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
    assert r.status_code == 200
    assert [e["seq"] for e in r.json()["events"]] == []


# ------------------------------------------------------- run and chat filters


async def _seed_tool_events(
    app_session: AppSessionFactory, tenant: uuid.UUID
) -> tuple[str, str, str]:
    """Two runs of one chat and one trigger run, each with a call and a result,
    plus an unrelated event. Returns (chat run, other chat run, chat session)."""
    chat_run, other_run, trigger_run = (str(uuid.uuid4()) for _ in range(3))
    chat = str(uuid.uuid4())
    async with app_session(tenant) as s:
        for run_id, session in ((chat_run, chat), (other_run, chat), (trigger_run, None)):
            link = {"run_id": run_id, **({"chat_session_id": session} if session else {})}
            for action in ("tool.call:search", "tool.result:search"):
                await append_event(
                    s,
                    tenant_id=tenant,
                    actor_type="agent",
                    actor_id=None,
                    category="tool_action",
                    action=action,
                    resource={"tool": "search", **link},
                    decision="allow",
                )
        await append_event(
            s, tenant_id=tenant, actor_type="system", actor_id=None, category="tool", action="x"
        )
    return chat_run, other_run, chat


async def test_filter_by_run_and_chat(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    chat_run, other_run, chat = await _seed_tool_events(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            h = {"Authorization": f"Bearer {_token(tenant)}"}
            by_run = (await c.get(f"/api/v1/audit?run_id={chat_run}", headers=h)).json()
            by_chat = (await c.get(f"/api/v1/audit?chat_session_id={chat}", headers=h)).json()
            both = (
                await c.get(f"/api/v1/audit?run_id={other_run}&chat_session_id={chat}", headers=h)
            ).json()
            none = (await c.get(f"/api/v1/audit?run_id={uuid.uuid4()}", headers=h)).json()
    assert {e["action"] for e in by_run["events"]} == {"tool.call:search", "tool.result:search"}
    assert {e["resource"]["run_id"] for e in by_run["events"]} == {chat_run}
    assert len(by_chat["events"]) == 4
    assert {e["resource"]["run_id"] for e in by_chat["events"]} == {chat_run, other_run}
    assert {e["resource"]["run_id"] for e in both["events"]} == {other_run}
    assert none["events"] == []


async def test_export_honours_the_run_filter(app_session: AppSessionFactory) -> None:
    import json as _json

    tenant = uuid.uuid4()
    chat_run, _other, chat = await _seed_tool_events(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            h = {"Authorization": f"Bearer {_token(tenant)}"}
            jsonl = await c.get(f"/api/v1/audit/export?format=jsonl&run_id={chat_run}", headers=h)
            csv_ = await c.get(f"/api/v1/audit/export?format=csv&chat_session_id={chat}", headers=h)
    assert jsonl.status_code == 200
    rows = [_json.loads(ln) for ln in jsonl.text.splitlines() if ln.strip()]
    assert len(rows) == 2
    assert {r["resource"]["run_id"] for r in rows} == {chat_run}
    assert csv_.status_code == 200
    assert len(csv_.text.strip().splitlines()) == 1 + 4


async def test_filter_requires_audit_view(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    chat_run, _other, _chat = await _seed_tool_events(app_session, tenant)
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            h = {"Authorization": f"Bearer {_token(tenant, role='member')}"}
            listed = await c.get(f"/api/v1/audit?run_id={chat_run}", headers=h)
            exported = await c.get(f"/api/v1/audit/export?run_id={chat_run}", headers=h)
    assert listed.status_code == 403
    assert exported.status_code == 403


async def test_malformed_run_id_is_422(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with await _client(app) as c:
            h = {"Authorization": f"Bearer {_token(tenant)}"}
            run = await c.get("/api/v1/audit?run_id=not-a-uuid", headers=h)
            chat = await c.get("/api/v1/audit?chat_session_id=1' OR '1'='1", headers=h)
            export = await c.get("/api/v1/audit/export?run_id=nope", headers=h)
    assert run.status_code == 422
    assert chat.status_code == 422
    assert export.status_code == 422
