"""A login enabled for departments: `credential_department`."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from oc8 import models as m
from tests.conftest import AppSessionFactory
from tests.connections.helpers import credential_row

pytestmark = pytest.mark.asyncio


async def _department(app_session: AppSessionFactory, tenant: uuid.UUID) -> uuid.UUID:
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Sales")
        db.add(dept)
        await db.flush()
        return dept.id


async def test_enabling_a_login_for_a_department_is_stored_once(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    cred = await credential_row(app_session, tenant, owner=None)
    dept = await _department(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(m.CredentialDepartment(tenant_id=tenant, credential_id=cred, department_id=dept))
    with pytest.raises(sa.exc.IntegrityError):
        async with app_session(tenant) as db:
            db.add(m.CredentialDepartment(tenant_id=tenant, credential_id=cred, department_id=dept))
            await db.flush()


async def test_deleting_a_department_drops_its_enablements(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    cred = await credential_row(app_session, tenant, owner=None)
    dept = await _department(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(m.CredentialDepartment(tenant_id=tenant, credential_id=cred, department_id=dept))
    async with app_session(tenant) as db:
        await db.execute(sa.delete(m.Department).where(m.Department.id == dept))
    async with app_session(tenant) as db:
        rows = (await db.scalars(sa.select(m.CredentialDepartment))).all()
        assert rows == []


async def test_deleting_a_login_drops_its_enablements(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    cred = await credential_row(app_session, tenant, owner=None)
    dept = await _department(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(m.CredentialDepartment(tenant_id=tenant, credential_id=cred, department_id=dept))
    async with app_session(tenant) as db:
        await db.execute(sa.delete(m.Credential).where(m.Credential.id == cred))
    async with app_session(tenant) as db:
        assert (await db.scalars(sa.select(m.CredentialDepartment))).all() == []


async def test_another_tenant_cannot_see_enablements(app_session: AppSessionFactory) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    cred = await credential_row(app_session, tenant, owner=None)
    dept = await _department(app_session, tenant)
    async with app_session(tenant) as db:
        db.add(m.CredentialDepartment(tenant_id=tenant, credential_id=cred, department_id=dept))
    async with app_session(other) as db:
        assert (await db.scalars(sa.select(m.CredentialDepartment))).all() == []
    async with app_session(tenant) as db:
        assert len((await db.scalars(sa.select(m.CredentialDepartment))).all()) == 1


async def test_a_login_defaults_to_no_departments(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    cred = await credential_row(app_session, tenant, owner=None)
    async with app_session(tenant) as db:
        row = await db.get(m.Credential, cred)
        assert row is not None
        assert row.all_departments is False
        assert row.consolidated_from is None
