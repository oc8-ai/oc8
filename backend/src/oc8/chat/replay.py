"""A chat's earlier turns as the in-process engine replays them.

The conversation reaches the model as real user/assistant turns, read from the
database at run start (spec §5.1): a person's message as a user turn, an
answer as an assistant turn followed by what its run did (the turn's work
digest), a follow-up or oc8 notice as a user turn under its speaker label.
Only the path to `transcript_until` is read, so a branch the person is not on
never reaches the prompt. Earlier images are named, never loaded.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.caps import ModelCaps
from oc8.chat import tree
from oc8.chat.turn_context import _speaker
from oc8.modelrouter import NeutralMessage

#: What a restricted answer becomes for a model that is not local (spec
#: §5.1a): its text and its digest may both hold restricted data.
RESTRICTED_PLACEHOLDER: Final = "[restricted answer withheld from this model]"
#: Opens the digest block under an answer's text.
WORK_DONE_HEADER: Final = "[Work done in this turn]"
#: An answer without text: its run ended with tool calls only, or failed.
NO_ANSWER: Final = "[no answer — run ended: {state}]"
#: The window a replay is bounded by when the caller names none.
DEFAULT_WINDOW: Final = ModelCaps().context_window_tokens


@dataclass(frozen=True)
class ChatReplay:
    """Which conversation a run replays, and up to which message."""

    session_id: uuid.UUID
    until_id: uuid.UUID | None


def chat_replay_of(run_context: Mapping[str, Any]) -> ChatReplay | None:
    """The replay a chat turn's run asks for: a chat session and the
    `transcript_until` key (a first turn's None included). Any other run --
    a trigger, a delegation, a wake-up -- replays nothing."""
    session_id = run_context.get("chat_session_id")
    if not session_id or "transcript_until" not in run_context:
        return None
    until = run_context["transcript_until"]
    return ChatReplay(
        session_id=uuid.UUID(str(session_id)),
        until_id=uuid.UUID(str(until)) if until is not None else None,
    )


def prompt_cache_key(run_context: Mapping[str, Any] | None, run_id: uuid.UUID | None) -> str | None:
    """The provider cache key of a run's completions. A chat turn shares its
    session's key, so the replayed prefix hits the cache across the turns of
    one chat (spec §5.2); the run context may name another; any other run
    keeps its own id, as does a chat run whose context cannot be read -- a
    cache key never fails a step."""
    context = run_context or {}
    explicit = context.get("prompt_cache_key")
    if explicit:
        return str(explicit)
    try:
        is_chat_turn = chat_replay_of(context) is not None
    except (TypeError, ValueError):
        is_chat_turn = False
    if is_chat_turn:
        return str(context["chat_session_id"])
    return str(run_id) if run_id is not None else None


def new_turn_text(run_context: Mapping[str, Any], task_text: str) -> str:
    """The new turn as the replay shows a person's message: without the
    "User: " label the run's task carries. A follow-up or notice keeps its
    label, as the replayed ones do."""
    if run_context.get("turn_role") == "user":
        return task_text.removeprefix(f"{_speaker('user')}: ")
    return task_text


def withheld(message: m.ChatMessage, *, model_locality: str) -> bool:
    """Whether `message` may not reach a model of `model_locality`."""
    return message.restricted and model_locality != "local"


async def replay_path(db: AsyncSession, *, replay: ChatReplay) -> list[m.ChatMessage]:
    """Root to `replay.until_id`, on the path that message is on."""
    if replay.until_id is None:
        return []
    session = await db.get(m.ChatSession, replay.session_id)
    if session is None:
        return []
    return await tree.path_to(db, session=session, message_id=replay.until_id)


def last_answer(path: list[m.ChatMessage], *, model_locality: str) -> str | None:
    """The newest answer's text on `path`, for the retrieval query; None when
    there is none, it is empty, or this model may not see it."""
    for message in reversed(path):
        if message.role == "assistant":
            if withheld(message, model_locality=model_locality) or not message.content:
                return None
            return message.content
    return None


async def _file_notes(db: AsyncSession, path: list[m.ChatMessage]) -> dict[uuid.UUID, list[str]]:
    """Each user message's attachments by name (metadata only, no bytes)."""
    ids = [message.id for message in path if message.role == "user"]
    if not ids:
        return {}
    rows = await db.execute(
        select(m.FileAttachment.owner_id, m.FileAttachment.filename, m.FileAttachment.is_image)
        .where(
            m.FileAttachment.owner_type == "chat_message",
            m.FileAttachment.owner_id.in_(ids),
        )
        .order_by(m.FileAttachment.created_at, m.FileAttachment.id)
    )
    notes: dict[uuid.UUID, list[str]] = {}
    for owner_id, filename, is_image in rows.all():
        notes.setdefault(owner_id, []).append(
            f"[image: {filename}]" if is_image else f"[Attached file: {filename}]"
        )
    return notes


async def _run_states(db: AsyncSession, path: list[m.ChatMessage]) -> dict[uuid.UUID, str]:
    """The run state of each answer that has no text."""
    ids = [
        message.run_id
        for message in path
        if message.role == "assistant" and not message.content and message.run_id is not None
    ]
    if not ids:
        return {}
    rows = await db.execute(select(m.AgentRun.id, m.AgentRun.state).where(m.AgentRun.id.in_(ids)))
    return {run_id: state for run_id, state in rows.all()}


def _assistant_text(
    message: m.ChatMessage, *, model_locality: str, states: Mapping[uuid.UUID, str]
) -> str:
    if withheld(message, model_locality=model_locality):
        return RESTRICTED_PLACEHOLDER
    text = message.content
    if not text:
        state = states.get(message.run_id, "unknown") if message.run_id else "unknown"
        text = NO_ANSWER.format(state=state)
    if message.work_digest:
        text = f"{text}\n\n{WORK_DONE_HEADER}\n{message.work_digest}"
    return text


async def message_turns(
    db: AsyncSession, path: list[m.ChatMessage], *, model_locality: str
) -> list[NeutralMessage]:
    """Each message on `path` as the model turn it is replayed as, one per
    message and not merged yet."""
    notes = await _file_notes(db, path)
    states = await _run_states(db, path)
    turns: list[NeutralMessage] = []
    for message in path:
        if message.role == "assistant":
            content = _assistant_text(message, model_locality=model_locality, states=states)
            turns.append(NeutralMessage(role="assistant", content=content))
        elif message.role == "user":
            files = notes.get(message.id)
            content = "\n".join([message.content, *files]) if files else message.content
            turns.append(NeutralMessage(role="user", content=content))
        else:
            content = f"{_speaker(message.role, message.content)}: {message.content}"
            turns.append(NeutralMessage(role="user", content=content))
    return turns


def merged(turns: list[NeutralMessage]) -> list[NeutralMessage]:
    """Neighbours of the same role as one message, for providers that
    require strict alternation."""
    out: list[NeutralMessage] = []
    for turn in turns:
        if out and out[-1].role == turn.role:
            previous = out[-1]
            out[-1] = NeutralMessage(
                role=previous.role, content=f"{previous.content}\n\n{turn.content}"
            )
        else:
            out.append(turn)
    return out


async def replay_messages(
    db: AsyncSession,
    *,
    replay: ChatReplay,
    model_locality: str,
    path: list[m.ChatMessage] | None = None,
    window: int = DEFAULT_WINDOW,
) -> list[NeutralMessage]:
    """The conversation before the new turn, as alternating model turns,
    within `window` (spec §5.3): from the newest checkpoint on the path, with
    a checkpoint made on the spot when the replay would still fill more than
    `SYNC_RATIO` of the window. `path` is `replay_path`, when the caller has
    read it already."""
    from oc8.chat.checkpoint import bounded_turns

    if path is None:
        path = await replay_path(db, replay=replay)
    if not path:
        return []
    turns = await message_turns(db, path, model_locality=model_locality)
    bounded = await bounded_turns(
        db,
        session_id=replay.session_id,
        path=path,
        turns=turns,
        window=window,
        model_locality=model_locality,
    )
    return merged(bounded)
