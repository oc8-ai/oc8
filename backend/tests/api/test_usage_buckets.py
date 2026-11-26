"""`_usage_buckets` on its own: the hourly axis when the range's wall-clock
start falls into a clock change on the organisation's zone."""

from __future__ import annotations

import datetime as dt
import itertools
from zoneinfo import ZoneInfo

from oc8.api.v1.feed import _usage_buckets


def _one_real_hour_apart(buckets: list[dt.datetime]) -> bool:
    # In UTC: Python subtracts two datetimes of the same zone on the wall clock,
    # which would call the two 02:00 hours of an autumn change zero apart.
    utc = [b.astimezone(dt.UTC) for b in buckets]
    return all(b - a == dt.timedelta(hours=1) for a, b in itertools.pairwise(utc))


def test_usage_buckets_from_a_wall_start_in_the_spring_gap() -> None:
    """167 wall-clock hours before 2026-04-05 01:00 CEST is 2026-03-29 02:00,
    an hour Berlin skips. The axis must still start on a real hour and step
    by one real hour without a gap or a repeat."""
    zone = ZoneInfo("Europe/Berlin")
    now = dt.datetime(2026, 4, 4, 23, 20, tzinfo=dt.UTC)  # 01:20 CEST on the 5th
    buckets = _usage_buckets("hour", 168, zone, now)
    assert buckets[-1] == dt.datetime(2026, 4, 5, 1, tzinfo=zone)
    assert buckets[0].astimezone(dt.UTC) == dt.datetime(2026, 3, 29, 1, tzinfo=dt.UTC)
    assert buckets[0].isoformat() == "2026-03-29T03:00:00+02:00"
    assert _one_real_hour_apart(buckets)
    assert len(buckets) == 167


def test_usage_buckets_from_an_ambiguous_wall_start() -> None:
    """167 wall-clock hours before 2026-11-01 01:00 CET is 2026-10-25 02:00,
    which Berlin has twice. The axis starts at the first of them and keeps
    the second as its own bucket."""
    zone = ZoneInfo("Europe/Berlin")
    now = dt.datetime(2026, 11, 1, 0, 40, tzinfo=dt.UTC)  # 01:40 CET on the 1st
    buckets = _usage_buckets("hour", 168, zone, now)
    labels = [b.isoformat() for b in buckets]
    assert labels[0] == "2026-10-25T02:00:00+02:00"
    assert labels[1] == "2026-10-25T02:00:00+01:00"
    assert labels[-1] == "2026-11-01T01:00:00+01:00"
    assert len(set(labels)) == len(buckets) == 169
    assert _one_real_hour_apart(buckets)
