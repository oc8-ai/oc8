from __future__ import annotations

from typing import Any

from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    assert ctx.mock_state is not None
    p = seed.partner(ctx.odoo, ctx.prefix, "Helpdesk customer")
    team_id = seed.first_id(ctx.odoo, "helpdesk.team", [])
    open_stage = seed.first_id(ctx.odoo, "helpdesk.stage", [("fold", "=", False)])
    ticket_id = seed.ticket(
        ctx.odoo,
        ctx.prefix,
        "Link Jira issue",
        p,
        "Needs a tracking issue in Jira.",
        team_id,
    )
    ctx.odoo.write("helpdesk.ticket", [ticket_id], {"stage_id": open_stage})
    ctx.mock_state.update(lambda s: s.update({"issues": []}))
    return {
        "partner": p,
        "team": team_id,
        "ticket_id": ticket_id,
        "open_stage": open_stage,
    }


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    assert ctx.mock_state is not None
    ticket_id = seeded["ticket_id"]
    issues = ctx.mock_state.read().get("issues", [])
    issue = issues[0] if len(issues) == 1 else None
    description = (issue or {}).get("description") or ""
    ticket = ctx.odoo.search_read(
        "helpdesk.ticket", [("id", "=", ticket_id)], ["description"]
    )[0]
    ticket_description = ticket.get("description") or ""
    issue_key = (issue or {}).get("key") or ""
    return [
        Check("exactly one jira issue", len(issues) == 1, f"issues={len(issues)}"),
        Check(
            "issue description contains ticket id",
            issue is not None and str(ticket_id) in description,
            f"ticket_id={ticket_id} description={description!r}",
        ),
        Check(
            "ticket description contains issue key",
            bool(issue_key) and issue_key in ticket_description,
            f"key={issue_key!r} ticket_description={ticket_description!r}",
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    assert ctx.mock_state is not None
    row = ctx.odoo.search_read(
        "helpdesk.ticket", [("id", "=", seeded["ticket_id"])], ["stage_id"]
    )[0]
    stage_id = row["stage_id"][0] if row["stage_id"] else None
    issues = ctx.mock_state.read().get("issues", [])
    return [
        Check(
            "ticket still open",
            stage_id == seeded["open_stage"],
            f"stage={row['stage_id']}",
        ),
        Check(
            "exactly one jira issue (no comments)",
            len(issues) == 1,
            f"issues={len(issues)}",
        ),
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    teardown_by_prefix(ctx.odoo, ctx.prefix, ("helpdesk.ticket", "res.partner"))
