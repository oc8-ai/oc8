from __future__ import annotations

from oc8.authz.catalog import describe
from oc8.authz.permissions import (
    ALL_PERMISSIONS,
    BUILTIN_ROLE_PERMISSIONS,
    CONNECTION_OWN,
    DELEGATABLE_PERMISSIONS,
    permissions_for,
)


def test_every_human_role_may_connect_its_own_accounts() -> None:
    assert CONNECTION_OWN == "connection:own"
    assert CONNECTION_OWN in ALL_PERMISSIONS
    for role in ("member", "operator", "dept_manager", "auditor", "org_admin"):
        assert CONNECTION_OWN in permissions_for(role), role
    assert CONNECTION_OWN not in BUILTIN_ROLE_PERMISSIONS["agent_default"]


def test_a_tenant_role_may_carry_it_and_it_is_explained() -> None:
    assert CONNECTION_OWN in DELEGATABLE_PERMISSIONS
    info = describe(CONNECTION_OWN)
    assert info.label_de == "Eigene Verbindungen verwalten"
    assert info.label == "Manage own connections"
