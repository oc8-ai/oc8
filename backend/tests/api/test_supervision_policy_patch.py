"""PATCH /supervision/policies/{policy_id}: tunable judge knobs."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.main import create_app

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(tenant, role)}"}


@asynccontextmanager
async def _http() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            yield client


async def _create_policy(client: AsyncClient, tenant: uuid.UUID) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/supervision/policies",
        json={
            "departmentId": str(uuid.uuid4()),
            "checkpointEvery": {"tool_calls": 5},
            "driftThresholds": {"steer": 0.35, "rewind": 0.55, "escalate": 0.75},
            "allowedInterventions": ["steer", "rewind"],
            "samplingRate": 1.0,
        },
        headers=_headers(tenant),
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_patch_updates_sampling_rate_and_rubric() -> None:
    tenant = uuid.uuid4()
    async with _http() as client:
        policy = await _create_policy(client, tenant)

        r = await client.patch(
            f"/api/v1/supervision/policies/{policy['id']}",
            json={"samplingRate": 0.25, "judgeRubric": {"text": "strict"}},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["samplingRate"] == pytest.approx(0.25)
        assert body["judgeRubric"] == {"text": "strict"}


async def test_partial_patch_leaves_other_fields_unchanged() -> None:
    tenant = uuid.uuid4()
    async with _http() as client:
        policy = await _create_policy(client, tenant)
        assert policy["checkpointEvery"] == {"tool_calls": 5}
        assert policy["allowedInterventions"] == ["steer", "rewind"]

        r = await client.patch(
            f"/api/v1/supervision/policies/{policy['id']}",
            json={"samplingRate": 0.5},
            headers=_headers(tenant),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["samplingRate"] == pytest.approx(0.5)
        assert body["checkpointEvery"] == {"tool_calls": 5}
        assert body["allowedInterventions"] == ["steer", "rewind"]
        assert body["driftThresholds"] == {"steer": 0.35, "rewind": 0.55, "escalate": 0.75}


async def test_patch_unknown_policy_404() -> None:
    tenant = uuid.uuid4()
    async with _http() as client:
        r = await client.patch(
            f"/api/v1/supervision/policies/{uuid.uuid4()}",
            json={"samplingRate": 0.5},
            headers=_headers(tenant),
        )
        assert r.status_code == 404, r.text


async def test_patch_cross_tenant_policy_404() -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    async with _http() as client:
        policy = await _create_policy(client, tenant_a)

        r = await client.patch(
            f"/api/v1/supervision/policies/{policy['id']}",
            json={"samplingRate": 0.5},
            headers=_headers(tenant_b),
        )
        assert r.status_code == 404, r.text


async def test_patch_forbidden_for_insufficient_role() -> None:
    tenant = uuid.uuid4()
    async with _http() as client:
        policy = await _create_policy(client, tenant)

        r = await client.patch(
            f"/api/v1/supervision/policies/{policy['id']}",
            json={"samplingRate": 0.5},
            headers=_headers(tenant, role="member"),
        )
        assert r.status_code == 403, r.text
