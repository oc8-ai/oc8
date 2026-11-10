"""0115 records where a chat's title came from: the model ('auto') or the member ('user')."""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_a_new_session_has_no_title_source(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        s = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4())
        db.add(s)
        await db.flush()
        await db.refresh(s)
        assert s.title == "" and s.title_source is None
        s.title, s.title_source = "Topic", "auto"
        await db.flush()
        await db.refresh(s)
        assert s.title_source == "auto"


async def test_only_auto_or_user(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(IntegrityError):
        async with app_session(tenant) as db:
            db.add(
                m.ChatSession(
                    tenant_id=tenant,
                    agent_id=uuid.uuid4(),
                    member_id=uuid.uuid4(),
                    title_source="model",
                )
            )
            await db.flush()


def test_existing_titles_count_as_the_members_own(pg_url: str) -> None:
    """Before 0115 only a person could name a chat: every non-empty title is
    theirs and must never be replaced by a generated one."""
    tenant = uuid.uuid4()
    named, unnamed = uuid.uuid4(), uuid.uuid4()
    dsn = pg_url.replace("+psycopg", "")
    cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    command.downgrade(cfg, "0114")
    try:
        with psycopg.connect(dsn) as conn:
            for sid, title in ((named, "VPN"), (unnamed, "")):
                conn.execute(
                    "INSERT INTO chat_session (id, tenant_id, agent_id, member_id, title) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (sid, tenant, uuid.uuid4(), uuid.uuid4(), title),
                )
    finally:
        command.upgrade(cfg, "head")
    with psycopg.connect(dsn) as conn:
        after = dict(
            conn.execute(
                "SELECT id, title_source FROM chat_session WHERE tenant_id = %s", (tenant,)
            ).fetchall()
        )
    assert after == {named: "user", unnamed: None}
