"""The seat-administration doors, past the two cases §8 names.

§8 asserts that only an `org_admin` may grant a seat (test 25) and that revoking
one bites on the next request (test 26). Everything below is a way this surface
can be wrong while both of those still pass -- and each one is authority, not
cosmetics: a promotion that writes a second live row, a grant into a department
that does not exist, a defaulted POST body that quietly strips a CEO of his
company-wide view, a members list that crosses a tenant boundary.
"""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_APPROVER, SEAT_VIEWER
from oc8.config import Settings
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """The invite tests' SMTP-configured branch resolves a secret-kind
    `password` field through the real vault, which needs a KEK (mirrors
    tests/api/test_auth_self_service.py's identical fixture)."""
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _subject_uuid(subject: str) -> uuid.UUID:
    try:
        return uuid.UUID(subject)
    except ValueError:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


async def _configure_smtp(db: AsyncSession, tenant: uuid.UUID) -> None:
    """Point the tenant's `active_smtp_credential_id` at a real credential --
    creating the tenant's `Organization` row first, since `resolve_smtp_config`
    reads it. Mirrors `test_auth_self_service.py`'s helper of the same name."""
    org = await db.get(m.Organization, tenant)
    if org is None:
        org = m.Organization(id=tenant, slug=str(tenant), name="t", settings={})
        db.add(org)
        await db.flush()
    credential = await create_credential(
        db,
        tenant_id=tenant,
        name="smtp",
        credential_type="smtp_server",
        field_values={"host": "smtp.example.com", "port": "587", "from_address": "a@b.com"},
    )
    org.settings = {**org.settings, "active_smtp_credential_id": str(credential.id)}
    await db.flush()


class _Office:
    tenant: uuid.UUID
    sales: uuid.UUID
    approval: uuid.UUID
    hos: uuid.UUID


async def _office(app_session: AppSessionFactory) -> _Office:
    office = _Office()
    office.tenant = uuid.uuid4()
    async with app_session(office.tenant) as db:
        sales = m.Department(tenant_id=office.tenant, name="Vertrieb")
        db.add(sales)
        await db.flush()
        agent = m.Agent(tenant_id=office.tenant, department_id=sales.id, name="Nora")
        db.add(agent)
        await db.flush()
        approval = m.ApprovalRequest(
            tenant_id=office.tenant,
            agent_id=agent.id,
            department_id=sales.id,
            action_type="tool_send",
            status="pending",
            title="Angebot Gartenholz GmbH",
            detail="",
            amount_text="4.320,00 EUR",
            payload={"tool": "odoo.send_quotation", "arguments": {}},
        )
        db.add(approval)
        member = m.OrgMember(
            tenant_id=office.tenant,
            subject="hos",
            subject_uuid=_subject_uuid("hos"),
            display_name="Head of Sales",
        )
        db.add(member)
        await db.flush()
        office.sales, office.approval, office.hos = sales.id, approval.id, member.id
    return office


async def _seat_rows(
    app_session: AppSessionFactory, tenant: uuid.UUID, member_id: uuid.UUID
) -> list[Any]:
    async with app_session(tenant) as db:
        return list(
            (
                await db.execute(
                    select(m.OrgMemberDepartment).where(
                        m.OrgMemberDepartment.member_id == member_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_promoting_a_seat_leaves_exactly_one_live_row_and_real_authority(
    app_session: AppSessionFactory,
) -> None:
    """A promotion is a revoke followed by a grant, in that order.

    `uq_org_member_department_live` is partial on `revoked_at IS NULL`, so the
    obvious implementation -- insert the new seat -- raises a unique violation,
    and the second-most obvious one -- update the row in place -- loses the answer
    to "and until when was he only a viewer". Both halves are asserted: the row
    count AND the authority, because a seat table that looks right while the
    decide door still refuses him is the same outage with better bookkeeping.
    """
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    his = _headers(office.tenant, "hos", "member")
    seat = f"/api/v1/members/{office.hos}/departments/{office.sales}"

    async with _http() as http:
        viewer = await http.put(seat, json={"seatRole": SEAT_VIEWER}, headers=admin)
        assert viewer.status_code < 300, viewer.text
        assert [s["seatRole"] for s in viewer.json()["seats"]] == [SEAT_VIEWER]

        # A viewer reads the queue and cannot empty it.
        assert (await http.get("/api/v1/approvals", headers=his)).status_code == 200
        refused = await http.post(
            f"/api/v1/approvals/{office.approval}/decision",
            json={"decision": "approve"},
            headers=his,
        )
        assert refused.status_code == 403, refused.text

        promoted = await http.put(seat, json={"seatRole": SEAT_APPROVER}, headers=admin)
        assert promoted.status_code < 300, promoted.text
        assert [s["seatRole"] for s in promoted.json()["seats"]] == [SEAT_APPROVER]

        # Same token, next request.
        decided = await http.post(
            f"/api/v1/approvals/{office.approval}/decision",
            json={"decision": "approve"},
            headers=his,
        )
        assert decided.status_code == 200, decided.text

    rows = await _seat_rows(app_session, office.tenant, office.hos)
    live = [r for r in rows if r.revoked_at is None]
    assert len(live) == 1 and live[0].seat_role == SEAT_APPROVER
    revoked = [r for r in rows if r.revoked_at is not None]
    assert len(revoked) == 1 and revoked[0].seat_role == SEAT_VIEWER, (
        "the viewer seat was overwritten instead of revoked; 'who could see this, "
        "and until when' is no longer answerable"
    )


async def test_re_granting_the_same_seat_does_not_churn_the_row(
    app_session: AppSessionFactory,
) -> None:
    """An idempotent PUT is a PUT. Re-issuing an identical seat must not write a
    revoke/grant pair for an act nobody performed -- an audit trail that records
    two authority changes every time somebody clicks the same button twice is one
    nobody can read a real change out of."""
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    seat = f"/api/v1/members/{office.hos}/departments/{office.sales}"

    async with _http() as http:
        first = await http.put(seat, json={"seatRole": SEAT_APPROVER}, headers=admin)
        assert first.status_code < 300, first.text
        again = await http.put(seat, json={"seatRole": SEAT_APPROVER}, headers=admin)
        assert again.status_code < 300, again.text

    rows = await _seat_rows(app_session, office.tenant, office.hos)
    assert len(rows) == 1 and rows[0].revoked_at is None


async def test_a_seat_cannot_be_written_into_a_department_that_is_not_there(
    app_session: AppSessionFactory,
) -> None:
    """There is no foreign key on `org_member_department.department_id` -- the
    seat deliberately outlives an archived department -- so nothing but this check
    stands between a typo and a seat that matches no approval, appears on no
    screen, and cannot be diagnosed from the row.

    The third case is the one that matters most: another TENANT's department id.
    """
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    other_tenant = uuid.uuid4()
    async with app_session(other_tenant) as db:
        foreign = m.Department(tenant_id=other_tenant, name="Fremd")
        db.add(foreign)
        await db.flush()
        foreign_id = foreign.id

    async with app_session(office.tenant) as db:
        archived = m.Department(tenant_id=office.tenant, name="Aufgelöst")
        db.add(archived)
        await db.flush()
        archived.deleted_at = dt.datetime.now(dt.UTC)
        archived_id = archived.id

    async with _http() as http:
        for department_id, why in (
            (uuid.uuid4(), "a department that never existed"),
            (archived_id, "an archived department"),
            (foreign_id, "another tenant's department"),
        ):
            got = await http.put(
                f"/api/v1/members/{office.hos}/departments/{department_id}",
                json={"seatRole": SEAT_APPROVER},
                headers=admin,
            )
            assert got.status_code == 404, f"{why}: {got.status_code} {got.text}"

        unknown_role = await http.put(
            f"/api/v1/members/{office.hos}/departments/{office.sales}",
            json={"seatRole": "dept_manager"},
            headers=admin,
        )
        assert unknown_role.status_code == 422, unknown_role.text
        assert "dept_manager" in unknown_role.text

    assert await _seat_rows(app_session, office.tenant, office.hos) == [], (
        "one of the four refusals wrote a seat anyway"
    )


async def test_revoking_a_seat_nobody_holds_says_so(app_session: AppSessionFactory) -> None:
    """404 rather than a cheerful 204. An administrator who mistypes the
    department and is told "done" walks away believing an authority was taken
    away that is still held."""
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        never = await http.delete(
            f"/api/v1/members/{office.hos}/departments/{office.sales}", headers=admin
        )
        assert never.status_code == 404, never.text

        granted = await http.put(
            f"/api/v1/members/{office.hos}/departments/{office.sales}",
            json={"seatRole": SEAT_APPROVER},
            headers=admin,
        )
        assert granted.status_code < 300
        assert (
            await http.delete(
                f"/api/v1/members/{office.hos}/departments/{office.sales}", headers=admin
            )
        ).status_code < 300
        twice = await http.delete(
            f"/api/v1/members/{office.hos}/departments/{office.sales}", headers=admin
        )
        assert twice.status_code == 404, twice.text

        stranger = await http.delete(
            f"/api/v1/members/{uuid.uuid4()}/departments/{office.sales}", headers=admin
        )
        assert stranger.status_code == 404, stranger.text


async def test_creating_a_member_adopts_the_row_the_gate_already_minted(
    app_session: AppSessionFactory,
) -> None:
    """The gate mints a person on their first request, so by the time an
    administrator enrols them the row usually exists. An INSERT here would be a
    unique violation -- a 500 for doing the obvious thing -- and a silent no-op
    would drop the display name they just typed."""
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        # `newjoiner` reaches the system first, which mints their row.
        seen = await http.get("/api/v1/me", headers=_headers(office.tenant, "newjoiner", "member"))
        assert seen.status_code == 200
        minted_id = seen.json()["memberId"]

        created = await http.post(
            "/api/v1/members",
            json={"subject": "newjoiner", "displayName": "Neue Kollegin"},
            headers=admin,
        )
        assert created.status_code == 200, (
            f"a row that already existed was reported as created: {created.text}"
        )
        assert created.json()["id"] == minted_id
        assert created.json()["displayName"] == "Neue Kollegin"

        fresh = await http.post(
            "/api/v1/members",
            json={"subject": "never-seen", "displayName": "Vorbereitet"},
            headers=admin,
        )
        assert fresh.status_code == 201, fresh.text


async def test_a_defaulted_post_body_does_not_strip_a_company_wide_view(
    app_session: AppSessionFactory,
) -> None:
    """`allDepartments` is only ever widened here.

    It defaults to `false`, and it is the ONLY unrestricted term the messenger
    door can read -- a Telegram message carries no token. If an upsert wrote the
    default through, an administrator correcting somebody's display name would
    silently stop the CEO's phone from being able to decide anything, and nothing
    anywhere would say so.
    """
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        ceo = await http.post(
            "/api/v1/members",
            json={"subject": "ceo", "displayName": "Chefin", "allDepartments": True},
            headers=admin,
        )
        assert ceo.status_code == 201, ceo.text
        assert ceo.json()["allDepartments"] is True

        renamed = await http.post(
            "/api/v1/members",
            json={"subject": "ceo", "displayName": "Chefin (neu)"},
            headers=admin,
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["allDepartments"] is True, (
            "a defaulted body took the company-wide view away"
        )
        assert renamed.json()["displayName"] == "Chefin (neu)"


async def test_the_members_list_stops_at_the_tenant_boundary(
    app_session: AppSessionFactory,
) -> None:
    """End-to-end, and honest about what is holding it up.

    RLS is what enforces this today: `list_members`' explicit `tenant_id`
    predicate is redundant under a bound session, and there is no way to exercise
    it from here -- an UNBOUND session fails closed and returns nothing either. So
    this is a boundary test of the door, not a test of that predicate, and it is
    here because the door is new: `GET /members` returns the list an administrator
    uses to decide who may release money, and "it is obviously scoped" is what was
    said about `GET /approvals` too.
    """
    office = await _office(app_session)
    other = uuid.uuid4()
    async with app_session(other) as db:
        db.add(
            m.OrgMember(
                tenant_id=other,
                subject="somebody-elses-employee",
                subject_uuid=_subject_uuid("somebody-elses-employee"),
                display_name="Fremd",
            )
        )
        await db.flush()

    async with _http() as http:
        listed = await http.get(
            "/api/v1/members", headers=_headers(office.tenant, "b", "org_admin")
        )
    assert listed.status_code == 200, listed.text
    subjects = {r["subject"] for r in listed.json()["items"]}
    assert "hos" in subjects
    assert "somebody-elses-employee" not in subjects


# --- invite links for a passwordless member -------------------------------
#
# `POST /members` with no password used to leave a member created with no way
# to ever sign in. These four tests cover the door that fixes it: community
# mode with a mail server mints AND sends a link, community mode with no mail
# server still mints and hands the link back, a password given up front skips
# it entirely, and dev mode (where local passwords are meaningless) skips it
# too. Redeeming that link at `POST /auth/password/reset` is covered in
# `test_auth_self_service.py` (`_redeem_token` now accepts either purpose).


def _community(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POST /members`' invite branch is gated on `not get_settings().is_dev`,
    the same flag `GET /auth/config` reads to report `mode`. The suite's
    default `Settings.env` is `"dev"` (see `test_auth_config.py`), so every
    OTHER test in this file exercises the feature switched OFF -- this is the
    one override that switches it on, mirroring `test_auth_config.py`'s own
    `Settings(env="production")` monkeypatch."""
    monkeypatch.setattr("oc8.api.v1.members.get_settings", lambda: Settings(env="production"))


async def test_creating_a_passwordless_member_mints_and_mails_an_invite(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    async with app_session(office.tenant) as db:
        await _configure_smtp(db, office.tenant)

    with patch("oc8.credentials.smtp.smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client
        async with _http() as http:
            created = await http.post(
                "/api/v1/members",
                json={"subject": "invitee@example.com", "displayName": "Invitee"},
                headers=admin,
            )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["inviteSent"] is True, body
    assert body["inviteLink"], "no link returned even though one was minted"
    assert "token=" in body["inviteLink"]
    assert client.send_message.call_count == 1

    async with app_session(office.tenant) as db:
        member = (
            await db.execute(
                select(m.OrgMember).where(
                    m.OrgMember.tenant_id == office.tenant,
                    m.OrgMember.subject == "invitee@example.com",
                )
            )
        ).scalar_one()
        assert member.password_hash is None
        token = (
            await db.execute(
                select(m.AccountVerificationToken).where(
                    m.AccountVerificationToken.tenant_id == office.tenant,
                    m.AccountVerificationToken.member_id == member.id,
                )
            )
        ).scalar_one()
        assert token.purpose == "invite"
        assert token.used_at is None


async def test_creating_a_passwordless_member_without_a_mail_server_still_returns_a_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No SMTP credential configured must not fail the request, and must not
    leave the administrator with nothing to hand the new person either."""
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "invitee2@example.com", "displayName": "Invitee"},
            headers=admin,
        )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["inviteSent"] is False
    assert body["inviteLink"], "the fallback link must still be returned with no mail server"

    async with app_session(office.tenant) as db:
        token = (
            await db.execute(
                select(m.AccountVerificationToken).where(
                    m.AccountVerificationToken.tenant_id == office.tenant,
                    m.AccountVerificationToken.purpose == "invite",
                )
            )
        ).scalar_one()
        assert token.used_at is None


async def test_re_inviting_a_passwordless_member_revokes_the_earlier_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked or misdirected invite link would otherwise stay live for the
    rest of `INVITE_TOKEN_TTL` (7 days) -- much longer than `password_reset`'s
    1-hour credential lifetime, so the gap matters. Re-inviting is just
    `POST /members` again for the same still-passwordless subject: gated on
    `member.password_hash is None`, not `created` (see the docstring above
    `_community`), so the second call reaches the exact same invite-mint
    branch and must spend the FIRST link before minting the second -- mirrors
    `forgot_password`'s UPDATE-then-mint pattern in `auth.py`.
    """
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        first = await http.post(
            "/api/v1/members",
            json={"subject": "reinvited@example.com", "displayName": "Reinvited"},
            headers=admin,
        )
        assert first.status_code == 201, first.text
        first_link = first.json()["inviteLink"]
        assert first_link

        second = await http.post(
            "/api/v1/members",
            json={"subject": "reinvited@example.com", "displayName": "Reinvited"},
            headers=admin,
        )
    # 200, not 201 -- `upsert_member` merged into the row the first call
    # created, which is exactly what puts this through the same `elif`
    # branch rather than some separate re-invite path.
    assert second.status_code == 200, second.text
    second_link = second.json()["inviteLink"]
    assert second_link and second_link != first_link

    async with app_session(office.tenant) as db:
        member = (
            await db.execute(
                select(m.OrgMember).where(
                    m.OrgMember.tenant_id == office.tenant,
                    m.OrgMember.subject == "reinvited@example.com",
                )
            )
        ).scalar_one()
        tokens = (
            (
                await db.execute(
                    select(m.AccountVerificationToken)
                    .where(
                        m.AccountVerificationToken.tenant_id == office.tenant,
                        m.AccountVerificationToken.member_id == member.id,
                        m.AccountVerificationToken.purpose == "invite",
                    )
                    .order_by(m.AccountVerificationToken.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(tokens) == 2, tokens
    assert tokens[0].used_at is not None, "the superseded first link must be spent"
    assert tokens[1].used_at is None, "the current second link must still be live"
    # `_redeem_token`'s WHERE clause matches on `used_at IS NULL`, so a spent
    # row here is already provably dead to `/auth/password/reset` -- that
    # redemption door itself (both purposes, the generic 400 for a spent
    # token) is exercised end-to-end in `test_auth_self_service.py`, which
    # this file does not duplicate since it needs a singleton `Organization`
    # this file's `_office` fixture does not set up.


async def test_creating_a_member_with_a_password_mints_no_invite(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A password given up front is the existing, unchanged behaviour -- no
    invite token, no link in the response, even with a mail server ready."""
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    async with app_session(office.tenant) as db:
        await _configure_smtp(db, office.tenant)

    with patch("oc8.credentials.smtp.smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client
        async with _http() as http:
            created = await http.post(
                "/api/v1/members",
                json={"subject": "hasapassword@example.com", "password": "correct-horse-battery"},
                headers=admin,
            )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body.get("inviteLink") is None
    assert body["inviteSent"] is False
    assert client.send_message.call_count == 0

    async with app_session(office.tenant) as db:
        assert (
            await db.execute(
                select(m.AccountVerificationToken).where(
                    m.AccountVerificationToken.tenant_id == office.tenant
                )
            )
        ).scalar_one_or_none() is None


async def test_dev_mode_mints_no_invite_for_a_passwordless_member(
    app_session: AppSessionFactory,
) -> None:
    """The suite's default settings ARE dev mode (`env="dev"`), so this test
    takes no monkeypatch at all -- it is what every other test in this file
    already exercises, made explicit."""
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "devmode@example.com"},
            headers=admin,
        )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body.get("inviteLink") is None
    assert body["inviteSent"] is False

    async with app_session(office.tenant) as db:
        assert (
            await db.execute(
                select(m.AccountVerificationToken).where(
                    m.AccountVerificationToken.tenant_id == office.tenant
                )
            )
        ).scalar_one_or_none() is None


# --- offboarding: DELETE /members/{id} -----------------------------------
#
# Soft-delete was on the row (`SoftDeleteMixin`, partial unique indexes,
# login/scope filters) but nothing in the API wrote `deleted_at`. The
# administrator's user-detail screen could not remove a person, so a leaver
# kept their seats, role, and any live invite/reset link.


async def test_deleting_a_member_soft_deletes_them_and_drops_them_from_the_list(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "leaver@example.com", "displayName": "Leaver"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        member_id = created.json()["id"]

        deleted = await http.delete(f"/api/v1/members/{member_id}", headers=admin)
        assert deleted.status_code == 204, deleted.text

        listed = await http.get("/api/v1/members", headers=admin)
    assert listed.status_code == 200, listed.text
    subjects = {r["subject"] for r in listed.json()["items"]}
    assert "leaver@example.com" not in subjects

    async with app_session(office.tenant) as db:
        row = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.id == uuid.UUID(member_id)))
        ).scalar_one()
        assert row.deleted_at is not None, "offboarding must stamp deleted_at, not vaporise the row"


async def test_deleting_yourself_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """The same lockout `PUT /members/{id}/role` refuses: an administrator who
    deletes their own row has no door back through the product."""
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "boss", "displayName": "Boss"},
            headers=admin,
        )
        assert created.status_code in {200, 201}, created.text
        member_id = created.json()["id"]

        deleted = await http.delete(f"/api/v1/members/{member_id}", headers=admin)
    assert deleted.status_code == 409, deleted.text
    assert "yourself" in deleted.text.lower() or "own" in deleted.text.lower()

    async with app_session(office.tenant) as db:
        row = (
            await db.execute(select(m.OrgMember).where(m.OrgMember.id == uuid.UUID(member_id)))
        ).scalar_one()
        assert row.deleted_at is None


async def test_deleting_an_unknown_member_is_404(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    async with _http() as http:
        deleted = await http.delete(f"/api/v1/members/{uuid.uuid4()}", headers=admin)
    assert deleted.status_code == 404, deleted.text


async def test_deleting_a_member_requires_member_manage(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    viewer = _headers(office.tenant, "viewer", "member")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "target@example.com"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        refused = await http.delete(f"/api/v1/members/{created.json()['id']}", headers=viewer)
    assert refused.status_code == 403, refused.text


async def test_deleting_a_member_spends_their_unused_invite_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover invite must not stay redeemable after the person is gone."""
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "gone@example.com"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        member_id = created.json()["id"]
        assert created.json()["inviteLink"]

        deleted = await http.delete(f"/api/v1/members/{member_id}", headers=admin)
    assert deleted.status_code == 204, deleted.text

    async with app_session(office.tenant) as db:
        token = (
            await db.execute(
                select(m.AccountVerificationToken).where(
                    m.AccountVerificationToken.tenant_id == office.tenant,
                    m.AccountVerificationToken.member_id == uuid.UUID(member_id),
                )
            )
        ).scalar_one()
        assert token.used_at is not None


async def test_deleting_a_member_is_audited(app_session: AppSessionFactory) -> None:
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "audited-leaver@example.com"},
            headers=admin,
        )
        member_id = created.json()["id"]
        deleted = await http.delete(f"/api/v1/members/{member_id}", headers=admin)
    assert deleted.status_code == 204, deleted.text

    async with app_session(office.tenant) as db:
        actions = list(
            (
                await db.execute(
                    select(m.AuditEvent.action).where(m.AuditEvent.tenant_id == office.tenant)
                )
            ).scalars()
        )
    assert "member.deleted" in actions


# --- admin-minted password-reset / re-invite from the user detail screen --
#
# `POST /members` mints an invite only when creating (or re-POSTing) a
# still-passwordless member. An administrator looking at an existing user
# had no door to mint a fresh reset link or re-trigger the email.


async def test_admin_password_reset_mails_a_link_for_a_member_with_a_password(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    async with app_session(office.tenant) as db:
        await _configure_smtp(db, office.tenant)

    with patch("oc8.credentials.smtp.smtplib.SMTP") as smtp_cls:
        client = MagicMock()
        smtp_cls.return_value.__enter__.return_value = client
        async with _http() as http:
            created = await http.post(
                "/api/v1/members",
                json={
                    "subject": "haspass@example.com",
                    "displayName": "Has Pass",
                    "password": "CorrectHorse1",
                },
                headers=admin,
            )
            assert created.status_code == 201, created.text
            member_id = created.json()["id"]

            reset = await http.post(f"/api/v1/members/{member_id}/password-reset", headers=admin)
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body["resetSent"] is True, body
    assert body["resetLink"] and "token=" in body["resetLink"]
    assert client.send_message.call_count == 1

    async with app_session(office.tenant) as db:
        token = (
            await db.execute(
                select(m.AccountVerificationToken).where(
                    m.AccountVerificationToken.tenant_id == office.tenant,
                    m.AccountVerificationToken.member_id == uuid.UUID(member_id),
                )
            )
        ).scalar_one()
        assert token.purpose == "password_reset"
        assert token.used_at is None


async def test_admin_password_reset_without_mail_server_still_returns_a_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "noleak@example.com", "password": "CorrectHorse1"},
            headers=admin,
        )
        member_id = created.json()["id"]
        reset = await http.post(f"/api/v1/members/{member_id}/password-reset", headers=admin)
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body["resetSent"] is False
    assert body["resetLink"] and "token=" in body["resetLink"]


async def test_admin_password_reset_for_a_passwordless_member_mints_an_invite(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same 7-day invite a create-without-password mints -- they still have
    nothing to reset, they have a password to SET."""
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "stillwaiting@example.com"},
            headers=admin,
        )
        member_id = created.json()["id"]
        reset = await http.post(f"/api/v1/members/{member_id}/password-reset", headers=admin)
    assert reset.status_code == 200, reset.text
    assert reset.json()["resetLink"]

    async with app_session(office.tenant) as db:
        tokens = list(
            (
                await db.execute(
                    select(m.AccountVerificationToken).where(
                        m.AccountVerificationToken.tenant_id == office.tenant,
                        m.AccountVerificationToken.member_id == uuid.UUID(member_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        live = [t for t in tokens if t.used_at is None]
        assert len(live) == 1
        assert live[0].purpose == "invite"


async def test_admin_password_reset_spends_the_earlier_unused_link(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _community(monkeypatch)
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "again@example.com", "password": "CorrectHorse1"},
            headers=admin,
        )
        member_id = created.json()["id"]
        first = await http.post(f"/api/v1/members/{member_id}/password-reset", headers=admin)
        second = await http.post(f"/api/v1/members/{member_id}/password-reset", headers=admin)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["resetLink"] != second.json()["resetLink"]

    async with app_session(office.tenant) as db:
        tokens = list(
            (
                await db.execute(
                    select(m.AccountVerificationToken)
                    .where(
                        m.AccountVerificationToken.tenant_id == office.tenant,
                        m.AccountVerificationToken.member_id == uuid.UUID(member_id),
                    )
                    .order_by(m.AccountVerificationToken.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(tokens) == 2
    assert tokens[0].used_at is not None
    assert tokens[1].used_at is None


async def test_admin_password_reset_requires_member_manage(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    viewer = _headers(office.tenant, "viewer", "member")

    async with _http() as http:
        created = await http.post(
            "/api/v1/members",
            json={"subject": "gated@example.com", "password": "CorrectHorse1"},
            headers=admin,
        )
        refused = await http.post(
            f"/api/v1/members/{created.json()['id']}/password-reset", headers=viewer
        )
    assert refused.status_code == 403, refused.text


async def test_admin_password_reset_unknown_member_is_404(
    app_session: AppSessionFactory,
) -> None:
    office = await _office(app_session)
    admin = _headers(office.tenant, "boss", "org_admin")
    async with _http() as http:
        reset = await http.post(f"/api/v1/members/{uuid.uuid4()}/password-reset", headers=admin)
    assert reset.status_code == 404, reset.text
