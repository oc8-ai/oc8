"""Checking the links in a final answer (spec §5 item 3).

A link pointing at a connected system is only shown as a link if oc8 itself
issued it as an `oc8_url`. Anything else there is the model guessing, and a
guessed record link looks exactly like a real one -- so it is unlinked, its
text kept. Links to every other host are left exactly as written.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.records.registry import host_of, issued_links, linked_hosts

logger = logging.getLogger(__name__)

_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\((https?://[^\s)]+)\)")
_BARE = re.compile(r"(?<![(\[<`])\bhttps?://[^\s<>()\[\]`]+")
_TRAILING = ".,;:!?'\""


def _strip_trailing(url: str) -> tuple[str, str]:
    core = url.rstrip(_TRAILING)
    return core, url[len(core) :]


def vet_links(text: str, *, connected_hosts: set[str], issued: set[str]) -> str:
    def suspect(url: str) -> bool:
        return host_of(url) in connected_hosts and url not in issued

    def vet_segment(seg: str) -> str:
        seg = _MD_LINK.sub(lambda mt: mt.group(1) if suspect(mt.group(2)) else mt.group(0), seg)

        def bare(mt: re.Match[str]) -> str:
            url, tail = _strip_trailing(mt.group(0))
            return f"`{host_of(url)}`{tail}" if suspect(url) else mt.group(0)

        return _BARE.sub(bare, seg)

    out: list[str] = []
    pos = 0
    for code in _CODE.finditer(text):
        out.append(vet_segment(text[pos : code.start()]))
        out.append(code.group(0))
        pos = code.end()
    out.append(vet_segment(text[pos:]))
    return "".join(out)


def _urls(text: str) -> set[str]:
    found = {mt.group(2) for mt in _MD_LINK.finditer(text)}
    found |= {_strip_trailing(mt.group(0))[0] for mt in _BARE.finditer(text)}
    return found


async def vet_answer_links(db: AsyncSession, *, tenant_id: uuid.UUID, text: str) -> str:
    if not text or "http" not in text:
        return text
    try:
        # A savepoint, so a failed read cannot poison the executor's transaction.
        async with db.begin_nested():
            hosts = await linked_hosts(db, tenant_id=tenant_id) | await _connection_hosts(db)
            candidates = {u for u in _urls(text) if host_of(u) in hosts}
            if not candidates:
                return text
            issued = await issued_links(db, tenant_id=tenant_id, urls=candidates)
        return vet_links(text, connected_hosts=hosts, issued=issued)
    except Exception:
        # Conservative on failure (spec §7): unlink every link to a known
        # connected host rather than show an unchecked one.
        logger.warning("answer links could not be checked", exc_info=True)
        try:
            async with db.begin_nested():
                known = await linked_hosts(db, tenant_id=tenant_id)
            return vet_links(text, connected_hosts=known, issued=set())
        except Exception:
            return text


async def _connection_hosts(db: AsyncSession) -> set[str]:
    """Base-URL hosts of the tenant's live connections, from config only."""
    # Lazy: approvals -> records.links -> agent -> approvals is an import cycle.
    from oc8.connections.access import connected_connections
    from oc8.records.links import config_path, link_context

    hosts: set[str] = set()
    for conn in await connected_connections(db):
        ctx = link_context(conn)
        if ctx is not None and ctx.spec.base_url_path:
            base = config_path(ctx, list(ctx.spec.base_url_path))
            if base:
                hosts.add(host_of(base))
    hosts.discard("")
    return hosts
