"""Edition routers are explicitly composed and inherit operator API guards."""

from __future__ import annotations

import uuid

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from oc8.auth import get_identity_provider
from oc8.edition import EditionExtension
from oc8.edition.runtime import RuntimeComposition
from oc8.main import create_app
from oc8.runtime.registry import agent_has_supervision
from oc8.runtime.supervision_hook import NOOP_SUPERVISION_RUN_HOOK

pytestmark = pytest.mark.asyncio


class _DummyEdition(EditionExtension):
    def routers(self) -> list[APIRouter]:
        router = APIRouter()

        @router.get("/edition-test")
        async def edition_test() -> dict[str, str]:
            return {"edition": "dummy"}

        return [router]


class _TrueQueryPort:
    async def has_supervision(self, db: object, *, agent_id: uuid.UUID) -> bool:
        return True


class _QueryProbeEdition(EditionExtension):
    def routers(self) -> list[APIRouter]:
        router = APIRouter()

        @router.get("/edition-query-probe")
        async def edition_query_probe() -> dict[str, bool]:
            value = await agent_has_supervision(
                None,  # type: ignore[arg-type]
                agent_id=uuid.uuid4(),
            )
            return {"hasSupervision": value}

        return [router]


class _ServiceEdition(EditionExtension):
    def routers(self) -> list[APIRouter]:
        return []

    def service_routers(self) -> list[APIRouter]:
        router = APIRouter()

        @router.get("/edition-service-test")
        async def edition_service_test() -> dict[str, str]:
            return {"edition": "service"}

        return [router]


async def test_service_router_is_mounted_without_any_auth_header() -> None:
    app = create_app(edition_extensions=[_ServiceEdition()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/edition-service-test")

    assert response.status_code == 200
    assert response.json() == {"edition": "service"}


async def test_default_extension_contributes_no_service_routers() -> None:
    """`_DummyEdition` überschreibt service_routers() nie -- der No-Op-Default
    des Protocols muss über normale nominale Vererbung greifen, statt die
    Mount-Schleife mit AttributeError abstürzen zu lassen."""
    app = create_app(edition_extensions=[_DummyEdition()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/edition-test")

    assert response.status_code == 401  # unverändert: routers()-Pfad weiterhin geschützt


async def test_default_app_does_not_mount_edition_routes() -> None:
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/edition-test")

    assert response.status_code == 404


async def test_unauthenticated_supervision_policies_are_rejected() -> None:
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/supervision/policies")

    assert response.status_code == 401


async def test_extension_route_is_mounted_under_operator_api() -> None:
    token = get_identity_provider().mint(
        tenant_id=uuid.uuid4(), subject="operator:test", role="org_admin", kind="operator"
    )
    app = create_app(edition_extensions=[_DummyEdition()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/edition-test", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 200
    assert response.json() == {"edition": "dummy"}


async def test_extension_route_requires_an_authenticated_principal() -> None:
    app = create_app(edition_extensions=[_DummyEdition()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/edition-test")

    assert response.status_code == 401


async def test_extension_route_refuses_agent_principal() -> None:
    token = get_identity_provider().mint(
        tenant_id=uuid.uuid4(),
        subject="agent:test",
        role="agent_default",
        kind="agent",
        scopes=[f"run:{uuid.uuid4()}"],
    )
    app = create_app(edition_extensions=[_DummyEdition()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/edition-test", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 403


async def test_extension_route_refuses_totp_pending_principal() -> None:
    """Fix round 1, finding 2 (HIGH): the edition-extensions mount used to
    carry only `deny_agent_principals` + `get_principal` -- and
    `require_permission`/`require_departmental` on Enterprise routes resolve
    authority from the DB by role/subject, never reading `principal.scopes`.
    A `totp:challenge` token (password proven, second factor still
    outstanding) could therefore reach an Enterprise route just because it is
    a structurally valid, verifiable token. `deny_totp_pending_principals` is
    now on this mount too, same as `api_router`'s."""
    token = get_identity_provider().mint(
        tenant_id=uuid.uuid4(),
        subject="pending@example.com",
        role="org_admin",
        scopes=["totp:challenge"],
    )
    app = create_app(edition_extensions=[_DummyEdition()])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/edition-test", headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 403
    assert "challenge" in response.json()["detail"].lower()


async def test_request_scopes_the_edition_query_port() -> None:
    token = get_identity_provider().mint(
        tenant_id=uuid.uuid4(), subject="operator:test", role="org_admin", kind="operator"
    )
    app = create_app(
        edition_extensions=[_QueryProbeEdition()],
        runtime_composition=RuntimeComposition(
            NOOP_SUPERVISION_RUN_HOOK,
            _TrueQueryPort(),
        ),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/edition-query-probe",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {"hasSupervision": True}
