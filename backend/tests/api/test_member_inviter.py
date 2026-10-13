from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.main import create_app
from oc8.workspace.inviter import InviteRefused, InviteResult, set_member_inviter
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


class _Recorder:
    def __init__(self, refuse: InviteRefused | None = None) -> None:
        self.invites: list[dict] = []
        self.removed: list[str] = []
        self.refuse = refuse

    async def invite(self, *, tenant_id, email, role, display_name, invited_by):
        if self.refuse:
            raise self.refuse
        self.invites.append({"email": email, "role": role, "by": invited_by.subject})
        return InviteResult(subject=f"kc-{email}")

    async def remove(self, *, tenant_id, subject):
        self.removed.append(subject)


@pytest.fixture
def inviter():
    recorder = _Recorder()
    set_member_inviter(recorder)
    yield recorder
    set_member_inviter(None)


def _headers(tenant: uuid.UUID, subject: str = "admin-sub", role: str = "org_admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)}"}


async def _post(tenant, body, headers=None):
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as client:
        return await client.post("/api/v1/members", json=body, headers=headers or _headers(tenant))


async def test_an_external_inviter_receives_the_email_and_role(inviter, app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    response = await _post(tenant, {"subject": "kim@acme.test", "displayName": "Kim", "role": "operator"})

    assert response.status_code == 201
    assert inviter.invites == [{"email": "kim@acme.test", "role": "operator", "by": "admin-sub"}]
    async with app_session(tenant) as db:
        member = (await db.execute(select(m.OrgMember).where(m.OrgMember.subject == "kc-kim@acme.test"))).scalar_one()
    assert member.password_hash is None


async def test_a_refusal_comes_back_as_409_with_its_reason() -> None:
    set_member_inviter(
        _Recorder(
            InviteRefused(
                409, "limit_reached", "This team is full.", {"actionLabel": "Upgrade", "actionUrl": "/upgrade"}
            )
        )
    )
    try:
        response = await _post(uuid.uuid4(), {"subject": "kim@acme.test"})
    finally:
        set_member_inviter(None)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "limit_reached", "message": "This team is full.", "actionLabel": "Upgrade", "actionUrl": "/upgrade",
    }


async def test_without_an_inviter_members_are_created_as_before() -> None:
    response = await _post(uuid.uuid4(), {"subject": "kim@acme.test", "displayName": "Kim"})
    assert response.status_code == 201
    assert response.json()["subject"] == "kim@acme.test"


async def test_auth_config_says_how_members_are_invited(inviter) -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as client:
        assert (await client.get("/api/v1/auth/config")).json()["memberInvites"] == "external"
    set_member_inviter(None)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as client:
        assert (await client.get("/api/v1/auth/config")).json()["memberInvites"] == "local"


def _subject_uuid(subject: str) -> uuid.UUID:
    from oc8.authz.scope import subject_uuid_for

    return subject_uuid_for(subject)


async def test_deleting_a_member_asks_the_inviter_to_remove_them(
    inviter, app_session: AppSessionFactory
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant, subject="kc-x", subject_uuid=_subject_uuid("kc-x"), display_name="X"
        )
        db.add(member)
        await db.flush()
        member_id = member.id

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as client:
        response = await client.delete(f"/api/v1/members/{member_id}", headers=_headers(tenant))

    assert response.status_code == 204
    assert inviter.removed == ["kc-x"]


async def test_with_an_inviter_no_local_password_is_created(inviter) -> None:
    # A password would make a local login past the deployment's own sign-in
    # service; with an inviter registered, people sign in only there.
    response = await _post(uuid.uuid4(), {"subject": "kim@acme.test", "password": "a long password"})

    assert response.status_code == 422
    assert inviter.invites == []


async def test_with_an_inviter_no_member_gets_a_local_password(inviter, app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member = m.OrgMember(
            tenant_id=tenant, subject="kc-y", subject_uuid=_subject_uuid("kc-y"), display_name="Y"
        )
        db.add(member)
        await db.flush()
        member_id = member.id

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as client:
        set_password = await client.put(
            f"/api/v1/members/{member_id}/password",
            json={"password": "a long password"},
            headers=_headers(tenant),
        )
        reset_link = await client.post(f"/api/v1/members/{member_id}/password-reset", headers=_headers(tenant))
        rename = await client.put(
            f"/api/v1/members/{member_id}/subject",
            json={"subject": "someone-else@acme.test"},
            headers=_headers(tenant),
        )

    assert set_password.status_code == 422
    assert reset_link.status_code == 422
    assert rename.status_code == 422
    async with app_session(tenant) as db:
        row = (await db.execute(select(m.OrgMember).where(m.OrgMember.id == member_id))).scalar_one()
    assert row.password_hash is None
