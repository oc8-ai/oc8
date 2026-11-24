"""The per-field definition of "tightening" (plan Ruling 2, `authz/tightening.py`).

A confirmation keeps holding after an admin change only when the change
tightens. Every field of a tool grant and every frame key is classified here
with one row per case: tighten, loosen, unchanged, and the key added or
removed. Anything not classified is a loosening (fail closed).
"""

from __future__ import annotations

from typing import Any

import pytest

from oc8.authz.pdp import Condition, Effect, ToolPolicy, UsageLimit
from oc8.authz.tightening import (
    grant_loosenings,
    grant_tool_entry,
    held_frame_tools,
    meet_policy,
    policy_loosenings,
)

Entry = dict[str, Any]


def L(tools: list[str], max_: int, per: str = "day") -> UsageLimit:
    return UsageLimit(tools=tuple(tools), max=max_, per=per)


def _cond(attribute: str, then: Effect, value: Any = 1) -> dict[str, Any]:
    return Condition(attribute=attribute, value=value, then=then).to_json()


RA = _cond("amount", Effect.REQUIRE_APPROVAL, 100)
RA2 = _cond("count", Effect.REQUIRE_APPROVAL, 5)
DENY = _cond("target", Effect.DENY, 7)
DENY2 = _cond("size", Effect.DENY, 9)
ALLOW = _cond("trusted", Effect.ALLOW, 1)


def P(
    *,
    enabled: bool = False,
    read: bool = False,
    modify: bool = False,
    approval_eur: int | None = None,
    approval_actions: list[str] | None = None,
    only: list[str] | None = None,
    connection_id: str | None = None,
    limits: list[UsageLimit] | None = None,
    fc: list[dict[str, Any]] | None = None,
    ac: list[dict[str, Any]] | None = None,
) -> Entry:
    """A grant tool entry in the exact stored shape: `ToolPolicy.to_json()`
    without `conditions`, plus `frame_conditions` and `agent_conditions`."""
    body = ToolPolicy(
        enabled=enabled,
        read=read,
        modify=modify,
        approval_eur=approval_eur,
        approval_actions=frozenset(approval_actions or ()),
        only=frozenset(only) if only is not None else None,
        connection_id=connection_id,
        limits=tuple(limits or ()),
    ).to_json()
    del body["conditions"]
    body["frame_conditions"] = list(fc or [])
    body["agent_conditions"] = list(ac or [])
    return body


def without(entry: Entry, key: str) -> Entry:
    return {k: v for k, v in entry.items() if k != key}


def plus(entry: Entry, **extra: Any) -> Entry:
    return {**entry, **extra}


ON = P(enabled=True)
RW = P(enabled=True, read=True, modify=True)

# (confirmed, current, loosened) -- the brief's 18 rows first, then the rows
# the pre-flight scan (F6) found missing, field by field.
POLICY_ROWS: list[tuple[Entry | None, Entry | None, list[str]]] = [
    # -- the brief's rows --
    (P(enabled=True, modify=True), P(enabled=True, modify=False), []),
    (P(enabled=True, modify=False), P(enabled=True, modify=True), ["modify"]),
    (P(enabled=True), None, []),  # tool gone
    (None, P(enabled=True), ["enabled"]),  # tool added
    (P(enabled=True, approval_eur=500), P(enabled=True, approval_eur=100), []),
    (P(enabled=True, approval_eur=500), P(enabled=True, approval_eur=None), ["approval_eur"]),
    (
        P(enabled=True, approval_actions=["modify"]),
        P(enabled=True, approval_actions=[]),
        ["approval_actions"],
    ),
    (P(enabled=True, only=None), P(enabled=True, only=["a"]), []),
    (P(enabled=True, only=["a"]), P(enabled=True, only=["a", "b"]), ["only"]),
    (P(enabled=True, connection_id="x"), P(enabled=True, connection_id="y"), ["connection_id"]),
    (P(enabled=True, limits=[L(["t"], 10)]), P(enabled=True, limits=[L(["t"], 5)]), []),
    (P(enabled=True, limits=[L(["t"], 10)]), P(enabled=True, limits=[L(["t"], 11)]), ["limits"]),
    (P(enabled=True, limits=[L(["t"], 10)]), P(enabled=True, limits=[]), ["limits"]),
    (
        P(enabled=True, limits=[L(["t"], 10, "day")]),
        P(enabled=True, limits=[L(["t"], 10, "hour")]),
        ["limits"],
    ),
    (P(enabled=True, fc=[RA]), P(enabled=True, fc=[RA, DENY]), []),
    (P(enabled=True, fc=[RA]), P(enabled=True, fc=[]), ["frame_conditions"]),
    (P(enabled=True, fc=[]), P(enabled=True, fc=[ALLOW]), ["frame_conditions"]),
    (P(enabled=True, fc=[], ac=[DENY]), P(enabled=True, fc=[RA], ac=[DENY]), ["frame_conditions"]),
    # -- enabled --
    (P(enabled=True), P(enabled=False), []),
    (P(enabled=False), P(enabled=True), ["enabled"]),
    (P(enabled=True), P(enabled=True), []),
    (P(enabled=False), P(enabled=False), []),
    (P(enabled=False), None, []),
    (None, P(enabled=False), []),  # an added key that grants nothing
    (None, None, []),
    (ON, without(ON, "enabled"), []),  # absent = False
    (without(ON, "enabled"), ON, ["enabled"]),
    # -- read --
    (P(enabled=True, read=True), P(enabled=True, read=False), []),
    (P(enabled=True, read=False), P(enabled=True, read=True), ["read"]),
    (P(enabled=True, read=True), P(enabled=True, read=True), []),
    (P(enabled=True, read=True), without(P(enabled=True, read=True), "read"), []),
    (without(P(enabled=True, read=True), "read"), P(enabled=True, read=True), ["read"]),
    # -- modify --
    (P(enabled=True, modify=True), P(enabled=True, modify=True), []),
    (P(enabled=True, modify=True), without(P(enabled=True, modify=True), "modify"), []),
    (without(P(enabled=True, modify=True), "modify"), P(enabled=True, modify=True), ["modify"]),
    (RW, P(enabled=True, read=True, modify=True), []),
    (P(enabled=True), RW, ["read", "modify"]),
    # -- approval_eur --
    (P(enabled=True, approval_eur=None), P(enabled=True, approval_eur=100), []),
    (P(enabled=True, approval_eur=100), P(enabled=True, approval_eur=500), ["approval_eur"]),
    (P(enabled=True, approval_eur=100), P(enabled=True, approval_eur=100), []),
    (P(enabled=True, approval_eur=0), P(enabled=True, approval_eur=0), []),
    (P(enabled=True, approval_eur=0), P(enabled=True, approval_eur=1), ["approval_eur"]),
    (P(enabled=True), P(enabled=True), []),  # None -> None
    (
        P(enabled=True, approval_eur=100),
        without(P(enabled=True, approval_eur=100), "approval_eur"),
        ["approval_eur"],
    ),
    (without(P(enabled=True), "approval_eur"), P(enabled=True, approval_eur=100), []),
    (
        P(enabled=True, approval_eur=100),
        plus(P(enabled=True), approval_eur="50"),  # not a number: fail closed
        ["approval_eur"],
    ),
    # -- approval_actions --
    (
        P(enabled=True, approval_actions=[]),
        P(enabled=True, approval_actions=["modify"]),
        [],
    ),
    (
        P(enabled=True, approval_actions=["modify"]),
        P(enabled=True, approval_actions=["modify", "tool_x"]),
        [],
    ),
    (
        P(enabled=True, approval_actions=["modify"]),
        P(enabled=True, approval_actions=["tool_x"]),
        ["approval_actions"],
    ),
    (
        P(enabled=True, approval_actions=["modify", "tool_x"]),
        P(enabled=True, approval_actions=["tool_x", "modify"]),
        [],
    ),
    (
        P(enabled=True, approval_actions=["modify"]),
        without(P(enabled=True), "approval_actions"),
        ["approval_actions"],
    ),
    (
        without(P(enabled=True), "approval_actions"),
        P(enabled=True, approval_actions=["modify"]),
        [],
    ),
    # -- only --
    (P(enabled=True, only=["a", "b"]), P(enabled=True, only=["a"]), []),
    (P(enabled=True, only=["a"]), P(enabled=True, only=None), ["only"]),
    (P(enabled=True, only=["a"]), P(enabled=True, only=["b"]), ["only"]),
    (P(enabled=True, only=["a"]), P(enabled=True, only=["a"]), []),
    (P(enabled=True, only=None), P(enabled=True, only=None), []),
    (P(enabled=True, only=["a"]), plus(P(enabled=True), only=[]), ["only"]),  # [] = every tool
    (P(enabled=True, only=["a"]), without(P(enabled=True), "only"), ["only"]),
    (without(P(enabled=True), "only"), P(enabled=True, only=["a"]), []),
    # -- connection_id --
    (P(enabled=True, connection_id="x"), P(enabled=True, connection_id="x"), []),
    (P(enabled=True, connection_id="x"), P(enabled=True, connection_id=None), ["connection_id"]),
    (P(enabled=True, connection_id=None), P(enabled=True, connection_id="x"), ["connection_id"]),
    (
        P(enabled=True, connection_id="x"),
        without(P(enabled=True), "connection_id"),
        ["connection_id"],
    ),
    (
        without(P(enabled=True), "connection_id"),
        P(enabled=True, connection_id="x"),
        ["connection_id"],
    ),
    # -- limits --
    (P(enabled=True, limits=[L(["t"], 10)]), P(enabled=True, limits=[L(["t"], 10)]), []),
    (
        P(enabled=True, limits=[L(["t"], 10)]),
        P(enabled=True, limits=[L(["t"], 10), L(["u"], 3, "hour")]),
        [],
    ),  # an extra window is fine
    (P(enabled=True, limits=[]), P(enabled=True, limits=[L(["t"], 10)]), []),
    (
        P(enabled=True, limits=[L(["t"], 10)]),
        P(enabled=True, limits=[L(["t", "u"], 10)]),
        ["limits"],
    ),  # a changed tool set drops the old window
    (
        P(enabled=True, limits=[L(["t", "u"], 10)]),
        P(enabled=True, limits=[L(["u", "t"], 10)]),
        [],
    ),  # order-free window
    (
        P(enabled=True, limits=[L(["t"], 10)]),
        P(enabled=True, limits=[L(["t"], 20), L(["t"], 5)]),
        [],
    ),  # the tightest max of a repeated window counts
    (P(enabled=True, limits=[L(["t"], 10)]), without(P(enabled=True), "limits"), ["limits"]),
    (without(P(enabled=True), "limits"), P(enabled=True, limits=[L(["t"], 10)]), []),
    # -- frame_conditions --
    (P(enabled=True, fc=[RA]), P(enabled=True, fc=[RA]), []),
    (P(enabled=True, fc=[]), P(enabled=True, fc=[DENY]), []),
    (P(enabled=True, fc=[]), P(enabled=True, fc=[RA]), []),  # no agent deny
    (P(enabled=True, fc=[], ac=[RA2]), P(enabled=True, fc=[RA], ac=[RA2]), []),
    (P(enabled=True, fc=[], ac=[DENY]), P(enabled=True, fc=[DENY2], ac=[DENY]), []),
    (
        P(enabled=True, fc=[RA]),
        P(enabled=True, fc=[DENY, RA]),
        ["frame_conditions"],
    ),  # not an append
    (P(enabled=True, fc=[RA, DENY]), P(enabled=True, fc=[DENY, RA]), ["frame_conditions"]),
    (
        P(enabled=True, fc=[RA]),
        P(enabled=True, fc=[_cond("amount", Effect.REQUIRE_APPROVAL, 900)]),
        ["frame_conditions"],
    ),  # changed in place
    (P(enabled=True, fc=[RA]), P(enabled=True, fc=[RA, ALLOW]), ["frame_conditions"]),
    (P(enabled=True, fc=[RA]), without(P(enabled=True), "frame_conditions"), ["frame_conditions"]),
    (without(P(enabled=True), "frame_conditions"), P(enabled=True, fc=[DENY]), []),
    # -- agent_conditions --
    (P(enabled=True, ac=[DENY]), P(enabled=True, ac=[DENY]), []),
    (P(enabled=True, ac=[]), P(enabled=True, ac=[DENY]), []),
    (P(enabled=True, ac=[DENY]), P(enabled=True, ac=[DENY, RA]), []),
    (P(enabled=True, ac=[DENY]), P(enabled=True, ac=[]), ["agent_conditions"]),
    (P(enabled=True, ac=[]), P(enabled=True, ac=[ALLOW]), ["agent_conditions"]),
    (P(enabled=True, ac=[DENY, RA]), P(enabled=True, ac=[RA, DENY]), ["agent_conditions"]),
    (
        P(enabled=True, ac=[DENY]),
        P(enabled=True, ac=[_cond("target", Effect.REQUIRE_APPROVAL, 7)]),
        ["agent_conditions"],
    ),
    (
        P(enabled=True, ac=[DENY]),
        without(P(enabled=True), "agent_conditions"),
        ["agent_conditions"],
    ),
    (without(P(enabled=True), "agent_conditions"), P(enabled=True, ac=[DENY]), []),
    # -- an unknown field: equal or a loosening --
    (ON, plus(ON, surprise=1), ["surprise"]),
    (plus(ON, surprise=1), ON, ["surprise"]),
    (plus(ON, surprise=1), plus(ON, surprise=2), ["surprise"]),
    (plus(ON, surprise=1), plus(ON, surprise=1), []),
    (ON, plus(ON, conditions=[RA]), ["conditions"]),  # a raw policy shape is not a grant entry
    # -- several fields at once, reported in field order --
    (
        P(enabled=True, approval_eur=100, limits=[L(["t"], 1)]),
        P(enabled=True, modify=True, limits=[]),
        ["modify", "approval_eur", "limits"],
    ),
]


@pytest.mark.parametrize(("confirmed", "current", "loosened"), POLICY_ROWS)
def test_policy_loosenings(
    confirmed: Entry | None, current: Entry | None, loosened: list[str]
) -> None:
    assert policy_loosenings(confirmed, current) == loosened


def test_a_disabled_policy_grants_nothing_whatever_else_it_says() -> None:
    confirmed = P(enabled=True, read=True, approval_eur=10, only=["a"], limits=[L(["t"], 1)])
    current = P(
        enabled=False,
        read=True,
        modify=True,
        only=None,
        connection_id="other",
        fc=[ALLOW],
    )
    assert policy_loosenings(confirmed, plus(current, surprise=1)) == []
    assert policy_loosenings(None, current) == []


def test_a_confirmed_disabled_policy_confirmed_nothing() -> None:
    assert policy_loosenings(P(enabled=False, modify=True), P(enabled=True, modify=True)) == [
        "enabled"
    ]


# --- the frame ------------------------------------------------------------


def _grant(
    tools: dict[str, Entry] | None = None,
    frame: dict[str, Any] | None = None,
    skills: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"tools": tools or {}, "frame": frame or {}, "skills": skills or {}}


KB_A = "kb-a"
KB_B = "kb-b"

FRAME_ROWS: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = [
    # closed
    ({"closed": True}, {"closed": False}, ["frame:closed"]),
    ({"closed": False}, {"closed": True}, []),
    ({"closed": True}, {"closed": True}, []),
    ({"closed": False}, {"closed": False}, []),
    ({}, {"closed": True}, []),  # key added: closed
    ({"closed": True}, {}, ["frame:closed"]),  # key removed: open
    ({"closed": False}, {}, []),  # not closed either way
    ({}, {"closed": False}, []),
    ({"closed": True}, {"closed": "yes"}, ["frame:closed"]),  # only `True` closes
    # kbs
    ({"kbs": [KB_A, KB_B]}, {"kbs": [KB_A]}, []),
    ({"kbs": [KB_A]}, {"kbs": [KB_A, KB_B]}, ["frame:kbs"]),
    ({"kbs": [KB_A]}, {"kbs": [KB_B]}, ["frame:kbs"]),
    ({"kbs": [KB_A, KB_B]}, {"kbs": [KB_B, KB_A]}, []),
    ({"kbs": [KB_A]}, {}, []),  # key removed: no kbs
    ({}, {"kbs": [KB_A]}, ["frame:kbs"]),  # key added with an item
    ({}, {"kbs": []}, []),
    ({"kbs": [{"id": KB_A, "x": 1}]}, {"kbs": [{"x": 1, "id": KB_A}]}, []),  # canonical JSON
    ({"kbs": [KB_A]}, {"kbs": "kb-a"}, ["frame:kbs"]),  # not a list: fail closed
    # memory
    ({"memory": {"department": ["read", "write"]}}, {"memory": {"department": ["read"]}}, []),
    (
        {"memory": {"department": ["read"]}},
        {"memory": {"department": ["read", "write"]}},
        ["frame:memory"],
    ),
    (
        {"memory": {"department": ["read"]}},
        {"memory": {"department": ["read"], "company": ["read"]}},
        ["frame:memory"],
    ),
    (
        {"memory": {"department": ["read"], "company": ["read"]}},
        {"memory": {"department": ["read"]}},
        [],
    ),
    (
        {"memory": {"department": ["read", "write"]}},
        {"memory": {"department": ["write", "read"]}},
        [],
    ),
    ({"memory": {"department": ["read"]}}, {}, []),  # key removed: no rights
    ({}, {"memory": {"department": ["read"]}}, ["frame:memory"]),
    ({}, {"memory": {"department": []}}, []),
    ({"memory": {"department": ["read"]}}, {"memory": "all"}, ["frame:memory"]),
    ({"memory": {"department": ["read"]}}, {"memory": {"department": "read"}}, ["frame:memory"]),
]

# Keys with no finer rule: equal, or a loosening. Each gets the four cases.
EQUAL_ONLY_KEYS: list[tuple[str, Any, Any]] = [
    ("emits", ["order.created"], ["order.created", "order.paid"]),
    ("intakes", [{"event": "order.created"}], [{"event": "order.paid"}]),
    ("cleared_classes", ["public", "internal"], ["public"]),  # even a narrower set: fail closed
    ("tools", {"t": ON}, {"t": RW}),  # grant.frame carries no tools; if it does, strict
    ("surprise", 1, 2),
]
for _key, _a, _b in EQUAL_ONLY_KEYS:
    FRAME_ROWS += [
        ({_key: _a}, {_key: _a}, []),
        ({_key: _a}, {_key: _b}, [f"frame:{_key}"]),
        ({}, {_key: _a}, [f"frame:{_key}"]),
        ({_key: _a}, {}, [f"frame:{_key}"]),
    ]


@pytest.mark.parametrize(("confirmed", "current", "loosened"), FRAME_ROWS)
def test_frame_rest(
    confirmed: dict[str, Any], current: dict[str, Any], loosened: list[str]
) -> None:
    assert grant_loosenings(_grant(frame=confirmed), _grant(frame=current)) == loosened


# --- skills ---------------------------------------------------------------

S1 = ["ver-1", {}]
S2 = ["ver-2", {"k": "v"}]

SKILL_ROWS: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = [
    ({"a1": S1, "a2": S2}, {"a1": S1}, []),  # removal is fine
    ({"a1": S1}, {"a1": S1}, []),
    ({}, {}, []),
    ({"a1": S1}, {"a1": S1, "a2": S2}, ["skill:a2"]),  # added
    ({"a1": S1}, {"a1": ["ver-9", {}]}, ["skill:a1"]),  # new skill content
    ({"a1": S2}, {"a1": ["ver-2", {"k": "w"}]}, ["skill:a1"]),  # overrides changed
    ({"a1": S2}, {"a1": ["ver-2", {}]}, ["skill:a1"]),  # an override dropped is a change too
    ({"a1": S1}, {"a2": S1}, ["skill:a2"]),  # a different assignment is an addition
]


@pytest.mark.parametrize(("confirmed", "current", "loosened"), SKILL_ROWS)
def test_skills(confirmed: dict[str, Any], current: dict[str, Any], loosened: list[str]) -> None:
    assert grant_loosenings(_grant(skills=confirmed), _grant(skills=current)) == loosened


# --- the whole grant ------------------------------------------------------


def test_grant_loosenings_lists_every_part_sorted() -> None:
    confirmed = _grant(
        tools={"b": P(enabled=True), "a": P(enabled=True)},
        frame={"closed": True, "kbs": [KB_A]},
        skills={"s1": S1},
    )
    current = _grant(
        tools={"b": P(enabled=True, modify=True), "a": P(enabled=True), "c": P(enabled=True)},
        frame={"closed": True, "kbs": [KB_A, KB_B]},
        skills={"s1": S1, "s2": S2},
    )
    assert grant_loosenings(confirmed, current) == [
        "frame:kbs",
        "skill:s2",
        "tool:b",
        "tool:c",
    ]
    assert grant_loosenings(confirmed, confirmed) == []


def test_a_removed_tool_tightens_the_grant() -> None:
    confirmed = _grant(tools={"a": P(enabled=True), "b": P(enabled=True)})
    assert grant_loosenings(confirmed, _grant(tools={"a": P(enabled=True)})) == []


def test_an_unknown_grant_part_fails_closed() -> None:
    confirmed = _grant()
    assert grant_loosenings(confirmed, {**confirmed, "extra": {"x": 1}}) == ["grant:extra"]
    assert grant_loosenings({**confirmed, "extra": 1}, confirmed) == ["grant:extra"]
    assert grant_loosenings({**confirmed, "extra": 1}, {**confirmed, "extra": 1}) == []


def test_a_missing_grant_part_reads_as_empty() -> None:
    assert grant_loosenings(_grant(tools={"a": ON}, skills={"s": S1}), {}) == []
    assert grant_loosenings({}, _grant(tools={"a": ON})) == ["tool:a"]


# --- grant_tool_entry -----------------------------------------------------


def test_grant_tool_entry_keeps_the_two_condition_lists_apart() -> None:
    frame_policy = {"enabled": True, "read": True, "conditions": [RA]}
    narrowing_policy = {"enabled": True, "read": True, "conditions": [DENY]}
    effective = ToolPolicy.from_json(
        {"enabled": True, "read": True, "conditions": [RA, DENY], "limits": []}
    )
    entry = grant_tool_entry(frame_policy, narrowing_policy, effective)
    expected = effective.to_json()
    del expected["conditions"]
    assert entry == {**expected, "frame_conditions": [RA], "agent_conditions": [DENY]}


def test_grant_tool_entry_without_either_side() -> None:
    effective = ToolPolicy(enabled=True)
    entry = grant_tool_entry(None, None, effective)
    assert entry["frame_conditions"] == [] and entry["agent_conditions"] == []
    assert "conditions" not in entry


# --- meet -----------------------------------------------------------------


def _as_policy(entry: Entry) -> ToolPolicy:
    raw = dict(entry)
    raw["conditions"] = list(entry.get("frame_conditions", [])) + list(
        entry.get("agent_conditions", [])
    )
    return ToolPolicy.from_json(raw)


def _as_frame_entry(policy: ToolPolicy) -> Entry:
    return grant_tool_entry(policy.to_json(), None, policy)


def _known_only(entry: Entry) -> bool:
    eur = entry.get("approval_eur")
    return set(entry) <= set(ON) and (eur is None or isinstance(eur, int))


MEET_PAIRS = [
    (_as_policy(c), _as_policy(r))
    for c, r, _ in POLICY_ROWS
    if c is not None and r is not None and _known_only(c) and _known_only(r)
] + [
    (ToolPolicy(enabled=True, connection_id="x"), ToolPolicy(enabled=True, connection_id="y")),
    (
        ToolPolicy(enabled=True, conditions=(Condition.from_json(ALLOW),)),
        ToolPolicy(enabled=True, conditions=(Condition.from_json(DENY),)),
    ),
]


def _not_looser_than(meet: ToolPolicy, other: ToolPolicy) -> None:
    """Field by field: `meet` grants no more than `other` does."""
    assert meet.enabled <= other.enabled
    assert meet.read <= other.read
    assert meet.modify <= other.modify
    if other.approval_eur is not None:
        assert meet.approval_eur is not None and meet.approval_eur <= other.approval_eur
    assert meet.approval_actions >= other.approval_actions
    if other.only is not None:
        assert meet.only is not None and meet.only <= other.only
    if meet.enabled:
        assert meet.connection_id == other.connection_id
    windows = {(lim.key, lim.per): lim.max for lim in meet.limits}
    for lim in other.limits:
        assert (lim.key, lim.per) in windows and windows[(lim.key, lim.per)] <= lim.max
    for cond in other.conditions:
        if cond.then is not Effect.ALLOW:
            assert cond in meet.conditions


@pytest.mark.parametrize(("applied", "requested"), MEET_PAIRS)
def test_meet_is_never_looser_than_either_input(applied: ToolPolicy, requested: ToolPolicy) -> None:
    meet = meet_policy(applied, requested)
    meet_entry = _as_frame_entry(meet)
    # Against the applied policy: the full rule, conditions included.
    assert policy_loosenings(_as_frame_entry(applied), meet_entry) == []
    _not_looser_than(meet, applied)
    # Against the requested policy: every field but the condition order (the
    # applied conditions come first, so they are not an append to the
    # requested list), and every requested non-allow condition is kept.
    assert set(policy_loosenings(_as_frame_entry(requested), meet_entry)) <= {"frame_conditions"}
    _not_looser_than(meet, requested)


def test_meet_appends_only_new_non_allow_conditions() -> None:
    ra, deny, allow = (Condition.from_json(c) for c in (RA, DENY, ALLOW))
    meet = meet_policy(
        ToolPolicy(enabled=True, conditions=(ra,)),
        ToolPolicy(enabled=True, conditions=(allow, deny, ra)),
    )
    assert meet.conditions == (ra, deny)


def test_meet_merges_limits_to_the_tighter_max() -> None:
    meet = meet_policy(
        ToolPolicy(enabled=True, limits=(L(["t"], 10), L(["u"], 1))),
        ToolPolicy(enabled=True, limits=(L(["t"], 4), L(["v"], 2, "hour"))),
    )
    assert {(lim.key, lim.per): lim.max for lim in meet.limits} == {
        ("t", "day"): 4,
        ("u", "day"): 1,
        ("v", "hour"): 2,
    }


def test_meet_of_two_logins_grants_nothing() -> None:
    meet = meet_policy(
        ToolPolicy(enabled=True, connection_id="x"), ToolPolicy(enabled=True, connection_id="y")
    )
    assert meet.enabled is False


def test_meet_of_disjoint_surfaces_grants_nothing() -> None:
    meet = meet_policy(
        ToolPolicy(enabled=True, only=frozenset({"a"})),
        ToolPolicy(enabled=True, only=frozenset({"b"})),
    )
    assert meet.enabled is False
    # Its stored form must not read back as a grant of every tool.
    assert ToolPolicy.from_json(meet.to_json()).enabled is False


def test_an_empty_effective_surface_is_recorded_as_off() -> None:
    entry = grant_tool_entry(None, None, ToolPolicy(enabled=True, only=frozenset()))
    assert entry["enabled"] is False
    # So a later grant of any named tool reads as the loosening it is.
    assert policy_loosenings(entry, P(enabled=True, only=["a"])) == ["enabled"]


# --- held frame tools -----------------------------------------------------

on_rw = ToolPolicy(enabled=True, read=True, modify=True).to_json()
on_r = ToolPolicy(enabled=True, read=True).to_json()


def test_held_frame_tools_applies_tightening_and_holds_loosening() -> None:
    applied, pending = held_frame_tools({"a": on_rw, "b": on_r}, {"a": on_r, "b": on_rw, "c": on_r})
    assert applied == {"a": on_r, "b": on_r}
    assert pending == {"a": on_r, "b": on_rw, "c": on_r}


def test_held_frame_tools_holds_nothing_for_a_pure_tightening() -> None:
    requested = {"a": on_r}
    applied, pending = held_frame_tools({"a": on_rw, "b": on_r}, requested)
    assert pending is None
    assert applied == requested


def test_an_added_disabled_key_is_applied() -> None:
    applied, pending = held_frame_tools({"a": on_r}, {"a": on_r, "c": {"enabled": False}})
    assert applied == {"a": on_r, "c": {"enabled": False}}
    assert pending is None


def test_a_tightened_key_keeps_its_stored_shape() -> None:
    raw = {"enabled": True, "read": True}  # not the full to_json() shape
    applied, pending = held_frame_tools({"a": on_rw}, {"a": raw})
    assert applied == {"a": raw} and pending is None


def test_a_loosened_key_is_written_as_the_meet() -> None:
    applied, pending = held_frame_tools(
        {"a": ToolPolicy(enabled=True, read=True, approval_eur=100).to_json()},
        {"a": ToolPolicy(enabled=True, read=True, modify=True, approval_eur=50).to_json()},
    )
    assert applied == {"a": ToolPolicy(enabled=True, read=True, approval_eur=50).to_json()}
    assert pending is not None


def test_an_unchanged_frame_holds_nothing() -> None:
    applied, pending = held_frame_tools({"a": on_r}, {"a": on_r})
    assert applied == {"a": on_r} and pending is None


def test_held_frame_tools_does_not_alias_its_inputs() -> None:
    requested = {"a": on_r, "c": on_r}
    applied, pending = held_frame_tools({"a": on_r}, requested)
    assert pending == requested and pending is not requested
    assert applied is not requested
