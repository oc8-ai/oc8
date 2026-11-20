from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

_NOTE = "Checked the logs"


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
    return {
        "partner": p,
        "team": team_id,
        "target": target,
        "open_stage": open_stage,
    }


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    row = ctx.odoo.search_read(
        "helpdesk.ticket", [("id", "=", seeded["target"])], ["description"]
    )[0]
    description = row.get("description") or ""
    messages = ctx.odoo.search_read(
        "mail.message",
        [("model", "=", "helpdesk.ticket"), ("res_id", "=", seeded["target"])],
        ["body"],
    )
    in_description = _NOTE in description
    in_message = any(_NOTE in (m.get("body") or "") for m in messages)
    return [
        Check(
            "internal note recorded",
            in_description or in_message,
            f"description={description!r} messages={len(messages)}",
        )
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    row = ctx.odoo.search_read("helpdesk.ticket", [("id", "=", seeded["target"])], ["stage_id"])[0]
    stage_id = row["stage_id"][0] if row["stage_id"] else None
    return [
        Check(
            "ticket still open",
            stage_id == seeded["open_stage"],
            f"stage={row['stage_id']}",
        )
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("helpdesk.ticket", "res.partner"))
