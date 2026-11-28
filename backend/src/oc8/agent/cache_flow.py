"""Cache-lookup/store orchestration shared by every runtime that resolves a
model call through department prompt caching -- the in-process agent engine
(agent/engine.py) and the isolated container runtime (api/v1/internal_agent.py)
-- so the two cannot drift on when the cache applies, how a Redis failure is
handled, or what gets stored under which key. department_cache.py stays the
pure key/get/store primitives this wraps; this module owns the fail-open
error handling and the correctness invariant on store (see store_if_matching).
"""

from __future__ import annotations

import json
import logging
import uuid

from oc8 import models as m
from oc8.caching.department_cache import cache_key, get_cached, is_enabled
from oc8.caching.department_cache import delete as _delete
from oc8.caching.department_cache import store as _store
from oc8.modelrouter.registry import canonical_provider
from oc8.modelrouter.sampling import truncated_without_answer
from oc8.modelrouter.types import (
    CompletionResult,
    ModelParams,
    NeutralMessage,
    NeutralTool,
)

logger = logging.getLogger(__name__)


def _is_bare_json(text: str) -> bool:
    """True when `text` is (after stripping) nothing but a single JSON
    value -- the shape a weak model produces when it meant to make a tool
    call but the result didn't match any declared tool name (engine.py's
    `_salvage_tool_calls` recovers the cases that DO match; this is what's
    left over when even that fails). A genuine prose final answer never
    parses this way, so it's a narrow, low-false-positive signal for "this
    response is a failed tool-call attempt, not a real answer" -- see its
    use in store_if_matching below. Module-level (not engine.py-local)
    because both callers of store_if_matching (the in-process engine and
    the isolated container runtime, see this module's own docstring) share
    this same store-time decision."""
    try:
        json.loads(text.strip())
    except (ValueError, TypeError):
        return False
    return True


async def lookup(
    *,
    department: m.Department | None,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID | None,
    provider: str,
    model: str,
    base_url: str | None,
    messages: list[NeutralMessage],
    tools: list[NeutralTool],
    params: ModelParams,
    contains_restricted: bool,
) -> tuple[str | None, CompletionResult | None]:
    """Returns (key, cached_result). key is None when the request is not
    cache-eligible at all (disabled, restricted, or no department) -- callers
    pass that same key straight to store_if_matching(), which is a no-op on
    None, so both call sites share one shape regardless of eligibility."""
    enabled = (
        department is not None
        and department_id is not None
        and is_enabled(department)
        and not contains_restricted
    )
    if not enabled or department_id is None:
        return None, None
    key = cache_key(
        tenant_id=tenant_id,
        department_id=department_id,
        provider=provider,
        model=model,
        base_url=base_url,
        messages=messages,
        tools=tools,
        params=params,
    )
    try:
        cached = await get_cached(key)
    except Exception:
        logger.warning("department cache lookup failed; proceeding without cache", exc_info=True)
        cached = None
    return key, cached


async def store_if_matching(
    key: str | None, result: CompletionResult, *, provider: str, model: str
) -> None:
    """Store on a miss -- but ONLY when the result that actually answered is
    the same provider/model the key was computed from. A ModelRouter
    availability downgrade or a complete_with_fallback fallback answers with a
    DIFFERENT provider/model than what was requested; caching that answer
    under the originally-requested key would let a later request that
    legitimately has the requested model available get served the
    downgrade's/fallback's stale answer instead, for up to the TTL.

    Also refuses a result with no tool calls whose text is bare JSON (see
    _is_bare_json): a weak model's failed tool-call attempt, caught neither
    as a real tool call nor recovered by the caller's own salvage step.
    Caching that exact failure would replay it verbatim on every identical
    retry for up to the TTL -- live-observed 2026-08-25, department prompt
    caching turned a single bad local-model response into 5 identical
    "did nothing" runs over ~9 hours before this guard existed. A genuine
    tool-less prose answer is unaffected -- only bare JSON is refused.

    Compared through canonical_provider(), not verbatim: `result.provider` is
    always canonical (every adapter stamps its own canonical id), but the
    caller's `provider` can be an uncanonicalized alias -- ModelConfig.provider
    is written raw by the seeder ("Claude", "GPT", "Ollama") and by the
    presentation-fallback path. Comparing raw strings made this guard fire on
    every request from such a config, permanently and silently disabling
    caching for it -- the exact "looks wired, does nothing" failure class this
    fix round exists to remove, reintroduced on a different axis. Falls back
    to the raw string when canonical_provider() doesn't recognise it, so an
    unknown-alias caller still gets the mismatch protection rather than none."""
    if key is None:
        return
    if truncated_without_answer(result):
        logger.info(
            "department cache: not storing a result cut off by the answer budget "
            "(a replay of the same cut-off would repeat the failure for the TTL)"
        )
        return
    if not result.tool_calls and _is_bare_json(result.text):
        logger.info(
            "department cache: not storing a tool-less bare-JSON result "
            "(likely a failed tool-call attempt, not a real answer)"
        )
        return
    if (canonical_provider(result.provider) or result.provider) != (
        canonical_provider(provider) or provider
    ) or result.model != model:
        logger.info(
            "department cache: not storing a %s/%s result under the %s/%s key "
            "(fallback or availability downgrade)",
            result.provider,
            result.model,
            provider,
            model,
        )
        return
    try:
        await _store(key, result)
    except Exception:
        logger.warning("department cache store failed; response was still returned", exc_info=True)


async def invalidate(key: str | None) -> None:
    """Delete a just-stored cache entry once its own tool call(s) turn out to
    have failed for real (see the ERROR: prefix check in engine.py/
    internal_agent.py's step loop, right after the tool-execution loop for a
    step). store_if_matching() cannot make this call itself: it runs before
    any tool call in the completion it is about to cache has executed, so
    whether the request it is caching leads to a real failure is only known
    afterward. Left uninvalidated, a first-try wrong guess (e.g. an invalid
    Odoo field name) would replay verbatim on every identical trigger for the
    rest of the TTL instead of giving the model a fresh chance -- exactly the
    department-cache failure class store_if_matching's bare-JSON guard
    already exists to prevent for a different symptom. A no-op on None,
    matching lookup()/store_if_matching()'s own not-cache-eligible shape."""
    if key is None:
        return
    try:
        await _delete(key)
    except Exception:
        logger.warning("department cache invalidate failed; entry may replay", exc_info=True)
