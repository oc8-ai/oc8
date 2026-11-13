from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update

from oc8 import models as m
from oc8.records.registry import (
    canonical,
    issued_links,
    linked_hosts,
    remember_links,
    verified_links,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_a_remembered_link_is_issued_for_its_tenant_only(
    app_session: AppSessionFactory,
) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    url = "https://odoo.example.com/odoo/sale.order/42"
    async with app_session(a) as db:
        await remember_links(db, tenant_id=a, urls=[url, url])
        assert await issued_links(db, tenant_id=a, urls=[url, "https://odoo.example.com/x"]) == {
            url
        }
        assert await linked_hosts(db, tenant_id=a) == {"odoo.example.com"}
    async with app_session(b) as db:
        assert await issued_links(db, tenant_id=b, urls=[url]) == set()


async def test_an_old_link_is_no_longer_issued(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    url = "https://jira.example.com/browse/ABC-1"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, urls=[url])
        await db.execute(
            update(m.RecordLink).values(issued_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=31))
        )
        assert await issued_links(db, tenant_id=t, urls=[url]) == set()


async def test_remembering_nothing_is_a_no_op(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, urls=[])
        assert await linked_hosts(db, tenant_id=t) == set()


async def test_a_link_is_stored_with_where_it_leads(app_session: AppSessionFactory) -> None:
    t = uuid.uuid4()
    url = "https://odoo.example.com/odoo/action-1?ids=[1,2]"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, urls=[url])
        stored = await db.scalar(select(m.RecordLink.canon).where(m.RecordLink.tenant_id == t))
    assert stored == canonical(url)
    assert stored == canonical("https://ODOO.example.com:443/odoo/action-1?ids=%5B1,2%5D")


async def test_remembering_again_fills_a_missing_canon(app_session: AppSessionFactory) -> None:
    """Rows written before `canon` existed get it the next time they are issued."""
    t = uuid.uuid4()
    url = "https://odoo.example.com/odoo/sale.order/42"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, urls=[url])
        await db.execute(update(m.RecordLink).values(canon=None))
        await remember_links(db, tenant_id=t, urls=[url])
        assert await db.scalar(select(m.RecordLink.canon)) == canonical(url)


async def test_verified_links_match_by_target_not_spelling(
    app_session: AppSessionFactory,
) -> None:
    t = uuid.uuid4()
    issued = "https://odoo.example.com/odoo/action-1?ids=[1,2]"
    reencoded = "https://odoo.example.com/odoo/action-1?ids=%5B1,2%5D"
    backslash = "https://odoo.example.com\\odoo\\action-1?ids=[1,2]"
    other_port = "https://odoo.example.com:8443/odoo/action-1?ids=[1,2]"
    async with app_session(t) as db:
        await remember_links(db, tenant_id=t, urls=[issued])
        got = await verified_links(
            db, tenant_id=t, urls=[reencoded, backslash, other_port, "not a url"]
        )
    assert got == {reencoded, backslash}


async def test_canonical_refuses_what_is_not_an_http_url() -> None:
    assert canonical("javascript:alert(1)") is None
    assert canonical("https://[x") is None
    assert canonical("ftp://h.example/x") is None
