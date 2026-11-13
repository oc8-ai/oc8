"""Hire-approval gate service (A4 §5.5): the tenant flag, the hire incident, and
its resolution. add/flush only -- the endpoints own the commit."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.approvals import raise_approval


async def require_hire_approval(db: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Whether new agents in this tenant need hire approval. Defaults to False
    (a missing org row or an unset flag) -- today's behavior."""
    org = await db.get(m.Organization, tenant_id)
    if org is None:
        return False
    return bool(org.settings.get("require_hire_approval", False))


async def set_require_hire_approval(
    db: AsyncSession, *, tenant_id: uuid.UUID, enabled: bool
) -> None:
    """Flip the tenant flag. Reassigns settings (never in-place) so SQLAlchemy
    tracks the JSONB change. No-op if the org row is missing."""
    org = await db.get(m.Organization, tenant_id)
    if org is None:
        return
    org.settings = {**org.settings, "require_hire_approval": enabled}
    await db.flush()


async def create_hire_request(db: AsyncSession, *, agent: m.Agent) -> m.ApprovalRequest:
    """Raise the pending hire_agent incident for a just-created pending agent,
    with a config snapshot the approver can read in the inbox."""
    req = await raise_approval(
        db,
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        action_type="hire_agent",
        payload={
            "name": agent.name,
            "role_title": agent.role_title,
            "department_id": str(agent.department_id),
            "mission": agent.mission,
            "is_team_lead": agent.is_team_lead,
            "model_config_id": (str(agent.model_config_id) if agent.model_config_id else None),
        },
        title=f"Hire agent: {agent.name}",
        detail=(
            f'A new agent "{agent.name}" is awaiting hire approval before it can '
            "operate. Approve to activate it, or reject to discard it."
        ),
    )
    return req


async def resolve_hire_agent(
    db: AsyncSession, *, approval_request: m.ApprovalRequest, decision: str
) -> None:
    """approve => activate the agent (pending_approval -> stopped, the normal
    post-creation state the operator then starts). reject => soft-delete it. The
    agent may be gone already (a concurrent delete) -- then it is a no-op."""
    agent = await db.get(m.Agent, approval_request.agent_id)
    if agent is None:
        return
    if decision == "approve":
        if agent.status == "pending_approval":
            agent.status = "stopped"
    else:  # reject
        agent.status = "stopped"
        agent.deleted_at = dt.datetime.now(tz=dt.UTC)
        # Archived like any deleted agent, so retired like one: a trigger made
        # while it waited for approval does not stay on.
        from oc8.agents.retire import retire_agent

        await retire_agent(db, agent_id=agent.id, hard_delete=False, principal=None)
    await db.flush()
