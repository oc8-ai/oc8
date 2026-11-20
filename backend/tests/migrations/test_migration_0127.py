"""0127: two-factor policy and email codes -- the two new tables, their RLS,
and the unused `organization.totp_step_up_enabled` column gone."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from oc8 import models as m
from oc8.config import get_settings
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def _columns(table: str) -> set[str]:
    engine = create_async_engine(get_settings().migration_async_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": table},
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def test_the_unused_step_up_column_is_dropped() -> None:
    assert "totp_step_up_enabled" not in await _columns("organization")


async def test_the_new_tables_exist() -> None:
    assert {"id", "tenant_id", "member_id", "method", "label", "data", "last_used_at"} <= (
        await _columns("second_factor")
    )
    assert {"member_id", "purpose", "code_hash", "expires_at", "attempts", "used_at"} <= (
        await _columns("email_otp_challenge")
    )


async def test_rls_isolates_the_new_tables(app_session: AppSessionFactory) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    member = uuid.uuid4()
    async with app_session(tenant_a) as db:
        db.add(m.SecondFactor(tenant_id=tenant_a, member_id=member, method="email"))
        db.add(
            m.EmailOtpChallenge(
                tenant_id=tenant_a,
                member_id=member,
                purpose="login",
                code_hash="x",
                expires_at=dt.datetime.now(tz=dt.UTC),
            )
        )
    async with app_session(tenant_a) as db:
        assert len((await db.execute(select(m.SecondFactor))).scalars().all()) == 1
    async with app_session(tenant_b) as db:
        for model in (m.SecondFactor, m.EmailOtpChallenge):
            assert (await db.execute(select(model))).scalars().all() == []


async def test_one_email_factor_per_member(app_session: AppSessionFactory) -> None:
    tenant, member = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(m.SecondFactor(tenant_id=tenant, member_id=member, method="email"))
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(m.SecondFactor(tenant_id=tenant, member_id=member, method="email"))
