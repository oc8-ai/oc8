from __future__ import annotations

import uuid
from typing import Any

import pytest

from oc8 import models as em
from oc8.modelrouter.router import ClassificationViolation
from oc8.modelrouter.types import CompletionResult, Usage
from oc8.supervision.judge import (
    VERDICT_FLOOR,
    judge_checkpoint,
    parse_verdict,
    should_judge,
)
from tests.conftest import AppSessionFactory

_GOOD_JSON = (
    '{"verdict": "off_track", "evidence": ["looped on search 4x"], '
    '"recommended_intervention": "pause_escalate", "rationale": "no progress"}'
)


class _FakeRouter:
    """Records calls; returns a canned completion."""

    def __init__(self, text: str = _GOOD_JSON) -> None:
        self.calls: list[Any] = []
        self._text = text

    async def complete(self, req: Any) -> CompletionResult:
        self.calls.append(req)
        return CompletionResult(
            text=self._text,
            tool_calls=[],
            usage=Usage(tokens_in=10, tokens_out=5),
            stop_reason="stop",
            provider="fake",
            model="fake-cheap",
        )


class _RaisingRouter:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[Any] = []

    async def complete(self, req: Any) -> CompletionResult:
        self.calls.append(req)
        raise self.exc


class _Rng:
    """Deterministic rng stub: returns the queued values."""

    def __init__(self, value: float) -> None:
        self._value = value

    def random(self) -> float:
        return self._value


def _policy(sampling_rate: float = 1.0) -> em.SupervisionPolicy:
    return em.SupervisionPolicy(
        tenant_id=uuid.uuid4(),
        department_id=uuid.uuid4(),
        checkpoint_every={"tool_calls": 5},
        drift_thresholds={},
        allowed_interventions=["steer", "pause_escalate"],
        sampling_rate=sampling_rate,
    )


# --- sampling ------------------------------------------------------------
def test_sampling_rate_zero_never_judges() -> None:
    assert (
        should_judge(
            policy=_policy(0.0), tier1_verdict="on_track", tier0_alert=False, rng=_Rng(0.0)
        )
        is False
    )


def test_sampling_rate_one_always_judges() -> None:
    assert (
        should_judge(
            policy=_policy(1.0), tier1_verdict="on_track", tier0_alert=False, rng=_Rng(0.99)
        )
        is True
    )


def test_tier1_drift_forces_judge_despite_zero_sampling() -> None:
    for verdict in ("drifting", "off_track"):
        assert (
            should_judge(
                policy=_policy(0.0), tier1_verdict=verdict, tier0_alert=False, rng=_Rng(0.99)
            )
            is True
        )


def test_tier0_alert_forces_judge_despite_zero_sampling() -> None:
    assert (
        should_judge(
            policy=_policy(0.0), tier1_verdict="on_track", tier0_alert=True, rng=_Rng(0.99)
        )
        is True
    )


# --- parsing -------------------------------------------------------------
def test_parse_verdict_happy() -> None:
    jv = parse_verdict(_GOOD_JSON, model="fake/fake-cheap")
    assert jv is not None
    assert jv.verdict == "off_track"
    assert jv.recommended_intervention == "pause_escalate"
    assert jv.evidence == ["looped on search 4x"]
    assert jv.model == "fake/fake-cheap"


@pytest.mark.parametrize(
    "bad",
    ["not json at all", "{}", '{"verdict": "banana"}', '{"evidence": []}', ""],
)
def test_parse_verdict_rejects_bad(bad: str) -> None:
    assert parse_verdict(bad, model="m") is None


def test_verdict_floor_ordering() -> None:
    assert VERDICT_FLOOR["on_track"] < VERDICT_FLOOR["drifting"] < VERDICT_FLOOR["off_track"]


# --- judge_checkpoint ----------------------------------------------------
async def _checkpoint(s: Any, tenant: uuid.UUID) -> em.AgentCheckpoint:
    cp = em.AgentCheckpoint(
        tenant_id=tenant,
        agent_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        seq=1,
        state_summary="did a thing",
        drift_score=0.1,
        verdict="on_track",
    )
    s.add(cp)
    await s.flush()
    return cp


async def test_judge_checkpoint_returns_verdict(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _FakeRouter()
    monkeypatch.setattr("oc8.supervision.judge.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp = await _checkpoint(s, tenant)
        jv = await judge_checkpoint(
            s,
            tenant_id=tenant,
            policy=_policy(),
            anchor=None,
            checkpoint=cp,
            trajectory_excerpt="tool: search x4",
            signals={"drift_score": 0.1},
            contains_restricted=False,
            supervisor_agent_id=uuid.uuid4(),
        )
    assert jv is not None and jv.verdict == "off_track"
    assert len(router.calls) == 1
    assert router.calls[0].contains_restricted is False


async def test_judge_threads_contains_restricted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _FakeRouter()
    monkeypatch.setattr("oc8.supervision.judge.get_model_router", lambda: router)
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp = await _checkpoint(s, tenant)
        await judge_checkpoint(
            s,
            tenant_id=tenant,
            policy=_policy(),
            anchor=None,
            checkpoint=cp,
            trajectory_excerpt="x",
            signals={},
            contains_restricted=True,
            supervisor_agent_id=uuid.uuid4(),
        )
    assert router.calls[0].contains_restricted is True


@pytest.mark.parametrize(
    "exc", [ClassificationViolation("restricted"), TimeoutError(), RuntimeError("boom")]
)
async def test_judge_degrades_to_none_on_any_error(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setattr("oc8.supervision.judge.get_model_router", lambda: _RaisingRouter(exc))
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp = await _checkpoint(s, tenant)
        jv = await judge_checkpoint(
            s,
            tenant_id=tenant,
            policy=_policy(),
            anchor=None,
            checkpoint=cp,
            trajectory_excerpt="x",
            signals={},
            contains_restricted=False,
            supervisor_agent_id=uuid.uuid4(),
        )
    assert jv is None  # never raises


async def test_judge_skips_when_budget_hard_exceeded(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = _FakeRouter()
    monkeypatch.setattr("oc8.supervision.judge.get_model_router", lambda: router)

    class _Hard:
        soft_exceeded = True
        hard_exceeded = True

    async def _fake_check(db: Any, **kw: Any) -> Any:
        return _Hard()

    monkeypatch.setattr("oc8.supervision.judge.check_budget", _fake_check)
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp = await _checkpoint(s, tenant)
        jv = await judge_checkpoint(
            s,
            tenant_id=tenant,
            policy=_policy(),
            anchor=None,
            checkpoint=cp,
            trajectory_excerpt="x",
            signals={},
            contains_restricted=False,
            supervisor_agent_id=uuid.uuid4(),
        )
    assert jv is None
    assert router.calls == []  # the LLM was never called
