"""S1: a personal department is visible by id to its owner and to a caller who
holds tenant-wide `department:manage`; tenant-wide read alone is not enough."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.authz.permissions import SEAT_VIEWER
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _subject_uuid(subject: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:subject:{subject}")


def _headers(tenant: uuid.UUID, subject: str, role: str) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject=subject, role=role)
    return {"Authorization": f"Bearer {token}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _setup(app_session: AppSessionFactory) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        shared = m.Department(tenant_id=tenant, name="Vertrieb", frame={})
        db.add(shared)
        members = {}
        for subject in ("owner", "colleague", "operator", "auditor", "manager", "admin"):
            member = m.OrgMember(
                tenant_id=tenant, subject=subject, subject_uuid=_subject_uuid(subject)
            )
            db.add(member)
            members[subject] = member
        await db.flush()
        personal = m.Department(
            tenant_id=tenant,
            name="Owner",
            frame={},
            kind="personal",
            owner_member_id=members["owner"].id,
        )
        db.add(personal)
        await db.flush()
        for subject in ("owner", "colleague", "operator", "auditor", "manager", "admin"):
            db.add(
                m.OrgMemberDepartment(
                    tenant_id=tenant,
                    member_id=members[subject].id,
                    department_id=personal.id if subject == "owner" else shared.id,
                    seat_role=SEAT_VIEWER,
                )
            )
        await db.flush()
        return tenant, personal.id


@pytest.mark.parametrize(
    ("subject", "role", "status"),
    [
        ("owner", "member", 200),
        ("admin", "org_admin", 200),
        ("operator", "operator", 404),
        ("auditor", "auditor", 404),
        ("manager", "dept_manager", 200),
        ("colleague", "member", 404),
    ],
)
async def test_a_personal_department_is_visible_by_id_only_to_owner_and_manager(
    app_session: AppSessionFactory, subject: str, role: str, status: int
) -> None:
    tenant, personal = await _setup(app_session)
    async with _http() as http:
        r = await http.get(
            f"/api/v1/departments/{personal}", headers=_headers(tenant, subject, role)
        )
    assert r.status_code == status, r.text
