# backend/tests/api/test_notifications.py
"""Member inbox API: list, count, mark read, delivery settings, retention."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from oc8.notifications.service import prune_read_notifications
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

TENANT = uuid.UUID(str(ACME_TENANT_ID))
BASE = "/api/v1/notifications"


def _headers(subject: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=TENANT, subject=subject, role="member")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _member_id(
    client: AsyncClient, app_session: AppSessionFactory, subject: str
) -> uuid.UUID:
    # Any authenticated call upserts the member row.
    assert (await client.get(f"{BASE}/unread-count", headers=_headers(subject))).status_code == 200
    async with app_session(TENANT) as db:
        row = (
            await db.execute(select(m.OrgMember.id).where(m.OrgMember.subject == subject))
        ).scalar_one()
    return row


async def _add(
    app_session: AppSessionFactory,
    member_id: uuid.UUID,
    *,
    title: str = "t",
    age_min: int = 0,
    read_days_ago: int | None = None,
    kind: str = "run.failed",
) -> uuid.UUID:
    now = dt.datetime.now(dt.UTC)
    nid = uuid.uuid4()
    async with app_session(TENANT) as db:
        db.add(
            m.MemberNotification(
                id=nid,
                tenant_id=TENANT,
                member_id=member_id,
                kind=kind,
                subject_id=uuid.uuid4(),
                title={"en": title, "de": title},
                body={"en": "b", "de": "b"},
                link="/x",
                created_at=now - dt.timedelta(minutes=age_min),
                read_at=None if read_days_ago is None else now - dt.timedelta(days=read_days_ago),
            )
        )
    return nid


async def test_list_is_own_newest_first_and_pages(
    client: AsyncClient, app_session: AppSessionFactory
) -> None:
    me = await _member_id(client, app_session, "inbox-a")
    other = await _member_id(client, app_session, "inbox-b")
    for i, age in enumerate((30, 20, 10)):
        await _add(app_session, me, title=f"n{i}", age_min=age)
    await _add(app_session, other, title="foreign")

    r = await client.get(f"{BASE}?limit=2", headers=_headers("inbox-a"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["title"]["en"] for i in body["items"]] == ["n2", "n1"]
    assert body["nextCursor"]
    first = body["items"][0]
    assert set(first) >= {"id", "kind", "subjectId", "title", "body", "link", "createdAt", "read"}
    assert first["read"] is False

    r2 = await client.get(
        f"{BASE}?limit=2&cursor={body['nextCursor']}", headers=_headers("inbox-a")
    )
    assert [i["title"]["en"] for i in r2.json()["items"]] == ["n0"]
    assert r2.json()["nextCursor"] is None


async def test_unread_filter_kind_and_count(
    client: AsyncClient, app_session: AppSessionFactory
) -> None:
    me = await _member_id(client, app_session, "inbox-c")
    await _add(app_session, me, title="u1")
    await _add(app_session, me, title="u2", kind="approval.decided")
    await _add(app_session, me, title="r1", read_days_ago=1)

    h = _headers("inbox-c")
    assert (await client.get(f"{BASE}/unread-count", headers=h)).json() == {"count": 2}
    r = await client.get(f"{BASE}?unread=true", headers=h)
    assert {i["title"]["en"] for i in r.json()["items"]} == {"u1", "u2"}
    r = await client.get(f"{BASE}?kind=approval.decided", headers=h)
    assert [i["title"]["en"] for i in r.json()["items"]] == ["u2"]
    assert (await client.get(f"{BASE}?limit=51", headers=h)).status_code == 422


async def test_mark_read_own_and_foreign_404(
    client: AsyncClient, app_session: AppSessionFactory
) -> None:
    me = await _member_id(client, app_session, "inbox-d")
    other = await _member_id(client, app_session, "inbox-e")
    mine = await _add(app_session, me)
    theirs = await _add(app_session, other)
    h = _headers("inbox-d")

    assert (await client.post(f"{BASE}/{theirs}/read", headers=h)).status_code == 404
    assert (await client.post(f"{BASE}/{uuid.uuid4()}/read", headers=h)).status_code == 404
    assert (await client.post(f"{BASE}/{mine}/read", headers=h)).status_code == 204
    assert (await client.get(f"{BASE}/unread-count", headers=h)).json() == {"count": 0}


async def test_read_all_marks_only_own(client: AsyncClient, app_session: AppSessionFactory) -> None:
    me = await _member_id(client, app_session, "inbox-f")
    other = await _member_id(client, app_session, "inbox-g")
    await _add(app_session, me)
    await _add(app_session, other)

    r = await client.post(f"{BASE}/read-all", headers=_headers("inbox-f"))
    assert r.status_code == 204
    assert (await client.get(f"{BASE}/unread-count", headers=_headers("inbox-f"))).json() == {
        "count": 0
    }
    assert (await client.get(f"{BASE}/unread-count", headers=_headers("inbox-g"))).json() == {
        "count": 1
    }


async def test_settings_defaults_and_round_trip(client: AsyncClient) -> None:
    h = _headers("inbox-h")
    r = await client.get(f"{BASE}/settings", headers=h)
    assert r.status_code == 200
    assert {s["kind"]: (s["pushEnabled"], s["messengerEnabled"]) for s in r.json()} == {
        "board.available": (True, True),
        "run.failed": (True, True),
        "approval.decided": (True, True),
    }
    put = [{"kind": "run.failed", "pushEnabled": False, "messengerEnabled": True}]
    assert (await client.put(f"{BASE}/settings", json=put, headers=h)).status_code == 200
    put[0]["messengerEnabled"] = False
    r = await client.put(f"{BASE}/settings", json=put, headers=h)
    got = {s["kind"]: (s["pushEnabled"], s["messengerEnabled"]) for s in r.json()}
    assert got["run.failed"] == (False, False)
    assert got["board.available"] == (True, True)
    r = await client.get(f"{BASE}/settings", headers=h)
    assert {s["kind"]: s["pushEnabled"] for s in r.json()}["run.failed"] is False


async def test_prune_removes_only_old_read_rows(app_session: AppSessionFactory) -> None:
    # Its own tenant: the sweep discovers tenants through `organization`.
    tenant = uuid.uuid4()
    member_id = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.Organization(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
        await db.flush()
        db.add(
            m.OrgMember(id=member_id, tenant_id=tenant, subject="prune", subject_uuid=uuid.uuid4())
        )
        await db.flush()
        now = dt.datetime.now(dt.UTC)
        ids = {}
        for name, read_ago, age in (("old", 91, 0), ("recent", 10, 0), ("unread", None, 200)):
            ids[name] = uuid.uuid4()
            db.add(
                m.MemberNotification(
                    id=ids[name],
                    tenant_id=tenant,
                    member_id=member_id,
                    kind="run.failed",
                    subject_id=uuid.uuid4(),
                    title={"en": "t", "de": "t"},
                    body={"en": "b", "de": "b"},
                    link="/x",
                    created_at=now - dt.timedelta(days=age),
                    read_at=None if read_ago is None else now - dt.timedelta(days=read_ago),
                )
            )

    assert await prune_read_notifications() >= 1
    async with app_session(tenant) as db:
        left = set((await db.execute(select(m.MemberNotification.id))).scalars())
    assert left == {ids["recent"], ids["unread"]}
