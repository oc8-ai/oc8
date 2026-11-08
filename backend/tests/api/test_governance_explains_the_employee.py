"""`/governance` is the page the workspace's "you hold no seat" empty state links
to, labelled "What may I do?". It told the entire population this slice creates
that their role does not exist.

`member` was deliberately absent from `BUILTIN_ROLE_PERMISSIONS`, so
`callerRoleIsKnown` was False for every employee and `src/routes/governance.tsx`
rendered, under an amber warning triangle:

    "This deployment does not define a role called 'member', so it grants
     nothing at all. That is why other screens appear empty. Map your identity
     provider's group to one of the roles below."

The remedy it prescribes -- map the group to `operator`, `dept_manager` or
`auditor` -- is the tenant-wide grant this slice exists to avoid. Meanwhile
`app-shell.tsx` labels the same role "Employee / Mitarbeiter" in the header chip.

The half that was supposed to say the true thing was on the wire and rendered
nowhere: `GovernanceDTO` gained `seats` and `seatRoles` for precisely this
sentence (§5: "so the screen that explains a refusal can say 'you hold no
seat'") and `grep seats src/routes/governance.tsx` returned nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import (
    BUILTIN_ROLE_PERMISSIONS,
    CONNECTION_OWN,
    COPILOT_USE,
    MEMBER_ROLE,
    SEAT_APPROVER,
    permissions_for,
    role_has,
)
from oc8.authz.scope import subject_uuid_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def test_member_is_a_role_the_deployment_admits_to_defining() -> None:
    """`member` must be a role `GET /governance` can tell from a typo. It was
    invitable (via `POST /members`, and formerly the now-removed `oc8 tenant
    invite --role`) and undefined at the same time, which is the contradiction
    the screen rendered.
    """
    assert MEMBER_ROLE in BUILTIN_ROLE_PERMISSIONS, (
        f"{MEMBER_ROLE!r} can be assigned and is not defined, so /governance tells "
        "everybody holding it that their role does not exist"
    )


def test_defining_it_granted_nothing_beyond_the_universal_copilot_use_default() -> None:
    """The whole risk of the fix, in one assertion: `member` is a key now, and it
    grants exactly the tenant-wide `copilot:use` default every human role gets --
    nothing role-specific -- while an unmapped role name still grants nothing at
    all. That asymmetry is deliberate; it is only the defined-and-empty-vs-absent
    equivalence that no longer holds.
    """
    from oc8.authz.permissions import tool_rights_for_role

    assert permissions_for(MEMBER_ROLE) == frozenset({CONNECTION_OWN, COPILOT_USE})
    assert permissions_for("menber") == frozenset()
    assert tool_rights_for_role(MEMBER_ROLE) == frozenset()
    for permission in ("approval:decide", "run:view", "agent:manage", "member:manage"):
        assert not role_has(MEMBER_ROLE, permission), permission


async def test_an_employee_is_told_where_he_stands_and_not_that_he_is_a_typo(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Vertrieb")
        db.add(dept)
        await db.flush()
        member = m.OrgMember(
            tenant_id=tenant,
            subject="hos",
            subject_uuid=subject_uuid_for("hos"),
            display_name="Head of Sales",
        )
        db.add(member)
        await db.flush()
        db.add(
            m.OrgMemberDepartment(
                tenant_id=tenant,
                member_id=member.id,
                department_id=dept.id,
                seat_role=SEAT_APPROVER,
            )
        )

    async with _http() as http:
        got = await http.get("/api/v1/governance", headers=_headers(tenant, "hos", MEMBER_ROLE))
        assert got.status_code == 200, got.text
        body = got.json()

    assert body["callerRole"] == MEMBER_ROLE
    assert body["callerRoleIsKnown"] is True, (
        "the screen leads with this flag and its false branch tells the caller to "
        "have his identity provider remapped onto a company-wide role"
    )
    assert body["callerPermissions"] == ["connection:own", "copilot:use"], (
        "defining the role must not grant anything beyond the universal copilot:use and connection:own defaults"
    )
    # The sentence the design promised the screen could say, with the data to
    # say it: which department, at which seat role, carrying which permissions.
    assert [s["departmentName"] for s in body["seats"]] == ["Vertrieb"]
    assert body["seats"][0]["seatRole"] == SEAT_APPROVER
    assert set(body["seatRoles"][SEAT_APPROVER]) == {
        "approval:view",
        "approval:decide",
        "clarification:view",
        "clarification:answer",
        # `agent:view`/`department:view` graduated onto the view level for both
        # seat roles (department-scoped-agent-authority design, decision 2's
        # read side) -- not gated on `agent_manage`, since read needs no toggle.
        "agent:view",
        "department:view",
    }
    # And `member` is in the matrix as a column, holding nothing, so the model the
    # screen renders contains the caller's own role rather than four others.
    #
    # Asserted field by field rather than as a whole dict. `RoleDTO` gained `kind`
    # and `id` when human and agent roles were split into two lists, and an
    # equality against a literal makes every future field of a diagnostic payload
    # a test failure in a file that is about something else -- which is how a real
    # regression comes to be fixed by editing the literal.
    listed = [r for r in body["roles"] if r["name"] == MEMBER_ROLE]
    assert len(listed) == 1
    assert listed[0]["builtin"] is True
    assert listed[0]["permissions"] == ["connection:own", "copilot:use"]
    # It is in the HUMAN list, and that is the half of this worth pinning: the
    # employee's own role must not have been sorted into the agents' table.
    assert listed[0]["kind"] == "human"


async def test_an_unmapped_role_is_still_reported_as_unmapped(
    app_session: AppSessionFactory,
) -> None:
    """The other side of the same flag, which the fix must not blunt: a typo in
    the identity provider's mapping still has to be visible AS a typo, because
    the symptom is otherwise a product that looks broken.
    """
    tenant = uuid.uuid4()
    async with _http() as http:
        body = (
            await http.get("/api/v1/governance", headers=_headers(tenant, "who", "menber"))
        ).json()
    assert body["callerRole"] == "menber"
    assert body["callerRoleIsKnown"] is False
    assert body["seats"] == []


async def test_a_seatless_employee_is_told_he_holds_no_seat(
    app_session: AppSessionFactory,
) -> None:
    """The refusal this endpoint exists to explain. `seats: []` plus a known role
    that grants nothing is exactly "nobody has put you in a department yet", and
    it must not be reachable only as a blank screen.
    """
    tenant = uuid.uuid4()
    async with _http() as http:
        body = (
            await http.get("/api/v1/governance", headers=_headers(tenant, "nobody", MEMBER_ROLE))
        ).json()
    assert body["callerRoleIsKnown"] is True
    assert body["callerPermissions"] == ["connection:own", "copilot:use"]
    assert body["seats"] == []
