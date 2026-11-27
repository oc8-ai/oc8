"""`/settings/web-search` and `/chat/web-search` (spec §10): a tenant picks
its provider, the key is write-only, the platform default fills in without
ever showing its address or key, a tenant can switch search off, and one
tenant never sees another's setting."""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.config import get_settings
from oc8.main import create_app
from oc8.websearch.service import API_KEY_SECRET, resolve_config
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

_PLATFORM_URL = "http://searxng.cluster.internal:8080"
_PLATFORM_KEY = "platform-key-do-not-show"


def _headers(tenant: uuid.UUID, role: str = "org_admin") -> dict[str, str]:
    token = get_identity_provider().mint(tenant_id=tenant, subject="op", role=role)
    return {"Authorization": f"Bearer {token}"}


async def _org(app_session: AppSessionFactory) -> uuid.UUID:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        db.add(
            m.Organization(
                id=tenant,
                slug=f"t{tenant.hex[:8]}",
                name="T",
                tier="standard",
                region="eu",
                settings={},
            )
        )
        await db.flush()
    return tenant


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """The secret store needs its key-encryption key (see test_totp_endpoints)."""
    monkeypatch.setattr(
        get_settings(), "secret_kek", base64.b64encode(bytes(range(32))).decode(), raising=False
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    async with LifespanManager(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _platform(monkeypatch: pytest.MonkeyPatch, provider: str = "searxng") -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "web_search_provider", provider)
    monkeypatch.setattr(settings, "web_search_base_url", _PLATFORM_URL)
    monkeypatch.setattr(settings, "web_search_api_key", _PLATFORM_KEY)


async def _put(c: AsyncClient, tenant: uuid.UUID, body: dict[str, object]) -> tuple[int, str]:
    r = await c.put("/api/v1/settings/web-search", json=body, headers=_headers(tenant))
    return r.status_code, r.text


async def test_nothing_configured(app_session: AppSessionFactory, client: AsyncClient) -> None:
    tenant = await _org(app_session)
    got = await client.get("/api/v1/settings/web-search", headers=_headers(tenant))
    assert got.status_code == 200
    assert got.json() == {"provider": None, "baseUrl": None, "hasKey": False, "source": None}
    avail = await client.get("/api/v1/chat/web-search", headers=_headers(tenant))
    assert avail.json() == {"available": False}


@pytest.mark.parametrize("provider", ["searxng", "brave"])
async def test_the_platform_default_applies_and_never_shows_its_address_or_key(
    app_session: AppSessionFactory,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    _platform(monkeypatch, provider)
    tenant = await _org(app_session)
    got = await client.get("/api/v1/settings/web-search", headers=_headers(tenant))
    assert got.json() == {
        "provider": provider,
        "baseUrl": None,
        "hasKey": False,
        "source": "platform",
    }
    avail = await client.get("/api/v1/chat/web-search", headers=_headers(tenant))
    assert avail.json() == {"available": True}
    for text in (got.text, avail.text):
        assert _PLATFORM_URL not in text and "cluster.internal" not in text
        assert _PLATFORM_KEY not in text
    async with app_session(tenant) as db:
        config = await resolve_config(db, tenant_id=tenant)
    assert config is not None and config.provider == provider
    if provider == "searxng":
        # The operator's own host may be internal; nothing else is.
        assert config.internal_host == "searxng.cluster.internal"


async def test_a_tenant_key_is_stored_as_a_secret_and_never_returned(
    app_session: AppSessionFactory, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _platform(monkeypatch)
    tenant = await _org(app_session)
    code, text = await _put(client, tenant, {"provider": "brave", "apiKey": "brv-secret-1"})
    assert code == 200, text
    assert "brv-secret-1" not in text
    got = await client.get("/api/v1/settings/web-search", headers=_headers(tenant))
    assert got.json() == {"provider": "brave", "baseUrl": None, "hasKey": True, "source": "tenant"}
    assert "brv-secret-1" not in got.text
    async with app_session(tenant) as db:
        org = await db.get(m.Organization, tenant)
        assert org is not None and "brv-secret-1" not in str(org.settings)
        secret = (
            await db.execute(
                select(m.Secret).where(
                    m.Secret.tenant_id == tenant, m.Secret.name == API_KEY_SECRET
                )
            )
        ).scalar_one()
        assert b"brv-secret-1" not in bytes(secret.ciphertext)
        # The tenant's choice wins over the platform default.
        config = await resolve_config(db, tenant_id=tenant)
        assert config is not None and config.provider == "brave"
        assert config.api_key == "brv-secret-1"
        audit = (
            (
                await db.execute(
                    select(m.AuditEvent).where(
                        m.AuditEvent.tenant_id == tenant,
                        m.AuditEvent.action == "organization.settings.updated",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert audit and all("brv-secret-1" not in str(e.resource) for e in audit)

    # Saved again without a key: the stored one stays.
    code, _ = await _put(client, tenant, {"provider": "brave"})
    assert code == 200
    # Another keyed provider needs its own key.
    code, _ = await _put(client, tenant, {"provider": "tavily"})
    assert code == 422
    # SearXNG needs none: the stored key goes.
    code, text = await _put(
        client, tenant, {"provider": "searxng", "baseUrl": "https://search.example.org"}
    )
    assert code == 200, text
    got = await client.get("/api/v1/settings/web-search", headers=_headers(tenant))
    assert got.json() == {
        "provider": "searxng",
        "baseUrl": "https://search.example.org",
        "hasKey": False,
        "source": "tenant",
    }


async def test_invalid_choices_are_refused(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    tenant = await _org(app_session)
    for body in (
        {"provider": "brave"},
        {"provider": "searxng"},
        {"provider": "searxng", "baseUrl": "search.example.org"},
        {"provider": "searxng", "baseUrl": "ftp://search.example.org"},
        {"provider": "searxng", "baseUrl": "https://user:pw@search.example.org"},
        {"provider": "google", "apiKey": "x"},
    ):
        code, text = await _put(client, tenant, body)
        assert code == 422, (body, text)
    forbidden = await client.put(
        "/api/v1/settings/web-search",
        json={"provider": "off"},
        headers=_headers(tenant, "operator"),
    )
    assert forbidden.status_code == 403


@pytest.mark.parametrize(
    "base_url",
    [
        "http://[::1",
        "http://search.example.org:99999",
        "http://search.example.org:port",
        "https://",
        "https:///search",
        "https://search.example.org/../admin",
        "https://search.example.org/a/./b",
        "https://search.example.org/a%2e%2e/b",
        "https://search.example.org\\@evil.example/",
        "https://search.example.org/?q=1",
        "https://search.example.org/#x",
        "https://search.example.org/a b",
        "https://search.example.org/\x00",
    ],
)
async def test_a_searxng_address_that_is_not_a_plain_base_url_is_refused(
    app_session: AppSessionFactory, client: AsyncClient, base_url: str
) -> None:
    """Refused as 422 (never a 500): scheme, host and port must be readable,
    no login, no query or fragment, and a path of plain segments only."""
    tenant = await _org(app_session)
    code, text = await _put(client, tenant, {"provider": "searxng", "baseUrl": base_url})
    assert code == 422, (base_url, code, text)


@pytest.mark.parametrize(
    "base_url", ["https://search.example.org", "http://searxng:8080/", "https://x.example/searx/"]
)
async def test_a_plain_searxng_base_url_is_accepted(
    app_session: AppSessionFactory, client: AsyncClient, base_url: str
) -> None:
    tenant = await _org(app_session)
    code, text = await _put(client, tenant, {"provider": "searxng", "baseUrl": base_url})
    assert code == 200, text


async def test_a_tenant_can_switch_it_off_and_back_to_the_platform(
    app_session: AppSessionFactory, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _platform(monkeypatch)
    tenant = await _org(app_session)
    code, _ = await _put(client, tenant, {"provider": "off"})
    assert code == 200
    got = await client.get("/api/v1/settings/web-search", headers=_headers(tenant))
    assert got.json() == {"provider": "off", "baseUrl": None, "hasKey": False, "source": None}
    avail = await client.get("/api/v1/chat/web-search", headers=_headers(tenant))
    assert avail.json() == {"available": False}
    code, _ = await _put(client, tenant, {"provider": None})
    assert code == 200
    got = await client.get("/api/v1/settings/web-search", headers=_headers(tenant))
    assert got.json()["source"] == "platform"


async def test_another_tenants_setting_is_invisible(
    app_session: AppSessionFactory, client: AsyncClient
) -> None:
    first = await _org(app_session)
    second = await _org(app_session)
    code, _ = await _put(client, first, {"provider": "tavily", "apiKey": "tvly-first"})
    assert code == 200
    got = await client.get("/api/v1/settings/web-search", headers=_headers(second))
    assert got.json() == {"provider": None, "baseUrl": None, "hasKey": False, "source": None}
    avail = await client.get("/api/v1/chat/web-search", headers=_headers(second))
    assert avail.json() == {"available": False}
    async with app_session(second) as db:
        assert await resolve_config(db, tenant_id=second) is None


async def test_the_web_switch_lands_in_the_turns_run_context(
    app_session: AppSessionFactory, client: AsyncClient, redis_url: str
) -> None:
    """`web: true` on a sent, regenerated or edited turn sets `web_search` on
    the run that answers it; without it (or on a regenerate without a body)
    the run has none."""
    from unittest.mock import patch

    from oc8.chat.service import record_assistant_reply

    tenant = await _org(app_session)
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="Ops", frame={})
        db.add(dept)
        await db.flush()
        agent = m.Agent(tenant_id=tenant, department_id=dept.id, name="A")
        db.add(agent)
        await db.flush()
        agent_id = agent.id

    async def run_context(run_id: str, *, finish: bool = True) -> dict[str, object]:
        async with app_session(tenant) as db:
            run = await db.get(m.AgentRun, uuid.UUID(run_id))
            assert run is not None
            context = dict(run.context or {})
            if finish:
                run.state = "done"
                await record_assistant_reply(db, run=run, output="ok")
        return context

    h = _headers(tenant)
    created = await client.post("/api/v1/chat/sessions", json={"agentId": str(agent_id)}, headers=h)
    base = f"/api/v1/chat/sessions/{created.json()['id']}/messages"
    with patch("oc8.runtime.intake.publish_run"):
        on = (await client.post(base, json={"message": "news?", "web": True}, headers=h)).json()
        assert (await run_context(on["runId"])).get("web_search") is True
        off = (await client.post(base, json={"message": "again"}, headers=h)).json()
        assert "web_search" not in await run_context(off["runId"])
        regen = await client.post(f"{base}/{off['id']}/regenerate", json={"web": True}, headers=h)
        assert regen.status_code == 201, regen.text
        assert (await run_context(regen.json()["runId"])).get("web_search") is True
        bare = await client.post(f"{base}/{on['id']}/regenerate", headers=h)
        assert bare.status_code == 201, bare.text
        assert "web_search" not in await run_context(bare.json()["runId"])
        edited = await client.post(
            f"{base}/{on['id']}/edit", json={"content": "news today?", "web": True}, headers=h
        )
        assert edited.status_code == 201, edited.text
        assert (await run_context(edited.json()["runId"])).get("web_search") is True
