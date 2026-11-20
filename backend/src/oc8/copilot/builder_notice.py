"""Tell the Copilot when the person's "Create this agent" was refused.

The card shows the refusal to the person; this module also hands it to the
Copilot, as one oc8 notice turn in the same chat (`chat.service.send_message`
with `notice=`), so its next answer explains in plain words what went wrong
and either proposes a corrected agent or says exactly what the person has to
do and where. Bounded: one notice per distinct refusal of a proposal -- the
same refusal clicked again does not start another turn, and a notice never
applies anything, so it cannot loop.

Only fixed codes, step labels and the agent's name or the board's title
(already on the card in this chat) go into the notice; no other submitted
text. A board's title is resolved by the caller (`builder.board_title`): this
module reads no board."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.copilot.builder import create_step
from oc8.copilot.builder_ops import BuilderStep, is_board_only
from oc8.copilot.models import CopilotProposal
from oc8.runtime.fencing import one_line

logger = logging.getLogger(__name__)

#: Refusals about the proposal's state, not its content: nothing to explain.
_STATE_REFUSALS: Final = frozenset(
    {"not_found", "already_applied", "not_open", "expired", "superseded_concurrently"}
)

#: What each step does, in the person's terms.
STEP_LABELS: Final[dict[str, str]] = {
    "department.create": "create the new department",
    "agent.create": "create the agent",
    "agent.mission.set": "set the mission",
    "agent.model.switch": "set the model",
    "agent.skill.assign": "assign a skill",
    "trigger.create": "create the schedule or event trigger",
    "agent.narrowing.set": "give the agent its systems",
    "agent.guardrail.set": "set a limit",
    "board.create": "create the dashboard",
    "board.update": "change the dashboard",
}

#: Why, for the Copilot: what broke and what fixes it. Keyed by the step's
#: `InvalidOperation.reason`, or by the refusal code when no step is to blame.
REASONS: Final[dict[str, str]] = {
    "skill_requirements_missing": (
        "the skill needs a system the agent does not have. Add that system to "
        "systems (builder_options lists the usable ones) or drop the skill; if "
        "no department offers it, the person connects it on /connections"
    ),
    "skill_needs_system": (
        "a chosen skill needs a system the agent would not have. Add that system "
        "to systems or drop the skill; if it is not usable yet, the person "
        "connects it on /connections"
    ),
    "runtime_lacks_skills": (
        "the agent's runtime cannot run skills. Drop the skills, or the person "
        "picks another runtime on the agent's page"
    ),
    "trigger_invalid": (
        "the schedule or event is not valid: a cron expression needs five fields "
        "(minute hour day month weekday, e.g. 0 8 * * 1-5) and an event must be "
        "one the installed capas declare. Correct it or leave the trigger out"
    ),
    "trigger_needs_login": (
        "the event listens on the agent's own organisation login for the "
        "event's system, and the agent has none fixed. Give the agent that "
        "system in systems with exactly one organisation login, or leave the "
        "trigger out; the person picks the login on the agent's page"
    ),
    "model_manual_only": (
        "the chosen model runs on a personal subscription and may only answer "
        "chats, never a schedule or an event. Pick another model or leave the "
        "trigger out"
    ),
    "system_outside_frame": (
        "a system is switched off or restricted for this department. Leave it "
        "out, pick another department, or an admin releases it on the "
        "department's page"
    ),
    "system_login_needed": (
        "a system has more than one organisation login or none the agent may "
        "use. Leave it out; the person chooses the login on the agent's page "
        "after creating it, or sets one up on /connections"
    ),
    "system_no_amount_limit": (
        "an amount limit was set on a system that cannot report amounts. Drop that limit"
    ),
    "connection_not_allowed": (
        "a system is no longer usable in this department. Call builder_options "
        "again and use only the systems it lists; a missing one is connected on "
        "/connections"
    ),
    "model_not_allowed": "the model is no longer available. Pick one from builder_options",
    "skill_not_allowed": "a skill is no longer available. Pick one from builder_options",
    "name_taken": "an agent with this name already exists. Pick another name",
    "department_unknown": "the department no longer exists. Pick one from builder_options",
    "department_exists": (
        "a department with the new department's name exists now. Use that "
        "department's id from builder_options instead"
    ),
    "secret_value": ("a text contains a credential. Remove it; logins are set up on /connections"),
    "board_spec_invalid": (
        "the dashboard's layout is not valid. Check its datasets, fields and blocks "
        "against the catalogue builder_options gives"
    ),
    "board_unknown": (
        "the dashboard no longer exists or is out of reach. Pick one from builder_options"
    ),
    "agent_unknown": (
        "the agent that fills the dashboard no longer exists or is out of reach. Pick "
        "one from builder_options"
    ),
    "agent_other_department": (
        "a dashboard can only be handed to an agent of its own department. Pick "
        "one of that department's agents"
    ),
    "board_version_unknown": (
        "that earlier version of the dashboard does not exist. Leave restoreVersion out"
    ),
    "board_changed": (
        "the dashboard changed since this proposal (another change was applied first). "
        "Read its current version in builder_options and propose the change again "
        "from it"
    ),
    "department_changed": (
        "the dashboard or its agent moved to another department after the request was "
        "sent. The approver rejects this request; propose it again so it goes to the "
        "right department"
    ),
    "artifact_not_available": (
        "artifact blocks need the Enterprise edition. Use the other block types"
    ),
    "dataset_removed": "a dashboard change may only add datasets. Keep every existing one",
    "field_removed": "a dashboard change may only add fields. Keep every existing one",
    "grain_changed": "a dataset's grain cannot change. Keep it, or add a new dataset",
    "field_type_changed": "a field's type cannot change. Keep it, or add a new field",
    "currency_changed": "a field's currency cannot change. Keep it, or add a new field",
}
_UNKNOWN: Final = (
    "oc8 refused it without saying why. Check the proposal against builder_options "
    "and propose it again, or tell the person to try again later"
)


def refusal_signature(result: dict[str, object]) -> str:
    return ":".join(str(result.get(key)) for key in ("refusal", "failedStep", "failedReason"))


def notice_text(
    proposal: CopilotProposal, steps: list[BuilderStep], *, board_title: str = ""
) -> str | None:
    """The notice for the proposal's stored refusal, or None when there is
    nothing the Copilot could explain (no refusal, or one about state).
    `board_title` names a board-only proposal's board."""
    from oc8.chat.service import NOTICE_PREFIX

    result = proposal.result or {}
    refusal = result.get("refusal")
    if not isinstance(refusal, str) or refusal in _STATE_REFUSALS:
        return None
    step = result.get("failedStep")
    reason = result.get("failedReason")
    code = reason if isinstance(reason, str) else refusal
    why = REASONS.get(code, _UNKNOWN)
    board = is_board_only(steps)
    # The board's title is person-written: one line, escaped and quoted, like
    # every label in the boards mission (`boards.mission._text`).
    name = json.dumps(one_line(board_title, 120)) if board else f"“{create_step(steps).name}”"
    tool, subject = ("propose_board", "dashboard") if board else ("propose_agent", "agent")
    card = subject
    where = ""
    if isinstance(step, int) and 0 <= step < len(steps):
        label = STEP_LABELS.get(steps[step].operation.type, steps[step].operation.type)
        where = f" at step {step + 1} ({label})"
    return (
        f"{NOTICE_PREFIX}the person clicked Create on the {card} card for "
        f"{name}, and oc8 refused it{where}: {why}. Nothing was created "
        f"(code: {code}). Tell the person in one or two plain sentences, in their "
        f"language, what went wrong. If you can fix it yourself, call {tool} "
        f"once with the corrected {subject} and say what you changed. If only they can "
        "fix it, say exactly what to do and on which page. Do not propose the same "
        f"{subject} unchanged."
    )


async def tell_copilot(
    db: AsyncSession,
    *,
    proposal: CopilotProposal,
    steps: list[BuilderStep],
    member: m.OrgMember,
    operator_role: str | None,
    board_title: str = "",
) -> bool:
    """Start the Copilot's one notice turn for `proposal`'s refusal in its
    chat, acting for `member` (the chat's owner, who clicked). Returns whether
    a turn was started. COMMITS (through `send_message`)."""
    from oc8.chat.service import send_message

    message = notice_text(proposal, steps, board_title=board_title)
    if message is None or proposal.session_id is None:
        return False
    result = dict(proposal.result or {})
    signature = refusal_signature(result)
    if result.get("noticeSent") == signature:
        return False
    session = await db.get(m.ChatSession, proposal.session_id)
    if session is None or session.member_id != member.id:
        return False
    proposal.result = {**result, "noticeSent": signature}
    await db.flush()
    await send_message(
        db,
        session=session,
        tenant_id=proposal.tenant_id,
        message=message,
        originating_operator=member.subject,
        operator_role=operator_role,
        role="followup",
        notice={"builder_proposal_id": str(proposal.id), "refusal": signature},
    )
    return True


async def tell_copilot_best_effort(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    proposal_id: uuid.UUID,
    steps: list[BuilderStep],
    member: m.OrgMember,
    operator_role: str | None,
    board_title: str = "",
) -> None:
    """`tell_copilot` after the refusal was committed: never lets the notice
    turn the person's refusal into an error."""
    try:
        await db.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)}
        )
        proposal = await db.get(CopilotProposal, proposal_id, populate_existing=True)
        if proposal is not None:
            await tell_copilot(
                db,
                proposal=proposal,
                steps=steps,
                member=member,
                operator_role=operator_role,
                board_title=board_title,
            )
    except Exception:
        logger.exception("agent builder: could not tell the Copilot about refusal %s", proposal_id)
        await db.rollback()
