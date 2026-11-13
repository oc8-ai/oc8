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

import html
import logging
import re
import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.records.registry import host_of, issued_links, linked_hosts

logger = logging.getLogger(__name__)

# Every scheme or `www.` is its own start; a URL runs to whitespace, one of
# <>"'` or the next start. Parentheses and brackets are legal inside URLs, so
# the issued-URL lookup (not this pattern) decides where a URL really ends.
# `www.` inside a URL or word is not a start of its own.
_START = r"https?://|(?<![A-Za-z0-9./@-])www\."
_URL = re.compile(rf"(?i)(?:{_START})(?:(?!{_START})[^\s<>\"'`])*")
# What may close a bare URL without being part of it: sentence punctuation and
# the Markdown around it (emphasis, strikethrough, table pipes), as GFM trims it.
_SENTENCE = ".,;:!?'\">*_~|"
_TRAILING = f"){_SENTENCE}]\\"
_LINK_TAIL = re.compile(r"""[>]?(?:\s+(?:"[^"\n]*"|'[^'\n]*'))?\s*\)""")
_MAX_URL = 2048
# What may follow an issued bare URL without being part of it: the sentence's
# own punctuation, the Markdown around it, and an escape of either.
_AROUND = set(_TRAILING) | set("[(")
# After an issued link's closing `)` the rest is the surrounding text -- unless
# it could carry a plain-text reading of the URL somewhere else.
_RETARGETS = set("/?#%")
# A Markdown parser deletes a backslash before ASCII punctuation.
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


def _host(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = f"http://{url}"
    return host_of(url)


def _candidates(found: str) -> list[str]:
    """Prefixes of the match that end at or next to punctuation: any of them
    may be the URL oc8 issued, the rest being the sentence around it."""
    found = found[:_MAX_URL]
    cuts = {len(found)}
    for i, ch in enumerate(found):
        if ch in _TRAILING and i > 0:
            cuts.update((i, i + 1))
    return [found[:c] for c in sorted(cuts, reverse=True)]


def _dot_after_backslash(rest: str) -> bool:
    # A browser reads `\` as `/`: `42\..\99` is the dot-segment `/../99`.
    return "\\" in rest and "." in rest


def _excused(found: str, issued: set[str]) -> bool:
    """An issued prefix excuses the match only when what follows it is
    punctuation -- `.../42./../99` is not the issued `.../42`."""
    return any(
        c in issued and set(rest := found[len(c) :]) <= _AROUND and not _dot_after_backslash(rest)
        for c in _candidates(found)
    )


def _link_excused(dest: str, rest: str, issued: set[str]) -> bool:
    """A link destination is excused only as exactly an issued URL; what
    follows its `)` is the surrounding text, `**`, `,` or a table pipe alike."""
    return dest in issued and not set(rest) & _RETARGETS and not _dot_after_backslash(rest)


def _destination_end(text: str, start: int, angled: bool) -> int:
    """End of the inline-link destination starting at `start`, read the way
    CommonMark does: `<...>` up to `>`, otherwise up to whitespace or the
    unbalanced `)`, backslash escapes skipped."""
    if angled:
        close = text.find(">", start)
        return close if close >= 0 else len(text)
    depth, i = 0, start
    while i < len(text) and not text[i].isspace():
        ch = text[i]
        if ch == "\\":
            i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        i += 1
    return min(i, len(text))


def _code(run: str) -> str:
    """The run as a code span, whatever backticks it holds."""
    longest = max((len(t) for t in re.findall(r"`+", run)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if run.startswith("`") or run.endswith("`") else ""
    return f"{fence}{pad}{run}{pad}{fence}"


def _readings(run: str) -> set[str]:
    """How a Markdown parser may read the run: escapes deleted (exactly, and
    every backslash) and entities decoded."""
    return {html.unescape(_ESCAPE.sub(r"\1", run)), html.unescape(run.replace("\\", ""))}


def _hidden_link(run: str, is_connected: Callable[[str], bool], issued: set[str]) -> bool:
    """Whether a parser's reading of `run` holds a URL the plain scan would
    neutralise. Only issued URLs written out verbatim in the run excuse one:
    the ambiguity must sit outside the URL itself."""
    verbatim = {c for c in issued if c in run}
    return any(
        _judge(reading, mt, is_connected, verbatim) is not None
        for reading in _readings(run) - {run}
        for mt in _URL.finditer(reading)
    )


def _ambiguous_runs(
    text: str,
    plain: list[tuple[int, int, str]],
    is_connected: Callable[[str], bool],
    issued: set[str],
) -> list[tuple[int, int, str]]:
    """Runs a Markdown parser reads differently from how they are written:
    backslash escapes and entities are decoded inside link destinations, so
    `https\\://h.example/x` is a link the plain scan cannot see. The run is
    judged after the plain scan's own rewrites; if a reading of what is left
    still holds a link to a connected (or undeterminable) host that oc8 did
    not issue, the whole run is shown as code."""
    spans: list[tuple[int, int, str]] = []
    for run in re.finditer(r"\S+", text):
        if _readings(run.group(0)) == {run.group(0)}:
            continue
        inside = [sp for sp in plain if sp[0] < run.end() and sp[1] > run.start()]
        lo = min([run.start(), *(sp[0] for sp in inside)])
        hi = max([run.end(), *(sp[1] for sp in inside)])
        left = _apply(text[lo:hi], [(s - lo, e - lo, r) for s, e, r in inside])
        if _hidden_link(left, is_connected, issued):
            spans.append((lo, hi, _code(left)))
    return spans


def _core(found: str) -> str:
    """The URL part of a match: trailing punctuation and unbalanced closing
    brackets are the sentence's, not the URL's."""
    while found:
        last = found[-1]
        if (
            last in _SENTENCE
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


def _judge(
    text: str, mt: re.Match[str], is_connected: Callable[[str], bool], issued: set[str]
) -> tuple[int, int, str] | None:
    """The rewrite for one URL match, or None when it may stay."""
    found = mt.group(0)
    start = mt.start()
    core = _core(found)
    host = _host(core)
    if not host:
        # A host that cannot be determined is not one we can call safe.
        return start, start + len(core), _code(core)
    angled = text[start - 3 : start] == "](<"
    opener = start - 3 if angled else start - 2 if text[start - 2 : start] == "](" else -1
    if opener >= 0:
        dest_end = _destination_end(text, start, angled)
        tail = _LINK_TAIL.match(text, dest_end)
        open_at = _link_text_start(text, opener) if tail is not None else None
        if tail is not None and open_at is not None:
            # A Markdown inline link: judge its destination up to the link's
            # own `)`; what follows belongs to the surrounding text.
            dest = text[start:dest_end]
            dest_host = _host(dest)
            suspect = not dest_host or is_connected(dest_host) or is_connected(host)
            if not suspect or _link_excused(dest, text[dest_end : mt.end()], issued):
                return None
            inner = _neutralise(text[open_at + 1 : opener], is_connected, issued)
            return open_at, tail.end(), inner
    if not is_connected(host) or _excused(found, issued):
        return None
    return start, start + len(core), f"`{host}`"


def _spans(
    text: str, is_connected: Callable[[str], bool], issued: set[str]
) -> list[tuple[int, int, str]]:
    plain = [sp for mt in _URL.finditer(text) if (sp := _judge(text, mt, is_connected, issued))]
    # Ambiguous runs first: on equal extents their rewrite is the one kept.
    return [*_ambiguous_runs(text, plain, is_connected, issued), *plain]


def _apply(text: str, spans: list[tuple[int, int, str]]) -> str:
    # Merge overlapping spans and rewrite once: a link whose text holds another
    # URL, or two URLs glued together, must not leave either one raw.
    merged: list[tuple[int, int, list[str]]] = []
    for start, end, repl in sorted(spans, key=lambda sp: (sp[0], -sp[1])):
        if merged and start < merged[-1][1]:
            prev = merged[-1]
            if end > prev[1]:  # a span inside another one adds nothing
                merged[-1] = (prev[0], end, [*prev[2], repl])
        else:
            merged.append((start, end, [repl]))
    out: list[str] = []
    pos = 0
    for start, end, repls in merged:
        out.append(text[pos:start])
        out.append(" ".join(dict.fromkeys(repls)))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _neutralise(text: str, is_connected: Callable[[str], bool], issued: set[str]) -> str:
    result = _apply(text, _spans(text, is_connected, issued))
    # CommonMark decodes entities in link destinations, so a scheme hidden as
    # `https&#58;//` is a link the scan above cannot see. If such a URL is in
    # the text, escape every `&` so nothing is decoded any more.
    if "&" in text:
        hidden = html.unescape(text)
        if hidden != text and any(
            core not in text
            for core in (_core(u.group(0)) for u in _URL.finditer(hidden))
            if (h := _host(core)) and is_connected(h) and not _excused(core, issued)
        ):
            result = result.replace("&", "&amp;")
    return result


def vet_links(text: str, *, connected_hosts: set[str], issued: set[str]) -> str:
    return _neutralise(text, lambda h: h in connected_hosts, issued)


def _all_candidates(text: str, hosts: set[str]) -> set[str]:
    wanted: set[str] = set()
    for seen in (text, *_readings(text)):
        for mt in _URL.finditer(seen):
            if _host(_core(mt.group(0))) in hosts:
                wanted.update(_candidates(mt.group(0)))
    return wanted


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
            wanted = _all_candidates(text, hosts)
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
