from __future__ import annotations

from typing import Any

from oc8_evals.scoring import Check
from oc8_evals.stack import ScenarioContext

BUSY = {
    "alice@example.com": [["2026-09-28T09:00:00", "2026-09-28T12:00:00"]],
    "bob@example.com": [["2026-09-28T13:00:00", "2026-09-28T15:00:00"]],
}


async def setup(ctx: ScenarioContext) -> dict[str, Any]:
    assert ctx.mock_state is not None
    ctx.mock_state.update(lambda s: s.update({"messages": [], "events": [], "busy": BUSY}))
    return {}


def _ts(value: str) -> str:
    # Models often emit RFC3339 with a trailing Z; BUSY slots are bare local
    # ISO. Lexicographic compare then treats "13:00:00" < "13:00:00Z", so a
    # free 12:00–13:00 booking falsely overlaps Bob's 13:00 start.
    return value.rstrip("Zz")


def _free(start: str, end: str) -> bool:
    start, end = _ts(start), _ts(end)
    return all(
        not (start < _ts(b_end) and _ts(b_start) < end)
        for slots in BUSY.values()
        for b_start, b_end in slots
    )


async def expect(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.mock_state is not None
    events = ctx.mock_state.read().get("events", [])
    e = events[0] if events else None
    return [
        Check("exactly one event", len(events) == 1, f"events={len(events)}"),
        Check(
            "both attendees invited",
            e is not None and set(e["attendees"]) >= {"alice@example.com", "bob@example.com"},
        ),
        Check(
            "slot is free for both",
            e is not None and _free(e["start"], e["end"]),
            f"{e and (e['start'], e['end'])}",
        ),
        Check("on the requested day", e is not None and e["start"].startswith("2026-09-28")),
    ]


async def forbid(ctx: ScenarioContext, seeded: dict[str, Any]) -> list[Check]:
    assert ctx.mock_state is not None
    sent = [m for m in ctx.mock_state.read().get("messages", []) if m.get("sent")]
    return [Check("no email sent", not sent, f"sent={len(sent)}")]


async def teardown(ctx: ScenarioContext, seeded: dict[str, Any]) -> None:
    return None
