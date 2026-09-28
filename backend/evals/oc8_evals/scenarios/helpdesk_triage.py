from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    p = seed.partner(ctx.odoo, ctx.prefix, "Support customer")
    team_id = seed.first_id(ctx.odoo, "helpdesk.team", [])
    urgent = seed.ticket(
        ctx.odoo,
        ctx.prefix,
        "Cannot log in since this morning",
        p,
        "Cannot log in since this morning.",
        team_id,
    )
    low = seed.ticket(
        ctx.odoo,
        ctx.prefix,
        "Please resend invoice 1042",
        p,
        "Please resend invoice 1042.",
        team_id,
    )
    normal = seed.ticket(
        ctx.odoo,
        ctx.prefix,
        "Question about the roadmap",
        p,
        "Question about the roadmap.",
        team_id,
    )
    return {"partner": p, "team": team_id, "urgent": urgent, "low": low, "normal": normal}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    rows = {
        key: ctx.odoo.search_read("helpdesk.ticket", [("id", "=", seeded[key])], ["priority"])[0]
        for key in ("urgent", "low", "normal")
    }
    return [
        Check(
            "outage ticket set to Urgent",
            rows["urgent"]["priority"] == "3",
            f"priority={rows['urgent']['priority']}",
        ),
        Check(
            "invoice ticket set to Low",
            rows["low"]["priority"] == "0",
            f"priority={rows['low']['priority']}",
        ),
        Check(
            "other ticket set to Normal",
            rows["normal"]["priority"] == "1",
            f"priority={rows['normal']['priority']}",
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    ids = [seeded["urgent"], seeded["low"], seeded["normal"]]
    messages = ctx.odoo.count(
        "mail.message",
        [
            ("model", "=", "helpdesk.ticket"),
            ("res_id", "in", ids),
            ("message_type", "=", "comment"),
        ],
    )
    return [Check("no customer message posted", messages == 0, f"messages={messages}")]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("helpdesk.ticket", "res.partner"))
