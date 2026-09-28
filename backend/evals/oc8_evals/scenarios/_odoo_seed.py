"""Seed helpers shared by the Odoo scenarios. Every seeded record's name
starts with the run's eval prefix so teardown can delete by prefix."""

from __future__ import annotations

from typing import Any

from oc8_evals.odoo import Odoo


def partner(odoo: Odoo, prefix: str, name: str, email: str | None = None) -> int:
    return odoo.create("res.partner", {"name": f"{prefix}{name}", "email": email})


def lead(odoo: Odoo, prefix: str, name: str, partner_id: int, description: str = "") -> int:
    return odoo.create(
        "crm.lead",
        {
            "name": f"{prefix}{name}",
            "partner_id": partner_id,
            "type": "opportunity",
            "description": description,
        },
    )


def product(odoo: Odoo, prefix: str, name: str, price: float) -> int:
    return odoo.create(
        "product.product", {"name": f"{prefix}{name}", "list_price": price, "type": "consu"}
    )


def ticket(
    odoo: Odoo,
    prefix: str,
    name: str,
    partner_id: int,
    description: str,
    team_id: int | None = None,
) -> int:
    values: dict[str, Any] = {
        "name": f"{prefix}{name}",
        "partner_id": partner_id,
        "description": description,
    }
    if team_id is not None:
        values["team_id"] = team_id
    return odoo.create("helpdesk.ticket", values)


def first_id(odoo: Odoo, model: str, domain: list[Any]) -> int:
    rows = odoo.search_read(model, domain, ["id"], limit=1)
    if not rows:
        raise RuntimeError(f"no {model} matching {domain}")
    return int(rows[0]["id"])


def stage_named(odoo: Odoo, model: str, needle: str) -> int:
    """`crm.stage` / `helpdesk.stage` whose name contains `needle` (case-insensitive)."""
    return first_id(odoo, model, [("name", "ilike", needle)])
