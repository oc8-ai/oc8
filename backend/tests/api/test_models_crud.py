from __future__ import annotations

import base64
import uuid

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8 import config
from oc8 import models as m
from oc8.auth import get_identity_provider
from oc8.constants import ACME_TENANT_ID
from oc8.credentials.service import create_credential
from oc8.main import create_app
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


def _token(tenant: uuid.UUID, role: str = "org_admin") -> str:
    return get_identity_provider().mint(tenant_id=tenant, subject="u", role=role)


async def test_list_providers_returns_every_canonical() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get(
                "/api/v1/models/providers",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 200, r.text
            by_canonical = {p["canonical"]: p["locality"] for p in r.json()}
            assert by_canonical == {
                "anthropic": "cloud",
                "openai": "cloud",
                "openai_compatible": "cloud",
                # The ChatGPT-subscription provider is listed for every tenant:
                # it is authorised by a per-connection login, not an env key.
                "openai_chatgpt": "cloud",
                "ollama": "local",
                "auto": "cloud",
            }
            assert all("available" in p for p in r.json())
            # ollama is local -> always available regardless of env keys
            ollama = next(p for p in r.json() if p["canonical"] == "ollama")
            assert ollama["available"] is True


async def test_list_providers_requires_auth() -> None:
    # Consistency with every other catalog GET: no bearer token -> 401. Without a
    # principal dependency the handler would answer unauthenticated requests.
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/models/providers")
            assert r.status_code == 401, r.text


async def test_list_providers_honors_a_tenant_byok_key(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression test: `available` used to only ever check the platform-wide
    # env key, so a tenant using their own key still saw "key missing". A
    # fresh tenant (never the shared ACME_TENANT_ID -- this asserts on a
    # clean "no key yet" starting point) creates its own anthropic completion
    # key (the unified credentials framework, Task 14 -- `resolve_model_key`
    # reads a `Credential` of type `anthropic_api_key`, not a bare secret) and
    # must see `available: True` even with the platform key unset.
    monkeypatch.setattr(config.get_settings(), "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )
    tenant = uuid.uuid4()

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}

            before = await client.get("/api/v1/models/providers", headers=headers)
            anthropic_before = next(p for p in before.json() if p["canonical"] == "anthropic")
            assert anthropic_before["available"] is False

            async with app_session(tenant) as db:
                await create_credential(
                    db,
                    tenant_id=tenant,
                    name="Prod Anthropic",
                    credential_type="anthropic_api_key",
                    field_values={"api_key": "sk-tenant"},
                )

            after = await client.get("/api/v1/models/providers", headers=headers)
            anthropic_after = next(p for p in after.json() if p["canonical"] == "anthropic")
            assert anthropic_after["available"] is True


async def test_create_and_patch_model_config() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={
                    "provider": "ollama",
                    "model": "llama3.1:8b",
                    "locality": "local",
                    "displayName": "Local Llama",
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["model"] == "llama3.1:8b"
            assert body["provider"] == "ollama"
            assert body["locality"] == "local"
            assert body["displayName"] == "Local Llama"
            model_id = body["id"]

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={"provider": "ollama", "model": "mistral:latest", "locality": "local"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            patched = r.json()
            assert patched["model"] == "mistral:latest"
            assert patched["provider"] == "ollama"
            assert patched["locality"] == "local"
            assert patched["id"] == model_id
            # omitted on PATCH -> preserved, not nulled
            assert patched["displayName"] == "Local Llama"


async def test_patch_sets_and_clears_max_tokens() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "llama3.1:8b", "locality": "local"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            model_id = r.json()["id"]
            assert r.json()["maxTokens"] is None

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "ollama",
                    "model": "llama3.1:8b",
                    "locality": "local",
                    "maxTokens": 8192,
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["maxTokens"] == 8192

            # Omitted on a later PATCH -> preserved, not nulled.
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={"provider": "ollama", "model": "llama3.1:8b", "locality": "local"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["maxTokens"] == 8192

            # Explicit 0 -> cleared back to "use the framework default".
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "ollama",
                    "model": "llama3.1:8b",
                    "locality": "local",
                    "maxTokens": 0,
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["maxTokens"] is None


async def test_create_model_persists_supports_vision() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={
                    "provider": "ollama",
                    "model": "llama3.1:8b",
                    "locality": "local",
                    "supportsVision": True,
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["supportsVision"] is True


async def test_patch_sets_and_clears_supports_vision() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "llama3.1:8b", "locality": "local"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            model_id = r.json()["id"]
            assert r.json()["supportsVision"] is False

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "ollama",
                    "model": "llama3.1:8b",
                    "locality": "local",
                    "supportsVision": True,
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["supportsVision"] is True

            # Omitted on a later PATCH -> preserved, not cleared.
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={"provider": "ollama", "model": "llama3.1:8b", "locality": "local"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["supportsVision"] is True

            # Explicit false -> cleared back to "not vision-capable".
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "ollama",
                    "model": "llama3.1:8b",
                    "locality": "local",
                    "supportsVision": False,
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["supportsVision"] is False


async def test_create_model_accepts_effort() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "locality": "cloud",
                    "effort": "high",
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["effort"] == "high"


async def test_patch_sets_and_clears_effort() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={"provider": "anthropic", "model": "claude-sonnet-5", "locality": "cloud"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            model_id = r.json()["id"]
            assert r.json()["effort"] is None

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "locality": "cloud",
                    "effort": "medium",
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["effort"] == "medium"

            # Omitted on a later PATCH -> preserved, not nulled.
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={"provider": "anthropic", "model": "claude-sonnet-5", "locality": "cloud"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["effort"] == "medium"

            # Explicit blank -> cleared.
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "locality": "cloud",
                    "effort": "",
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["effort"] is None


async def test_create_model_persists_extra_params() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "locality": "cloud",
                    "extra": {"top_p": 0.9},
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["extra"] == {"top_p": 0.9}


async def test_patch_sets_and_clears_extra_params() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={"provider": "anthropic", "model": "claude-sonnet-5", "locality": "cloud"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            model_id = r.json()["id"]
            assert r.json()["extra"] is None

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "locality": "cloud",
                    "extra": {"top_p": 0.9},
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["extra"] == {"top_p": 0.9}

            # Omitted on a later PATCH -> preserved, not nulled.
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={"provider": "anthropic", "model": "claude-sonnet-5", "locality": "cloud"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["extra"] == {"top_p": 0.9}

            # Explicit empty dict -> cleared.
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "locality": "cloud",
                    "extra": {},
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["extra"] is None


async def test_create_model_canonicalizes_provider_alias() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={
                    "provider": "claude",
                    "model": "claude-3-5-sonnet-20241022",
                    "locality": "cloud",
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["provider"] == "anthropic"  # alias "claude" -> canonical


async def test_patch_foreign_tenant_config_returns_404() -> None:
    owner = uuid.UUID(str(ACME_TENANT_ID))
    other = uuid.uuid4()  # a different tenant; its org_admin token still cannot reach owner's rows
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "llama3.1:8b", "locality": "local"},
                headers={"Authorization": f"Bearer {_token(owner)}"},
            )
            assert r.status_code == 201, r.text
            model_id = r.json()["id"]

            # a different tenant's admin tries to patch it -> RLS hides the row -> 404
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={"provider": "ollama", "model": "mistral:latest", "locality": "local"},
                headers={"Authorization": f"Bearer {_token(other)}"},
            )
            assert r.status_code == 404, r.text


async def test_create_model_rejects_unknown_provider_and_bad_locality() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={"provider": "bogus", "model": "x"},
                headers=headers,
            )
            assert r.status_code == 422, r.text

            r = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "x", "locality": "weird"},
                headers=headers,
            )
            assert r.status_code == 422, r.text


async def test_create_auto_model_with_tiers(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    async with app_session(tenant) as db:
        fast = m.ModelConfig(
            tenant_id=tenant,
            provider="ollama",
            model="tiny",
            locality="local",
            display_name="Tiny",
        )
        strong = m.ModelConfig(
            tenant_id=tenant,
            provider="anthropic",
            model="opus",
            locality="cloud",
            display_name="Opus",
        )
        db.add_all([fast, strong])
        await db.flush()
        fast_id, strong_id = str(fast.id), str(strong.id)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={
                    "provider": "auto",
                    "model": "router",
                    "locality": "cloud",
                    "displayName": "Auto Router",
                    "autoTiers": {"fast": fast_id, "strong": strong_id},
                    "autoShadowOnly": True,
                    "autoCascadeVerify": True,
                    "autoPreferenceRouter": True,
                    "autoPreferenceExamples": [
                        {"text": "security audit", "needs_strong": True},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["provider"] == "auto"
            assert body["autoTiers"] == {"fast": fast_id, "strong": strong_id}
            assert body["autoShadowOnly"] is True
            assert body["autoCascadeVerify"] is True
            assert body["autoPreferenceRouter"] is True
            assert body["autoPreferenceExamples"] == [
                {"text": "security audit", "needs_strong": True},
            ]

            # Nested auto target rejected
            nested = await client.post(
                "/api/v1/models",
                json={
                    "provider": "auto",
                    "locality": "cloud",
                    "autoTiers": {"fast": body["id"]},
                },
                headers=headers,
            )
            assert nested.status_code == 422, nested.text

            # Missing tiers rejected
            missing = await client.post(
                "/api/v1/models",
                json={"provider": "auto", "locality": "cloud"},
                headers=headers,
            )
            assert missing.status_code == 422, missing.text


async def test_patch_missing_model_returns_404() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.patch(
                f"/api/v1/models/{uuid.uuid4()}",
                json={"provider": "ollama", "model": "x"},
                headers=headers,
            )
            assert r.status_code == 404, r.text


async def test_create_model_forbidden_for_non_admin() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant, role='member')}"}
            r = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "x"},
                headers=headers,
            )
            assert r.status_code == 403, r.text


async def test_delete_unassigned_model_returns_204() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            created = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "todelete:latest", "locality": "local"},
                headers=headers,
            )
            assert created.status_code == 201, created.text
            model_id = created.json()["id"]
            r = await client.delete(f"/api/v1/models/{model_id}", headers=headers)
            assert r.status_code == 204, r.text
            # gone: a second delete -> 404
            r2 = await client.delete(f"/api/v1/models/{model_id}", headers=headers)
            assert r2.status_code == 404, r2.text


async def test_delete_missing_model_returns_404() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.delete(
                f"/api/v1/models/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 404, r.text


async def test_delete_model_forbidden_for_non_admin() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.delete(
                f"/api/v1/models/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {_token(tenant, role='member')}"},
            )
            assert r.status_code == 403, r.text


async def test_create_and_patch_model_config_bind_credential_id(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ModelConfigs of the same provider must be able to bind two
    DIFFERENT credentials (the "connect multiple accounts" feature) -- the
    field must round-trip through create, the DTO, and patch."""
    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred_a = await create_credential(
            db,
            tenant_id=tenant,
            name="Account A",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-a"},
        )
        cred_b = await create_credential(
            db,
            tenant_id=tenant,
            name="Account B",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-b"},
        )

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.post(
                "/api/v1/models",
                json={
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet-20241022",
                    "locality": "cloud",
                    "credentialId": str(cred_a.id),
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["credentialId"] == str(cred_a.id)
            model_id = r.json()["id"]

            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet-20241022",
                    "credentialId": str(cred_b.id),
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["credentialId"] == str(cred_b.id)

            # unbinding: an explicit null clears it back to the tenant-wide
            # convention rather than being ignored as "not provided".
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet-20241022",
                    "credentialId": None,
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["credentialId"] is None


async def test_delete_assigned_model_returns_409(app_session: AppSessionFactory) -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    # The test DB carries no seed data, so create the department directly
    # (mirrors tests/api/test_contracts_endpoint.py's pattern).
    async with app_session(tenant) as db:
        dept = m.Department(tenant_id=tenant, name="delete-assigned-model-dept")
        db.add(dept)
        await db.flush()
        dept_id = str(dept.id)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            cfg = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "assigned:latest", "locality": "local"},
                headers=headers,
            )
            model_id = cfg.json()["id"]
            agent = await client.post(
                "/api/v1/agents",
                json={
                    "name": "Assigner",
                    "departmentId": dept_id,
                    "roleTitle": "t",
                    "mission": "m",
                    "modelConfigId": model_id,
                },
                headers=headers,
            )
            assert agent.status_code == 201, agent.text
            r = await client.delete(f"/api/v1/models/{model_id}", headers=headers)
            assert r.status_code == 409, r.text


# ------------------------------------------------------- POST /models/discover


async def test_discover_uses_the_credentials_key_and_base_url(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )
    seen: dict[str, object] = {}

    async def fake_get(
        self: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None, **kw: object
    ) -> httpx.Response:
        seen["url"] = url
        seen["headers"] = headers
        return httpx.Response(
            200,
            json={"data": [{"id": "mistral-large"}, {"id": "codestral"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="opaas ai",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-discover", "base_url": "https://opaas.cloud/v1"},
        )

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/models/discover",
                json={"provider": "openai_compatible", "credentialId": str(cred.id)},
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["models"] == ["codestral", "mistral-large"]
            assert seen["url"] == "https://opaas.cloud/v1/models"
            assert seen["headers"]["Authorization"] == "Bearer sk-discover"  # type: ignore[index]


async def test_discover_without_a_credential_id_falls_back_to_the_bound_one(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )

    async def fake_get(
        self: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None, **kw: object
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-3-5-sonnet-20241022"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/models/discover",
                json={"provider": "anthropic"},
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["models"] == ["claude-3-5-sonnet-20241022"]


async def test_discover_reports_the_providers_own_error_message(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            # No credential at all and no platform key -> DiscoveryError, "key
            # is required" -- surfaced as 400, not a 500 or a silent empty list.
            r = await client.post(
                "/api/v1/models/discover",
                json={"provider": "anthropic"},
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 400, r.text
            assert "key is required" in r.json()["detail"]


async def test_discover_rejects_unknown_provider() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/models/discover",
                json={"provider": "bogus"},
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 422, r.text


async def test_discover_forbidden_for_non_admin() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                "/api/v1/models/discover",
                json={"provider": "anthropic"},
                headers={"Authorization": f"Bearer {_token(tenant, role='member')}"},
            )
            assert r.status_code == 403, r.text


# ------------------------------------------------------- POST /models/{id}/test


async def test_test_marks_healthy_when_the_model_is_in_the_providers_list(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real, free "is this still valid" check (§ the status pill used to
    default to the literal string "healthy" unconditionally, since
    ModelConfig.health was never written by anything)."""
    import httpx

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )

    async def fake_get(
        self: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None, **kw: object
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "claude-3-5-sonnet-20241022"}, {"id": "claude-3-opus"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            created = await client.post(
                "/api/v1/models",
                json={
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet-20241022",
                    "locality": "cloud",
                },
                headers=headers,
            )
            model_id = created.json()["id"]

            r = await client.post(f"/api/v1/models/{model_id}/test", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "healthy"
            assert body["healthError"] is None
            assert body["healthCheckedAt"]


async def test_test_marks_error_when_the_model_is_missing_from_the_list(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model whose name was renamed/removed upstream must read as broken,
    not silently stay "healthy" because the credential itself still works."""
    import httpx

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )

    async def fake_get(
        self: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None, **kw: object
    ) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"id": "claude-3-opus"}]}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            created = await client.post(
                "/api/v1/models",
                json={
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet-retired",
                    "locality": "cloud",
                },
                headers=headers,
            )
            model_id = created.json()["id"]

            r = await client.post(f"/api/v1/models/{model_id}/test", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "error"
            assert "claude-3-5-sonnet-retired" in body["healthError"]
            assert body["healthCheckedAt"]


async def test_test_marks_error_on_a_discovery_error(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential and no platform key -> DiscoveryError from discover_models
    itself ("key is required") -- must still be a clean status=error, not a 500."""
    monkeypatch.setattr(config.get_settings(), "anthropic_api_key", "", raising=False)
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            created = await client.post(
                "/api/v1/models",
                json={
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet-20241022",
                    "locality": "cloud",
                },
                headers=headers,
            )
            model_id = created.json()["id"]

            r = await client.post(f"/api/v1/models/{model_id}/test", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "error"
            assert "key is required" in body["healthError"]


async def test_test_marks_unknown_for_a_provider_discovery_cannot_check(
    app_session: AppSessionFactory,
) -> None:
    """A ModelConfig whose provider isn't a real, resolvable canonical at all
    (this DB row bypasses the create-time entitlement gate on purpose, to
    simulate a provider discover_models() genuinely has nothing to say
    about) must read as "unknown", not "error" -- it's an unverifiable
    state, not a proven failure."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cfg = m.ModelConfig(
            tenant_id=tenant, provider="totally_bogus", model="whatever", locality="cloud"
        )
        db.add(cfg)
        await db.flush()
        model_id = str(cfg.id)

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/models/{model_id}/test",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "unknown"
            assert body["healthError"]


async def test_test_missing_model_returns_404() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(
                f"/api/v1/models/{uuid.uuid4()}/test",
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 404, r.text


async def test_test_forbidden_for_non_admin() -> None:
    tenant = uuid.UUID(str(ACME_TENANT_ID))
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            created = await client.post(
                "/api/v1/models",
                json={"provider": "ollama", "model": "llama3.1:8b", "locality": "local"},
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            model_id = created.json()["id"]
            r = await client.post(
                f"/api/v1/models/{model_id}/test",
                headers={"Authorization": f"Bearer {_token(tenant, role='member')}"},
            )
            assert r.status_code == 403, r.text


async def test_patch_rejects_binding_a_subscription_credential_to_a_scheduled_model(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fifth bind path (final-review Important #6).

    Sequence that used to slip past every guard: create an `openai_chatgpt`
    ModelConfig with NO credential, bind it to an agent, add an enabled cron
    trigger (all legal -- no subscription credential in the chain yet), then
    PATCH the ModelConfig's `credentialId` at a subscription credential. The
    agent would then run unattended on a personal subscription.
    """
    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sub = await create_credential(
            db,
            tenant_id=tenant,
            name="My ChatGPT",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(tenant_id=tenant, provider="openai_chatgpt", model="gpt-5")
        db.add(mc)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Scheduled",
            model_config_id=mc.id,
        )
        db.add(agent)
        await db.flush()
        db.add(
            m.Trigger(
                tenant_id=tenant,
                agent_id=agent.id,
                kind="cron",
                task_text="x",
                cron_expression="0 * * * *",
                enabled=True,
            )
        )
        await db.flush()
        model_id, sub_id = mc.id, sub.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            headers = {"Authorization": f"Bearer {_token(tenant)}"}
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "openai_chatgpt",
                    "model": "gpt-5",
                    "credentialId": str(sub_id),
                },
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert "ChatGPT subscription" in r.json()["detail"]

    # And the bind must not have landed.
    async with app_session(tenant) as db:
        cfg = await db.get(m.ModelConfig, model_id)
        assert cfg is not None
        assert cfg.credential_id is None


async def test_patch_allows_binding_a_subscription_credential_without_triggers(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not block the ordinary case it shares a code path with."""
    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        sub = await create_credential(
            db,
            tenant_id=tenant,
            name="My ChatGPT",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(uuid.uuid4())},
        )
        mc = m.ModelConfig(tenant_id=tenant, provider="openai_chatgpt", model="gpt-5")
        db.add(mc)
        await db.flush()
        agent = m.Agent(
            tenant_id=tenant,
            department_id=uuid.uuid4(),
            name="Manual only",
            model_config_id=mc.id,
        )
        db.add(agent)
        await db.flush()
        model_id, sub_id = mc.id, sub.id

    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.patch(
                f"/api/v1/models/{model_id}",
                json={
                    "provider": "openai_chatgpt",
                    "model": "gpt-5",
                    "credentialId": str(sub_id),
                },
                headers={"Authorization": f"Bearer {_token(tenant)}"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["credentialId"] == str(sub_id)
