"""Validation for a Copilot follow-up (design §7a.4). Pure: no database, no
clock of its own -- `now` is passed in so a test can pin it.

Every rule here is enforced in code rather than asked of the model: a
recurring schedule without a time zone or an end is exactly the kind of
"sounds right" instruction that quietly runs at the wrong hour for ever."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter

MIN_INTERVAL = dt.timedelta(minutes=15)
#: Research may delegate, so it costs more than a check-in; an hour bounds the
#: worst case per responsibility (D2 design section 1).
RESEARCH_MIN_INTERVAL = dt.timedelta(hours=1)
MAX_HORIZON = dt.timedelta(days=366)
MAX_ACTIVE_FOLLOWUPS = 20


class FollowupRejected(ValueError):
    """A follow-up the tool refuses, with a sentence the model can act on."""


FollowupPurpose = Literal["check_in", "research"]


@dataclass(frozen=True)
class FollowupSpec:
    kind: Literal["once", "cron"]
    timezone: str
    run_at: dt.datetime | None
    cron_expression: str | None
    ends_at: dt.datetime | None
    purpose: FollowupPurpose = "check_in"


def _zone(name: str | None) -> ZoneInfo:
    if not name:
        raise FollowupRejected("a follow-up needs a time zone -- ask the member which one")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise FollowupRejected(
            f"unknown time zone {name!r} -- use an IANA name like Europe/Berlin"
        ) from exc


def _instant(raw: str | None, what: str) -> dt.datetime:
    if not raw:
        raise FollowupRejected(f"{what} is required")
    try:
        value = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise FollowupRejected(f"{what} must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        raise FollowupRejected(f"{what} must include a UTC offset, e.g. 2026-10-06T09:00:00+02:00")
    return value.astimezone(dt.UTC)


def next_fire(cron_expression: str, *, timezone: str, after: dt.datetime) -> dt.datetime:
    """The next fire strictly after `after`, reading the expression in `timezone`."""
    local_after = after.astimezone(ZoneInfo(timezone))
    try:
        nxt = cast(dt.datetime, croniter(cron_expression, local_after).get_next(dt.datetime))
    except (CroniterBadCronError, ValueError) as exc:
        raise FollowupRejected(f"invalid cron expression: {exc}") from exc
    return nxt.astimezone(dt.UTC)


def validate_followup(
    *,
    kind: str,
    timezone: str | None,
    run_at: str | None,
    cron_expression: str | None,
    ends_at: str | None,
    now: dt.datetime,
    purpose: str | None = None,
) -> FollowupSpec:
    zone = _zone(timezone)
    if purpose not in (None, "check_in", "research"):
        raise FollowupRejected("purpose must be 'check_in' or 'research'")
    research = purpose == "research"
    floor = RESEARCH_MIN_INTERVAL if research else MIN_INTERVAL
    gap = "an hour" if research else "15 minutes"
    what = "research follow-up" if research else "follow-up"
    chosen: FollowupPurpose = "research" if research else "check_in"
    if kind == "once":
        at = _instant(run_at, "run_at")
        if at <= now:
            raise FollowupRejected("run_at must be in the future")
        # Same floor as a recurring one: a follow-up rescheduling itself must
        # not become a faster loop than cron is allowed to be.
        if at < now + floor:
            raise FollowupRejected(f"a {what} must be at least {gap} from now")
        if at - now > MAX_HORIZON:
            raise FollowupRejected("a follow-up can be at most one year out")
        return FollowupSpec("once", zone.key, at, None, None, chosen)
    if kind != "cron":
        raise FollowupRejected("kind must be 'once' or 'cron'")
    if not cron_expression:
        raise FollowupRejected("a recurring follow-up needs a cron expression")
    if not ends_at:
        raise FollowupRejected(
            "a recurring follow-up needs an end date -- ask the member until when"
        )
    end = _instant(ends_at, "ends_at")
    if end <= now:
        raise FollowupRejected("ends_at must be in the future")
    if end - now > MAX_HORIZON:
        raise FollowupRejected("a recurring follow-up can run at most one year")
    # Check the actual spacing of the next few fires, not the expression text:
    # "*/5" and "0,5,10 * * * *" are both too frequent, and only the fires say so.
    fire = next_fire(cron_expression, timezone=zone.key, after=now)
    for _ in range(5):
        following = next_fire(cron_expression, timezone=zone.key, after=fire)
        if following - fire < floor:
            raise FollowupRejected(f"a recurring {what} must be at least {gap} apart")
        fire = following
    return FollowupSpec("cron", zone.key, None, cron_expression, end, chosen)
