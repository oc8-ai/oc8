"""The caller's capabilities in `GET /api/v1/governance`.

An embedding deployment may resolve entitlements per organisation by registering
a resolver; without one the answer is unknown (null) and nothing changes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import Principal, get_identity_provider
from oc8.licensing import (
    ALL_CAPABILITIES,
    CommunityEntitlements,
    get_entitlements_resolver,
    set_entitlements_resolver,
)
from oc8.licensing.capabilities import ENTERPRISE_SSO_SAML, ENTERPRISE_SUPERVISION_ADVANCED
from oc8.main import create_app


class _Granting:
    def __init__(self, *granted: str) -> None:
        self._granted = set(granted)

    def has(self, capability: str) -> bool:
        return capability in self._granted

    def limit(self, name: str) -> int | None:
        return None


@pytest.fixture(autouse=True)
def _reset_resolver() -> Iterator[None]:
    set_entitlements_resolver(None)
    yield
    set_entitlements_resolver(None)


async def _capabilities() -> object:
    token = get_identity_provider().mint(tenant_id=uuid.uuid4(), subject="s", role="operator")
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/governance", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    return r.json()["callerCapabilities"]


def test_all_capabilities_holds_both_constants() -> None:
    assert ALL_CAPABILITIES == frozenset({ENTERPRISE_SUPERVISION_ADVANCED, ENTERPRISE_SSO_SAML})


def test_the_resolver_seam_sets_and_clears() -> None:
    async def resolver(principal: Principal) -> CommunityEntitlements:
        return CommunityEntitlements()

    set_entitlements_resolver(resolver)
    assert get_entitlements_resolver() is resolver
    set_entitlements_resolver(None)
    assert get_entitlements_resolver() is None


async def test_without_a_resolver_the_capabilities_are_unknown() -> None:
    assert await _capabilities() is None


async def test_a_resolver_granting_one_capability_reports_it() -> None:
    async def resolver(principal: Principal) -> _Granting:
        return _Granting(ENTERPRISE_SUPERVISION_ADVANCED)

    set_entitlements_resolver(resolver)
    assert await _capabilities() == [ENTERPRISE_SUPERVISION_ADVANCED]


async def test_a_resolver_that_raises_reports_none() -> None:
    async def resolver(principal: Principal) -> _Granting:
        raise RuntimeError("entitlement service down")

    set_entitlements_resolver(resolver)
    assert await _capabilities() == []
