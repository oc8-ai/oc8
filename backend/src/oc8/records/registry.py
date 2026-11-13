"""Which record links oc8 issued, per tenant (spec §4.2). Read by the answer
check (`oc8.records.vetting`), written wherever `enrich` issued links."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.db.base import uuid7

ISSUED_LINK_TTL = dt.timedelta(days=30)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


async def remember_links(db: AsyncSession, *, tenant_id: uuid.UUID, urls: Sequence[str]) -> None:
    rows = [
        {"id": uuid7(), "tenant_id": tenant_id, "url": u, "host": host_of(u)}
        for u in dict.fromkeys(urls)
        if host_of(u)
    ]
    if not rows:
        return
    stmt = insert(m.RecordLink).values(rows)
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_record_link_tenant_url", set_={"issued_at": func.now()}
        )
    )


async def issued_links(db: AsyncSession, *, tenant_id: uuid.UUID, urls: Iterable[str]) -> set[str]:
    wanted = list(dict.fromkeys(urls))
    if not wanted:
        return set()
    since = dt.datetime.now(dt.UTC) - ISSUED_LINK_TTL
    rows = await db.execute(
        select(m.RecordLink.url).where(
            m.RecordLink.tenant_id == tenant_id,
            m.RecordLink.url.in_(wanted),
            m.RecordLink.issued_at >= since,
        )
    )
    return set(rows.scalars())


async def linked_hosts(db: AsyncSession, *, tenant_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(m.RecordLink.host).where(m.RecordLink.tenant_id == tenant_id).distinct()
    )
    return set(rows.scalars())
