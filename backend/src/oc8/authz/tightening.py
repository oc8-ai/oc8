"""When an admin change only tightens what a member confirmed.

A member's confirmation of a login binding or a trigger keeps holding after an
admin change that only tightens; anything else asks them to confirm again.
These pure functions are the exact, per-field definition of "tightening".
A confirmation snapshot (`grant`) has three parts: `tools` (one grant entry per
enabled tool key, see `grant_tool_entry`), `frame` (the department frame without
`tools`) and `skills` (`{assignment id: [skill_version_id, overrides]}`, in a
department with an owner `[skill_version_id, overrides, name, description]`,
the name and description being the text the model reads).

Each rule: what keeps the confirmation (tightening or equal). Anything else
is a loosening.

- `tools[key]`: absent now, or not enabled now (grants nothing). Present and
  enabled now but absent or not enabled in the confirmed grant: loosens.
- `enabled`, `read`, `modify`: now <= confirmed (True->False, or unchanged).
- `approval_eur` (None = no threshold): confirmed None; or both numbers and now
  <= confirmed. set->None, raised, or not a number: loosens.
- `approval_actions`: now is a superset of confirmed.
- `only` (None = every tool): confirmed None; or both set and now a subset of
  confirmed. set->None or a name added: loosens.
- `connection_id`: equal.
- `frame_conditions`, `agent_conditions` (stored apart; the runtime evaluates
  frame then agent conditions, first match wins): judged on that effective
  list, by severity DENY > REQUIRE_APPROVAL > ALLOW or no match. It tightens
  when no call can get a milder outcome than before (`_conditions_tighten`):
  every confirmed `deny`/`require_approval` is still there with the same or a
  stricter `then`, nothing milder now runs ahead of it unless a condition that
  already ran ahead of it in the confirmed list, and every `allow` now was an
  `allow` before. So appending a `deny`/`require_approval`, removing an
  `allow`, moving a `deny` ahead and allow->require_approval->deny tighten. A
  condition removed or changed, an `allow` added or moved up, a milder `then`,
  and a frame `require_approval` ahead of a confirmed agent `deny` loosen. A
  loosening is reported against the list(s) that changed.
- `limits`: every confirmed (sorted tools, per) window is still there with max
  <= confirmed; extra windows are fine. A changed per or tool set drops the old
  window.
- any other entry field: equal (absent on both sides counts as equal).
- `frame.closed`: unchanged, or not True -> True. True -> not True loosens.
- `frame.kbs` (absent = []): now a subset of confirmed, by canonical JSON.
- `frame.memory` (absent = {}): each scope's rights a subset of the confirmed
  rights for that scope. A right or a scope added loosens.
- `frame.cleared_classes` (absent = the default set of
  `pdp.effective_cleared_classes`): now a subset of confirmed.
- `frame.<any other key>` (`emits`, `intakes`, a stray `tools`, unknown
  keys): equal.
- `skills`: current ids a subset of confirmed ids, each entry equal. Added,
  new version, changed overrides or (where the entry carries them) a changed
  name or description: loosens.
  Removed: tightens (a skill's guardrails apply only once the model
  activates it, so removing one opens nothing).
- any other grant part: equal.

Missing fields read as `ToolPolicy.from_json` reads them, so a stored policy
means here what it means to the runtime. When in doubt the answer is
"loosening": the confirmation is asked for once too often, never skipped.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from oc8.authz.pdp import (
    Condition,
    Effect,
    ToolPolicy,
    effective_cleared_classes,
    merge_limits,
    min_threshold,
    narrower_surface,
)

#: The fields of a grant tool entry, in the order a loosening is reported.
ENTRY_FIELDS: tuple[str, ...] = (
    "enabled",
    "read",
    "modify",
    "approval_eur",
    "approval_actions",
    "only",
    "connection_id",
    "limits",
    "frame_conditions",
    "agent_conditions",
)

#: The parts of a grant snapshot with a rule of their own.
GRANT_PARTS: tuple[str, ...] = ("tools", "frame", "skills")

_MISSING = object()


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _same(a: Any, b: Any) -> bool:
    if a is _MISSING or b is _MISSING:
        return a is b
    return _canon(a) == _canon(b)


# --- one tool ---------------------------------------------------------------


def _parse_entry(entry: Mapping[str, Any]) -> ToolPolicy:
    """The entry's policy fields as the runtime reads them. Conditions are
    read apart (`_conditions`), so they are left out here."""
    body = {k: v for k, v in entry.items() if k in ENTRY_FIELDS and not k.endswith("conditions")}
    return ToolPolicy.from_json(body)


def _conditions(entry: Mapping[str, Any], name: str) -> list[Condition] | None:
    """The entry's condition list, or None when it cannot be read (compared
    raw then, so any change to it loosens)."""
    raw = entry.get(name)
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    try:
        return [Condition.from_json(c) for c in raw]
    except (KeyError, TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _approval_eur_tightens(conf: ToolPolicy, cur: ToolPolicy) -> bool:
    if conf.approval_eur is None:
        return True
    was, now = _number(conf.approval_eur), _number(cur.approval_eur)
    if was is not None and now is not None:
        return now <= was
    return _same(conf.approval_eur, cur.approval_eur)


def _only_tightens(conf: ToolPolicy, cur: ToolPolicy) -> bool:
    if conf.only is None:
        return True
    return cur.only is not None and cur.only <= conf.only


def _limits_tighten(conf: ToolPolicy, cur: ToolPolicy) -> bool:
    now = {(lim.key, lim.per): lim.max for lim in merge_limits(cur.limits)}
    for lim in merge_limits(conf.limits):
        window = (lim.key, lim.per)
        if window not in now or now[window] > lim.max:
            return False
    return True


#: How severe an outcome is: no call may get a milder one after a tightening.
#: A condition's `allow` counts as mild as no match at all.
_SEVERITY: dict[Effect, int] = {Effect.ALLOW: 0, Effect.REQUIRE_APPROVAL: 1, Effect.DENY: 2}


def _predicate(cond: Condition) -> tuple[Any, ...]:
    """What a condition matches, without what it decides."""
    return (cond.attribute, cond.datatype, cond.operator, cond.value)


def _conditions_tighten(confirmed: Sequence[Condition], current: Sequence[Condition]) -> bool:
    """No call gets a milder outcome from `current` than from `confirmed`
    (both first-match lists). Sound, not complete: it reasons on identical
    predicates only, so anything it cannot prove counts as a loosening.

    A call whose first confirmed match is condition i with a `then` t > allow
    still matches i's predicate in `current`; it needs the first current
    condition with that predicate to decide at least t, and every milder
    current condition ahead of it to be one the call cannot match: one whose
    predicate ran ahead of i in `confirmed` (the call did not match it there).
    A call no confirmed condition decided above allow can only get worse, as
    long as every current `allow` was an identical confirmed `allow`."""
    for cond in current:
        if cond.then is Effect.ALLOW and cond not in confirmed:
            return False
    for i, cond in enumerate(confirmed):
        need = _SEVERITY[cond.then]
        if need == 0:
            continue
        earlier = [_predicate(c) for c in confirmed[:i]]
        pred = _predicate(cond)
        at = next((k for k, c in enumerate(current) if _predicate(c) == pred), None)
        if at is None or _SEVERITY[current[at].then] < need:
            return False
        for ahead in current[:at]:
            if _SEVERITY[ahead.then] < need and _predicate(ahead) not in earlier:
                return False
    return True


_CONDITION_FIELDS = ("frame_conditions", "agent_conditions")


def _conditions_loosenings(confirmed: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """The condition lists to report: none when the effective list (frame
    then agent conditions) tightens, else each list that changed."""
    conf = [_conditions(confirmed, name) for name in _CONDITION_FIELDS]
    cur = [_conditions(current, name) for name in _CONDITION_FIELDS]
    if any(c is None for c in conf + cur):
        # Unreadable: only "unchanged" keeps it.
        return [
            name
            for name in _CONDITION_FIELDS
            if not _same(confirmed.get(name, _MISSING), current.get(name, _MISSING))
        ]
    conf_lists = [c or [] for c in conf]
    cur_lists = [c or [] for c in cur]
    if _conditions_tighten(conf_lists[0] + conf_lists[1], cur_lists[0] + cur_lists[1]):
        return []
    return [
        name
        for name, was, now in zip(_CONDITION_FIELDS, conf_lists, cur_lists, strict=True)
        if was != now
    ]


_PolicyRule = Callable[[ToolPolicy, ToolPolicy], bool]

#: One predicate per field (True = tightening or equal).
_POLICY_RULES: dict[str, _PolicyRule] = {
    "enabled": lambda conf, cur: cur.enabled <= conf.enabled,
    "read": lambda conf, cur: cur.read <= conf.read,
    "modify": lambda conf, cur: cur.modify <= conf.modify,
    "approval_eur": _approval_eur_tightens,
    "approval_actions": lambda conf, cur: cur.approval_actions >= conf.approval_actions,
    "only": _only_tightens,
    "connection_id": lambda conf, cur: _same(conf.connection_id, cur.connection_id),
    "limits": _limits_tighten,
}


def policy_loosenings(
    confirmed: Mapping[str, Any] | None, current: Mapping[str, Any] | None
) -> list[str]:
    """The fields in which `current` grants more than `confirmed`, in field
    order (unknown fields last, sorted). Both are grant tool entries
    (`grant_tool_entry`); None is no grant. Empty means tightening or equal."""
    if current is None:
        return []
    try:
        cur = _parse_entry(current)
    except (KeyError, TypeError, ValueError):
        return _raw_differences(confirmed or {}, current)
    if not cur.enabled:
        # A policy that is not enabled grants nothing, whatever else it says.
        return []
    if confirmed is None:
        return ["enabled"]
    try:
        conf = _parse_entry(confirmed)
    except (KeyError, TypeError, ValueError):
        return _raw_differences(confirmed, current)
    if not conf.enabled:
        return ["enabled"]
    loosened = [name for name, rule in _POLICY_RULES.items() if not rule(conf, cur)]
    loosened += _conditions_loosenings(confirmed, current)
    order = {name: i for i, name in enumerate(ENTRY_FIELDS)}
    loosened.sort(key=order.__getitem__)
    unknown = sorted((set(confirmed) | set(current)) - set(ENTRY_FIELDS))
    loosened += [
        key
        for key in unknown
        if not _same(confirmed.get(key, _MISSING), current.get(key, _MISSING))
    ]
    return loosened


def _raw_differences(confirmed: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """An entry that cannot be read: every field that changed loosens."""
    return sorted(
        key
        for key in set(confirmed) | set(current)
        if not _same(confirmed.get(key, _MISSING), current.get(key, _MISSING))
    )


# --- the frame and the skills ----------------------------------------------


def _closed_tightens(confirmed: Any, current: Any) -> bool:
    # Only `True` closes a frame (`pdp.frame_is_closed`).
    return not (confirmed is True and current is not True)


def _subset_of(confirmed: Any, current: Any) -> bool:
    """Both lists: every current item is a confirmed item (canonical JSON).
    Anything else: equal."""
    if isinstance(confirmed, list) and isinstance(current, list):
        return {_canon(i) for i in current} <= {_canon(i) for i in confirmed}
    return _same(confirmed, current)


def _kbs_tighten(confirmed: Any, current: Any) -> bool:
    return _subset_of(
        [] if confirmed is _MISSING else confirmed, [] if current is _MISSING else current
    )


def _memory_tightens(confirmed: Any, current: Any) -> bool:
    conf = {} if confirmed is _MISSING else confirmed
    cur = {} if current is _MISSING else current
    if not isinstance(conf, Mapping) or not isinstance(cur, Mapping):
        return _same(conf, cur)
    return all(_subset_of(conf.get(scope, []), rights) for scope, rights in cur.items())


def _cleared_classes_tighten(confirmed: Any, current: Any) -> bool:
    default = sorted(effective_cleared_classes({}))
    return _subset_of(
        default if confirmed is _MISSING else confirmed,
        default if current is _MISSING else current,
    )


_FrameRule = Callable[[Any, Any], bool]

#: Frame keys with a rule of their own; every other key must stay equal.
_FRAME_RULES: dict[str, _FrameRule] = {
    "closed": lambda conf, cur: _closed_tightens(
        None if conf is _MISSING else conf, None if cur is _MISSING else cur
    ),
    "kbs": _kbs_tighten,
    "memory": _memory_tightens,
    "cleared_classes": _cleared_classes_tighten,
}


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return {}
    return value if isinstance(value, Mapping) else None


def _keys(*parts: Mapping[str, Any]) -> Iterable[str]:
    return sorted({k for p in parts for k in p})


def grant_loosenings(confirmed: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Everything `current` grants beyond `confirmed`, as sorted items
    `tool:<key>`, `frame:<key>`, `skill:<assignment id>` (and `grant:<part>`
    for a part with no rule). Empty means the confirmation still holds."""
    out: list[str] = []
    for part in _keys(confirmed, current):
        if part not in GRANT_PARTS and not _same(
            confirmed.get(part, _MISSING), current.get(part, _MISSING)
        ):
            out.append(f"grant:{part}")
    for part, check in _PART_CHECKS.items():
        conf, cur = _mapping(confirmed.get(part)), _mapping(current.get(part))
        if conf is None or cur is None:
            # Not a mapping: unreadable, so only "unchanged" keeps it.
            if not _same(confirmed.get(part), current.get(part)):
                out.append(f"grant:{part}")
            continue
        out += check(conf, cur)
    return sorted(out)


def _tools_loosenings(conf: Mapping[str, Any], cur: Mapping[str, Any]) -> list[str]:
    return [
        f"tool:{key}" for key in _keys(conf, cur) if policy_loosenings(conf.get(key), cur.get(key))
    ]


def _frame_loosenings(conf: Mapping[str, Any], cur: Mapping[str, Any]) -> list[str]:
    return [
        f"frame:{key}"
        for key in _keys(conf, cur)
        if not _FRAME_RULES.get(key, _same)(conf.get(key, _MISSING), cur.get(key, _MISSING))
    ]


def _skills_loosenings(conf: Mapping[str, Any], cur: Mapping[str, Any]) -> list[str]:
    return [
        f"skill:{key}" for key in _keys(cur) if key not in conf or not _same(conf[key], cur[key])
    ]


_PART_CHECKS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], list[str]]] = {
    "tools": _tools_loosenings,
    "frame": _frame_loosenings,
    "skills": _skills_loosenings,
}


# --- building entries, the meet, the held frame ------------------------------


def _raw_condition_list(policy: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    raw = (policy or {}).get("conditions") or []
    return [Condition.from_json(c).to_json() for c in raw]


def grant_tool_entry(
    frame_policy: Mapping[str, Any] | None,
    narrowing_policy: Mapping[str, Any] | None,
    effective: ToolPolicy,
) -> dict[str, Any]:
    """The grant entry for one tool: the effective policy without its merged
    `conditions`, plus the frame's and the agent's conditions kept apart, so
    a change to either list can be told apart.

    Always store this entry, never a raw `effective.to_json()`: an empty
    surface (`only=frozenset()`) would be stored as `[]` and read back as
    every tool. The policies' conditions must already have been validated
    (`ToolPolicy.from_json(..., strict=True)` on every write path), or this
    raises KeyError/ValueError."""
    entry = effective.to_json()
    del entry["conditions"]
    if effective.only is not None and not effective.only:
        # An empty surface offers no tool, but stored as `[]` it would read
        # back as "every tool" (`ToolPolicy.from_json`): record it as off.
        entry["enabled"] = False
    entry["frame_conditions"] = _raw_condition_list(frame_policy)
    entry["agent_conditions"] = _raw_condition_list(narrowing_policy)
    return entry


def meet_policy(applied: ToolPolicy, requested: ToolPolicy) -> ToolPolicy:
    """The most `applied` and `requested` both allow: never looser than either.

    Conditions are every `deny` of both, then every `require_approval` of both,
    and no `allow` of either: a call that either side refuses is refused, one
    that either side sends to a human goes to a human. (Keeping the applied
    list first would let its milder match pre-empt a requested `deny`.) Limits
    keep the tighter max per window. Two different logins,
    or two surfaces with no tool in common, have no common grant, so their
    meet is not enabled."""
    conditions: list[Condition] = []
    for then in (Effect.DENY, Effect.REQUIRE_APPROVAL):
        for cond in applied.conditions + requested.conditions:
            if cond.then is then and cond not in conditions:
                conditions.append(cond)
    same_login = applied.connection_id == requested.connection_id
    only = narrower_surface(applied.only, requested.only)
    # Disjoint surfaces leave no tool; `only=[]` would read back as every tool.
    no_surface = only is not None and not only
    return ToolPolicy(
        enabled=applied.enabled and requested.enabled and same_login and not no_surface,
        read=applied.read and requested.read,
        modify=applied.modify and requested.modify,
        approval_eur=min_threshold(applied.approval_eur, requested.approval_eur),
        approval_actions=applied.approval_actions | requested.approval_actions,
        only=only,
        connection_id=applied.connection_id,
        conditions=tuple(conditions),
        limits=merge_limits(applied.limits, requested.limits),
    )


def _frame_entry(raw: Mapping[str, Any]) -> dict[str, Any]:
    return grant_tool_entry(raw, None, ToolPolicy.from_json(dict(raw)))


def held_frame_tools(
    applied: Mapping[str, Any], requested: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Split a frame tools write into what applies now and what waits.

    Per key: in both, the requested policy when it only tightens (its stored
    shape kept), else the meet of the two; removed, removed; added, applied
    when it is not enabled, else held. Returns the tools to write now and the
    pending tools: `requested` itself whenever anything differs from it, None
    when nothing is held.

    Both sides must already be validated (`ToolPolicy.from_json(...,
    strict=True)` on the requested write), or a malformed condition raises
    KeyError/ValueError here."""
    now: dict[str, Any] = {}
    for key, raw in requested.items():
        if key in applied:
            old = applied[key]
            if not policy_loosenings(_frame_entry(old), _frame_entry(raw)):
                now[key] = copy.deepcopy(raw)
            else:
                meet = meet_policy(ToolPolicy.from_json(dict(old)), ToolPolicy.from_json(dict(raw)))
                now[key] = meet.to_json()
        elif not ToolPolicy.from_json(dict(raw)).enabled:
            now[key] = copy.deepcopy(raw)
    pending = copy.deepcopy(dict(requested)) if now != dict(requested) else None
    return now, pending
