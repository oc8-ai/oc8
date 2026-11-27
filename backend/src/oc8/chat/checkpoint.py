"""Chat checkpoints: a long chat summarised, so its replay fits the window.

Spec §5.3. Once a reply is recorded and the replayed path fills more than
`BACKGROUND_RATIO` of the session model's window, a background job summarises
everything up to a cut that keeps about `TAIL_RATIO` of the window verbatim,
and stores the summary as a `chat_checkpoint`. A replay starts from the newest
checkpoint on the person's path -- one on another branch is never used -- and
replays only the messages after it. When the replay still fills more than
`SYNC_RATIO` of the window at run start, the run makes a checkpoint first;
when that fails, the oldest messages are dropped until the rest fits. Neither
ever fails the turn.

The summary is written by the session's own model, metered like any other
call. A restricted answer reaches the summary input only when that model is
local; otherwise it is the replay's placeholder, as it is for the run.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agent.harness.prompts import compaction_instruction
from oc8.agent.harness.stages.a_compaction import prompt_token_fallback
from oc8.chat.model_choice import session_model_config
from oc8.chat.replay import DEFAULT_WINDOW, merged, message_turns
from oc8.modelrouter.fallback import complete_with_fallback
from oc8.modelrouter.router import get_model_router, locality_for_provider
from oc8.modelrouter.types import ModelParams, NeutralMessage, TextPart

logger = logging.getLogger(__name__)

#: Above this share of the window, a recorded reply starts a background checkpoint.
BACKGROUND_RATIO: Final = 0.40
#: The share of the window a checkpoint keeps verbatim after its cut.
TAIL_RATIO: Final = 0.15
#: Above this share at run start, the run makes a checkpoint before it starts.
SYNC_RATIO: Final = 0.70

#: Opens the summary where the replay puts it, in place of the turns it covers.
SUMMARY_HEADER: Final = "[Conversation so far]"
#: Ends a message the replay had to shorten to fit the window.
CUT_NOTE: Final = "[… cut]"
#: Opens a replay that had to drop its oldest messages to fit the window.
EARLIER_CUT: Final = "[Earlier messages were cut to fit the context window]"
#: The chat summary's sections (spec §5.3), asked for after the in-run
#: compaction's own instruction.
SUMMARY_TEMPLATE: Final = (
    "This is a chat conversation between a person and you, not a single run. "
    "Write the summary under these headings instead:\n"
    "Goal of the conversation\n"
    "Decisions made\n"
    "Facts, files and objects referenced (with ids)\n"
    "Open questions\n"
    "Current work state\n"
    "Next move"
)
#: The longest summary the model may write.
_SUMMARY_MAX_TOKENS: Final = 2000


def estimate_tokens(messages: list[NeutralMessage]) -> int:
    """The token estimate the in-run compaction uses: chars // 4."""
    return prompt_token_fallback(messages)


def summary_turn(checkpoint: m.ChatCheckpoint) -> NeutralMessage:
    """A checkpoint as the replay shows it: one user turn."""
    return NeutralMessage(role="user", content=f"{SUMMARY_HEADER}\n{checkpoint.summary}")


def context_window_of(run_context: Mapping[str, Any] | None) -> int:
    """The window of the model a run's last leg used, else the default."""
    window = (run_context or {}).get("context_window_tokens")
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        return window
    return DEFAULT_WINDOW


async def newest_checkpoint_on_path(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    path_ids: list[uuid.UUID],
    restricted_ok: bool = False,
) -> m.ChatCheckpoint | None:
    """The newest checkpoint whose `upto` is on the path (of two written at
    once, the one further along it): one on another branch never is. A
    restricted one only when `restricted_ok` (fail closed: only a caller that
    sends nothing to a model may skip it)."""
    if not path_ids:
        return None
    query = select(m.ChatCheckpoint).where(
        m.ChatCheckpoint.session_id == session_id,
        m.ChatCheckpoint.upto_message_id.in_(path_ids),
    )
    if not restricted_ok:
        query = query.where(m.ChatCheckpoint.restricted.is_(False))
    position = {message_id: index for index, message_id in enumerate(path_ids)}
    rows = list((await db.execute(query)).scalars())
    return max(rows, key=lambda row: (row.created_at, position[row.upto_message_id]), default=None)


def _start_after(path: list[m.ChatMessage], checkpoint: m.ChatCheckpoint | None) -> int:
    """The index of the first message after `checkpoint` on `path`."""
    if checkpoint is None:
        return 0
    for index, message in enumerate(path):
        if message.id == checkpoint.upto_message_id:
            return index + 1
    return 0


def _cut(tokens: list[int], *, start: int, window: int) -> int | None:
    """The index of the last message a checkpoint summarises: the messages
    after it are the newest ones that fit `TAIL_RATIO` of the window, and the
    newest message always stays verbatim. None when nothing after `start`
    is left to summarise."""
    target = TAIL_RATIO * window
    index = len(tokens) - 1
    tail = tokens[index]
    index -= 1
    while index >= start and tail + tokens[index] <= target:
        tail += tokens[index]
        index -= 1
    return index if index >= start else None


async def _session_agent(db: AsyncSession, *, session: m.ChatSession) -> m.Agent | None:
    """The chat's own agent, or None when it is gone."""
    agent = await db.get(m.Agent, session.agent_id)
    if agent is None or agent.tenant_id != session.tenant_id:
        return None
    return agent


async def session_model_locality(
    db: AsyncSession, *, session: m.ChatSession, agent: m.Agent
) -> str:
    """The locality of the model the session's runs use, as the engine
    derives it."""
    config, provider, _model = await session_model_config(db, session=session, agent=agent)
    return config.locality if config is not None else locality_for_provider(provider)


async def _session_model(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    agent: m.Agent,
    messages: list[NeutralMessage],
    contains_restricted: bool,
) -> tuple[m.ModelConfig | None, str, str]:
    """(primary, provider, model) for the summary: the session's own model,
    not the cheapest tier. An Auto config routes `messages` as a run would."""
    from oc8.modelrouter.auto_router import is_auto_config, resolve_auto_config

    config, provider, model = await session_model_config(db, session=session, agent=agent)
    if is_auto_config(config):
        assert config is not None
        config, _decision = await resolve_auto_config(
            db,
            config,
            tenant_id=session.tenant_id,
            agent_id=agent.id,
            agent=agent,
            messages=messages,
            contains_restricted=contains_restricted,
        )
        return config, config.provider, config.model
    return config, provider, model


def summary_prompt(
    turns: list[NeutralMessage], *, base: m.ChatCheckpoint | None
) -> list[NeutralMessage]:
    """The summary input: the earlier checkpoint, the turns it summarises,
    then the instruction with the chat's template."""
    instruction = NeutralMessage(
        role="user", content=f"{compaction_instruction()}\n\n{SUMMARY_TEMPLATE}"
    )
    head = [summary_turn(base)] if base is not None else []
    return merged([*head, *turns, instruction])


async def make_checkpoint(
    db: AsyncSession,
    *,
    session: m.ChatSession,
    path: list[m.ChatMessage],
    window: int,
    model_locality: str,
    agent: m.Agent | None = None,
) -> m.ChatCheckpoint | None:
    """Summarise `path` up to the cut that keeps about `TAIL_RATIO * window`
    verbatim, on top of the newest checkpoint before it, and store it. None
    when there is nothing to cut or no model to ask. A model failure raises.

    A restricted answer goes into the summary only when both `model_locality`
    (the run's model) and the summarising model are local. `agent` is the
    session's agent, when the caller has read it already."""
    from oc8.metering import check_budget, record_usage

    if len(path) < 2:
        return None
    if agent is None:
        agent = await _session_agent(db, session=session)
    if agent is None:
        return None
    budget = await check_budget(db, tenant_id=session.tenant_id, department_id=agent.department_id)
    if budget.hard_exceeded:
        return None
    withheld_turns = await message_turns(db, path, model_locality="cloud")
    has_restricted = any(message.restricted for message in path)
    primary, provider, model = await _session_model(
        db,
        session=session,
        agent=agent,
        messages=withheld_turns,
        contains_restricted=has_restricted and model_locality == "local",
    )
    summariser = primary.locality if primary is not None else locality_for_provider(provider)
    locality = "local" if model_locality == "local" and summariser == "local" else "cloud"
    turns = (
        withheld_turns
        if locality != "local" or not has_restricted
        else await message_turns(db, path, model_locality="local")
    )
    base = await newest_checkpoint_on_path(
        db,
        session_id=session.id,
        path_ids=[message.id for message in path],
        restricted_ok=locality == "local",
    )
    start = _start_after(path, base)
    cut = _cut([estimate_tokens([turn]) for turn in turns], start=start, window=window)
    if cut is None:
        return None
    restricted = locality == "local" and (
        any(message.restricted for message in path[start : cut + 1])
        or (base is not None and base.restricted)
    )
    request_id = uuid.uuid4()
    result = await complete_with_fallback(
        db,
        get_model_router(),
        tenant_id=session.tenant_id,
        agent_id=agent.id,
        primary=primary,
        no_config_provider=provider,
        no_config_model=model,
        messages=summary_prompt(turns[start : cut + 1], base=base),
        tools=[],
        params=ModelParams(temperature=0.2, max_tokens=_SUMMARY_MAX_TOKENS),
        request_id=request_id,
        contains_restricted=restricted,
    )
    try:
        async with db.begin_nested():
            await record_usage(
                db,
                tenant_id=session.tenant_id,
                request_id=request_id,
                model=result.model,
                provider=result.provider,
                tokens_in=result.usage.tokens_in,
                tokens_out=result.usage.tokens_out,
                cache_read_tokens=result.usage.cache_read_tokens,
                cache_write_tokens=result.usage.cache_write_tokens,
                agent_id=agent.id,
                department_id=agent.department_id,
                member_id=session.member_id,
            )
    except Exception:
        logger.warning("chat checkpoint: could not record usage", exc_info=True)
    summary = (result.text or "").strip()
    if not summary:
        return None
    checkpoint = m.ChatCheckpoint(
        tenant_id=session.tenant_id,
        session_id=session.id,
        upto_message_id=path[cut].id,
        summary=summary,
        token_estimate=len(summary) // 4,
        model=result.model or model,
        restricted=restricted,
    )
    db.add(checkpoint)
    await db.flush()
    return checkpoint


def _text_of(turn: NeutralMessage) -> str:
    if isinstance(turn.content, str):
        return turn.content
    return "".join(part.text for part in turn.content if isinstance(part, TextPart))


def tail_fit(turns: list[NeutralMessage], *, budget: int) -> list[NeutralMessage]:
    """The newest turns that fit `budget` tokens. The newest one that does
    not fit whole is cut to the room left, with `CUT_NOTE`; the ones before
    it are dropped, and the replay says so."""
    # Counted in text chars, because `estimate_tokens` is joined text chars
    # // 4: change one and the other must follow. The note on a dropped
    # start and its separator are reserved up front.
    room = budget * 4 - len(EARLIER_CUT) - 2
    kept: list[NeutralMessage] = []
    for turn in reversed(turns):
        text = _text_of(turn)
        if len(text) <= room:
            kept.append(turn)
            room -= len(text)
            continue
        room -= len(CUT_NOTE) + 2
        if room > 0:
            kept.append(NeutralMessage(role=turn.role, content=f"{text[:room]}\n\n{CUT_NOTE}"))
        break
    kept.reverse()
    if len(kept) < len(turns):
        kept.insert(0, NeutralMessage(role="user", content=EARLIER_CUT))
    return kept


async def bounded_turns(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    path: list[m.ChatMessage],
    turns: list[NeutralMessage],
    window: int,
    model_locality: str,
) -> list[NeutralMessage]:
    """`turns` (one per message on `path`) as the replay sends them: from the
    newest checkpoint on the path this model may see, then the sync rule. A
    replay still above `SYNC_RATIO` of the window has no fresh checkpoint --
    the newest one lies before every message a cut would summarise -- so it
    makes one now; when that fails or is not enough, the newest turns that
    fit are kept."""
    base = await newest_checkpoint_on_path(
        db,
        session_id=session_id,
        path_ids=[message.id for message in path],
        restricted_ok=model_locality == "local",
    )

    def _from(checkpoint: m.ChatCheckpoint | None) -> list[NeutralMessage]:
        rest = turns[_start_after(path, checkpoint) :]
        return [summary_turn(checkpoint), *rest] if checkpoint is not None else rest

    replayed = _from(base)
    budget = int(SYNC_RATIO * window)
    if estimate_tokens(replayed) <= budget:
        return replayed
    start = _start_after(path, base)
    cut = _cut([estimate_tokens([turn]) for turn in turns], start=start, window=window)
    if cut is not None:
        session = await db.get(m.ChatSession, session_id)
        made = None
        if session is not None:
            try:
                async with db.begin_nested():
                    made = await make_checkpoint(
                        db, session=session, path=path, window=window, model_locality=model_locality
                    )
            except Exception:
                logger.warning(
                    "chat checkpoint at run start for session %s failed; replaying what fits",
                    session_id,
                    exc_info=True,
                )
        if made is not None:
            replayed = _from(made)
    if estimate_tokens(replayed) <= budget:
        return replayed
    return tail_fit(replayed, budget=budget)


async def replayed_estimate(
    db: AsyncSession, *, session: m.ChatSession, path: list[m.ChatMessage]
) -> int:
    """The estimated size of `path` as a replay sends it, from its newest
    checkpoint on."""
    # Measures size only, nothing reaches a model: a restricted one counts.
    base = await newest_checkpoint_on_path(
        db, session_id=session.id, path_ids=[message.id for message in path], restricted_ok=True
    )
    rest = path[_start_after(path, base) :]
    turns = await message_turns(db, rest, model_locality="cloud")
    return estimate_tokens([summary_turn(base), *turns] if base is not None else turns)


async def checkpoint_after_reply(
    db: AsyncSession, *, session_id: uuid.UUID, run_id: uuid.UUID
) -> m.ChatCheckpoint | None:
    """The background job: summarise the path that ends at `run_id`'s reply.
    Nothing when a newer checkpoint already covers the cut."""
    from oc8.chat import tree

    session = await db.get(m.ChatSession, session_id)
    run = await db.get(m.AgentRun, run_id)
    # A temporary chat is never summarised in the background (`chat.temporary`).
    if session is None or run is None or session.temporary:
        return None
    reply_id = await db.scalar(
        select(m.ChatMessage.id)
        .where(
            m.ChatMessage.session_id == session.id,
            m.ChatMessage.run_id == run.id,
            m.ChatMessage.role == "assistant",
        )
        .order_by(m.ChatMessage.id.desc())
        .limit(1)
    )
    if reply_id is None:
        return None
    agent = await _session_agent(db, session=session)
    if agent is None:
        return None
    path = await tree.path_to(db, session=session, message_id=reply_id)
    return await make_checkpoint(
        db,
        session=session,
        path=path,
        window=context_window_of(run.context),
        model_locality=await session_model_locality(db, session=session, agent=agent),
        agent=agent,
    )


def _spawn_task(job: Callable[[], Coroutine[Any, Any, None]]) -> None:
    task = asyncio.get_running_loop().create_task(job())
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)


#: Live references to running checkpoint jobs: an unreferenced task can be
#: garbage-collected mid-flight.
_jobs: set[asyncio.Task[None]] = set()
#: How a checkpoint job is started. Swapped out in tests, where nothing runs
#: in the background unless a test runs it.
_spawn: Callable[[Callable[[], Coroutine[Any, Any, None]]], None] = _spawn_task


def schedule_checkpoint(*, tenant_id: uuid.UUID, session_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """Start a checkpoint of the chat in the background, on a session of its
    own. Never raises: without it, the next run's start bounds the replay."""

    async def _job() -> None:
        from oc8.db.session import tenant_session

        try:
            async with tenant_session(tenant_id) as db:
                await checkpoint_after_reply(db, session_id=session_id, run_id=run_id)
        except Exception:
            logger.warning("chat checkpoint for session %s failed", session_id, exc_info=True)

    _spawn(_job)
