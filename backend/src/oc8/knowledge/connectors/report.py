"""What a connector tells a sync about what it left out, and why.

A connector calls `report_skip("unsupported_type")` for a file it could not take
and `report_note("api_key_recommended")` for something an operator should know.
`run_source_sync` installs a fresh collector around the connector's `fetch` and
reads it afterwards.

The report lives in a ContextVar and never on the connector. Plugin connector
instances are process-global: one object serves every tenant and every
concurrent sync, so any per-sync state kept on it would leak between them. An
async generator runs in the context of the task iterating it, so each sync (one
task each) sees only its own collector. Outside a sync both calls do nothing,
and nothing here may ever fail a sync.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from dataclasses import dataclass, field

MAX_TEXT = 64
MAX_NOTES = 10
#: Reasons are free strings; bound how many distinct ones one sync can store so a
#: connector that interpolates something variable cannot bloat job.stats.
MAX_REASONS = 20
OTHER = "other"


@dataclass
class SyncReport:
    skipped: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        key = str(reason)[:MAX_TEXT]
        if key not in self.skipped and len(self.skipped) >= MAX_REASONS:
            key = OTHER
        self.skipped[key] = self.skipped.get(key, 0) + 1

    def note(self, code: str) -> None:
        text = str(code)[:MAX_TEXT]
        if text not in self.notes and len(self.notes) < MAX_NOTES:
            self.notes.append(text)


_current: contextvars.ContextVar[SyncReport | None] = contextvars.ContextVar(
    "oc8_sync_report", default=None
)


def report_skip(reason: str) -> None:
    """Count one document left out for `reason`. A no-op outside a sync."""
    try:
        report = _current.get()
        if report is not None:
            report.skip(reason)
    except Exception:
        pass


def report_note(code: str) -> None:
    """Record a note code for the operator. A no-op outside a sync."""
    try:
        report = _current.get()
        if report is not None:
            report.note(code)
    except Exception:
        pass


@contextlib.contextmanager
def collect_sync_report() -> Iterator[SyncReport]:
    """Install a fresh collector for the enclosed sync and yield it."""
    report = SyncReport()
    token = _current.set(report)
    try:
        yield report
    finally:
        _current.reset(token)
