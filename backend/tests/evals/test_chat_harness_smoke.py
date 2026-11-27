"""A smoke subset of the chat_harness eval suite, with a fake model, so the
runner's chat path cannot rot (spec §5.8). The real suite runs against a
configured model in the dev stack (docs/developer/chat-harness-evals.md).

The fake model is an echo: it answers with every non-system message it was
sent, keeping only the first hit of a knowledge block. A case therefore passes
only when what it asks for reached the model and what must not reach it did
not: a fact only through the checkpoint, a restricted answer never, an old
branch never. On the Copilot it calls the one status tool offered first.
Runs execute in-process here (`execute_run`), where the dev stack's worker
would pick them up.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from oc8_evals import __main__ as runner
from oc8_evals import stack
from oc8_evals.tasks import load_suite

from oc8 import models as m
from oc8.config import get_settings
from oc8.modelrouter import CompletionResult, Usage, chunk_from_result
from oc8.modelrouter.types import CompletionChunk, CompletionRequest, ToolCall
from oc8.models.knowledge import EMBED_DIM
from oc8.runtime.executor import execute_run
from oc8.runtime.queue import RunMessage
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

SUITE = Path(stack.__file__).parent / "suites" / "chat_harness"
_KNOWLEDGE = re.compile(r"\[Knowledge: [^\]]*\]\n(- [^\n]*)(?:\n- [^\n]*)*")
_WORD = re.compile(r"[a-z0-9][a-z0-9-]*[a-z0-9]")


def _vector(text: str) -> list[float]:
    """Hashed bag of words: texts that share words sit close together, and a
    shared identifier (a word with a digit) counts most."""
    vec = [0.0] * EMBED_DIM
    for word in _WORD.findall(text.lower()):
        weight = 10.0 if any(c.isdigit() for c in word) else 1.0
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % EMBED_DIM] += weight
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(str(getattr(part, "text", "")) for part in content or [])


class _EchoModel:
    async def complete(self, req: CompletionRequest) -> CompletionResult:
        offered = [t.name for t in req.tools or []]
        if "budget_overview" in offered and not any(msg.role == "tool" for msg in req.messages):
            return self._result("", [ToolCall(id="call-1", name="budget_overview", arguments={})])
        said = "\n".join(_text(msg.content) for msg in req.messages if msg.role != "system")
        return self._result(_KNOWLEDGE.sub(r"\1", said), [])

    @staticmethod
    def _result(text: str, calls: list[ToolCall]) -> CompletionResult:
        return CompletionResult(
            text=text,
            tool_calls=calls,
            usage=Usage(tokens_in=100, tokens_out=10, cache_read_tokens=40),
            stop_reason="tool_use" if calls else "stop",
            provider="openai",
            model="echo",
        )

    async def stream(self, req: CompletionRequest) -> AsyncIterator[CompletionChunk]:
        yield chunk_from_result(await self.complete(req))

    async def embed(self, text: str, model: str | None = None, **_: object) -> list[float]:
        return _vector(text)


@pytest.fixture
def echo(monkeypatch: pytest.MonkeyPatch) -> _EchoModel:
    model = _EchoModel()
    for where in (
        "oc8.agent.engine.get_model_router",
        "oc8.chat.checkpoint.get_model_router",
        "oc8.knowledge.retrieval.get_model_router",
        "oc8.memory.router.get_model_router",
        "oc8_evals.scenarios.chat_knowledge.get_model_router",
        "oc8_evals.scenarios.chat_restricted.get_model_router",
    ):
        monkeypatch.setattr(where, lambda: model, raising=False)
    real_wait = stack.wait_for_terminal

    async def run_then_wait(
        tenant_id: uuid.UUID, run_id: uuid.UUID, *, timeout_s: int
    ) -> tuple[str, dict[str, Any]]:
        message = RunMessage(
            run_id=str(run_id), tenant_id=str(tenant_id), entry_id="0-0", redelivered=False
        )
        await execute_run(message)
        return await real_wait(tenant_id, run_id, timeout_s=5)

    monkeypatch.setattr(stack, "wait_for_terminal", run_then_wait)
    return model


async def _cloud_model(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        config = m.ModelConfig(
            tenant_id=tenant, provider="openai", model="echo", locality="cloud", chat_enabled=True
        )
        db.add(config)
        await db.commit()
        return config.id


SMOKE = [
    "recall_booking_code",
    "knowledge_exact_article",
    "tool_budget_left",
    "restricted_history",
    "edit_drops_later_turns",
    "regenerate_ignores_first_answer",
]


@pytest.mark.parametrize("task_id", SMOKE)
async def test_a_smoke_case_passes_on_state(
    app_session: AppSessionFactory, redis_url: str, echo: _EchoModel, task_id: str
) -> None:
    tenant = uuid.uuid4()
    model_config_id = await _cloud_model(app_session, tenant)
    [task] = [t for t in load_suite(SUITE) if t.id == task_id]

    attempt = await runner._attempt(
        tenant,
        task,
        1,
        runtime="inprocess",
        run_tag=f"smoke-{uuid.uuid4().hex[:6]}",
        model_config_id=model_config_id,
        timeout_s=5,
    )

    assert attempt.error is None, attempt.error
    failed = [c for c in (*attempt.expect, *attempt.forbid) if not c.ok]
    assert not failed, failed
    assert attempt.passed and attempt.final_state == "done"
    assert attempt.group == task.group
    assert attempt.tokens_in > 0 and attempt.cache_read_tokens > 0


async def test_the_knowledge_case_reports_its_ranking(
    app_session: AppSessionFactory, redis_url: str, echo: _EchoModel
) -> None:
    tenant = uuid.uuid4()
    model_config_id = await _cloud_model(app_session, tenant)
    [task] = [t for t in load_suite(SUITE) if t.id == "knowledge_exact_article"]

    attempt = await runner._attempt(
        tenant,
        task,
        1,
        runtime="inprocess",
        run_tag=f"smoke-{uuid.uuid4().hex[:6]}",
        model_config_id=model_config_id,
        timeout_s=5,
    )

    assert attempt.metrics == {"hit@1": 1.0, "hit@5": 1.0}


async def test_a_restricted_answer_that_reached_the_model_fails_the_case(
    app_session: AppSessionFactory,
    redis_url: str,
    echo: _EchoModel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restricted check can fail: with the withholding switched off, the
    echo repeats the canary and the case is a policy violation."""
    monkeypatch.setattr("oc8.chat.replay.withheld", lambda msg, *, model_locality: False)
    tenant = uuid.uuid4()
    model_config_id = await _cloud_model(app_session, tenant)
    [task] = [t for t in load_suite(SUITE) if t.id == "restricted_history"]

    attempt = await runner._attempt(
        tenant,
        task,
        1,
        runtime="inprocess",
        run_tag=f"smoke-{uuid.uuid4().hex[:6]}",
        model_config_id=model_config_id,
        timeout_s=5,
    )

    assert attempt.error is None, attempt.error
    assert attempt.policy_violation and not attempt.passed


def test_the_suite_has_thirty_three_cases_in_seven_groups() -> None:
    tasks = load_suite(SUITE)
    assert len(tasks) == 33
    assert {t.group for t in tasks} == {
        "recall",
        "knowledge",
        "must_call_tool",
        "rejection_reason",
        "restricted",
        "edit_regenerate",
        "web",
    }
    assert all(t.kind == "chat" for t in tasks)


class _JiraModel(_EchoModel):
    """Reads ISSUE-1 (again in each resumed leg), then writes its description
    as asked -- or, once a rejection is in the conversation, as the reason
    says -- then answers."""

    async def complete(self, req: CompletionRequest) -> CompletionResult:
        said = "\n".join(_text(msg.content) for msg in req.messages)
        if "updated ISSUE-1" in said:
            return self._result("Done.", [])
        # This leg's own read: a resumed leg is checked afresh (read-before-write).
        leg = said.split("# Run context", 1)[-1].rsplit("Resuming after a decision", 1)[-1]
        if 'jira:jira_get_issue"' not in leg:
            read = ToolCall(id="read-1", name="jira_get_issue", arguments={"issue_key": "ISSUE-1"})
            return self._result("", [read])
        version = "2.4" if "rejected" in said.lower() else "2.3"
        call = ToolCall(
            id=f"call-{version}",
            name="jira_update_issue",
            arguments={"issue_key": "ISSUE-1", "description": f"Fixed in release {version}"},
        )
        return self._result("", [call])


@pytest.fixture
def jira_mock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """The repository's capas (the Jira tool pack's manifest), the mock server
    importable, a scratch directory for the mock's state file, and a secret
    store for the department's login."""
    monkeypatch.setenv("OC8_CAPAS_PATH", str(Path(stack.__file__).parents[3] / "capas"))
    monkeypatch.setenv("OC8_RUNTIME_SESSION_ROOT", str(tmp_path))
    # The secret store the department's login keeps its token in (a test key).
    monkeypatch.setenv("OC8_SECRET_KEK", base64.b64encode(bytes(range(32))).decode())
    monkeypatch.setattr(stack, "MOCK_PYTHONPATH", str(Path(stack.__file__).parents[1]))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _enable_jira(app_session: AppSessionFactory, tenant: uuid.UUID) -> None:
    """As in the dev stack the suite runs against: the Jira plugin is enabled."""
    async with app_session(tenant) as db:
        capa = m.Capa(tenant_id=tenant, name="jira_mcp", type="tool_pack")
        db.add(capa)
        await db.flush()
        db.add(
            m.CapaInstallation(
                tenant_id=tenant, capa_id=capa.id, status="enabled", granted_permissions=[]
            )
        )
        await db.commit()


async def test_a_rejected_write_is_decided_and_the_reason_followed(
    app_session: AppSessionFactory,
    redis_url: str,
    echo: _EchoModel,
    jira_mock: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _JiraModel()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: model)
    tenant = uuid.uuid4()
    model_config_id = await _cloud_model(app_session, tenant)
    await _enable_jira(app_session, tenant)
    [task] = [t for t in load_suite(SUITE) if t.id == "reject_use_other_version"]

    attempt = await runner._attempt(
        tenant,
        task,
        1,
        runtime="inprocess",
        run_tag=f"smoke-{uuid.uuid4().hex[:6]}",
        model_config_id=model_config_id,
        timeout_s=5,
    )

    assert attempt.error is None, attempt.error
    failed = [c for c in (*attempt.expect, *attempt.forbid) if not c.ok]
    assert not failed, (failed, attempt.tool_calls)
