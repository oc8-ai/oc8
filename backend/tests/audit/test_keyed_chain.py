from __future__ import annotations

import base64
import uuid
from typing import Any

import pytest
from tests.audit.helpers import append_now
from tests.conftest import AppSessionFactory

from oc8.audit.chain import MacDowngrade, verify_chain
from oc8.config import get_settings

pytestmark = pytest.mark.asyncio

_KEK = base64.b64encode(b"\x33" * 32).decode()


def _enable_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_SECRET_KEK", _KEK)
    monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "true")
    get_settings.cache_clear()


async def test_unkeyed_hashes_are_unchanged(app_session: AppSessionFactory) -> None:
    """The flag defaults off; hashing must be byte-identical to the old code."""
    import hashlib
    import json

    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        ev = await append_now(
            s,
            tenant_id=tenant,
            actor_type="system",
            actor_id=None,
            category="test",
            action="t.one",
        )
        payload: dict[str, Any] = {
            "tenant_id": str(tenant),
            "actor_type": "system",
            "actor_id": None,
            "category": "test",
            "action": "t.one",
            "resource": {},
            "decision": None,
            "reason": None,
            "responsible_type": "tenant",
            "responsible_id": str(tenant),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        assert ev.mac_version == 0
        assert ev.hash == hashlib.sha256(b"\x00" * 32 + canonical).digest()


async def test_keyed_rows_are_written_at_version_1(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            ev = await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.keyed",
            )
            assert ev.mac_version == 1
            assert await verify_chain(s, tenant) is True
    finally:
        get_settings.cache_clear()


async def test_a_chain_across_the_switch_point_verifies(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            for i in range(2):
                await append_now(
                    s,
                    tenant_id=tenant,
                    actor_type="system",
                    actor_id=None,
                    category="test",
                    action=f"keyed.{i}",
                )
            assert await verify_chain(s, tenant) is True
    finally:
        get_settings.cache_clear()


async def test_a_keyed_hash_cannot_be_reproduced_with_a_wrong_key(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_mac(monkeypatch)
    try:
        tenant = uuid.uuid4()
        async with app_session(tenant) as s:
            ev = await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.secret",
            )
            good = ev.hash
        # Same rows, different KEK -> a different key -> a different hash.
        monkeypatch.setenv("OC8_SECRET_KEK", base64.b64encode(b"\x44" * 32).decode())
        get_settings.cache_clear()
        from oc8.audit.chain import recompute_hash

        async with app_session(tenant) as s:
            from sqlalchemy import select

            from oc8.models.ops import AuditEvent

            row = (
                await s.execute(select(AuditEvent).where(AuditEvent.tenant_id == tenant))
            ).scalar_one()
            assert recompute_hash(row, b"\x00" * 32) != good
    finally:
        get_settings.cache_clear()


async def test_writing_unkeyed_behind_a_keyed_head_is_refused(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Turning the flag back off must fail loudly, not silently degrade."""
    _enable_mac(monkeypatch)
    tenant = uuid.uuid4()
    try:
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.keyed",
            )
    finally:
        monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "false")
        get_settings.cache_clear()

    with pytest.raises(MacDowngrade):
        async with app_session(tenant) as s:
            await append_now(
                s,
                tenant_id=tenant,
                actor_type="system",
                actor_id=None,
                category="test",
                action="t.after",
            )
    get_settings.cache_clear()
