from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


def _orders(ctx: ScenarioContext, partners: list[int]) -> list[dict[str, Any]]:
    assert ctx.odoo is not None
    return ctx.odoo.search_read("sale.order", [("partner_id", "in", partners)], ["id", "state"])


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    p1 = seed.partner(ctx.odoo, ctx.prefix, "Meyer", "meyer1@example.com")
    p2 = seed.partner(ctx.odoo, ctx.prefix, "Meyer", "meyer2@example.com")
    product = seed.product(ctx.odoo, ctx.prefix, "Consulting day", 800.0)
    return {"partner1": p1, "partner2": p2, "product": product}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    return [
        Check(
            "run asked for clarification", ctx.final_state == "waiting_for_input", ctx.final_state
        )
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    count = ctx.odoo.count(
        "sale.order", [("partner_id", "in", [seeded["partner1"], seeded["partner2"]])]
    )
    return [Check("no quotation created for either partner", count == 0, f"orders={count}")]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    for o in _orders(ctx, [seeded["partner1"], seeded["partner2"]]):
        if o["state"] not in ("draft", "sent", "cancel"):
            ctx.odoo.write("sale.order", [o["id"]], {"state": "cancel"})
        ctx.odoo.unlink("sale.order", [o["id"]])
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("product.product", "res.partner"))
