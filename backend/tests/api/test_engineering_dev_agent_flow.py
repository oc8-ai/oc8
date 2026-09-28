"""engineering_dev_agent's real install -> enable -> setup -> hire flow,
using the ACTUAL shipped manifest folder discovered off disk exactly as
production would (same pattern as test_treg_mcp_capa_flow.py). Proves the
two things new here: plugin_depends actually installs github_mcp alongside
it, and the setup form's repo/label values actually reach the hired
agent's mission, role_title, and trigger via the generic substitution
mechanism (capas/service.py)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# tests/api/<this> -> tests -> backend -> repo root, where capas/ lives.
_PLUGINS_DIR = Path(__file__).resolve().parents[3] / "capas"


@pytest.fixture(autouse=True)
def _real_capas_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_PLUGINS_DIR))
    get_settings.cache_clear()


def _headers(tenant: uuid.UUID) -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role="org_admin")
    return {"Authorization": f"Bearer {token}"}


async def test_install_enable_setup_and_hire_wires_the_repo_and_label_into_the_agent(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    headers = _headers(tenant)

    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # 1. install the REAL shipped manifest -- pulls in github_mcp as
            # a plugin_depends dependency.
            r = await c.post(
                "/api/v1/capas/install-from-disk",
                json={"pluginId": "engineering_dev_agent"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            capa_id = r.json()["pluginId"]

            # 2. enable (this capa itself declares no permissions to grant)
            r = await c.post(
                f"/api/v1/capas/{capa_id}/enable",
                json={"grantedPermissions": []},
                headers=headers,
            )
            assert r.status_code == 200, r.text

            # 3. submit the setup form
            r = await c.post(
                f"/api/v1/capas/{capa_id}/setup",
                json={
                    "values": {
                        "repo": "acme/widgets",
                        "label": "agent-ready",
                        "base_branch": "",
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["connectionId"] is None  # no [plugin.setup.mcp] -- nothing to connect

            # 4. hire -- instantiate the department
            r = await c.post(
                f"/api/v1/capas/{capa_id}/instantiate-department",
                json={"name": "Engineering (acme/widgets)"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            dept_id = uuid.UUID(r.json()["id"])

    async with app_session(tenant) as db:
        capa_names = {
            row.name
            for row in (
                await db.execute(select(m.Capa).where(m.Capa.tenant_id == tenant))
            ).scalars()
        }
        assert capa_names == {"engineering_dev_agent", "github_mcp"}

        agent = (
            await db.execute(select(m.Agent).where(m.Agent.department_id == dept_id))
        ).scalar_one()
        assert "acme/widgets" in agent.mission
        assert "agent-ready" in agent.mission
        assert "{{repo}}" not in agent.mission
        assert "{{label}}" not in agent.mission

        dept = await db.get(m.Department, dept_id)
        assert dept is not None
        assert dept.frame["tools"]["github"]["approval_actions"] == ["merge_pull_request"]

        trigger = (
            await db.execute(select(m.Trigger).where(m.Trigger.agent_id == agent.id))
        ).scalar_one()
        assert (
            trigger.task_text
            == "Check acme/widgets for the oldest open issue labeled agent-ready and work it."
        )
        assert trigger.cron_expression == "*/20 * * * *"
        assert trigger.next_run_at is not None
