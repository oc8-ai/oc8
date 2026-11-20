"""Steer a running agent (oc8 Chat spec §3.2, §8 eval).

While the run works on the ticket, a member writes to it: "write 'Checked the
logs twice' instead". Pass on STATE, never the transcript: the note on the
ticket carries the steered text, and the run_input row was consumed at a step.
The input is queued through `add_run_input` (what POST /runs/{id}/inputs
stores), as soon as the run is running -- this measures delivery, not the API.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import select

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8.runtime.run_inputs import add_run_input
from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

_STEERED = "Checked the logs twice"
_INPUT = f'Change of plan: write the note text "{_STEERED}" instead of "Checked the logs".'
_tasks: set[asyncio.Task[None]] = set()


async def _steer_when_running(
    ctx: ScenarioContext, member_id: uuid.UUID, seeded: dict[str, Any]
) -> None:
    for _ in range(600):
        if ctx.run_id is not None:
            async with tenant_session(ctx.tenant_id) as db:
                run = await db.get(m.AgentRun, ctx.run_id)
                member = await db.get(m.OrgMember, member_id)
                if run is not None and member is not None and run.state == "running":
                    row = await add_run_input(db, run=run, member=member, text=_INPUT)
                    seeded["input_id"] = row.id
                    return
        await asyncio.sleep(0.2)


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    p = seed.partner(ctx.odoo, ctx.prefix, "Helpdesk customer")
    team_id = seed.first_id(ctx.odoo, "helpdesk.team", [])
    open_stage = seed.first_id(ctx.odoo, "helpdesk.stage", [("fold", "=", False)])
    target = seed.ticket(
        ctx.odoo,
        ctx.prefix,
        "Login fails intermittently",
        p,
        "Customer reports intermittent login failures.",
        team_id,
    )
    ctx.odoo.write("helpdesk.ticket", [target], {"stage_id": open_stage})
    async with tenant_session(ctx.tenant_id) as db:
        member = m.OrgMember(
            tenant_id=ctx.tenant_id,
            subject=f"eval-steer-{ctx.run_tag}",
            subject_uuid=uuid.uuid4(),
            display_name="Eval operator",
        )
        db.add(member)
        await db.flush()
        member_id = member.id
    seeded: dict[str, Any] = {
        "partner": p,
        "target": target,
        "open_stage": open_stage,
        "member_id": member_id,
    }
    task = asyncio.create_task(_steer_when_running(ctx, member_id, seeded))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return seeded


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    messages = ctx.odoo.search_read(
        "mail.message",
        [("model", "=", "helpdesk.ticket"), ("res_id", "=", seeded["target"])],
        ["body"],
    )
    row = ctx.odoo.search_read("helpdesk.ticket", [("id", "=", seeded["target"])], ["description"])[
        0
    ]
    steered = any(_STEERED in (msg.get("body") or "") for msg in messages) or _STEERED in (
        row.get("description") or ""
    )
    input_id = seeded.get("input_id")
    consumed_step = None
    if input_id is not None:
        async with tenant_session(ctx.tenant_id) as db:
            item = (
                await db.execute(select(m.RunInput).where(m.RunInput.id == input_id))
            ).scalar_one_or_none()
            consumed_step = item.consumed_step if item is not None else None
    return [
        Check(
            "the input was written while the run was running",
            input_id is not None,
            f"input_id={input_id}",
        ),
        Check(
            "the input was taken into account at a step",
            consumed_step is not None,
            f"consumed_step={consumed_step}",
        ),
        Check("the note carries the steered text", steered, f"messages={len(messages)}"),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    row = ctx.odoo.search_read("helpdesk.ticket", [("id", "=", seeded["target"])], ["stage_id"])[0]
    stage_id = row["stage_id"][0] if row["stage_id"] else None
    return [
        Check("ticket still open", stage_id == seeded["open_stage"], f"stage={row['stage_id']}")
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("helpdesk.ticket", "res.partner"))
    member_id = seeded.get("member_id")
    if member_id is not None:
        async with tenant_session(ctx.tenant_id) as db:
            member = await db.get(m.OrgMember, member_id)
            if member is not None:
                await db.delete(member)
