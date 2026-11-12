"""Trims Moodle's answers to what a task needs before they reach a model.

Three rules: lists are capped (with `total`/`truncated` so the agent knows
there is more), long text is plain and capped, and a person is reduced to
the handful of fields an agent works with. Address, phone, profile text,
custom profile fields and picture URLs never leave the bridge.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TEXT = 4000
TRUNCATED_MARK = " … [truncated]"

_BREAKS = re.compile(r"<\s*(?:br|/p|/div|/li|/h[1-6])\s*/?\s*>", re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")


def plain_text(value: str | None, limit: int = MAX_TEXT) -> str:
    if not value:
        return ""
    text = html.unescape(_TAGS.sub("", _BREAKS.sub("\n", value)))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if len(text) > limit:
        return text[:limit] + TRUNCATED_MARK
    return text


def as_html(text: str) -> str:
    """A model's plain text as the HTML Moodle stores for forum posts."""
    return html.escape(text, quote=False).replace("\n", "<br>")


def refusals(answer: Any, item_id: int) -> str:
    """Moodle's per-item refusals, joined by "; ".

    Update functions answer HTTP 200 with `warnings` for items they skipped;
    a warning about this id, or one with no id at all, means it was refused.
    """
    warnings = answer.get("warnings") if isinstance(answer, Mapping) else None
    messages = [
        str(w.get("message") or w.get("warningcode") or "refused")
        for w in warnings or []
        if isinstance(w, Mapping) and w.get("itemid") in (None, item_id, str(item_id))
    ]
    return "; ".join(messages)


def clamp_limit(value: Any) -> int:
    try:
        number = DEFAULT_LIMIT if value is None else int(value)
    except (TypeError, ValueError):
        number = DEFAULT_LIMIT
    return max(1, min(number, MAX_LIMIT))


def clamp_offset(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def page(items: Sequence[Any], args: Mapping[str, Any], total: int | None = None) -> dict[str, Any]:
    limit = clamp_limit(args.get("limit"))
    offset = clamp_offset(args.get("offset"))
    window = list(items[offset : offset + limit])
    count = len(items) if total is None else total
    return {
        "items": window,
        "total": count,
        "offset": offset,
        "truncated": offset + len(window) < count,
    }


def iso(timestamp: Any) -> str | None:
    """A Moodle Unix timestamp as ISO 8601 UTC; 0 (Moodle's "never") is None."""
    try:
        seconds = int(timestamp)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def to_timestamp(value: Any) -> int:
    """An ISO date or datetime as Moodle's Unix seconds (UTC when no zone)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            parsed = datetime(day.year, day.month, day.day, tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Not an ISO date: {value!r} (use e.g. 2026-09-01).") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def pick_one(args: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, Any]:
    given = [(key, args[key]) for key in keys if args.get(key) not in (None, "")]
    if len(given) != 1:
        raise ValueError(f"Give exactly one of: {', '.join(keys)}.")
    return given[0]


def has_role(raw_user: Mapping[str, Any], shortname: str) -> bool:
    return any(role.get("shortname") == shortname for role in raw_user.get("roles") or [])


def shape_user(raw: Mapping[str, Any]) -> dict[str, Any]:
    names = (raw.get("firstname"), raw.get("lastname"))
    user: dict[str, Any] = {
        "id": raw.get("id"),
        "fullname": raw.get("fullname") or " ".join(str(n) for n in names if n),
    }
    for key in ("email", "username"):
        if raw.get(key):
            user[key] = raw[key]
    if "suspended" in raw:
        user["suspended"] = bool(raw["suspended"])
    roles = [r["shortname"] for r in raw.get("roles") or [] if r.get("shortname")]
    if roles:
        user["roles"] = roles
    groups = [g["name"] for g in raw.get("groups") or [] if g.get("name")]
    if groups:
        user["groups"] = groups
    if "lastaccess" in raw:
        user["last_access"] = iso(raw["lastaccess"])
    if "lastcourseaccess" in raw:
        user["last_course_access"] = iso(raw["lastcourseaccess"])
    return user


def shape_course(raw: Mapping[str, Any]) -> dict[str, Any]:
    course: dict[str, Any] = {
        "id": raw.get("id"),
        "fullname": raw.get("fullname") or raw.get("displayname"),
        "shortname": raw.get("shortname"),
    }
    if raw.get("idnumber"):
        course["idnumber"] = raw["idnumber"]
    course["category_id"] = raw.get("categoryid", raw.get("category"))
    course["visible"] = bool(raw.get("visible", 1))
    course["start"] = iso(raw.get("startdate"))
    course["end"] = iso(raw.get("enddate"))
    course["summary"] = plain_text(raw.get("summary"), 500)
    return course
