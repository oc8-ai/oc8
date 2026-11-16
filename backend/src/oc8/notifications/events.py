"""Who is told what when a board is created, a run fails, an approval is decided.

Each hook runs inside its business funnel's own transaction (`notify` writes
the rows there and delivers only after commit), and inside a SAVEPOINT of its
own: a notification that cannot be addressed or written is logged and dropped,
never the reason a board apply, a run's ending or a decision fails.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.connections.acting import resolve_acting_person
from oc8.notifications.service import Localized, notify

logger = logging.getLogger(__name__)

FailureCategory = Literal["connection_required", "step_limit", "model_error", "error"]

#: The only text a failure notification carries about WHY. Never the run's own
#: error, which may quote a customer's data or a provider's response body.
CATEGORY_TEXT: dict[FailureCategory, Localized] = {
    "connection_required": (
        "A connection has to be set up first.",
        "Zuerst muss eine Verbindung eingerichtet werden.",
    ),
    "step_limit": (
        "It reached its step limit.",
        "Das Schrittlimit wurde erreicht.",
    ),
    "model_error": (
        "The language model could not answer.",
        "Das Sprachmodell konnte nicht antworten.",
    ),
    "error": (
        "The run ended with an error.",
        "Der Lauf endete mit einem Fehler.",
    ),
}

# How each place that fails a run leaves its reason on `run.context` (see
# `runtime/executor.py` and the closers beside it), and the category it gets:
#
#   executor ~846   agent row gone        error "agent not found"            -> error
#   executor ~894   hire gate             error "agent awaiting hire …"      -> error
#   executor ~1025  _resolve_mcp_conn.    error "tool '…' needs a login …",
#                                         "… pinned login …", "tool '…' is
#                                         pinned to …", "unknown MCP
#                                         connection: …"                     -> connection_required
#   runtime_env     (any site)            connection_required {…}            -> connection_required
#   executor ~1272  the runtime raised    error repr(exc):
#                                           "TimeoutError(" (the docker
#                                           driver's wait past the step
#                                           budget)                          -> step_limit
#                                           "ProviderBudgetExceeded(",
#                                           "HTTPStatusError(", "AutoRouter
#                                           Error(", "TenantKeyRequired(" …  -> model_error
#                                           any other exception              -> error
#   executor ~1431  repeated question     error str(exc)                     -> error
#   executor ~1464  unknown status        error "unknown run status: …"      -> error
#   executor ~1464  runtime said failed   output only, no error:
#                                           auto-router, answer budget,
#                                           quota, rate-limit sentences      -> model_error
#                                           "Reached step limit."            -> step_limit
#                                           anything else                    -> error
#   executor ~1712  lease lost            error "lease lost: …"              -> error
#   reconcile       heartbeat lapsed      error REASON                       -> error
#
# `run.phase` only ever holds the workspace start phases (provisioning,
# starting); a run failing in one of them failed to START, which says nothing
# about a step budget or a model, so phase maps to `error` like the rest.
_CONNECTION_ERROR = re.compile(
    r"^(connection_required\b|unknown MCP connection:"
    r"|tool .+? (needs a login|has an unreadable pinned login|is pinned to ))"
)
_STEP_LIMIT_ERRORS = ("TimeoutError(",)
_MODEL_ERRORS = (
    "ProviderBudgetExceeded(",
    "HTTPStatusError(",
    "AutoRouterError(",
    "TenantKeyRequired(",
    "SubscriptionModelNotManualOnly(",
    "EmbeddingUnavailable(",
)
_STEP_LIMIT_OUTPUTS = ("Reached step limit.",)
_MODEL_OUTPUTS = (
    "Auto model router misconfigured:",
    "The model used its whole answer budget",
    "The monthly token quota for",
    "The model provider is rate-limiting",
)


def failure_category(run: m.AgentRun) -> FailureCategory:
    """A fixed reason for a failed run. Reads the raw error; returns none of it."""
    ctx = run.context or {}
    if ctx.get("connection_required"):
        return "connection_required"
    error = ctx.get("error")
    if isinstance(error, str) and error:
        if _CONNECTION_ERROR.match(error):
            return "connection_required"
        if error.startswith(_STEP_LIMIT_ERRORS):
            return "step_limit"
        if error.startswith(_MODEL_ERRORS):
            return "model_error"
        return "error"
    output = ctx.get("output")
    if isinstance(output, str):
        if output.startswith(_STEP_LIMIT_OUTPUTS):
            return "step_limit"
        if output.startswith(_MODEL_OUTPUTS):
            return "model_error"
    return "error"


async def _guarded(db: AsyncSession, what: str, hook: Callable[[], Awaitable[None]]) -> None:
    """Run `hook` in a SAVEPOINT; on failure roll back only that and log it.
    Rolling back the savepoint also drops whatever `notify` staged in it.

    The caller's pending rows are flushed FIRST and outside the guard:
    `begin_nested()` would flush them itself, and a business write that fails
    there (the run's transition row, the board's audit row) must raise from
    the caller -- not be logged as a notice and leave a dead transaction."""
    await db.flush()
    try:
        async with db.begin_nested():
            await hook()
    except Exception:
        logger.warning("notifications: could not notify about %s", what, exc_info=True)


async def _seat_holders(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    department_id: uuid.UUID,
    agent_manage: bool = False,
) -> list[uuid.UUID]:
    """Live members holding a live seat in this live department -- and with
    `agent_manage` on it, when asked. `all_departments` alone is not a seat."""
    seat = m.OrgMemberDepartment
    stmt = (
        select(m.OrgMember.id)
        .join(seat, seat.member_id == m.OrgMember.id)
        .join(m.Department, m.Department.id == seat.department_id)
        .where(
            m.OrgMember.tenant_id == tenant_id,
            m.OrgMember.deleted_at.is_(None),
            seat.tenant_id == tenant_id,
            seat.department_id == department_id,
            seat.revoked_at.is_(None),
            m.Department.deleted_at.is_(None),
        )
        .order_by(m.OrgMember.id)
    )
    if agent_manage:
        stmt = stmt.where(seat.agent_manage.is_(True))
    return list(dict.fromkeys((await db.execute(stmt)).scalars()))


async def on_board_created(
    db: AsyncSession, *, board: m.DataBoard, actor_member_id: uuid.UUID | None
) -> None:
    """Tell the board's department's seat holders, not whoever created it."""

    async def _hook() -> None:
        agent = await db.get(m.Agent, board.agent_id) if board.agent_id else None
        body = f"{board.title} · {agent.name}" if agent is not None else board.title
        members = await _seat_holders(
            db, tenant_id=board.tenant_id, department_id=board.department_id
        )
        await notify(
            db,
            tenant_id=board.tenant_id,
            member_ids=[mid for mid in members if mid != actor_member_id],
            kind="board.available",
            subject_id=board.id,
            # Same title as a share (`boards/sharing.py`): for the person it
            # is one event -- a dashboard they can now open.
            title=("New dashboard", "Neues Dashboard"),
            body=(body, body),
            link="/workspace",
            classification="internal",
        )

    await _guarded(db, f"board {board.id}", _hook)


async def on_run_failed(db: AsyncSession, run: m.AgentRun) -> None:
    """Tell the person behind the run; with nobody behind it, the department's
    agent managers. The Copilot's own runs tell nobody."""

    async def _hook() -> None:
        agent = await db.get(m.Agent, run.agent_id)
        if agent is None or agent.is_tenant_assistant:
            return
        person = (await resolve_acting_person(db, run)).member_id
        members = (
            [person]
            if person is not None
            else await _seat_holders(
                db, tenant_id=run.tenant_id, department_id=agent.department_id, agent_manage=True
            )
        )
        reason = CATEGORY_TEXT[failure_category(run)]
        await notify(
            db,
            tenant_id=run.tenant_id,
            member_ids=members,
            kind="run.failed",
            # Per agent, so repeated failures collapse into the one unread row.
            subject_id=agent.id,
            title=(
                f"Agent '{agent.name}' failed",
                f"Agent ‚{agent.name}‘ fehlgeschlagen",  # noqa: RUF001
            ),
            body=reason,
            link=f"/agents/{agent.id}?tab=live",
            classification="internal",
        )

    await _guarded(db, f"run {run.id}", _hook)


async def on_approval_decided(db: AsyncSession, approval: m.ApprovalRequest) -> None:
    """Tell the person behind the run that raised it, unless they decided it."""

    async def _hook() -> None:
        from oc8.channels.dispatch import _classification_of

        if approval.task_id is None:
            return
        run = (
            await db.execute(
                select(m.AgentRun)
                .where(m.AgentRun.task_id == approval.task_id)
                .order_by(desc(m.AgentRun.created_at), desc(m.AgentRun.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            return
        person = (await resolve_acting_person(db, run)).member_id
        if person is None or person == approval.decided_by:
            return
        decider = await db.get(m.OrgMember, approval.decided_by) if approval.decided_by else None
        name = decider.display_name if decider is not None else ""
        body: Localized = (name, name) if name else ("Someone", "Jemand")
        if approval.status == "approved":
            title: Localized = (f"Approved: {approval.title}", f"Genehmigt: {approval.title}")
        else:
            title = (f"Rejected: {approval.title}", f"Abgelehnt: {approval.title}")
        session_id = (run.context or {}).get("chat_session_id")
        link = f"/chat/{session_id}" if session_id else f"/agents/{run.agent_id}?tab=live"
        await notify(
            db,
            tenant_id=approval.tenant_id,
            member_ids=[person],
            kind="approval.decided",
            subject_id=approval.id,
            title=title,
            body=body,
            link=link,
            classification=_classification_of(approval),
        )

    await _guarded(db, f"approval {approval.id}", _hook)
