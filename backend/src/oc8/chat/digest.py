"""A compact record of what a run did, stored on its assistant message.

One line per tool call (`tool args → outcome: preview`), capped, so a later
turn can be replayed to the model with the work behind an earlier answer
without carrying the tool results themselves. A restricted run keeps tool
names and outcomes only: arguments and previews may hold restricted data.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Final

from oc8.runtime.fencing import one_line

DIGEST_LIMIT: Final = 1500
ARGS_LIMIT: Final = 200
PREVIEW_LIMIT: Final = 300

_OUTCOME: Final = {
    "done": "ok",
    "denied": "denied",
    "failed": "error",
    "awaiting_approval": "waiting",
}
_SPILLED: Final = re.compile(r'kept as file "([^"]+)"')


def _marker(hidden: int) -> str:
    return f"… (+{hidden} more calls)"


def _line(call: dict[str, Any], *, run_id: uuid.UUID, restricted: bool) -> str:
    outcome = _OUTCOME.get(str(call.get("state")), str(call.get("state") or "unknown"))
    if restricted:
        return f"{call.get('tool')} → {outcome}"
    args = one_line(
        json.dumps(call.get("arguments") or {}, ensure_ascii=False, sort_keys=True, default=str),
        ARGS_LIMIT,
    )
    line = f"{call.get('tool')} {args} → {outcome}"
    result = str(call.get("result") or "")
    if outcome not in ("ok", "error") or not result:
        return line
    spilled = _SPILLED.search(result)
    if spilled:
        name = spilled.group(1)
        preview = f'file "{name}" — read_run_file(filename="{name}", run_id="{run_id}")'
    else:
        preview = one_line(result, PREVIEW_LIMIT)
    return f"{line}: {preview}" if preview else line


def build_work_digest(
    tool_calls: list[dict[str, Any]], *, run_id: uuid.UUID, restricted: bool
) -> str | None:
    """The digest of `tool_calls` in call order, or None when there are none."""
    if not tool_calls:
        return None
    lines = [_line(c, run_id=run_id, restricted=restricted) for c in tool_calls]
    kept: list[str] = []
    size = 0
    for index, line in enumerate(lines):
        # Reserve room for the marker, in case this is not the last line.
        reserve = len(_marker(len(lines) - index)) + 1
        if size + len(line) + (1 if kept else 0) > DIGEST_LIMIT - (
            reserve if index < len(lines) - 1 else 0
        ):
            break
        size += len(line) + (1 if kept else 0)
        kept.append(line)
    if len(kept) < len(lines):
        kept.append(_marker(len(lines) - len(kept)))
    return "\n".join(kept)
