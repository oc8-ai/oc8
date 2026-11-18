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
