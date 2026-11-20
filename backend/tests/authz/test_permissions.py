"""What each built-in role may do, and the promise that nothing got wider.

The dangerous half of introducing a permission layer is not leaving a route
unguarded -- a test catches that. It is quietly ADMITTING somebody who was
refused before, because that failure has no symptom until it is used.
"""

from __future__ import annotations

import pytest

from oc8.authz.permissions import (
    AGENT_DEFAULT,
    ALL_PERMISSIONS,
    APPROVAL_DECIDE,
    AUDIT_VERIFY,
    AUDITOR,
    BUILTIN_ROLE_PERMISSIONS,
    CONNECTION_OWN,
    COPILOT_USE,
    DEPT_MANAGER,
    OPERATOR,
    ORG_ADMIN,
    RUN_CONTROL,
    RUN_START,
    TOOL_READ,
    TOOL_SEND,
    TOOL_WRITE,
    permissions_for,
    role_has,
)


def test_org_admin_holds_everything() -> None:
    """Every route that was `require_role("org_admin")` maps to a permission, so
    this is what keeps all 38 previously-gated routes working exactly as before."""
    assert permissions_for(ORG_ADMIN) == ALL_PERMISSIONS


def test_an_unknown_role_holds_nothing() -> None:
    """Fail-closed, and deliberately the OPPOSITE of `required_right`, which
    resolves an unknown TOOL to the more dangerous right. There the unknown
    thing is the action; here it is the caller."""
    assert permissions_for("") == frozenset()
    assert not role_has("typo_admin", APPROVAL_DECIDE)


def test_the_role_that_guarded_a_route_still_grants_nothing_operationally() -> None:
    """`require_role("org_admin", "member")` guarded GET /agents/{id}/skills.
    That route was org_admin-only while reading as though it welcomed two roles,
    because `member` granted nothing operational -- the fact that makes naming
    permissions instead of roles worth the change.

    `member` is a DEFINED role now, and issued: `oc8 tenant invite --role member`
    mints the employee whose authority is his seats. Since the copilot-access
    slice, `member` also holds `copilot:use` tenant-wide (see COPILOT_USE) --
    that is the ONE exception to "grants nothing", and it is asserted here
    explicitly rather than silently allowed to slip in. What must not change is
    everything else that route depended on: `member` still holds no seat-bearing
    or `:manage` permission tenant-wide, and `menber` (a typo) still resolves to
    truly nothing.
    """
    assert BUILTIN_ROLE_PERMISSIONS["member"] == frozenset({CONNECTION_OWN, COPILOT_USE})
    assert permissions_for("member") == frozenset({CONNECTION_OWN, COPILOT_USE})
    assert permissions_for("menber") == frozenset()
    assert not role_has("member", APPROVAL_DECIDE)


def test_operator_runs_the_office_but_cannot_rebuild_it() -> None:
    assert role_has(OPERATOR, RUN_START)
    assert role_has(OPERATOR, RUN_CONTROL)
    assert role_has(OPERATOR, APPROVAL_DECIDE)
    for denied in ("plugin:manage", "secret:manage", "budget:manage", "agent:manage"):
        assert not role_has(OPERATOR, denied), denied


def test_a_department_manager_shapes_a_department_but_not_the_tenant() -> None:
    for allowed in ("agent:manage", "skill:manage", "trigger:manage", "supervision:manage"):
        assert role_has(DEPT_MANAGER, allowed), allowed
    for denied in ("plugin:manage", "secret:manage", "settings:manage", "integration:manage"):
        assert not role_has(DEPT_MANAGER, denied), denied


def test_an_auditor_can_read_and_verify_but_never_act() -> None:
    """The value of the role is that its account cannot have caused what it is
    auditing -- so it holds no `manage` at all, and not even `run:start`."""
    assert role_has(AUDITOR, "audit:view")
    assert role_has(AUDITOR, AUDIT_VERIFY)
    assert not role_has(AUDITOR, RUN_START)
    assert not any(p.endswith(":manage") for p in permissions_for(AUDITOR))


def test_only_the_auditor_and_the_admin_reach_the_audit_trail() -> None:
    holders = {r for r, p in BUILTIN_ROLE_PERMISSIONS.items() if "audit:view" in p}
    assert holders == {AUDITOR, ORG_ADMIN}


def test_the_secret_list_stays_where_it_was() -> None:
    """`GET /secrets` was org_admin-only. The list of which credentials a tenant
    holds is a map of where it can reach, so 'everyone may view' stops here."""
    holders = {r for r, p in BUILTIN_ROLE_PERMISSIONS.items() if "secret:view" in p}
    assert holders == {ORG_ADMIN}


def test_an_agent_role_grants_nothing_on_the_operator_api() -> None:
    """An agent token is refused by the operator API as a whole, so this role
    holds no operator permission at all -- a statement, not an oversight.

    What it does hold is the three TOOL rights (§5.3's role term), which are a
    different resource entirely. Asserted as "nothing outside the tool namespace"
    rather than as "nothing", so that adding a tool right cannot be mistaken for
    adding an operator one.
    """
    granted = permissions_for(AGENT_DEFAULT)
    assert granted == {TOOL_READ, TOOL_WRITE, TOOL_SEND}
    assert not {p for p in granted if not p.startswith("tool:")}


@pytest.mark.parametrize("role", sorted(BUILTIN_ROLE_PERMISSIONS))
def test_no_role_claims_a_permission_that_does_not_exist(role: str) -> None:
    unknown = permissions_for(role) - ALL_PERMISSIONS
    assert not unknown, f"{role} holds undefined permissions: {sorted(unknown)}"


def test_the_three_routes_that_admitted_operator_still_do() -> None:
    """POST /runs/{id}/{answer,cancel,message} were `require_role("org_admin",
    "operator")`. They map to run:control, so both roles keep them."""
    assert role_has(OPERATOR, RUN_CONTROL)
    assert role_has(ORG_ADMIN, RUN_CONTROL)


def test_deciding_an_approval_is_not_folded_into_managing_an_agent() -> None:
    """§5.5's whole point is that a SECOND person signs off. If whoever
    configures the agent automatically held the approval right, the gate would
    be decoration."""
    dept = permissions_for(DEPT_MANAGER)
    assert "agent:manage" in dept
    # They do both here -- a manager is a legitimate approver -- but the rights
    # are separable, which is what lets a tenant hand them to different people.
    assert APPROVAL_DECIDE != "agent:manage"
    assert not role_has(AUDITOR, APPROVAL_DECIDE)
