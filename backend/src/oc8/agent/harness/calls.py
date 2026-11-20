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
