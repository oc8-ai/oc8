"""Making a repeated side-effectful tool call safe (§8.7 R5).

A prerequisite for a self-driving agent runtime rather than a refinement. Without
checkpoints, a killed container restarts its task from the beginning; for an agent
that writes to an external system, "from the beginning" means doing the write
again. Three identical Odoo quotes came out of exactly that kind of repetition.

Two deliberate limits:

- **Reads are not recorded.** Deduplicating a search would hide changes the agent
  is supposed to see -- the whole point of reading twice is that the answer may
  differ. This holds only as far as the CONNECTION says which of its tools are
  reads: `required_right` fail-closes an unlisted tool to `write`, so a
  connection with no declared scopes has every read cached and replayed here,
  which is the opposite of this rule. That is not hypothetical -- see
  `warn_unclassified_connection`, which exists because it happened.
- **A replay is announced, not disguised.** The stored result comes back with a
  note saying no second action was taken. Returning it silently would let the
  agent report having created a second quote that does not exist, which is a
  worse failure than the duplicate it prevents.

The trade-off this accepts: two *intentionally* identical writes inside one task
collapse into one. That is rare and usually a bug, and the announcement makes it
visible rather than silent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m

logger = logging.getLogger(__name__)

REPLAY_NOTE = (
    "[replay: an identical call was already executed for this task; "
    "the earlier result is returned and NO second action was taken]"
)

#: Connections already complained about, so the warning below is one line per
#: connection per process rather than one per tool call.
_UNCLASSIFIED: set[uuid.UUID] = set()


def warn_unclassified_connection(connection_id: uuid.UUID, name: str) -> None:
    """Say out loud that a connection declares no read/write classification.

    `required_right` fail-closes an unlisted tool to `write`, which is right for
    AUTHORIZATION -- an unknown tool should need the higher right. For
    IDEMPOTENCY it inverts this module's own rule: every read then gets recorded
    AND replayed, so a second identical search inside one task is answered from
    the first one's result, with a note claiming no second action was taken. The
    agent is told the world has not changed. That is the failure this module
    exists to avoid, arriving through the door marked "safe default".

    Seen live 2026-07-30: a connection created before its plugin declared its
    scopes kept `[]` afterwards -- plugin manifests do not reach rows that
    already exist -- and its reads were still being cached three days later.
    Nothing anywhere said so, which is the only reason it lasted that long.

    Not raised and not blocked: the deployment is working, and the fix (re-apply
    the plugin so the connection gets its declared scopes) belongs to an
    operator, not to a tool call in flight.
    """
    if connection_id in _UNCLASSIFIED:
        return
    _UNCLASSIFIED.add(connection_id)
    logger.warning(
        "connection %s (%s) declares no read/write scopes: every tool on it "
        "counts as a write, so its READS are cached and replayed within a task "
        "-- a repeated search will return the first result and report that "
        "nothing was done. Re-apply the plugin that owns this connection.",
        name,
        connection_id,
    )


def args_fingerprint(arguments: Mapping[str, Any]) -> str:
    """Order-insensitive hash of a call's arguments.

    A harness may serialise the same arguments differently between attempts, so
    key order must not make it look like a different call.
    """
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def replayed_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    tool: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """The earlier result for this exact call, or None if it has not run yet."""
    row = (
        await db.execute(
            select(m.ToolInvocation).where(
                m.ToolInvocation.tenant_id == tenant_id,
                m.ToolInvocation.task_id == task_id,
                m.ToolInvocation.tool == tool,
                m.ToolInvocation.args_hash == args_fingerprint(arguments),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return f"{row.result}\n\n{REPLAY_NOTE}"


async def record_invocation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    tool: str,
    arguments: Mapping[str, Any],
    result: str,
) -> None:
    """Remember that this call ran, so a repeat can be answered from it.

    A losing race is swallowed: two containers can attempt the same call after a
    restart, and the second writer must not fail the run with a unique violation.
    The first result stands, because that is the one that actually happened.
    """
    # ON CONFLICT DO NOTHING rather than catching IntegrityError: a failed flush
    # marks the whole session as needing rollback, so the caller's next statement
    # dies with PendingRollbackError -- turning a harmless race into a failed run,
    # the exact opposite of the point. Letting the database decide keeps
    # "first writer wins" without any exception crossing the session at all.
    await db.execute(
        pg_insert(m.ToolInvocation)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            task_id=task_id,
            tool=tool,
            args_hash=args_fingerprint(arguments),
            result=result,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "task_id", "tool", "args_hash"])
    )


async def claim_invocation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    tool: str,
    arguments: Mapping[str, Any],
    result: str,
) -> bool:
    """`record_invocation` that says whether THIS request wrote the row:
    `INSERT ... ON CONFLICT DO NOTHING RETURNING`. A concurrent insert of the
    same key blocks on the unique index until the first transaction ends,
    then returns nothing -- so exactly one claimant wins."""
    won = (
        await db.execute(
            pg_insert(m.ToolInvocation)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                task_id=task_id,
                tool=tool,
                args_hash=args_fingerprint(arguments),
                result=result,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "task_id", "tool", "args_hash"])
            .returning(m.ToolInvocation.id)
        )
    ).scalar_one_or_none()
    return won is not None
