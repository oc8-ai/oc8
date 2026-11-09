"""Turn a human's decision into work the agent performs.

The agent that raised the decision did not wait for it -- see
`control_tools.REQUEST_DECISION` -- so nothing is sitting there to hand it to.
Without this the inbox would be a dead end: the operator clicks, the row says
"approved", and the customer waits forever.

The decision therefore becomes a fresh run through the ordinary intake path, the
same one a cron trigger or an operator uses. That has a consequence worth
stating: the new run starts a NEW conversation and remembers nothing, so the
instruction has to carry its own context. Hence the original question and basis
travel with it, not just "approved".
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.connections.acting import resolve_acting_person
from oc8.runtime.intake import enqueue_run
from oc8.tenants.activity import TenantInactive

logger = logging.getLogger(__name__)


class UnknownOption(ValueError):
    """The chosen option is not one the agent offered."""


def option_labels(approval: m.ApprovalRequest) -> dict[str, dict[str, Any]]:
    raw = (approval.payload or {}).get("options") or []
    return {str(o["key"]): o for o in raw if isinstance(o, dict) and o.get("key")}


def instruction_for(approval: m.ApprovalRequest, *, decision: str, reason: str) -> str:
    """What the agent is told to do now.

    Written as an instruction, not as a record: the agent reads this cold, with
    no memory of having asked.
    """
    lines = [
        "Ein Mensch hat eine Entscheidung getroffen, um die du gebeten hattest. "
        "Setze sie jetzt um.",
        "",
        f"DEINE FRAGE WAR: {approval.title}",
    ]
    if approval.detail:
        lines += ["", f"WORUM ES GING: {approval.detail}"]

    chosen = option_labels(approval).get(approval.decision_option or "")
    if decision == "reject":
        lines += ["", "ENTSCHEIDUNG: ABGELEHNT — führe die vorgeschlagene Aktion NICHT aus."]
    elif chosen is not None:
        detail = f" ({chosen['detail']})" if chosen.get("detail") else ""
        lines += ["", f"ENTSCHEIDUNG: {chosen['label']}{detail}"]
    else:
        lines += ["", "ENTSCHEIDUNG: FREIGEGEBEN."]
    if reason:
        # Last, so it overrides a chosen option it contradicts: a human who
        # writes an instruction means it more than a button they also pressed.
        lines += ["", f"VORGABE DES MENSCHEN (geht allem anderen vor): {reason}"]

    lines += [
        "",
        "Führe genau das aus und informiere danach den Kunden im Chatter. "
        "Wiederhole keine Schritte, die du damals schon erledigt hattest.",
    ]
    return "\n".join(lines)


async def _carried_from_asking_run(
    db: AsyncSession, approval: m.ApprovalRequest, reason: str
) -> dict[str, Any]:
    """The person the run that asked acted for, and the trigger that started
    it -- what a delegation carries too (connections design). Nothing when
    that run is unknown or gone: the follow-up then acts for nobody.

    The approver's free-text reason becomes the follow-up's overriding
    instruction, so it acts for that person only when they wrote it themselves
    or nobody wrote anything: someone else's words never run under their
    account."""
    raw = (approval.payload or {}).get("run_id")
    try:
        asking = await db.get(m.AgentRun, uuid.UUID(str(raw))) if raw else None
    except ValueError:
        asking = None
    if asking is None or asking.tenant_id != approval.tenant_id:
        return {}
    carried: dict[str, Any] = {}
    acting = await resolve_acting_person(db, asking)
    if acting.member_id is not None and (
        not reason.strip() or approval.decided_by == acting.member_id
    ):
        carried["on_behalf_of"] = str(acting.member_id)
    ctx = asking.context if isinstance(asking.context, dict) else {}
    followup = ctx.get("followup")
    trigger_id = ctx.get("trigger_id") or (
        followup.get("trigger_id") if isinstance(followup, dict) else None
    )
    if trigger_id:
        carried["trigger_id"] = str(trigger_id)
    return carried


async def carry_out_decision(
    db: AsyncSession,
    *,
    approval: m.ApprovalRequest,
    decision: str,
    reason: str,
    option: str | None,
) -> uuid.UUID | None:
    """Record which option was picked and enqueue the run that acts on it.

    COMMITS `db` (via `enqueue_run`); see its docstring. Returns None, and
    commits nothing, when the organisation may not run: the decision is still
    recorded by the caller, but no run carries it out.
    """
    options = option_labels(approval)
    if option is not None:
        if option not in options:
            raise UnknownOption(option)
        approval.decision_option = option
    await db.flush()

    context: dict[str, Any] = {
        "task": instruction_for(approval, decision=decision, reason=reason),
        **await _carried_from_asking_run(db, approval, reason),
    }
    try:
        run, _published = await enqueue_run(
            db,
            tenant_id=approval.tenant_id,
            agent_id=approval.agent_id,
            context=context,
            source="decision",
            # One run per decision, forever: a double-click on approve must not
            # have the agent act twice on an external system.
            idempotency_key=f"decision:{approval.id}",
        )
    except TenantInactive:
        logger.info(
            "decision %s not carried out: organisation %s is not active",
            approval.id,
            approval.tenant_id,
        )
        return None
    return run.id
