"""What a tenant's own administrator may put in a role -- and what it costs to
add a permission after today.

The one assertion this file exists for is
`test_every_permission_is_classified_exactly_once`. Every earlier attempt at
this feature computed the offerable set as `ALL_PERMISSIONS - UNGRANTABLE`, and
every one of them failed OPEN in the same way: the seat slice added
`member:manage`, `approval:view_any` and `approval:decide_any` to the catalogue
without touching the role builder, and under a deny-list all three would have
appeared as tickable boxes. `member:manage` is the right to enrol yourself in
any department; the two `_any` flags ARE the words "in every department". Any
one of them, ticked once, ends the department boundary the previous slice was
written to create.

So the partition is three hand-written sets and the union check is the forcing
function: a permission added to `ALL_PERMISSIONS` fails the build until a human
has said which of the three it belongs to. Nothing here is about today's 52
strings; it is about the 53rd.
"""

from __future__ import annotations

from typing import Any

import pytest

from oc8.authz.permissions import (
    AGENT_DEFAULT,
    ALL_PERMISSIONS,
    APPROVAL_DECIDE,
    APPROVAL_DECIDE_ANY,
    APPROVAL_VIEW_ANY,
    AUDIT_VERIFY,
    BUILTIN_ROLE_PERMISSIONS,
    CLARIFICATION_ANSWER,
    CONNECTION_OWN,
    COPILOT_USE,
    DELEGATABLE_PERMISSIONS,
    DEPARTMENT_SCOPABLE,
    MANAGE,
    NEVER_DELEGATABLE,
    NOT_YET_DELEGATABLE,
    ORG_ADMIN,
    ROLE,
    RUN_CONTROL,
    RUN_START,
    SECRET,
    VIEW,
    delegation_refusal,
    perm,
    permissions_for,
    role_kind,
)
from tests.api.test_every_route_is_governed import (
    _api_routes,
    _closed_over_permission,
    _walk,
)


def test_every_permission_is_classified_exactly_once() -> None:
    """Pairwise disjoint, and covering the catalogue exactly.

    This is the whole guard. Add a permission to `ALL_PERMISSIONS` and this test
    names it as unclassified; delete one and this test names it as classified
    twice. Neither can be discovered later by a customer.
    """
    delegatable = set(DELEGATABLE_PERMISSIONS)
    not_yet = set(NOT_YET_DELEGATABLE)
    never = set(NEVER_DELEGATABLE)

    assert not delegatable & not_yet, sorted(delegatable & not_yet)
    assert not delegatable & never, sorted(delegatable & never)
    assert not not_yet & never, sorted(not_yet & never)

    classified = delegatable | not_yet | never
    unclassified = sorted(ALL_PERMISSIONS - classified)
    assert not unclassified, (
        f"{len(unclassified)} permission(s) exist that nobody has classified: {unclassified}. "
        "Add each to DELEGATABLE_PERMISSIONS, or to one of the two refusal dicts WITH a "
        "reason. Until then the role builder does not know whether to offer them."
    )
    invented = sorted(classified - ALL_PERMISSIONS)
    assert not invented, f"classified strings that are not permissions at all: {invented}"


def test_no_manage_permission_is_delegatable() -> None:
    """Decision C, as one assertion.

    `require_permission` sees a string and nothing else -- it structurally cannot
    carry a resource -- so EVERY `:manage` in this catalogue is tenant-wide.
    Offering an admin a builder whose output is tenant-wide `department:manage`
    is precisely the flaw the seat vocabulary exists to close, and a caption
    saying "gilt mandantenweit" does not close it.
    """
    escapes = sorted(p for p in DELEGATABLE_PERMISSIONS if p.endswith(f":{MANAGE}"))
    assert not escapes, (
        f"a tenant-defined role may not manage anything, and these would: {escapes}. "
        "A :manage graduates only when its route resolves a department first."
    )


def test_the_flags_that_would_end_the_seat_mechanism_are_never_delegatable() -> None:
    """Named one at a time rather than left to the arithmetic above.

    These four are the permissions a deny-list would have handed out silently,
    and each of them alone makes the department boundary decorative. If a future
    refactor "simplifies" the three sets, this is the test that reads as a
    sentence about what was lost.
    """
    for flag in (
        APPROVAL_VIEW_ANY,
        APPROVAL_DECIDE_ANY,
        perm("member", MANAGE),
        perm(ROLE, MANAGE),
    ):
        assert flag in NEVER_DELEGATABLE, flag
        assert flag not in DELEGATABLE_PERMISSIONS, flag


def test_a_delegatable_permission_changes_no_configuration() -> None:
    """The escalation ceiling, stated as set arithmetic rather than as a promise.

    Look, start work, answer, decide, verify -- and nothing else. A permission
    that is neither a `:view` nor one of these six verbs has appeared in the
    delegatable set, and whatever it is, it is not one of the six things the
    design says a tenant role may ever be.
    """
    verbs = {
        RUN_START,
        RUN_CONTROL,
        APPROVAL_DECIDE,
        CLARIFICATION_ANSWER,
        AUDIT_VERIFY,
        COPILOT_USE,
        CONNECTION_OWN,
    }
    strange = sorted(
        p for p in DELEGATABLE_PERMISSIONS if not p.endswith(f":{VIEW}") and p not in verbs
    )
    assert not strange, (
        f"these are offerable but are neither a read nor one of the six verbs: {strange}"
    )


def test_every_delegatable_permission_gates_at_least_one_route() -> None:
    """An offerable box that gates nothing is a lie with a checkbox next to it.

    The role builder renders `DELEGATABLE_PERMISSIONS`. An administrator ticks
    "Prüfprotokoll lesen", saves, tells the auditor it is done -- and the
    permission gates no route, so nothing changed and nothing said so. That is
    worse than refusing it, because a refusal is visible.

    It is also the assertion that stops the delegatable set being padded to look
    generous: `role:view` is in it, and until `GET /roles` exists there is no
    route that reads it, so this fails until the endpoint that gives it meaning
    is mounted.

    One-way, like `DEPARTMENT_SCOPABLE`'s sweep: a route may declare a permission
    nobody may delegate (that is most of them), but a permission somebody may
    delegate must open a door.
    """
    declared = {
        detail for row in _api_routes() for kind, detail in row["guards"] if kind == "permission"
    }
    assert len(declared) > 30, (
        f"the route sweep found only {len(declared)} permissions; it is broken"
    )

    # TODO(connections): drop this exemption once the connections API (a later
    # task of the same change) gates its routes with `connection:own`.
    inert = sorted(DELEGATABLE_PERMISSIONS - declared - {CONNECTION_OWN})
    assert not inert, (
        f"these can be ticked in the role builder and gate no route at all: {inert}. "
        "Either a route is missing, or the box should not be offered."
    )


def test_the_secret_store_is_refused_on_both_sides() -> None:
    """`secret:view` is metadata only and still refused: the list of credentials
    a tenant holds is a map of everywhere it can reach. It is the one `:view` in
    the catalogue that is NEVER delegatable rather than merely not-yet, so a
    later reviewer moving it for symmetry has to walk past this."""
    assert perm(SECRET, VIEW) in NEVER_DELEGATABLE
    assert perm(SECRET, MANAGE) in NEVER_DELEGATABLE
    assert perm(SECRET, VIEW) not in NOT_YET_DELEGATABLE


@pytest.mark.parametrize(
    "permission",
    sorted(set(NOT_YET_DELEGATABLE) | set(NEVER_DELEGATABLE)),
)
def test_every_refused_permission_names_its_reason(permission: str) -> None:
    """The same discipline `unguarded()` is held to: a refusal is only worth
    having if it carries a sentence a reviewer can disagree with -- and this one
    is rendered to the admin who ticked the box, so "no" without a "because" is
    a support ticket."""
    reason = delegation_refusal(permission)
    assert reason is not None
    assert len(reason.strip()) > 30, f"{permission}: {reason!r} explains nothing"


def test_a_delegatable_permission_has_no_refusal_reason() -> None:
    """The 422 body is built from `delegation_refusal`, so a reason attached to
    something offerable would name a right the caller was actually granted."""
    for permission in sorted(DELEGATABLE_PERMISSIONS):
        assert delegation_refusal(permission) is None, permission
    assert delegation_refusal("plugin:mange") is None, "a typo is not a right somebody nearly had"


def test_the_admin_still_holds_everything_the_builder_refuses() -> None:
    """The point of a refusal is that the work is still possible -- by the person
    the tenant already trusts with it. If this ever fails, the builder is not
    narrowing authority, it is deleting a capability."""
    refused = set(NOT_YET_DELEGATABLE) | set(NEVER_DELEGATABLE)
    assert refused <= permissions_for(ORG_ADMIN)


def test_the_new_role_resource_is_readable_widely_and_writable_by_one() -> None:
    """`role:view` sweeps into `_VIEW_EVERYTHING` and is delegatable -- an admin
    composing a role for a team lead can let her see the tenant's roles. `role:
    manage` is the one that mints authority, so it stays where nothing can reach
    it but the built-in administrator."""
    assert perm(ROLE, VIEW) in ALL_PERMISSIONS
    assert perm(ROLE, VIEW) in DELEGATABLE_PERMISSIONS
    assert perm(ROLE, MANAGE) in NEVER_DELEGATABLE


def test_only_the_agents_own_role_is_of_the_agent_kind() -> None:
    """The `role` table holds two populations and `kind` is the whole of what
    keeps them apart -- a tenant naming a role `agent_default` must not be able
    to reach the agent's vocabulary through the CODE dict, which is what happens
    when the name is the discriminator.

    A name nobody recognises resolves to `'human'`: the safe direction, because
    a human-kind row grants an agent nothing, where the inverse mistake grants a
    person's role every tool right there is.
    """
    assert role_kind(AGENT_DEFAULT) == "agent"
    for name in set(BUILTIN_ROLE_PERMISSIONS) - {AGENT_DEFAULT}:
        assert role_kind(name) == "human", name
    assert role_kind("Freigabe Vertrieb") == "human"
    assert role_kind("agent_defualt") == "human"


def _departmental_permissions() -> set[str]:
    """The permissions declared by routes gated on `require_departmental`.

    Its own sweep rather than `test_every_route_is_governed._guards`, which
    labels a departmental gate and a tenant-wide one identically -- that is
    correct there (both are "this route names a permission") and is exactly the
    distinction being asserted here.
    """
    from oc8.main import create_app

    found: set[str] = set()

    def _visit(dependant: Any, seen: set[int]) -> None:
        for sub in getattr(dependant, "dependencies", []) or []:
            call = getattr(sub, "call", None)
            if call is not None and id(call) not in seen:
                seen.add(id(call))
                name = getattr(call, "__qualname__", "") or ""
                if "require_departmental" in name:
                    found.add(_closed_over_permission(call))
            _visit(sub, seen)

    app = create_app()
    for _prefix, route in _walk(list(app.routes)):
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            _visit(dependant, set())
    return found


def test_department_scopable_is_a_subset_of_what_departmental_routes_declare() -> None:
    """One-way on purpose.

    A permission may only be held IN a department if every route that reads it
    resolves a department first -- so `DEPARTMENT_SCOPABLE ⊆ departmental
    routes`. The inverse is deliberately not asserted: a later slice making a
    fifth route departmental must not thereby widen what a SEAT may carry. That
    stays a decision somebody makes in `SEAT_PERMISSIONS`, in front of a
    reviewer.
    """
    declared = _departmental_permissions()
    assert declared, "the sweep found no departmental routes at all; it is broken"
    orphans = sorted(DEPARTMENT_SCOPABLE - declared)
    assert not orphans, (
        f"these can be granted in ONE department but are read by routes that resolve "
        f"none, so the grant is tenant-wide in practice: {orphans}"
    )
    assert DEPARTMENT_SCOPABLE <= DELEGATABLE_PERMISSIONS, (
        "a permission a seat may carry must also be one a tenant role may hold; "
        "otherwise the same right is refused in the builder and granted by a seat"
    )
