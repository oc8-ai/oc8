"""What a rating of a chat answer is counted for."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.chat.model_choice import session_model_config


async def answer_model(
    db: AsyncSession, *, session: m.ChatSession, answer: m.ChatMessage, agent: m.Agent | None
) -> str | None:
    """The model that wrote `answer`, for counting its rating.

    A usage record names no run, so the answering run's completions are its
    agent's usage records for the chat's member between the run's creation
    and the answer's: the routed model of an Auto answer, the model the
    answer ran on even after the chat switched. Of several (a fallback, a
    title or a checkpoint summary written in that window) the one with the
    most output tokens wins -- the answer's own completions dominate a
    title's few words; ties go to the latest. Without a run or a usage
    record (an answer written before metering, or no model call at all) the
    chat's current model stands in."""
    run = await db.get(m.AgentRun, answer.run_id) if answer.run_id is not None else None
    if run is not None and run.tenant_id == session.tenant_id:
        usage = m.TokenUsageRecord
        model = await db.scalar(
            select(usage.model)
            .where(
                usage.tenant_id == session.tenant_id,
                usage.agent_id == run.agent_id,
                or_(usage.member_id == session.member_id, usage.member_id.is_(None)),
                usage.ts >= run.created_at,
                usage.ts <= answer.created_at,
            )
            .group_by(usage.model)
            .order_by(func.sum(usage.tokens_out).desc(), func.max(usage.ts).desc())
            .limit(1)
        )
        if model is not None:
            return model
    if agent is None:
        return None
    _config, _provider, model = await session_model_config(db, session=session, agent=agent)
    return model
