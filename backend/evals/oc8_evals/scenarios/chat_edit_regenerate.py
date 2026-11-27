"""chat_harness / edit_regenerate: after an edit or a regenerate, the replay
is the person's branch and nothing else (spec §5.3 "Edit", §2.1 "Regenerate").

`setup` seeds `params.seed` ([role, text] pairs) as one path. The `start` hook
then edits the user turn at `params.target` (an index into `seed`) to
`params.content`, or regenerates the answer to it, through the same service
calls the chat API makes. What only the old branch or the first answer said
must not reach the new answer (`params.forbid`); what the kept path says must
(`params.expect`). Scored on the stored answer and on the chat's leaf.
"""

from __future__ import annotations

import uuid
from typing import Any

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8_evals import stack
from oc8_evals.scenarios import _chat
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    rows = [(str(role), str(text)) for role, text in ctx.task.params["seed"]]
    made = await _chat.seed_turns(ctx, rows)
    target = made[int(ctx.task.params["target"])]
    if target.role != "user":
        raise RuntimeError("params.target must index a user turn of params.seed")
    return {"target": target.id, "ids": [msg.id for msg in made]}


async def start(ctx: ScenarioContext, seeded: dict[str, Any]) -> uuid.UUID:
    from oc8.chat.service import edit_message, lock_regenerate, regenerate_reply

    fixture = _chat.chat(ctx)
    action = str(ctx.task.params.get("action", "edit"))
    async with tenant_session(ctx.tenant_id) as db:
        session = await db.get(m.ChatSession, fixture.session_id)
        member = await db.get(m.OrgMember, fixture.member_id)
        original = await db.get(m.ChatMessage, seeded["target"])
        assert session is not None and member is not None and original is not None
        if action == "edit":
            _message, run = await edit_message(
                db,
                session=session,
                tenant_id=ctx.tenant_id,
                original=original,
                content=str(ctx.task.params["content"]),
                originating_operator=fixture.subject,
                operator_role=stack.CHAT_ROLE,
                acting_member=member,
            )
        elif action == "regenerate":
            await lock_regenerate(db, session_id=session.id)
            run = await regenerate_reply(
                db,
                session=session,
                tenant_id=ctx.tenant_id,
                user_message=original,
                originating_operator=fixture.subject,
                operator_role=stack.CHAT_ROLE,
                acting_member=member,
            )
        else:
            raise RuntimeError(f"unknown action {action!r}")
        if run is None:
            raise RuntimeError("the chat refused the edited turn (no run was started)")
        return run.id


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    text = await _chat.reply_text(ctx)
    found, _ = _chat.reply_checks(text, expect=list(ctx.task.params.get("expect", [])), forbid=[])
    async with tenant_session(ctx.tenant_id) as db:
        session = await db.get(m.ChatSession, _chat.chat(ctx).session_id)
        leaf = session.current_leaf_id if session is not None else None
        reply = await db.get(m.ChatMessage, leaf) if leaf is not None else None
    on_leaf = reply is not None and reply.run_id == ctx.run_id
    return [
        _chat.answered(ctx, text),
        Check("the new answer is the chat's leaf", on_leaf, f"leaf={leaf}"),
        *found,
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    text = await _chat.reply_text(ctx)
    _, bad = _chat.reply_checks(text, expect=[], forbid=list(ctx.task.params.get("forbid", [])))
    return bad


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    return None
