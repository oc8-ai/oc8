from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    summary = seed.partner(ctx.odoo, ctx.prefix, "Summary")
    lead_names = [f"Lead {n:02d}" for n in range(1, 31)]
    lead_ids = [
        seed.lead(ctx.odoo, ctx.prefix, name, summary, f"Long-run lead {name}.")
        for name in lead_names
    ]
    full_names = [f"{ctx.prefix}{name}" for name in lead_names]
    return {
        "summary": summary,
        "leads": lead_ids,
        "lead_names": full_names,
    }


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    row = ctx.odoo.search_read(
        "res.partner", [("id", "=", seeded["summary"])], ["comment"]
    )[0]
    comment = str(row.get("comment") or "")
    missing = [name for name in seeded["lead_names"] if name not in comment]
    harness = ctx.run_context.get("harness") or {}
    last_compacted = int(harness.get("last_compacted_step", -999))
    return [
        Check(
            "comment lists all 30 lead names",
            len(missing) == 0,
            f"missing={missing} comment={comment!r}",
        ),
        Check(
            "harness compacted at least once",
            last_compacted >= 0,
            f"last_compacted_step={last_compacted}",
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    return []


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("crm.lead", "res.partner"))
