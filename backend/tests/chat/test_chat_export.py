"""Export a chat as Markdown or JSON (spec §5.5): the current path for people,
every branch for machines, file names but never file contents."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.chat.export import export_filename_stem
from oc8.chat.service import NOTICE_PREFIX
from tests.chat.test_edit_and_switch import (
    _answer,
    _client,
    _new_chat,
    _post,
    _seed_agent,
    _token,
)
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

SECRET = "SECRET-FILE-CONTENTS"


async def _branching_chat(
    c: object, app_session: AppSessionFactory, tenant: uuid.UUID, agent_id: uuid.UUID
) -> tuple[str, dict[str, str]]:
    """u1 -> r1 -> u2 -> r2, then u2 edited: the old branch is off the path."""
    sid = await _new_chat(c, tenant, agent_id)  # type: ignore[arg-type]
    u1 = await _post(c, tenant, sid, "first question")  # type: ignore[arg-type]
    r1 = await _answer(app_session, tenant, u1["runId"], "first answer")
    u2 = await _post(c, tenant, sid, "second question")  # type: ignore[arg-type]
    await _answer(app_session, tenant, u2["runId"], "old second answer")
    r = await c.post(  # type: ignore[attr-defined]
        f"/api/v1/chat/sessions/{sid}/messages/{u2['id']}/edit",
        json={"content": "second question, edited"},
        headers=_token(tenant),
    )
    assert r.status_code == 201, r.text
    edited = r.json()
    await _answer(app_session, tenant, edited["runId"], "new second answer")
    return sid, {"u1": u1["id"], "r1": r1, "u2": u2["id"], "edited": edited["id"]}


async def test_markdown_is_the_current_path_only(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid, _ids = await _branching_chat(c, app_session, tenant, agent_id)
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=md", headers=_token(tenant))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    for kept in ("first question", "first answer", "second question, edited", "new second answer"):
        assert kept in body
    for dropped in ("old second answer",):
        assert dropped not in body
    assert "\n## You · " in body and "\n## A · " in body, "speakers: the person and the agent"
    assert "Z\n" in body, "ISO UTC timestamps"
    assert body.index("first question") < body.index("new second answer")


async def test_json_has_every_branch_and_the_tree(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid, ids = await _branching_chat(c, app_session, tenant, agent_id)
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=json", headers=_token(tenant))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    data = json.loads(r.text)
    assert data["id"] == sid
    contents = [x["content"] for x in data["messages"]]
    assert "old second answer" in contents and "new second answer" in contents
    assert "second question" in contents and "second question, edited" in contents
    assert data["currentLeafId"] is not None
    by_id = {x["id"]: x for x in data["messages"]}
    assert by_id[ids["edited"]]["parentId"] == ids["r1"] == by_id[ids["u2"]]["parentId"]
    assert by_id[ids["u1"]]["parentId"] is None
    first = by_id[ids["u1"]]
    assert set(first) == {
        "id",
        "parentId",
        "role",
        "content",
        "createdAt",
        "mode",
        "attachments",
    }


async def test_attachment_names_but_never_contents(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        u1 = await _post(c, tenant, sid, "see file")
        async with app_session(tenant) as db:
            db.add(
                m.FileAttachment(
                    tenant_id=tenant,
                    owner_type="chat_message",
                    owner_id=uuid.UUID(u1["id"]),
                    bucket_key=f"k/{uuid.uuid4()}",
                    filename="report.pdf",
                    content_type="application/pdf",
                    size_bytes=10,
                    extracted_text=SECRET,
                )
            )
        md = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=md", headers=_token(tenant))
        js = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=json", headers=_token(tenant))
    assert "report.pdf" in md.text and SECRET not in md.text
    assert "report.pdf" in js.text and SECRET not in js.text
    assert json.loads(js.text)["messages"][0]["attachments"] == ["report.pdf"]


async def test_tool_steps_and_notices(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        u1 = await _post(c, tenant, sid, "hello")
        await _answer(app_session, tenant, u1["runId"], "hi there")
        async with app_session(tenant) as db:
            last = (
                (
                    await db.execute(
                        select(m.ChatMessage)
                        .where(m.ChatMessage.session_id == uuid.UUID(sid))
                        .order_by(m.ChatMessage.id.desc())
                    )
                )
                .scalars()
                .first()
            )
            assert last is not None
            last.rendered_components = [{"type": "tool", "name": "TOOLSTEP-LEAK"}]
            last.parts = [{"type": "tool", "name": "TOOLSTEP-LEAK"}]
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=md", headers=_token(tenant))
    assert "TOOLSTEP-LEAK" not in r.text


async def test_notice_is_spoken_by_oc8(app_session: AppSessionFactory, redis_url: str) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        async with app_session(tenant) as db:
            db.add(
                m.ChatMessage(
                    tenant_id=tenant,
                    session_id=uuid.UUID(sid),
                    role="followup",
                    content=f"{NOTICE_PREFIX}the person clicked Create",
                )
            )
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=md", headers=_token(tenant))
    assert "## oc8 (notice) · " in r.text
    assert "the person clicked Create" in r.text and NOTICE_PREFIX not in r.text


async def test_no_access_is_404_and_bad_format_is_422(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant, other = uuid.uuid4(), uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=md", headers=_token(other))
        assert r.status_code == 404, r.text
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=pdf", headers=_token(tenant))
        assert r.status_code == 422, r.text
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export", headers=_token(tenant))
        assert r.status_code == 422, r.text


async def test_filename_header_is_slugified_and_safe(
    app_session: AppSessionFactory, redis_url: str
) -> None:
    tenant = uuid.uuid4()
    agent_id = await _seed_agent(app_session, tenant)
    async with _client() as c:
        sid = await _new_chat(c, tenant, agent_id)
        async with app_session(tenant) as db:
            session = await db.get(m.ChatSession, uuid.UUID(sid))
            assert session is not None
            session.title = 'Q4 "Plan"\r\nX-Evil: 1 – Übersicht'
        r = await c.get(f"/api/v1/chat/sessions/{sid}/export?format=json", headers=_token(tenant))
    assert (
        r.headers["content-disposition"] == 'attachment; filename="q4-plan-x-evil-1-ubersicht.json"'
    )


async def test_slug_rules() -> None:
    assert export_filename_stem("") == "chat"
    assert export_filename_stem("!!!") == "chat"
    assert export_filename_stem("Hello, World") == "hello-world"
    long = export_filename_stem("a" * 200)
    assert len(long) == 80
    assert export_filename_stem("a" * 79 + " b") == "a" * 79
