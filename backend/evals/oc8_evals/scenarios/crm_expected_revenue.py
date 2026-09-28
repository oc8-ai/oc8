from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    p = seed.partner(ctx.odoo, ctx.prefix, "Revenue customer", "info@revenue.example")
    target = seed.lead(
        ctx.odoo,
        ctx.prefix,
        "Acme — expected revenue update",
        p,
        "Pipeline opportunity awaiting revenue estimate.",
    )
    other = seed.lead(ctx.odoo, ctx.prefix, "Other lead (control)", p, "Do not touch.")
    return {"partner": p, "target": target, "other": other}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    row = ctx.odoo.search_read(
        "crm.lead", [("id", "=", seeded["target"])], ["expected_revenue"]
    )[0]
    revenue = row.get("expected_revenue")
    return [
        Check(
            "expected revenue is 1500",
            revenue == 1500 or revenue == 1500.0,
            f"expected_revenue={revenue}",
        )
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    others = ctx.odoo.search_read(
        "crm.lead",
        [("id", "!=", seeded["target"]), ("expected_revenue", "=", 1500)],
        ["id", "name"],
    )
    return [
        Check(
            "no other lead at 1500",
            len(others) == 0,
            f"others={[o['id'] for o in others]}",
        )
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("crm.lead", "res.partner"))
