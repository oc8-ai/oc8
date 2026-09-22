"""Create a tenant's structural baseline (design: tenant-provisioning).

Extracted from `_seed_globex`, which was already the minimal-tenant recipe.
Everything the codebase creates on demand is deliberately omitted: model
configs (the engine falls back to `settings.default_model*`), memory stores
(created on first write), the tenant DEK, budgets, supervision policies and
knowledge bases. A new tenant starts empty and stays correct.

Must run under the schema-owner session (`settings.migration_async_url`): the
app role cannot insert an `organization` row, because migrations 0013/0015
constrain the INSERT to `id = current_setting('app.tenant_id')`.

add/flush only -- never commits. The caller owns the transaction.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.audit import append_event
from oc8.authz.permissions import role_kind
from oc8.seed import BUILTIN_ROLES, det
from oc8.seed.department_templates import seed_department_templates

VALID_TIERS = ("standard", "enterprise", "onprem")


async def get_singleton_organization(db: AsyncSession) -> m.Organization:
    """Resolve the singleton Organization for Community single-instance.

    Per spec §3.2, Community has exactly one active Organization (root).
    Shared by password auth (`api/v1/auth.py`) and the outward MCP gateway's
    API-key auth (`api/mcp_external.py`), both of which have to resolve a
    tenant from a credential that carries no tenant hint of its own.

    Raises:
        HTTPException(404): if no Organization exists (setup not complete)
        HTTPException(409): if multiple Organizations exist (data corruption)
    """
    result = await db.execute(select(m.Organization))
    orgs = result.scalars().all()

    if len(orgs) == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Instance not initialized. No Organization found.",
        )

    if len(orgs) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Multi-organization configuration detected. Setup is ambiguous.",
        )

    return orgs[0]

# The built-in template ships `memory: {}`, which DENIES department-tier memory
# writes (memory/policy.py). A tenant created with that frame would get agents
# that silently cannot remember anything, so the first department overrides it.
DEFAULT_DEPARTMENT_FRAME: dict[str, Any] = {
    "tools": {},
    "kbs": [],
    "memory": {"department": ["read", "write"], "company": ["read"]},
}


class TenantExists(Exception):
    """A tenant with this slug already exists (slug is globally unique)."""


@dataclass(frozen=True)
class TenantCreated:
    tenant_id: uuid.UUID
    slug: str
    department_id: uuid.UUID


@dataclass(frozen=True)
class TenantSummary:
    tenant_id: uuid.UUID
    slug: str
    name: str
    tier: str
    region: str


def _slugify(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return out or "general"


async def slug_is_free(db: AsyncSession, *, slug: str) -> bool:
    row = (
        await db.execute(select(m.Organization.id).where(m.Organization.slug == slug))
    ).first()
    return row is None


async def create_tenant(
    db: AsyncSession,
    *,
    slug: str,
    name: str,
    tier: str = "standard",
    region: str = "eu",
    department_name: str = "General",
    tenant_id: uuid.UUID | None = None,
) -> TenantCreated:
    if tier not in VALID_TIERS:
        raise ValueError(f"invalid tier {tier!r}; expected one of {', '.join(VALID_TIERS)}")
    if not await slug_is_free(db, slug=slug):
        raise TenantExists(f"slug already taken: {slug}")

    tid = tenant_id or uuid.uuid4()
    db.add(
        m.Organization(id=tid, slug=slug, name=name, tier=tier, region=region, settings={})
    )

    # Deterministic ids, so running this twice is a primary-key conflict rather
    # than a second set of roles. Since 0047 `role` also carries UNIQUE
    # (tenant_id, lower(name)), so the name is protected too -- which matters
    # now that a name is something a human types rather than a constant.
    # `kind` per name and not uniform -- one of these five is the AGENT's role,
    # and the two resolvers that read this table each take exactly one kind.
    for role_name in BUILTIN_ROLES:
        db.add(
            m.Role(
                id=det(tid, "role", role_name),
                tenant_id=tid,
                name=role_name,
                builtin=True,
                kind=role_kind(role_name),
            )
        )

    dept_slug = _slugify(department_name)
    dept_id = det(tid, "dept", dept_slug)
    db.add(
        m.Department(
            id=dept_id,
            tenant_id=tid,
            name=department_name,
            goal="",
            frame=dict(DEFAULT_DEPARTMENT_FRAME),
            presentation={"icon": "building", "slug": dept_slug},
        )
    )

    # Required: there is no POST /departments endpoint. The only runtime path to
    # a department is POST /plugins/{id}/instantiate-department, which needs an
    # installed department_template plugin version in this tenant.
    await seed_department_templates(db, tenant_id=tid)

    await db.flush()
    await append_event(
        db,
        tenant_id=tid,
        actor_type="system",
        actor_id=None,
        category="admin",
        action="tenant.created",
        resource={"tenant": slug},
    )
    return TenantCreated(tenant_id=tid, slug=slug, department_id=dept_id)


async def list_tenants(db: AsyncSession) -> list[TenantSummary]:
    rows = (
        (await db.execute(select(m.Organization).order_by(m.Organization.slug)))
        .scalars()
        .all()
    )
    return [
        TenantSummary(
            tenant_id=o.id, slug=o.slug, name=o.name, tier=o.tier, region=o.region
        )
        for o in rows
    ]
