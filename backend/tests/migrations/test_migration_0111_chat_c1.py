"""0111 adds the chat override/author/reply columns, parts and chat_enabled."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from oc8 import models as m
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_override_author_and_reply_link_round_trip(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    model_id, member_id = uuid.uuid4(), uuid.uuid4()
    async with app_session(tenant) as db:
        session = m.ChatSession(
            tenant_id=tenant, agent_id=uuid.uuid4(), member_id=member_id, model_override_id=model_id
        )
        db.add(session)
        await db.flush()
        user_turn = m.ChatMessage(
            tenant_id=tenant,
            session_id=session.id,
            role="user",
            content="hi",
            author_member_id=member_id,
        )
        db.add(user_turn)
        await db.flush()
        reply = m.ChatMessage(
            tenant_id=tenant,
            session_id=session.id,
            role="assistant",
            content="hello",
            reply_to_id=user_turn.id,
            parts=[{"type": "text", "text": "hello"}],
        )
        db.add(reply)
        await db.flush()
        session_id, reply_id = session.id, reply.id
    async with app_session(tenant) as db:
        loaded = await db.get(m.ChatSession, session_id)
        loaded_reply = await db.get(m.ChatMessage, reply_id)
        assert loaded is not None and loaded.model_override_id == model_id
        assert loaded_reply is not None and loaded_reply.reply_to_id is not None
        assert loaded_reply.parts == [{"type": "text", "text": "hello"}]


async def test_existing_rows_read_back_with_nulls(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = m.ChatSession(tenant_id=tenant, agent_id=uuid.uuid4(), member_id=uuid.uuid4())
        db.add(session)
        await db.flush()
        assert session.model_override_id is None
        msg = m.ChatMessage(tenant_id=tenant, session_id=session.id, role="user", content="x")
        db.add(msg)
        await db.flush()
        assert msg.parts is None


async def test_model_config_chat_enabled_defaults_true(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cfg = m.ModelConfig(tenant_id=tenant, provider="openai", model="gpt-x")
        db.add(cfg)
        await db.flush()
        await db.refresh(cfg)
        assert cfg.chat_enabled is True
        default = (
            await db.execute(
                sa.text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name='model_config' AND column_name='chat_enabled'"
                )
            )
        ).scalar_one()
        assert "true" in str(default).lower()
