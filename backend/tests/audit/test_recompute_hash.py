from __future__ import annotations

import uuid

import pytest
from tests.audit.helpers import append_now
from tests.conftest import AppSessionFactory

from oc8.audit.chain import GENESIS, recompute_hash, verify_chain

pytestmark = pytest.mark.asyncio


async def test_recompute_hash_reproduces_stored_hashes(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        first = await append_now(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="test",
            action="t.one",
        )
        second = await append_now(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="test",
            action="t.two",
            resource={"k": "v"},
            decision="allow",
            reason="because",
        )

        assert recompute_hash(first, GENESIS) == first.hash
        assert recompute_hash(second, first.hash) == second.hash


async def test_verify_chain_still_passes_after_refactor(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        for i in range(3):
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action=f"t.{i}",
            )
        await s.flush()
        assert await verify_chain(s, tenant) is True
