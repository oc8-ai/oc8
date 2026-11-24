"""A chat as a file (spec §5.5): Markdown for people, JSON for machines.

Markdown is the conversation the person is looking at -- the current path
only, who spoke and when, and the *names* of attached files. JSON is the whole
tree (every branch) so a later import can rebuild it. Neither carries file
contents, the chat's knowledge base, or tool steps: an export is a transcript,
not a data dump.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.service import NOTICE_PREFIX

_MAX_SLUG = 80


def _stamp(moment: dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _one_line(text: str) -> str:
    return " ".join(text.split())


def export_filename_stem(title: str) -> str:
    """The chat's title as an ASCII, lowercase, dash-separated file name stem.
    Only `a-z0-9-` survives, so the value is safe inside a header: no quotes,
    no newlines, no path separators."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")[:_MAX_SLUG].strip("-")
    return slug or "chat"


async def attachment_names(
    db: AsyncSession, *, tenant_id: uuid.UUID, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """File names per message -- never the files, their text or their index."""
    if not message_ids:
        return {}
    rows = await db.execute(
        select(m.FileAttachment.owner_id, m.FileAttachment.filename)
        .where(
            m.FileAttachment.tenant_id == tenant_id,
            m.FileAttachment.owner_type == "chat_message",
            m.FileAttachment.owner_id.in_(message_ids),
        )
        .order_by(m.FileAttachment.created_at.asc(), m.FileAttachment.id.asc())
    )
    names: dict[uuid.UUID, list[str]] = {}
    for owner_id, filename in rows:
        names.setdefault(owner_id, []).append(filename)
    return names


def _speaker(message: m.ChatMessage, agent_name: str) -> tuple[str, str]:
    """(speaker, content as written). A system notice is oc8's, not the
    person's, so it never reads as something they typed."""
    if message.role == "user":
        return "You", message.content
    if message.role == "followup":
        if message.content.startswith(NOTICE_PREFIX):
            return "oc8 (notice)", message.content[len(NOTICE_PREFIX) :]
        return "Follow-up (scheduled)", message.content
    return agent_name or "Assistant", message.content


def export_markdown(
    session: m.ChatSession,
    path: list[m.ChatMessage],
    attachments: dict[uuid.UUID, list[str]],
    *,
    agent_name: str = "",
    exported_at: dt.datetime | None = None,
) -> str:
    title = _one_line(session.title) or "Chat"
    now = exported_at or dt.datetime.now(dt.UTC)
    lines = [f"# {title}", "", f"Exported {_stamp(now)}", ""]
    for message in path:
        speaker, content = _speaker(message, agent_name)
        lines += [f"## {speaker} · {_stamp(message.created_at)}", ""]
        if message.mode:
            lines += [f"_Mode: {_one_line(message.mode)}_", ""]
        if content.strip():
            lines += [content.strip(), ""]
        names = attachments.get(message.id, [])
        if names:
            lines += ["Attachments:", ""]
            lines += [f"- {_one_line(name)}" for name in names]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_json(
    session: m.ChatSession,
    messages: list[m.ChatMessage],
    attachments: dict[uuid.UUID, list[str]] | None = None,
) -> dict[str, Any]:
    names = attachments or {}
    return {
        "id": str(session.id),
        "title": session.title,
        "currentLeafId": str(session.current_leaf_id) if session.current_leaf_id else None,
        "messages": [
            {
                "id": str(msg.id),
                "parentId": str(msg.parent_id) if msg.parent_id else None,
                "role": msg.role,
                "content": msg.content,
                "createdAt": _stamp(msg.created_at),
                "mode": msg.mode,
                "attachments": names.get(msg.id, []),
            }
            for msg in messages
        ],
    }
