"""`needs-me` is a writable widget type (run step timeline, Task 16).

`PUT /dashboard/layout` validates `type` against a Literal, so a new widget
the frontend can render but the backend refuses to store is a tile that
vanishes on reload.
"""

from __future__ import annotations

import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _h(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def _put(tenant: uuid.UUID, widget_type: str) -> int:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            resp = await c.put(
                "/api/v1/dashboard/layout",
                json={
                    "widgets": [
                        {
                            "id": "w1",
                            "type": widget_type,
                            "x": 0,
                            "y": 0,
                            "w": 4,
                            "h": 5,
                            "config": {},
                        }
                    ]
                },
                headers=_h(tenant),
            )
            return resp.status_code


async def test_a_needs_me_tile_can_be_saved(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    assert await _put(tenant, "needs-me") == 200


async def test_an_unknown_type_is_still_refused(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    assert await _put(tenant, "needs-everything") == 422
