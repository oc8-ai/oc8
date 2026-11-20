"""The one state that is a genuine misconfiguration, and the screen that has to
say so.

Three ways a person can hold nothing company-wide, and they need three different
sentences because they send the reader to three different places:

* their token role is `member` -- **correct**, by design, their authority is
  their seats;
* their token role is not one this deployment defines -- **the identity
  provider**, someone must map the group;
* a role WAS assigned to them and that row cannot grant -- **`org_member.role_id`**,
  one nullable column, and nothing about their token is wrong at all.

The third was rendered as the first. `Authority.role` is deliberately left NULL
for an unusable assignment -- a name printed beside an empty set reads as "the
role is empty" rather than "the role is unusable" -- so `callerTenantRoleName`
was null, so the screen fell through to *"your token carries `org_admin`, which
holds nothing company-wide BY DESIGN"*: false in both halves, on the one page in
the product whose entire job is to be believed about a refusal. The frontend even
carried a branch written for this case; with no field to key it off, that branch
could never fire.

Both routes into the state are exercised, because the resolver refuses them on
two different lines and a fix that only covered one would still be wrong half the
time. Both are written directly, which is how they occur: `POST /roles` refuses an
agent-kind assignment and `DELETE` refuses a role somebody holds, so an unusable
assignment arrives by restore, by psql, or from an importer written next year --
which is exactly the arrival the resolver's own docstring is about.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import AGENT_DEFAULT, MEMBER_ROLE, ORG_ADMIN, permissions_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str = ORG_ADMIN) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _assign_directly(
    app_session: AppSessionFactory, tenant: uuid.UUID, subject: str, role: m.Role
) -> None:
    async with app_session(tenant) as db:
        db.add(role)
        await db.flush()
        await db.execute(
            sa.update(m.OrgMember)
            .where(m.OrgMember.tenant_id == tenant, m.OrgMember.subject == subject)
            .values(role_id=role.id)
        )


@pytest.mark.parametrize(
    ("label", "make_role"),
    [
        (
            "agent-kind",
            lambda tenant: m.Role(tenant_id=tenant, name=AGENT_DEFAULT, builtin=True, kind="agent"),
        ),
        (
            "soft-deleted",
            lambda tenant: m.Role(
                tenant_id=tenant,
                name="Geloescht",
                builtin=False,
                kind="human",
                deleted_at=sa.func.now(),
            ),
        ),
    ],
)
async def test_an_assignment_that_cannot_grant_is_named_as_the_reason(
    app_session: AppSessionFactory, label: str, make_role: object
) -> None:
    """The caller holds nothing, and the screen says WHY -- not "by design"."""
    tenant = uuid.uuid4()
    headers = _headers(tenant, "anna")

    async with _http() as http:
        # Her first request mints her member row, exactly as a real employee's is.
        assert (await http.get("/api/v1/me", headers=headers)).status_code == 200

    await _assign_directly(app_session, tenant, "anna", make_role(tenant))  # type: ignore[operator]

    async with _http() as http:
        got = await http.get("/api/v1/governance", headers=headers)

    assert got.status_code == 200, got.text
    body = got.json()

    # The refusal itself is unchanged and still fails closed.
    assert body["callerPermissions"] == []
    assert body["callerRoleSource"] == "assigned"
    # Still no name: an unusable role must not be printed as though it were an
    # empty one. That rule is what made this state indistinguishable, so it is
    # pinned here rather than left to be "simplified" away.
    assert body["callerTenantRoleName"] is None

    # And the new fact, which is the whole point: the reason is the ASSIGNMENT.
    assert body["callerRoleUnusable"] is True, (
        f"a {label} assignment leaves the caller with nothing and the screen cannot "
        "say why: it renders 'your token holds nothing company-wide by design', "
        "which is false for org_admin and points the reader at seats"
    )
    # The token is not the culprit and the payload must not imply it is.
    assert body["callerRole"] == ORG_ADMIN
    assert body["callerRoleIsKnown"] is True
    assert len(permissions_for(ORG_ADMIN)) == len(body["permissions"])
    assert body["authorityUnavailable"] is False


async def test_the_flag_is_off_for_every_caller_who_is_not_broken(
    app_session: AppSessionFactory,
) -> None:
    """The other three states, so the new sentence cannot start appearing under
    people who are configured correctly.

    An amber warning shown to a correctly-provisioned employee is the same defect
    in the other direction, and it is the one slice 1 had to add a third state to
    fix -- 500 people told their token was broken.
    """
    tenant = uuid.uuid4()

    async with _http() as http:
        # 1. An administrator on a tenant that has configured nothing.
        admin = _headers(tenant, "chefin", ORG_ADMIN)
        assert (await http.get("/api/v1/me", headers=admin)).status_code == 200
        body = (await http.get("/api/v1/governance", headers=admin)).json()
        assert body["callerRoleUnusable"] is False
        assert body["callerRoleSource"] == "token"
        assert sorted(body["callerPermissions"]) == sorted(permissions_for(ORG_ADMIN))

        # 2. An ordinary employee: holds only the universal copilot:use default
        #    tenant-wide, nothing role-specific.
        employee = _headers(tenant, "mitarbeiterin", MEMBER_ROLE)
        assert (await http.get("/api/v1/me", headers=employee)).status_code == 200
        body = (await http.get("/api/v1/governance", headers=employee)).json()
        assert body["callerPermissions"] == ["connection:own", "copilot:use"]
        assert body["callerRoleUnusable"] is False, (
            "a correctly-provisioned employee must not be told his assignment is broken"
        )
        assert body["callerRoleIsKnown"] is True

        # 3. A token role nobody mapped: also not an assignment problem.
        stranger = _headers(tenant, "fremde", "menber")
        assert (await http.get("/api/v1/me", headers=stranger)).status_code == 200
        body = (await http.get("/api/v1/governance", headers=stranger)).json()
        assert body["callerRoleUnusable"] is False
        assert body["callerRoleIsKnown"] is False

    # 4. And a WORKING assignment, which is the state most easily confused with a
    #    broken one: it grants little, it grants it from a row, and it is fine.
    async with _http() as http:
        anna = _headers(tenant, "anna", ORG_ADMIN)
        assert (await http.get("/api/v1/me", headers=anna)).status_code == 200
        members = (await http.get("/api/v1/members", headers=_headers(tenant, "chefin"))).json()[
            "items"
        ]
        member_id = next(x["id"] for x in members if x["subject"] == "anna")
        created = await http.post(
            "/api/v1/roles",
            headers=_headers(tenant, "chefin"),
            json={"name": "Nur lesen", "permissions": ["approval:view"]},
        )
        assert created.status_code == 201, created.text
        assigned = await http.put(
            f"/api/v1/members/{member_id}/role",
            headers=_headers(tenant, "chefin"),
            json={"roleId": created.json()["id"]},
        )
        assert assigned.status_code == 200, assigned.text

        body = (await http.get("/api/v1/governance", headers=anna)).json()
        assert body["callerRoleUnusable"] is False
        assert body["callerRoleSource"] == "assigned"
        assert body["callerTenantRoleName"] == "Nur lesen"
        assert body["callerPermissions"] == ["approval:view"]
