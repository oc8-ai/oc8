"""RBAC gate for agent memory reads/writes across the three §10 tiers.

Mirrors oc8.authz.pdp's frame∩narrowing algebra (§5.3), applied to the
`memory` key of the frame/narrowing JSON shape: {"department": [...],
"company": [...]}. Tier "agent" is always fully open — it's private
memory gated by ownership, not RBAC, and never appears under `memory`.
"""

from __future__ import annotations

from typing import Any

from oc8.authz.pdp import Decision, Effect

_TIERS = ("agent", "department", "company")


def _memory_rights(scope: dict[str, Any] | None, tier: str) -> set[str]:
    if not scope:
        return set()
    raw = (scope.get("memory") or {}).get(tier)
    if not raw:
        return set()
    return set(raw)


def _effective_rights(frame: dict[str, Any], narrowing: dict[str, Any], tier: str) -> set[str]:
    if tier == "agent":
        return {"read", "write"}
    frame_rights = _memory_rights(frame, tier)
    narrow_memory = narrowing.get("memory") if narrowing else None
    if not narrow_memory or tier not in narrow_memory:
        return frame_rights
    return frame_rights & _memory_rights(narrowing, tier)


def authorize_memory_read(frame: dict[str, Any], narrowing: dict[str, Any], tier: str) -> bool:
    """Read is a plain RBAC check for every tier."""
    if tier not in _TIERS:
        return False
    return "read" in _effective_rights(frame, narrowing, tier)


def authorize_memory_write(
    frame: dict[str, Any],
    narrowing: dict[str, Any],
    tier: str,
    *,
    personal_only: bool = False,
) -> Decision:
    """Agent/department writes are RBAC-gated; company writes always require
    human approval (§10.1) — that gate is not something a frame grant can
    bypass or a missing grant can escape.

    `personal_only` is the Copilot: it keeps personal notes (tier 'agent')
    and nothing shared, so any other tier is denied before the company
    approval path can park a run on a write that cannot happen (§7a.5)."""
    if personal_only and tier != "agent":
        return Decision(Effect.DENY, "the Copilot keeps personal notes only (tier 'agent')")
    if tier == "company":
        return Decision(Effect.REQUIRE_APPROVAL, "company memory writes always require approval")
    if tier not in ("agent", "department"):
        return Decision(Effect.DENY, f"unknown tier '{tier}'")
    if "write" in _effective_rights(frame, narrowing, tier):
        return Decision(Effect.ALLOW)
    return Decision(Effect.DENY, f"write not granted for tier '{tier}'")
