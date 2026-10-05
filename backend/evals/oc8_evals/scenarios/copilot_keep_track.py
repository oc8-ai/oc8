"""The member asks the Copilot to keep an eye on something until month end.

Checked on DB state, never on the transcript: one Responsibility, one enabled
cron follow-up with a timezone that ends this month -- or, when the zone cannot
be inferred, a parked `ask_user` run that asks for it. Either way the Copilot
must not have called any connection tool (only control tools / delegations).

NOT YET RUNNABLE: the harness starts runs on a fixture agent straight in the DB
(`stack.start_run`), but this scenario needs a Copilot chat turn for a seeded
member who holds `copilot:use` through an assigned role. The harness has no HTTP
path or member auth to open one, so `setup` fails loudly rather than letting a
fixture-agent run score against the wrong thing. It lives in its own `copilot`
suite, so the default `office` suite never picks it up. Once the harness can
start a member's chat turn, `setup` returns `{"member_id": ..., "started_at": ...}`
and `expect`/`forbid` below work unchanged.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from sqlalchemy import select

from oc8 import models as m
from oc8.agent.control_tools import CONTROL_TOOL_NAMES
from oc8.db.session import tenant_session
from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

_QUESTION_WORDS = ("zeitzone", "time zone", "timezone")


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    raise RuntimeError(
        "copilot_keep_track is not runnable yet: the eval harness cannot start a "
        "Copilot chat turn for a member (see the module docstring)"
    )


def _asked_for_timezone(ctx: ScenarioContext) -> bool:
    if ctx.final_state != "waiting_for_input":
        return False
    for call in ctx.run_context.get("toolCalls", []):
        if call.get("tool") == "ask_user":
            question = str(call.get("arguments", {}).get("question", "")).lower()
            if any(word in question for word in _QUESTION_WORDS):
                return True
    return False


def _within_this_month(ends_at: dt.datetime | None, now: dt.datetime) -> bool:
    if ends_at is None:
        return False
    last_day = calendar.monthrange(now.year, now.month)[1]
    month_end = dt.datetime(now.year, now.month, last_day, 23, 59, 59, tzinfo=dt.UTC)
    return now - dt.timedelta(days=1) <= ends_at <= month_end + dt.timedelta(days=1)


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    since: dt.datetime = seeded["started_at"]
    if _asked_for_timezone(ctx):
        return [
            Check(
                "asked for the time zone instead of guessing",
                True,
                "waiting_for_input",
            )
        ]
    async with tenant_session(ctx.tenant_id) as db:
        responsibilities = (
            (
                await db.execute(
                    select(m.Responsibility).where(
                        m.Responsibility.tenant_id == ctx.tenant_id,
                        m.Responsibility.member_id == seeded["member_id"],
                        m.Responsibility.created_at >= since,
                    )
                )
            )
            .scalars()
            .all()
        )
        triggers = (
            (
                await db.execute(
                    select(m.Trigger).where(
                        m.Trigger.tenant_id == ctx.tenant_id,
                        m.Trigger.responsibility_id.in_([r.id for r in responsibilities]),
                        m.Trigger.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    now = dt.datetime.now(tz=dt.UTC)
    cron = [t for t in triggers if t.kind == "cron"]
    return [
        Check(
            "exactly one responsibility opened",
            len(responsibilities) == 1,
            f"responsibilities={len(responsibilities)}",
        ),
        Check(
            "one enabled cron follow-up",
            len(cron) == 1,
            f"cron_triggers={len(cron)}",
        ),
        Check(
            "follow-up has a timezone",
            bool(cron) and bool(cron[0].timezone),
            f"timezone={cron[0].timezone if cron else None}",
        ),
        Check(
            "follow-up ends at the end of this month",
            bool(cron) and _within_this_month(cron[0].ends_at, now),
            f"ends_at={cron[0].ends_at if cron else None}",
        ),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    offending = [
        str(call.get("tool"))
        for call in ctx.run_context.get("toolCalls", [])
        if call.get("tool") not in CONTROL_TOOL_NAMES
    ]
    return [
        Check(
            "no connection tool called by the Copilot",
            not offending,
            f"connection_calls={offending}",
        )
    ]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    return None
