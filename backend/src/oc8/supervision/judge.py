"""Tier-2 LLM-as-judge for supervision drift control (§8.6.2).

Sampled per policy, forced on tier-0/tier-1 alerts. Runs INLINE in the
checkpoint path, so it inherits loop_hook's contract: it must never raise into
run_agent. Every failure -- budget, classification, provider, timeout, malformed
JSON -- degrades to "no verdict" and the caller falls back to the tier-1 score.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.metering import record_usage
from oc8.metering.budget import check_budget
from oc8.modelrouter import get_model_router
from oc8.modelrouter.router import locality_for_provider
from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage

_VALID_VERDICTS = ("on_track", "drifting", "off_track")

# Maps a judge verdict onto the drift-score scale so the shipped threshold logic
# in choose_intervention keeps working unchanged. Values line up with the default
# thresholds (steer .35 / rewind .55 / escalate .75).
VERDICT_FLOOR: dict[str, float] = {"on_track": 0.0, "drifting": 0.55, "off_track": 0.75}

DEFAULT_RUBRIC = (
    "You are a supervision judge. Decide whether the agent is still pursuing its "
    "stated objective within its constraints. Judge only the evidence given; do not "
    "speculate. Repetition without progress, ignoring acceptance criteria, or "
    "violating a stated constraint indicate drift."
)

_SCHEMA_INSTRUCTION = (
    "Reply with ONLY a JSON object, no prose, with exactly these keys: "
    '{"verdict": "on_track"|"drifting"|"off_track", "evidence": [string, ...], '
    '"recommended_intervention": "steer"|"pause_escalate"|null, "rationale": string}'
)

_MAX_EXCERPT_CHARS = 4000


class _Rng(Protocol):
    def random(self) -> float: ...


@dataclass
class JudgeVerdict:
    verdict: str
    evidence: list[str]
    recommended_intervention: str | None
    rationale: str
    model: str


def should_judge(
    *,
    policy: m.SupervisionPolicy,
    tier1_verdict: str | None,
    tier0_alert: bool,
    rng: _Rng | None = None,
) -> bool:
    """Sampled per policy.sampling_rate; ALWAYS on a tier-0 or tier-1 alert (§8.6.2)."""
    if tier0_alert or tier1_verdict in ("drifting", "off_track"):
        return True
    rate = float(policy.sampling_rate or 0.0)
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    return (rng or random).random() < rate


def build_judge_messages(
    *,
    anchor: m.TaskAnchor | None,
    state_summary: str,
    trajectory_excerpt: str,
    signals: dict[str, Any],
    rubric: str,
) -> list[NeutralMessage]:
    objective = getattr(anchor, "objective", "") or "(no recorded objective)"
    acceptance = getattr(anchor, "acceptance_criteria", None) or []
    constraints = getattr(anchor, "constraints", None) or []
    excerpt = trajectory_excerpt[:_MAX_EXCERPT_CHARS]
    user = (
        f"OBJECTIVE:\n{objective}\n\n"
        f"ACCEPTANCE CRITERIA:\n{json.dumps(acceptance)}\n\n"
        f"CONSTRAINTS:\n{json.dumps(constraints)}\n\n"
        f"STATE SUMMARY:\n{state_summary}\n\n"
        f"RECENT TRAJECTORY:\n{excerpt}\n\n"
        f"DRIFT SIGNALS:\n{json.dumps(signals, default=str)}"
    )
    return [
        NeutralMessage(role="system", content=f"{rubric}\n\n{_SCHEMA_INSTRUCTION}"),
        NeutralMessage(role="user", content=user),
    ]


def parse_verdict(text: str, *, model: str) -> JudgeVerdict | None:
    """Strictly parse the judge's JSON. Anything unexpected -> None (never raise)."""
    try:
        raw = json.loads(text.strip())
    except (ValueError, AttributeError):
        return None
    if not isinstance(raw, dict):
        return None
    verdict = raw.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return None
    rec = raw.get("recommended_intervention")
    if rec is not None and not isinstance(rec, str):
        rec = None
    evidence_raw = raw.get("evidence") or []
    evidence = [str(e) for e in evidence_raw] if isinstance(evidence_raw, list) else []
    return JudgeVerdict(
        verdict=str(verdict),
        evidence=evidence,
        recommended_intervention=rec,
        rationale=str(raw.get("rationale") or ""),
        model=model,
    )


async def _resolve_judge_model(db: AsyncSession, policy: m.SupervisionPolicy) -> tuple[str, str]:
    """policy.judge_model_config_id -> ModelConfig; else the configured defaults."""
    settings = get_settings()
    if policy.judge_model_config_id is not None:
        cfg = await db.get(m.ModelConfig, policy.judge_model_config_id)
        if cfg is not None:
            provider, model = str(cfg.provider), str(cfg.model)
        else:
            provider, model = settings.default_model_provider, settings.default_model
    else:
        provider, model = settings.default_model_provider, settings.default_model
    # Strict BYOK mode: the judge doesn't go through complete_with_fallback (no BYOK
    # key), so a cloud judge model would raise TenantKeyRequired -- it's
    # platform-internal quality control, not tenant-attributed work, so pin it to
    # local instead. Full judge-BYOK is a tracked follow-up.
    if settings.require_tenant_model_key and locality_for_provider(provider) == "cloud":
        return "ollama", settings.default_model
    return provider, model


async def judge_checkpoint(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    policy: m.SupervisionPolicy,
    anchor: m.TaskAnchor | None,
    checkpoint: m.AgentCheckpoint,
    trajectory_excerpt: str,
    signals: dict[str, Any],
    contains_restricted: bool,
    supervisor_agent_id: uuid.UUID | None,
) -> JudgeVerdict | None:
    """Run the tier-2 judge. Returns None on ANY failure -- never raises."""
    try:
        if policy.department_id is not None:
            budget = await check_budget(db, tenant_id=tenant_id, department_id=policy.department_id)
            if budget.hard_exceeded:
                return None  # skip judging; never hard-stop the run from here

        provider, model = await _resolve_judge_model(db, policy)
        rubric = str((policy.judge_rubric or {}).get("text") or DEFAULT_RUBRIC)
        messages = build_judge_messages(
            anchor=anchor,
            state_summary=checkpoint.state_summary,
            trajectory_excerpt=trajectory_excerpt,
            signals=signals,
            rubric=rubric,
        )
        request_id = uuid.uuid4()
        result = await get_model_router().complete(
            CompletionRequest(
                provider=provider,
                model=model,
                messages=messages,
                tools=[],
                params=ModelParams(temperature=0.0, max_tokens=512),
                tenant_id=tenant_id,
                agent_id=supervisor_agent_id,
                request_id=request_id,
                contains_restricted=contains_restricted,
            )
        )
        await record_usage(
            db,
            tenant_id=tenant_id,
            request_id=request_id,
            model=result.model,
            provider=result.provider,
            tokens_in=result.usage.tokens_in,
            tokens_out=result.usage.tokens_out,
            agent_id=supervisor_agent_id,
            department_id=policy.department_id,
        )
        return parse_verdict(result.text, model=f"{result.provider}/{result.model}")
    except Exception:
        # Supervision must never break the supervised run (loop_hook contract).
        return None
