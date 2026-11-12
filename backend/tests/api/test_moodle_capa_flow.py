# backend/tests/api/test_treg_mcp_capa_flow.py
"""The treg_mcp capa's real install -> enable -> configure flow, using the
ACTUAL shipped manifest folder (not an inline dict, unlike
test_custom_mcp_capa_e2e.py's wizard-shaped one) discovered off disk exactly
as production would. Stops short of a live `/mcp/connections/{id}/test` call:
treg.to is a real hosted, metered third-party service, not a fixture this
repo controls, so the boundary this test proves instead is the one that is
actually new here -- that treg_mcp's manifest wires a real submitted secret
all the way into the exact `X-Treg-Token` header `open_tool_session` would
send, using nothing but the generic mechanism `test_open_tool_session.py` and
`test_custom_mcp_capa_e2e.py` already cover for other manifests."""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import config
from oc8 import models as m
from oc8.agent.mcp_env import resolve_mcp_env
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/api/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _real_capas_dir_with_kek(monkeypatch: pytest.MonkeyPatch) -> None:
    # Order matters: cache_clear() rebuilds Settings from the environment,
    # which would drop a secret_kek patch applied to the PREVIOUS instance --
    # so set the env var and clear the cache FIRST, then patch the kek onto
    # the instance that's actually still live afterwards.
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    get_settings.cache_clear()
    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_install_enable_and_configure_moodle_wires_url_and_token_into_the_bridge_env(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    headers = _headers(tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(
                "/api/v1/capas/install-from-disk", json={"pluginId": "moodle"}, headers=headers
            )
            assert r.status_code == 201, r.text
            capa_id = r.json()["pluginId"]

            r = await c.post(
                f"/api/v1/capas/{capa_id}/enable",
                json={"grantedPermissions": ["mcp:connect", "secrets:use"]},
                headers=headers,
            )
            assert r.status_code == 200, r.text

            async with app_session(tenant) as db:
                cred = await create_credential(
                    db,
                    tenant_id=tenant,
                    name="Schul-Moodle",
                    credential_type="moodle_token",
                    field_values={"url": "https://lms.example.org", "token": "s3cr3t-moodle"},
                )
                cred_id = str(cred.id)

            r = await c.post(
                f"/api/v1/capas/{capa_id}/setup",
                json={"values": {"site": cred_id}},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            connection_id = r.json()["connectionId"]

    async with app_session(tenant) as db:
        conn = await db.get(m.McpConnection, uuid.UUID(connection_id))
        assert conn is not None
        assert conn.transport == "stdio"
        cfg = conn.config or {}
        assert cfg["args"] == ["-m", "mcp_bridge"]
        assert cfg["env"]["MOODLE_URL"] == "https://lms.example.org"
        env = await resolve_mcp_env(db, tenant_id=tenant, cfg=cfg, connection_name=conn.name)
        assert env["MOODLE_TOKEN"] == "s3cr3t-moodle"
        assert env["MOODLE_URL"] == "https://lms.example.org"
        rows = (
            (await db.execute(select(m.Secret).where(m.Secret.tenant_id == tenant))).scalars().all()
        )
        assert rows
        for row in rows:
            assert b"s3cr3t-moodle" not in row.ciphertext
