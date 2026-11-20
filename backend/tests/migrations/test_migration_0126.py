"""0126: two-factor policy -- the unused `organization.totp_step_up_enabled`
column is gone."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from oc8.config import get_settings

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
