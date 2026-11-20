# backend/tests/agents/test_department_cache_wiring.py
from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result
from oc8.modelrouter.types import NeutralTool
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _CountingRouter:
    """Fake ModelRouter -- every call returns the same fixed answer, and
    self.calls lets a test assert exactly how many real model calls
    happened, which is the whole point of a cache.

    It echoes the REQUESTED provider/model back on the result, as a real
    adapter does when the requested model is the one that answered. That is
    load-bearing, not cosmetic: `cache_flow.store_if_matching` refuses to cache
    a result whose provider/model differs from the key's, so a fake answering
    as some other model would (correctly) never populate the cache and every
    hit assertion below would fail for the wrong reason. `_DowngradingRouter`
    is the fake for the opposite case."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        return CompletionResult(
            text="Triage: billing question, routed to finance.",
            tool_calls=[],
            usage=Usage(tokens_in=120, tokens_out=40),
            stop_reason="stop",
            provider=req.provider,
            model=req.model,
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


class _DowngradingRouter:
    """Answers with a DIFFERENT provider/model than the one asked for -- what
    `ModelRouter.resolve()` really does when no key is available for the
    requested cloud provider, and what `complete_with_fallback` does when the
    primary is down and a fallback config answers instead."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        return CompletionResult(
            text="Downgraded local answer.",
            tool_calls=[],
            usage=Usage(tokens_in=10, tokens_out=5),
            stop_reason="stop",
            provider="ollama",
            model="some-other-local-model",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


class _ToolCallThenDoneRouter:
    """First turn of any run asks for `search_records`; the turn right after
    a tool result comes back answers with a final prose "Done." -- the same
    two-step shape as Lennart's real Odoo runs (guess a field, get an error,
    answer from the corrected retry). Discriminates on the last message's
    role rather than a call counter so it behaves the same regardless of
    which of two separate runs (cached vs. not) is asking."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        last = req.messages[-1] if req.messages else None
        if last is not None and last.role == "tool":
            return CompletionResult(
                text="Done.",
                tool_calls=[],
                usage=Usage(tokens_in=10, tokens_out=5),
                stop_reason="stop",
                provider=req.provider,
                model=req.model,
            )
        return CompletionResult(
            text="",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    name="search_records",
                    arguments={"model": "helpdesk.ticket", "order": "sla_date asc"},
                )
            ],
            usage=Usage(tokens_in=10, tokens_out=5),
            stop_reason="tool_calls",
            provider=req.provider,
            model=req.model,
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


class _RejectingToolset:
    """Mirrors Odoo rejecting an unknown field -- every call fails the same
    way, exactly like the live-observed odoo_mcp bug this guards against."""

    tools = [
        NeutralTool(
            name="search_records", description="", parameters={"type": "object", "properties": {}}
        )
    ]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        raise RuntimeError("Invalid field 'sla_date' in request")


async def test_a_completion_whose_tool_call_fails_is_not_replayed_from_cache(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The department cache stores a completion BEFORE any tool call it
    requested has executed -- store_if_matching has no way to know yet that
    the request it is caching is about to fail. Without invalidation, a
    first-try wrong tool-call guess (e.g. an invalid Odoo field name) would
    replay verbatim on every identical trigger for the rest of the TTL
    instead of giving the model a fresh chance, live-observed on the
    odoo_mcp plugin 2026-08-26. Two agents run the SAME task through a
    router that always makes the same wrong first guess against a toolset
    that always rejects it: the first run's opening step must not poison
    the second run's opening step."""
    router = _ToolCallThenDoneRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    _dept_id, agent_a, agent_b = await _seed_department_with_two_agents(app_session, tenant)

    async with app_session(tenant) as db:
        await run_agent(
            db,
            agent=agent_a,
            task_text="find open tickets",
            tenant_id=tenant,
            toolset=_RejectingToolset(),
        )
    assert router.calls == 2, "first run is always real: the tool-call step, then the final step"

    async with app_session(tenant) as db:
        await run_agent(
            db,
            agent=agent_b,
            task_text="find open tickets",
            tenant_id=tenant,
            toolset=_RejectingToolset(),
        )

    # Only 1 more call, not 2: the opening step was invalidated (miss, real
    # call), but the SECOND step's completion ("Done.", no tool call, never
    # failed) is still a legitimate cache hit -- invalidation must be
    # precise, not a blanket "never cache this run again".
    assert router.calls == 3, (
        "the failed opening step must reach the router again on a fresh identical "
        "trigger instead of replaying its own failing tool call from cache, while "
        "the unrelated second step stays a normal cache hit"
    )


async def _seed_department_with_two_agents(
    app_session: AppSessionFactory, tenant: uuid.UUID, *, caching_enabled: bool = True
) -> tuple[uuid.UUID, m.Agent, m.Agent]:
    """Two DISTINCT agent rows (different ids) that are same-role clones: the
    scenario this feature targets is several agents in one department doing
    the same kind of routine work, which -- since `system_prompt()` opens with
    literally `f"You are {agent.name}"` -- requires an identical `name`, not
    just identical `definition`/`presentation`, for their assembled messages
    to actually match. Two agents with genuinely different names (real
    individual personas) would correctly MISS the cache; that's exactly the
    exact-match semantics this plan chose (design doc §2), not a bug."""
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="Support",
            frame={},
            prompt_caching_enabled=caching_enabled,
        )
        db.add(dept)
        await db.flush()
        agent_a = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Triage Agent",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        agent_b = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Triage Agent",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add_all([agent_a, agent_b])
        await db.flush()
        return dept.id, agent_a, agent_b


async def test_a_second_identical_request_in_the_same_department_is_served_from_cache(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two DIFFERENT agents in the SAME department, asked the identical
    question -- the model is only called once. The load-bearing assertion for
    the whole feature; everything else is plumbing in service of this."""
    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    _dept_id, agent_a, agent_b = await _seed_department_with_two_agents(app_session, tenant)

    async with app_session(tenant) as db:
        result_a = await run_agent(
            db, agent=agent_a, task_text="classify this ticket", tenant_id=tenant
        )
    async with app_session(tenant) as db:
        result_b = await run_agent(
            db, agent=agent_b, task_text="classify this ticket", tenant_id=tenant
        )

    assert router.calls == 1, "the second identical request must be served from cache"
    assert result_b.output == result_a.output


async def test_an_answer_from_a_different_model_than_requested_is_not_cached(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache key says "this is what provider/model X answers". A router
    availability downgrade or a fallback-chain hop answers as something else
    entirely -- storing THAT under the requested model's key would keep serving
    the substitute's answer for up to 24h to later requests that legitimately
    have the requested model available again. So it must not be stored, and a
    second identical request must reach the router for real."""
    router = _DowngradingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    _dept_id, agent_a, agent_b = await _seed_department_with_two_agents(app_session, tenant)

    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_a, task_text="classify this ticket", tenant_id=tenant)
    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_b, task_text="classify this ticket", tenant_id=tenant)

    assert router.calls == 2, (
        "a downgraded/fallback answer must never be cached under the key "
        "computed from the originally-requested provider/model"
    )


async def test_a_credentials_base_url_change_is_not_served_from_the_old_cache(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache key must include the base_url that will ACTUALLY be sent,
    not just provider/model -- an openai_compatible tenant rotating their
    endpoint (e.g. swapping which opaas.cloud-style proxy they point at)
    must reach the router for real, not keep getting an answer cached under
    the old address. Both agents get the identical task/messages; only the
    tenant's credential changes between the two runs."""
    import base64

    from oc8 import config
    from oc8.credentials.service import create_credential

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )

    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    _dept_id, agent_a, agent_b = await _seed_department_with_two_agents(app_session, tenant)

    async with app_session(tenant) as db:
        # `agent_a`/`agent_b` are detached (returned from a now-closed
        # session block) -- re-fetch through THIS session to mutate them,
        # same as every other cross-session use of these two just reads
        # them, never writes.
        for agent_id in (agent_a.id, agent_b.id):
            fresh = await db.get(m.Agent, agent_id)
            assert fresh is not None
            fresh.presentation = {"provider": "openai_compatible"}
        await create_credential(
            db,
            tenant_id=tenant,
            name="opaas ai",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-opaas", "base_url": "https://opaas.cloud/v1"},
        )
        await db.flush()

    async with app_session(tenant) as db:
        fresh_a = await db.get(m.Agent, agent_a.id)
        assert fresh_a is not None
        await run_agent(db, agent=fresh_a, task_text="classify this ticket", tenant_id=tenant)
    assert router.calls == 1

    async with app_session(tenant) as db:
        cred = (
            (
                await db.execute(
                    select(m.Credential).where(
                        m.Credential.tenant_id == tenant,
                        m.Credential.credential_type == "openai_compatible_api_key",
                    )
                )
            )
            .scalars()
            .one()
        )
        cred.field_values = {**cred.field_values, "base_url": "https://new-endpoint.example.com/v1"}
        await db.flush()

    async with app_session(tenant) as db:
        fresh_b = await db.get(m.Agent, agent_b.id)
        assert fresh_b is not None
        await run_agent(db, agent=fresh_b, task_text="classify this ticket", tenant_id=tenant)

    assert router.calls == 2, (
        "a base_url rotation must not keep serving the previous endpoint's "
        "cached answer -- the cache key has to have moved with it"
    )


async def test_two_different_departments_never_share_a_cache_hit(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same tenant, two SEPARATE departments, byte-identical request -- must
    NOT hit. Two real model calls, not one.

    The two agents share the SAME name (unlike their department names) so
    that department is the only variable between the two runs: system_prompt()
    opens with `f"You are {agent.name}"`, so differently-named agents would
    already produce non-matching messages regardless of department isolation,
    letting this test pass for the wrong reason (message content differing,
    not department_id namespacing the key) -- see
    _seed_department_with_two_agents's docstring for the same issue in the
    same-department tests."""
    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept_x = m.Department(tenant_id=tenant, name="X", frame={})
        dept_y = m.Department(tenant_id=tenant, name="Y", frame={})
        db.add_all([dept_x, dept_y])
        await db.flush()
        agent_x = m.Agent(
            tenant_id=tenant,
            department_id=dept_x.id,
            name="Triage Agent",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        agent_y = m.Agent(
            tenant_id=tenant,
            department_id=dept_y.id,
            name="Triage Agent",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add_all([agent_x, agent_y])
        await db.flush()

    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_x, task_text="classify this ticket", tenant_id=tenant)
    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_y, task_text="classify this ticket", tenant_id=tenant)

    assert router.calls == 2, "two different departments must never share a cache entry"


async def test_disabling_prompt_caching_bypasses_the_cache_even_for_a_repeat(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """department.prompt_caching_enabled = False -- every call reaches the
    router, even a literal repeat from the SAME agent."""
    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    _dept_id, agent_a, _agent_b = await _seed_department_with_two_agents(
        app_session, tenant, caching_enabled=False
    )

    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_a, task_text="classify this ticket", tenant_id=tenant)
    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_a, task_text="classify this ticket", tenant_id=tenant)

    assert router.calls == 2, "caching disabled must mean every call is real, even a repeat"


async def test_a_cache_hit_writes_a_token_usage_record_with_zero_actual_cost(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the SECOND (cache-hit) call: a TokenUsageRecord exists with
    cache_hit=True, tokens_in=0, tokens_out=0, and
    saved_tokens_in/saved_tokens_out matching what the
    ORIGINAL call actually produced."""
    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    dept_id, agent_a, agent_b = await _seed_department_with_two_agents(app_session, tenant)

    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_a, task_text="classify this ticket", tenant_id=tenant)
    async with app_session(tenant) as db:
        await run_agent(db, agent=agent_b, task_text="classify this ticket", tenant_id=tenant)

    assert router.calls == 1
    async with app_session(tenant) as db:
        records = (
            (
                await db.execute(
                    select(m.TokenUsageRecord)
                    .where(m.TokenUsageRecord.department_id == dept_id)
                    .order_by(m.TokenUsageRecord.ts)
                )
            )
            .scalars()
            .all()
        )
    assert len(records) == 2
    original, hit = records
    assert original.cache_hit is False
    assert hit.cache_hit is True
    assert hit.tokens_in == 0
    assert hit.tokens_out == 0
    assert hit.saved_tokens_in == original.tokens_in == 120
    assert hit.saved_tokens_out == original.tokens_out == 40


async def test_redis_being_unreachable_does_not_fail_the_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do NOT request the redis_url fixture here. Point the cache module's
    own client factory at an address nothing is listening on, and confirm
    run_agent still completes with a real (uncached) model response instead
    of raising."""
    import redis.asyncio as redis_asyncio

    def _unreachable_client() -> Any:
        return redis_asyncio.from_url(
            "redis://127.0.0.1:1", decode_responses=True, socket_connect_timeout=0.2
        )

    monkeypatch.setattr("oc8.caching.department_cache._client", _unreachable_client)
    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    _dept_id, agent_a, _agent_b = await _seed_department_with_two_agents(app_session, tenant)

    async with app_session(tenant) as db:
        result = await run_agent(
            db, agent=agent_a, task_text="classify this ticket", tenant_id=tenant
        )

    assert result.output == "Triage: billing question, routed to finance."
    assert router.calls == 1


async def test_restricted_content_is_never_cached(
    app_session: AppSessionFactory, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose retrieved KB context is classified restricted must not be
    read from or written to the department cache -- proven not merely by call
    counts (a bug that caches restricted content under a slightly different
    key could still pass a naive count-based check) but by reusing the exact
    same KB chunk, reclassified non-restricted, for a second identical
    request: retrieve_kb_context's assembled context text depends only on KB
    name/chunk content/source_uri, never on the classification field itself
    (see oc8.knowledge.retrieval.retrieve_kb_context), so the two requests
    produce byte-identical assembled messages and therefore the SAME cache
    key -- differing only in whether contains_restricted was True. If the
    first (restricted) run had wrongly written to that key, the second
    (declassified) run would come back a hit; it must not."""
    from oc8.knowledge.ingest import ingest_document
    from oc8.models.knowledge import EMBED_DIM

    class _FakeEmbedRouter:
        async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
            seed = sum(ord(c) for c in text) or 1
            return [float((seed + i) % 23) for i in range(EMBED_DIM)]

    monkeypatch.setattr("oc8.knowledge.ingest.get_model_router", lambda: _FakeEmbedRouter())
    monkeypatch.setattr("oc8.knowledge.retrieval.get_model_router", lambda: _FakeEmbedRouter())
    router = _CountingRouter()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(
            tenant_id=tenant,
            name="Legal",
            frame={"cleared_classes": ["public", "internal", "confidential", "restricted"]},
            prompt_caching_enabled=True,
        )
        db.add(dept)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=dept.id,
            name="Agent R",
            status="running",
            narrowing={},
            definition={},
            presentation={},
        )
        db.add(agent)
        await db.flush()
        kb = m.KnowledgeBase(tenant_id=tenant, name="Legal", embedding_model="nomic-embed-text")
        db.add(kb)
        await db.flush()
        await ingest_document(
            db,
            tenant_id=tenant,
            kb_id=kb.id,
            filename="a.txt",
            content="top secret merger terms",
            content_type="text/plain",
        )
        chunk = (await db.execute(select(m.KbChunk).where(m.KbChunk.kb_id == kb.id))).scalar_one()
        chunk.classification = "restricted"
        await db.flush()
        chunk_id = chunk.id
        db.add(
            m.KnowledgeGrant(
                tenant_id=tenant, kb_id=kb.id, grantee_type="agent", grantee_id=agent.id
            )
        )
        await db.flush()

    # First run: the agent has no model_config_id, so provider defaults to
    # "ollama" and model_locality resolves to "local" -- the restricted chunk
    # clears retrieve_kb_context's locality check and is included, so
    # contains_restricted is True for this run.
    async with app_session(tenant) as db:
        result_1 = await run_agent(db, agent=agent, task_text="merger terms", tenant_id=tenant)
    assert router.calls == 1, "first call is always real -- nothing was cached yet either way"

    # Reclassify the SAME chunk as non-restricted. Content, KB name and
    # source_uri (everything that reaches the assembled messages) are
    # unchanged, so this run's messages are byte-identical to the first run's
    # -- only contains_restricted flips to False.
    async with app_session(tenant) as db:
        chunk_2 = await db.get(m.KbChunk, chunk_id)
        assert chunk_2 is not None
        chunk_2.classification = "internal"
        await db.flush()

    async with app_session(tenant) as db:
        result_2 = await run_agent(db, agent=agent, task_text="merger terms", tenant_id=tenant)
    assert router.calls == 2, (
        "the restricted run must not have populated the cache entry this "
        "byte-identical, now-declassified request reads from"
    )
    assert result_2.output == result_1.output

    # And the now-non-restricted request is itself cacheable -- proving the
    # miss above was specifically about restricted-content exclusion, not
    # some other reason the two runs never shared an entry.
    async with app_session(tenant) as db:
        result_3 = await run_agent(db, agent=agent, task_text="merger terms", tenant_id=tenant)
    assert router.calls == 2, "the second (non-restricted) request must itself be cacheable"
    assert result_3.output == result_2.output
