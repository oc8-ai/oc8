"""`remove_tenant` deletes an organisation and every row, object and cache entry
it owns, and nothing of any other organisation's."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import defaultdict
from typing import Any

import pytest
import redis.asyncio as redis
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    Numeric,
    Table,
    Text,
    Uuid,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.config import get_settings
from oc8.db.base import Base
from oc8.models._mixins import TenantMixin
from oc8.runtime.states import RunState
from oc8.storage import s3
from oc8.tenants.provision import create_tenant
from oc8.tenants.removal import remove_tenant
from tests.tenants.conftest import OwnerSessionFactory

pytestmark = pytest.mark.asyncio

#: A table only this test creates. Its rows must go too, which shows that
#: removal finds tables by their `tenant_id` column rather than from a list.
_PROBE_TABLE = "removal_probe_extra"

#: Values for columns whose CHECK constraint a generic value cannot satisfy.
_OVERRIDES: dict[tuple[str, str], Any] = {
    ("trigger", "kind"): "cron",
    ("trigger", "cron_expression"): "0 * * * *",
    ("agent_run", "state"): RunState.QUEUED.value,
}

_ANY_ARRAY = re.compile(r"\(\(?(\w+)\)?(?:::text)? = ANY \(\(?ARRAY\['([^']*)'")


def _tenant_tables() -> list[Table]:
    tables = {
        mapper.local_table.name: mapper.local_table
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, TenantMixin) and isinstance(mapper.local_table, Table)
    }
    return [tables[name] for name in sorted(tables)]


async def _allowed_values(db: AsyncSession) -> dict[tuple[str, str], str]:
    """The first allowed value of every `col = ANY (ARRAY[...])` CHECK."""
    rows = (
        await db.execute(
            text(
                """
                SELECT cl.relname, pg_get_constraintdef(c.oid)
                FROM pg_constraint c JOIN pg_class cl ON cl.oid = c.conrelid
                WHERE c.contype = 'c' AND c.connamespace = current_schema()::regnamespace
                """
            )
        )
    ).all()
    out: dict[tuple[str, str], str] = {}
    for table, definition in rows:
        for col, value in _ANY_ARRAY.findall(definition):
            out.setdefault((table, col), value)
    return out


async def _foreign_keys(db: AsyncSession) -> dict[tuple[str, str], str]:
    """(child table, child column) -> parent table, for every FK column."""
    rows = (
        await db.execute(
            text(
                """
                SELECT child.relname, a.attname, parent.relname
                FROM pg_constraint c
                JOIN pg_class child ON child.oid = c.conrelid
                JOIN pg_class parent ON parent.oid = c.confrelid
                JOIN LATERAL unnest(c.conkey) AS k(attnum) ON true
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                WHERE c.contype = 'f' AND c.connamespace = current_schema()::regnamespace
                """
            )
        )
    ).all()
    return {(child, col): parent for child, col, parent in rows}


def _value_for(table: Table, column: Any, allowed: dict[tuple[str, str], str]) -> Any:
    key = (table.name, column.name)
    if key in _OVERRIDES:
        return _OVERRIDES[key]
    if key in allowed:
        return allowed[key]
    type_ = column.type
    if isinstance(type_, Uuid):
        return uuid.uuid4()
    if isinstance(type_, Boolean):
        return False
    if isinstance(type_, DateTime):
        return dt.datetime.now(tz=dt.UTC)
    if isinstance(type_, Date):
        return dt.date.today()
    if isinstance(type_, Integer | Float | Numeric):
        return 1
    if isinstance(type_, ARRAY):
        return []
    if isinstance(type_, JSONB | JSON):
        return {}
    if isinstance(type_, LargeBinary):
        return b"x"
    if isinstance(type_, Text) or type_.python_type is str:
        return f"v-{uuid.uuid4().hex[:12]}"
    raise AssertionError(f"no test value for {table.name}.{column.name} ({type_!r})")


async def _populate(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    """One row in every table that carries `TenantMixin`, parents first, each
    foreign key pointing at this tenant's own parent row."""
    allowed = await _allowed_values(db)
    fks = await _foreign_keys(db)
    tables = _tenant_tables()
    parents: dict[str, set[str]] = defaultdict(set)
    for (child, _col), parent in fks.items():
        if child != parent:
            parents[child].add(parent)

    inserted: dict[str, uuid.UUID] = {}
    remaining = list(tables)
    while remaining:
        ready = [t for t in remaining if parents[t.name] <= set(inserted) | {t.name}]
        assert ready, f"cannot order {[t.name for t in remaining]}"
        for table in ready:
            values: dict[str, Any] = {"tenant_id": tenant_id}
            for column in table.columns:
                if column.name == "tenant_id":
                    continue
                parent = fks.get((table.name, column.name))
                if parent is not None and parent in inserted:
                    values[column.name] = inserted[parent]
                    continue
                if (table.name, column.name) in _OVERRIDES:
                    values[column.name] = _OVERRIDES[(table.name, column.name)]
                    continue
                if column.nullable or column.server_default is not None:
                    continue
                if column.default is not None:
                    continue
                values[column.name] = _value_for(table, column, allowed)
            row_id = (
                await db.execute(insert(table).values(**values).returning(table.c.id))
            ).scalar_one()
            inserted[table.name] = row_id
            remaining.remove(table)


async def _rows_per_table(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    names = (
        (
            await db.execute(
                text(
                    """
                    SELECT c.table_name
                    FROM information_schema.columns c
                    JOIN information_schema.tables t
                      ON t.table_schema = c.table_schema AND t.table_name = c.table_name
                    WHERE c.table_schema = current_schema()
                      AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'
                    """
                )
            )
        )
        .scalars()
        .all()
    )
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = (
            await db.execute(
                text(f'SELECT count(*) FROM "{name}" WHERE tenant_id = :t'), {"t": tenant_id}
            )
        ).scalar_one()
    return counts


async def _new_tenant(owner_session: OwnerSessionFactory) -> uuid.UUID:
    async with owner_session() as db:
        created = await create_tenant(db, slug=f"rm-{uuid.uuid4().hex[:10]}", name="Removal")
        await _populate(db, created.tenant_id)
        await db.execute(
            text(f"INSERT INTO {_PROBE_TABLE} (tenant_id) VALUES (:t)"),
            {"t": created.tenant_id},
        )
    await s3.put_object(f"{created.tenant_id}/chat_message/a-hello.txt", b"hi", "text/plain")
    await s3.put_object(f"{created.tenant_id}/agent_run/b/c.txt", b"hi", "text/plain")
    client = redis.from_url(get_settings().redis_url)
    try:
        await client.set(f"deptcache:{created.tenant_id}:{uuid.uuid4()}:abc", "cached")
    finally:
        await client.aclose()
    return created.tenant_id


@pytest.fixture
async def probe_table(owner_session: OwnerSessionFactory) -> Any:
    async with owner_session() as db:
        await db.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {_PROBE_TABLE} "
                "(id bigserial PRIMARY KEY, tenant_id uuid NOT NULL)"
            )
        )
    yield _PROBE_TABLE
    async with owner_session() as db:
        await db.execute(text(f"DROP TABLE IF EXISTS {_PROBE_TABLE}"))


async def _keys(tenant_id: uuid.UUID) -> list[Any]:
    client = redis.from_url(get_settings().redis_url)
    try:
        return [k async for k in client.scan_iter(match=f"deptcache:{tenant_id}:*")]
    finally:
        await client.aclose()


async def _objects(tenant_id: uuid.UUID) -> list[str]:
    resp = s3._client().list_objects_v2(Bucket=s3._bucket(), Prefix=f"{tenant_id}/")
    return [o["Key"] for o in resp.get("Contents", [])]


async def test_removes_every_row_object_and_cache_entry_of_the_organisation(
    owner_session: OwnerSessionFactory,
    probe_table: str,
    minio_url: str,
    redis_url: str,
) -> None:
    tid = await _new_tenant(owner_session)
    async with owner_session() as db:
        before = await _rows_per_table(db, tid)
    # The population reached every TenantMixin table and the probe table.
    assert {t.name for t in _tenant_tables()} | {probe_table} <= {
        name for name, n in before.items() if n > 0
    }

    assert await remove_tenant(tid) is True

    async with owner_session() as db:
        after = await _rows_per_table(db, tid)
        org = await db.get(m.Organization, tid)
    assert {name: n for name, n in after.items() if n} == {}
    assert org is None
    assert await _objects(tid) == []
    assert await _keys(tid) == []


async def test_leaves_another_organisation_untouched(
    owner_session: OwnerSessionFactory,
    probe_table: str,
    minio_url: str,
    redis_url: str,
) -> None:
    doomed = await _new_tenant(owner_session)
    kept = await _new_tenant(owner_session)
    async with owner_session() as db:
        kept_before = await _rows_per_table(db, kept)

    assert await remove_tenant(doomed) is True

    async with owner_session() as db:
        assert await _rows_per_table(db, kept) == kept_before
        assert await db.get(m.Organization, kept) is not None
    assert len(await _objects(kept)) == 2
    assert len(await _keys(kept)) == 1


async def test_a_second_call_returns_false(
    owner_session: OwnerSessionFactory,
    probe_table: str,
    minio_url: str,
    redis_url: str,
) -> None:
    tid = await _new_tenant(owner_session)
    assert await remove_tenant(tid) is True
    assert await remove_tenant(tid) is False


async def test_an_unknown_organisation_returns_false(
    minio_url: str,
    redis_url: str,
) -> None:
    assert await remove_tenant(uuid.uuid4()) is False


async def test_active_runs_are_cancelled_before_the_rows_go(
    owner_session: OwnerSessionFactory,
    minio_url: str,
    redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every run that has not finished gets the same run_cancellation row the
    cancel endpoint writes, committed before anything is deleted, so a running
    executor stops at its next step boundary instead of holding its row."""
    from oc8.tenants import removal

    async with owner_session() as db:
        created = await create_tenant(db, slug=f"rm-{uuid.uuid4().hex[:10]}", name="Runs")
        tid = created.tenant_id
        agent_id = uuid.uuid4()
        runs = {}
        for state in (RunState.QUEUED, RunState.RUNNING, RunState.DONE):
            run_id = uuid.uuid4()
            runs[state] = run_id
            db.add(
                m.AgentRun(
                    id=run_id,
                    tenant_id=tid,
                    agent_id=agent_id,
                    state=state.value,
                    context={},
                )
            )

    seen: dict[uuid.UUID, str] = {}
    real_sweep = removal._delete_tenant_rows

    async def _spy(tenant_id: uuid.UUID) -> int:
        async with owner_session() as db:
            rows = (
                await db.execute(
                    select(m.RunCancellation.run_id, m.RunCancellation.cancellation_kind).where(
                        m.RunCancellation.tenant_id == tenant_id
                    )
                )
            ).all()
        seen.update({run_id: kind for run_id, kind in rows})
        return await real_sweep(tenant_id)

    monkeypatch.setattr(removal, "_delete_tenant_rows", _spy)

    assert await remove_tenant(tid) is True
    assert seen == {
        runs[RunState.QUEUED]: "tenant_inactive",
        runs[RunState.RUNNING]: "tenant_inactive",
    }
