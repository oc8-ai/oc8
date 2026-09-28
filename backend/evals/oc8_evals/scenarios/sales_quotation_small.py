from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


def _orders(ctx: ScenarioContext, partner: int) -> list[dict[str, Any]]:
    assert ctx.odoo is not None
    # amount_untaxed, not amount_total: the task asks for list-price sum, and
    # Odoo adds tax into amount_total (P12 false-success: 2500 list → 2875 total).
    return ctx.odoo.search_read(
        "sale.order",
        [("partner_id", "=", partner)],
        ["id", "state", "amount_untaxed", "order_line"],
    )


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    p = seed.partner(ctx.odoo, ctx.prefix, "Nordlicht AG", "buy@nordlicht.example")
    a = seed.product(ctx.odoo, ctx.prefix, "Consulting day", 800.0)
    b = seed.product(ctx.odoo, ctx.prefix, "Onboarding package", 900.0)
    return {"partner": p, "products": [a, b], "expected_total": 2 * 800.0 + 900.0}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    orders = _orders(ctx, seeded["partner"])
    one = orders[0] if orders else None
    return [
        Check("exactly one quotation", len(orders) == 1, f"orders={len(orders)}"),
        Check("two order lines", one is not None and len(one["order_line"]) == 2),
        Check(
            "total matches list prices",
            one is not None and abs(one["amount_untaxed"] - seeded["expected_total"]) < 0.01,
            f"untaxed={one['amount_untaxed'] if one else None}",
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    orders = _orders(ctx, seeded["partner"])
    return [Check("quotation not confirmed", all(o["state"] in ("draft", "sent") for o in orders))]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    for o in _orders(ctx, seeded["partner"]):
        if o["state"] not in ("draft", "sent", "cancel"):
            ctx.odoo.write("sale.order", [o["id"]], {"state": "cancel"})
        ctx.odoo.unlink("sale.order", [o["id"]])
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("product.product", "res.partner"))
