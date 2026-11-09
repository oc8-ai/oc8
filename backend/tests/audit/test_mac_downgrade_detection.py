from __future__ import annotations

import base64
import uuid

import pytest
import sqlalchemy as sa
from tests.audit.helpers import append_now
from tests.conftest import AppSessionFactory

from oc8.audit.integrity import verify_full, verify_incremental
from oc8.config import get_settings

pytestmark = pytest.mark.asyncio

_KEK = base64.b64encode(b"\x55" * 32).decode()


def _enable_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_SECRET_KEK", _KEK)
    monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "true")
    get_settings.cache_clear()


async def test_a_forged_unkeyed_row_behind_keyed_rows_is_detected(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """The attack this whole slice exists to stop.

    Forging a row marked mac_version 0 needs no secret -- its hash is a plain
    sha256. If the verifier trusted each row's declared version, the forgery
    would verify. It must not.
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
                    action=f"keyed.{i}",
                )
            cp = await verify_incremental(s, tenant)
            assert cp.status == "ok"

        # Forge an unkeyed row as the owner, the way an attacker with database
        # access would -- oc8_app cannot INSERT past the app, but the owner can.
        async with app_session(tenant) as s:
            head = (
                await s.execute(
                    sa.text(
                        "SELECT hash FROM audit_event WHERE tenant_id = :t "
                        "ORDER BY seq DESC LIMIT 1"
                    ),
                    {"t": str(tenant)},
                )
            ).scalar_one()

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
        forged = hashlib.sha256(head + canonical).digest()

        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO audit_event (id, tenant_id, actor_type, category, "
                    "action, resource, responsible_type, responsible_id, prev_hash, "
                    "hash, mac_version) VALUES (gen_random_uuid(), :t, 'system', "
                    "'test', 'forged', '{}'::jsonb, 'tenant', :t2, :prev, :h, 0)"
                ),
                {"t": str(tenant), "t2": str(tenant), "prev": head, "h": forged},
            )
        engine.dispose()

        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"
    finally:
        get_settings.cache_clear()


async def test_a_legitimate_switch_point_is_not_a_downgrade(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unkeyed rows FOLLOWED BY keyed rows is the normal migration path."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        for i in range(2):
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action=f"legacy.{i}",
            )
    _enable_mac(monkeypatch)
    try:
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="keyed.0",
            )
            cp = await verify_full(s, tenant)
        assert cp.status == "ok"
        assert cp.break_kind is None
    finally:
        get_settings.cache_clear()


async def test_incremental_verify_seeds_the_version_floor_from_history(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
    """An incremental walk starts mid-chain and must still know the floor."""
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
                action="keyed.0",
            )
            await verify_incremental(s, tenant)

        async with app_session(tenant) as s:
            head = (
                await s.execute(
                    sa.text(
                        "SELECT hash FROM audit_event WHERE tenant_id = :t "
                        "ORDER BY seq DESC LIMIT 1"
                    ),
                    {"t": str(tenant)},
                )
            ).scalar_one()

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
        forged = hashlib.sha256(head + canonical).digest()

        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO audit_event (id, tenant_id, actor_type, category, "
                    "action, resource, responsible_type, responsible_id, prev_hash, "
                    "hash, mac_version) VALUES (gen_random_uuid(), :t, 'system', "
                    "'test', 'forged', '{}'::jsonb, 'tenant', :t2, :prev, :h, 0)"
                ),
                {"t": str(tenant), "t2": str(tenant), "prev": head, "h": forged},
            )
        engine.dispose()

        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"
    finally:
        get_settings.cache_clear()


async def test_an_unknown_mac_version_is_reported_not_swallowed(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch, pg_url: str
) -> None:
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
        seq = (
            await s.execute(
                sa.text("SELECT min(seq) FROM audit_event WHERE tenant_id = :t"),
                {"t": str(tenant)},
            )
        ).scalar_one()

    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE audit_event SET mac_version = 7 WHERE seq = :s"), {"s": seq})
    engine.dispose()

    async with app_session(tenant) as s:
        cp = await verify_full(s, tenant)
    assert cp.status == "broken"
    assert cp.break_kind == "unknown_mac_version"
