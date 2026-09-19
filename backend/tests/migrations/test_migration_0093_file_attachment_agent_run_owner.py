"""Migration 0093 widens file_attachment's ck_file_attachment_owner_type
CHECK constraint to also accept 'agent_run' (see
migrations/versions/0093_file_attachment_agent_run_owner.py). Exercises the
constraint directly with raw SQL rather than the ORM, since the ORM's own
CheckConstraint literal could drift from what the migration actually
applied to the database -- this test would catch that drift."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_INSERT = text(
    """
    INSERT INTO file_attachment
        (id, tenant_id, owner_type, owner_id, bucket_key, filename, content_type,
         size_bytes, is_image)
    VALUES
        (:id, :tenant_id, :owner_type, :owner_id, :bucket_key, :filename, :content_type,
         :size_bytes, false)
    """
)


async def test_agent_run_owner_type_is_accepted(app_session: AppSessionFactory) -> None:
    tenant_id = uuid.uuid4()
    async with app_session(tenant_id) as db:
        await db.execute(
            _INSERT,
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "owner_type": "agent_run",
                "owner_id": uuid.uuid4(),
                "bucket_key": "k",
                "filename": "report.txt",
                "content_type": "text/plain",
                "size_bytes": 5,
            },
        )
        await db.flush()


async def test_an_arbitrary_owner_type_is_still_rejected(app_session: AppSessionFactory) -> None:
    tenant_id = uuid.uuid4()
    async with app_session(tenant_id) as db:
        with pytest.raises(IntegrityError, match="ck_file_attachment_owner_type"):
            await db.execute(
                _INSERT,
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "owner_type": "something_else",
                    "owner_id": uuid.uuid4(),
                    "bucket_key": "k",
                    "filename": "report.txt",
                    "content_type": "text/plain",
                    "size_bytes": 5,
                },
            )
            await db.flush()
