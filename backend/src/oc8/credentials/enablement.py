"""Which departments an organisation login is enabled for.

A login is created once and enabled for departments (`credential_department`
rows) or for all of them (`Credential.all_departments`). A personal login
(`owner_member_id` set) is never enabled for a department.

add/flush only, never commit -- the caller owns the transaction.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m


async def set_departments(
    db: AsyncSession,
    cred: m.Credential,
    *,
    department_ids: set[uuid.UUID],
    all_departments: bool,
) -> None:
    """Replace the login's enablement. Raises `ValueError("personal")` for a
    personal login."""
    if cred.owner_member_id is not None:
        raise ValueError("personal")
    await db.execute(
        delete(m.CredentialDepartment).where(
            m.CredentialDepartment.tenant_id == cred.tenant_id,
            m.CredentialDepartment.credential_id == cred.id,
        )
    )
    for department_id in sorted(department_ids):
        db.add(
            m.CredentialDepartment(
                tenant_id=cred.tenant_id, credential_id=cred.id, department_id=department_id
            )
        )
    cred.all_departments = all_departments
    await db.flush()


async def enable_for_department(
    db: AsyncSession, cred: m.Credential, department_id: uuid.UUID | None
) -> None:
    """Add one department to the login's enablement, never taking it from
    another; `None` enables it for all departments (the department rows are
    kept). Raises `ValueError("personal")` for a personal login."""
    current = set((await department_ids_by_credential(db, [cred.id])).get(cred.id, []))
    if department_id is None:
        if not cred.all_departments:
            await set_departments(db, cred, department_ids=current, all_departments=True)
    elif department_id not in current:
        await set_departments(
            db, cred, department_ids=current | {department_id}, all_departments=cred.all_departments
        )


async def choose_for_department(
    db: AsyncSession, cred: m.Credential, department_id: uuid.UUID
) -> None:
    """Make `cred` THE login of its type for one department (a surface that
    names a single login for it: a tool setup, a department's default): the
    department is taken off every other organisation login of the same type
    and `cred` is enabled for it. Logins enabled for all departments are
    left alone -- the department's own choice wins over them in the
    resolver. Raises `ValueError("personal")` for a personal login."""
    if cred.owner_member_id is not None:
        raise ValueError("personal")
    others = select(m.Credential.id).where(
        m.Credential.credential_type == cred.credential_type,
        m.Credential.owner_member_id.is_(None),
        m.Credential.id != cred.id,
    )
    await db.execute(
        delete(m.CredentialDepartment).where(
            m.CredentialDepartment.tenant_id == cred.tenant_id,
            m.CredentialDepartment.department_id == department_id,
            m.CredentialDepartment.credential_id.in_(others),
        )
    )
    await enable_for_department(db, cred, department_id)


async def department_ids_by_credential(
    db: AsyncSession, credential_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """The departments each of `credential_ids` is enabled for."""
    out: dict[uuid.UUID, list[uuid.UUID]] = {cid: [] for cid in credential_ids}
    if not credential_ids:
        return out
    rows = await db.execute(
        select(m.CredentialDepartment.credential_id, m.CredentialDepartment.department_id).where(
            m.CredentialDepartment.credential_id.in_(credential_ids)
        )
    )
    for credential_id, department_id in rows:
        out[credential_id].append(department_id)
    return out


async def chosen_credentials(
    db: AsyncSession, *, credential_type: str, department_ids: list[uuid.UUID]
) -> list[m.Credential]:
    """The logins of `credential_type` that apply to `department_ids`: those
    enabled for one of them directly -- or, only when there is none, those
    enabled for all departments. A department's own choice wins over an
    "all departments" login; several of the same tier are for the caller to
    report (ambiguous). Oldest first, no duplicates."""
    direct: dict[uuid.UUID, m.Credential] = {}
    everywhere: dict[uuid.UUID, m.Credential] = {}
    for department_id in department_ids:
        enabled_here = set(
            (
                await db.execute(
                    select(m.CredentialDepartment.credential_id).where(
                        m.CredentialDepartment.department_id == department_id
                    )
                )
            ).scalars()
        )
        for cred in await enabled_credentials(
            db, credential_type=credential_type, department_id=department_id
        ):
            (direct if cred.id in enabled_here else everywhere).setdefault(cred.id, cred)
    return list((direct or everywhere).values())


async def enabled_credentials(
    db: AsyncSession, *, credential_type: str, department_id: uuid.UUID
) -> list[m.Credential]:
    """Organisation logins of `credential_type` enabled for `department_id`,
    directly or through `all_departments`, oldest first."""
    enabled_here = select(m.CredentialDepartment.credential_id).where(
        m.CredentialDepartment.department_id == department_id
    )
    stmt = (
        select(m.Credential)
        .where(
            m.Credential.credential_type == credential_type,
            m.Credential.owner_member_id.is_(None),
            or_(m.Credential.all_departments.is_(True), m.Credential.id.in_(enabled_here)),
        )
        .order_by(m.Credential.created_at, m.Credential.name, m.Credential.id)
    )
    return list((await db.execute(stmt)).scalars().all())
