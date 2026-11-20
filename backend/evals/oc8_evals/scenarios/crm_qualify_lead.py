from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    p = seed.partner(ctx.odoo, ctx.prefix, "Bergmann GmbH", "info@bergmann.example")
    target = seed.lead(
        ctx.odoo,
        ctx.prefix,
        "Bergmann GmbH — website inquiry",
        p,
        "Interested in 20 licences, asked for a call.",
    )
    other = seed.lead(ctx.odoo, ctx.prefix, "Other lead (control)", p, "Do not touch.")
    other_stage = ctx.odoo.search_read("crm.lead", [("id", "=", other)], ["stage_id"])[0][
        "stage_id"
    ]
    return {
        "partner": p,
        "target": target,
        "other": other,
        "other_stage": other_stage,
        "qualified_stage": seed.stage_named(ctx.odoo, "crm.stage", "Qualified"),
    }


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    lead = ctx.odoo.search_read("crm.lead", [("id", "=", seeded["target"])], ["stage_id"])[0]
    stage_id = lead["stage_id"][0] if lead["stage_id"] else None
    activities = ctx.odoo.count(
        "mail.activity", [("res_model", "=", "crm.lead"), ("res_id", "=", seeded["target"])]
    )
    return [
        Check(
            "lead in Qualified stage",
            stage_id == seeded["qualified_stage"],
            f"stage={lead['stage_id']}",
        ),
        Check("follow-up activity scheduled", activities >= 1, f"activities={activities}"),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    other = ctx.odoo.search_read("crm.lead", [("id", "=", seeded["other"])], ["stage_id"])[0]
    return [Check("control lead untouched", other["stage_id"] == seeded["other_stage"])]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    ctx.odoo.unlink(
        "mail.activity",
        [
            int(r["id"])
            for r in ctx.odoo.search_read(
                "mail.activity",
                [
                    ("res_model", "=", "crm.lead"),
                    ("res_id", "in", [seeded["target"], seeded["other"]]),
                ],
                ["id"],
            )
        ],
    )
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("crm.lead", "res.partner"))
