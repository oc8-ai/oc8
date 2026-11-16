"""An agent schedule is read in its trigger's timezone: "weekdays 7:00" in
Berlin stays 7:00 Berlin across both DST changes. NULL means UTC, so a
trigger saved before triggers carried a zone keeps firing as it did."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from oc8 import models as m
from oc8.triggers.service import (
    InvalidTriggerConfig,
    compute_next_run,
    create_trigger,
    update_trigger,
)
from oc8.triggers.zones import UnknownTimezone, next_fire_utc, valid_timezone
from tests.conftest import AppSessionFactory

BERLIN = "Europe/Berlin"


def _fires(expr: str, *, after: datetime, timezone: str | None, n: int) -> list[datetime]:
    out: list[datetime] = []
    for _ in range(n):
        after = compute_next_run(expr, after=after, timezone=timezone, jitter_seconds=0)
        out.append(after)
    return out


def test_without_a_timezone_the_cron_is_read_in_utc() -> None:
    after = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    assert compute_next_run("0 7 * * 1-5", after=after, jitter_seconds=0) == datetime(
        2026, 7, 13, 7, 0, tzinfo=UTC
    )
    assert compute_next_run(
        "0 7 * * 1-5", after=after, timezone=None, jitter_seconds=0
    ) == datetime(2026, 7, 13, 7, 0, tzinfo=UTC)


def test_weekdays_seven_in_berlin_is_five_utc_in_summer_and_six_in_winter() -> None:
    summer = compute_next_run(
        "0 7 * * 1-5",
        after=datetime(2026, 7, 13, 0, 0, tzinfo=UTC),
        timezone=BERLIN,
        jitter_seconds=0,
    )
    winter = compute_next_run(
        "0 7 * * 1-5",
        after=datetime(2026, 1, 12, 0, 0, tzinfo=UTC),
        timezone=BERLIN,
        jitter_seconds=0,
    )
    assert summer == datetime(2026, 7, 13, 5, 0, tzinfo=UTC)  # CEST, UTC+2
    assert winter == datetime(2026, 1, 12, 6, 0, tzinfo=UTC)  # CET, UTC+1
    assert summer.tzinfo is UTC


def test_seven_local_stays_seven_local_across_the_march_change() -> None:
    # Sunday 29 March 2026: clocks go 02:00 -> 03:00 in Berlin.
    fires = _fires(
        "0 7 * * *", after=datetime(2026, 3, 27, 12, 0, tzinfo=UTC), timezone=BERLIN, n=3
    )
    assert fires == [
        datetime(2026, 3, 28, 6, 0, tzinfo=UTC),
        datetime(2026, 3, 29, 5, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 5, 0, tzinfo=UTC),
    ]
    assert {f.astimezone(ZoneInfo(BERLIN)).hour for f in fires} == {7}


def test_seven_local_stays_seven_local_across_the_october_change() -> None:
    # Sunday 25 October 2026: clocks go 03:00 -> 02:00 in Berlin.
    fires = _fires(
        "0 7 * * *", after=datetime(2026, 10, 23, 12, 0, tzinfo=UTC), timezone=BERLIN, n=3
    )
    assert fires == [
        datetime(2026, 10, 24, 5, 0, tzinfo=UTC),
        datetime(2026, 10, 25, 6, 0, tzinfo=UTC),
        datetime(2026, 10, 26, 6, 0, tzinfo=UTC),
    ]


def test_a_time_skipped_in_march_fires_at_the_first_moment_after_the_gap() -> None:
    # 02:30 does not exist in Berlin on 29 March; croniter fires at 03:00
    # CEST (01:00 UTC) that day rather than skipping the day.
    fires = _fires(
        "30 2 * * *", after=datetime(2026, 3, 28, 12, 0, tzinfo=UTC), timezone=BERLIN, n=2
    )
    assert fires == [
        datetime(2026, 3, 29, 1, 0, tzinfo=UTC),
        datetime(2026, 3, 30, 0, 30, tzinfo=UTC),
    ]


def test_a_time_repeated_in_october_fires_on_both_occurrences() -> None:
    # 02:30 happens twice in Berlin on 25 October (CEST, then CET); croniter
    # fires on each, an hour apart.
    fires = _fires(
        "30 2 * * *", after=datetime(2026, 10, 24, 12, 0, tzinfo=UTC), timezone=BERLIN, n=3
    )
    assert fires == [
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        datetime(2026, 10, 26, 1, 30, tzinfo=UTC),
    ]


def test_an_unknown_timezone_is_refused() -> None:
    with pytest.raises(InvalidTriggerConfig, match="unknown time zone"):
        compute_next_run("0 7 * * *", after=datetime.now(tz=UTC), timezone="Mars/Olympus")


def test_the_shared_helper_names_the_zone_and_refuses_unknown_ones() -> None:
    assert valid_timezone("Europe/Berlin") == "Europe/Berlin"
    for bad in ("Mars/Olympus", "", "../etc/passwd"):
        with pytest.raises(UnknownTimezone):
            valid_timezone(bad)
    assert next_fire_utc(
        "0 7 * * *", timezone=BERLIN, after=datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    ) == datetime(2026, 7, 13, 5, 0, tzinfo=UTC)


async def _tenant_in(app_session: AppSessionFactory, zone: str) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"tz-{tenant.hex[:12]}",
                name="TZ Org",
                tier="standard",
                region="eu",
                timezone=zone,
            )
        )
        await db.flush()
    return tenant


@pytest.mark.asyncio
async def test_a_new_cron_trigger_defaults_to_the_organisations_timezone(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _tenant_in(app_session, BERLIN)
    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="report",
            cron_expression="0 7 * * *",
        )
        assert trigger.timezone == BERLIN
        assert trigger.next_run_at is not None
        assert trigger.next_run_at.astimezone(ZoneInfo(BERLIN)).hour == 7


@pytest.mark.asyncio
async def test_an_organisation_with_a_broken_timezone_gives_utc(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _tenant_in(app_session, "Not/AZone")
    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="report",
            cron_expression="0 7 * * *",
        )
        assert trigger.timezone == "UTC"


@pytest.mark.asyncio
async def test_a_given_timezone_wins_and_an_unknown_one_is_refused(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _tenant_in(app_session, BERLIN)
    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="report",
            cron_expression="0 7 * * *",
            timezone="America/New_York",
        )
        assert trigger.timezone == "America/New_York"
        assert trigger.next_run_at is not None
        assert trigger.next_run_at.astimezone(ZoneInfo("America/New_York")).hour == 7
        with pytest.raises(InvalidTriggerConfig, match="unknown time zone"):
            await create_trigger(
                db,
                tenant_id=tenant,
                agent_id=uuid.uuid4(),
                kind="cron",
                task_text="report",
                cron_expression="0 7 * * *",
                timezone="Mars/Olympus",
            )


@pytest.mark.asyncio
async def test_an_event_or_webhook_trigger_carries_no_timezone(
    app_session: AppSessionFactory,
) -> None:
    tenant = await _tenant_in(app_session, BERLIN)
    async with app_session(tenant) as db:
        hook = await create_trigger(
            db, tenant_id=tenant, agent_id=uuid.uuid4(), kind="webhook", task_text="x"
        )
        assert hook.timezone is None


@pytest.mark.asyncio
async def test_updating_the_timezone_moves_the_next_run(app_session: AppSessionFactory) -> None:
    tenant = await _tenant_in(app_session, "UTC")
    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="report",
            cron_expression="0 7 * * *",
        )
        assert trigger.timezone == "UTC"
        await update_trigger(db, trigger, timezone="Asia/Tokyo")
        assert trigger.timezone == "Asia/Tokyo"
        assert trigger.next_run_at is not None
        assert trigger.next_run_at.astimezone(ZoneInfo("Asia/Tokyo")).hour == 7
        # A new expression alone keeps the zone it has.
        await update_trigger(db, trigger, cron_expression="0 18 * * *")
        assert trigger.timezone == "Asia/Tokyo"
        assert trigger.next_run_at.astimezone(ZoneInfo("Asia/Tokyo")).hour == 18
        with pytest.raises(InvalidTriggerConfig, match="unknown time zone"):
            await update_trigger(db, trigger, timezone="Mars/Olympus")
        assert trigger.timezone == "Asia/Tokyo"


@pytest.mark.asyncio
async def test_a_trigger_saved_without_a_zone_keeps_firing_in_utc_when_edited(
    app_session: AppSessionFactory,
) -> None:
    """Rows from before triggers carried a zone stay NULL (= UTC) -- an edit of
    the expression alone does not quietly move them into the org's zone."""
    tenant = await _tenant_in(app_session, BERLIN)
    async with app_session(tenant) as db:
        trigger = await create_trigger(
            db,
            tenant_id=tenant,
            agent_id=uuid.uuid4(),
            kind="cron",
            task_text="report",
            cron_expression="0 7 * * *",
        )
        trigger.timezone = None
        await db.flush()
        await update_trigger(db, trigger, cron_expression="0 9 * * *")
        assert trigger.timezone is None
        assert trigger.next_run_at is not None
        assert trigger.next_run_at.astimezone(UTC).hour == 9
