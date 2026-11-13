"""Checking the links in a final answer (spec §5 item 3).

A link pointing at a connected system is only shown as a link if oc8 itself
issued it as an `oc8_url`. Anything else there is the model guessing, and a
guessed record link looks exactly like a real one -- so it is neutralised: a
Markdown link keeps only its text, any other occurrence becomes its bare host
in backticks. Links to every other host are left exactly as written.

The text is scanned as a whole, code spans included (a guessed URL in code is
still a guessed URL), and by host rather than by Markdown shape: whatever the
model wraps around a URL, the URL itself is found and judged. Every error path
fails closed -- it neutralises more, never shows an unchecked link.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.records.registry import host_of, issued_links, linked_hosts

logger = logging.getLogger(__name__)

# Greedy on purpose: parentheses and brackets are legal inside URLs, so the
# issued-URL lookup (not this pattern) decides where a URL really ends.
_URL = re.compile(r"(?i)(?:https?://|www\.)[^\s<>\"'`]+")
_TRAILING = ")]>.,;:!?'\""
_LINK_TAIL = re.compile(r"""[>]?(?:\s+(?:"[^"\n]*"|'[^'\n]*'))?\s*\)""")


def _host(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = f"http://{url}"
    try:
        return host_of(url.rstrip(_TRAILING))
    except ValueError:
        return ""


def _candidates(found: str) -> list[str]:
    """The match itself, then it with trailing characters taken off one at a
    time: any of them may be the URL oc8 issued."""
    out = [found]
    while found and found[-1] in _TRAILING:
        found = found[:-1]
        out.append(found)
    return out


def _core(found: str) -> str:
    """The URL part of a match: trailing punctuation and unbalanced closing
    brackets are the sentence's, not the URL's."""
    while found:
        last = found[-1]
        if (
            last in ".,;:!?'\">"
            or (last == ")" and found.count(")") > found.count("("))
            or (last == "]" and found.count("]") > found.count("["))
        ):
            found = found[:-1]
        else:
            break
    return found


def _link_text_start(text: str, close: int) -> int | None:
    """Index of the `[` matching the `]` at `close`, link text may nest []."""
    depth = 1
    for i in range(close - 1, -1, -1):
        if text[i] == "]":
            depth += 1
        elif text[i] == "[":
            depth -= 1
            if depth == 0:
                return i
    return None


def _neutralise(text: str, is_connected: Callable[[str], bool], issued: set[str]) -> str:
    spans: list[tuple[int, int, str]] = []
    for mt in _URL.finditer(text):
        found = mt.group(0)
        host = _host(found)
        if not host or not is_connected(host):
            continue
        if any(c in issued for c in _candidates(found)):
            continue
        start, end = mt.start(), mt.start() + len(_core(found))
        if text[start - 3 : start] == "](<":
            opener = start - 3
        elif text[start - 2 : start] == "](":
            opener = start - 2
        else:
            opener = -1
        tail = _LINK_TAIL.match(text, end) if opener >= 0 else None
        open_at = _link_text_start(text, opener) if tail is not None else None
        if tail is not None and open_at is not None:
            inner = _neutralise(text[open_at + 1 : opener], is_connected, issued)
            spans.append((open_at, tail.end(), inner))
        else:
            spans.append((start, end, f"`{host}`"))
    out: list[str] = []
    pos = 0
    for start, end, repl in sorted(spans, key=lambda sp: sp[0]):
        if start < pos:  # inside a Markdown link already replaced
            continue
        out.append(text[pos:start])
        out.append(repl)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def vet_links(text: str, *, connected_hosts: set[str], issued: set[str]) -> str:
    return _neutralise(text, lambda h: h in connected_hosts, issued)


def _has_url(text: str) -> bool:
    low = text.lower()
    return "http" in low or "www." in low


async def vet_answer_links(db: AsyncSession, *, tenant_id: uuid.UUID, text: str) -> str:
    if not text or not _has_url(text):
        return text
    hosts: set[str] | None = None
    try:
        # A savepoint, so a failed read cannot poison the executor's transaction.
        async with db.begin_nested():
            hosts = await linked_hosts(db, tenant_id=tenant_id) | await _connection_hosts(db)
            wanted = {
                c
                for mt in _URL.finditer(text)
                if _host(mt.group(0)) in hosts
                for c in _candidates(mt.group(0))
            }
            issued = await issued_links(db, tenant_id=tenant_id, urls=wanted) if wanted else set()
        return vet_links(text, connected_hosts=hosts, issued=issued)
    except Exception:
        # Fail closed (spec §7): without the issued set, every link to a
        # connected host is unissued; without the hosts, every link is suspect.
        logger.warning("answer links could not be checked", exc_info=True)
        if hosts is None:
            try:
                async with db.begin_nested():
                    hosts = await linked_hosts(db, tenant_id=tenant_id)
            except Exception:
                return _neutralise(text, lambda _h: True, set())
        return vet_links(text, connected_hosts=hosts, issued=set())


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
