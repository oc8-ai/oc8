"""`oc8 tenant ...` command bodies.

Community is single-tenant in production, provisioned by `POST /auth/setup`
against the one bootstrapped organization -- there is no `tenant create` or
administrator-invitation CLI here. What remains is everything else an
operator needs against an existing tenant: listing it, and managing its
members and roles (`tenant list`, plus the `member`/`role` command families
below).
"""

from __future__ import annotations

import csv
import sys
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.authz.permissions import SEAT_PERMISSIONS
from oc8.db.session import owner_session, tenant_session
from oc8.departments.personal import PersonalDepartmentSeatRefused
from oc8.roles.service import (
    RoleRefused,
    assign_role,
    create_role,
    delete_role,
    holders_of,
    list_roles,
    validated_name,
    validated_permissions,
)
from oc8.tenants.provision import list_tenants
from oc8.workspace.members import (
    UnknownSeatRole,
    grant_seat,
    list_members,
    revoke_seat,
    upsert_member,
    validated_seat_role,
)

EXIT_OK = 0
EXIT_BAD_INPUT = 2


def _err(message: str) -> None:
    print(message, file=sys.stderr)


async def cmd_list() -> int:
    async with owner_session() as db:
        rows = await list_tenants(db)
    for r in rows:
        print(f"{r.slug:<24} {r.tenant_id}  {r.name}  ({r.tier}/{r.region})")
    return EXIT_OK


# ------------------------------------------------------------------ people/seats
#
# These run through `tenant_session`, not `owner_session`: RLS then applies as
# it does to the API, so a slug typo cannot reach into another tenant's rows. The
# session commits once, at the end of the context -- a commit in the middle would
# unbind `app.tenant_id` and every statement after it would silently see nothing.


async def _tenant_id_for(slug: str) -> uuid.UUID | None:
    async with owner_session() as db:
        org = (
            await db.execute(select(m.Organization).where(m.Organization.slug == slug))
        ).scalar_one_or_none()
    if org is None:
        _err(f"error: no tenant with slug {slug!r}")
        return None
    return org.id


async def _department_by_name(
    db: AsyncSession, tenant_id: uuid.UUID, name: str
) -> m.Department | None:
    rows = (
        (
            await db.execute(
                select(m.Department).where(
                    m.Department.tenant_id == tenant_id,
                    m.Department.name == name,
                    m.Department.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        _err(f"error: no department named {name!r} in this tenant")
        return None
    if len(rows) > 1:
        # Department names are not unique in the schema. Refusing beats guessing:
        # a seat granted in the wrong one of two departments with the same name is
        # invisible on every screen and looks like the grant simply did not work.
        _err(
            f"error: {len(rows)} departments are named {name!r}; "
            f"ids: {', '.join(str(r.id) for r in rows)}"
        )
        return None
    return rows[0]


async def cmd_member_grant(
    *, slug: str, subject: str, department: str, role: str, display_name: str = ""
) -> int:
    """Seat a person in a department, creating their row if this tenant has never
    seen them.

    Creating the row is the point: an administrator should be able to prepare
    somebody's authority before their first sign-in, and `subject` is the token
    `sub` the IdP will send when it happens -- the same value the gate upserts on,
    so the two meet on one row rather than making two.

    `granted_by` is left NULL. A CLI grant has no `org_member` behind it and
    inventing one would be a lie in the very trail this column exists for.
    """
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT
    if role not in SEAT_PERMISSIONS:
        _err(
            f"error: unknown seat role {role!r}; expected one of "
            f"{', '.join(sorted(SEAT_PERMISSIONS))}"
        )
        return EXIT_BAD_INPUT

    async with tenant_session(tenant_id) as db:
        dept = await _department_by_name(db, tenant_id, department)
        if dept is None:
            return EXIT_BAD_INPUT
        member, created = await upsert_member(
            db, tenant_id=tenant_id, subject=subject, display_name=display_name
        )
        try:
            seat, changed = await grant_seat(
                db,
                tenant_id=tenant_id,
                member_id=member.id,
                department_id=dept.id,
                seat_role=role,
                granted_by=None,
            )
        except UnknownSeatRole as exc:  # pragma: no cover - guarded above
            _err(f"error: {exc}")
            return EXIT_BAD_INPUT
        except PersonalDepartmentSeatRefused as exc:
            _err(f"error: {exc}")
            return EXIT_BAD_INPUT
        if changed:
            await append_event(
                db,
                tenant_id=tenant_id,
                actor_type="system",
                actor_id=None,
                category="member",
                action="member.seat_granted",
                resource={
                    "member_id": str(member.id),
                    "subject": subject,
                    "department_id": str(dept.id),
                    "department": dept.name,
                    "seat_role": seat.seat_role,
                },
                reason="granted with `oc8 member grant`",
            )
    if created:
        print(f"created member {subject} in tenant {slug}")
    print(f"{'granted' if changed else 'already held'}: {subject} is {role} in {dept.name}")
    return EXIT_OK


async def cmd_member_revoke(*, slug: str, subject: str, department: str) -> int:
    """Take a seat away. Effective on that person's next request."""
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT

    async with tenant_session(tenant_id) as db:
        dept = await _department_by_name(db, tenant_id, department)
        if dept is None:
            return EXIT_BAD_INPUT
        member = (
            (
                await db.execute(
                    select(m.OrgMember).where(
                        m.OrgMember.tenant_id == tenant_id,
                        m.OrgMember.subject == subject,
                        m.OrgMember.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if member is None:
            _err(f"error: this tenant knows no member with subject {subject!r}")
            return EXIT_BAD_INPUT
        try:
            seat = await revoke_seat(
                db, tenant_id=tenant_id, member_id=member.id, department_id=dept.id
            )
        except PersonalDepartmentSeatRefused as exc:
            _err(f"error: {exc}")
            return EXIT_BAD_INPUT
        if seat is None:
            _err(f"error: {subject} holds no live seat in {dept.name}")
            return EXIT_BAD_INPUT
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            category="member",
            action="member.seat_revoked",
            resource={
                "member_id": str(member.id),
                "subject": subject,
                "department_id": str(dept.id),
                "seat_role": seat.seat_role,
            },
            reason="revoked with `oc8 member revoke`",
        )
    print(f"revoked: {subject} is no longer {seat.seat_role} in {dept.name}")
    return EXIT_OK


async def cmd_member_list(*, slug: str) -> int:
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT
    async with tenant_session(tenant_id) as db:
        rows, _total = await list_members(db, tenant_id=tenant_id)
    if not rows:
        print("this tenant knows nobody yet (a person is minted on their first request)")
        return EXIT_OK
    for row in rows:
        # `all_departments` is printed BESIDE the seats, never instead of them.
        # It is a separate, company-wide grant -- the only unrestricted term the
        # messenger door can read -- and a listing that hid it behind "he has two
        # seats" would be the one line an administrator needed to see.
        parts = [f"{s.department_name or s.department_id}={s.seat_role}" for s in row.seats]
        if row.all_departments:
            parts.insert(0, "ALL DEPARTMENTS")
        print(
            f"{row.subject:<32} {row.id}  {row.display_name or '-':<20} "
            f"{', '.join(parts) or 'no seat'}"
        )
    return EXIT_OK


# ---------------------------------------------------------------------- roles
#
# The same `tenant_session` discipline as the seat commands above: RLS applies
# exactly as it does to the API, so a slug typo cannot reach into another
# tenant's rows, and each context commits ONCE at the end.
#
# These are not a second implementation of §6's endpoints. Every write goes
# through `oc8.roles.service`, which is the only module allowed to write `role`
# and `role_permission`, so the CLI and the API cannot come to disagree about
# what a name may be or which permissions may be granted. What the CLI adds is
# the two things HTTP cannot do: it runs when nobody can log in (the lockout
# repair), and it can drive five hundred rows from a file.


async def cmd_role_list(*, slug: str) -> int:
    """Every role this tenant has, built-ins included, with holder counts."""
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT
    async with tenant_session(tenant_id) as db:
        rows = await list_roles(db, tenant_id=tenant_id)
    for row in rows:
        kind = "built-in" if row.builtin else "tenant"
        print(
            f"{row.name:<28} {kind:<9} {row.holder_count:>4} holder(s)  "
            f"{len(row.permissions):>2} permission(s)  {row.id or '-'}"
        )
    return EXIT_OK


async def cmd_role_show(*, slug: str, name: str) -> int:
    """One role, its grants and the people holding it."""
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT
    async with tenant_session(tenant_id) as db:
        rows = await list_roles(db, tenant_id=tenant_id)
        match = next((r for r in rows if r.name.lower() == name.lower()), None)
        if match is None:
            _err(f"error: no role named {name!r} in this tenant")
            return EXIT_BAD_INPUT
        holders = (
            [] if match.id is None else await holders_of(db, tenant_id=tenant_id, role_id=match.id)
        )
    print(f"{match.name}  ({'built-in, read-only' if match.builtin else 'tenant-defined'})")
    if match.description:
        print(f"  {match.description}")
    for permission in sorted(match.permissions):
        print(f"  + {permission}")
    for holder in holders:
        print(f"  @ {holder.subject}  {holder.display_name or '-'}")
    if not holders:
        print("  @ nobody holds it")
    return EXIT_OK


async def cmd_role_create(*, slug: str, name: str, permissions: list[str], description: str) -> int:
    """Compose a role from the offerable rights.

    The permission list is validated by the same function the endpoint uses, so a
    refusal here names the same offender and prints the same reason an
    administrator would have read next to the checkbox.
    """
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT
    try:
        granted = validated_permissions(permissions)
        clean = validated_name(name)
    except RoleRefused as exc:
        _err(f"error: {exc.detail}")
        return EXIT_BAD_INPUT

    async with tenant_session(tenant_id) as db:
        try:
            role = await create_role(
                db,
                tenant_id=tenant_id,
                name=clean,
                description=description,
                permissions=granted,
                # NULL: a role written by the CLI has no `org_member` behind it,
                # and inventing one would be a lie in the trail this column exists
                # for. Same rule as `OrgMemberDepartment.granted_by`.
                created_by=None,
            )
        except RoleRefused as exc:
            _err(f"error: {exc.detail}")
            return EXIT_BAD_INPUT
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            category="role",
            action="role.created",
            resource={
                "role_id": str(role.id),
                "name": role.name,
                "permissions": sorted(granted),
            },
            reason="created with `oc8 role create`",
        )
    print(f"created role {clean!r} with {len(granted)} permission(s)")
    return EXIT_OK


async def cmd_role_delete(*, slug: str, name: str, reassign_to: str | None) -> int:
    """Soft-delete a role, moving its holders first if a target was named.

    Refuses while somebody holds it, exactly as the endpoint does: deleting it
    would restore every holder to whatever their TOKEN says, which on every live
    tenant is `org_admin`.
    """
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT
    async with tenant_session(tenant_id) as db:
        role = await _role_by_name(db, tenant_id, name)
        if role is None:
            return EXIT_BAD_INPUT
        target = None
        if reassign_to is not None:
            target = await _role_by_name(db, tenant_id, reassign_to)
            if target is None:
                return EXIT_BAD_INPUT
        try:
            # `caller_subject=""` on purpose, and it is not a placeholder. This
            # command runs out of band, as the schema owner, with no token and no
            # member row -- it IS the documented repair for a lockout, so the
            # self-demotion refusal that protects an administrator from locking
            # himself out through the API must not be able to refuse the tool he
            # is told to reach for. No `org_member.subject` is ever the empty
            # string (it is a token `sub`), so nothing matches it.
            moved = await delete_role(db, role=role, reassign_to=target, caller_subject="")
        except RoleRefused as exc:
            _err(f"error: {exc.detail}")
            return EXIT_BAD_INPUT
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            category="role",
            action="role.deleted",
            resource={
                "role_id": str(role.id),
                "name": role.name,
                "reassigned_to": None if target is None else str(target.id),
                "moved": [str(h.id) for h in moved],
            },
            reason="deleted with `oc8 role delete`",
        )
    print(f"deleted role {role.name!r}; {len(moved)} holder(s) moved")
    return EXIT_OK


async def _role_by_name(db: AsyncSession, tenant_id: uuid.UUID, name: str) -> m.Role | None:
    """A live role of this tenant by name, case-insensitively.

    Names are unique per tenant on `lower(name)`, so this cannot be ambiguous --
    unlike `_department_by_name` above, which has to refuse a tie.
    """
    role = (
        (
            await db.execute(
                select(m.Role).where(
                    m.Role.tenant_id == tenant_id,
                    func.lower(m.Role.name) == name.strip().lower(),
                    m.Role.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if role is None:
        _err(f"error: no role named {name!r} in this tenant")
    return role


async def cmd_member_set_role(*, slug: str, subject: str, role: str | None) -> int:
    """Give one person a role, or clear the override.

    `--clear` is the out-of-band lockout repair, and it is the reason this
    command exists at all rather than being only an endpoint: `member:manage` is
    the permission that assigns roles, so an administrator who assigns himself a
    role without it has locked the only door back. `PUT /members/{id}/role`
    refuses that assignment (409), and this is what fixes it if it happens
    anyway -- by restore, by psql, or by a future importer.

    Clearing sets `role_id` back to NULL, which is NOT "no permissions": the
    person resolves through their token again, exactly as before this feature
    existed.
    """
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT
    async with tenant_session(tenant_id) as db:
        target = None
        if role is not None:
            target = await _role_by_name(db, tenant_id, role)
            if target is None:
                return EXIT_BAD_INPUT
        member = (
            (
                await db.execute(
                    select(m.OrgMember).where(
                        m.OrgMember.tenant_id == tenant_id,
                        m.OrgMember.subject == subject,
                        m.OrgMember.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if member is None:
            _err(f"error: this tenant knows no member with subject {subject!r}")
            return EXIT_BAD_INPUT
        try:
            # `caller_subject` is deliberately a value no token can carry: the
            # self-demotion refusal protects a caller from locking HIMSELF out,
            # and this command is the repair for exactly that state. A CLI run has
            # no session to lock out of.
            await assign_role(db, member=member, role=target, caller_subject="")
        except RoleRefused as exc:
            _err(f"error: {exc.detail}")
            return EXIT_BAD_INPUT
        await append_event(
            db,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=None,
            category="member",
            action="member.role_assigned" if target is not None else "member.role_cleared",
            resource={
                "member_id": str(member.id),
                "subject": subject,
                "role_id": None if target is None else str(target.id),
                "role": None if target is None else target.name,
            },
            reason="set with `oc8 member set-role`",
        )
    if target is None:
        print(f"cleared: {subject} resolves through their token again")
    else:
        print(f"assigned: {subject} now holds {target.name!r}")
    return EXIT_OK


#: The columns `oc8 member import --csv` reads. `subject` is the only required
#: one -- everything else is optional, because the three things this file can
#: carry are three separate decisions and a reorganisation usually only makes
#: one of them.
CSV_COLUMNS = ("subject", "display_name", "role", "department", "seat_role")


def _read_csv(path: str) -> list[dict[str, str]]:
    """The whole file, up front, and synchronously.

    Synchronous on purpose and in its own function: a blocking read inside a
    coroutine is a real smell in a server and a non-issue in a one-shot CLI, and
    a `noqa` would have said "we know" rather than "here is why". Read in FULL
    before anything is written, so a misspelled column heading is a spelling
    mistake rather than a half-imported tenant.

    `utf-8-sig` because this file comes out of a spreadsheet more often than out
    of a script, and a byte-order mark would otherwise make the first column
    heading `﻿subject` -- reported as an unknown column, which is true and
    unhelpful.
    """
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


async def cmd_member_import(*, slug: str, path: str) -> int:
    """Drive people, roles and seats from a CSV. ONE TRANSACTION PER ROW.

    That is the whole design of this command and it is not an optimisation in
    either direction. A commit inside `tenant_session` unbinds `app.tenant_id`
    for every statement after it, so a single transaction spanning five hundred
    rows with a commit at the end would be fine -- and a single transaction with
    a commit per row would silently apply only the first. Both shapes are one
    keystroke apart and only one of them fails loudly.

    So each row opens its own session, binds the tenant, writes, and commits. The
    cost is five hundred short transactions; what it buys is that row 500 naming
    a role that does not exist does not throw away the 499 that were correct, and
    that the report at the end is true row by row.

    The file is READ AND VALIDATED IN FULL before anything is written. A
    misspelled column heading discovered on row one is a spelling mistake; the
    same mistake discovered after 300 rows have already landed is an
    inconsistent tenant somebody has to unpick by hand.
    """
    tenant_id = await _tenant_id_for(slug)
    if tenant_id is None:
        return EXIT_BAD_INPUT

    try:
        rows = _read_csv(path)
    except OSError as exc:
        _err(f"error: cannot read {path!r}: {exc}")
        return EXIT_BAD_INPUT
    if not rows:
        _err(f"error: {path!r} has no data rows")
        return EXIT_BAD_INPUT

    unknown = sorted(set(rows[0]) - set(CSV_COLUMNS) - {None})
    if unknown:
        _err(
            f"error: unknown column(s) {', '.join(str(c) for c in unknown)}; "
            f"expected any of {', '.join(CSV_COLUMNS)}"
        )
        return EXIT_BAD_INPUT
    missing = [i for i, row in enumerate(rows, start=2) if not (row.get("subject") or "").strip()]
    if missing:
        _err(f"error: line(s) {', '.join(str(i) for i in missing[:10])} have no subject")
        return EXIT_BAD_INPUT

    applied = 0
    failures: list[str] = []
    for line, row in enumerate(rows, start=2):
        # One transaction per row, and it is the whole point of the loop. See the
        # docstring: the alternative shapes lose either 499 good rows or all but
        # the first, and neither says so.
        #
        # The exception is allowed OUT of the session context on purpose --
        # `tenant_session` rolls back on the way out -- so a row that named a
        # good role and a bad department lands neither half of itself. It is
        # caught here, outside, so one bad row costs one row.
        try:
            async with tenant_session(tenant_id) as db:
                await _import_one(db, tenant_id, row)
            applied += 1
        except (RoleRefused, UnknownSeatRole, PersonalDepartmentSeatRefused, ValueError) as exc:
            failures.append(f"line {line} ({row.get('subject')}): {exc}")

    for failure in failures:
        _err(f"  {failure}")
    print(f"imported {applied} of {len(rows)} row(s) from {path}")
    return EXIT_OK if not failures else EXIT_BAD_INPUT


async def _import_one(db: AsyncSession, tenant_id: uuid.UUID, row: dict[str, str]) -> None:
    """One CSV row: enrol the person, set their role, seat them.

    In that order, because each step needs the one before it, and all three are
    optional except the first. Enrolling is an UPSERT -- the row may already
    exist because the person signed in this morning, and a second row for the
    same human would be two answers to "what may Anna do".
    """
    subject = (row.get("subject") or "").strip()
    member, _created = await upsert_member(
        db,
        tenant_id=tenant_id,
        subject=subject,
        display_name=(row.get("display_name") or "").strip(),
    )

    role_name = (row.get("role") or "").strip()
    if role_name:
        role = (
            (
                await db.execute(
                    select(m.Role).where(
                        m.Role.tenant_id == tenant_id,
                        func.lower(m.Role.name) == role_name.lower(),
                        m.Role.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if role is None:
            raise ValueError(f"no role named {role_name!r}")
        await assign_role(db, member=member, role=role, caller_subject="")

    department_name = (row.get("department") or "").strip()
    if department_name:
        department = await _department_by_name(db, tenant_id, department_name)
        if department is None:
            raise ValueError(f"no department named {department_name!r}")
        await grant_seat(
            db,
            tenant_id=tenant_id,
            member_id=member.id,
            department_id=department.id,
            seat_role=validated_seat_role((row.get("seat_role") or "dept_viewer").strip()),
            granted_by=None,
        )
    await append_event(
        db,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id=None,
        category="member",
        action="member.imported",
        resource={
            "member_id": str(member.id),
            "subject": subject,
            "role": role_name or None,
            "department": department_name or None,
        },
        reason="imported with `oc8 member import`",
    )
