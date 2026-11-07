"""Policy Decision Point.

Pure functions over the department frame (the ceiling) and the agent narrowing
(per-agent tightening). The algebra is fixed (tech-spec §5.3):

    effective = department_frame ∩ agent_narrowing

For a tool key the frame grants, an agent narrowing may only *remove*
tools/rights or *lower* an approval threshold — never widen. `narrowing_within_frame`
enforces that at write time; `authorize_tool` enforces it again at decision
time (defense in depth).

A tool key the frame does NOT grant is a different case: an agent may still
be given that tool directly (an agent-exclusive grant, invisible to sibling
agents in the same department), since there is no frame policy to widen
beyond -- the narrowing entry simply becomes that tool's whole policy. See
`effective_tool_policies`'s second loop and `narrowing_within_frame`'s
`key not in frame_tools` branch.

Frame / narrowing JSON shape (stored on ``department.frame`` / ``agent.narrowing``):

    {
      "tools": {
        "<tool_key>": {
          "enabled": bool,
          "read": bool, "modify": bool,
          "approval_eur": int | null,  # threshold at or above which modify needs approval
          "approval_actions": ["modify", ...],  # rights and/or tool keys that always need a human
          "conditions": [  # generic "with limits" rules -- see `Condition` below
            {"attribute": "order_value", "datatype": "number", "operator": ">",
             "value": 5000, "then": "require_approval"}
          ]
        }
      },
      "kbs": ["<kb_id>", ...],
      "memory": {"department": ["read","write"], "company": ["read"]}
    }

`conditions` is deliberately generic and tool-agnostic: it does not know about
euros or Odoo. Which `attribute` names are actually evaluable for a given tool
call is a CAPA-declared concern (see `capas/manifest.py`'s
`GuardrailAttribute`), not a PDP one -- this module only ever sees an already
name-keyed `attributes` mapping (`authorize_tool_call`'s `attributes` param)
and a list of conditions to check it against. `approval_eur`/`approval_actions`
remain as they were (a numeric-threshold-only, tool-agnostic-but-narrower
mechanism); they are not being replaced or reinterpreted -- `conditions` is
the generalization new callers should reach for, kept alongside rather than
instead of, so existing saved data keeps meaning exactly what it always did.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

RIGHTS = ("read", "modify")


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class ConditionDatatype(StrEnum):
    """The set of value shapes a `Condition.attribute` may hold. A CAPA's
    `GuardrailAttribute` (capas/manifest.py) declares which datatype a given
    attribute name is, so the UI's operator/value picker can be built from
    that alone -- this PDP module never hardcodes what an attribute means,
    only how to compare a value against a condition once one is authored."""

    NUMBER = "number"
    STRING = "string"
    BOOLEAN = "boolean"
    ENUM = "enum"


class ConditionOperator(StrEnum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="
    NEQ = "!="
    IN = "in"
    NOT_IN = "not_in"


@dataclass(frozen=True)
class Condition:
    """One business rule row for the "with limits" permission state:

        IF <attribute> <operator> <value> THEN <then>

    Deliberately generic -- `attribute` is just a name (e.g. "order_value",
    "recipient_domain", "environment"); this module has no special case for
    any particular one. What attributes exist and what they mean is a CAPA
    concern (`GuardrailAttribute`); what a condition made from them decides is
    this module's concern. `datatype` is carried alongside the raw `value` so
    round-tripping through the UI/API never has to guess a stored value's
    intended type back from raw JSON.
    """

    attribute: str
    datatype: str = ConditionDatatype.NUMBER.value
    operator: str = ConditionOperator.GTE.value
    value: Any = None
    then: Effect = Effect.REQUIRE_APPROVAL

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Condition:
        raw_value = data.get("value")
        return cls(
            attribute=str(data["attribute"]),
            datatype=str(data.get("datatype", ConditionDatatype.NUMBER.value)),
            operator=str(data.get("operator", ConditionOperator.GTE.value)),
            # Lists (the "in"/"not_in" comparison set) become tuples so every
            # `Condition` -- and therefore every `ToolPolicy` -- stays
            # hashable; JSON has no tuple type, so `to_json` converts back.
            value=tuple(raw_value) if isinstance(raw_value, list) else raw_value,
            then=Effect(data.get("then", Effect.REQUIRE_APPROVAL.value)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "attribute": self.attribute,
            "datatype": self.datatype,
            "operator": self.operator,
            "value": list(self.value) if isinstance(self.value, tuple) else self.value,
            "then": self.then.value,
        }


@dataclass(frozen=True)
class ToolPolicy:
    enabled: bool = False
    read: bool = False
    modify: bool = False
    approval_eur: int | None = None
    #: Rights and/or tool keys that always need a human, whatever the call is
    #: worth. `approval_eur` answers "how expensive before someone checks";
    #: this answers "which actions -- or which single tool -- does nobody do
    #: unattended" -- the only question a connection without monetary values
    #: (a helpdesk, a knowledge source) can be asked. A right in this set
    #: (e.g. "send") gates every tool that grants it; a tool key (e.g.
    #: "post_message") gates only that tool, leaving other tools sharing the
    #: same right alone -- the precision `only` gives to surface, applied to
    #: approval.
    approval_actions: frozenset[str] = frozenset()
    #: Generic "with limits" rules, evaluated in order by `evaluate_conditions`
    #: (first match wins). Additive alongside `approval_eur`/`approval_actions`,
    #: not a replacement -- see the module docstring. Kept alongside `only`,
    #: `approval_actions` etc. as a tighten-only field: `effective_tool_policies`
    #: unions a frame's and a narrowing's conditions rather than intersecting,
    #: same as `approval_actions`, because a narrowing can only ever add a
    #: rule, never drop one the frame set.
    conditions: tuple[Condition, ...] = ()
    #: When set, the ONLY tool names this connection may offer. None means all.
    #:
    #: A ceiling has always been about rights; this makes it about surface too,
    #: and for a reason we measured. Connecting a second system took the tool
    #: list from 14 to 35, and the model stopped working: it looped on the same
    #: lookup instead of proceeding. Most of those tools were ones the
    #: department never uses -- a bridge exposes everything its software can do,
    #: which is not the same as everything this department should see.
    #:
    #: It is also the tighter grant. "May write to the issue tracker" and "may
    #: merge pull requests and push commits" were the same permission before.
    only: frozenset[str] | None = None
    # The McpConnection (a Credential-backed login) this tool key's agent has
    # been assigned, or None if unassigned -- agent tool login selection
    # design. Frame-level policies never set this (a department default has
    # no login of its own); only agent-level narrowing does.
    connection_id: str | None = None
    #: The acting member's own approval rules (D3a, member guardrails). Kept
    #: apart from `approval_actions`/`approval_eur`/`conditions` because they
    #: route to a different person: the member, not the department's
    #: approvers. Rights (`enabled`/`read`/`modify`/`only`) need no routing
    #: and are ANDed straight into the fields above. Never read from or
    #: written to JSON (`from_json`/`to_json` leave them alone).
    member_approval_actions: frozenset[str] = frozenset()
    member_approval_eur: int | None = None
    member_conditions: tuple[Condition, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> ToolPolicy:
        if not data:
            return cls()
        raw_only = data.get("only")
        raw_actions = data.get("approval_actions")
        raw_conditions = data.get("conditions")
        return cls(
            enabled=bool(data.get("enabled", False)),
            read=bool(data.get("read", False)),
            modify=bool(data.get("modify", False)),
            approval_eur=data.get("approval_eur"),
            approval_actions=frozenset(str(a) for a in raw_actions) if raw_actions else frozenset(),
            only=frozenset(str(t) for t in raw_only) if raw_only else None,
            connection_id=data.get("connection_id"),
            conditions=tuple(Condition.from_json(c) for c in raw_conditions)
            if raw_conditions
            else (),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "read": self.read,
            "modify": self.modify,
            "approval_eur": self.approval_eur,
            "approval_actions": sorted(self.approval_actions),
            "only": sorted(self.only) if self.only is not None else None,
            "connection_id": self.connection_id,
            "conditions": [c.to_json() for c in self.conditions],
        }

    def has_right(self, action: str) -> bool:
        return bool(getattr(self, action, False))

    def offers(self, tool: str) -> bool:
        """Whether this connection may expose `tool` at all."""
        return self.only is None or tool in self.only


@dataclass(frozen=True)
class Decision:
    effect: Effect
    reason: str = ""
    #: A stable, translatable identifier for WHY this decision was made --
    #: e.g. "condition_matched", "always_requires_approval",
    #: "value_threshold_exceeded" -- alongside `context`, the structured
    #: parameters an i18n template needs to render `reason` as a real
    #: sentence for a human approver ("Lennart may confirm sales orders
    #: autonomously up to €5,000. This order is €12,480.") instead of
    #: showing `reason`'s raw, un-i18n'd English string verbatim (today's
    #: approval-pane behaviour, kept as the fallback for any reason that
    #: predates this field or any DENY path that never set one -- see
    #: `approvals/service.py`'s own docstring on `detail`). None for every
    #: DENY/no-grant path, which this pass leaves as free text only.
    reason_code: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    #: Who must approve a REQUIRE_APPROVAL: "department" (frame or agent
    #: narrowing, the department's approvers) and/or "member" (the acting
    #: member's own guardrail). Empty for ALLOW and DENY.
    approval_from: frozenset[str] = frozenset()

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW


def _tools(frame: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return frame.get("tools", {}) if frame else {}


def effective_tool_policies(
    frame: dict[str, Any],
    narrowing: dict[str, Any],
    member: dict[str, Any] | None = None,
) -> dict[str, ToolPolicy]:
    """§5.3's intersection, with frame and narrowing terms.

        effective = frame ∩ narrowing ∩ member

    The optional `member` term (D3a) is a third, tighten-only level: it can
    switch rights off and add approval rules routed to the member, never
    grant a tool or right the other two withhold.

    The frame and the narrowing intersect: each can only tighten, never widen.
    A tool the frame withholds cannot be granted by the narrowing; a right the
    frame denies cannot be granted by the narrowing. The intersection is taken
    rather than the union, for the same reason `_narrower_surface` gives.
    """
    frame_tools = _tools(frame)
    narrow_tools = _tools(narrowing)
    out: dict[str, ToolPolicy] = {}
    for key, raw in frame_tools.items():
        f = ToolPolicy.from_json(raw)
        n = narrow_tools.get(key)
        no = ToolPolicy.from_json(n) if n is not None else None
        out[key] = ToolPolicy(
            enabled=f.enabled and (no.enabled if no else True),
            read=f.read and (no.read if no else True),
            modify=f.modify and (no.modify if no else True),
            approval_eur=_min_threshold(f.approval_eur, no.approval_eur if no else None),
            approval_actions=f.approval_actions | (no.approval_actions if no else frozenset()),
            only=_narrower_surface(f.only, no.only if no else None),
            # Narrowing-only, unlike every field above: a `connection_id` pin
            # is an AGENT-level login selection (Task 4/6, agent tool login
            # selection design) with no frame-level counterpart to intersect
            # against -- a department default has no login of its own. So
            # this passes through from the narrowing term alone; there is no
            # `f.connection_id` to consider.
            connection_id=no.connection_id if no else None,
            # Union, same reasoning as `approval_actions`: a narrowing may add
            # a stricter rule the frame never set, but cannot drop one the
            # frame did -- so the effective rule set is everything either
            # level named, not just what both agree on.
            conditions=f.conditions + (no.conditions if no else ()),
        )
    # Agent-exclusive grants: a narrowing key with no frame counterpart at
    # all. There is nothing to intersect against, so the narrowing term IS
    # the policy outright. `narrowing_within_frame` is what allows this key to
    # reach here in the first place (its `key not in frame_tools` branch no
    # longer flags it); this loop is where that grant actually takes effect.
    for key, raw in narrow_tools.items():
        if key in frame_tools:
            continue
        exclusive = ToolPolicy.from_json(raw)
        out[key] = ToolPolicy(
            enabled=exclusive.enabled,
            read=exclusive.read,
            modify=exclusive.modify,
            approval_eur=exclusive.approval_eur,
            approval_actions=exclusive.approval_actions,
            only=exclusive.only,
            connection_id=exclusive.connection_id,
            conditions=exclusive.conditions,
        )
    for key, raw in _tools(member or {}).items():
        base = out.get(key)
        if base is None:
            # Tighten-only: a member cannot bring in a tool that department
            # and agent do not grant.
            continue
        mp = ToolPolicy.from_json(raw)
        out[key] = replace(
            base,
            enabled=base.enabled and mp.enabled,
            read=base.read and mp.read,
            modify=base.modify and mp.modify,
            only=_narrower_surface(base.only, mp.only),
            member_approval_actions=mp.approval_actions,
            member_approval_eur=mp.approval_eur,
            member_conditions=mp.conditions,
        )
    return out


def _narrower_surface(
    frame_only: frozenset[str] | None, narrow_only: frozenset[str] | None
) -> frozenset[str] | None:
    """An agent may only ever tighten the surface, never widen it.

    Naming a tool the frame withholds must not grant it -- same rule as every
    other field here, and the reason the intersection is taken rather than the
    union.
    """
    if frame_only is None:
        return narrow_only
    if narrow_only is None:
        return frame_only
    return frame_only & narrow_only


def _min_threshold(frame_eur: int | None, narrow_eur: int | None) -> int | None:
    # A lower threshold is stricter; None means "no threshold" (least strict).
    if frame_eur is None:
        return narrow_eur
    if narrow_eur is None:
        return frame_eur
    return min(frame_eur, narrow_eur)


#: Ordered narrowest-first. A future "company"/org-wide tier slots in
#: between "department" and "capa_default" -- `tool_policy_source` walks an
#: ordered list built from this shape rather than assuming exactly two
#: levels, so adding one is an extra entry in that walk, not a rewrite of it.
PROVENANCE_LEVELS: tuple[str, ...] = ("member", "agent", "department", "capa_default")


def tool_policy_source(
    key: str,
    *,
    frame: dict[str, Any],
    capa_defaults: dict[str, Any] | None,
    narrowing_overridden_keys: Sequence[str] | frozenset[str],
    member_keys: Sequence[str] | frozenset[str] = (),
) -> str:
    """Which level a tool policy key's effective value actually came from --
    the guardrails UX Source column (design: OC8 Guardrails UX spec
    §"Source"). Walks `PROVENANCE_LEVELS` narrowest-first as an ordered list
    of `(level, is_customized_at_this_level)` pairs and returns the first
    level found customized; falls through to the broadest level if none was.

    "Customized" means something different at each level -- an agent's is an
    explicit override flag, a department's is a diff against its own
    captured default -- so each level's own test is spelled out once, in
    `PROVENANCE_LEVELS` order, rather than one generic test applied
    identically at every level. Inserting a `company` level between
    "department" and "capa_default" later means adding one more `(label,
    customized)` pair to this list (plus, if needed, changing what
    "department" diffs against) -- it does not touch how "agent" is decided
    or how callers use this function's return value.

    `capa_defaults=None` (see `Department.frame_capa_defaults`'s own
    docstring) means this department has no captured default at all -- a
    hand-created department, or one that predates that column -- so nothing
    below "department" is ever reachable: every key not agent-overridden
    reports "department".
    """
    levels: list[tuple[str, bool]] = [
        ("member", key in member_keys),
        ("agent", key in narrowing_overridden_keys),
        (
            "department",
            capa_defaults is None or _tools(frame).get(key) != _tools(capa_defaults).get(key),
        ),
    ]
    for label, customized in levels:
        if customized:
            return label
    return "capa_default"


def _condition_matches(condition: Condition, attributes: Mapping[str, Any]) -> bool:
    if condition.attribute not in attributes:
        # An attribute this call never produced (e.g. a condition written for
        # an action that doesn't extract it) cannot match -- fail toward "no
        # rule fired" here; the caller's own DENY-by-default checks already
        # covered whether the call is permitted at all.
        return False
    actual = attributes[condition.attribute]
    expected = condition.value
    try:
        if condition.operator == ConditionOperator.GT:
            return bool(actual > expected)
        if condition.operator == ConditionOperator.GTE:
            return bool(actual >= expected)
        if condition.operator == ConditionOperator.LT:
            return bool(actual < expected)
        if condition.operator == ConditionOperator.LTE:
            return bool(actual <= expected)
        if condition.operator == ConditionOperator.EQ:
            return bool(actual == expected)
        if condition.operator == ConditionOperator.NEQ:
            return bool(actual != expected)
        if condition.operator == ConditionOperator.IN:
            return actual in expected
        if condition.operator == ConditionOperator.NOT_IN:
            return actual not in expected
    except TypeError:
        # Comparing incompatible types (e.g. a string against a number because
        # the attribute's real value drifted from what the condition expects)
        # is a data problem, not a match -- never raise out of a policy check.
        return False
    logger.warning(
        "unknown condition operator %r on attribute %r", condition.operator, condition.attribute
    )
    return False


def evaluate_conditions(
    conditions: Sequence[Condition], attributes: Mapping[str, Any]
) -> Decision | None:
    """The generic "with limits" resolver: first condition whose `attribute`
    is present in `attributes` and whose `operator`/`value` matches wins;
    returns None if nothing matched (falls through to plain ALLOW at the
    caller). Deliberately knows nothing about euros, Odoo, or any other
    domain concept -- see the module docstring and `Condition`."""
    for condition in conditions:
        if _condition_matches(condition, attributes):
            reason = f"condition '{condition.attribute} {condition.operator} {condition.value!r}'"
            return Decision(
                condition.then,
                f"{reason} matched",
                reason_code="condition_matched",
                context={
                    "attribute": condition.attribute,
                    "operator": condition.operator,
                    "threshold": condition.value,
                    "actual": attributes.get(condition.attribute),
                },
            )
    return None


def authorize_tool(
    frame: dict[str, Any],
    narrowing: dict[str, Any],
    *,
    tool_key: str,
    action: str,
    value_eur: float | None = None,
) -> Decision:
    """Authorize a single tool action against the effective policy.

    NOT the runtime's gate -- `authorize_tool_call` below is, reached from
    `agent/engine.py` and `api/mcp_gateway.py`. This one exists because it asks
    a genuinely different question and cannot be reduced to a call to the other
    without changing what either answers:

    - It takes `frame`/`narrowing` directly and resolves them itself via
      `effective_tool_policies`, where `authorize_tool_call` takes an
      already-resolved `policies` mapping (its caller does that resolution
      once per run, not once per call).
    - `tool_key` does double duty here: it is both the policy's lookup key
      AND, for `approval_actions`' tool-name case, the tool identity itself.
      `authorize_tool_call` keeps those separate (`connection_key` vs. `tool`)
      because a connection and the specific MCP tool it exposes are not the
      same string -- an "odoo" connection's `only` list names tools like
      `res_partner_read`, not "odoo". Passing `tool_key` as `tool` here would
      wrongly run it through `policy.offers()`, which this function never
      checks at all.
    - It has no `extra_thresholds`: the skill guardrail and per-agent
      threshold that `authorize_tool_call` folds in have no equivalent input
      here.

    Kept for the semantics it pins down (and the test suite that encodes
    them) rather than deleted; if a caller needs the live runtime path, use
    `authorize_tool_call`.
    """
    if action not in RIGHTS:
        return Decision(Effect.DENY, f"unknown action '{action}'")
    eff = effective_tool_policies(frame, narrowing).get(tool_key)
    if eff is None or not eff.enabled:
        return Decision(Effect.DENY, f"tool '{tool_key}' not enabled for this agent")
    if not eff.has_right(action):
        # Deliberately BEFORE the approval check below: an action the policy
        # does not grant at all is denied, not sent to a human. Listing it in
        # `approval_actions` cannot resurrect it -- there is nothing to approve.
        return Decision(Effect.DENY, f"action '{action}' not permitted on '{tool_key}'")
    if action in eff.approval_actions or tool_key in eff.approval_actions:
        # Wins over the euro threshold below: an operator who asked for a
        # human on every action of this kind (or on this specific tool) has
        # already answered the question the threshold would ask, so the
        # value is never compared. `approval_actions` may name a right
        # (gates every tool granting it) or a tool key (gates only that
        # tool, leaving others that share the right alone).
        reason = (
            f"'{action}' always needs approval on '{tool_key}'"
            if action in eff.approval_actions
            else f"'{tool_key}' always needs approval"
        )
        return Decision(Effect.REQUIRE_APPROVAL, reason)
    if action == "modify" and eff.approval_eur is not None:
        # See authorize_tool_call: an unreadable value counts as zero, so a
        # threshold of €0 means "a human decides every modify".
        effective_value = value_eur if value_eur is not None else 0.0
        if effective_value >= eff.approval_eur:
            return Decision(
                Effect.REQUIRE_APPROVAL,
                f"value €{value_eur:g} meets approval threshold €{eff.approval_eur}"
                if value_eur is not None
                else f"every modify needs approval (threshold €{eff.approval_eur})",
            )
    return Decision(Effect.ALLOW)


_DEFAULT_CLEARED_CLASSES = frozenset({"public", "internal", "confidential"})


def effective_cleared_classes(frame: dict[str, Any]) -> frozenset[str]:
    """The classifications this department's agents may retrieve
    (tech-spec §5.3/§11.5), from frame.cleared_classes. Defaults to
    everything except 'restricted' when unset — no breaking change for
    existing data (KB/chunk default classification is 'internal')."""
    if not frame or "cleared_classes" not in frame:
        return _DEFAULT_CLEARED_CLASSES
    return frozenset(frame["cleared_classes"])


def classification_rule(
    resource_classification: str, cleared_classes: frozenset[str], model_locality: str
) -> Decision:
    """§5.3 step 1 / the §11.5 access-scoping rule, as a reusable pure
    function: DENY if the resource's classification isn't cleared for this
    department; DENY if it's 'restricted' and the model isn't local."""
    if resource_classification not in cleared_classes:
        return Decision(Effect.DENY, f"classification '{resource_classification}' not cleared")
    if resource_classification == "restricted" and model_locality != "local":
        return Decision(Effect.DENY, "restricted classification requires a local model")
    return Decision(Effect.ALLOW)


@dataclass
class SubsetViolation:
    tool_key: str
    reason: str


def member_term_violations(
    base: Mapping[str, ToolPolicy], member: dict[str, Any]
) -> list[SubsetViolation]:
    """Every way `member` would widen department ∩ agent (D3a: tighten only)."""
    out: list[SubsetViolation] = []
    for key, raw in _tools(member).items():
        b = base.get(key)
        if b is None:
            out.append(SubsetViolation(key, "tool not granted by department or agent"))
            continue
        mp = ToolPolicy.from_json(raw)
        if mp.connection_id is not None:
            out.append(SubsetViolation(key, "a member guardrail cannot choose a login"))
        for right in ("enabled", *RIGHTS):
            if getattr(mp, right) and not getattr(b, right):
                out.append(SubsetViolation(key, f"'{right}' not granted by department or agent"))
    return out


def narrowing_within_frame(
    frame: dict[str, Any], narrowing: dict[str, Any]
) -> list[SubsetViolation]:
    """Return violations where the narrowing widens a FRAME tool beyond what
    the frame grants (empty = ok). A key absent from the frame entirely is
    not a violation -- it's an agent-exclusive grant (see module docstring
    and `effective_tool_policies`'s second loop), which has no frame policy
    to widen beyond."""
    frame_tools = _tools(frame)
    violations: list[SubsetViolation] = []
    for key, raw in _tools(narrowing).items():
        if key not in frame_tools:
            continue
        n = ToolPolicy.from_json(raw)
        f = ToolPolicy.from_json(frame_tools.get(key))
        if n.enabled and not f.enabled:
            violations.append(SubsetViolation(key, "tool disabled in department frame"))
        for right in RIGHTS:
            if getattr(n, right) and not getattr(f, right):
                violations.append(SubsetViolation(key, f"'{right}' not granted by frame"))
        if (
            f.approval_eur is not None
            and n.approval_eur is not None
            and n.approval_eur > f.approval_eur
        ):
            violations.append(SubsetViolation(key, "approval threshold higher (looser) than frame"))
        if f.approval_eur is not None and n.approval_eur is None:
            violations.append(SubsetViolation(key, "narrowing removes a frame approval threshold"))
    return violations


@dataclass
class MissingRequirement:
    kind: str  # "tool" | "kb"
    key: str
    reason: str


def missing_skill_requirements(
    frame: dict[str, Any],
    narrowing: dict[str, Any],
    requires: dict[str, Any],
    granted_kb_ids: set[str] | None = None,
) -> list[MissingRequirement]:
    """A skill can be assigned only if requires ⊆ effective(agent) (tech-spec §6.5)."""
    eff = effective_tool_policies(frame, narrowing)
    granted_kb_ids = granted_kb_ids or set()
    missing: list[MissingRequirement] = []
    for req in requires.get("tools", []):
        key = str(req.get("tool") if isinstance(req, dict) else req)
        rights = req.get("rights", ["read"]) if isinstance(req, dict) else ["read"]
        pol = eff.get(key)
        if pol is None or not pol.enabled:
            missing.append(MissingRequirement("tool", key, f"'{key}' not enabled"))
            continue
        for right in rights:
            if not pol.has_right(right):
                missing.append(MissingRequirement("tool", key, f"'{right}' on '{key}' not granted"))
    for req in requires.get("kbs", []):
        key = str(req.get("kb_ref") if isinstance(req, dict) else req)
        if key not in granted_kb_ids:
            missing.append(MissingRequirement("kb", key, f"KB '{key}' not granted"))
    return missing


def required_right(tool_name: str, scopes: Mapping[str, Any] | None) -> str:
    """Classify a tool call as read / modify.

    `scopes` is the connection's classification, e.g.
    `{"read": ["fs_read"], "modify": ["send_email"]}`. Anything unlisted requires
    `modify` -- fail-closed, so an unknown tool is treated as the more dangerous
    case rather than waved through.
    """
    if not scopes:
        return "modify"
    for right in RIGHTS:
        names = scopes.get(right)
        if isinstance(names, (list, tuple)) and tool_name in names:
            return right
    return "modify"


def _approval_rules(
    *,
    right: str,
    tool: str | None,
    connection_key: str,
    value: float | None,
    attributes: Mapping[str, Any] | None,
    approval_actions: frozenset[str],
    conditions: tuple[Condition, ...],
    thresholds: Sequence[float | None],
) -> Decision | None:
    """One level's approval rules; None when nothing asks for a human.

    Moved out of `authorize_tool_call` unchanged so the department's and the
    member's rules are evaluated by the same code (D3a)."""
    if right in approval_actions or (tool is not None and tool in approval_actions):
        # Deliberately AFTER every DENY check in the caller and BEFORE the euro
        # threshold below -- same ordering and the same reasoning as
        # `authorize_tool`. An action or tool this frame does not grant at
        # all was already denied above; there is nothing to send to a human
        # in something that is not permitted. And once we know it IS
        # permitted, `approval_actions` wins over `approval_eur`/
        # `extra_thresholds` without comparing the value at all: an operator
        # who asked for a human on every action of this kind (or on this one
        # tool) has already answered the question a threshold would ask.
        # `approval_actions` may name a right (gates every tool granting it)
        # or a tool name (gates only that tool, leaving others sharing the
        # right alone).
        reason = (
            f"{right!r} always needs approval on {connection_key!r}"
            if right in approval_actions
            else f"{tool!r} always needs approval"
        )
        return Decision(
            Effect.REQUIRE_APPROVAL,
            reason,
            reason_code="always_requires_approval",
            context={"right": right, "tool": tool, "connection": connection_key},
        )

    if conditions:
        condition_decision = evaluate_conditions(conditions, attributes or {})
        if condition_decision is not None:
            return condition_decision

    present = [float(t) for t in thresholds if t is not None]
    if present:
        strictest = min(present)
        # An unreadable value counts as ZERO, not as "exempt". Otherwise every
        # action without a price -- a reply to a customer, a status change --
        # bypasses approval no matter what the frame says, and a threshold of €0
        # ("a human decides every outward action") cannot be expressed at all.
        # A positive threshold is unaffected: 0 is under it, as before.
        if (value if value is not None else 0.0) >= strictest:
            return Decision(
                Effect.REQUIRE_APPROVAL,
                f"value €{value:g} meets threshold €{strictest:g}"
                if value is not None
                else f"every action needs approval (threshold €{strictest:g})",
                reason_code="value_threshold_exceeded",
                context={"threshold": strictest, "actual": value if value is not None else 0.0},
            )
    return None


def authorize_tool_call(
    *,
    policies: Mapping[str, ToolPolicy],
    connection_key: str | None,
    right: str,
    value: float | None,
    extra_thresholds: Sequence[float | None] = (),
    tool: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Decision:
    """Decide a tool call against the department frame (§5.3).

    `extra_thresholds` carries the agent's own `approval_value_eur` and any
    active skill guardrail; the strictest (lowest non-null) of those and the
    policy's own `approval_eur` governs.

    `attributes` is the generic, CAPA-declared-name-keyed sibling of `value`
    (e.g. `{"order_value": 12480, "customer_type": "wholesale"}`), checked
    against `policy.conditions` via `evaluate_conditions`. It is additive
    alongside `value`/`extra_thresholds`, not a replacement for them -- a
    connection with no conditions authored behaves exactly as before.

    `tool` is checked against the frame's surface (`only`) where one is set.
    Withholding a tool from the advertised list is not enough on its own: a
    model that has seen the name once, in an earlier run or a mission, will
    call it anyway, and a grant that only hides is not a grant that holds.

    THE live gate: this is what `agent/engine.py` and `api/mcp_gateway.py`
    call for every real tool call. `authorize_tool` above answers a related
    but distinct question over a raw frame/narrowing pair with a single
    conflated `tool_key`; see the comment on it for why the two do not
    collapse into one function.
    """
    if connection_key is None:
        return Decision(Effect.DENY, "no tool connection bound to this run")
    if right not in RIGHTS:
        return Decision(Effect.DENY, f"unknown right {right!r}")
    policy = policies.get(connection_key)
    if policy is None:
        return Decision(Effect.DENY, f"no grant for {connection_key!r} in this frame")
    if not policy.enabled:
        return Decision(Effect.DENY, f"{connection_key!r} is disabled in this frame")
    if not getattr(policy, right, False):
        return Decision(Effect.DENY, f"{connection_key!r} does not grant {right!r}")
    if tool is not None and not policy.offers(tool):
        return Decision(
            Effect.DENY, f"{connection_key!r} does not grant the tool {tool!r} in this frame"
        )

    department = _approval_rules(
        right=right,
        tool=tool,
        connection_key=connection_key,
        value=value,
        attributes=attributes,
        approval_actions=policy.approval_actions,
        conditions=policy.conditions,
        thresholds=(policy.approval_eur, *extra_thresholds),
    )
    member = _approval_rules(
        right=right,
        tool=tool,
        connection_key=connection_key,
        value=value,
        attributes=attributes,
        approval_actions=policy.member_approval_actions,
        conditions=policy.member_conditions,
        thresholds=(policy.member_approval_eur,),
    )
    for d in (department, member):
        if d is not None and d.effect is Effect.DENY:
            return d
    first = department or member
    if first is None:
        return Decision(Effect.ALLOW)
    return replace(
        first,
        approval_from=frozenset(
            name for name, d in (("department", department), ("member", member)) if d is not None
        ),
    )
