"""Concurrency-safe writes to ``agent_run.context``.

Two writers share that JSONB column: the executor records a run's outcome, and
the approvals endpoint records an operator's decision on a run that is still
going. Both used to read it into Python, merge, and write the whole object back
-- a read-modify-write with no lock in between, so whichever committed second
erased the other's key. The decision is the one that matters: the operator sees
"approved" and the action never happens, rarely and unreproducibly.

The fix is to stop reading it into Python at all. Each function here merges
inside ONE statement, so Postgres' own row locking serialises them: under READ
COMMITTED a second UPDATE waits for the first to commit and then re-evaluates
its expression against the NEW row. Neither writer needs to know the other
exists, which is the property an explicit lock would not have given us -- both
sides would have had to remember to take it.

`run_cancellation` and `run_message` avoid the same hazard by being side tables.
This column predates that pattern and is read by too many callers to move now.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from oc8.models.run import AgentRun

_MERGE = (
    text("UPDATE agent_run SET context = context || :patch WHERE id = :id RETURNING context")
    .bindparams(bindparam("patch", type_=JSONB))
)

#: `coalesce` because the key is absent until the first decision, and `||` on a
#: NULL jsonb yields NULL -- which would wipe the whole column rather than
#: create the list.
_APPEND = text(
    "UPDATE agent_run SET context = jsonb_set("
    "  context, '{resolved_tool_approvals}',"
    "  coalesce(context->'resolved_tool_approvals', '[]'::jsonb) || :entry"
    ") WHERE id = :id RETURNING context"
).bindparams(bindparam("entry", type_=JSONB))

#: Same append-not-overwrite shape as _APPEND above, targeting `toolCalls`
#: instead of `resolved_tool_approvals`. Lets a live run's tool calls become
#: visible (to a page reload, not just an open WS tab) one at a time as the
#: engine's step loop executes them, instead of only once at the very end
#: when executor.py's own merge_context({"toolCalls": result.tool_calls, ...})
#: writes the whole list in one shot. That final write stays authoritative
#: (it also carries "output"/"steps", which this call never touches) -- these
#: incremental appends just mean it's no longer the FIRST time toolCalls ever
#: appears in the row.
_APPEND_TOOL_CALL = text(
    "UPDATE agent_run SET context = jsonb_set("
    "  context, '{toolCalls}',"
    "  coalesce(context->'toolCalls', '[]'::jsonb) || :entry"
    ") WHERE id = :id RETURNING context"
).bindparams(bindparam("entry", type_=JSONB))


def _adopt(run: AgentRun, context: dict[str, Any]) -> dict[str, Any]:
    """Put the stored value on the instance WITHOUT marking it dirty.

    A plain assignment would queue an ORM flush that writes the whole object
    back -- reintroducing exactly the read-modify-write this module exists to
    remove, just with a shorter window.
    """
    set_committed_value(run, "context", context)
    return context


async def merge_context(db: AsyncSession, run: AgentRun, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the run's context, keeping every key it does not name."""
    stored = (await db.execute(_MERGE, {"patch": patch, "id": run.id})).scalar_one()
    return _adopt(run, stored)


async def append_resolved_approval(
    db: AsyncSession, run: AgentRun, entry: dict[str, Any]
) -> dict[str, Any]:
    """Append one decision to `resolved_tool_approvals`.

    An append, not a whole-list write: two operators deciding two held calls at
    the same moment would otherwise each write a one-element list built from
    their own snapshot, and the run would resume having forgotten one of them.
    """
    stored = (await db.execute(_APPEND, {"entry": [entry], "id": run.id})).scalar_one()
    return _adopt(run, stored)


async def append_tool_call(
    db: AsyncSession, run: AgentRun, entry: dict[str, Any]
) -> dict[str, Any]:
    """Append one tool-call trace entry to `toolCalls` as the engine's step
    loop executes it, so it's visible to a fresh page load mid-run -- not
    just the one whole-list write executor.py still makes when the run ends.
    Same shape as `append_resolved_approval` and for the same reason: many
    calls happen across one run, and a read-modify-write in Python would
    lose one under concurrent writers to this row (a live steering message,
    an approval decision) exactly like resolved_tool_approvals's docstring
    describes.
    """
    stored = (await db.execute(_APPEND_TOOL_CALL, {"entry": [entry], "id": run.id})).scalar_one()
    return _adopt(run, stored)


# C0/C1 controls plus line/paragraph separators and bidi overrides/isolates,
# which could visually reorder a chip.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]+")


def clean_suggestions(raw: list[Any]) -> list[str]:
    """Model-written, untrusted: control characters out, trimmed, empties
    dropped, non-strings skipped, 120 characters each, three at most."""
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        chip = _CONTROL_CHARS.sub(" ", item).strip()[:120].strip()
        if chip:
            out.append(chip)
    return out[:3]
