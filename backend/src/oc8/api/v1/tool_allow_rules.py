"""A person's own "always allow" rules (`approvals.allow_rules`): list and
revoke them.

Self-service, like `api/v1/api_keys.py`: `member_id` is always the caller's
own (`member_id_for_principal`), never a path parameter, so no other member's
rule is reachable here. Somebody else's rule is the same 404 as one that does
not exist. Creating a rule is part of a decision (`POST .../decision` with
`alwaysAllow`), never a route of its own.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, status

from oc8.api.deps import CurrentPrincipal, DbSession, unguarded
from oc8.approvals.allow_rules import member_rules, revoke_rule
from oc8.auth.totp_gate import member_id_for_principal
from oc8.schemas.dto import ToolAllowRuleDTO

router = APIRouter()

_UNGUARDED_REASON: Final = "self-service: acts only on the caller's own allow rules"


@router.get(
    "/tool-allow-rules",
    response_model=list[ToolAllowRuleDTO],
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def list_own_tool_allow_rules(
    principal: CurrentPrincipal, db: DbSession
) -> list[ToolAllowRuleDTO]:
    member_id = await member_id_for_principal(db, principal)
    if member_id is None:
        return []
    return [
        ToolAllowRuleDTO(
            id=str(view.rule.id),
            agent_id=str(view.rule.agent_id),
            agent_name=view.agent_name,
            connection_name=view.connection_name,
            tool_key=view.rule.tool_key,
            created_at=view.rule.created_at.isoformat(),
        )
        for view in await member_rules(db, member_id=member_id)
    ]


@router.delete(
    "/tool-allow-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(unguarded(_UNGUARDED_REASON))],
)
async def revoke_own_tool_allow_rule(
    rule_id: uuid.UUID, principal: CurrentPrincipal, db: DbSession
) -> None:
    member_id = await member_id_for_principal(db, principal)
    if member_id is None or not await revoke_rule(db, rule_id=rule_id, member_id=member_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "rule not found")
    await db.commit()
