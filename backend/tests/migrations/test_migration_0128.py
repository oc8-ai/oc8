"""0128: passkeys -- the short-lived challenge table, its RLS, and one
passkey row per credential."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from oc8 import models as m
from oc8.backup.tables import EXCLUDED_TABLES
from oc8.config import get_settings
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_the_challenge_table_exists() -> None:
    engine = create_async_engine(get_settings().migration_async_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
                {"t": "passkey_challenge"},
            )
            columns = {row[0] for row in rows}
    finally:
        await engine.dispose()
    assert {"tenant_id", "member_id", "purpose", "challenge", "expires_at", "used_at"} <= columns


def test_challenges_are_not_backed_up() -> None:
    assert "passkey_challenge" in EXCLUDED_TABLES


async def test_rls_isolates_challenges(app_session: AppSessionFactory) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant_a) as db:
        db.add(
            m.PasskeyChallenge(
                tenant_id=tenant_a,
                member_id=uuid.uuid4(),
                purpose="login",
                challenge="abc",
                expires_at=dt.datetime.now(tz=dt.UTC),
            )
        )
    async with app_session(tenant_a) as db:
        assert len((await db.execute(select(m.PasskeyChallenge))).scalars().all()) == 1
    async with app_session(tenant_b) as db:
        assert (await db.execute(select(m.PasskeyChallenge))).scalars().all() == []


async def test_a_credential_is_registered_once_per_organisation(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    data = {"credential_id": "same", "public_key": "k", "sign_count": 0}
    async with app_session(tenant) as db:
        db.add(
            m.SecondFactor(tenant_id=tenant, member_id=uuid.uuid4(), method="passkey", data=data)
        )
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(
                m.SecondFactor(
                    tenant_id=tenant, member_id=uuid.uuid4(), method="passkey", data=data
                )
            )
