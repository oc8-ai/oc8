"""oc8's own objects as chat context: a compact summary and a deep link
(oc8 Chat spec §5.1). None of them is readable page by page.

Each check is the rule oc8 already applies to that object, held with the
VIEWING member's authority:

* agent, department -- the doors of `GET /agents/{id}` and
  `GET /departments/{id}` (`require_departmental(<kind>:view)`), then the
  scoped repository (`visible_agent` / `visible_department`, with
  `tenant_wide_read`). The rows are loaded ONLY through that repository, so
  `load` keeps the parsed id and `authorize` does the scoped load.
* run -- the Live tab's rule (`api/v1/chat_live.py`, `chat/live.py`,
  `chat/watch.py`, Rulings 15-18): the member must reach the run's agent
  (`reachable_chat_agent`); her own runs and the runs of her own chats show
  in full; the agent's autonomous runs in full only within run reach
  (`run:view`, `reaches_agent_runs`); a colleague's run only to whoever
  reaches the agent's runs and may manage the agent ("All runs"), and then
  REDACTED -- labels and state, never the task, tool arguments or result.
* approval -- the approvals view (`GET /approvals`): `approval:view` held
  tenant-wide or in a seat, then `approvals.repo.load_for_actor`, the only
  legal load of an approval for a person.
* responsibility -- `/copilot/responsibilities`: `copilot:use` held
  tenant-wide, and only the member's own (`copilot.responsibilities.get_owned`'s
  term).
* chat_session -- the chat routes' door, then OWNER ONLY
  (`api/v1/chat.py::_session_owned_by_caller`, Rulings 8/9): another member's
  chat is never pinnable, not even by an org admin.

A summary is shown to a model as untrusted data, so it is plain text of at
most `SUMMARY_CHARS`, credential-shaped text is redacted, and nothing from a
run's context but its result, step count and tool names is ever read: not its
task, which the Live tab does not show either, and never `pending_questions`,
`originating_operator` or any other key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.agents.repo import visible_agent, visible_agents
from oc8.approvals.repo import load_for_actor
from oc8.authz.authority import tenant_wide_read
from oc8.authz.permissions import AGENT, COPILOT_USE, DEPARTMENT, VIEW, perm
from oc8.authz.scope import APPROVAL_VIEW, AgentActor
from oc8.chat.context.base import ContextPage, Viewer
from oc8.chat.context.registry import register
from oc8.chat.context.sources import DEFAULT_CLASSIFICATION
from oc8.chat.reach import reachable_chat_agent, reaches_agent_runs
from oc8.copilot.redaction import redact_text
from oc8.departments.repo import visible_department, visible_departments
from oc8.runtime.person import whose_run
from oc8.runtime.run_inputs import may_manage_agent

#: The longest summary a resolver hands back.
SUMMARY_CHARS = 500
#: How many of a chat's newest messages its summary quotes, at most.
_CHAT_MESSAGES = 6
_CHAT_MESSAGE_CHARS = 160
#: How many distinct tool names a run summary lists.
_RUN_TOOLS = 8


def _compact(text: str, limit: int = SUMMARY_CHARS) -> str:
    """One line, credentials redacted, at most `limit` characters."""
    flat = redact_text(" ".join(str(text).split()))
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _holds(viewer: Viewer, permission: str) -> bool:
    """`require_departmental(permission)`'s admission test: held tenant-wide or
    in at least one seat."""
    return permission in viewer.authority.tenant_wide or viewer.scope.holds_anywhere(permission)


def _chat_door(viewer: Viewer) -> bool:
    """The chat and Live routes' door:
    `require_departmental(agent:view, or_tenant_wide=copilot:use)`."""
    return _holds(viewer, perm(AGENT, VIEW)) or COPILOT_USE in viewer.authority.tenant_wide


@dataclass
class Scoped[T]:
    """A pinned id, and -- once `authorize` has loaded it through the scoped
    repository for THIS viewer -- its row. A fresh instance per resolution."""

    id: uuid.UUID
    row: T | None = None

    def loaded(self) -> T:
        if self.row is None:
            raise LookupError("not authorized yet")
        return self.row


class _Summarized:
    readable = False

    async def classification(self, db: AsyncSession, obj: Any) -> str:
        return DEFAULT_CLASSIFICATION

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None:
        return None


# ----------------------------------------------------------------------- agent


class AgentResolver(_Summarized):
    kind = "agent"

    def admits(self, viewer: Viewer) -> bool:
        return _holds(viewer, perm(AGENT, VIEW))

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return Scoped[m.Agent](id=uuid.UUID(ref_id))

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        row = await visible_agent(
            db,
            scope=viewer.scope,
            tenant_wide=tenant_wide_read(viewer.authority, perm(AGENT, VIEW)),
            agent_id=obj.id,
        )
        if row is None or row.tenant_id != viewer.member.tenant_id:
            return False
        obj.row = row
        return True

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.loaded().name)

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        agent = obj.loaded()
        return _compact(
            f"Agent {agent.name} ({agent.role_title or 'no role title'}), status "
            f"{agent.status}. Mission: {agent.mission or 'none'}"
        )

    def deep_link(self, obj: Any) -> str | None:
        return f"/agents/{obj.id}"

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        if not self.admits(viewer):
            return []
        rows, _total = await visible_agents(
            db,
            scope=viewer.scope,
            tenant_wide=tenant_wide_read(viewer.authority, perm(AGENT, VIEW)),
            search=query,
            limit=limit,
        )
        return [Scoped(id=row.id, row=row) for row in rows]


# ------------------------------------------------------------------ department


class DepartmentResolver(_Summarized):
    kind = "department"

    def admits(self, viewer: Viewer) -> bool:
        return _holds(viewer, perm(DEPARTMENT, VIEW))

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return Scoped[m.Department](id=uuid.UUID(ref_id))

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        row = await visible_department(
            db,
            scope=viewer.scope,
            tenant_wide=tenant_wide_read(viewer.authority, perm(DEPARTMENT, VIEW)),
            department_id=obj.id,
        )
        if row is None or row.tenant_id != viewer.member.tenant_id:
            return False
        obj.row = row
        return True

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.loaded().name)

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        dept = obj.loaded()
        return _compact(f"Department {dept.name}. Goal: {dept.goal or 'none'}")

    def deep_link(self, obj: Any) -> str | None:
        return f"/departments/{obj.id}"

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        if not self.admits(viewer):
            return []
        rows, _total = await visible_departments(
            db,
            scope=viewer.scope,
            tenant_wide=tenant_wide_read(viewer.authority, perm(DEPARTMENT, VIEW)),
            search=query,
            limit=limit,
        )
        return [Scoped(id=row.id, row=row) for row in rows]


# ------------------------------------------------------------------------- run


@dataclass
class RunView:
    """A run as one viewer may see it. Both flags fail closed and only
    `authorize` sets them: `authorized` once the viewer may see the run at
    all (until then nothing of it is shown, like `Scoped.loaded()`), and
    `redacted` is cleared for a run that is the viewer's own, the agent's
    autonomous run, or a run of the viewer's own chat."""

    run: m.AgentRun
    redacted: bool = True
    authorized: bool = False

    def shown(self) -> m.AgentRun:
        if not self.authorized:
            raise LookupError("not authorized yet")
        return self.run


class RunResolver(_Summarized):
    kind = "run"

    def admits(self, viewer: Viewer) -> bool:
        return _chat_door(viewer)

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        run = (
            await db.execute(
                select(m.AgentRun).where(
                    m.AgentRun.id == uuid.UUID(ref_id), m.AgentRun.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        return RunView(run=run) if run is not None else None

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        run: m.AgentRun = obj.run
        agent = await reachable_chat_agent(
            db, authority=viewer.authority, scope=viewer.scope, agent_id=run.agent_id
        )
        if agent is None:
            return False
        whose = await whose_run(db, run)
        if whose.is_own(viewer.member.subject) or await self._of_own_chat(db, viewer, run):
            obj.redacted = False
            obj.authorized = True
            return True
        # Not her conversation: a run read, so run reach first (`run:view`,
        # the run doors' rule), as on the Live timeline.
        if not await reaches_agent_runs(
            db, authority=viewer.authority, scope=viewer.scope, agent=agent
        ):
            return False
        if whose.autonomous:
            obj.redacted = False
            obj.authorized = True
            return True
        # A colleague's run: under "All runs" only, which needs the right to
        # manage the agent -- and then labels and state only (Ruling 15).
        obj.redacted = True
        obj.authorized = await may_manage_agent(
            db, agent=agent, authority=viewer.authority, scope=viewer.scope
        )
        return bool(obj.authorized)

    @staticmethod
    async def _of_own_chat(db: AsyncSession, viewer: Viewer, run: m.AgentRun) -> bool:
        """A run of one of the viewer's own chats with this agent -- the
        session-audience branch of `chat/watch.py::may_watch_run`."""
        raw = (run.context or {}).get("chat_session_id")
        if not raw:
            return False
        try:
            session_id = uuid.UUID(str(raw))
        except ValueError:
            return False
        owned = await db.execute(
            select(m.ChatSession.id).where(
                m.ChatSession.id == session_id,
                m.ChatSession.tenant_id == viewer.member.tenant_id,
                m.ChatSession.member_id == viewer.member.id,
                m.ChatSession.agent_id == run.agent_id,
            )
        )
        return owned.scalar_one_or_none() is not None

    def ref_id(self, obj: Any) -> str:
        return str(obj.run.id)

    def label(self, obj: Any) -> str:
        run = obj.shown()
        return f"Run {str(run.id)[:8]} ({run.source})"

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        run: m.AgentRun = obj.shown()
        ctx = run.context or {}
        steps = ctx.get("steps") if isinstance(ctx.get("steps"), int) else 0
        head = f"run ({run.source}), state {run.state}, {steps} steps."
        if obj.redacted:
            tools: list[str] = []
            for call in ctx.get("toolCalls") or []:
                name = call.get("tool") if isinstance(call, dict) else None
                if isinstance(name, str) and name and name not in tools:
                    tools.append(name)
            listed = f" Tools used: {', '.join(tools[:_RUN_TOOLS])}." if tools else ""
            return _compact(f"A colleague's {head}{listed} Task and result are not shown.")
        # Never the task: no more than the Live tab's run item shows.
        output = ctx.get("output")
        result = _compact(str(output), 400) if output else "none yet"
        return _compact(f"Agent {head} Result: {result}")

    def deep_link(self, obj: Any) -> str | None:
        return f"/activity?run={obj.run.id}"

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        # A run is pinned from a link to it, not found by a name search.
        return []


# -------------------------------------------------------------------- approval


class ApprovalResolver(_Summarized):
    kind = "approval"

    def admits(self, viewer: Viewer) -> bool:
        return _holds(viewer, APPROVAL_VIEW)

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return Scoped[m.ApprovalRequest](id=uuid.UUID(ref_id))

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        actor = AgentActor(member=viewer.member, scope=viewer.scope, via="chat_context")
        row = await load_for_actor(db, obj.id, actor=actor)
        if row is None or row.tenant_id != viewer.member.tenant_id:
            return False
        obj.row = row
        return True

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.loaded().title or "Approval")

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        row = obj.loaded()
        amount = f", amount {row.amount_text}" if row.amount_text else ""
        return _compact(
            f"Approval '{row.title or 'untitled'}' ({row.action_type}), status "
            f"{row.status}{amount}. {row.detail or ''}"
        )

    def deep_link(self, obj: Any) -> str | None:
        return "/workspace"

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        # An approval is pinned from the queue, not found by a name search:
        # `visible_approvals` lists by status, it does not search.
        return []


# -------------------------------------------------------------- responsibility


class ResponsibilityResolver(_Summarized):
    kind = "responsibility"

    def admits(self, viewer: Viewer) -> bool:
        return COPILOT_USE in viewer.authority.tenant_wide

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return (
            await db.execute(
                select(m.Responsibility).where(
                    m.Responsibility.id == uuid.UUID(ref_id),
                    m.Responsibility.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        return bool(obj.member_id == viewer.member.id)

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.title)

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        return _compact(
            f"Responsibility '{obj.title}', state {obj.state}. Goal: {obj.goal}. "
            f"Next step: {obj.next_step or 'none'}"
        )

    def deep_link(self, obj: Any) -> str | None:
        return "/copilot"

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        if not self.admits(viewer):
            return []
        rows = await db.execute(
            select(m.Responsibility)
            .where(
                m.Responsibility.tenant_id == viewer.member.tenant_id,
                m.Responsibility.member_id == viewer.member.id,
                m.Responsibility.title.icontains(query, autoescape=True),
            )
            .order_by(m.Responsibility.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars())


# ---------------------------------------------------------------- chat session


class ChatSessionResolver(_Summarized):
    kind = "chat_session"

    def admits(self, viewer: Viewer) -> bool:
        return _chat_door(viewer)

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None:
        return (
            await db.execute(
                select(m.ChatSession).where(
                    m.ChatSession.id == uuid.UUID(ref_id), m.ChatSession.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool:
        # Owner only, with no oversight carve-out (Rulings 8/9).
        return bool(obj.member_id == viewer.member.id)

    def ref_id(self, obj: Any) -> str:
        return str(obj.id)

    def label(self, obj: Any) -> str:
        return str(obj.title or "Chat")

    async def summary(self, db: AsyncSession, obj: Any) -> str:
        newest_first = (
            await db.execute(
                select(m.ChatMessage.role, m.ChatMessage.content)
                .where(
                    m.ChatMessage.tenant_id == obj.tenant_id,
                    m.ChatMessage.session_id == obj.id,
                )
                .order_by(m.ChatMessage.created_at.desc(), m.ChatMessage.id.desc())
                .limit(_CHAT_MESSAGES)
            )
        ).all()
        text = f"Chat '{_compact(obj.title or 'Chat', 80)}':"
        lines: list[str] = []
        budget = SUMMARY_CHARS - len(text)
        for role, content in newest_first:
            line = f" {role}: {_compact(content, _CHAT_MESSAGE_CHARS)}"
            if len(line) > budget:
                break
            lines.insert(0, line)
            budget -= len(line)
        return text + "".join(lines)

    def deep_link(self, obj: Any) -> str | None:
        return f"/copilot?session={obj.id}"

    async def search(self, db: AsyncSession, viewer: Viewer, query: str, limit: int) -> list[Any]:
        if not self.admits(viewer):
            return []
        rows = await db.execute(
            select(m.ChatSession)
            .where(
                m.ChatSession.tenant_id == viewer.member.tenant_id,
                m.ChatSession.member_id == viewer.member.id,
                m.ChatSession.title.icontains(query, autoescape=True),
            )
            .order_by(m.ChatSession.last_message_at.desc().nulls_last())
            .limit(limit)
        )
        return list(rows.scalars())


for _resolver in (
    AgentResolver(),
    DepartmentResolver(),
    RunResolver(),
    ApprovalResolver(),
    ResponsibilityResolver(),
    ChatSessionResolver(),
):
    register(_resolver)
