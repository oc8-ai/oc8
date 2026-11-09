"""Two people, and five hundred. The same feature has to be right at both ends.

The ask this slice answers is one sentence with a contradiction in it: *"the IT
admin must create and assign roles himself, as simple as possible -- and it has
to fit a two-user tenant without being overkill AND a 500+ user tenant and be
configurable."*

Those are two different tests, not one:

**Two people.** The correct amount of this feature for them is NONE. Not a
default role, not a starter set, not a migration that backfills something
sensible: `create_tenant` writes the same five `role` rows it always wrote, zero
`role_permission` rows and zero assignments, and both people resolve to the code
table byte for byte. Every earlier draft of this feature failed here rather than
at the top end -- a layer that seeds "sensible defaults" is a layer that changes
what a live tenant may do on the day it is installed, and the doctrine at
`permissions.py:18-24` exists because an authorization layer whose grants come
from rows locks every operator out the day the rows are missing.

**Five hundred people.** The correct amount is O(1) per POLICY, never per person.
*"Jede Teamleitung darf ab jetzt auch das Prüfprotokoll lesen"* is one `PUT
/roles/{id}` that lands for forty people on their next request -- not forty
writes, not a nightly sync, not a token refresh. That single property is the
entire reason this design has roles rather than per-person grants, and it is why
§5.4's snapshot cache is deliberately not built: a cache makes "on their next
request" false, and the window is exactly as long as the revocation is wrong for.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import sqlalchemy as sa
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from oc8 import models as m
from oc8.auth import Principal, get_identity_provider
from oc8.authz.permissions import (
    ALL_PERMISSIONS,
    APPROVAL,
    APPROVAL_DECIDE,
    AUDIT,
    AUDITOR,
    BUILTIN_ROLE_PERMISSIONS,
    CLARIFICATION_ANSWER,
    CLARIFICATION_VIEW,
    CONNECTION_OWN,
    COPILOT_USE,
    DEPT_MANAGER,
    MEMBER_ROLE,
    OPERATOR,
    ORG_ADMIN,
    VIEW,
    perm,
    permissions_for,
)
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def authority_for_principal(request: Request, db: Any, principal: Principal) -> Any:
    """`oc8.authz.authority.authority_for_principal`, imported at CALL time.

    A module-level import would be a COLLECTION error while that module does
    not exist, and pytest aborts the whole session on one of those -- so every
    test in the repository would be unrunnable until this slice lands. Delete
    this shim and import normally once `authz/authority.py` exists.
    """
    from oc8.authz.authority import authority_for_principal as _resolve

    return await _resolve(request, db, principal)


APPROVAL_VIEW = perm(APPROVAL, VIEW)
AUDIT_VIEW = perm(AUDIT, VIEW)


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _request() -> Request:
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    )


# ---------------------------------------------------------- the two-person company


async def test_a_tenant_that_configures_nothing_resolves_exactly_as_it_does_today(
    app_session: AppSessionFactory,
) -> None:
    """Byte-identical, for every built-in role name there is.

    Not "the same for org_admin", which is the case somebody would check: the
    `member` role resolving to the empty set through the resolver rather than
    through a fallback is the same guarantee, and the unrecognised name is the
    third. All six go through the new code path and all six must come out of it
    holding what `permissions_for` says, because on the day this lands that is
    every human on every tenant.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        for role in (*sorted(BUILTIN_ROLE_PERMISSIONS), "menber"):
            principal = Principal(
                subject=f"{role}-{uuid.uuid4()}", tenant_id=tenant, role=role, kind="operator"
            )
            authority = await authority_for_principal(_request(), db, principal)
            assert authority.tenant_wide == permissions_for(role), role
            assert authority.source == "token", role


async def test_the_two_person_company_is_handed_no_configuration_at_all(
    app_session: AppSessionFactory,
) -> None:
    """Zero rows on both new surfaces, and both people still administrators.

    A "starter role" would look thoughtful and would be a behaviour change
    shipped to every existing customer by a migration -- exactly the class of
    change this slice's migration writes no data rows in order to avoid.
    """
    tenant = uuid.uuid4()
    async with _http() as http:
        for subject in ("chefin", "kollege"):
            # Their first request is what mints their member row (the seat
            # slice's upsert). It must still leave `role_id` NULL.
            got = await http.get("/api/v1/me", headers=_headers(tenant, subject, ORG_ADMIN))
            assert got.status_code == 200, got.text

            for path in ("/api/v1/capas", "/api/v1/budgets", "/api/v1/agents"):
                assert (
                    await http.get(path, headers=_headers(tenant, subject, ORG_ADMIN))
                ).status_code == 200, f"{subject} lost {path}"

    async with app_session(tenant) as db:
        roles = (
            await db.execute(
                sa.select(sa.func.count()).select_from(m.Role).where(m.Role.tenant_id == tenant)
            )
        ).scalar_one()
        grants = (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(m.RolePermission)
                .where(m.RolePermission.tenant_id == tenant)
            )
        ).scalar_one()
        assigned = (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(m.OrgMember)
                .where(m.OrgMember.tenant_id == tenant, m.OrgMember.role_id.is_not(None))
            )
        ).scalar_one()

    assert roles == 0, "something invented a role for a tenant that asked for none"
    assert grants == 0
    assert assigned == 0


async def test_the_third_hire_costs_four_steps_and_touches_nobody_else(
    app_session: AppSessionFactory,
) -> None:
    """ "Der Dritte darf nur Angebote freigeben" -- fork, untick, name, assign.

    And the assertion that makes it a feature rather than a demo: the two people
    who were already there are unchanged afterwards. A role model where adding a
    third person's narrower role quietly narrows the other two is one nobody
    would use twice.
    """
    tenant = uuid.uuid4()
    admin = _headers(tenant, "chefin", ORG_ADMIN)

    async with _http() as http:
        created = await http.post(
            "/api/v1/roles",
            json={
                "name": "Freigabe",
                "description": "Darf Angebote freigeben und Rückfragen beantworten",
                "permissions": [
                    APPROVAL_VIEW,
                    APPROVAL_DECIDE,
                    CLARIFICATION_VIEW,
                    CLARIFICATION_ANSWER,
                ],
                "basedOn": OPERATOR,
            },
            headers=admin,
        )
        assert created.status_code == 201, created.text
        # `basedOn` pre-ticks boxes in the UI and is NOT stored: no parent, no
        # inheritance, the stored artefact is always a flat set.
        assert sorted(created.json()["permissions"]) == sorted(
            [APPROVAL_VIEW, APPROVAL_DECIDE, CLARIFICATION_VIEW, CLARIFICATION_ANSWER]
        )

        seen = await http.get("/api/v1/me", headers=_headers(tenant, "dritter", MEMBER_ROLE))
        assert seen.status_code == 200, seen.text
        assigned = await http.put(
            f"/api/v1/members/{seen.json()['memberId']}/role",
            json={"roleId": created.json()["id"]},
            headers=admin,
        )
        assert assigned.status_code < 300, assigned.text

        his = _headers(tenant, "dritter", MEMBER_ROLE)
        assert (await http.get("/api/v1/approvals", headers=his)).status_code == 200
        assert (await http.get("/api/v1/capas", headers=his)).status_code == 403

        # The other two are exactly where they were.
        for subject in ("chefin", "kollege"):
            theirs = _headers(tenant, subject, ORG_ADMIN)
            assert (await http.get("/api/v1/capas", headers=theirs)).status_code == 200
            me = await http.get("/api/v1/governance", headers=theirs)
            assert sorted(me.json()["callerPermissions"]) == sorted(ALL_PERMISSIONS), subject

    async with app_session(tenant) as db:
        untouched = (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(m.OrgMember)
                .where(m.OrgMember.tenant_id == tenant, m.OrgMember.role_id.is_not(None))
            )
        ).scalar_one()
    assert untouched == 1, "assigning one person a role wrote a role_id for somebody else"


# ------------------------------------------------------ the five-hundred-person company

FORTY = 40


async def test_one_role_edit_lands_for_forty_people_on_their_next_request(
    app_session: AppSessionFactory,
) -> None:
    """The O(1) that is the whole reason roles exist rather than per-person grants.

    Forty team leads hold *Abteilungsleitung*. The company decides they may all
    read the audit trail from now on; later it decides they may no longer decide
    approvals. Each is ONE `PUT /roles/{id}`, and each lands on every holder's
    next request with the token they are already carrying.

    Asserted for all forty at the RESOLVER, and end to end at the DOOR for a
    sample of three. Forty HTTP round trips per assertion would be forty times
    the runtime for a fortieth of the information: the interesting question is
    whether the write reached every holder, and that is a property of the
    resolver reading the role's rows rather than a cached copy of them.

    The removal half is the one that matters. Adding a permission late is a
    convenience; a revocation that lands in five minutes is not a revocation, and
    it is why there is no cache here to invalidate.
    """
    tenant = uuid.uuid4()
    admin = _headers(tenant, "it", ORG_ADMIN)
    leads = [f"lead-{i:03d}" for i in range(FORTY)]

    async with _http() as http:
        created = await http.post(
            "/api/v1/roles",
            json={
                "name": "Abteilungsleitung",
                "description": "Leitet eine Abteilung",
                "permissions": [APPROVAL_VIEW, APPROVAL_DECIDE, CLARIFICATION_VIEW],
            },
            headers=admin,
        )
        assert created.status_code == 201, created.text
        role_id = created.json()["id"]

        member_ids: list[str] = []
        for subject in leads:
            minted = await http.post(
                "/api/v1/members", json={"subject": subject, "displayName": subject}, headers=admin
            )
            assert minted.status_code in (200, 201), minted.text
            member_ids.append(minted.json()["id"])

        bulk = await http.post(
            "/api/v1/members/roles:bulk",
            json={"memberIds": member_ids, "roleId": role_id},
            headers=admin,
        )
        assert bulk.status_code < 300, bulk.text

        listed = await http.get("/api/v1/roles", headers=admin)
        assert listed.status_code == 200, listed.text
        row = next(r for r in listed.json() if r["id"] == role_id)
        assert row["holderCount"] == FORTY, (
            "the blast radius shown before Save is wrong, which is the number an "
            "administrator is being asked to decide on"
        )

        # One edit: they may all read the audit trail now.
        widened = await http.put(
            f"/api/v1/roles/{role_id}",
            json={
                "description": "Leitet eine Abteilung",
                "permissions": [
                    APPROVAL_VIEW,
                    APPROVAL_DECIDE,
                    CLARIFICATION_VIEW,
                    AUDIT_VIEW,
                ],
            },
            headers=admin,
        )
        assert widened.status_code < 300, widened.text

        for subject in leads[:3]:
            theirs = _headers(tenant, subject, MEMBER_ROLE)
            assert (await http.get("/api/v1/audit", headers=theirs)).status_code == 200

        await _assert_all_hold(app_session, tenant, leads, AUDIT_VIEW, True)

        # And one edit back: they may no longer decide.
        narrowed = await http.put(
            f"/api/v1/roles/{role_id}",
            json={
                "description": "Leitet eine Abteilung",
                "permissions": [APPROVAL_VIEW, CLARIFICATION_VIEW, AUDIT_VIEW],
            },
            headers=admin,
        )
        assert narrowed.status_code < 300, narrowed.text

        for subject in leads[:3]:
            theirs = _headers(tenant, subject, MEMBER_ROLE)
            # Still admitted to the queue, refused at the decision -- which is
            # the distinction a role is for.
            assert (await http.get("/api/v1/approvals", headers=theirs)).status_code == 200

    await _assert_all_hold(app_session, tenant, leads, APPROVAL_DECIDE, False)


async def _assert_all_hold(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    subjects: list[str],
    permission: str,
    expected: bool,
) -> None:
    async with app_session(tenant) as db:
        wrong = []
        for subject in subjects:
            principal = Principal(
                subject=subject, tenant_id=tenant, role=MEMBER_ROLE, kind="operator"
            )
            authority = await authority_for_principal(_request(), db, principal)
            if (permission in authority.tenant_wide) is not expected:
                wrong.append(subject)
    assert not wrong, (
        f"{len(wrong)} of {len(subjects)} holders did not see the edit "
        f"({permission!r} should be {expected}): {wrong[:5]}"
    )


async def test_a_promotion_is_two_writes_and_neither_touches_the_identity_provider(
    app_session: AppSessionFactory,
) -> None:
    """ "Anna leitet jetzt Vertrieb": one role assignment, one seat.

    Role says WHAT, seat says WHERE, and the reorganisation argument depends on
    them being separate rows: *Abteilungsleitung* is one role reused across
    twelve departments, so splitting Vertrieb into DACH and EMEA costs seat
    writes and ZERO role edits. If the role carried the department, a thirteenth
    department would cost a thirteenth role.
    """
    tenant = uuid.uuid4()
    admin = _headers(tenant, "it", ORG_ADMIN)
    async with app_session(tenant) as db:
        dach = m.Department(tenant_id=tenant, name="Vertrieb DACH")
        emea = m.Department(tenant_id=tenant, name="Vertrieb EMEA")
        db.add_all([dach, emea])
        await db.flush()
        dach_id, emea_id = dach.id, emea.id

    async with _http() as http:
        role = await http.post(
            "/api/v1/roles",
            json={
                "name": "Abteilungsleitung",
                "description": "",
                "permissions": [APPROVAL_VIEW, APPROVAL_DECIDE],
            },
            headers=admin,
        )
        assert role.status_code == 201, role.text

        anna = await http.post(
            "/api/v1/members", json={"subject": "anna", "displayName": "Anna"}, headers=admin
        )
        assert anna.status_code in (200, 201), anna.text
        anna_id = anna.json()["id"]

        assert (
            await http.put(
                f"/api/v1/members/{anna_id}/role",
                json={"roleId": role.json()["id"]},
                headers=admin,
            )
        ).status_code < 300
        for department_id in (dach_id, emea_id):
            granted = await http.put(
                f"/api/v1/members/{anna_id}/departments/{department_id}",
                json={"seatRole": "dept_approver"},
                headers=admin,
            )
            assert granted.status_code < 300, granted.text

        # The same ONE role, in two departments, with no second role anywhere.
        listed = await http.get("/api/v1/roles", headers=admin)
        assert [r["name"] for r in listed.json() if r["canEdit"]] == ["Abteilungsleitung"]

    async with app_session(tenant) as db:
        seats = (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(m.OrgMemberDepartment)
                .where(m.OrgMemberDepartment.tenant_id == tenant)
            )
        ).scalar_one()
    assert seats == 2


async def test_a_joiner_who_holds_nothing_is_not_locked_out_of_the_explanation(
    app_session: AppSessionFactory,
) -> None:
    """The 500-person tenant's default floor is `MEMBER_ROLE`, which holds only
    the tenant-wide `copilot:use` default -- nothing else.

    Between a person's first sign-in and being given a role and a seat, that
    person sees every screen empty. `/governance` is the one page that can tell
    them why, and it is the reason that route carries no permission: gating it
    would refuse precisely the caller it exists for. This is that promise, held
    against a caller who holds literally nothing.
    """
    tenant = uuid.uuid4()
    hers = _headers(tenant, "neue-kollegin", MEMBER_ROLE)
    async with _http() as http:
        explained = await http.get("/api/v1/governance", headers=hers)
        assert explained.status_code == 200, explained.text
        body = explained.json()
        assert body["callerPermissions"] == ["connection:own", "copilot:use"]
        assert body["seats"] == []
        assert body["callerRoleSource"] == "token"
        # Known, not a typo -- and those must be distinguishable, or the panel
        # tells 500 correctly-provisioned employees their token is broken.
        assert body["callerRoleIsKnown"] is True

        assert (await http.get("/api/v1/approvals", headers=hers)).status_code == 403
        assert (await http.get("/api/v1/capas", headers=hers)).status_code == 403

    # And nothing about the built-in ladder moved to make that possible.
    assert permissions_for(MEMBER_ROLE) == frozenset({CONNECTION_OWN, COPILOT_USE})
    assert permissions_for(OPERATOR) < permissions_for(DEPT_MANAGER) < permissions_for(ORG_ADMIN)
    assert permissions_for(AUDITOR) < permissions_for(ORG_ADMIN)
