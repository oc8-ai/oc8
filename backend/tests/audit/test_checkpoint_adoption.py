"""Turning OC8_AUDIT_MAC_ENABLED on over an existing chain (§12.5).

Migration 0031 adopts unsigned checkpoints only if the flag happens to be on
WHEN THE MIGRATION RUNS. The normal rollout is the other order -- migrations
ship, the flag is turned on later -- and in that ordering every checkpoint that
has verified something and carries no marker hits the erasure branch on the
first keyed tick and is reported as mac_downgrade, which is deliberately
unclearable in band. `oc8 audit-adopt-checkpoints` is the in-band remedy.
"""

from __future__ import annotations

import base64
import uuid

import pytest
import sqlalchemy as sa
from tests.audit.helpers import append_now
from tests.conftest import AppSessionFactory

from oc8.audit.integrity import (
    adopt_checkpoints,
    get_checkpoint,
    verify_full,
    verify_incremental,
)
from oc8.config import get_settings

pytestmark = pytest.mark.asyncio

_KEK = base64.b64encode(b"\x66" * 32).decode()


def _enable_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_SECRET_KEK", _KEK)
    monkeypatch.setenv("OC8_AUDIT_MAC_ENABLED", "true")
    get_settings.cache_clear()


async def _existing_unkeyed_tenant(app_session: AppSessionFactory) -> uuid.UUID:
    """A tenant whose chain was written and verified before the flag went on:
    a checkpoint with verified_count > 0 and no marker."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        await s.execute(
            sa.text(
                "INSERT INTO organization (id, slug, name, tier, region, settings, onboarding_status) "
                "VALUES (:id, :slug, 'adopt-test', 'standard', 'eu', '{}'::jsonb, 'pending') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": str(tenant), "slug": str(tenant)},
        )
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
        assert cp.state_mac is None
        assert cp.verified_count > 0
    return tenant


async def test_turning_the_flag_on_over_an_existing_chain_bricks_until_adopted(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _existing_unkeyed_tenant(app_session)
    _enable_mac(monkeypatch)
    try:
        # The brick: the erasure branch cannot tell "the operator just turned
        # the flag on" from "an attacker deleted the marker".
        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.break_kind == "mac_downgrade"

        assert await adopt_checkpoints([tenant]) == 1

        # Adoption re-issues the seal; it deliberately does NOT clear the
        # standing break. "Only an operator clears a break, via verify_full"
        # is a hard invariant of _apply, and a command that could clear one
        # would be a laundering primitive. An incremental tick therefore still
        # reports broken -- but no longer as mac_downgrade evidence it invents
        # anew, and the operator's full check now succeeds.
        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "broken"
        assert cp.state_mac is not None

        async with app_session(tenant) as s:
            cp = await verify_full(s, tenant)
        assert cp.status == "ok", f"still broken: {cp.break_kind}"
        assert cp.break_kind is None
        assert cp.state_mac is not None
        # Write-once: the break is acknowledged, never erased.
        assert cp.first_break_at is not None
    finally:
        get_settings.cache_clear()


async def test_adoption_is_idempotent_and_never_overwrites_a_marker(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe to run twice -- and the second run must not re-sign anything, or
    the command would become a way to launder a marker that failed."""
    tenant = await _existing_unkeyed_tenant(app_session)
    _enable_mac(monkeypatch)
    try:
        assert await adopt_checkpoints([tenant]) == 1
        async with app_session(tenant) as s:
            cp = await get_checkpoint(s, tenant)
            assert cp is not None
            first = cp.state_mac

        assert await adopt_checkpoints([tenant]) == 0
        async with app_session(tenant) as s:
            cp = await get_checkpoint(s, tenant)
            assert cp is not None
            assert cp.state_mac == first

        async with app_session(tenant) as s:
            assert (await verify_incremental(s, tenant)).status == "ok"
    finally:
        get_settings.cache_clear()


async def test_adoption_refuses_to_run_on_an_unkeyed_deployment(
    app_session: AppSessionFactory,
) -> None:
    """Signing markers with the flag off would write evidence nothing checks
    and that the next keyed run would have to reject."""
    tenant = await _existing_unkeyed_tenant(app_session)
    with pytest.raises(RuntimeError):
        await adopt_checkpoints([tenant])


async def test_a_fresh_checkpoint_is_left_alone(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verified_count == 0 signs itself on its first clean pass, and the
    erasure branch never fires for it -- so there is nothing to adopt."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        cp = await verify_incremental(s, tenant)
        assert cp.verified_count == 0
    _enable_mac(monkeypatch)
    try:
        assert await adopt_checkpoints([tenant]) == 0
        async with app_session(tenant) as s:
            cp = await verify_incremental(s, tenant)
        assert cp.status == "ok"
    finally:
        get_settings.cache_clear()
