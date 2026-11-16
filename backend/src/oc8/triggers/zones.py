"""Reading a cron expression in a timezone -- shared by agent triggers
(`triggers.service.compute_next_run`) and Copilot follow-ups
(`copilot.schedule.next_fire`), so "weekdays 7:00" means 7:00 on the
person's clock on both sides of a DST change.

DST edges are croniter's, pinned in tests/triggers/test_timezones.py: a
local time skipped in spring fires at the first moment after the gap (02:30
on the March change in Berlin fires at 03:00), and a local time that happens
twice in autumn fires on both occurrences, an hour apart."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.models.core import Organization


class UnknownTimezone(ValueError):
    """Not an IANA timezone name this server knows."""


def valid_timezone(name: str) -> str:
    """`name` when it is a known IANA zone; UnknownTimezone otherwise."""
    if not name:
        raise UnknownTimezone("unknown time zone '' -- use an IANA name like Europe/Berlin")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezone(
            f"unknown time zone {name!r} -- use an IANA name like Europe/Berlin"
        ) from exc
    return name


def next_fire_utc(cron_expression: str, *, timezone: str | None, after: dt.datetime) -> dt.datetime:
    """The next fire strictly after `after`, reading `cron_expression` in
    `timezone` (None = UTC), returned in UTC. Raises UnknownTimezone for a
    bad zone and croniter's own errors for a bad expression."""
    zone = ZoneInfo(valid_timezone(timezone)) if timezone else dt.UTC
    nxt = cast(dt.datetime, croniter(cron_expression, after.astimezone(zone)).get_next(dt.datetime))
    return nxt.astimezone(dt.UTC)


async def organisation_timezone(db: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The organisation's own timezone, the default for a new schedule; UTC
    when it has none or names one this server does not know (the same safe
    fallback as `agent.harness.prompts.resolve_timezone`)."""
    org = await db.get(Organization, tenant_id)
    try:
        return valid_timezone(org.timezone if org is not None else "UTC")
    except UnknownTimezone:
        return "UTC"
