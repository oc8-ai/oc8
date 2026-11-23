"""Call identity shared by every stage that needs to say "the same call
again" -- the repeat-call tracker, the approval-resume matcher and the
idempotency stage must agree on what identical means."""

from __future__ import annotations

import json

from oc8.modelrouter import ToolCall


def call_sig(tc: ToolCall) -> str:
    """Stable signature of a tool call, so an approval decided on a suspended run
    can be matched to the same call when the run resumes and replays it."""
    return tc.name + "\n" + json.dumps(tc.arguments, sort_keys=True, default=str)


def unique_call_ids(calls: list[ToolCall], taken: set[str]) -> None:
    """Give every call an id no other call in `taken` has, in place, and add
    it there. A model's call ids are not unique: one reused for a different
    call would ride on what was booked against the first (an approval's
    claim, a local tool's go-ahead). The renamed id is what the model then
    sees with the call and its result, so the transcript stays consistent."""
    for tc in calls:
        if tc.id and tc.id not in taken:
            taken.add(tc.id)
            continue
        base = tc.id or "call"
        n = 1
        while f"{base}_dup{n}" in taken:
            n += 1
        tc.id = f"{base}_dup{n}"
        taken.add(tc.id)
