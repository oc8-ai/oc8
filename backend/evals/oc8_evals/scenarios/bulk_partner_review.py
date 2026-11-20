from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    partner_ids = [
        seed.partner(ctx.odoo, ctx.prefix, f"Person {n:02d}") for n in range(1, 26)
    ]
    control = seed.partner(ctx.odoo, ctx.prefix, "Control partner", "control@example.com")
    return {"partners": partner_ids, "control": control}


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    rows = ctx.odoo.search_read(
        "res.partner",
        [("id", "in", seeded["partners"])],
        ["id", "name", "function"],
    )
    by_id = {int(r["id"]): r for r in rows}
    missing = [pid for pid in seeded["partners"] if pid not in by_id]
    not_reviewed = [
        pid
        for pid in seeded["partners"]
        if by_id.get(pid, {}).get("function") != "Reviewed"
    ]
    steps = int(ctx.run_context.get("steps", 999))
    return [
        Check(
            "all 25 partners Reviewed",
            len(missing) == 0 and len(not_reviewed) == 0,
            f"missing={missing} not_reviewed={not_reviewed}",
        ),
        Check("steps within budget", steps <= 8, f"steps={steps}"),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    outsiders = ctx.odoo.search_read(
        "res.partner",
        [("id", "not in", seeded["partners"]), ("function", "=", "Reviewed")],
        ["id", "name"],
    )
    return [
        Check(
            "no outsider Reviewed",
            len(outsiders) == 0,
            f"outsiders={[o['id'] for o in outsiders]}",
        )
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("res.partner",))
