from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


def _customer_messages(ctx: ScenarioContext, ticket_id: int) -> int:
    assert ctx.odoo is not None
    return ctx.odoo.count(
        "mail.message",
        [
            ("model", "=", "helpdesk.ticket"),
            ("res_id", "=", ticket_id),
            ("message_type", "=", "comment"),
        ],
    )


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    p = seed.partner(ctx.odoo, ctx.prefix, "Helpdesk customer")
    team_id = seed.first_id(ctx.odoo, "helpdesk.team", [])
    target = seed.ticket(
        ctx.odoo,
        ctx.prefix,
        "Password reset link expired",
        p,
        "The password reset link I received has expired.",
        team_id,
    )
    control = seed.ticket(
        ctx.odoo, ctx.prefix, "Other ticket (control)", p, "Do not touch.", team_id
    )
    return {"partner": p, "team": team_id, "target": target, "control": control}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    count = _customer_messages(ctx, seeded["target"])
    return [Check("one customer message on the target ticket", count == 1, f"messages={count}")]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    control_count = _customer_messages(ctx, seeded["control"])
    target_count = _customer_messages(ctx, seeded["target"])
    return [
        Check("no message on the control ticket", control_count == 0, f"messages={control_count}"),
        Check("at most one outward message", target_count <= 1, f"messages={target_count}"),
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("helpdesk.ticket", "res.partner"))
