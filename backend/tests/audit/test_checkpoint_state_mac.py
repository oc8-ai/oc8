from __future__ import annotations

import base64
import uuid

import pytest
import sqlalchemy as sa
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from tests.audit.helpers import append_now
from tests.conftest import AppSessionFactory

from oc8.audit.integrity import get_checkpoint, verify_full, verify_incremental
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.main import create_app
from oc8.secrets.keyprovider import SecretStoreUnavailable

pytestmark = pytest.mark.asyncio

_KEK = base64.b64encode(b"\x66" * 32).decode()


def _auth(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="u", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


def _enable_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_SECRET_KEK", _KEK)
    monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "true")
    get_settings.cache_clear()


def _lose_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keyed history, key gone.

    The flag goes off with it: Settings refuses audit_mac_enabled=True without
    a valid KEK, so "flag on, key missing" is unrepresentable. The reachable
    shape is a deployment whose chain is already keyed being restarted without
    the KEK -- a rotation, a lost secret, a bad rollback.
    """
    monkeypatch.setenv("OC8_SECRET_KEK", "")
    monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "false")
    get_settings.cache_clear()


async def test_a_keyed_run_records_a_state_mac_and_the_version_reached(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.one",
            )
            cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
        assert cp.max_mac_version == 1
        assert cp.state_mac is not None
    finally:
        get_settings.cache_clear()


async def test_a_wholesale_downgrade_of_the_chain_is_detected(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """The attack this task exists to stop.

    Rewriting EVERY row as unkeyed satisfies monotonicity and every hash
    verifies -- the chain cannot detect it. The checkpoint remembers that the
    tenant reached version 1, and that memory is signed, so rolling it back
    needs the key.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            for i in range(3):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"t.{i}",
                )
            cp = await verify_incremental(s, tenant)
            assert cp.max_mac_version == 1

        # Rewrite the whole chain as unkeyed, exactly as an attacker with
        # UPDATE would: recompute each row's sha256 over its own canonical
        # payload, walking forward from genesis.
        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE audit_event SET mac_version = 0 WHERE tenant_id = :t"),
                {"t": str(tenant)},
            )
            _rehash_unkeyed(conn, tenant)
        engine.dispose()

        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"
    finally:
        get_settings.cache_clear()


async def _downgrade_the_whole_chain(pg_url: str, tenant: uuid.UUID) -> None:
    """Every row to mac_version 0, re-hashed as plain sha256 from genesis."""
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE audit_event SET mac_version = 0 WHERE tenant_id = :t"),
            {"t": str(tenant)},
        )
        _rehash_unkeyed(conn, tenant)
    engine.dispose()


async def test_a_keyed_append_after_a_downgrade_cannot_launder_it(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """C1: one legitimate append re-arms the chain and a full verify says 'ok'.

    max(mac_version) over the whole chain compared against a whole-chain
    maximum only differs while EVERY row is at 0 -- which ends at the next
    honest append, i.e. seconds on a live tenant against an hourly tick. After
    that the laundered chain (0,0,...,0,1) is byte-for-byte the shape of a
    normal migration, so the comparison structurally cannot tell them apart.

    The quantity that CAN tell them apart is measured strictly below the
    checkpointed mark, where no future append can land.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            for i in range(3):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"t.{i}",
                )
            cp = await verify_incremental(s, tenant)
            assert cp.status == "ok"
            assert cp.max_mac_version == 1

        await _downgrade_the_whole_chain(pg_url, tenant)

        # The app appends normally: head is v0, target is v1, so the
        # monotonicity rule permits it and the chain is no longer all-zero.
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.after",
            )

        # The tick sees the rewritten tail and reports a hash mismatch, which
        # is what sends the operator to the full check.
        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"

        # The full check the UI recommends must NOT come back clean.
        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"
    finally:
        get_settings.cache_clear()


async def test_erasing_the_marker_is_not_a_way_out(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """C2a: erasure is not forgery, and nothing was checking for it.

    One statement nulls the marker and the remembered version; the wholesale
    downgrade then verifies perfectly, and the clean branch cheerfully signs a
    fresh marker over max_mac_version 0. The tenant becomes indistinguishable
    from one that was never keyed.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            for i in range(3):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"t.{i}",
                )
            cp = await verify_incremental(s, tenant)
            assert cp.state_mac is not None

        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE audit_chain_checkpoint SET state_mac = NULL, "
                    "max_mac_version = 0 WHERE tenant_id = :t"
                ),
                {"t": str(tenant)},
            )
        engine.dispose()
        await _downgrade_the_whole_chain(pg_url, tenant)

        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"
    finally:
        get_settings.cache_clear()


async def test_moving_the_resumption_point_with_the_chain_is_detected(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """C2b: the marker did not cover last_seq/last_hash.

    Those two are the incremental walk's resumption point. Rewriting the chain
    AND pointing the checkpoint at the rewritten tail leaves every signed field
    untouched, so the marker still authenticates, the walk resumes from the
    forged tail and links cleanly, and the attack is silent from the very first
    tick -- no operator action needed at all.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            for i in range(3):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"t.{i}",
                )
            cp = await verify_incremental(s, tenant)
            assert cp.status == "ok"

        await _downgrade_the_whole_chain(pg_url, tenant)
        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE audit_chain_checkpoint SET last_hash = ("
                    "  SELECT hash FROM audit_event WHERE tenant_id = :t "
                    "  ORDER BY seq DESC LIMIT 1) WHERE tenant_id = :t"
                ),
                {"t": str(tenant)},
            )
        engine.dispose()

        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.after",
            )

        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"
    finally:
        get_settings.cache_clear()


async def test_a_tampered_state_mac_is_detected(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.one",
            )
            await verify_incremental(s, tenant)

        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE audit_chain_checkpoint SET max_mac_version = 0 WHERE tenant_id = :t"
                ),
                {"t": str(tenant)},
            )
        engine.dispose()

        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"
    finally:
        get_settings.cache_clear()


async def test_an_unkeyed_deployment_is_unaffected(
    app_session: AppSessionFactory,
) -> None:
    """Flag off: no state_mac, no new failure mode, behaviour unchanged."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await append_now(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="test",
            action="t.one",
        )
        cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
        assert cp.max_mac_version == 0
        assert cp.state_mac is None
        assert (await get_checkpoint(s, tenant)) is not None


async def test_a_verifier_that_could_not_run_is_unverifiable_not_ok(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing key must never leave the last 'ok' standing.

    recompute_hash raises SecretStoreUnavailable, which is NOT a ValueError, so
    before this it escaped _run entirely: run_integrity_tick logged and moved
    on, no checkpoint was written, and GET /audit/integrity kept serving the
    stored 'ok' -- a green chain over a verifier that never ran. It is also not
    a break: a missing key is a configuration fault, not evidence of tampering.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.one",
            )
            cp = await verify_incremental(s, tenant)
            assert cp.status == "ok"

        _lose_the_key(monkeypatch)
        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "unverifiable"
        assert cp.break_kind is None
        assert cp.broken_at_seq is None
        assert cp.first_break_at is None

        # Not sticky: it says nothing about the chain, only about the verifier.
        monkeypatch.setenv("OC8_SECRET_KEK", _KEK)
        get_settings.cache_clear()
        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
    finally:
        get_settings.cache_clear()


async def test_missing_rows_still_outrank_a_missing_key(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """Truncation is detected by counting, which needs no key at all."""
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            for i in range(3):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"t.{i}",
                )
            await verify_incremental(s, tenant)

        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM audit_event WHERE tenant_id = :t AND seq = "
                    "(SELECT max(seq) FROM audit_event WHERE tenant_id = :t)"
                ),
                {"t": str(tenant)},
            )
        engine.dispose()

        _lose_the_key(monkeypatch)
        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "truncation"
    finally:
        get_settings.cache_clear()


async def test_a_keyed_tenant_recovers_from_a_truncation_when_the_rows_come_back(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """The disaster-recovery path, on a KEYED tenant.

    The existing restore test runs unkeyed, so no marker exists at all and the
    state block never speaks. With a marker in play the two quantities the
    message binds have OPPOSITE requirements on a break path: the resumption
    pair must be re-signed (_apply retreats it, and a marker signed over the
    old pair fails on the very next run), while the keyed-row count must NOT be
    (rows are missing, so a live re-derivation is short by exactly the rows the
    restore is about to bring back). Deriving the count live at signing time
    cannot satisfy both -- it signs the SHORT count during the truncation, and
    then the restore that fixes the truncation makes the marker fail, reporting
    a permanent, unclearable mac_downgrade to an operator who did nothing worse
    than restore a backup.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            for i in range(4):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"t.{i}",
                )
            cp = await verify_incremental(s, tenant)
            assert cp.status == "ok"

        victim = _take_the_last_row(pg_url, tenant)

        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "truncation"

        _restore_the_row(pg_url, victim)

        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "ok", f"bricked: {cp.break_kind}"
        assert cp.break_kind is None
    finally:
        get_settings.cache_clear()


def _take_the_last_row(pg_url: str, tenant: uuid.UUID) -> dict[str, object]:
    """Delete the tenant's newest event, keeping every column so it can come
    back byte-for-byte -- which is what restoring from a backup does."""
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        row = dict(
            conn.execute(
                sa.text("SELECT * FROM audit_event WHERE tenant_id = :t ORDER BY seq DESC LIMIT 1"),
                {"t": str(tenant)},
            )
            .mappings()
            .one()
        )
        conn.execute(sa.text("DELETE FROM audit_event WHERE seq = :s"), {"s": row["seq"]})
    engine.dispose()
    return row


def _restore_the_row(pg_url: str, row: dict[str, object]) -> None:
    cols = ", ".join(row.keys())
    binds = ", ".join(f":{k}" for k in row)
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO audit_event ({cols}) OVERRIDING SYSTEM VALUE VALUES ({binds})"
            ).bindparams(sa.bindparam("resource", type_=JSONB)),
            row,
        )
    engine.dispose()


async def test_verify_endpoint_reports_unverifiable_rather_than_500(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.one",
            )
            await verify_incremental(s, tenant)

        _lose_the_key(monkeypatch)
        app = create_app()
        async with LifespanManager(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.post("/api/v1/audit/verify", headers=_auth(tenant))
        assert r.status_code == 200
        assert r.json()["status"] == "unverifiable"
    finally:
        get_settings.cache_clear()


async def test_a_secret_error_out_of_verify_is_a_503_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Belt and braces for the handler itself.

    _run converts the key-missing case into 'unverifiable', but the endpoint
    caught only SQLAlchemyError, so any SecretError reaching it surfaced as a
    500. A run that did not finish must never look like a server bug, and must
    never be reported as a result.
    """
    tenant = uuid.uuid4()

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise SecretStoreUnavailable("secret_kek is not configured")

    monkeypatch.setattr("oc8.api.v1.audit.verify_incremental", _boom)
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/audit/verify", headers=_auth(tenant))
    assert r.status_code == 503


async def test_a_lost_key_never_silences_a_standing_break(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """I1: 'unverifiable' must not overwrite a STORED 'broken'.

    The guard tested this run's verdict, not the stored one, so a tenant
    already recorded broken/hash_mismatch whose key then went missing came back
    as status='unverifiable' with break_kind='hash_mismatch' still set -- an
    incoherent DTO, and any alerting keyed on status == 'broken' goes quiet for
    as long as the key is missing.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            for i in range(2):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"t.{i}",
                )
            await verify_incremental(s, tenant)

        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE audit_event SET reason = 'tampered' WHERE seq = "
                    "(SELECT max(seq) FROM audit_event WHERE tenant_id = :t)"
                ),
                {"t": str(tenant)},
            )
        engine.dispose()

        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "hash_mismatch"
        broken_at = cp.broken_at_seq

        _lose_the_key(monkeypatch)
        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "hash_mismatch"
        assert cp.broken_at_seq == broken_at
    finally:
        get_settings.cache_clear()


async def test_a_backfilled_checkpoint_over_a_keyed_chain_does_not_crash(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """I2: the SecretError crash path the 'unverifiable' status exists to kill.

    The pre-flight tested `keyed or cp.max_mac_version > 0` on the PRE-update
    value while the signing site tested the post-update max(old, chain_version).
    They diverge exactly where migration 0030 leaves a deployment: checkpoints
    backfilled with max_mac_version = 0 over a chain that is already keyed. Set
    the flag off and lose the KEK and the walk finds no new rows (nothing
    raises), the state block is skipped (no marker), the pre-flight is skipped
    -- and then the signing call raised SecretStoreUnavailable out of _run. The
    tick swallowed it, no checkpoint was written, and the API kept serving the
    stored 'ok'.
    """
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.one",
            )
            await verify_incremental(s, tenant)

        # Exactly what 0030's backfill leaves behind on a pre-existing
        # checkpoint: the columns exist, at their defaults, over a keyed chain.
        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE audit_chain_checkpoint SET max_mac_version = 0, "
                    "state_mac = NULL WHERE tenant_id = :t"
                ),
                {"t": str(tenant)},
            )
        engine.dispose()

        _lose_the_key(monkeypatch)
        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "unverifiable"
        assert cp.state_mac is None
    finally:
        get_settings.cache_clear()


async def test_a_clean_rewalk_over_extra_rows_still_reports_them(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """M1: the forced-walk-came-back-clean fallback.

    The insertion detector is a pure count, and the walk it forces normally
    finds the forged row's broken linkage. This is the case where it does not:
    the count says rows exist below the mark that were not there at the last
    clean pass, the re-walk from genesis is internally perfect, and the
    fallback is the only thing left that can speak. Reached here by lowering
    verified_count -- the checkpoint's UPDATE privilege is retained by the
    runtime role (0028 revokes only DELETE), so this is the reachable shape,
    and it is indistinguishable from rows having been added below the mark.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        for i in range(3):
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action=f"t.{i}",
            )
        cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
        mark = cp.max_seen_seq

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "UPDATE audit_chain_checkpoint SET verified_count = verified_count - 1 "
                "WHERE tenant_id = :t"
            ),
            {"t": str(tenant)},
        )
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
    assert cp.status == "broken"
    assert cp.break_kind == "hash_mismatch"
    # The mark itself, which is what the fallback reports -- no walk position
    # could produce it, because the walk found nothing wrong.
    assert cp.broken_at_seq == mark


def _rehash_unkeyed(conn: sa.Connection, tenant: uuid.UUID) -> None:
    """Recompute the tenant's whole chain as plain sha256, from genesis.

    This is what an attacker with UPDATE does after flipping mac_version to 0:
    the resulting chain is completely self-consistent -- every prev_hash links,
    every hash recomputes -- and needs no key at all. Without a signed memory of
    the version the tenant reached, the verifier accepts it.
    """
    import hashlib
    import json

    rows = conn.execute(
        sa.text(
            "SELECT seq, tenant_id, actor_type, actor_id, category, action, resource, "
            "decision, reason, responsible_type, responsible_id FROM audit_event "
            "WHERE tenant_id = :t ORDER BY seq ASC"
        ),
        {"t": str(tenant)},
    ).mappings()
    prev = b"\x00" * 32
    for row in rows.all():
        payload: dict[str, object] = {
            "tenant_id": str(row["tenant_id"]),
            "actor_type": row["actor_type"],
            "actor_id": str(row["actor_id"]) if row["actor_id"] else None,
            "category": row["category"],
            "action": row["action"],
            "resource": row["resource"],
            "decision": row["decision"],
            "reason": row["reason"],
        }
        if row["responsible_type"] is not None:
            payload["responsible_type"] = row["responsible_type"]
            payload["responsible_id"] = row["responsible_id"]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        digest = hashlib.sha256(prev + canonical).digest()
        conn.execute(
            sa.text("UPDATE audit_event SET prev_hash = :p, hash = :h WHERE seq = :s"),
            {"p": prev, "h": digest, "s": row["seq"]},
        )
        prev = digest


async def _forge_below_the_mark(pg_url: str, tenant: uuid.UUID, seq: int, prev: bytes) -> None:
    """Insert a fully self-consistent row at an already-checkpointed seq.

    seq is Identity(always=True), but OVERRIDING SYSTEM VALUE needs only the
    INSERT privilege that migration 0001 leaves oc8_app -- and _walk starts at
    seq > cp.last_seq, so it never revisits the range this lands in.
    """
    import hashlib
    import json

    payload: dict[str, object] = {
        "tenant_id": str(tenant),
        "actor_type": "system",
        "actor_id": None,
        "category": "test",
        "action": "forged",
        "resource": {},
        "decision": None,
        "reason": None,
        "responsible_type": "tenant",
        "responsible_id": str(tenant),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO audit_event (id, seq, tenant_id, actor_type, category, "
                "action, resource, responsible_type, responsible_id, prev_hash, hash, "
                "mac_version) OVERRIDING SYSTEM VALUE VALUES (gen_random_uuid(), :s, :t, "
                "'system', 'test', 'forged', '{}'::jsonb, 'tenant', :t2, :prev, :h, 0)"
            ),
            {
                "s": seq,
                "t": str(tenant),
                "t2": str(tenant),
                "prev": prev,
                "h": hashlib.sha256(prev + canonical).digest(),
            },
        )
    engine.dispose()


async def test_a_row_inserted_below_the_high_water_mark_is_detected(
    app_session: AppSessionFactory, pg_url: str
) -> None:
    """The gap an incremental walk cannot see.

    The truncation guard fires on present < verified_count, and an insertion
    RAISES the count. But present > verified_count is not legitimately
    reachable: a real append always lands above max_seen_seq, so it cannot add
    to the count at or below it. Extra rows below the mark can only have been
    put there out of band.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await append_now(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="test",
            action="t.0",
        )

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        # Burn a seq so there is a free slot INSIDE the range about to be
        # checkpointed. A rolled-back insert leaves exactly such a hole in
        # production; nextval is the same thing without the insert.
        free_seq = conn.execute(
            sa.text("SELECT nextval(pg_get_serial_sequence('audit_event', 'seq'))")
        ).scalar_one()
    engine.dispose()

    async with app_session(tenant) as s:
        for i in (1, 2):
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action=f"t.{i}",
            )
        cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
        assert cp.verified_count == 3
        assert cp.max_seen_seq > free_seq

    async with app_session(tenant) as s:
        prev = (
            await s.execute(
                sa.text(
                    "SELECT hash FROM audit_event WHERE tenant_id = :t AND seq < :s "
                    "ORDER BY seq DESC LIMIT 1"
                ),
                {"t": str(tenant), "s": free_seq},
            )
        ).scalar_one()
    await _forge_below_the_mark(pg_url, tenant, free_seq, prev)

    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
    assert cp.status == "broken"
    assert cp.break_kind is not None
