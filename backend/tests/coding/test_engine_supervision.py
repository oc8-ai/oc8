from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.agent.engine import run_agent
from oc8.constants import ACME_TENANT_ID
from oc8.modelrouter import (
    CompletionResult,
    EmbeddingUnavailable,
    NeutralTool,
    ToolCall,
    Usage,
    chunk_from_result,
)
from oc8.runtime.supervision_hook import use_supervision_runtime
from oc8.supervision.runtime import SUPERVISION_QUERY_PORT, SUPERVISION_RUN_HOOK
from oc8.supervision.service import assign_supervisor, create_policy
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio
LOOP_HOOK_MODULE = "oc8.supervision.loop_hook"


@pytest.fixture(autouse=True)
def _activate_supervision_runtime() -> Iterator[None]:
    """Direct engine calls must install the run-loop hook themselves."""
    with use_supervision_runtime(SUPERVISION_RUN_HOOK, SUPERVISION_QUERY_PORT):
        yield


class _FakeEmbedRouter:
    async def embed(self, text: str) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(8)]


class _UnavailableEmbedRouter:
    async def embed(self, text: str) -> list[float]:
        raise EmbeddingUnavailable("no embedding backend configured")


class _NoToolCallRouter:
    """A single completion turn with no tool calls -> run_agent finishes
    immediately via the 'done' early-exit path."""

    async def complete(self, req: Any) -> CompletionResult:
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(tokens_in=3, tokens_out=3),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


class _SixPingsThenDoneRouter:
    """Step 1: six tool calls in a single turn (crosses a checkpoint_every
    tool_calls=5 cadence mid-step). Step 2: no tool calls -> done."""

    def __init__(self) -> None:
        self._calls = 0

    async def complete(self, req: Any) -> CompletionResult:
        self._calls += 1
        if self._calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id=f"c{i}", name="ping", arguments={}) for i in range(6)],
                usage=Usage(tokens_in=5, tokens_out=5),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(tokens_in=3, tokens_out=3),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


class _PingToolset:
    tools: list[NeutralTool] = [
        NeutralTool(
            name="ping", description="ping", parameters={"type": "object", "properties": {}}
        )
    ]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        return "pong"


async def _make_supervised_agent(db: Any, tenant: uuid.UUID) -> m.Agent:
    dept = m.Department(tenant_id=tenant, name="Ops", frame={})
    db.add(dept)
    await db.flush()
    supervisor = m.Agent(tenant_id=tenant, department_id=dept.id, name="Chief")
    supervised = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep")
    db.add(supervisor)
    db.add(supervised)
    await db.flush()
    policy = await create_policy(
        db,
        tenant_id=tenant,
        department_id=dept.id,
        checkpoint_every={"tool_calls": 5},
        drift_thresholds={"steer": 0.35, "rewind": 0.55, "escalate": 0.75},
        allowed_interventions=["steer", "rewind", "pause_escalate"],
    )
    await assign_supervisor(
        db,
        tenant_id=tenant,
        supervisor_agent_id=supervisor.id,
        supervised_agent_id=supervised.id,
        policy_id=policy.id,
    )
    return supervised


async def test_supervised_agent_gets_anchor_and_mid_run_checkpoint(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        agent = await _make_supervised_agent(db, tenant)
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _SixPingsThenDoneRouter())
        result = await run_agent(
            db,
            agent=agent,
            task_text="run the playbook",
            tenant_id=tenant,
            toolset=_PingToolset(),
        )
        assert result.status == "done"

        anchors = (
            (await db.execute(select(m.TaskAnchor).where(m.TaskAnchor.task_id == result.task_id)))
            .scalars()
            .all()
        )
        assert len(anchors) == 1
        assert anchors[0].objective == "run the playbook"

        checkpoints = (
            (
                await db.execute(
                    select(m.AgentCheckpoint)
                    .where(m.AgentCheckpoint.task_id == result.task_id)
                    .order_by(m.AgentCheckpoint.seq)
                )
            )
            .scalars()
            .all()
        )
        # one mid-run checkpoint (5th ping crosses the tool_calls=5 cadence)
        # + one forced final checkpoint at the 'done' exit.
        assert len(checkpoints) == 2
        assert checkpoints[0].seq == 1
        assert checkpoints[1].seq == 2


async def test_supervised_agent_short_run_gets_one_forced_checkpoint(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())

    async with app_session(tenant) as db:
        agent = await _make_supervised_agent(db, tenant)
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _NoToolCallRouter())
        result = await run_agent(
            db,
            agent=agent,
            task_text="quick question",
            tenant_id=tenant,
        )
        assert result.status == "done"

        checkpoints = (
            (
                await db.execute(
                    select(m.AgentCheckpoint).where(m.AgentCheckpoint.task_id == result.task_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(checkpoints) == 1


async def test_unsupervised_agent_gets_no_anchor_or_checkpoint(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))

    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Eng", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Dev")
        db.add(agent)
        await db.flush()
        # No SupervisionAssignment created.

        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _NoToolCallRouter())
        result = await run_agent(db, agent=agent, task_text="anything", tenant_id=tenant)
        assert result.status == "done"

        anchors = (
            (await db.execute(select(m.TaskAnchor).where(m.TaskAnchor.task_id == result.task_id)))
            .scalars()
            .all()
        )
        checkpoints = (
            (
                await db.execute(
                    select(m.AgentCheckpoint).where(m.AgentCheckpoint.task_id == result.task_id)
                )
            )
            .scalars()
            .all()
        )
        assert anchors == []
        assert checkpoints == []


async def test_supervised_agent_embedding_failure_does_not_break_run(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _UnavailableEmbedRouter())

    async with app_session(tenant) as db:
        agent = await _make_supervised_agent(db, tenant)
        monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: _NoToolCallRouter())
        result = await run_agent(db, agent=agent, task_text="quick question", tenant_id=tenant)
        assert result.status == "done"

        anchor = (
            await db.execute(select(m.TaskAnchor).where(m.TaskAnchor.task_id == result.task_id))
        ).scalar_one()
        assert anchor.anchor_embedding is None
        checkpoint = (
            await db.execute(
                select(m.AgentCheckpoint).where(m.AgentCheckpoint.task_id == result.task_id)
            )
        ).scalar_one()
        assert checkpoint.drift_score is not None  # anchor exists -> score computed, never None
