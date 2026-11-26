"""The member remembers the role of their last token (ruling U4).

A run with no sign-in behind it (a trigger, a schedule) has no token, and a
member with no assigned role holds nothing without one. So the gate's
first-sight upsert copies the token's role claim onto the member -- only when
it differs, so a returning person's request writes nothing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.auth.principal import Principal
from oc8.authz.scope import scope_for_principal
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _principal(tenant: uuid.UUID, role: str) -> Principal:
    return Principal(subject="pat", tenant_id=tenant, role=role, kind="operator")


async def _stored(db: AsyncSession, tenant: uuid.UUID) -> tuple[str | None, object]:
    row = (
        await db.execute(
            select(m.OrgMember.last_token_role, m.OrgMember.last_token_role_at)
            .where(m.OrgMember.tenant_id == tenant, m.OrgMember.subject == "pat")
            .execution_options(populate_existing=True)
        )
    ).one()
    return row.last_token_role, row.last_token_role_at


async def test_first_sight_stores_the_tokens_role(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        member, _scope = await scope_for_principal(db, _principal(tenant, "operator"), upsert=True)
        assert member is not None
        role, at = await _stored(db, tenant)
    assert role == "operator"
    assert at is not None


async def test_the_role_is_written_only_when_it_changes(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await scope_for_principal(db, _principal(tenant, "operator"), upsert=True)
        await db.flush()
        _role, first_at = await _stored(db, tenant)

        await scope_for_principal(db, _principal(tenant, "operator"), upsert=True)
        await db.flush()
        role, again_at = await _stored(db, tenant)
        assert (role, again_at) == ("operator", first_at), "an unchanged claim wrote the row"

        await scope_for_principal(db, _principal(tenant, "member"), upsert=True)
        await db.flush()
        role, changed_at = await _stored(db, tenant)
    assert role == "member"
    assert changed_at is not None and changed_at != first_at


async def test_an_unchanged_claim_leaves_the_row_clean(app_session: AppSessionFactory) -> None:
    """No write per request: the returning person's row is not dirty."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await scope_for_principal(db, _principal(tenant, "operator"), upsert=True)
        await db.flush()
        member, _scope = await scope_for_principal(db, _principal(tenant, "operator"), upsert=True)
        assert member is not None
        assert member not in db.dirty
