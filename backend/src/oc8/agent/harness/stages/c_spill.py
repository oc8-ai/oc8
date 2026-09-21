"""C1 spill (spec §6 C1) — replace the hard cut with a file + head/tail preview.

Pure text helpers live here. Persistence is `persist_spill` (Task 2) using
`store_attachment_bytes`; Harness.shape never opens a DB session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SPILL_THRESHOLD_CHARS = 8_000
SPILL_HEAD_CHARS = 3_000
SPILL_TAIL_CHARS = 800
_TOOL_FILENAME = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Spill:
    filename: str
    content: str


def spill_filename(step_no: int, tool: str) -> str:
    safe = _TOOL_FILENAME.sub("_", tool).strip("_") or "tool"
    return f"step-{step_no}-{safe}.txt"


def spill_preview(tool: str, body: str, filename: str) -> str:
    n = len(body)
    lines = body.count("\n") + 1 if body else 0
    omitted = n - SPILL_HEAD_CHARS - SPILL_TAIL_CHARS
    head = body[:SPILL_HEAD_CHARS]
    tail = body[-SPILL_TAIL_CHARS:]
    return (
        f'[{tool} returned {n} chars ({lines} lines); kept as file "{filename}".\n'
        f"Showing first {SPILL_HEAD_CHARS} and last {SPILL_TAIL_CHARS} chars. "
        f"Use read_run_file(filename, offset, limit) to read more, or process "
        f"the file with run_program/run_shell.]\n"
        f"{head}\n"
        f"… {omitted} chars omitted …\n"
        f"{tail}"
    )


def maybe_spill(tool: str, body: str, *, step_no: int) -> tuple[str, Spill | None]:
    if len(body) <= SPILL_THRESHOLD_CHARS:
        return body, None
    filename = spill_filename(step_no, tool)
    return spill_preview(tool, body, filename), Spill(filename=filename, content=body)
