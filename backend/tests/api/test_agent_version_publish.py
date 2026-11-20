"""`POST /agents/{id}/versions`.

Two gates and two refusals. The gates are `agent_version:publish` tenant-wide
(the route dependency) and `authorize_agent_write`'s department narrow (in the
body, first, per `agents_write.py`'s module docstring). The refusals are both
409s and are deliberately distinguishable: "your view is stale, refetch" and
"there is nothing to publish, change something", which are opposite
instructions to give a client.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from oc8 import models as m
from oc8.agents.publish_hooks import (
    _reset_publish_hooks_for_tests,
    register_publish_hook,
)
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def _hire(client: AsyncClient, headers: dict[str, str]) -> str:
    dept = await client.post(
        "/api/v1/departments",
        json={"name": f"D-{uuid.uuid4().hex}", "icon": "Bot"},
        headers=headers,
    )
    assert dept.status_code in (200, 201), dept.text
    created = await client.post(
        "/api/v1/agents",
        json={
            "departmentId": dept.json()["id"],
            "name": f"A-{uuid.uuid4().hex[:8]}",
            "roleTitle": "Tester",
            "mission": "original",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


@pytest.fixture(autouse=True)
def _clean_hooks() -> Iterator[None]:
    """Empty on BOTH sides. The registry is a process-global, so a hook a test
    here registers would otherwise still be live in the next test module and
    veto every `POST /agents` there (which publishes v1)."""
    _reset_publish_hooks_for_tests()
    yield
    _reset_publish_hooks_for_tests()


async def test_publishing_an_edited_agent_creates_v2(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "changed"},
                headers=headers,
            )
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"note": "tightened the mission", "expectedCurrentVersionNo": 1},
                headers=headers,
            )
            assert res.status_code == 201, res.text
            body = res.json()
            assert body["versionNo"] == 2
            assert body["isCurrent"] is True
            assert body["note"] == "tightened the mission"
            assert body["payload"]["mission"] == "changed"
            assert body["rolledBackFrom"] is None

            status_ = await client.get(f"/api/v1/agents/{agent_id}/draft-status", headers=headers)
            assert status_.json() == {
                "dirty": False,
                "changedFields": [],
                "currentVersionNo": 2,
            }


async def test_publishing_nothing_is_refused_with_409(
    app_session: AppSessionFactory,
) -> None:
    """Spec §2.7. An empty version would trigger a compliance re-check and an
    eval run over a configuration that did not change."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=headers,
            )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"] == "no_changes_to_publish"


async def test_a_stale_expected_version_is_refused_with_409(
    app_session: AppSessionFactory,
) -> None:
    """The optimistic check spec §2.2 promised. Two editors share one draft; this
    is what makes the second one find out rather than silently win."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "changed"},
                headers=headers,
            )
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 7},
                headers=headers,
            )
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["error"] == "stale_version"
    assert detail["expected"] == 7
    assert detail["current"] == 1


async def test_staleness_is_checked_before_the_no_op_check(
    app_session: AppSessionFactory,
) -> None:
    """Opposite instructions, so the order matters. A client whose view is stale
    AND whose draft is clean must be told to refetch -- "nothing to publish"
    would send it away still holding a stale version number."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 99},
                headers=headers,
            )
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["error"] == "stale_version"


async def test_publishing_appends_an_audit_event(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            agent_id = await _hire(client, headers)
            await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "changed"},
                headers=headers,
            )
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=headers,
            )
            assert res.status_code == 201, res.text

    async with app_session(tenant) as db:
        # Scoped to THIS agent: the tenant is shared across the suite.
        events = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.action == "agent.version.published",
                        m.AuditEvent.resource["agent_id"].astext == agent_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    # Two: `create_agent`'s v1 and the explicit publish of v2.
    assert len(events) == 2
    assert {int(e.resource["version_no"]) for e in events} == {1, 2}


async def test_an_operator_may_not_publish(app_session: AppSessionFactory) -> None:
    """`operator` puts work into the office and must not re-configure it.
    `agent_version:publish` is in `_DEPT_MANAGER` and not in `_OPERATOR`, so the
    route dependency refuses before the body runs."""
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            agent_id = await _hire(client, {"Authorization": f"Bearer {_token(tenant)}"})
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers={"Authorization": f"Bearer {_token(tenant, 'operator')}"},
            )
    assert res.status_code == 403, res.text


async def test_a_refusing_hook_fails_the_request_and_leaves_no_version(
    app_session: AppSessionFactory,
) -> None:
    """The gate, through HTTP. `get_db` rolls back on any exception, so the 422
    must leave `current_version_id` and the row count exactly as they were."""

    async def refuse(_db: object, _version: object) -> None:
        raise RuntimeError("eval suite has not passed")

    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            # Registered AFTER the hire. `create_agent` publishes v1 in the same
            # transaction, so a hook registered before this line would refuse
            # the hire itself and this test would read as if the publish
            # endpoint were broken.
            agent_id = await _hire(client, headers)
            register_publish_hook("evals", refuse)
            await client.patch(
                f"/api/v1/agents/{agent_id}/instructions",
                json={"instructions": "changed"},
                headers=headers,
            )
            res = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                json={"expectedCurrentVersionNo": 1},
                headers=headers,
            )
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["error"] == "publish_hook_rejected"
    assert detail["hook"] == "evals"
    assert "eval suite has not passed" in detail["reason"]

    async with app_session(tenant) as db:
        total = (
            await db.execute(
                select(func.count())
                .select_from(m.AgentVersion)
                .where(m.AgentVersion.agent_id == uuid.UUID(agent_id))
            )
        ).scalar_one()
    assert total == 1
