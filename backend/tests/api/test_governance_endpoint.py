"""The permission model, made readable to the people it refuses.

The layer decides correctly and explains nothing: `403 requires permission:
plugin:manage` does not say which role holds it, nor whether the caller's own
role is one the deployment recognises. The unrecognised case is the expensive
one -- every screen comes back empty, which reads as a broken product rather
than as an IdP group nobody mapped.

Once roles can be tenant-defined this endpoint has to answer three more things
without becoming the one place a seatless caller can read the tenant's authority
map:

* **the caller's OWN resolved authority and its SOURCE** -- "aus deiner Rolle
  `operator`" versus "aus der Rolle *Freigabe Vertrieb*, die dir zugewiesen
  wurde". `callerPermissions` was `permissions_for(principal.role)`, which for
  exactly the population this slice creates renders "holds 0 of 52";
* **nothing at all about anybody else's** -- the tenant's role catalogue lives at
  `GET /roles`, behind `role:view`. This route is `unguarded()` because 12c40e6
  recorded the bug of gating it, and that reason string is only true while it
  enumerates nothing;
* **human and agent roles as two lists**, because today `agent_default` is the
  leftmost column of the governance screen with `tool:send` as a row beside
  `plugin:manage`.

And it must survive the database being unavailable, because the screen that
explains a refusal must never be the first thing to die.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import (
    AGENT_DEFAULT,
    ALL_PERMISSIONS,
    APPROVAL,
    APPROVAL_DECIDE,
    CLARIFICATION_ANSWER,
    CLARIFICATION_VIEW,
    ORG_ADMIN,
    VIEW,
    perm,
)
from oc8.authz.scope import subject_uuid_for
from oc8.main import create_app
from tests.conftest import AppSessionFactory

FREIGABE = frozenset(
    {perm(APPROVAL, VIEW), APPROVAL_DECIDE, CLARIFICATION_VIEW, CLARIFICATION_ANSWER}
)


def _headers(tenant: uuid.UUID, role: str, subject: str = "s") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _get(role: str) -> tuple[int, dict[str, Any]]:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/governance", headers=_headers(tenant, role))
            return r.status_code, (r.json() if r.status_code == 200 else {})


async def _get_as(tenant: uuid.UUID, subject: str, role: str) -> tuple[int, dict[str, Any]]:
    async with _http() as http:
        r = await http.get("/api/v1/governance", headers=_headers(tenant, role, subject))
        return r.status_code, (r.json() if r.status_code == 200 else {})


async def _assigned_role(
    app_session: AppSessionFactory,
    tenant: uuid.UUID,
    subject: str,
    name: str,
    permissions: frozenset[str] = FREIGABE,
) -> uuid.UUID:
    async with app_session(tenant) as db:
        role = m.Role(tenant_id=tenant, name=name, kind="human")
        db.add(role)
        await db.flush()
        for permission in sorted(permissions):
            db.add(m.RolePermission(tenant_id=tenant, role_id=role.id, permission=permission))
        db.add(
            m.OrgMember(
                tenant_id=tenant,
                subject=subject,
                subject_uuid=subject_uuid_for(subject),
                display_name=subject,
                role_id=role.id,
            )
        )
        await db.flush()
        return role.id


async def test_it_returns_the_whole_model_not_just_the_callers_slice() -> None:
    """Hiding the other BUILT-IN roles would buy nothing -- the same table is in
    the source -- while making every 403 undiagnosable.

    `agent_default` is deliberately not among them any more; see
    `test_agent_roles_are_a_separate_list_from_human_roles`.
    """
    status, body = await _get("operator")

    assert status == 200
    assert sorted(body["permissions"]) == sorted(ALL_PERMISSIONS)
    names = {r["name"] for r in body["roles"]}
    assert {"org_admin", "dept_manager", "operator", "auditor"} <= names


async def test_an_operator_sees_what_it_may_do_and_what_it_may_not() -> None:
    status, body = await _get("operator")

    assert status == 200
    assert "run:start" in body["callerPermissions"]
    assert "plugin:manage" not in body["callerPermissions"]
    assert body["callerRoleIsKnown"] is True


async def test_an_unrecognised_role_is_shown_as_itself_not_as_unknown() -> None:
    """The whole reason this endpoint exists, and the reason it carries no
    permission of its own.

    The first version gated it on `settings:view` -- which an unrecognised role
    does not hold, so it refused precisely the caller it was built for. A token
    whose role nobody mapped holds nothing and sees every screen empty; the
    answer that ends that call is "your token says `menber`, which this
    deployment does not define", and a 403 does not give it.
    """
    status, body = await _get("menber")

    assert status == 200
    assert body["callerRole"] == "menber"
    assert body["callerRoleIsKnown"] is False
    assert body["callerPermissions"] == []
    # And the model is still there, so they can see what the real roles are.
    assert {r["name"] for r in body["roles"]} >= {"operator", "org_admin"}


async def test_an_auditor_can_read_the_model() -> None:
    """An auditor's job is to check who may do what; being unable to see the
    grant table would make that impossible."""
    status, body = await _get("auditor")

    assert status == 200
    assert "audit:view" in body["callerPermissions"]
    assert not [p for p in body["callerPermissions"] if p.endswith(":manage")]


async def test_the_caller_slice_agrees_with_the_role_table() -> None:
    """Two paths to the same answer must not drift: what the caller is told they
    hold has to be exactly what the listed role holds."""
    status, body = await _get("dept_manager")

    assert status == 200
    listed = next(r for r in body["roles"] if r["name"] == "dept_manager")
    assert sorted(listed["permissions"]) == sorted(body["callerPermissions"])


# ------------------------------------------------ what this slice adds, and does not


async def test_an_unrecognised_role_still_gets_200() -> None:
    """The existing guarantee, restated as its own assertion because the endpoint
    now reads the database.

    `test_an_unrecognised_role_is_shown_as_itself_not_as_unknown` above must keep
    passing verbatim; this one exists so that the reason it must -- a 200 with the
    static model, for the one caller who holds nothing anywhere -- is not
    something a future reader has to infer from a longer test.
    """
    status, body = await _get("menber")

    assert status == 200
    assert body["callerRole"] == "menber"
    assert sorted(body["permissions"]) == sorted(ALL_PERMISSIONS)


async def test_agent_roles_are_a_separate_list_from_human_roles() -> None:
    """The commit's visible proof that the separation happened.

    One table holds two populations and the screen conflated them: the backend
    sorts built-in roles by name, so `agent_default` -- whose entire content is
    `tool:read`, `tool:write`, `tool:send` -- is the LEFTMOST column of the
    governance matrix, sitting beside `plugin:manage` as though a person could be
    given it. They describe different things about different actors and belong in
    different tables on the screen.
    """
    status, body = await _get("operator")

    assert status == 200
    human = {r["name"] for r in body["roles"]}
    agents = {r["name"] for r in body["agentRoles"]}

    assert AGENT_DEFAULT in agents
    assert AGENT_DEFAULT not in human, (
        "the agent's own role is still offered next to the human ones, so the "
        "screen still reads as though a person could be given `tool:send`"
    )
    assert not human & agents
    assert all(r["kind"] == "human" for r in body["roles"])
    assert all(r["kind"] == "agent" for r in body["agentRoles"])


async def test_governance_names_the_source_of_the_callers_authority(
    app_session: AppSessionFactory,
) -> None:
    """ "Aus deiner Rolle `operator`" versus "aus der Rolle *Freigabe Vertrieb*,
    die dir zugewiesen wurde".

    Without the source, the panel that answers "why is my screen empty" says the
    same thing to somebody whose IdP group nobody mapped and to somebody an
    administrator deliberately narrowed yesterday -- and only one of those is
    fixed by talking to the administrator.

    `callerPermissions` is the RESOLVED set, not `permissions_for(token.role)`:
    the caller below carries an `org_admin` token, and rendering all 52 to
    somebody who is refused at 48 of them is the screen contradicting the gate.
    """
    tenant = uuid.uuid4()
    subject = f"anna-{uuid.uuid4()}"

    status, before = await _get_as(tenant, subject, ORG_ADMIN)
    assert status == 200
    assert before["callerRoleSource"] == "token"
    assert before["callerTenantRoleName"] is None
    assert sorted(before["callerPermissions"]) == sorted(ALL_PERMISSIONS)

    await _assigned_role(app_session, tenant, subject, "Freigabe Vertrieb")

    status, after = await _get_as(tenant, subject, ORG_ADMIN)
    assert status == 200
    assert after["callerRoleSource"] == "assigned"
    assert after["callerTenantRoleName"] == "Freigabe Vertrieb"
    assert after["callerRoleKind"] == "human"
    assert sorted(after["callerPermissions"]) == sorted(FREIGABE)
    # Still their own token's role name, unnormalised -- that answer never stops
    # being the one that ends a support call.
    assert after["callerRole"] == ORG_ADMIN


async def test_governance_does_not_enumerate_other_roles_of_the_tenant(
    app_session: AppSessionFactory,
) -> None:
    """What keeps the `unguarded()` reason literally true.

    This route carries no permission because gating it locks out precisely the
    caller who needs it. That is only defensible while it returns the caller's
    OWN authority and nothing about anyone else's -- otherwise it is the one
    endpoint on which a seatless employee can read the whole tenant's authority
    map, which is a better reconnaissance target than most of what it protects.
    The catalogue lives at `GET /roles`, behind `role:view`.
    """
    tenant = uuid.uuid4()
    await _assigned_role(app_session, tenant, "anna", "Freigabe Vertrieb")
    await _assigned_role(app_session, tenant, "bea", "Abteilungsleitung DACH")

    status, body = await _get_as(tenant, "carla", "menber")

    assert status == 200
    rendered = repr(body)
    assert "Freigabe Vertrieb" not in rendered
    assert "Abteilungsleitung DACH" not in rendered
    assert "anna" not in rendered and "bea" not in rendered
    assert body["callerRoleSource"] == "token"
    assert body["callerPermissions"] == []


async def test_governance_degrades_rather_than_500s_when_the_authority_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The screen that explains a refusal must never be the first thing to die.

    And the branch is not a `try/except` around the read: `get_db` yields inside
    `tenant_session`, which COMMITS on the way out, so a failed statement leaves
    the Postgres transaction aborted and the commit raises after the handler has
    already returned its careful fallback. The read has to run inside
    `db.begin_nested()`, and the assertion that distinguishes the two
    implementations is simply that the response arrives at all.

    The failure injected here is a real Postgres error rather than a Python one,
    for exactly that reason: a `ValueError` would leave the transaction healthy
    and a plain `try/except` would pass this test.
    """
    import oc8.api.v1.governance as governance

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        db = next(
            (a for a in (*args, *kwargs.values()) if isinstance(a, AsyncSession)),
            None,
        )
        assert db is not None, "the resolver is no longer called with the session"
        await db.execute(text("SELECT * FROM a_table_that_is_not_there"))
        raise AssertionError("unreachable")  # pragma: no cover

    monkeypatch.setattr(governance, "authority_for_principal", _explode, raising=True)

    status, body = await _get("operator")

    assert status == 200, "the diagnostic screen 500s when the database blinks"
    assert body["authorityUnavailable"] is True
    assert sorted(body["permissions"]) == sorted(ALL_PERMISSIONS)
    assert {r["name"] for r in body["roles"]} >= {"operator", "org_admin"}
