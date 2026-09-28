"""Automatic model selection by task complexity (§ plan: Auto LLM routing).

An Auto ``ModelConfig`` (``provider="auto"``) is a virtual catalog entry that
resolves to a concrete tenant model before each completion. Routing is a
policy layer on top of the existing ModelRouter — never a foreign gateway.

Tier order: ``fast`` → ``balanced`` → ``strong``. Escalation is one-way and
latched on the agent run so tool loops stay on the same model.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event

logger = logging.getLogger(__name__)

AUTO_PROVIDER = "auto"
AUTO_MODEL = "router"
TIER_ORDER: tuple[str, ...] = ("fast", "balanced", "strong")

_COMPLEX_KEYWORDS = re.compile(
    r"\b("
    r"analys[ee]r?|analysis|architect|debug|refactor|migrate|prove|reason|"
    r"step[- ]by[- ]step|complex|multi[- ]?step|compare|evaluate|design|"
    r"implement|algorithm|security|compliance|legal|audit"
    r")\b",
    re.IGNORECASE,
)
_UNCERTAIN = re.compile(
    r"\b(i'?m not sure|cannot determine|need more (info|context)|"
    r"unsure|unclear|as an ai)\b",
    re.IGNORECASE,
)

_CASCADE_CHECK_SYSTEM = (
    "You are a strict answer-quality checker for an agent platform. "
    "Given USER (the task) and DRAFT (the model's answer), decide whether the "
    "draft adequately completes the task. Reply with ONLY a JSON object, no "
    'prose: {"adequate": true|false, "reason": "<short>"}. '
    "adequate=false when the draft is incomplete, evasive, contradictory, "
    "obviously wrong, or refuses without a hard constraint."
)
_MAX_CASCADE_EXCERPT = 3000


class ComplexityTier(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    STRONG = "strong"


@dataclass
class RoutingDecision:
    """What the auto-router decided (and optionally would have decided)."""

    tier: str
    config_id: uuid.UUID
    reason: str
    signals: dict[str, Any] = field(default_factory=dict)
    shadow_would_tier: str | None = None
    escalated: bool = False
    preference_score: float | None = None


class AutoRouterError(Exception):
    """Misconfigured Auto ModelConfig or missing tier targets."""


def is_auto_config(cfg: m.ModelConfig | None) -> bool:
    return cfg is not None and (cfg.provider or "").lower() == AUTO_PROVIDER


def auto_params(cfg: m.ModelConfig) -> dict[str, Any]:
    return dict(cfg.params or {})


def cascade_verify_enabled(cfg: m.ModelConfig) -> bool:
    return bool(auto_params(cfg).get("cascade_verify"))


def tier_map(cfg: m.ModelConfig) -> dict[str, uuid.UUID]:
    raw = auto_params(cfg).get("tiers") or {}
    if not isinstance(raw, dict):
        raise AutoRouterError("auto ModelConfig.params.tiers must be an object")
    out: dict[str, uuid.UUID] = {}
    for name, value in raw.items():
        key = str(name).strip().lower()
        if key not in TIER_ORDER:
            continue
        try:
            out[key] = uuid.UUID(str(value))
        except ValueError as exc:
            raise AutoRouterError(f"tier {key!r} is not a valid model config id") from exc
    if not out:
        raise AutoRouterError(
            "auto ModelConfig needs at least one of tiers.fast|balanced|strong"
        )
    return out


def _run_state(run: m.AgentRun | None) -> dict[str, Any]:
    if run is None:
        return {}
    ctx = run.context or {}
    state = ctx.get("auto_router")
    return dict(state) if isinstance(state, dict) else {}


def _store_run_state(run: m.AgentRun | None, state: dict[str, Any]) -> None:
    if run is None:
        return
    ctx = dict(run.context or {})
    ctx["auto_router"] = state
    run.context = ctx


_AFFINITY_KEY = "auto_router_affinity"


def agent_affinity(agent: m.Agent | None) -> dict[str, Any]:
    """Cross-run preferred tier for an agent (same work → same model)."""
    if agent is None:
        return {}
    raw = (agent.definition or {}).get(_AFFINITY_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def store_agent_affinity(
    agent: m.Agent | None,
    *,
    tier: str,
    config_id: uuid.UUID,
) -> None:
    """Remember the tier that worked for this agent for the next run."""
    if agent is None or tier not in TIER_ORDER:
        return
    definition = dict(agent.definition or {})
    previous = agent_affinity(agent)
    # Only raise or confirm — never silently drop to a weaker remembered tier
    # from a fluke cheap success after the agent already needed strong.
    if previous.get("tier") in TIER_ORDER:
        if TIER_ORDER.index(tier) < TIER_ORDER.index(str(previous["tier"])):
            return
    definition[_AFFINITY_KEY] = {"tier": tier, "config_id": str(config_id)}
    agent.definition = definition


def remember_agent_route(
    agent: m.Agent | None,
    auto_cfg: m.ModelConfig,
    *,
    run: m.AgentRun | None,
    concrete: m.ModelConfig | None,
) -> None:
    """Persist affinity from run latch or the concrete model just used."""
    if agent is None or not is_auto_config(auto_cfg):
        return
    state = _run_state(run)
    tier = str(state.get("tier") or "")
    raw_id = state.get("config_id")
    if tier in TIER_ORDER and raw_id:
        try:
            store_agent_affinity(agent, tier=tier, config_id=uuid.UUID(str(raw_id)))
            return
        except ValueError:
            pass
    if concrete is None:
        return
    try:
        inverse = {str(cfg_id): name for name, cfg_id in tier_map(auto_cfg).items()}
    except AutoRouterError:
        return
    name = inverse.get(str(concrete.id))
    if name:
        store_agent_affinity(agent, tier=name, config_id=concrete.id)


def estimate_complexity(
    *,
    messages: list[Any] | None = None,
    tools: list[Any] | None = None,
    needs_vision: bool = False,
) -> ComplexityTier:
    """Heuristic complexity class — no ML, sub-millisecond."""
    text_parts: list[str] = []
    for msg in messages or []:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
                elif isinstance(part, str):
                    text_parts.append(part)
    blob = "\n".join(text_parts)
    chars = len(blob)
    n_msgs = len(messages or [])
    n_tools = len(tools or [])

    score = 0
    if n_tools:
        score += 2
    if needs_vision:
        score += 2
    if chars > 12_000 or n_msgs > 24:
        score += 3
    elif chars > 4_000 or n_msgs > 10:
        score += 2
    elif chars > 800:
        score += 1
    if _COMPLEX_KEYWORDS.search(blob):
        score += 2
    if "```" in blob:
        score += 1

    if score >= 5:
        return ComplexityTier.STRONG
    if score >= 2:
        return ComplexityTier.BALANCED
    return ComplexityTier.FAST


def _capability_ok(
    cfg: m.ModelConfig,
    *,
    needs_vision: bool,
    approx_tokens: int,
    require_local: bool,
) -> bool:
    if require_local and cfg.locality != "local":
        return False
    params = cfg.params or {}
    if needs_vision and not bool(params.get("supports_vision")):
        return False
    window = params.get("context_window")
    if isinstance(window, int) and window > 0 and approx_tokens > window:
        return False
    return True


def _approx_tokens(messages: list[Any] | None) -> int:
    total = 0
    for msg in messages or []:
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            total += max(1, len(content) // 4)
    return total


def _pick_tier_name(
    available: dict[str, m.ModelConfig],
    desired: ComplexityTier,
) -> str:
    """Nearest available tier at or above desired; else best below."""
    start = TIER_ORDER.index(desired.value)
    for name in TIER_ORDER[start:]:
        if name in available:
            return name
    for name in reversed(TIER_ORDER[:start]):
        if name in available:
            return name
    return next(iter(available))


def preference_score(
    *,
    messages: list[Any] | None,
    examples: list[dict[str, Any]] | None,
) -> float | None:
    """Lightweight preference signal in [0, 1] — higher means prefer strong.

    Overlap against labeled examples
    ``{"text": "...", "needs_strong": true}``. Learned cascade/task labels use
    the same shape. Returns None when unused.
    """
    if not examples:
        return None
    blob = " ".join(
        getattr(msg, "content", "")
        if isinstance(getattr(msg, "content", None), str)
        else ""
        for msg in (messages or [])
    ).lower()
    if not blob.strip():
        return 0.0
    tokens = set(re.findall(r"[a-z0-9]{3,}", blob))
    if not tokens:
        return 0.0
    strong_hits = 0.0
    weak_hits = 0.0
    for ex in examples:
        if not isinstance(ex, dict):
            continue
        text = str(ex.get("text") or "").lower()
        ex_tokens = set(re.findall(r"[a-z0-9]{3,}", text))
        if not ex_tokens:
            continue
        overlap = len(tokens & ex_tokens) / len(ex_tokens)
        if overlap < 0.15:
            continue
        # Cascade-labeled examples are stronger evidence than static seeds.
        weight = 1.5 if str(ex.get("source") or "").startswith("cascade") else 1.0
        if ex.get("needs_strong"):
            strong_hits += overlap * weight
        else:
            weak_hits += overlap * weight
    total = strong_hits + weak_hits
    if total <= 0:
        return None
    return strong_hits / total


_MAX_LEARNED_EXAMPLES = 200
_MIN_LABEL_CHARS = 24


def preference_examples_for(cfg: m.ModelConfig) -> list[dict[str, Any]]:
    """Static seeds + learned labels from cascade / task outcomes."""
    params = auto_params(cfg)
    out: list[dict[str, Any]] = []
    static = params.get("preference_examples")
    if isinstance(static, list):
        out.extend(ex for ex in static if isinstance(ex, dict))
    learned = params.get("preference_learned")
    if isinstance(learned, list):
        out.extend(ex for ex in learned if isinstance(ex, dict))
    return out


def _prompt_excerpt_for_label(messages: list[Any] | None) -> str:
    for msg in reversed(messages or []):
        if getattr(msg, "role", None) != "user":
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()[:800]
    return ""


def record_preference_label(
    auto_cfg: m.ModelConfig,
    *,
    messages: list[Any] | None,
    needs_strong: bool,
    source: str,
) -> bool:
    """Append a labeled preference example onto the Auto ModelConfig.

    Ring-buffer capped at ``_MAX_LEARNED_EXAMPLES``. No-op when preference
    router is disabled or the prompt excerpt is too short. Returns True when
    a label was stored.
    """
    if not is_auto_config(auto_cfg):
        return False
    params = auto_params(auto_cfg)
    if not bool(params.get("preference_router")):
        return False
    text = _prompt_excerpt_for_label(messages)
    if len(text) < _MIN_LABEL_CHARS:
        return False
    learned = [ex for ex in (params.get("preference_learned") or []) if isinstance(ex, dict)]
    # Skip near-duplicates of the newest label.
    if learned:
        last = str(learned[-1].get("text") or "")
        if last == text and bool(learned[-1].get("needs_strong")) == needs_strong:
            return False
    learned.append(
        {
            "text": text,
            "needs_strong": bool(needs_strong),
            "source": source,
        }
    )
    params["preference_learned"] = learned[-_MAX_LEARNED_EXAMPLES:]
    auto_cfg.params = params
    return True


async def _tier_connected(
    db: AsyncSession,
    cfg: m.ModelConfig,
    *,
    tenant_id: uuid.UUID,
) -> bool:
    """True when this tier can actually be called (credential / local / OAuth).

    Mirrors what the Models providers list means by ``available``, plus an
    explicit skip for ModelConfigs already marked ``health.status=error``.
    """
    health = cfg.health or {}
    if health.get("status") == "error":
        return False

    from oc8.config import get_settings
    from oc8.modelrouter.keys import resolve_model_key
    from oc8.modelrouter.registry import resolve_provider

    entry = await resolve_provider(db, tenant_id=tenant_id, name=cfg.provider)
    if entry is None:
        return False
    # Local runtimes (Ollama) need no tenant key.
    if entry.locality == "local":
        return True

    key = await resolve_model_key(
        db,
        tenant_id=tenant_id,
        provider=entry.canonical,
        credential_id=cfg.credential_id,
    )
    # ChatGPT subscription is always "offered" in the providers list, but
    # without a live OAuth token there is nothing to call.
    if entry.canonical == "openai_chatgpt":
        return key is not None
    return bool(entry.available(get_settings(), key))


async def _load_capable(
    db: AsyncSession,
    tiers: dict[str, uuid.UUID],
    *,
    tenant_id: uuid.UUID,
    needs_vision: bool,
    approx_tokens: int,
    require_local: bool,
) -> dict[str, m.ModelConfig]:
    capable: dict[str, m.ModelConfig] = {}
    for name, cfg_id in tiers.items():
        cfg = await db.get(m.ModelConfig, cfg_id)
        if cfg is None:
            logger.warning("auto router tier %s points at missing model %s", name, cfg_id)
            continue
        if is_auto_config(cfg):
            logger.warning("auto router tier %s must not point at another auto config", name)
            continue
        if not _capability_ok(
            cfg,
            needs_vision=needs_vision,
            approx_tokens=approx_tokens,
            require_local=require_local,
        ):
            continue
        if not await _tier_connected(db, cfg, tenant_id=tenant_id):
            logger.info(
                "auto router skipping tier %s (%s:%s): not connected or unhealthy",
                name,
                cfg.provider,
                cfg.model,
            )
            continue
        capable[name] = cfg
    return capable


async def resolve_auto_config(
    db: AsyncSession,
    auto_cfg: m.ModelConfig,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    run: m.AgentRun | None = None,
    agent: m.Agent | None = None,
    messages: list[Any] | None = None,
    tools: list[Any] | None = None,
    needs_vision: bool = False,
    contains_restricted: bool = False,
    force_escalate: bool = False,
    escalate_reason: str | None = None,
) -> tuple[m.ModelConfig, RoutingDecision]:
    """Resolve an Auto ModelConfig to a concrete one.

    Honours run latch (session affinity) and cross-run agent affinity floor.
    Escalation is one-way.
    """
    if not is_auto_config(auto_cfg):
        raise AutoRouterError("resolve_auto_config called with a non-auto ModelConfig")

    params = auto_params(auto_cfg)
    tiers = tier_map(auto_cfg)
    shadow_only = bool(params.get("shadow_only"))
    use_preference = bool(params.get("preference_router"))
    examples = preference_examples_for(auto_cfg) if use_preference else None
    if examples is not None and not examples:
        examples = None

    state = _run_state(run)
    latched_tier = str(state.get("tier") or "")
    already_escalated = bool(state.get("escalated"))
    affinity = agent_affinity(agent)
    affinity_tier = str(affinity.get("tier") or "")

    require_local = contains_restricted
    capable = await _load_capable(
        db,
        tiers,
        tenant_id=tenant_id,
        needs_vision=needs_vision,
        approx_tokens=_approx_tokens(messages),
        require_local=require_local,
    )
    if not capable:
        capable = await _load_capable(
            db,
            tiers,
            tenant_id=tenant_id,
            needs_vision=needs_vision,
            approx_tokens=_approx_tokens(messages),
            require_local=False,
        )
    if not capable:
        raise AutoRouterError(
            "no connected models on auto tiers "
            "(configure credentials or fix unhealthy tier targets)"
        )

    desired = estimate_complexity(
        messages=messages, tools=tools, needs_vision=needs_vision
    )
    pref = preference_score(messages=messages, examples=examples) if use_preference else None
    if pref is not None and pref >= 0.55:
        desired = ComplexityTier.STRONG
    elif pref is not None and pref >= 0.35 and desired == ComplexityTier.FAST:
        desired = ComplexityTier.BALANCED

    natural_tier = _pick_tier_name(capable, desired)
    chosen_tier = natural_tier
    escalated = False
    reason = f"complexity={desired.value}"

    # Cross-run agent floor: same agent, same kind of work — skip rediscovery.
    if (
        not force_escalate
        and affinity_tier in capable
        and TIER_ORDER.index(affinity_tier) > TIER_ORDER.index(chosen_tier)
    ):
        chosen_tier = affinity_tier
        reason = "agent_affinity"

    if latched_tier in capable:
        if TIER_ORDER.index(latched_tier) >= TIER_ORDER.index(chosen_tier):
            chosen_tier = latched_tier
            reason = "session_affinity"
        elif TIER_ORDER.index(natural_tier) > TIER_ORDER.index(chosen_tier):
            chosen_tier = natural_tier
            reason = f"complexity_upgrade={desired.value}"

    if already_escalated and latched_tier in capable:
        chosen_tier = latched_tier
        escalated = True
        reason = "escalation_latch"

    if force_escalate:
        start = TIER_ORDER.index(chosen_tier) if chosen_tier in TIER_ORDER else 0
        higher = [n for n in TIER_ORDER[start + 1 :] if n in capable]
        if higher:
            chosen_tier = higher[0]
            escalated = True
            reason = escalate_reason or "escalated"
        else:
            escalated = True
            reason = escalate_reason or "escalated_at_ceiling"

    if require_local and capable[chosen_tier].locality != "local":
        local_tiers = [n for n, c in capable.items() if c.locality == "local"]
        if local_tiers:
            chosen_tier = max(local_tiers, key=lambda n: TIER_ORDER.index(n))
            reason = "restricted_requires_local"
            escalated = True

    concrete = capable[chosen_tier]
    decision = RoutingDecision(
        tier=chosen_tier,
        config_id=concrete.id,
        reason=reason,
        signals={
            "desired": desired.value,
            "n_messages": len(messages or []),
            "n_tools": len(tools or []),
            "needs_vision": needs_vision,
            "contains_restricted": contains_restricted,
            "shadow_only": shadow_only,
            "agent_affinity": affinity_tier or None,
        },
        shadow_would_tier=natural_tier if shadow_only else None,
        escalated=escalated or already_escalated,
        preference_score=pref,
    )

    if shadow_only:
        enforce_tier = (
            latched_tier
            if latched_tier in capable
            else ("balanced" if "balanced" in capable else natural_tier)
        )
        if enforce_tier not in capable:
            enforce_tier = chosen_tier
        concrete = capable[enforce_tier]
        decision = RoutingDecision(
            tier=enforce_tier,
            config_id=concrete.id,
            reason="shadow_only",
            signals=decision.signals,
            shadow_would_tier=natural_tier,
            escalated=False,
            preference_score=pref,
        )

    _store_run_state(
        run,
        {
            "tier": decision.tier,
            "config_id": str(decision.config_id),
            "escalated": bool(decision.escalated),
            "last_reason": decision.reason,
        },
    )
    await _log_decision(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        auto_cfg=auto_cfg,
        concrete=concrete,
        decision=decision,
    )
    return concrete, decision


async def escalate_auto_router(
    db: AsyncSession,
    auto_cfg: m.ModelConfig,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    run: m.AgentRun | None,
    reason: str,
    agent: m.Agent | None = None,
    messages: list[Any] | None = None,
    tools: list[Any] | None = None,
    needs_vision: bool = False,
    contains_restricted: bool = False,
) -> tuple[m.ModelConfig, RoutingDecision] | None:
    """Bump one tier and latch. Returns None if already at the top capable tier."""
    if not is_auto_config(auto_cfg):
        return None
    before = str(_run_state(run).get("tier") or "")
    concrete, decision = await resolve_auto_config(
        db,
        auto_cfg,
        tenant_id=tenant_id,
        agent_id=agent_id,
        run=run,
        agent=agent,
        messages=messages,
        tools=tools,
        needs_vision=needs_vision,
        contains_restricted=contains_restricted,
        force_escalate=True,
        escalate_reason=reason,
    )
    if decision.tier == before:
        return None
    store_agent_affinity(agent, tier=decision.tier, config_id=decision.config_id)
    return concrete, decision


def answer_looks_uncertain(text: str) -> bool:
    """Cheap cascade signal: empty/short or hedging language."""
    stripped = (text or "").strip()
    if len(stripped) < 40:
        return True
    return bool(_UNCERTAIN.search(stripped))


@dataclass
class CascadeVerdict:
    """Whether cascade_verify should bump the tier."""

    escalate: bool
    reason: str
    via: str  # heuristic | self_check | check_failed | skipped


def _last_user_text(messages: list[Any] | None) -> str:
    for msg in reversed(messages or []):
        if getattr(msg, "role", None) != "user":
            continue
        content = getattr(msg, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _parse_adequate(text: str) -> tuple[bool, str] | None:
    try:
        raw = json.loads((text or "").strip())
    except (ValueError, AttributeError):
        # Models sometimes wrap JSON in fences or prose — take the first {...}.
        match = re.search(r"\{[^{}]*\}", text or "", re.DOTALL)
        if not match:
            return None
        try:
            raw = json.loads(match.group(0))
        except ValueError:
            return None
    if not isinstance(raw, dict) or "adequate" not in raw:
        return None
    return bool(raw.get("adequate")), str(raw.get("reason") or "")


async def cascade_should_escalate(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    answer: str,
    messages: list[Any] | None,
    verifier_config: m.ModelConfig,
    contains_restricted: bool = False,
) -> CascadeVerdict:
    """Decide whether a final cheap answer should escalate (FrugalGPT-style).

    1. Heuristic gate (short / hedging) → escalate immediately.
    2. Otherwise a tiny self-check completion on the same (cheap) model.
    Failures degrade to "do not escalate" so cascade never breaks the run.
    """
    if answer_looks_uncertain(answer):
        return CascadeVerdict(True, "heuristic_uncertain", "heuristic")

    question = _last_user_text(messages)
    if not question:
        return CascadeVerdict(False, "no_user_question", "skipped")

    try:
        from oc8.modelrouter import get_model_router
        from oc8.modelrouter.keys import resolve_model_base_url, resolve_model_key
        from oc8.modelrouter.types import CompletionRequest, ModelParams, NeutralMessage

        api_key = await resolve_model_key(
            db,
            tenant_id=tenant_id,
            provider=verifier_config.provider,
            credential_id=verifier_config.credential_id,
        )
        base_url = (verifier_config.params or {}).get("base_url") or await resolve_model_base_url(
            db,
            tenant_id=tenant_id,
            provider=verifier_config.provider,
            credential_id=verifier_config.credential_id,
        )
        user_blob = (
            f"USER:\n{question[:_MAX_CASCADE_EXCERPT]}\n\n"
            f"DRAFT:\n{(answer or '')[:_MAX_CASCADE_EXCERPT]}"
        )
        result = await get_model_router().complete(
            CompletionRequest(
                provider=verifier_config.provider,
                model=verifier_config.model,
                messages=[
                    NeutralMessage(role="system", content=_CASCADE_CHECK_SYSTEM),
                    NeutralMessage(role="user", content=user_blob),
                ],
                tools=[],
                params=ModelParams(temperature=0.0, max_tokens=128),
                tenant_id=tenant_id,
                agent_id=agent_id,
                request_id=uuid.uuid4(),
                contains_restricted=contains_restricted,
                base_url=base_url,
                api_key=api_key,
            )
        )
        parsed = _parse_adequate(result.text)
        if parsed is None:
            return CascadeVerdict(False, "unparseable_check", "check_failed")
        adequate, reason = parsed
        if adequate:
            return CascadeVerdict(False, reason or "adequate", "self_check")
        return CascadeVerdict(True, reason or "inadequate", "self_check")
    except Exception:
        logger.exception("auto cascade self-check failed; skipping escalate")
        return CascadeVerdict(False, "check_exception", "check_failed")


async def _log_decision(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    auto_cfg: m.ModelConfig,
    concrete: m.ModelConfig,
    decision: RoutingDecision,
) -> None:
    resource: dict[str, Any] = {
        "auto_config_id": str(auto_cfg.id),
        "tier": decision.tier,
        "selected": f"{concrete.provider}:{concrete.model}",
        "selected_config_id": str(concrete.id),
        "reason": decision.reason,
        "signals": decision.signals,
        "escalated": decision.escalated,
    }
    if decision.shadow_would_tier is not None:
        resource["shadow_would_tier"] = decision.shadow_would_tier
    if decision.preference_score is not None:
        resource["preference_score"] = decision.preference_score
    try:
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="agent" if agent_id else "system",
            actor_id=agent_id,
            category="model_router",
            action="auto_route",
            resource=resource,
            decision="route",
        )
    except Exception:
        logger.exception("failed to audit auto_route decision")
