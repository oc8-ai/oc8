from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import update

from oc8 import models as m
from oc8.records.registry import issued_links, linked_hosts, remember_links
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
