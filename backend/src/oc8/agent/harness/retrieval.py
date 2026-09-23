"""A4 tool retrieval: core-set selection and BM25 ranking (spec §4 A4).

When the full catalog exceeds 30 tools and the model can change its tool list
mid-run, offer a core subset inline and defer the rest behind ``find_tools``.
Ranking is pure BM25 over name/description/notes/connection — no embeddings,
no new dependency, sync and DB-free.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from oc8.agent.tool_routing import SEPARATOR
from oc8.modelrouter import NeutralTool

logger = logging.getLogger(__name__)

_INLINE_THRESHOLD = 30
_WARN_ALL_INLINE = 60
_BM25_K1 = 1.5
_BM25_B = 0.75
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class ToolCard:
    name: str
    description: str
    connection: str
    notes: str = ""


_FIND_TOOLS_CARD = ToolCard(
    name="find_tools",
    description=(
        "Search tools that are not in your current list. "
        "Returns up to 10 matches; they are available on the next step."
    ),
    connection="oc8",
    notes="",
)


def select_inline(
    catalog: list[ToolCard],
    *,
    control_names: frozenset[str],
    skill_names: frozenset[str],
    mission: str,
    skill_texts: list[str],
    procedure_texts: list[str],
    pinned: list[str],
    tool_list_may_change: bool,
) -> tuple[list[ToolCard], list[ToolCard]]:
    """Return (inline, deferred). Deferred is empty when not deferring."""
    if len(catalog) <= _INLINE_THRESHOLD or not tool_list_may_change:
        if not tool_list_may_change and len(catalog) > _WARN_ALL_INLINE:
            logger.warning(
                "Offering %d tools inline because tool_list_may_change is false "
                "(threshold for warning is %d).",
                len(catalog),
                _WARN_ALL_INLINE,
            )
        return list(catalog), []

    by_name = {card.name: card for card in catalog}
    haystacks = (mission, *skill_texts, *procedure_texts)

    core_names: set[str] = set()
    for name in control_names | skill_names:
        if name in by_name:
            core_names.add(name)
    for card in catalog:
        if any(card.name in text for text in haystacks):
            core_names.add(card.name)
    for name in pinned:
        if name in by_name:
            core_names.add(name)

    # Control tools first (catalog order), then the rest of the core (catalog order).
    seen: set[str] = set()
    inline: list[ToolCard] = []
    for card in catalog:
        if card.name in control_names and card.name in core_names and card.name not in seen:
            inline.append(card)
            seen.add(card.name)
    for card in catalog:
        if card.name in core_names and card.name not in seen:
            inline.append(card)
            seen.add(card.name)

    inline.append(_FIND_TOOLS_CARD)
    deferred = [card for card in catalog if card.name not in core_names]
    return inline, deferred


def cards_from_offered(
    tools: Sequence[NeutralTool],
    *,
    control_names: frozenset[str],
    skill_names: frozenset[str],
    mcp_connection: str | None,
    tool_notes: Mapping[str, Any] | None = None,
) -> list[ToolCard]:
    """Build ToolCards from the full ``offered_tools`` list (ruling 4)."""
    notes_map = tool_notes if isinstance(tool_notes, Mapping) else {}
    cards: list[ToolCard] = []
    for tool in tools:
        if tool.name in control_names or tool.name in skill_names:
            connection = "oc8"
            notes = ""
        else:
            connection = mcp_connection or "oc8"
            raw_note = notes_map.get(tool.name)
            if isinstance(raw_note, str):
                notes = raw_note
            elif SEPARATOR in tool.name:
                bare = tool.name.partition(SEPARATOR)[2]
                bare_note = notes_map.get(bare)
                notes = bare_note if isinstance(bare_note, str) else ""
            else:
                notes = ""
        cards.append(
            ToolCard(
                name=tool.name,
                description=tool.description or "",
                connection=connection,
                notes=notes,
            )
        )
    return cards


def completion_tools_from_inline(
    offered: Sequence[NeutralTool],
    inline: Sequence[ToolCard],
    *,
    find_tools: NeutralTool,
) -> list[NeutralTool]:
    """Filter original NeutralTools by inline order; append find_tools when selected."""
    by_name = {tool.name: tool for tool in offered}
    out: list[NeutralTool] = []
    for card in inline:
        if card.name == find_tools.name:
            out.append(find_tools)
        elif card.name in by_name:
            out.append(by_name[card.name])
    return out


def select_completion_tools(
    offered: Sequence[NeutralTool],
    *,
    control_names: frozenset[str],
    skill_names: frozenset[str],
    mission: str,
    skill_texts: list[str],
    pinned: list[str],
    tool_list_may_change: bool,
    mcp_connection: str | None,
    tool_notes: Mapping[str, Any] | None,
    find_tools: NeutralTool,
    procedure_texts: list[str] | None = None,
) -> tuple[list[NeutralTool], list[dict[str, Any]]]:
    """A4 step: catalog + select_inline → completion tools and deferred catalog.

    ``tool_catalog`` is the deferred subset only (empty when not deferring) so
    ``find_tools`` ranks tools that are not already inline.
    """
    catalog = cards_from_offered(
        offered,
        control_names=control_names,
        skill_names=skill_names,
        mcp_connection=mcp_connection,
        tool_notes=tool_notes,
    )
    inline, deferred = select_inline(
        catalog,
        control_names=control_names,
        skill_names=skill_names,
        mission=mission,
        skill_texts=skill_texts,
        procedure_texts=list(procedure_texts or []),
        pinned=pinned,
        tool_list_may_change=tool_list_may_change,
    )
    completion = completion_tools_from_inline(offered, inline, find_tools=find_tools)
    # find_tools ranks the deferred set only (ruling 3 / empty query → first
    # 10 deferred by name). When not deferring, deferred is empty.
    return completion, [asdict(card) for card in deferred]


def rank_tools(
    catalog: list[ToolCard],
    query: str,
    *,
    connection: str | None,
    limit: int = 10,
    require_match: bool = False,
) -> list[ToolCard]:
    """BM25-rank cards; optionally filter by connection first.

    When ``require_match`` is true (find_tools), drop zero-score cards so a
    specific query does not pad results. Default keeps Task 1 top-N-even-at-0.
    """
    if connection is not None:
        catalog = [c for c in catalog if c.connection == connection]
    if not catalog:
        return []

    if not query.strip():
        return sorted(catalog, key=lambda c: c.name)[:limit]

    docs = [_corpus(c) for c in catalog]
    tokenized = [_tokenize(doc) for doc in docs]
    scores = _bm25_scores(tokenized, _tokenize(query))
    ranked = sorted(
        zip(scores, catalog, strict=True),
        key=lambda pair: (-pair[0], pair[1].name),
    )
    if require_match:
        return [card for score, card in ranked if score > 0][:limit]
    return [card for _score, card in ranked][:limit]


def _corpus(card: ToolCard) -> str:
    return f"{card.name}\n{card.description}\n{card.notes}\n{card.connection}"


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _bm25_scores(docs: list[list[str]], query_tokens: list[str]) -> list[float]:
    n = len(docs)
    if n == 0 or not query_tokens:
        return [0.0] * n

    avgdl = sum(len(d) for d in docs) / n
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))

    scores = [0.0] * n
    for qi in query_tokens:
        n_qi = df.get(qi, 0)
        idf = math.log((n - n_qi + 0.5) / (n_qi + 0.5) + 1.0)
        for i, doc in enumerate(docs):
            freq = doc.count(qi)
            if freq == 0:
                continue
            denom = freq + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * len(doc) / avgdl)
            scores[i] += idf * (freq * (_BM25_K1 + 1.0)) / denom
    return scores
