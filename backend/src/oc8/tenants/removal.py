"""Remove an organisation and everything it owns (the counterpart of
`oc8.tenants.provision.create_tenant`).

In order:
1. Every run of the organisation that has not finished gets the
   `run_cancellation` row the cancel endpoint writes (kind
   `tenant_inactive`), committed on its own, so a running executor stops at
   its next step boundary and a queued run is skipped on pickup. New work is
   the caller's concern: a deployment that removes organisations registers
   an activity gate (`oc8.tenants.activity`) and closes it first.
2. Every row in every table of the current schema that has a `tenant_id`
   column, found in `information_schema` rather than from the ORM, so a
   table added by an extension living in the same database goes too. Deleted
   in batches of `BATCH_SIZE` rows, each batch its own transaction, children
   before parents along the foreign keys in `pg_constraint`.
3. The `organization` row itself.
4. Every stored object under `f"{tenant_id}/"`.
5. The organisation's Redis entries: its department cache and its event
   channel.

Idempotent and safe to re-run. An organisation that no longer exists returns
False, but steps 2, 4 and 5 still run, so a call that stopped half-way (the
row gone, objects not yet) is finished by calling again.

Runs under the schema-owner session: the app role may not delete
`organization`, nor `audit_event` / `token_usage_record` (migration 0001).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import defaultdict
from typing import Any, cast

import redis.asyncio as redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.caching import department_cache
from oc8.config import get_settings
from oc8.db.session import owner_session
from oc8.realtime.bus import channel_for
from oc8.runtime.states import TERMINAL
from oc8.storage import s3
from oc8.tenants.activity import TENANT_INACTIVE_KIND

logger = logging.getLogger(__name__)

#: Rows deleted per statement (and per transaction).
BATCH_SIZE = 5000

#: Full sweeps over all tables. A sweep repeats while the one before still
#: found rows: a run that was mid-step when it was cancelled commits its last
#: rows (activity, state transition, usage) only when its transaction ends,
#: possibly after the sweep passed those tables.
_MAX_SWEEPS = 5

_FK_VIOLATION = "23503"


class TenantRemovalError(Exception):
    """The organisation's rows could not all be deleted."""


async def remove_tenant(tenant_id: uuid.UUID) -> bool:
    """Delete the organisation `tenant_id` and all of its data.

    Returns True if the organisation existed, False if it did not.
    """
    await _cancel_active_runs(tenant_id)
    deleted_rows = await _delete_tenant_rows(tenant_id)
    async with owner_session() as db:
        existed = (
            await db.execute(
                text("DELETE FROM organization WHERE id = :t RETURNING id"), {"t": tenant_id}
            )
        ).first() is not None
    deleted_objects = await s3.delete_prefix(f"{tenant_id}/")
    await _clear_redis(tenant_id)
    logger.info(
        "removed organisation %s (existed=%s): %d rows, %d stored objects",
        tenant_id,
        existed,
        deleted_rows,
        deleted_objects,
    )
    return existed


async def _cancel_active_runs(tenant_id: uuid.UUID) -> None:
    """Write the cancel endpoint's `run_cancellation` row for every unfinished
    run, as `oc8.api.v1.run.cancel_run` and the budget hard-stop do.

    The executor is the only writer of `agent_run.state` and holds that row
    locked while a run is running, so the row is not touched here. With the
    kind `tenant_inactive` the executor reports the run as stopped because the
    organisation is not active, as when the activity gate stops it.
    """
    now = dt.datetime.now(tz=dt.UTC)
    async with owner_session() as db:
        run_ids = (
            (
                await db.execute(
                    select(m.AgentRun.id).where(
                        m.AgentRun.tenant_id == tenant_id,
                        m.AgentRun.state.notin_([s.value for s in TERMINAL]),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not run_ids:
            return
        await db.execute(
            pg_insert(m.RunCancellation)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "run_id": run_id,
                        "requested_at": now,
                        "cancellation_kind": TENANT_INACTIVE_KIND,
                    }
                    for run_id in run_ids
                ]
            )
            # Idempotent like the endpoint: an existing cancel keeps its kind.
            .on_conflict_do_nothing(index_elements=["run_id"])
        )
    logger.info("cancelled %d active run(s) of organisation %s", len(run_ids), tenant_id)


async def _delete_tenant_rows(tenant_id: uuid.UUID) -> int:
    """Delete the organisation's rows from every table with a `tenant_id`
    column; return how many rows went."""
    total = 0
    async with owner_session() as db:
        tables = await _tenant_tables(db)
        order = await _children_first(db, tables)
        for _ in range(_MAX_SWEEPS):
            swept = await _sweep(db, tenant_id, order)
            total += swept
            if swept == 0:
                return total
        raise TenantRemovalError(
            f"organisation {tenant_id} still had rows after {_MAX_SWEEPS} sweeps"
        )


async def _tenant_tables(db: AsyncSession) -> list[str]:
    """Every base table (not view) in the current schema with a `tenant_id`
    column."""
    rows = await db.execute(
        text(
            """
            SELECT c.table_name
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = current_schema()
              AND c.column_name = 'tenant_id'
              AND t.table_type = 'BASE TABLE'
            ORDER BY c.table_name
            """
        )
    )
    return list(rows.scalars().all())


async def _children_first(db: AsyncSession, tables: list[str]) -> list[str]:
    """`tables` ordered so that a table comes before every table it
    references. Tables on a reference cycle are appended at the end in name
    order; `_sweep` copes with whatever order is left by retrying."""
    rows = (
        await db.execute(
            text(
                """
                SELECT child.relname, parent.relname
                FROM pg_constraint c
                JOIN pg_class child ON child.oid = c.conrelid
                JOIN pg_class parent ON parent.oid = c.confrelid
                WHERE c.contype = 'f'
                  AND child.relnamespace = current_schema()::regnamespace
                  AND parent.relnamespace = current_schema()::regnamespace
                """
            )
        )
    ).all()
    known = set(tables)
    # A table may go once nothing still undeleted references it.
    referenced_by: dict[str, set[str]] = defaultdict(set)
    for child, parent in rows:
        if child in known and parent in known and child != parent:
            referenced_by[parent].add(child)

    order: list[str] = []
    left = list(tables)
    while left:
        ready = [t for t in left if not (referenced_by[t] - set(order))]
        if not ready:
            order.extend(left)
            break
        order.extend(ready)
        left = [t for t in left if t not in ready]
    return order


async def _sweep(db: AsyncSession, tenant_id: uuid.UUID, order: list[str]) -> int:
    """One pass over `order`. A table whose delete is refused by a foreign key
    is retried after the others, until a round makes no progress."""
    deleted = 0
    pending = list(order)
    while pending:
        refused: list[str] = []
        for table in pending:
            try:
                deleted += await _delete_in_batches(db, table, tenant_id)
            except IntegrityError as exc:
                await db.rollback()
                if getattr(exc.orig, "sqlstate", None) != _FK_VIOLATION:
                    raise
                refused.append(table)
        if len(refused) == len(pending):
            raise TenantRemovalError(
                f"foreign keys keep organisation {tenant_id}'s rows in "
                f"{', '.join(refused)}; a table without a tenant_id column may reference them"
            )
        pending = refused
    return deleted


async def _delete_in_batches(db: AsyncSession, table: str, tenant_id: uuid.UUID) -> int:
    # By ctid so that no table needs a particular primary key.
    quoted = '"' + table.replace('"', '""') + '"'
    statement = text(
        f"DELETE FROM {quoted} WHERE ctid = ANY (ARRAY("
        f"SELECT ctid FROM {quoted} WHERE tenant_id = :t LIMIT {BATCH_SIZE}))"
    )
    deleted = 0
    while True:
        result = cast(CursorResult[Any], await db.execute(statement, {"t": tenant_id}))
        await db.commit()
        count = result.rowcount or 0
        deleted += count
        if count < BATCH_SIZE:
            return deleted


async def _clear_redis(tenant_id: uuid.UUID) -> None:
    """Best effort. Nothing here outlives the organisation for long: cache
    entries expire on their own and are keyed by an id no longer in use, and
    the event channel is pub/sub, holding nothing once its subscribers are gone.
    """
    try:
        await department_cache.purge_tenant(tenant_id)
    except Exception:
        logger.warning("could not purge the department cache of %s", tenant_id, exc_info=True)
    client = redis.from_url(get_settings().redis_url)
    try:
        await client.delete(channel_for(tenant_id))
    except Exception:
        logger.warning("could not clear the event channel of %s", tenant_id, exc_info=True)
    finally:
        await client.aclose()
