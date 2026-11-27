# backend/tests/agents/test_cache_flow.py
from __future__ import annotations

import uuid

import pytest

from oc8 import models as m
from oc8.agent import cache_flow
from oc8.caching.department_cache import get_cached
from oc8.modelrouter.types import CompletionResult, ModelParams, NeutralMessage, Usage

pytestmark = pytest.mark.asyncio


def _dept() -> m.Department:
    return m.Department(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Sales",
        goal="",
        frame={},
        presentation={},
        prompt_caching_enabled=True,
    )


async def test_store_if_matching_accepts_an_alias_provider_against_its_canonical_result(
    redis_url: str,
) -> None:
    """ModelConfig.provider is written raw by the seeder ("Claude", "GPT",
    "Ollama"), but a real adapter always stamps its CANONICAL name on the
    result ("anthropic", "openai", "ollama"). Comparing those two strings
    verbatim made the mismatch guard fire on every call from such a config,
    permanently and silently turning caching off for it -- the same
    "looks wired, does nothing" failure the guard itself exists to prevent,
    reintroduced on a different axis. An alias must be recognised as a
    match, not a mismatch."""
    dept = _dept()
    messages = [NeutralMessage(role="user", content="classify this ticket")]
    key, cached = await cache_flow.lookup(
        department=dept,
        tenant_id=dept.tenant_id,
        department_id=dept.id,
        provider="Claude",  # alias, uncanonicalized -- exactly what the seeder writes
        model="claude-sonnet-4",
        base_url=None,
        messages=messages,
        tools=[],
        params=ModelParams(),
        contains_restricted=False,
    )
    assert cached is None
    assert key is not None

    result = CompletionResult(
        text="ok",
        tool_calls=[],
        usage=Usage(tokens_in=10, tokens_out=5),
        stop_reason="stop",
        provider="anthropic",
        model="claude-sonnet-4",
    )
    await cache_flow.store_if_matching(key, result, provider="Claude", model="claude-sonnet-4")

    assert await get_cached(key) is not None, (
        "an alias provider string must be recognised as matching its canonical "
        "result, or caching silently never stores anything for that config"
    )


async def test_store_if_matching_still_rejects_a_genuine_provider_downgrade(
    redis_url: str,
) -> None:
    """The canonicalization fix must not weaken the guard it was added
    alongside: a REAL downgrade (requested anthropic, answered by ollama) is
    still two different canonical providers and must still be refused."""
    dept = _dept()
    messages = [NeutralMessage(role="user", content="classify this ticket")]
    key, _cached = await cache_flow.lookup(
        department=dept,
        tenant_id=dept.tenant_id,
        department_id=dept.id,
        provider="anthropic",
        model="claude-sonnet-4",
        base_url=None,
        messages=messages,
        tools=[],
        params=ModelParams(),
        contains_restricted=False,
    )
    assert key is not None

    downgraded = CompletionResult(
        text="local answer",
        tool_calls=[],
        usage=Usage(tokens_in=10, tokens_out=5),
        stop_reason="stop",
        provider="ollama",
        model="some-local-model",
    )
    await cache_flow.store_if_matching(
        key, downgraded, provider="anthropic", model="claude-sonnet-4"
    )

    assert await get_cached(key) is None, "a genuine cross-provider downgrade must still be refused"


async def test_store_if_matching_refuses_a_tool_less_bare_json_result(redis_url: str) -> None:
    """A weak model's failed tool-call attempt -- raw JSON that didn't match
    any declared tool, so tool_calls stayed empty -- must not be cached.
    Live-observed 2026-08-25: department prompt caching turned one such bad
    local-model answer into 5 identical "did nothing" runs over ~9 hours,
    because the exact-match key (built from the fixed placeholder task text
    the empty Run-now field falls back to) was identical every time."""
    dept = _dept()
    messages = [NeutralMessage(role="user", content="do something")]
    key, _cached = await cache_flow.lookup(
        department=dept,
        tenant_id=dept.tenant_id,
        department_id=dept.id,
        provider="ollama",
        model="llama3.2:3b",
        base_url=None,
        messages=messages,
        tools=[],
        params=ModelParams(),
        contains_restricted=False,
    )
    assert key is not None

    failed_attempt = CompletionResult(
        text='{"model": "ir.model_configuration", "values": {"name": "Lennart"}}',
        tool_calls=[],
        usage=Usage(tokens_in=10, tokens_out=5),
        stop_reason="stop",
        provider="ollama",
        model="llama3.2:3b",
    )
    await cache_flow.store_if_matching(key, failed_attempt, provider="ollama", model="llama3.2:3b")

    assert await get_cached(key) is None, (
        "a tool-less bare-JSON result must never be cached -- it would replay "
        "verbatim on every identical retry for the cache's TTL"
    )


async def test_store_if_matching_still_caches_a_genuine_tool_less_prose_answer(
    redis_url: str,
) -> None:
    """The bare-JSON guard must not overreach: a real final answer with no
    tool call (the normal shape of the LAST step of a run) is still cached
    exactly as before."""
    dept = _dept()
    messages = [NeutralMessage(role="user", content="say hello")]
    key, _cached = await cache_flow.lookup(
        department=dept,
        tenant_id=dept.tenant_id,
        department_id=dept.id,
        provider="ollama",
        model="llama3.2:3b",
        base_url=None,
        messages=messages,
        tools=[],
        params=ModelParams(),
        contains_restricted=False,
    )
    assert key is not None

    prose = CompletionResult(
        text="Hello! How can I help you today?",
        tool_calls=[],
        usage=Usage(tokens_in=10, tokens_out=5),
        stop_reason="stop",
        provider="ollama",
        model="llama3.2:3b",
    )
    await cache_flow.store_if_matching(key, prose, provider="ollama", model="llama3.2:3b")

    assert await get_cached(key) is not None, (
        "a genuine tool-less prose answer must still be cached normally"
    )


async def test_store_if_matching_refuses_a_result_cut_off_by_the_budget(
    redis_url: str,
) -> None:
    """The run retried this step with a doubled budget and got a usable answer;
    storing the cut-off one under the same key would replay the failure verbatim
    on every identical retry for the TTL -- the same class of failure as the
    bare-JSON guard below."""
    dept = _dept()
    messages = [NeutralMessage(role="user", content="do something")]
    key, _cached = await cache_flow.lookup(
        department=dept,
        tenant_id=dept.tenant_id,
        department_id=dept.id,
        provider="ollama",
        model="llama3.2:3b",
        base_url=None,
        messages=messages,
        tools=[],
        params=ModelParams(),
        contains_restricted=False,
    )
    assert key is not None

    cut_off = CompletionResult(
        text="",
        tool_calls=[],
        usage=Usage(tokens_in=10, tokens_out=400),
        stop_reason="length",
        provider="ollama",
        model="llama3.2:3b",
    )
    await cache_flow.store_if_matching(key, cut_off, provider="ollama", model="llama3.2:3b")
    assert await get_cached(key) is None

    half_written = CompletionResult(
        text="Sure, here is the record:",
        tool_calls=[],
        usage=Usage(tokens_in=10, tokens_out=400),
        stop_reason="length",
        provider="ollama",
        model="llama3.2:3b",
        truncated_tool_calls=True,
    )
    await cache_flow.store_if_matching(key, half_written, provider="ollama", model="llama3.2:3b")
    assert await get_cached(key) is None, "a tool call cut off mid-JSON must never be cached either"


async def test_invalidate_deletes_a_stored_entry(redis_url: str) -> None:
    """The counterpart to store_if_matching for a failure it cannot see
    coming: it runs before any tool call in the completion it caches has
    executed, so a request that leads to a real tool-execution error (e.g.
    Odoo rejecting an invalid field name) gets stored anyway. Once the
    caller (engine.py's step loop / internal_agent.py's /tool endpoint)
    learns that, it calls invalidate() so the bad first guess does not
    replay verbatim on every identical trigger for the rest of the TTL."""
    dept = _dept()
    messages = [NeutralMessage(role="user", content="search open tickets")]
    key, _cached = await cache_flow.lookup(
        department=dept,
        tenant_id=dept.tenant_id,
        department_id=dept.id,
        provider="ollama",
        model="llama3.2:3b",
        base_url=None,
        messages=messages,
        tools=[],
        params=ModelParams(),
        contains_restricted=False,
    )
    assert key is not None

    result = CompletionResult(
        text="",
        tool_calls=[],
        usage=Usage(tokens_in=10, tokens_out=5),
        stop_reason="tool_calls",
        provider="ollama",
        model="llama3.2:3b",
    )
    await cache_flow.store_if_matching(key, result, provider="ollama", model="llama3.2:3b")
    assert await get_cached(key) is not None

    await cache_flow.invalidate(key)

    assert await get_cached(key) is None, "invalidate() must delete the entry it is given"


async def test_invalidate_is_a_noop_on_none(redis_url: str) -> None:
    """Mirrors lookup()/store_if_matching()'s own not-cache-eligible shape:
    a cache-ineligible request's key is None, and invalidate() must accept
    that silently rather than every caller having to guard the call."""
    await cache_flow.invalidate(None)  # must not raise
