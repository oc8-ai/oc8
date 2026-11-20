"""The tenant chain is appended at COMMIT, not while the transaction runs.

`append_event` used to take `pg_advisory_xact_lock(tenant)` on the spot, and
that lock is transaction-scoped: an in-process agent run (one long transaction)
held it from its first audited tool call until it ended, so every other audited
write in the tenant -- other runs' tool calls, approvals, logins -- queued
behind it. `append_event` now only stages the event on the session; the
`before_commit` hook in `oc8.audit.chain` takes the lock, reads the head,
hashes and inserts the staged events in order, inside the committing
transaction. These tests pin that contract.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from tests.audit.helpers import flush_audit
from tests.conftest import AppSessionFactory

from oc8 import models as m
from oc8.audit.chain import AuditChainPoisoned, append_event, verify_chain
from oc8.constants import ACME_TENANT_ID
from oc8.db.session import owner_session
from oc8.modelrouter import CompletionResult, ToolCall, Usage, chunk_from_result

pytestmark = pytest.mark.asyncio

# Long enough for a healthy run on a loaded CI box, short enough that the old
# behaviour (blocking until the other transaction ends) fails instead of hanging.
NOT_BLOCKED = 5.0


async def _append(s: AsyncSession, tenant: uuid.UUID, action: str) -> m.AuditEvent:
    return await append_event(
        s, tenant_id=tenant, actor_type="system", actor_id=None, category="test", action=action
    )


async def _actions(app_session: AppSessionFactory, tenant: uuid.UUID) -> list[str]:
    async with app_session(tenant) as s:
        rows = await s.execute(
            sa.select(m.AuditEvent.action)
            .where(m.AuditEvent.tenant_id == tenant)
            .order_by(m.AuditEvent.seq.asc())
        )
        return list(rows.scalars().all())


async def _verified(app_session: AppSessionFactory, tenant: uuid.UUID) -> bool:
    async with app_session(tenant) as s:
        return await verify_chain(s, tenant)


async def test_concurrent_transactions_do_not_block_each_other_before_commit(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s1:
        await _append(s1, tenant, "first")
        # s1 is still open (its "slow work" is everything below). A second
        # transaction in the same tenant must be able to append and keep
        # working without waiting for s1 to end.
        async with app_session(tenant) as s2:
            await asyncio.wait_for(_append(s2, tenant, "second"), NOT_BLOCKED)
            await asyncio.wait_for(s2.execute(sa.text("SELECT 1")), NOT_BLOCKED)
        # s2 committed while s1 is still open.
    assert await _actions(app_session, tenant) == ["second", "first"]
    assert await _verified(app_session, tenant) is True


async def test_staged_event_is_not_visible_before_commit_and_lands_on_commit(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        ev = await _append(s, tenant, "staged")
        assert ev.seq is None and ev.hash is None
        await s.commit()
        assert ev.seq is not None and ev.hash is not None
    assert await _actions(app_session, tenant) == ["staged"]


async def test_rolled_back_transaction_writes_no_event(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    with pytest.raises(RuntimeError):
        async with app_session(tenant) as s:
            await _append(s, tenant, "doomed")
            raise RuntimeError("business write failed")
    async with app_session(tenant) as s:
        await _append(s, tenant, "kept")
    assert await _actions(app_session, tenant) == ["kept"]
    assert await _verified(app_session, tenant) is True


async def test_explicit_rollback_then_commit_in_same_session(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _append(s, tenant, "rolled-back")
        await s.rollback()
        await s.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)})
        await _append(s, tenant, "committed")
    assert await _actions(app_session, tenant) == ["committed"]


async def test_savepoint_rollback_discards_its_events(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _append(s, tenant, "outer.before")
        with pytest.raises(RuntimeError):
            async with s.begin_nested():
                await _append(s, tenant, "inner.rolled-back")
                async with s.begin_nested():
                    await _append(s, tenant, "inner.inner.released")
                raise RuntimeError("savepoint fails")
        async with s.begin_nested():
            await _append(s, tenant, "inner.released")
        await _append(s, tenant, "outer.after")
    assert await _actions(app_session, tenant) == [
        "outer.before",
        "inner.released",
        "outer.after",
    ]
    assert await _verified(app_session, tenant) is True


async def test_releasing_a_savepoint_does_not_write_the_chain(
    app_session: AppSessionFactory,
) -> None:
    """A RELEASE SAVEPOINT is not the commit: the lock must still not be taken."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s1:
        async with s1.begin_nested():
            await _append(s1, tenant, "in-savepoint")
        async with app_session(tenant) as s2:
            await asyncio.wait_for(_append(s2, tenant, "other"), NOT_BLOCKED)
            await asyncio.wait_for(s2.commit(), NOT_BLOCKED)
    assert await _actions(app_session, tenant) == ["other", "in-savepoint"]


async def test_order_within_one_transaction_is_preserved(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        for i in range(5):
            await _append(s, tenant, f"n.{i}")
    assert await _actions(app_session, tenant) == [f"n.{i}" for i in range(5)]
    assert await _verified(app_session, tenant) is True


async def test_flush_audit_writes_now_and_is_refused_inside_a_savepoint(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        ev = await _append(s, tenant, "flushed")
        async with s.begin_nested():
            with pytest.raises(RuntimeError):
                await flush_audit(s)
        await flush_audit(s)
        assert ev.seq is not None and ev.hash is not None
        assert await verify_chain(s, tenant) is True
        await _append(s, tenant, "at-commit")
    assert await _actions(app_session, tenant) == ["flushed", "at-commit"]
    assert await _verified(app_session, tenant) is True


async def test_several_tenants_in_one_session_lock_in_a_stable_order() -> None:
    """Two sessions that stage events for the same two tenants in opposite order
    must not deadlock: the hook locks tenants sorted, not in staging order."""
    a, b = uuid.uuid4(), uuid.uuid4()
    gate = asyncio.Event()

    async def _txn(order: tuple[uuid.UUID, uuid.UUID]) -> None:
        async with owner_session() as s:
            for t in order:
                await _append(s, t, f"from.{order[0]}")
            await gate.wait()

    tasks = [asyncio.create_task(_txn((a, b))), asyncio.create_task(_txn((b, a)))]
    await asyncio.sleep(0.2)
    gate.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 30)

    async with owner_session() as s:
        for t in (a, b):
            n = (
                await s.execute(sa.select(sa.func.count()).where(m.AuditEvent.tenant_id == t))
            ).scalar_one()
            assert n == 2
            assert await verify_chain(s, t) is True


async def test_heavy_concurrent_appends_keep_a_continuous_chain(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    parallel = 50

    async def _txn(i: int) -> None:
        async with app_session(tenant) as s:
            await _append(s, tenant, f"p.{i}.a")
            await asyncio.sleep(0.05)  # slow work between the append and commit
            await _append(s, tenant, f"p.{i}.b")

    await asyncio.wait_for(asyncio.gather(*(_txn(i) for i in range(parallel))), 120)

    async with app_session(tenant) as s:
        rows = (
            await s.execute(
                sa.select(m.AuditEvent.action, m.AuditEvent.prev_hash, m.AuditEvent.hash)
                .where(m.AuditEvent.tenant_id == tenant)
                .order_by(m.AuditEvent.seq.asc())
            )
        ).all()
        assert len(rows) == 2 * parallel
        for prev, cur in itertools.pairwise(rows):
            assert cur.prev_hash == prev.hash
        # Each transaction's two events are adjacent: the batch is written under
        # one lock hold, so nothing interleaves inside it.
        for first, second in zip(rows[::2], rows[1::2], strict=True):
            assert first.action.endswith(".a") and second.action.endswith(".b")
            assert first.action[:-2] == second.action[:-2]
        assert await verify_chain(s, tenant) is True


class _ToolThenWait:
    """First completion asks for a tool (so the run audits one tool call); the
    second waits on ``release`` -- the run is mid-flight, transaction open."""

    def __init__(self) -> None:
        self.calls = 0
        self.tool_done = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, req: Any) -> CompletionResult:
        self.calls += 1
        if self.calls == 1:
            return CompletionResult(
                text="",
                tool_calls=[ToolCall(id="c1", name="noop", arguments={})],
                usage=Usage(tokens_in=1, tokens_out=1),
                stop_reason="tool_use",
                provider="fake",
                model="fake",
            )
        self.tool_done.set()
        await self.release.wait()
        return CompletionResult(
            text="done",
            tool_calls=[],
            usage=Usage(tokens_in=1, tokens_out=1),
            stop_reason="stop",
            provider="fake",
            model="fake",
        )

    async def stream(self, req: Any) -> Any:
        yield chunk_from_result(await self.complete(req))


async def test_in_process_run_does_not_hold_the_tenant_audit_lock(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8.agent.engine import run_agent

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    router = _ToolThenWait()
    monkeypatch.setattr("oc8.agent.engine.get_model_router", lambda: router)

    async def _run() -> None:
        async with app_session(tenant) as s:
            dept = m.Department(tenant_id=tenant, name="Ops", frame={})
            s.add(dept)
            await s.flush()
            agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="Rep")
            s.add(agent)
            await s.flush()
            await run_agent(s, agent=agent, task_text="do a thing", tenant_id=tenant)

    run = asyncio.create_task(_run())
    try:
        await asyncio.wait_for(router.tool_done.wait(), 30)
        # The run has audited its tool call and is still open. An unrelated
        # audited write in the same tenant must commit now, not after the run.
        async with app_session(tenant) as other:
            await asyncio.wait_for(_append(other, tenant, "unrelated.login"), NOT_BLOCKED)
            await asyncio.wait_for(other.commit(), NOT_BLOCKED)
    finally:
        router.release.set()
        await asyncio.wait_for(run, 30)

    async with app_session(tenant) as s:
        actions = (
            (
                await s.execute(
                    sa.select(m.AuditEvent.action)
                    .where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action.in_(["unrelated.login", "tool.call:noop"]),
                    )
                    .order_by(m.AuditEvent.seq.desc())
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        # The run committed last, so its tool call follows the unrelated write.
        assert list(actions) == ["tool.call:noop", "unrelated.login"]
        assert await verify_chain(s, tenant) is True


# ------------------------------------------------------- failed and odd commits


async def _keyed_head(app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """A tenant whose chain head is keyed (mac_version 1), with the flag then
    turned OFF -- so the next commit that writes the chain raises MacDowngrade."""
    import base64

    from oc8.config import get_settings

    tenant = uuid.uuid4()
    monkeypatch.setenv("OC8_SECRET_KEK", base64.b64encode(b"\x33" * 32).decode())
    monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "true")
    get_settings.cache_clear()
    async with app_session(tenant) as s:
        await _append(s, tenant, "keyed.head")
    monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "false")
    get_settings.cache_clear()
    return tenant


async def _business_rows(app_session: AppSessionFactory, tenant: uuid.UUID, name: str) -> int:
    async with app_session(tenant) as s:
        return (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(m.Department)
                .where(m.Department.tenant_id == tenant, m.Department.name == name)
            )
        ).scalar_one()


async def test_a_failed_commit_poisons_the_session_until_rollback(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a chain write that fails at commit must not leave a session that a
    second commit() persists WITHOUT its audit rows."""
    from oc8.audit.chain import MacDowngrade
    from oc8.config import get_settings

    try:
        tenant = await _keyed_head(app_session, monkeypatch)
        async with app_session(tenant) as s:
            s.add(m.Department(tenant_id=tenant, name="poisoned", frame={}))
            await _append(s, tenant, "lost?")
            with pytest.raises(MacDowngrade):
                await s.commit()
            # The retry must refuse, not silently persist the department.
            with pytest.raises(AuditChainPoisoned):
                await s.commit()
            await s.rollback()
            # A rollback clears the mark: the session is usable again.
            await s.execute(
                sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
            )
            await s.execute(sa.text("SELECT 1"))
        assert await _business_rows(app_session, tenant, "poisoned") == 0
        assert await _actions(app_session, tenant) == ["keyed.head"]
    finally:
        get_settings.cache_clear()


async def test_a_downgrade_at_commit_writes_neither_the_business_row_nor_the_event(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1: a real failure driven through commit() leaves no trace."""
    from oc8.audit.chain import MacDowngrade
    from oc8.config import get_settings

    try:
        tenant = await _keyed_head(app_session, monkeypatch)
        with pytest.raises(MacDowngrade):
            async with app_session(tenant) as s:
                s.add(m.Department(tenant_id=tenant, name="business", frame={}))
                await _append(s, tenant, "downgraded")
        assert await _business_rows(app_session, tenant, "business") == 0
        assert await _actions(app_session, tenant) == ["keyed.head"]
    finally:
        get_settings.cache_clear()


async def test_committing_the_root_with_a_savepoint_open_is_refused(
    app_session: AppSessionFactory,
) -> None:
    """C2: SessionTransaction.commit() on the root while a savepoint is still
    open looks like a RELEASE to before_commit. The engine-level guard must
    stop the DBAPI COMMIT rather than persist the rows without their audit."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        s.add(m.Department(tenant_id=tenant, name="unaudited", frame={}))
        await _append(s, tenant, "outer")

        def _commit_root_under_savepoint(sync: Any) -> None:
            sync.begin_nested()
            root = sync.get_transaction()
            root.commit()

        with pytest.raises(AuditChainPoisoned):
            await s.run_sync(_commit_root_under_savepoint)
        await s.rollback()
    assert await _business_rows(app_session, tenant, "unaudited") == 0
    assert await _actions(app_session, tenant) == []


async def test_an_abandoned_session_with_a_savepoint_event_does_not_pin_its_connection() -> None:
    """The commit guard's per-connection registry must not keep a Connection
    alive. A staged event's scope (the savepoint SessionTransaction) references
    the Connection; if the registry's VALUE reached it, a weak KEY could never
    be collected and the pool slot -- with its open transaction -- would be
    pinned forever."""
    import gc
    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        os.environ["OC8_DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=5
    )
    tenant = uuid.uuid4()
    try:

        async def _abandon() -> None:
            s = AsyncSession(engine, expire_on_commit=False)
            await s.execute(
                sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
            )
            await s.begin_nested()
            await _append(s, tenant, "abandoned")
            # Neither closed nor rolled back: dropped on the floor.

        await _abandon()
        gc.collect()
        await asyncio.sleep(0)
        gc.collect()
        assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        async with AsyncSession(engine) as s2:
            assert (await s2.execute(sa.text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()
