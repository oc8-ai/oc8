"""Shared helpers for the audit tests."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.audit.chain import _write_staged_now, append_event
from oc8.models.ops import AuditEvent


async def flush_audit(session: AsyncSession) -> None:
    """Write the session's staged audit events to the chain now.

    Test-only: production code lets the commit write them (see
    `oc8.audit.chain`). Refused inside a savepoint.
    """
    await session.run_sync(_write_staged_now)


async def append_now(session: AsyncSession, **kwargs: Any) -> AuditEvent:
    """Append an event and write it to the chain at once.

    `append_event` only stages; the chain is written when the transaction
    commits. Tests that read their own events back inside the transaction that
    appended them (verifiers, checkpoints, hash fixtures) use this instead.
    """
    ev = await append_event(session, **kwargs)
    await flush_audit(session)
    return ev
