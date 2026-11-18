from __future__ import annotations

import base64
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from oc8 import models as m
from oc8.credentials.service import create_credential, list_credentials
from oc8.modelrouter.keys import model_key_ref, resolve_model_base_url, resolve_model_key
from oc8.oauth.tokens import access_ref
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def test_ref_is_deterministic_and_canonical() -> None:
    # `model_key_ref` is retained only as a small documented naming helper
    # (paired with `model_admin_key_ref`'s own, still-live convention) -- it
    # is no longer read by `resolve_model_key`, which resolves the unified
    # credentials framework's `{canonical}_api_key` credential type instead
    # (Task 14).
    assert model_key_ref("anthropic") == "model/anthropic/api_key"


async def test_returns_the_stored_tenant_key(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )
        got = await resolve_model_key(db, tenant_id=tenant, provider="anthropic")
        assert got == "sk-tenant"


async def test_alias_resolves_to_canonical(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )
        # "claude" is an alias of anthropic
        got = await resolve_model_key(db, tenant_id=tenant, provider="claude")
        assert got == "sk-tenant"


async def test_missing_key_returns_none(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await resolve_model_key(db, tenant_id=tenant, provider="anthropic") is None


async def test_ollama_never_has_a_key(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await resolve_model_key(db, tenant_id=tenant, provider="ollama") is None


async def test_unknown_provider_returns_none(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await resolve_model_key(db, tenant_id=tenant, provider="banana") is None


async def test_another_tenants_key_is_not_visible(app_session: AppSessionFactory) -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    async with app_session(tenant_a) as db:
        await create_credential(
            db,
            tenant_id=tenant_a,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant-a"},
        )
    async with app_session(tenant_b) as db:
        assert await resolve_model_key(db, tenant_id=tenant_b, provider="anthropic") is None


async def test_ordering_matches_list_credentials_when_a_tenant_has_two(
    app_session: AppSessionFactory,
) -> None:
    """Nothing stops a tenant from ending up with two credentials of the same
    `{provider}_api_key` type -- `CredentialPicker`'s "Create new" button is
    always visible, so "Change key" can add a second one rather than editing
    the first. `resolve_model_key` must agree with `list_credentials` (used
    by `GET /credentials?type=...`, which is what `ProviderCard` reads to
    decide which credential is "bound") about which one is "first", or the
    pill/picker could show one credential as bound while real completions
    silently keep using a different one. Names are chosen so creation order
    (ZZZ first) and alphabetical order (AAA first) disagree -- a regression to
    ordering by `created_at` would return the ZZZ value here.
    """
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="ZZZ key",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-zzz-created-first"},
        )
        await create_credential(
            db,
            tenant_id=tenant,
            name="AAA key",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-aaa-alphabetically-first"},
        )

        listed = await list_credentials(db, tenant_id=tenant, credential_type="anthropic_api_key")
        assert [c.name for c in listed] == ["AAA key", "ZZZ key"]

        got = await resolve_model_key(db, tenant_id=tenant, provider="anthropic")
        assert got == "sk-aaa-alphabetically-first"


async def test_store_unavailable_propagates(
    app_session: AppSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oc8 import config
    from oc8.secrets.keyprovider import SecretStoreUnavailable

    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )
        # Break the KEK so the store can't decrypt: a misconfig must fail loud,
        # not silently fall back to the platform key.
        monkeypatch.setattr(config.get_settings(), "secret_kek", "", raising=False)
        with pytest.raises(SecretStoreUnavailable):
            await resolve_model_key(db, tenant_id=tenant, provider="anthropic")


def test_request_has_optional_api_key() -> None:
    from oc8.modelrouter.types import CompletionRequest

    req = CompletionRequest(provider="anthropic", model="x", messages=[])
    assert req.api_key is None
    req2 = CompletionRequest(provider="anthropic", model="x", messages=[], api_key="k")
    assert req2.api_key == "k"


def test_flag_defaults_off() -> None:
    from oc8.config import Settings

    assert Settings().require_tenant_model_key is False


def test_tenant_key_required_error_exists() -> None:
    from oc8.modelrouter.router import TenantKeyRequired

    assert issubclass(TenantKeyRequired, RuntimeError)


# ------------------------------------------------------------- base_url


async def test_resolve_model_base_url_returns_the_stored_value(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="opaas ai",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-opaas", "base_url": "https://opaas.cloud/v1"},
        )
        got = await resolve_model_base_url(db, tenant_id=tenant, provider="openai_compatible")
        assert got == "https://opaas.cloud/v1"


async def test_resolve_model_base_url_is_none_when_the_field_was_never_set(
    app_session: AppSessionFactory,
) -> None:
    """A credential created before this field existed (or one saved with the
    field left blank) has no `base_url` key in `field_values` at all -- this
    must degrade to None, the same "nothing configured" signal as a missing
    credential, not raise."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        # Written as a row: saving a credential without its required
        # `base_url` is refused now, so only an older row can lack it.
        db.add(
            m.Credential(
                tenant_id=tenant,
                name="opaas ai",
                credential_type="openai_compatible_api_key",
                field_values={},
                secret_refs={},
            )
        )
        await db.flush()
        assert (
            await resolve_model_base_url(db, tenant_id=tenant, provider="openai_compatible") is None
        )


async def test_resolve_model_base_url_is_none_without_a_credential(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert (
            await resolve_model_base_url(db, tenant_id=tenant, provider="openai_compatible") is None
        )


async def test_resolve_model_base_url_is_none_for_a_provider_with_no_url_concept(
    app_session: AppSessionFactory,
) -> None:
    """anthropic/openai have fixed endpoints -- their credential type never
    declared a base_url field, so even a (hypothetical) stray key in
    field_values must not leak through as a real override."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )
        assert await resolve_model_base_url(db, tenant_id=tenant, provider="anthropic") is None


async def test_resolve_model_base_url_none_for_ollama(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await resolve_model_base_url(db, tenant_id=tenant, provider="ollama") is None


async def test_resolve_model_base_url_none_for_unknown_provider(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        assert await resolve_model_base_url(db, tenant_id=tenant, provider="banana") is None


async def test_key_and_base_url_agree_on_which_credential_is_bound(
    app_session: AppSessionFactory,
) -> None:
    """Same ordering-by-name convention as `resolve_model_key`'s own test
    above (`test_ordering_matches_list_credentials_when_a_tenant_has_two`):
    a tenant with two openai_compatible credentials must have the api_key
    lookup and the base_url lookup agree on which one is "bound", or a
    request could pair one credential's key with another's URL."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="ZZZ (created first)",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-zzz", "base_url": "https://zzz.example.com/v1"},
        )
        await create_credential(
            db,
            tenant_id=tenant,
            name="AAA (alphabetically first)",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-aaa", "base_url": "https://aaa.example.com/v1"},
        )
        key = await resolve_model_key(db, tenant_id=tenant, provider="openai_compatible")
        base_url = await resolve_model_base_url(db, tenant_id=tenant, provider="openai_compatible")
        assert key == "sk-aaa"
        assert base_url == "https://aaa.example.com/v1"


# ------------------------------------------------------ credential_id override


async def test_credential_id_wins_over_the_tenant_wide_first_by_name_pick(
    app_session: AppSessionFactory,
) -> None:
    """A ModelConfig with its own `credential_id` set (multiple accounts of
    one provider, one per ModelConfig) must resolve to THAT credential, even
    though it is not the tenant-wide "first by name" one (see the ordering
    test above) -- otherwise two ModelConfigs of the same provider could
    never actually use different accounts."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        first_by_name = await create_credential(
            db,
            tenant_id=tenant,
            name="AAA (alphabetically first)",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-aaa"},
        )
        second = await create_credential(
            db,
            tenant_id=tenant,
            name="ZZZ (not first alphabetically)",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-zzz"},
        )
        assert first_by_name.id != second.id

        got = await resolve_model_key(
            db, tenant_id=tenant, provider="anthropic", credential_id=second.id
        )
        assert got == "sk-zzz"


async def test_credential_id_none_falls_back_to_tenant_wide_pick(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Prod Anthropic",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-tenant"},
        )
        got = await resolve_model_key(
            db, tenant_id=tenant, provider="anthropic", credential_id=None
        )
        assert got == "sk-tenant"


async def test_credential_id_pointing_at_a_missing_row_returns_none_not_a_fallback(
    app_session: AppSessionFactory,
) -> None:
    """A ModelConfig explicitly bound to a credential that has since been
    deleted must read as "no usable key" -- never silently fall back to
    guessing a different, unrelated credential of the same type."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="Some other credential",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-other"},
        )
        got = await resolve_model_key(
            db, tenant_id=tenant, provider="anthropic", credential_id=uuid.uuid4()
        )
        assert got is None


async def test_credential_id_from_a_different_tenant_is_not_visible(
    app_session: AppSessionFactory,
) -> None:
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    async with app_session(tenant_a) as db:
        cred_a = await create_credential(
            db,
            tenant_id=tenant_a,
            name="Tenant A's credential",
            credential_type="anthropic_api_key",
            field_values={"api_key": "sk-a"},
        )
    async with app_session(tenant_b) as db:
        got = await resolve_model_key(
            db, tenant_id=tenant_b, provider="anthropic", credential_id=cred_a.id
        )
        assert got is None


# ---------------------------------------------- chatgpt subscription bridge


async def test_resolve_model_key_bridges_chatgpt_subscription(
    app_session: AppSessionFactory,
) -> None:
    """`openai_chatgpt_subscription` credentials have no `api_key` field --
    the normal secret-store lookup would fail. `resolve_model_key` must
    instead read the credential's `oauth_connection_id`, call
    `get_access_token` for a live bearer token, and pair it with the
    connection's `chatgpt_account_id` -- exactly the composite JSON shape
    `ChatGptSubscriptionAdapter` parses."""
    tenant = uuid.uuid4()
    oauth_conn_id = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = m.OAuthConnection(
            id=oauth_conn_id,
            tenant_id=tenant,
            provider="openai_chatgpt",
            account_label="acct_123",
            access_secret_ref=access_ref(oauth_conn_id),
            grant_type="device_code",
            client_source="tenant",
            provider_metadata={"chatgpt_account_id": "acct_123"},
        )
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="my chatgpt",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(oauth_conn_id)},
        )
        model_config = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        db.add_all([conn, model_config])
        await db.flush()

        with patch(
            "oc8.modelrouter.keys.get_access_token", AsyncMock(return_value="live-token-123")
        ) as mocked:
            key = await resolve_model_key(
                db, tenant_id=tenant, provider="openai_chatgpt", credential_id=cred.id
            )
        assert key is not None
        assert json.loads(key) == {"access_token": "live-token-123", "account_id": "acct_123"}
        mocked.assert_awaited_once_with(db, tenant_id=tenant, connection_id=oauth_conn_id)


async def test_resolve_model_key_bridge_handles_missing_account_id(
    app_session: AppSessionFactory,
) -> None:
    """A connection whose `provider_metadata` never got `chatgpt_account_id`
    written (e.g. an older row, or the account-id lookup failed at connect
    time) must degrade to `account_id: None` in the composite JSON, not
    raise or crash the whole key resolution."""
    tenant = uuid.uuid4()
    oauth_conn_id = uuid.uuid4()
    async with app_session(tenant) as db:
        conn = m.OAuthConnection(
            id=oauth_conn_id,
            tenant_id=tenant,
            provider="openai_chatgpt",
            account_label="ChatGPT subscription",
            access_secret_ref=access_ref(oauth_conn_id),
            grant_type="device_code",
            client_source="tenant",
        )
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="my chatgpt",
            credential_type="openai_chatgpt_subscription",
            field_values={"oauth_connection_id": str(oauth_conn_id)},
        )
        model_config = m.ModelConfig(
            tenant_id=tenant, provider="openai_chatgpt", model="gpt-5", credential_id=cred.id
        )
        db.add_all([conn, model_config])
        await db.flush()

        with patch(
            "oc8.modelrouter.keys.get_access_token", AsyncMock(return_value="live-token-123")
        ):
            key = await resolve_model_key(
                db, tenant_id=tenant, provider="openai_chatgpt", credential_id=cred.id
            )
        assert key is not None
        assert json.loads(key) == {"access_token": "live-token-123", "account_id": None}


async def test_base_url_also_honors_credential_id(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db,
            tenant_id=tenant,
            name="AAA (alphabetically first)",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-aaa", "base_url": "https://aaa.example.com/v1"},
        )
        second = await create_credential(
            db,
            tenant_id=tenant,
            name="ZZZ (not first alphabetically)",
            credential_type="openai_compatible_api_key",
            field_values={"api_key": "sk-zzz", "base_url": "https://zzz.example.com/v1"},
        )
        got = await resolve_model_base_url(
            db, tenant_id=tenant, provider="openai_compatible", credential_id=second.id
        )
        assert got == "https://zzz.example.com/v1"


async def test_resolve_model_key_returns_none_for_a_needs_reauth_subscription(
    app_session: AppSessionFactory,
) -> None:
    """A dead subscription connection must read as "no usable key" (the
    documented fallback signal), not as an exception.

    `get_access_token` raises `OAuthReauthRequired` for a `needs_reauth`,
    deleted, or missing connection and `OAuthExchangeFailed` for a failed
    refresh. `oc8.modelrouter.fallback` calls `resolve_model_key` OUTSIDE its
    own try/except, so letting either escape tore down the entire fallback
    chain instead of moving on to the next model -- the only credential
    failure mode in this function that broke its own contract.
    """
    from oc8.oauth.errors import OAuthExchangeFailed, OAuthReauthRequired

    for failure in (
        OAuthReauthRequired("connection is needs_reauth"),
        OAuthExchangeFailed("refresh failed (400)"),
    ):
        tenant = uuid.uuid4()
        oauth_conn_id = uuid.uuid4()
        async with app_session(tenant) as db:
            conn = m.OAuthConnection(
                id=oauth_conn_id,
                tenant_id=tenant,
                provider="openai_chatgpt",
                account_label="acct_dead",
                access_secret_ref=access_ref(oauth_conn_id),
                grant_type="device_code",
                client_source="tenant",
                status="needs_reauth",
            )
            cred = await create_credential(
                db,
                tenant_id=tenant,
                name="my chatgpt",
                credential_type="openai_chatgpt_subscription",
                field_values={"oauth_connection_id": str(oauth_conn_id)},
            )
            db.add(conn)
            await db.flush()

            with patch("oc8.modelrouter.keys.get_access_token", AsyncMock(side_effect=failure)):
                key = await resolve_model_key(
                    db, tenant_id=tenant, provider="openai_chatgpt", credential_id=cred.id
                )
            assert key is None
