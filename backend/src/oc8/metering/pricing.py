"""Versioned, DB-backed per-model pricing -> cost for a completion.

Prices are USD per 1M tokens. Because a cost value in micro-dollars needs
1e6 micros = 1 USD, USD-per-1M-tokens equals micros-per-token, so
cost_micros = round(tokens_in * price_in + tokens_out * price_out), except
that the part of tokens_in read from or written to a provider's prompt cache
is billed at the cache rates below instead of the full input price.

Nothing here is called from the completion hot path anymore (see the
2026-08-19 Cost Center design spec, Part A) -- this module is only ever
called lazily, when a Cost Center report is actually rendered. `active_price_rows`
does the one DB read per report; `price_as_of` and `cost_micros_from_price`
are pure functions over an in-memory list, so the substring-matching logic
stays trivially unit-testable without a database.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.models.pricing import ModelPrice


def normalize_model(s: str) -> str:
    """A model name or price pattern in the one form they are compared in:
    lowercase letters and digits only (`claude-haiku-4-5` -> `claudehaiku45`)."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


_norm = normalize_model


@dataclass(frozen=True)
class PriceRow:
    provider: str
    model_pattern: str
    price_in_usd_per_1m: float
    price_out_usd_per_1m: float
    effective_from: dt.datetime
    active: bool


async def active_price_rows(db: AsyncSession, *, as_of: dt.datetime) -> list[PriceRow]:
    """Every version effective on or before `as_of` -- the caller (a report
    spanning a date range) fetches once with as_of = the end of the range,
    then calls price_as_of() per usage bucket with that bucket's own date."""
    rows = (
        (await db.execute(select(ModelPrice).where(ModelPrice.effective_from <= as_of)))
        .scalars()
        .all()
    )
    return [
        PriceRow(
            r.provider,
            r.model_pattern,
            float(r.price_in_usd_per_1m),
            float(r.price_out_usd_per_1m),
            r.effective_from,
            r.active,
        )
        for r in rows
    ]


def price_as_of(
    rows: list[PriceRow], provider: str, model: str, at: dt.datetime
) -> tuple[float, float] | None:
    """The (price_in, price_out) in effect for `provider`/`model` at time `at`,
    or None if unknown (never guess a rate). Most-specific-first matching:
    among every active row whose pattern is a substring of the normalized
    model name and whose effective_from is <= at, the LONGEST pattern wins
    (e.g. "gpt4omini" beats "gpt4o", since "gpt4o" is itself a substring of
    "gpt4omini") -- ties broken by the most recent effective_from."""
    n = _norm(model)
    # The pattern is normalised like the name: the Models page stores what was
    # typed or picked (`claude-haiku-4-5-20251001`), and compared raw such a
    # pattern never matched, so its model silently cost nothing.
    candidates = [
        (pattern, r)
        for r in rows
        if r.active
        and r.provider == provider
        and r.effective_from <= at
        and (pattern := _norm(r.model_pattern))
        and pattern in n
    ]
    if not candidates:
        return None
    _, best = max(candidates, key=lambda c: (len(c[0]), c[1].effective_from))
    return (best.price_in_usd_per_1m, best.price_out_usd_per_1m)


#: Prompt-cache rates as multiples of the input price. Anthropic: a cache
#: read costs a tenth, writing the prefix into the cache a quarter more.
#: OpenAI caches on its own and bills no write; its read discount depends on
#: the model family (`_CACHE_READ_RATES`).
CACHE_READ_RATE = 0.1
CACHE_WRITE_RATE = 1.25

#: OpenAI's cached-input price as a share of the input price, by model family,
#: most specific first. Any other model (Claude, gpt-5 and later) reads at
#: `CACHE_READ_RATE`.
_CACHE_READ_RATES: tuple[tuple[re.Pattern[str], float], ...] = (
    (re.compile(r"^(gpt-4\.1|o3|o4)"), 0.25),
    (re.compile(r"^(gpt-4o|o1)"), 0.5),
)


def cache_read_rate(model: str) -> float:
    """What one cached prompt token costs, as a share of the input price."""
    base = model.lower().rsplit("/", 1)[-1]
    for pattern, rate in _CACHE_READ_RATES:
        if pattern.match(base):
            return rate
    return CACHE_READ_RATE


def cost_micros_from_price(
    price: tuple[float, float],
    tokens_in: int,
    tokens_out: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    model: str = "",
) -> int:
    """Cost of one call (or a sum of calls). `tokens_in` is the whole prompt;
    the cached parts inside it are billed at the cache rates of `model`."""
    p_in, p_out = price
    uncached = max(tokens_in - cache_read_tokens - cache_write_tokens, 0)
    read_rate = cache_read_rate(model) if model else CACHE_READ_RATE
    return round(
        uncached * p_in
        + cache_read_tokens * p_in * read_rate
        + cache_write_tokens * p_in * CACHE_WRITE_RATE
        + tokens_out * p_out
    )
