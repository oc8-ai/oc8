from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from oc8 import models as em
from oc8 import models as m
from oc8.constants import ACME_TENANT_ID
from oc8.modelrouter import EmbeddingUnavailable
from oc8.supervision.judge import JudgeVerdict
from oc8.supervision.loop_hook import maybe_checkpoint, maybe_create_anchor
from oc8.supervision.service import assign_supervisor, create_policy
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio
LOOP_HOOK_MODULE = "oc8.supervision.loop_hook"


class _FakeEmbedRouter:
    async def embed(self, text: str) -> list[float]:
        seed = sum(ord(c) for c in text) or 1
        return [float((seed + i) % 23) for i in range(8)]


class _UnavailableRouter:
    async def embed(self, text: str) -> list[float]:
        raise EmbeddingUnavailable("no embedding backend configured")


async def _assign(
    db: Any,
    *,
    tenant: uuid.UUID,
    supervisor_id: uuid.UUID,
    supervised_id: uuid.UUID,
    sampling_rate: float = 1.0,
    checkpoint_every: dict[str, Any] | None = None,
    drift_thresholds: dict[str, Any] | None = None,
) -> None:
    policy = await create_policy(
        db,
        tenant_id=tenant,
        department_id=uuid.uuid4(),
        checkpoint_every=checkpoint_every or {"tool_calls": 3},
        drift_thresholds=drift_thresholds or {"steer": 0.35, "rewind": 0.55, "escalate": 0.75},
        allowed_interventions=["steer", "rewind", "pause_escalate"],
        sampling_rate=sampling_rate,
    )
    await assign_supervisor(
        db,
        tenant_id=tenant,
        supervisor_agent_id=supervisor_id,
        supervised_agent_id=supervised_id,
        policy_id=policy.id,
    )


async def test_maybe_create_anchor_none_without_assignment(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            task_text="do the thing",
        )
        assert anchor is None


async def test_maybe_create_anchor_creates_row_when_supervised(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship the report",
        )
        assert anchor is not None
        assert anchor.objective == "ship the report"
        assert anchor.task_id == task_id
        assert anchor.anchor_embedding is not None


async def test_maybe_create_anchor_degrades_on_embedding_failure(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _UnavailableRouter())
    supervised_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=uuid.uuid4(),
            task_text="ship the report",
        )
        assert anchor is not None
        assert anchor.anchor_embedding is None


async def test_maybe_checkpoint_none_without_anchor(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            anchor=None,
            tool_trace_delta=[{"tool": "x", "result": "y"}],
            tokens_since_checkpoint=999999,
            force=True,
        )
        assert cp is None


async def test_maybe_checkpoint_waits_for_cadence_threshold(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        # policy's checkpoint_every.tool_calls == 3 (see _assign)
        below = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}, {"tool": "b", "result": "2"}],
            tokens_since_checkpoint=0,
            force=False,
        )
        assert below is None
        at_threshold = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[
                {"tool": "a", "result": "1"},
                {"tool": "b", "result": "2"},
                {"tool": "c", "result": "3"},
            ],
            tokens_since_checkpoint=0,
            force=False,
        )
        assert at_threshold is not None
        assert at_threshold.seq == 1


async def test_maybe_checkpoint_forced_fires_with_empty_trace(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert cp is not None
        assert cp.state_summary  # non-empty fallback text, not blank


async def test_maybe_checkpoint_degrades_on_trajectory_embedding_failure(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        monkeypatch.setattr(
            "oc8.supervision.loop_hook.get_model_router", lambda: _FakeEmbedRouter()
        )
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        monkeypatch.setattr(
            "oc8.supervision.loop_hook.get_model_router", lambda: _UnavailableRouter()
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert cp is not None
        # anchor embedding present, trajectory embedding failed -> divergence
        # contributes 0, so score is a computed 0.0 (on_track), never None,
        # because an anchor exists (only a fully unsupervised agent -> no
        # anchor -> score None; see compute_drift_score's own contract).
        assert cp.drift_score is not None
        assert cp.verdict == "on_track"


# --- §8.6 Task 3: tier-0 alerts + tier-2 judge + ladder wiring ---


class _RecordingJudge:
    """Stands in for judge_checkpoint; records every invocation."""

    def __init__(self, verdict: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, db: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._verdict


async def test_judge_is_invoked_and_verdict_persisted_when_sampled(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    judge = _RecordingJudge(
        JudgeVerdict(
            verdict="drifting",
            evidence=["repeated the same lookup"],
            recommended_intervention="steer",
            rationale="no progress",
            model="fake/fake",
        )
    )
    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", judge)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(
            db,
            tenant=tenant,
            supervisor_id=uuid.uuid4(),
            supervised_id=supervised_id,
            sampling_rate=1.0,
        )
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert cp is not None
        assert len(judge.calls) == 1
        assert judge.calls[0]["contains_restricted"] is False
        assert cp.judge_verdict == "drifting"
        assert cp.judge_evidence is not None
        assert cp.judge_evidence["evidence"] == ["repeated the same lookup"]
        # judge floor (drifting -> 0.55) drives the ladder past `steer`; `rewind`
        # is allowed by the policy but must never be applied automatically.
        interventions = (
            (
                await db.execute(
                    select(em.SupervisionIntervention).where(
                        em.SupervisionIntervention.task_id == task_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [i.kind for i in interventions] == ["steer"]
        assert interventions[0].reason["judge"]["verdict"] == "drifting"


async def test_raising_judge_does_not_propagate_out_of_maybe_checkpoint(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())

    async def _boom(db: Any, **kwargs: Any) -> Any:
        raise RuntimeError("judge exploded")

    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", _boom)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}],
            tokens_since_checkpoint=0,
            force=True,
        )
        # The checkpoint is still recorded and returned; nothing escaped.
        assert cp is not None
        assert cp.judge_verdict is None


async def test_tier0_loop_alert_forces_judge_at_zero_sampling(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    judge = _RecordingJudge(None)
    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", judge)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(
            db,
            tenant=tenant,
            supervisor_id=uuid.uuid4(),
            supervised_id=supervised_id,
            sampling_rate=0.0,
        )
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        # No tier-0 alert: distinct tools, no errors -> sampling_rate 0 skips.
        quiet = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}, {"tool": "b", "result": "2"}],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert quiet is not None
        assert judge.calls == []

        # Same tool three times in the window -> loop_detected -> judge forced.
        looped = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": str(i)} for i in range(3)],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert looped is not None
        assert len(judge.calls) == 1
        assert judge.calls[0]["signals"]["tier0_alert"] is True


async def test_tier0_tool_error_alert_forces_judge_at_zero_sampling(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    judge = _RecordingJudge(None)
    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", judge)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(
            db,
            tenant=tenant,
            supervisor_id=uuid.uuid4(),
            supervised_id=supervised_id,
            sampling_rate=0.0,
        )
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            # run_agent's real convention for a failed/denied call.
            tool_trace_delta=[
                {"tool": "a", "result": "ERROR: boom"},
                {"tool": "b", "result": "ok"},
            ],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert cp is not None
        assert len(judge.calls) == 1
        assert judge.calls[0]["signals"]["tier0_alert"] is True


async def test_ladder_never_auto_applies_rewind_from_the_loop(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """policy.allowed_interventions includes rewind; a rewind-band score must
    still only produce a `steer`."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    judge = _RecordingJudge(
        JudgeVerdict(
            verdict="off_track",
            evidence=["abandoned the objective"],
            recommended_intervention="pause_escalate",
            rationale="",
            model="fake/fake",
        )
    )
    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", judge)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        db.add(m.Agent(id=supervised_id, tenant_id=tenant, department_id=dept.id, name="sub"))
        await db.flush()
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert cp is not None and cp.judge_verdict == "off_track"
        interventions = (
            (
                await db.execute(
                    select(em.SupervisionIntervention).where(
                        em.SupervisionIntervention.task_id == task_id
                    )
                )
            )
            .scalars()
            .all()
        )
        # off_track floor 0.75 -> escalate band; rewind is filtered out entirely.
        assert [i.kind for i in interventions] == ["pause_escalate"]
        agent = await db.get(m.Agent, supervised_id)
        assert agent is not None and agent.status == "paused"


# --- Consolidated fix wave (final review) ---


async def test_contains_restricted_is_threaded_through_to_the_judge(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX 1: the caller's real restricted-data flag must reach judge_checkpoint,
    which is the only thing that makes the model router's classification guard
    fire (router.py inspects the flag, never the content)."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    judge = _RecordingJudge(None)
    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", judge)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(db, tenant=tenant, supervisor_id=uuid.uuid4(), supervised_id=supervised_id)
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}],
            tokens_since_checkpoint=0,
            force=True,
            contains_restricted=True,
        )
        assert cp is not None
        assert len(judge.calls) == 1
        assert judge.calls[0]["contains_restricted"] is True


async def test_token_cadence_checkpoint_does_not_force_the_judge(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX 2: with a token-cadence policy the tier-0 token watchdog must not be
    true-by-construction at every checkpoint — otherwise sampling_rate=0.0 still
    buys an LLM call every single time."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())
    judge = _RecordingJudge(None)
    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", judge)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(
            db,
            tenant=tenant,
            supervisor_id=uuid.uuid4(),
            supervised_id=supervised_id,
            sampling_rate=0.0,
            checkpoint_every={"tokens": 100},
        )
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        # A NORMAL token-cadence checkpoint: exactly at the cadence limit.
        normal = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}, {"tool": "b", "result": "2"}],
            tokens_since_checkpoint=100,
            force=False,
        )
        assert normal is not None
        assert judge.calls == []  # sampling_rate 0.0 must actually mean "don't judge"

        # Genuine burn, well past the cadence -> the tier-0 watchdog DOES fire.
        burning = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}, {"tool": "b", "result": "2"}],
            tokens_since_checkpoint=100_000,
            force=False,
        )
        assert burning is not None
        assert len(judge.calls) == 1
        assert judge.calls[0]["signals"]["tier0_alert"] is True


async def test_judge_db_failure_still_runs_the_ladder_and_leaves_session_usable(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX 3: a DB-level failure while judging must (a) not skip the intervention
    ladder, and (b) not leave the AsyncSession in PendingRollbackError state —
    otherwise supervision breaks the supervised run one statement later."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    monkeypatch.setattr(f"{LOOP_HOOK_MODULE}.get_model_router", lambda: _FakeEmbedRouter())

    async def _db_boom(db: Any, **kwargs: Any) -> Any:
        # A real database error (asyncpg DivisionByZeroError), which is what
        # poisons the session — not a plain Python exception.
        await db.execute(sa_text("SELECT 1 / 0"))
        return None

    monkeypatch.setattr("oc8.supervision.loop_hook.judge_checkpoint", _db_boom)
    supervised_id = uuid.uuid4()
    task_id = uuid.uuid4()
    async with app_session(tenant) as db:
        await _assign(
            db,
            tenant=tenant,
            supervisor_id=uuid.uuid4(),
            supervised_id=supervised_id,
            # steer at 0.0 -> the tier-1 score alone reaches the first rung, so
            # the ladder's behaviour is independent of the (failed) judge.
            drift_thresholds={"steer": 0.0, "rewind": 0.55, "escalate": 0.75},
        )
        anchor = await maybe_create_anchor(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            task_text="ship it",
        )
        cp = await maybe_checkpoint(
            db,
            tenant_id=tenant,
            agent_id=supervised_id,
            task_id=task_id,
            anchor=anchor,
            tool_trace_delta=[{"tool": "a", "result": "1"}],
            tokens_since_checkpoint=0,
            force=True,
        )
        assert cp is not None
        assert cp.judge_verdict is None  # judging failed, rolled back

        # (a) the ladder still fired on the tier-1 score
        interventions = (
            (
                await db.execute(
                    select(em.SupervisionIntervention).where(
                        em.SupervisionIntervention.task_id == task_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [i.kind for i in interventions] == ["steer"]

        # (b) the session is still usable for the caller's next statement
        assert (await db.execute(sa_text("SELECT 1"))).scalar_one() == 1
