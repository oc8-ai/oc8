"""The member a token usage was spent for: stored by record_usage so the Cost
Center can chart usage per person; NULL when the caller names none."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from oc8 import models as m
from oc8.metering.usage import record_usage
from tests.conftest import AppSessionFactory


async def _stored_member(
    app_session: AppSessionFactory, tenant: uuid.UUID, request_id: uuid.UUID
) -> uuid.UUID | None:
    async with app_session(tenant) as db:
        return (
            await db.execute(
                select(m.TokenUsageRecord.member_id).where(
                    m.TokenUsageRecord.request_id == request_id
                )
            )
        ).scalar_one()


async def test_record_usage_stores_member_id(app_session: AppSessionFactory) -> None:
    tenant, request_id, member = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await record_usage(
            db,
            tenant_id=tenant,
            request_id=request_id,
            model="m",
            provider="p",
            tokens_in=1,
            tokens_out=2,
            member_id=member,
        )
    assert await _stored_member(app_session, tenant, request_id) == member


async def test_record_usage_without_member_keeps_null(app_session: AppSessionFactory) -> None:
    tenant, request_id = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        await record_usage(
            db,
            tenant_id=tenant,
            request_id=request_id,
            model="m",
            provider="p",
            tokens_in=1,
            tokens_out=2,
        )
    assert await _stored_member(app_session, tenant, request_id) is None
