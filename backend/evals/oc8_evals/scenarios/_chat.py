"""Shared pieces of the chat_harness scenarios.

A chat's state is what the person and the product keep of it: the stored
reply row, the checkpoints, the approval cards, the usage rows, the tool calls
on the run. The scorers read those -- never the model's request or the run's
transcript.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8_evals.scoring import Check
from oc8_evals.stack import ChatFixture, ScenarioContext

#: Fifteen sentences the filler turns cycle through: office small talk that
#: names no code, no figure and no person the questions ask about.
_FILLER = (
    "The coffee machine on the second floor was descaled this morning.",
    "Someone left a stack of flyers about the summer party at the front desk.",
    "The window cleaners are booked for the whole building next Thursday.",
    "Our team calendar now shows public holidays for three countries.",
    "The meeting room projector needs a newer adapter for some laptops.",
    "The plants in the hallway are watered by the facilities team on Mondays.",
    "The canteen menu changes every week and is posted on the intranet.",
    "Parking spaces near the entrance are reserved for visitors until noon.",
    "The printer on the third floor prints double-sided by default now.",
    "New desk chairs arrive in batches over the next two months.",
    "The fire drill is announced a day in advance by email.",
    "The library corner got a few new books about project management.",
    "Bicycles can be stored in the basement room next to the lifts.",
    "The kitchen dishwasher is emptied by whoever arrives first.",
    "The lights in the stairwell switch off automatically after ten minutes.",
)


def chat(ctx: ScenarioContext) -> ChatFixture:
    if ctx.chat is None:
        raise RuntimeError(f"{ctx.task.id} is a chat scenario; set kind = 'chat'")
    return ctx.chat


def filler(n: int, sentences: int = 6) -> str:
    """The n-th filler text: deterministic, about `sentences` sentences long."""
    return " ".join(_FILLER[(n + i) % len(_FILLER)] for i in range(sentences))


async def seed_turns(
    ctx: ScenarioContext, rows: list[tuple[str, str]], **assistant_kw: Any
) -> list[m.ChatMessage]:
    """`rows` (role, content) appended as one path below the chat's leaf; each
    assistant row answers the user row before it. The last row becomes the
    leaf. `assistant_kw` (e.g. `restricted=True`) applies to assistant rows
    whose content contains `{restricted}`, which is removed from the text."""
    from oc8.agent.engine import open_run_task

    fixture = chat(ctx)
    made: list[m.ChatMessage] = []
    async with tenant_session(ctx.tenant_id) as db:
        session = await db.get(m.ChatSession, fixture.session_id)
        agent = await db.get(m.Agent, fixture.agent_id)
        assert session is not None and agent is not None
        if session.task_id is None:
            task = await open_run_task(db, agent=agent, task_text="chat", tenant_id=ctx.tenant_id)
            session.task_id = task.id
        parent_id = session.current_leaf_id
        last_user: m.ChatMessage | None = None
        for role, content in rows:
            kw: dict[str, Any] = {}
            if role == "assistant" and "{restricted}" in content:
                kw = dict(assistant_kw)
                content = content.replace("{restricted}", "")
            message = m.ChatMessage(
                tenant_id=ctx.tenant_id,
                session_id=session.id,
                role=role,
                content=content,
                parent_id=parent_id,
                author_member_id=fixture.member_id if role == "user" else None,
                reply_to_id=last_user.id if role == "assistant" and last_user else None,
                **kw,
            )
            db.add(message)
            await db.flush()
            made.append(message)
            parent_id = message.id
            if role == "user":
                last_user = message
        session.current_leaf_id = parent_id
        await db.flush()
    return made


async def reply_text(ctx: ScenarioContext) -> str:
    """The answer the run stored in the chat (what the person reads), or ""."""
    if ctx.run_id is None:
        return ""
    async with tenant_session(ctx.tenant_id) as db:
        row = (
            await db.execute(
                select(m.ChatMessage.content)
                .where(
                    m.ChatMessage.tenant_id == ctx.tenant_id,
                    m.ChatMessage.run_id == ctx.run_id,
                    m.ChatMessage.role == "assistant",
                )
                .order_by(m.ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return row or ""


def called_tools(ctx: ScenarioContext) -> list[str]:
    return [str(c.get("tool")) for c in ctx.run_context.get("toolCalls", [])]


def matches(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def reply_checks(
    text: str, *, expect: list[str], forbid: list[str]
) -> tuple[list[Check], list[Check]]:
    """One check per pattern on the stored answer; the detail quotes its start."""
    head = text[:160].replace("\n", " ")
    return (
        [Check(f"answer matches {p!r}", matches(text, p), head) for p in expect],
        [Check(f"answer does not match {p!r}", not matches(text, p), head) for p in forbid],
    )


def answered(ctx: ScenarioContext, text: str) -> Check:
    return Check(
        "the run finished with a stored answer",
        ctx.final_state == "done" and bool(text),
        f"state={ctx.final_state} chars={len(text)}",
    )


async def checkpoints_of(ctx: ScenarioContext) -> list[m.ChatCheckpoint]:
    async with tenant_session(ctx.tenant_id) as db:
        return list(
            (
                await db.execute(
                    select(m.ChatCheckpoint)
                    .where(
                        m.ChatCheckpoint.tenant_id == ctx.tenant_id,
                        m.ChatCheckpoint.session_id == chat(ctx).session_id,
                    )
                    .order_by(m.ChatCheckpoint.created_at.asc())
                )
            )
            .scalars()
            .all()
        )


async def model_locality(ctx: ScenarioContext) -> str | None:
    """The locality of the model the chat runs on (its clone), else None."""
    clone = chat(ctx).model_config_id
    if clone is None:
        return None
    async with tenant_session(ctx.tenant_id) as db:
        config = await db.get(m.ModelConfig, clone)
    return config.locality if config is not None else None
