"""What a chat run is told about its conversation.

A chat run's context carries the new turn only (`turn_text`, `turn_role`) and
where in the tree it follows (`transcript_until`), never a copy of the
transcript: the conversation is read from the database when the run starts.
The in-process engine replays it as messages; every other runtime still gets
one flattened text, rebuilt here byte for byte as it was built before
(`flattened_task_text`).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat import tree

#: How many prior turns to fold into the flattened task text. A chat is
#: interactive, not a long autonomous investigation -- this keeps the prompt
#: bounded without needing a separate summarization step for v1.
_TRANSCRIPT_TURNS = 20

#: How an oc8 notice turn starts (`send_message(notice=...)`): something oc8
#: tells the Copilot about this chat -- an agent the person tried to create
#: from its card was refused -- neither typed by the person nor scheduled by
#: the Copilot. Stored as a `followup` row (no new role), told apart by this.
NOTICE_PREFIX: Final = "oc8 notice: "


def _speaker(role: str, content: str = "") -> str:
    if role == "user":
        return "User"
    if role == "followup" and content.startswith(NOTICE_PREFIX):
        return "oc8 (a notice from the system, not typed by the user)"
    if role == "followup":
        return "Follow-up (scheduled by you, not typed by the user)"
    return "You (assistant)"


def transcript_lines(history: list[m.ChatMessage]) -> list[str]:
    """The last `_TRANSCRIPT_TURNS` messages of `history`, plain-labelled, one
    line each."""
    return [
        f"{_speaker(msg.role, msg.content)}: {msg.content}" for msg in history[-_TRANSCRIPT_TURNS:]
    ]


def turn_keys(
    *, turn_role: str, message: str, turn_text: str, transcript_until: uuid.UUID | None
) -> dict[str, Any]:
    """The run-context keys of one chat turn. `message` is what was said,
    `turn_text` that plus everything appended for the run; `task` is the
    turn's own labelled line, without the transcript before it."""
    return {
        "task": f"{_speaker(turn_role, message)}: {turn_text}",
        "turn_text": turn_text,
        "turn_role": turn_role,
        "transcript_until": str(transcript_until) if transcript_until is not None else None,
    }


async def flattened_task_text(db: AsyncSession, run: m.AgentRun) -> str:
    """The run's task as one text: for a chat turn the transcript up to
    `transcript_until`, plain-labelled, ending in the turn -- what `run_agent`
    and the external runtimes know nothing of "chat" beyond. Any other run's
    `task` unchanged."""
    context = run.context or {}
    task = str(context.get("task", ""))
    if "transcript_until" not in context:
        return task
    path: list[m.ChatMessage] = []
    until = context["transcript_until"]
    session_id = context.get("chat_session_id")
    if until is not None and session_id:
        session = await db.get(m.ChatSession, uuid.UUID(str(session_id)))
        if session is not None:
            path = await tree.path_to(db, session=session, message_id=uuid.UUID(str(until)))
    return "\n".join([*transcript_lines(path), task])
