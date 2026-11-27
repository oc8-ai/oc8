"""A restricted answer says so in its DTO: the chat shows its HTML previews
without scripts."""

from __future__ import annotations

import uuid

import pytest

from oc8.api.v1.chat import _message_dto
from tests.chat.test_message_tree import _reply, _send, _session
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


async def test_message_dto_carries_restricted(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        session = await _session(db, tenant)
        question, run = await _send(db, session, "question")
        run.context = {**run.context, "contains_restricted": True}
        reply = await _reply(db, run, "```html\n<p>x</p>\n```")
        assert (await _message_dto(reply, db, tenant)).restricted is True
        assert (await _message_dto(question, db, tenant)).restricted is False
