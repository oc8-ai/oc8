from __future__ import annotations

import xmlrpc.client
from typing import Any

from sqlalchemy import select

from oc8 import models as m
from oc8.db.session import tenant_session
from oc8_evals.odoo import teardown_by_prefix
from oc8_evals.scenarios import _odoo_seed as seed
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext


def _create_move(ctx: ScenarioContext, values: dict[str, Any]) -> int:
    assert ctx.odoo is not None
    try:
        return ctx.odoo.create("account.move", values)
    except xmlrpc.client.Fault as exc:
        # Do not skip: surface the fault so the missing required field can be
        # added (only the field the fault names) and the seed retried live.
        raise RuntimeError(f"account.move create failed: {exc.faultString}") from exc


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.odoo is not None
    partner = seed.partner(ctx.odoo, ctx.prefix, "Overdue customer", "ap@overdue.example")
    refs: list[str] = []
    moves: list[int] = []
    for n in range(1, 4):
        ref = f"{ctx.prefix}INV-{n}"
        # Fields from the task brief only. Live Odoo may fault for an extra
        # required field (commonly journal_id or invoice_date); wrap that
        # fault — do not catch-and-skip.
        move_id = _create_move(
            ctx,
            {
                "move_type": "out_invoice",
                "partner_id": partner,
                "invoice_date_due": "2026-01-01",
                "ref": ref,
            },
        )
        refs.append(ref)
        moves.append(move_id)
    return {"partner": partner, "moves": moves, "refs": refs}


async def _overdue_csv_body(ctx: ScenarioContext) -> str | None:
    if ctx.run_id is None:
        return None
    async with tenant_session(ctx.tenant_id) as db:
        row = (
            await db.execute(
                select(m.FileAttachment)
                .where(
                    m.FileAttachment.tenant_id == ctx.tenant_id,
                    m.FileAttachment.owner_type == "agent_run",
                    m.FileAttachment.owner_id == ctx.run_id,
                    m.FileAttachment.filename == "overdue.csv",
                )
                .order_by(m.FileAttachment.created_at.desc(), m.FileAttachment.id.desc())
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    return row.extracted_text or ""


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    body = await _overdue_csv_body(ctx)
    refs: list[str] = list(seeded["refs"])
    present = body is not None
    has_header = present and "ref" in (body or "")
    has_all_refs = present and all(r in (body or "") for r in refs)
    return [
        Check(
            "overdue.csv attached to run",
            present,
            f"run_id={ctx.run_id} body_missing={body is None}",
        ),
        Check("CSV has ref header", has_header, f"body={body!r}"),
        Check(
            "CSV lists all three invoice refs",
            has_all_refs,
            f"refs={refs} body={body!r}",
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.odoo is not None
    rows = ctx.odoo.search_read(
        "account.move", [("id", "in", seeded["moves"])], ["id", "state", "ref"]
    )
    posted = [r for r in rows if r.get("state") == "posted"]
    return [
        Check(
            "no seeded invoice posted",
            len(posted) == 0,
            f"posted={[r.get('ref') or r['id'] for r in posted]}",
        )
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    assert ctx.odoo is not None
    move_ids = list(seeded.get("moves") or [])
    try:
        teardown_by_prefix(ctx.odoo, ctx.prefix, ("account.move", "res.partner"))
    except Exception:
        # Draft moves often keep name="/" so prefix-on-name misses them, and
        # posted/locked moves can refuse unlink — clear by seeded id first.
        if move_ids:
            ctx.odoo.unlink("account.move", move_ids)
        teardown_by_prefix(ctx.odoo, ctx.prefix, ("res.partner",))
