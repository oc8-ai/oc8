"""Which record links oc8 issued, per tenant (spec §4.2). Read by the answer
check (`oc8.records.vetting`), written wherever `enrich` issued links."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
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
    percent-decoded, then `normalise_host` -- `h%2Eexample`, `H.example.` and
    `h。example` are all `h.example`, `3232235786` is `192.168.1.10`. Pure and
    never raising: a host that cannot be determined is the empty string."""
    try:
        # Browsers read `\` as `/` in http(s) URLs: `h.example\@o.org` is h.example.
        host = unquote(urlsplit(url.replace("\\", "/")).hostname or "")
    except ValueError:
        return ""
    return normalise_host(host)


def normalise_host(host: str) -> str:
    """A host as a browser serialises it: NFKC-folded, ideographic dots as
    dots, lowercase, no trailing dot, IDNA-encoded; an IPv4 address in any
    WHATWG spelling (`3232235786`, `0xC0A8010A`, `0300.0250.1.10`) as a dotted
    quad, an IPv6 address compressed and without brackets. Never raises: the
    empty string if unsure."""
    try:
        host = unicodedata.normalize("NFKC", host).translate(_DOTS).lower()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if ":" in host:
            return ipaddress.IPv6Address(host).compressed
        host = host.rstrip(".")
        if (ipv4 := _ipv4(host)) is not None:
            return ipv4
        if not host.isascii():
            host = host.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""
    return host.rstrip(".")


_IPV4_DIGITS = {16: set("0123456789abcdef"), 10: set("0123456789"), 8: set("01234567")}


def _ipv4_part(part: str) -> int | None:
    """One dotted part as WHATWG reads it: `0x` hex, a leading `0` octal,
    decimal otherwise. None if it is no number at all."""
    base = 10
    if part[:2] == "0x":
        part, base = part[2:], 16
    elif len(part) > 1 and part.startswith("0"):
        part, base = part[1:], 8
    if not part:
        return 0 if base == 16 else None
    if not set(part) <= _IPV4_DIGITS[base]:
        return None
    return int(part, base)


def _ipv4(host: str) -> str | None:
    """The dotted quad a browser makes of an all-numeric host, or None for a
    name (or for an address a browser would refuse -- a name it is not)."""
    parts = host.split(".")
    if not host or len(parts) > 4 or "" in parts:
        return None
    numbers = [_ipv4_part(p) for p in parts]
    if any(n is None for n in numbers):
        return None
    *head, last = [n for n in numbers if n is not None]
    if any(n > 255 for n in head) or last >= 256 ** (4 - len(head)):
        return None
    value = last + sum(n << (8 * (3 - i)) for i, n in enumerate(head))
    return str(ipaddress.IPv4Address(value))


async def remember_links(db: AsyncSession, *, tenant_id: uuid.UUID, urls: Sequence[str]) -> None:
    rows = [
        {"id": uuid7(), "tenant_id": tenant_id, "url": u, "host": host_of(u), "canon": canonical(u)}
        for u in dict.fromkeys(urls)
        if host_of(u)
    ]
    if not rows:
        return
    stmt = insert(m.RecordLink).values(rows)
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_record_link_tenant_url",
            set_={"issued_at": func.now(), "canon": stmt.excluded.canon},
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


def canonical(url: str) -> str | None:
    """Where a URL leads, independent of how it is spelled: `[`, `%5B` and an
    upper-case host are the same target, `:8443` and `http:` are not. Stored
    with every issued link, so a re-encoded URL is matched by an index lookup.
    None for anything that is not an http(s) URL with a host."""
    try:
        # A browser reads `\` as `/` before the query starts, not after it.
        cut = min((i for i in (url.find("?"), url.find("#")) if i >= 0), default=len(url))
        parts = urlsplit(url[:cut].replace("\\", "/") + url[cut:])
        scheme = parts.scheme.lower()
        host = host_of(url)
        if scheme not in _DEFAULT_PORTS or not host:
            return None
        port = parts.port or _DEFAULT_PORTS[scheme]
        userinfo = [parts.username, parts.password]
    except ValueError:
        return None
    path = unquote(parts.path) or "/"
    target = [scheme, userinfo, host, port, path, unquote(parts.query), unquote(parts.fragment)]
    return json.dumps(target, ensure_ascii=False, separators=(",", ":"))


async def verified_links(
    db: AsyncSession, *, tenant_id: uuid.UUID, urls: Iterable[str]
) -> set[str]:
    """The given strings that lead exactly where an issued link does.

    `issued_links` matches strings; this matches targets, for callers that see
    a URL only after something else re-encoded it -- the browser turns an
    issued `?ids=[1,2]` into `?ids=%5B1,2%5D`. The caller gets back its own
    spelling of each match."""
    wanted = {u: c for u in dict.fromkeys(urls) if (c := canonical(u)) is not None}
    if not wanted:
        return set()
    since = dt.datetime.now(dt.UTC) - ISSUED_LINK_TTL
    rows = await db.execute(
        select(m.RecordLink.canon).where(
            m.RecordLink.tenant_id == tenant_id,
            m.RecordLink.canon.in_(set(wanted.values())),
            m.RecordLink.issued_at >= since,
        )
    )
    issued = set(rows.scalars())
    return {u for u, c in wanted.items() if c in issued}
