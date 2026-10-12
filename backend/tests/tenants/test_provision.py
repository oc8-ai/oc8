from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.tenants.provision import (
    TenantExists,
    create_tenant,
    list_tenants,
    slug_is_free,
)
from tests.tenants.conftest import OwnerSessionFactory

pytestmark = pytest.mark.asyncio


def _slug() -> str:
    return f"acme-{uuid.uuid4().hex[:10]}"


async def test_creates_the_organization_row(owner_session: OwnerSessionFactory) -> None:
    slug = _slug()
    async with owner_session() as db:
        created = await create_tenant(db, slug=slug, name="Muster GmbH")
        org = await db.get(m.Organization, created.tenant_id)
        assert org is not None
        assert org.slug == slug
        assert org.name == "Muster GmbH"
        assert org.tier == "standard"
        assert org.region == "eu"
        assert org.settings == {}


async def test_creates_the_five_builtin_roles(owner_session: OwnerSessionFactory) -> None:
    from oc8.seed import BUILTIN_ROLES

    async with owner_session() as db:
        created = await create_tenant(db, slug=_slug(), name="R")
        rows = (
            (await db.execute(select(m.Role).where(m.Role.tenant_id == created.tenant_id)))
            .scalars()
            .all()
        )
        assert sorted(r.name for r in rows) == sorted(BUILTIN_ROLES)
        assert all(r.builtin for r in rows)


async def test_a_new_tenant_gets_a_kind_on_every_role_and_no_grants_at_all(
    owner_session: OwnerSessionFactory,
) -> None:
    """Exactly one of the five rows is the AGENT's, and the new tenant is
    configured with nothing.

    The kind half is the guard against the uniform loop: every writer of these
    rows builds all five in one `for` with no per-name discrimination, and a
    uniform `'human'` is every agent in the tenant granted no tool rights the
    moment the PDP reads the column -- an outage that looks like a runtime bug.

    The zero half is the "configures nothing" claim, asserted at the source
    rather than promised in a design. A tenant-defined role is something a
    tenant's administrator writes; provisioning inventing one would mean every
    new customer starts with authority nobody chose.
    """
    async with owner_session() as db:
        created = await create_tenant(db, slug=_slug(), name="K")
        rows = (
            (await db.execute(select(m.Role).where(m.Role.tenant_id == created.tenant_id)))
            .scalars()
            .all()
        )
        assert {r.name for r in rows if r.kind == "agent"} == {"agent_default"}
        assert len([r for r in rows if r.kind == "human"]) == 4
        assert all(r.deleted_at is None and r.created_by is None for r in rows)

        grants = (
            (
                await db.execute(
                    select(m.RolePermission).where(m.RolePermission.tenant_id == created.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert grants == []
        holders = (
            (
                await db.execute(
                    select(m.OrgMember).where(m.OrgMember.tenant_id == created.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert holders == []


async def test_installs_the_department_template(owner_session: OwnerSessionFactory) -> None:
    # Required, not cosmetic: there is no POST /departments endpoint. The only
    # runtime path to a department is POST /plugins/{id}/instantiate-department,
    # which needs an installed department_template plugin in this tenant.
    async with owner_session() as db:
        created = await create_tenant(db, slug=_slug(), name="T")
        plugin = (
            await db.execute(
                select(m.Capa).where(
                    m.Capa.tenant_id == created.tenant_id,
                    m.Capa.name == "sales-department",
                )
            )
        ).scalar_one_or_none()
        assert plugin is not None


async def test_first_department_can_write_department_memory(
    owner_session: OwnerSessionFactory,
) -> None:
    # The template ships memory:{}, which DENIES department-tier writes. A tenant
    # created with that frame would get agents that silently cannot remember.
    async with owner_session() as db:
        created = await create_tenant(db, slug=_slug(), name="M", department_name="Betrieb")
        dept = await db.get(m.Department, created.department_id)
        assert dept is not None
        assert dept.name == "Betrieb"
        assert dept.frame["memory"] == {
            "department": ["read", "write"],
            "company": ["read"],
        }
        assert dept.frame["tools"] == {}
        assert dept.frame["kbs"] == []


async def test_writes_a_tenant_created_audit_event(
    owner_session: OwnerSessionFactory,
) -> None:
    async with owner_session() as db:
        created = await create_tenant(db, slug=_slug(), name="A")
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(m.AuditEvent.tenant_id == created.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert [e.action for e in events] == ["tenant.created"]


async def test_duplicate_slug_raises_before_writing(
    owner_session: OwnerSessionFactory,
) -> None:
    slug = _slug()
    async with owner_session() as db:
        await create_tenant(db, slug=slug, name="First")
    async with owner_session() as db:
        with pytest.raises(TenantExists, match=slug):
            await create_tenant(db, slug=slug, name="Second")
        # Nothing partial was written for the second attempt.
        orgs = (
            (await db.execute(select(m.Organization).where(m.Organization.slug == slug)))
            .scalars()
            .all()
        )
        assert len(orgs) == 1


async def test_slug_is_free_agrees_with_reality(
    owner_session: OwnerSessionFactory,
) -> None:
    slug = _slug()
    async with owner_session() as db:
        assert await slug_is_free(db, slug=slug) is True
        await create_tenant(db, slug=slug, name="X")
        assert await slug_is_free(db, slug=slug) is False


async def test_tenant_ids_are_random_not_derived_from_the_slug(
    owner_session: OwnerSessionFactory,
) -> None:
    # A deterministic id would let a recreated tenant inherit orphaned rows from
    # a deleted one of the same slug.
    async with owner_session() as db:
        a = await create_tenant(db, slug=_slug(), name="A")
        b = await create_tenant(db, slug=_slug(), name="B")
        assert a.tenant_id != b.tenant_id


async def test_explicit_tenant_id_is_honoured(
    owner_session: OwnerSessionFactory,
) -> None:
    tid = uuid.uuid4()
    async with owner_session() as db:
        created = await create_tenant(db, slug=_slug(), name="E", tenant_id=tid)
        assert created.tenant_id == tid


async def test_list_tenants_includes_the_new_one(
    owner_session: OwnerSessionFactory,
) -> None:
    # Two independently-created tenants must both surface -- this is not just
    # "the last insert is visible": list_tenants must not scope to one tenant.
    slug_a, slug_b = _slug(), _slug()
    async with owner_session() as db:
        await create_tenant(db, slug=slug_a, name="Listed A")
        await create_tenant(db, slug=slug_b, name="Listed B")
        rows = await list_tenants(db)
        slugs = {r.slug for r in rows}
        assert slug_a in slugs
        assert slug_b in slugs


async def test_invalid_tier_is_rejected(owner_session: OwnerSessionFactory) -> None:
    async with owner_session() as db:
        with pytest.raises(ValueError, match="tier"):
            await create_tenant(db, slug=_slug(), name="Bad", tier="platinum")


async def test_new_tenant_has_oc8_ai_installed(
    owner_session: OwnerSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from oc8.config import get_settings

    # oc8 AI ships in the repo's top-level capas/, like in the seed test.
    monkeypatch.setenv("OC8_CAPAS_PATH", str(Path(__file__).resolve().parents[3] / "capas"))
    get_settings.cache_clear()
    slug = f"ai-{uuid.uuid4().hex[:8]}"
    async with owner_session() as db:
        created = await create_tenant(db, slug=slug, name="AI GmbH")
        capa = (
            await db.execute(
                select(m.Capa).where(
                    m.Capa.tenant_id == created.tenant_id, m.Capa.name == "opaas_ai_provider"
                )
            )
        ).scalar_one_or_none()
    get_settings.cache_clear()
    assert capa is not None
