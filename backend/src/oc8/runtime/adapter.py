"""Pluggable agent runtimes (§8.7, dev-stage adaptation): a RuntimeAdapter
protocol that run_agent is wrapped by, plus a minimal test/proof stub that
never touches the model router. Real container-per-runtime execution,
sandboxing by trust level, and dynamic entry-point loading are out of scope
— see docs/superpowers/specs/2026-07-16-pluggable-runtimes-design.md."""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.engine import CancelCheck, InboxCheck, RunResult, run_agent


class RuntimeAdapter(Protocol):
    async def execute(
        self,
        db: AsyncSession,
        *,
        agent: m.Agent,
        task_text: str,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        mcp_conn: m.McpConnection | None = None,
        parent_task_id: uuid.UUID | None = None,
        delegation_depth: int = 0,
        cancel_check: CancelCheck | None = None,
        inbox_check: InboxCheck | None = None,
        pre_decided: dict[str, str] | None = None,
        originating_operator: str | None = None,
        task_images_raw: list[dict[str, str]] | None = None,
    ) -> RunResult: ...


@runtime_checkable
class EvidenceProducingRuntime(Protocol):
    """A runtime that leaves a per-run directory behind on the host.

    Optional, and deliberately the only thing core knows about that directory.
    Core owns the archive format, the hash, the ledger entry and the retention
    window (`oc8.evidence`); the runtime owns where its evidence lives and which
    of the files in it are its own housekeeping rather than a record of what the
    agent did. Core naming `claude-home/telemetry/` would put one vendor's
    private layout in the neutral core -- the same rule that keeps
    `runtime_session_root` a path core agrees on rather than a layout core
    understands.

    A runtime that does not implement this simply has no evidence to sweep; the
    in-process runtime writes nothing to disk, so it does not.
    """

    #: Paths relative to the evidence directory that this runtime writes for its
    #: OWN purposes. Left out of the archive and reported as dropped, never
    #: silently discarded. Matched on a path boundary, so a directory prefix
    #: takes everything under it.
    evidence_excludes: tuple[str, ...]

    def evidence_dir(self, *, agent_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
        """This run's evidence directory, or None if it never had one."""
        ...


class Oc8AgentRuntime:
    """First-party reference runtime — the §8.3 loop, unmodified."""

    #: Declares whether a message written to a running run can reach it
    #: (spec §3.2): the step loop reads run inputs between steps.
    accepts_inputs: bool = True

    async def execute(
        self,
        db: AsyncSession,
        *,
        agent: m.Agent,
        task_text: str,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        mcp_conn: m.McpConnection | None = None,
        parent_task_id: uuid.UUID | None = None,
        delegation_depth: int = 0,
        cancel_check: CancelCheck | None = None,
        inbox_check: InboxCheck | None = None,
        pre_decided: dict[str, str] | None = None,
        originating_operator: str | None = None,
        task_images_raw: list[dict[str, str]] | None = None,
    ) -> RunResult:
        return await run_agent(
            db,
            agent=agent,
            task_text=task_text,
            tenant_id=tenant_id,
            # Passed through so a resume leg continues its suspended leg's task
            # instead of opening a second one (see open_run_task).
            run_id=run_id,
            mcp_conn=mcp_conn,
            parent_task_id=parent_task_id,
            delegation_depth=delegation_depth,
            cancel_check=cancel_check,
            inbox_check=inbox_check,
            pre_decided=pre_decided,
            originating_operator=originating_operator,
            task_images_raw=task_images_raw,
        )


class EchoRuntimeStub:
    """Minimal, non-production runtime used only to prove the negotiation
    and dispatch machinery against real installed-plugin data. Not a usable
    runtime — never call the model router, never persist anything real."""

    #: Declares whether a message written to a running run can reach it
    #: (spec §3.2): this stub has no step boundary to deliver one at.
    accepts_inputs: bool = False

    async def execute(
        self,
        db: AsyncSession,
        *,
        agent: m.Agent,
        task_text: str,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        mcp_conn: m.McpConnection | None = None,
        parent_task_id: uuid.UUID | None = None,
        delegation_depth: int = 0,
        cancel_check: CancelCheck | None = None,
        inbox_check: InboxCheck | None = None,
        pre_decided: dict[str, str] | None = None,
        originating_operator: str | None = None,
        task_images_raw: list[dict[str, str]] | None = None,
    ) -> RunResult:
        return RunResult(
            task_id=uuid.uuid4(),
            agent_id=agent.id,
            status="done",
            output=f"echo-runtime-stub: {task_text}",
            tool_calls=[],
            steps=0,
        )
