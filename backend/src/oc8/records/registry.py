"""Which record links oc8 issued, per tenant (spec §4.2). Read by the answer
check (`oc8.records.vetting`), written wherever `enrich` issued links."""

from __future__ import annotations

import datetime as dt
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from urllib.parse import unquote, urlsplit

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.db.base import uuid7

ISSUED_LINK_TTL = dt.timedelta(days=30)


_DOTS = str.maketrans({"\u3002": ".", "\uff0e": ".", "\uff61": "."})


def host_of(url: str) -> str:
    """The host a URL points at, spelled the way a browser resolves it:
    percent-decoded, NFKC-folded, ideographic dots as dots, IDNA-encoded,
    lowercase, no trailing dot -- `h%2Eexample`, `H.example.` and `h。example`
    are all `h.example`. Pure and never raising: a host that cannot be
    determined is the empty string."""
    try:
        # Browsers read `\` as `/` in http(s) URLs: `h.example\@o.org` is h.example.
        host = unquote(urlsplit(url.replace("\\", "/")).hostname or "")
        host = unicodedata.normalize("NFKC", host).translate(_DOTS).lower()
        if not host.isascii():
            host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    return host.rstrip(".")


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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _canonical(url: str) -> tuple[object, ...] | None:
    """Where a URL leads, independent of how it is spelled: `[`, `%5B` and an
    upper-case host are the same target, `:8443` and `http:` are not. None for
    anything that is not an http(s) URL with a host."""
    try:
        # A browser reads `\` as `/` before the query starts, not after it.
        cut = min((i for i in (url.find("?"), url.find("#")) if i >= 0), default=len(url))
        parts = urlsplit(url[:cut].replace("\\", "/") + url[cut:])
        scheme = parts.scheme.lower()
        host = host_of(url)
        if scheme not in _DEFAULT_PORTS or not host:
            return None
        port = parts.port or _DEFAULT_PORTS[scheme]
        userinfo = (parts.username, parts.password)
    except ValueError:
        return None
    path = unquote(parts.path) or "/"
    return scheme, userinfo, host, port, path, unquote(parts.query), unquote(parts.fragment)


async def verified_links(
    db: AsyncSession, *, tenant_id: uuid.UUID, urls: Iterable[str]
) -> set[str]:
    """The submitted strings that lead exactly where an issued link does.

    `issued_links` matches strings; this matches targets, for callers that see
    a URL only after something else re-encoded it -- the browser turns an
    issued `?ids=[1,2]` into `?ids=%5B1,2%5D`. Issued links are therefore read
    by host and compared canonically, and the caller gets back its own
    spelling of each match."""
    wanted = {u: c for u in dict.fromkeys(urls) if (c := _canonical(u)) is not None}
    hosts = {c[2] for c in wanted.values()}
    if not hosts:
        return set()
    since = dt.datetime.now(dt.UTC) - ISSUED_LINK_TTL
    rows = await db.execute(
        select(m.RecordLink.url).where(
            m.RecordLink.tenant_id == tenant_id,
            m.RecordLink.host.in_(hosts),
            m.RecordLink.issued_at >= since,
        )
    )
    issued = {c for u in rows.scalars() if (c := _canonical(u)) is not None}
    return {u for u, c in wanted.items() if c in issued}
