# backend/tests/db/test_migration_0071.py
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from oc8.config import get_settings

pytestmark = pytest.mark.asyncio


async def test_totp_credential_table_and_new_columns_exist() -> None:
    engine = create_async_engine(get_settings().migration_async_url)
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda c: {
                    row[0]
                    for row in c.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'totp_credential'"
                        )
                    )
                }
            )
            assert cols == {
                "id", "tenant_id", "member_id", "secret_ref", "enrolled_at",
                "backup_codes", "created_at", "updated_at",
            }
            org_member_cols = await conn.run_sync(
                lambda c: {
                    row[0]
                    for row in c.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'org_member' "
                            "AND column_name = 'totp_grace_started_at'"
                        )
                    )
                }
            )
            assert org_member_cols == {"totp_grace_started_at"}
            # 0071 also added organization.totp_step_up_enabled; 0127 drops it
            # again (see test_migration_0127.py), so it is not checked at head.
    finally:
        await engine.dispose()
