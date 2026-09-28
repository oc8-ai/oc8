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
from oc8.agent.mcp_client import resolve_auth_header
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


async def test_install_enable_and_configure_treg_creates_an_http_connection_with_the_real_token(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    headers = _headers(tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # 1. install the REAL shipped manifest from disk
            r = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "treg_mcp"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            capa_id = r.json()["pluginId"]

            # 2. enable
            r = await c.post(
                f"/api/v1/capas/{capa_id}/enable",
                json={"grantedPermissions": ["mcp:connect", "secrets:use"]},
                headers=headers,
            )
            assert r.status_code == 200, r.text

            # 3. treg_mcp's own credential type is only reachable once the
            # plugin that declares it is enabled for this tenant -- create the
            # credential the setup form will reference, same as the browser's
            # CredentialPicker would.
            async with app_session(tenant) as db:
                cred = await create_credential(
                    db,
                    tenant_id=tenant,
                    name="Acme's treg account",
                    credential_type="treg_token",
                    field_values={"token": "s3cr3t-treg-token"},
                )
                cred_id = str(cred.id)

            # 4. configure with that credential
            r = await c.post(
                f"/api/v1/capas/{capa_id}/setup",
                json={"values": {"token": cred_id}},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            connection_id = r.json()["connectionId"]
            assert connection_id

    async with app_session(tenant) as db:
        conn = await db.get(m.McpConnection, uuid.UUID(connection_id))
        assert conn is not None
        assert conn.transport == "http"
        assert conn.server_url == "https://treg.to/mcp/v2/"
        assert conn.scopes == []
        assert conn.connected is False  # only a live connection test flips this
        cfg = conn.config or {}
        assert cfg["auth_header_name"] == "X-Treg-Token"

        # 5. the exact boundary the runtime executor and a live connection
        # test both cross before ever speaking MCP: resolve the stored secret
        # and build the real request header from it.
        env = await resolve_mcp_env(db, tenant_id=tenant, cfg=cfg, connection_name=conn.name)
        real_headers = resolve_auth_header(cfg, env)
        assert real_headers == {"X-Treg-Token": "s3cr3t-treg-token"}

        # The plaintext only ever exists transiently in `env`/`real_headers`
        # above -- what's actually persisted is encrypted ciphertext.
        secrets_rows = (
            await db.execute(select(m.Secret).where(m.Secret.tenant_id == tenant))
        ).scalars().all()
        assert secrets_rows
        for row in secrets_rows:
            assert b"s3cr3t-treg-token" not in row.ciphertext
