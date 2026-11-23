"""`oc8 member ...`.

These are the bridge until there is a seat-administration screen (§10 item 7),
so they get the same scrutiny the endpoints do: a promotion must leave one
live row, a revoke must be reported when there was nothing to revoke, and a
department name that matches twice must refuse rather than guess.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.authz.permissions import SEAT_APPROVER, SEAT_VIEWER
from oc8.tenants import cli as tcli
from oc8.tenants.provision import create_tenant
from tests.tenants.conftest import OwnerSessionFactory

pytestmark = pytest.mark.asyncio


def _slug() -> str:
    return f"muster-{uuid.uuid4().hex[:10]}"


async def _tenant(owner_session: OwnerSessionFactory) -> tuple[str, uuid.UUID]:
    """A real tenant with one department named Vertrieb."""
    slug = _slug()
    async with owner_session() as db:
        created = await create_tenant(db, slug=slug, name="Muster GmbH", department_name="Vertrieb")
        return slug, created.tenant_id


async def _seats(owner_session: OwnerSessionFactory, tenant_id: uuid.UUID) -> list[Any]:
    async with owner_session() as db:
        return list(
            (
                await db.execute(
                    select(m.OrgMemberDepartment).where(
                        m.OrgMemberDepartment.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )


async def test_granting_a_seat_creates_the_person_if_nobody_has_seen_them(
    owner_session: OwnerSessionFactory,
) -> None:
    """An administrator has to be able to prepare somebody's authority before
    their first sign-in. `subject` is the token `sub` the IdP will send, which is
    also what the gate upserts on -- so the two meet on one row instead of making
    two of them."""
    slug, tenant_id = await _tenant(owner_session)

    assert (
        await tcli.cmd_member_grant(
            slug=slug,
            subject="hos",
            department="Vertrieb",
            role=SEAT_APPROVER,
            display_name="Lena",
        )
        == 0
    )

    async with owner_session() as db:
        member = (
            await db.execute(
                select(m.OrgMember).where(
                    m.OrgMember.tenant_id == tenant_id, m.OrgMember.subject == "hos"
                )
            )
        ).scalar_one()
        assert member.display_name == "Lena"

    seats = await _seats(owner_session, tenant_id)
    assert len(seats) == 1
    assert seats[0].seat_role == SEAT_APPROVER and seats[0].revoked_at is None
    # NULL on purpose: a CLI grant has no `org_member` behind it, and inventing
    # one would be a lie in the very trail this column exists for.
    assert seats[0].granted_by is None


async def test_the_cli_promotes_and_revokes_the_same_way_the_endpoint_does(
    owner_session: OwnerSessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    slug, tenant_id = await _tenant(owner_session)
    assert (
        await tcli.cmd_member_grant(
            slug=slug, subject="hos", department="Vertrieb", role=SEAT_VIEWER
        )
        == 0
    )
    # Idempotent: the same seat again is not a second row and not a revoke/grant
    # pair in the audit trail.
    assert (
        await tcli.cmd_member_grant(
            slug=slug, subject="hos", department="Vertrieb", role=SEAT_VIEWER
        )
        == 0
    )
    assert len(await _seats(owner_session, tenant_id)) == 1

    assert (
        await tcli.cmd_member_grant(
            slug=slug, subject="hos", department="Vertrieb", role=SEAT_APPROVER
        )
        == 0
    )
    seats = await _seats(owner_session, tenant_id)
    live = [s for s in seats if s.revoked_at is None]
    assert len(live) == 1 and live[0].seat_role == SEAT_APPROVER
    assert [s.seat_role for s in seats if s.revoked_at is not None] == [SEAT_VIEWER]

    assert await tcli.cmd_member_revoke(slug=slug, subject="hos", department="Vertrieb") == 0
    assert [s for s in await _seats(owner_session, tenant_id) if s.revoked_at is None] == []

    # Nothing left to revoke, and it is said out loud rather than reported as
    # success -- an administrator told "done" walks away believing an authority
    # was taken away that is still held.
    assert await tcli.cmd_member_revoke(slug=slug, subject="hos", department="Vertrieb") == 2

    assert await tcli.cmd_member_list(slug=slug) == 0
    out = capsys.readouterr().out
    assert "hos" in out


async def test_a_bad_slug_department_or_seat_role_writes_nothing(
    owner_session: OwnerSessionFactory,
) -> None:
    slug, tenant_id = await _tenant(owner_session)

    assert (
        await tcli.cmd_member_grant(
            slug="no-such-tenant", subject="hos", department="Vertrieb", role=SEAT_APPROVER
        )
        == 2
    )
    assert (
        await tcli.cmd_member_grant(
            slug=slug, subject="hos", department="Entwicklung", role=SEAT_APPROVER
        )
        == 2
    )
    assert (
        await tcli.cmd_member_grant(
            slug=slug, subject="hos", department="Vertrieb", role="dept_manager"
        )
        == 2
    )
    assert await _seats(owner_session, tenant_id) == []


async def test_an_ambiguous_department_name_refuses_rather_than_guesses(
    owner_session: OwnerSessionFactory,
) -> None:
    """Department names are not unique in the schema. A seat granted in the wrong
    one of two departments called "Vertrieb" is invisible on every screen and
    looks exactly like the grant not working at all."""
    slug, tenant_id = await _tenant(owner_session)
    async with owner_session() as db:
        db.add(m.Department(tenant_id=tenant_id, name="Vertrieb"))

    assert (
        await tcli.cmd_member_grant(
            slug=slug, subject="hos", department="Vertrieb", role=SEAT_APPROVER
        )
        == 2
    )
    assert await _seats(owner_session, tenant_id) == []


async def _personal_department(owner_session: OwnerSessionFactory, tenant_id: uuid.UUID) -> None:
    from oc8.models.core import DEPARTMENT_KIND_PERSONAL

    async with owner_session() as db:
        owner = m.OrgMember(tenant_id=tenant_id, subject="owner", subject_uuid=uuid.uuid4())
        db.add(owner)
        await db.flush()
        db.add(
            m.Department(
                tenant_id=tenant_id,
                name="Persoenlich",
                goal="",
                frame={},
                kind=DEPARTMENT_KIND_PERSONAL,
                owner_member_id=owner.id,
            )
        )


async def test_a_personal_department_takes_no_seat_from_the_cli(
    owner_session: OwnerSessionFactory,
) -> None:
    slug, tenant_id = await _tenant(owner_session)
    await _personal_department(owner_session, tenant_id)

    assert (
        await tcli.cmd_member_grant(
            slug=slug, subject="hos", department="Persoenlich", role=SEAT_VIEWER
        )
        == 2
    )
    assert await _seats(owner_session, tenant_id) == []


async def test_a_bulk_row_naming_a_personal_department_fails_as_a_row(
    owner_session: OwnerSessionFactory, tmp_path: Any
) -> None:
    slug, tenant_id = await _tenant(owner_session)
    await _personal_department(owner_session, tenant_id)
    path = tmp_path / "m.csv"
    path.write_text(
        "subject,display_name,role,department,seat_role\n"
        "anna,Anna,,Persoenlich,dept_viewer\n"
        "ben,Ben,,Vertrieb,dept_viewer\n"
    )

    assert await tcli.cmd_member_import(slug=slug, path=str(path)) == 2
    seats = await _seats(owner_session, tenant_id)
    assert len(seats) == 1  # ben's row landed, anna's did not
