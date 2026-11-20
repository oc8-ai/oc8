from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from tests.audit.helpers import append_now
from tests.conftest import AppSessionFactory

from oc8.audit import integrity
from oc8.audit.integrity import get_checkpoint, verify_full, verify_incremental

pytestmark = pytest.mark.asyncio


async def _emit(s: AsyncSession, tenant: uuid.UUID, n: int) -> None:
    for i in range(n):
        await append_now(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="test",
            action=f"t.{i}",
        )
    await s.flush()


async def test_first_run_verifies_everything_and_stores_head(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 3)
        cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
        assert cp.last_seq > 0
        assert cp.broken_at_seq is None


async def test_second_run_advances_only_over_new_events(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 2)
        first = await verify_incremental(s, tenant)
        first_seq = first.last_seq

        await _emit(s, tenant, 2)
        second = await verify_incremental(s, tenant)
        assert second.last_seq > first_seq
        assert second.status == "ok"


async def test_no_new_events_is_a_noop(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 2)
        a = await verify_incremental(s, tenant)
        b = await verify_incremental(s, tenant)
        assert (a.last_seq, a.last_hash) == (b.last_seq, b.last_hash)
        assert b.status == "ok"


async def test_empty_chain_reports_ok_at_genesis(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
        assert cp.last_seq == 0


async def test_tampered_row_is_detected(app_session: AppSessionFactory, pg_url: str) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 3)
        rows = (
            await s.execute(
                sa.text(
                    "SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 1"
                ),
                {"t": str(tenant)},
            )
        ).all()
        victim_seq = rows[0][0]

    # Tamper as the owning role -- oc8_app is REVOKEd from UPDATE (migration 0001).
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET reason = 'tampered' WHERE seq = :s"),
            {"s": victim_seq},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.broken_at_seq == victim_seq


async def test_broken_status_survives_a_second_run(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 2)
        seq = (
            await s.execute(
                sa.text("SELECT min(seq) FROM audit_event WHERE tenant_id = :t"),
                {"t": str(tenant)},
            )
        ).scalar_one()

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE audit_event SET action = 'x' WHERE seq = :s"), {"s": seq})
    engine.dispose()

    async with app_session(tenant) as s:
        await verify_incremental(s, tenant)
        again = await verify_incremental(s, tenant)
        assert again.status == "broken"


async def test_verify_full_reverifies_from_genesis(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 3)
        await verify_incremental(s, tenant)
        cp = await verify_full(s, tenant)
        assert cp.status == "ok"
        assert (await get_checkpoint(s, tenant)) is not None


async def test_app_role_cannot_update_audit_event(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 1)
    with pytest.raises(Exception):  # noqa: B017 -- driver-wrapped permission error, type varies
        async with app_session(tenant) as s:
            await s.execute(
                sa.text("UPDATE audit_event SET reason = 'nope' WHERE tenant_id = :t"),
                {"t": str(tenant)},
            )


async def test_app_role_cannot_delete_the_checkpoint(
    app_session: AppSessionFactory,
) -> None:
    """Deleting the checkpoint row resets tamper evidence: _ensure_checkpoint
    would rebuild a fresh, clean one over the survivors, silently erasing both
    an in-progress truncation and first_break_at. That primitive must sit
    above the application role, same as the immutability of audit_event
    itself -- see migration 0028."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 1)
        await verify_incremental(s, tenant)
    with pytest.raises(Exception):  # noqa: B017 -- driver-wrapped permission error, type varies
        async with app_session(tenant) as s:
            await s.execute(
                sa.text("DELETE FROM audit_chain_checkpoint WHERE tenant_id = :t"),
                {"t": str(tenant)},
            )


async def test_app_role_can_still_update_the_checkpoint(
    app_session: AppSessionFactory,
) -> None:
    """Unlike audit_event, this table is legitimately mutable: the
    verification job rewrites it on every run. Only DELETE is revoked."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 1)
        await verify_incremental(s, tenant)

    async with app_session(tenant) as s:
        await s.execute(
            sa.text("UPDATE audit_chain_checkpoint SET verified_at = now() WHERE tenant_id = :t"),
            {"t": str(tenant)},
        )
        cp = await get_checkpoint(s, tenant)
        assert cp is not None


async def test_full_verify_does_not_let_incremental_launder_a_break(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """Reproduces the laundering bug: a row inside an *already checkpointed*
    range gets tampered, verify_full flags it broken, and a later
    verify_incremental must not silently flip it back to ok just because it
    finds no new rows beyond the (stale) checkpoint."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 3)
        first = await verify_incremental(s, tenant)
        assert first.status == "ok"
        rows = (
            await s.execute(
                sa.text(
                    "SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 1"
                ),
                {"t": str(tenant)},
            )
        ).all()
        victim_seq = rows[0][0]

    # Tamper as the owning role -- oc8_app is REVOKEd from UPDATE (migration 0001).
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET reason = 'tampered' WHERE seq = :s"),
            {"s": victim_seq},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        full = await verify_full(s, tenant)
        assert full.status == "broken"
        assert full.broken_at_seq == victim_seq
        # The checkpoint must retreat to the last-good position, never sit
        # past an unresolved break.
        assert full.last_seq < victim_seq

    async with app_session(tenant) as s:
        again = await verify_incremental(s, tenant)
        assert again.status == "broken"
        assert again.broken_at_seq == victim_seq


async def test_trailing_truncation_is_detected(app_session: AppSessionFactory, pg_url: str) -> None:
    """Deleting the newest events must not look like 'nothing new happened'."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 4)
        first = await verify_incremental(s, tenant)
        assert first.status == "ok"

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM audit_event WHERE tenant_id = :t "
                "AND seq = (SELECT max(seq) FROM audit_event WHERE tenant_id = :t)"
            ),
            {"t": str(tenant)},
        )
        remaining_max = conn.execute(
            sa.text("SELECT max(seq) FROM audit_event WHERE tenant_id = :t"),
            {"t": str(tenant)},
        ).scalar_one()
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.broken_at_seq == remaining_max


async def test_multi_page_walk_reaches_true_head(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BATCH=1000 in production and no test emits more than a handful of
    events, so the paging loop in _walk is otherwise never exercised across
    more than one page."""
    monkeypatch.setattr(integrity, "BATCH", 2)
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 5)
        max_seq = (
            await s.execute(
                sa.text("SELECT max(seq) FROM audit_event WHERE tenant_id = :t"),
                {"t": str(tenant)},
            )
        ).scalar_one()
        cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
        assert cp.last_seq == max_seq


async def test_multi_page_walk_catches_tamper_across_page_boundary(
    app_session: AppSessionFactory, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With BATCH=2, 5 events page as [1,2] [3,4] [5]. Tampering the 4th
    event (2nd row of the 2nd page) proves the prev-hash carried correctly
    across the page-1/page-2 boundary (event 3 must NOT false-positive) and
    that the real break is still caught at the right seq."""
    monkeypatch.setattr(integrity, "BATCH", 2)
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 5)
        rows = (
            await s.execute(
                sa.text(
                    "SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 3"
                ),
                {"t": str(tenant)},
            )
        ).all()
        victim_seq = rows[0][0]

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET reason = 'tampered' WHERE seq = :s"),
            {"s": victim_seq},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.broken_at_seq == victim_seq


async def test_verify_full_cannot_clear_a_detected_truncation(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """C1: the surviving rows after a head-truncation still form a valid chain
    from genesis, so a full walk finds no hash break. The monotonic high-water
    mark is the only surviving evidence -- verify_full must consult it and
    must NOT return the tenant to "ok" while rows are still missing."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 5)
        first = await verify_incremental(s, tenant)
        assert first.status == "ok"

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM audit_event WHERE tenant_id = :t AND seq >= "
                "(SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 3)"
            ),
            {"t": str(tenant)},
        )
        remaining_max = conn.execute(
            sa.text("SELECT max(seq) FROM audit_event WHERE tenant_id = :t"),
            {"t": str(tenant)},
        ).scalar_one()
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.broken_at_seq == remaining_max

    # The operator clicks "Full check". The rows are still gone, so this must
    # not turn the banner green.
    async with app_session(tenant) as s:
        cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.broken_at_seq == remaining_max

    # ...and again, forever, as long as the rows are missing.
    async with app_session(tenant) as s:
        cp = await verify_full(s, tenant)
        assert cp.status == "broken"


async def test_truncation_survives_a_later_append_and_a_full_verify(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """C2, the decisive case: seq is a GLOBAL identity column, so after a
    head-truncation the tenant's tail climbs back over any high-water *mark*
    as soon as one new event is appended -- and on a live tenant every
    approval decision appends. The invariant therefore has to be a COUNT of
    rows at or below the high-water mark, which a later append (whose seq is
    above the mark) cannot inflate.

    Truncate -> append -> full verify must still report broken."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 5)
        first = await verify_incremental(s, tenant)
        assert first.status == "ok"

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM audit_event WHERE tenant_id = :t AND seq >= "
                "(SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 2)"
            ),
            {"t": str(tenant)},
        )
        remaining_max = conn.execute(
            sa.text("SELECT max(seq) FROM audit_event WHERE tenant_id = :t"),
            {"t": str(tenant)},
        ).scalar_one()
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "truncation"
        assert cp.broken_at_seq == remaining_max

    # A new event lands. Its seq is above the old high-water mark, because seq
    # is global and other tenants have been writing. This is the step that used
    # to launder the break.
    async with app_session(tenant) as s:
        await _emit(s, tenant, 1)
        new_tail = (
            await s.execute(
                sa.text("SELECT max(seq) FROM audit_event WHERE tenant_id = :t"),
                {"t": str(tenant)},
            )
        ).scalar_one()
        assert new_tail > remaining_max
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"

    # The operator clicks "Full check". The surviving rows form a valid chain
    # from genesis and the tail is now *above* the old mark -- and it must
    # still be broken, because rows are still missing.
    async with app_session(tenant) as s:
        cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "truncation"
        # The reported position stays where the entries went missing, it does
        # not drift up to the new tail.
        assert cp.broken_at_seq == remaining_max


async def test_middle_row_deletion_is_detected(app_session: AppSessionFactory, pg_url: str) -> None:
    """A deletion strictly inside an already-checkpointed range leaves the tail
    untouched, so no high-water comparison can see it. The count invariant
    does."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 5)
        assert (await verify_incremental(s, tenant)).status == "ok"
        victim_seq = (
            await s.execute(
                sa.text(
                    "SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 2"
                ),
                {"t": str(tenant)},
            )
        ).scalar_one()

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM audit_event WHERE seq = :s"), {"s": victim_seq})
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "truncation"


async def test_restoring_the_missing_rows_clears_a_truncation(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """The only legitimate remedy: the rows come back. Nothing else clears it,
    and there is deliberately no acknowledgement endpoint."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 4)
        assert (await verify_incremental(s, tenant)).status == "ok"

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        victim = (
            conn.execute(
                sa.text("SELECT * FROM audit_event WHERE tenant_id = :t ORDER BY seq DESC LIMIT 1"),
                {"t": str(tenant)},
            )
            .mappings()
            .one()
        )
        conn.execute(sa.text("DELETE FROM audit_event WHERE seq = :s"), {"s": victim["seq"]})
    engine.dispose()

    async with app_session(tenant) as s:
        assert (await verify_incremental(s, tenant)).status == "broken"

    cols = ", ".join(victim.keys())
    binds = ", ".join(f":{k}" for k in victim.keys())
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO audit_event ({cols}) OVERRIDING SYSTEM VALUE VALUES ({binds})"
            ).bindparams(sa.bindparam("resource", type_=JSONB)),
            dict(victim),
        )
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_full(s, tenant)
        assert cp.status == "ok"
        assert cp.break_kind is None


async def test_hash_mismatch_is_labelled_as_such(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 3)
        victim_seq = (
            await s.execute(
                sa.text(
                    "SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 1"
                ),
                {"t": str(tenant)},
            )
        ).scalar_one()

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET reason = 'tampered' WHERE seq = :s"),
            {"s": victim_seq},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "hash_mismatch"


async def test_verified_count_only_advances_on_a_clean_pass(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 3)
        cp = await verify_incremental(s, tenant)
        assert cp.verified_count == 3
        mark, count = cp.max_seen_seq, cp.verified_count

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "DELETE FROM audit_event WHERE tenant_id = :t "
                "AND seq = (SELECT max(seq) FROM audit_event WHERE tenant_id = :t)"
            ),
            {"t": str(tenant)},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        await _emit(s, tenant, 2)
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        # Neither the mark nor the count moved: a broken pass proves nothing.
        assert cp.max_seen_seq == mark
        assert cp.verified_count == count


async def test_first_break_marker_survives_a_successful_full_verification(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """C1: status may return to "ok" (the operator's acknowledgement), but the
    fact that a break was once observed must never be erased."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await _emit(s, tenant, 3)
        rows = (
            await s.execute(
                sa.text(
                    "SELECT seq FROM audit_event WHERE tenant_id = :t ORDER BY seq LIMIT 1 OFFSET 1"
                ),
                {"t": str(tenant)},
            )
        ).all()
        victim_seq = rows[0][0]

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET reason = 'tampered' WHERE seq = :s"),
            {"s": victim_seq},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.first_break_at is not None
        marked_at = cp.first_break_at

    # Undo the tamper, so a full re-verification legitimately succeeds.
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET reason = NULL WHERE seq = :s"),
            {"s": victim_seq},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_full(s, tenant)
        assert cp.status == "ok"
        assert cp.broken_at_seq is None
        # The residue survives.
        assert cp.first_break_at == marked_at
