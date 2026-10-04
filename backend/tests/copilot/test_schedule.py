from __future__ import annotations

import datetime as dt

import pytest

from oc8.copilot.schedule import FollowupRejected, next_fire, validate_followup

NOW = dt.datetime(2026, 10, 5, 7, 0, tzinfo=dt.UTC)


def test_recurring_needs_timezone() -> None:
    with pytest.raises(FollowupRejected, match="time zone"):
        validate_followup(
            kind="cron",
            timezone=None,
            run_at=None,
            cron_expression="0 9 * * 1-5",
            ends_at="2026-10-31T00:00:00+01:00",
            now=NOW,
        )


def test_recurring_needs_end() -> None:
    with pytest.raises(FollowupRejected, match="end date"):
        validate_followup(
            kind="cron",
            timezone="Europe/Berlin",
            run_at=None,
            cron_expression="0 9 * * 1-5",
            ends_at=None,
            now=NOW,
        )


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(FollowupRejected, match="time zone"):
        validate_followup(
            kind="once",
            timezone="Mars/Olympus",
            run_at="2026-10-06T09:00:00+02:00",
            cron_expression=None,
            ends_at=None,
            now=NOW,
        )


def test_naive_run_at_is_rejected() -> None:
    with pytest.raises(FollowupRejected, match="offset"):
        validate_followup(
            kind="once",
            timezone="Europe/Berlin",
            run_at="2026-10-06T09:00:00",
            cron_expression=None,
            ends_at=None,
            now=NOW,
        )


def test_interval_below_fifteen_minutes_rejected() -> None:
    with pytest.raises(FollowupRejected, match="15 minutes"):
        validate_followup(
            kind="cron",
            timezone="UTC",
            run_at=None,
            cron_expression="*/5 * * * *",
            ends_at="2026-10-06T00:00:00+00:00",
            now=NOW,
        )


def test_end_beyond_a_year_rejected() -> None:
    with pytest.raises(FollowupRejected, match="one year"):
        validate_followup(
            kind="cron",
            timezone="UTC",
            run_at=None,
            cron_expression="0 9 * * *",
            ends_at="2027-12-01T00:00:00+00:00",
            now=NOW,
        )


def test_once_in_the_past_rejected() -> None:
    with pytest.raises(FollowupRejected, match="future"):
        validate_followup(
            kind="once",
            timezone="UTC",
            run_at="2026-10-01T09:00:00+00:00",
            cron_expression=None,
            ends_at=None,
            now=NOW,
        )


def test_next_fire_respects_timezone() -> None:
    # 09:00 Berlin on a Monday in CEST (UTC+2) is 07:00 UTC.
    after = dt.datetime(2026, 10, 5, 6, 0, tzinfo=dt.UTC)  # Monday 08:00 Berlin
    assert next_fire("0 9 * * 1-5", timezone="Europe/Berlin", after=after) == dt.datetime(
        2026, 10, 5, 7, 0, tzinfo=dt.UTC
    )


def test_valid_recurring_spec() -> None:
    spec = validate_followup(
        kind="cron",
        timezone="Europe/Berlin",
        run_at=None,
        cron_expression="0 9 * * 1-5",
        ends_at="2026-10-31T00:00:00+01:00",
        now=NOW,
    )
    assert spec.kind == "cron" and spec.ends_at is not None and spec.ends_at.tzinfo is not None
