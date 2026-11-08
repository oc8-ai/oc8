from __future__ import annotations

import base64
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from oc8 import models as m
from oc8.capas import contributions, loader
from oc8.capas.discovery import find_plugin
from oc8.capas.lifecycle import enable_plugin
from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.capas.service import install_plugin
from oc8.config import get_settings
from oc8.credentials import registry
from oc8.credentials.service import (
    CredentialFieldNotSet,
    CredentialInUse,
    CredentialNotFound,
    create_credential,
    delete_credential,
    get_credential,
    list_credentials,
    resolve_credential_field,
    update_credential,
)

# Imported under an alias: `test_credential` matches pytest's `test_*`
# collection pattern, so importing it under its real name would make pytest
# try to collect and directly call the SERVICE FUNCTION itself as a test.
from oc8.credentials.service import test_credential as run_test_credential
from oc8.oauth import http as oauth_http
from tests.conftest import AppSessionFactory

pytestmark = pytest.mark.asyncio

# The real, shipped capas/ dir -- NOT a throwaway fixtures/ folder -- for the
# one test below that needs the REAL s3_source Capa + its real s3_api
# credential_type (Bug 1 regression, Task 15 report).
_REAL_CAPAS_DIR = Path(__file__).resolve().parents[3] / "capas"


# `test_credential` resolves a CORE credential_type's `validate_entry_point`
# (a `module:attr` string) via a plain `importlib.import_module` + `getattr`
# -- no owning Capa, so `find_plugin`/`TRUSTED` never come into it (see that
# function's own comment). These two module-level async functions ARE real,
# importable `tests.credentials.test_service:_validate_*` targets for exactly
# that path -- not mocks, the actual entry point `test_credential` imports
# and calls.
async def _validate_ok(values: dict[str, str]) -> None:
    return None


async def _validate_fail(values: dict[str, str]) -> None:
    raise ValueError("credential validation failed for test")


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    # create_credential/resolve_credential_field store secret-kind field
    # values in the real oc8.secrets.service vault, which needs a KEK
    # configured to encrypt/decrypt anything (mirrors tests/secrets/test_service.py).
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


@pytest.fixture(autouse=True)
def _register_throwaway_credential_types() -> Iterator[None]:
    """`test_service.py` exercises CRUD against `credential_type="s3_api"`/
    `"telegram_bot"`, but nothing registers those types for real until Task
    14 (core LLM provider types) and Tasks 12/13 (Capa-contributed types via
    real plugin folders). Register throwaway specs directly into
    `CORE_CREDENTIAL_TYPES` for the duration of this module so
    `create_credential`/`update_credential`'s `get_credential_type` lookup
    succeeds, and remove them afterwards so no other test module sees them.
    """
    registry.CORE_CREDENTIAL_TYPES["s3_api"] = CredentialTypeSpec(
        name="s3_api",
        display_name="S3",
        fields=[
            SetupFieldSpec(key="access_key", label="Access key", kind="password", required=False),
            # `default="us-east-1"`, matching the real `capas/s3_source/
            # credential_types/s3_api.toml` -- this is exactly the field the
            # backfill-a-default regression test below exercises.
            SetupFieldSpec(
                key="region", label="Region", kind="text", required=False, default="us-east-1"
            ),
        ],
    )
    registry.CORE_CREDENTIAL_TYPES["telegram_bot"] = CredentialTypeSpec(
        name="telegram_bot",
        display_name="Telegram bot",
        fields=[
            SetupFieldSpec(key="bot_token", label="Bot token", kind="password", required=False),
        ],
    )
    registry.CORE_CREDENTIAL_TYPES["test_validate_ok"] = CredentialTypeSpec(
        name="test_validate_ok",
        display_name="Test: validates ok",
        fields=[SetupFieldSpec(key="token", label="Token", kind="password", required=False)],
        validate_entry_point="tests.credentials.test_service:_validate_ok",
    )
    registry.CORE_CREDENTIAL_TYPES["test_validate_fail"] = CredentialTypeSpec(
        name="test_validate_fail",
        display_name="Test: validates fail",
        fields=[SetupFieldSpec(key="token", label="Token", kind="password", required=False)],
        validate_entry_point="tests.credentials.test_service:_validate_fail",
    )
    try:
        yield
    finally:
        registry.CORE_CREDENTIAL_TYPES.pop("s3_api", None)
        registry.CORE_CREDENTIAL_TYPES.pop("telegram_bot", None)
        registry.CORE_CREDENTIAL_TYPES.pop("test_validate_ok", None)
        registry.CORE_CREDENTIAL_TYPES.pop("test_validate_fail", None)


def _evict_plugin_modules() -> None:
    """Drop cached `connector`/`connector.*` and `credential`/`credential.*`
    modules. `s3_source` (like every other connector-type Capa post-restructure)
    names its package `connector` -- a name several sibling plugins reuse -- and
    its credential-type entry point's own top level is `credential`. Called
    symmetrically before/after the one test below that loads the REAL
    `s3_source` plugin, so it neither inherits nor leaves behind a stale
    same-named module for `tests/plugins/test_s3_plugin.py` or any other test
    module that also imports a bare `connector`/`credential` (mirrors
    `tests/plugins/test_s3_plugin.py`'s own `_evict()`)."""
    for name in [
        n
        for n in sys.modules
        if n in ("connector", "credential")
        or n.startswith("connector.")
        or n.startswith("credential.")
    ]:
        del sys.modules[name]


@pytest.fixture()
def _real_s3_source_plugin(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Points capa discovery at the REAL `capas/` dir so `install_plugin`/
    `enable_plugin` register the actual shipped `s3_source` Capa and its real
    `s3_api` credential_type -- including its real
    `validate_entry_point = "credential:validate_s3"`
    (`capas/s3_source/credential_types/s3_api.toml`), which resolves to
    `capas/s3_source/credential.py:validate_s3`. Only the ONE test that needs
    this real plugin-owned resolution path (the Bug 1 regression) requests
    it; every other test in this module uses cheap `CORE_CREDENTIAL_TYPES`
    throwaways instead, exactly as before.

    `get_settings.cache_clear()` throws away whatever the module-level
    autouse `_kek` fixture already patched onto the (now-stale) cached
    `Settings` instance -- regardless of which of the two function-scoped
    fixtures pytest happens to run first -- so `secret_kek` is re-applied
    here too, redundantly but order-independently.
    """
    _evict_plugin_modules()
    monkeypatch.setenv("OC8_CAPAS_PATH", str(_REAL_CAPAS_DIR))
    get_settings.cache_clear()
    monkeypatch.setattr(
        get_settings(), "secret_kek", base64.b64encode(bytes(range(32))).decode(), raising=False
    )
    loader.reset_for_tests()
    contributions.reset_for_tests()
    yield
    oauth_http.set_transport_override(None)
    loader.reset_for_tests()
    contributions.reset_for_tests()
    get_settings.cache_clear()
    _evict_plugin_modules()


async def test_create_stores_secret_fields_in_the_vault_not_on_the_row(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Prod S3",
            credential_type="s3_api",
            field_values={"region": "eu-central-1", "access_key": "AKIA_TEST"},
        )
        await db.commit()
        assert cred.field_values == {"region": "eu-central-1"}
        assert "access_key" in cred.secret_refs


async def test_create_backfills_a_declared_default_when_the_field_is_omitted(
    app_session: AppSessionFactory,
) -> None:
    """`CredentialPicker`'s create form only shows a field's `default` as a
    placeholder -- it never commits that value into what actually gets
    submitted unless the user retypes it. A user who leaves `s3_api`'s
    pre-filled "Region: us-east-1" untouched (easy to do, since it looks
    already set) must still end up with a `region` in `field_values`, or
    `resolve_credential_field` later raises `CredentialNotFound` for a field
    that was never really missing from the user's point of view."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Prod S3",
            credential_type="s3_api",
            # `region` omitted entirely -- exactly what an untouched,
            # placeholder-only CredentialPicker field submits.
            field_values={"access_key": "AKIA_TEST"},
        )
        await db.commit()
        assert cred.field_values == {"region": "us-east-1"}

    async with app_session(tenant) as db:
        value = await resolve_credential_field(
            db, tenant_id=tenant, credential_id=cred.id, field_key="region"
        )
        assert value == "us-east-1"


async def test_resolve_credential_field_returns_the_secret_value(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Prod S3",
            credential_type="s3_api",
            field_values={"access_key": "AKIA_TEST"},
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        value = await resolve_credential_field(
            db, tenant_id=tenant, credential_id=cred_id, field_key="access_key"
        )
        assert value == "AKIA_TEST"


async def test_get_unknown_credential_raises_not_found(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        with pytest.raises(CredentialNotFound):
            await get_credential(db, tenant_id=tenant, credential_id=uuid.uuid4())


async def test_update_leaves_omitted_secret_field_untouched(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Prod S3",
            credential_type="s3_api",
            field_values={"access_key": "AKIA_ORIGINAL"},
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        await update_credential(
            db, tenant_id=tenant, credential_id=cred_id, name="Prod S3 (renamed)"
        )
        await db.commit()

    async with app_session(tenant) as db:
        reloaded = await get_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert reloaded.name == "Prod S3 (renamed)"
        value = await resolve_credential_field(
            db, tenant_id=tenant, credential_id=cred_id, field_key="access_key"
        )
        assert value == "AKIA_ORIGINAL"


async def test_delete_removes_the_row(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db, tenant_id=tenant, name="Throwaway", credential_type="s3_api", field_values={}
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        await delete_credential(db, tenant_id=tenant, credential_id=cred_id)
        await db.commit()

    async with app_session(tenant) as db:
        with pytest.raises(CredentialNotFound):
            await get_credential(db, tenant_id=tenant, credential_id=cred_id)


async def test_delete_refuses_when_a_data_source_still_references_the_credential(
    app_session: AppSessionFactory,
) -> None:
    """Task 12's `_find_references` extension: a DataSource whose config
    names this credential (`s3_source`'s `{"credential": "<id>"}` shape)
    must block deletion, naming the source so an operator knows what to
    unlink first -- an orphaned reference would otherwise fail invisibly
    the next time that source tried to sync."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Prod S3",
            credential_type="s3_api",
            field_values={"access_key": "AKIA_TEST"},
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        source = m.DataSource(
            tenant_id=tenant,
            connector_type="s3",
            name="Reports bucket",
            config={"bucket": "reports", "credential": str(cred_id)},
        )
        db.add(source)
        await db.commit()

    async with app_session(tenant) as db:
        with pytest.raises(CredentialInUse) as exc_info:
            await delete_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert "Reports bucket" in str(exc_info.value)

    async with app_session(tenant) as db:
        # Still there -- the refusal must not have deleted it anyway.
        reloaded = await get_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert reloaded.id == cred_id


async def test_list_filters_by_type(app_session: AppSessionFactory) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        await create_credential(
            db, tenant_id=tenant, name="S3 one", credential_type="s3_api", field_values={}
        )
        await create_credential(
            db,
            tenant_id=tenant,
            name="Telegram one",
            credential_type="telegram_bot",
            field_values={},
        )
        await db.commit()

    async with app_session(tenant) as db:
        s3_only = await list_credentials(db, tenant_id=tenant, credential_type="s3_api")
        assert [c.name for c in s3_only] == ["S3 one"]
        everything = await list_credentials(db, tenant_id=tenant)
        assert len(everything) == 2


async def test_test_credential_success_records_last_test_ok(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Validates ok",
            credential_type="test_validate_ok",
            field_values={"token": "shhh"},
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        await run_test_credential(db, tenant_id=tenant, credential_id=cred_id)
        await db.commit()

    async with app_session(tenant) as db:
        reloaded = await get_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert reloaded.last_test_ok is True
        assert reloaded.last_tested_at is not None


async def test_test_credential_failure_raises_and_still_persists_the_failed_result(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Validates fail",
            credential_type="test_validate_fail",
            field_values={"token": "shhh"},
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        with pytest.raises(ValueError):
            await run_test_credential(db, tenant_id=tenant, credential_id=cred_id)
        await db.commit()

    async with app_session(tenant) as db:
        reloaded = await get_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert reloaded.last_test_ok is False
        assert reloaded.last_tested_at is not None


async def test_test_credential_with_no_validate_entry_point_succeeds_trivially(
    app_session: AppSessionFactory,
) -> None:
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db, tenant_id=tenant, name="No validation", credential_type="s3_api", field_values={}
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        await run_test_credential(db, tenant_id=tenant, credential_id=cred_id)
        await db.commit()

    async with app_session(tenant) as db:
        reloaded = await get_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert reloaded.last_test_ok is True
        assert reloaded.last_tested_at is not None


async def test_test_credential_resolves_a_plugin_owned_validate_entry_point(
    app_session: AppSessionFactory,
    _real_s3_source_plugin: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1 regression (Task 15 report, "Test button fails for plugin-owned
    credential types"). Exercises the REAL, shipped `s3_source` Capa and its
    REAL `s3_api` credential_type -- not a throwaway CORE-registered stand-in
    -- because the bug was specifically in resolving a PLUGIN-owned
    `validate_entry_point`: the old code passed the credential_type's own
    name ("s3_api") to `find_plugin` as if it were a PLUGIN name, found
    nothing (no plugin is named "s3_api"), and silently fell through to the
    core-only `importlib.import_module("credential")` branch --
    `ModuleNotFoundError: No module named 'credential'`, live-reproduced in
    the report. A throwaway CORE-registered "s3_api" (as every other test in
    this module uses) would never hit that branch at all, which is exactly
    why Task 4's own tests never caught this.
    """
    # The module's autouse `_register_throwaway_credential_types` fixture
    # shadows "s3_api" with a CORE (non-plugin) throwaway for every OTHER
    # test in this file; drop it so `get_credential_type` falls through to
    # the real, plugin-contributed one below. The autouse fixture's own
    # teardown re-pops "s3_api" afterwards regardless, so this is harmless.
    registry.CORE_CREDENTIAL_TYPES.pop("s3_api", None)
    # The example endpoint has no DNS; the url guard needs it to resolve.
    monkeypatch.setattr("oc8.net.outbound.resolve_ips", lambda host: ["93.184.216.34"])

    tenant = uuid.uuid4()
    found = find_plugin("s3_source")
    assert found is not None and found.manifest is not None, "s3_source plugin not discovered"

    async with app_session(tenant) as db:
        version = await install_plugin(db, tenant_id=tenant, manifest_data=found.manifest)
        await enable_plugin(
            db,
            tenant_id=tenant,
            capa_id=version.capa_id,
            granted_permissions=list(version.permissions),
        )
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Verify S3",
            credential_type="s3_api",
            field_values={
                "access_key": "AKIAEXAMPLE",
                "secret_key": "shhh",
                "endpoint": "http://minio.example.test",
            },
        )
        await db.commit()
        cred_id = cred.id

    # A minimal in-process fake S3 endpoint -- this proves the REAL
    # `validate_s3` entry point actually RAN (it signs and sends a real
    # HTTP GET via `capas/s3_source/connector/connector.py`'s `_Client`),
    # not merely that the wiring was reached without erroring.
    oauth_http.set_transport_override(
        httpx.MockTransport(lambda request: httpx.Response(200, text="<ListBucketResult/>"))
    )

    async with app_session(tenant) as db:
        await run_test_credential(db, tenant_id=tenant, credential_id=cred_id)
        await db.commit()

    async with app_session(tenant) as db:
        reloaded = await get_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert reloaded.last_test_ok is True
        assert reloaded.last_tested_at is not None


async def test_test_credential_still_resolves_a_core_owned_validate_entry_point(
    app_session: AppSessionFactory,
) -> None:
    """The CORE-type path (Task 14's LLM provider keys and the like, no
    owning Capa) must keep working after Bug 1's fix -- `find_plugin` never
    finding an owner for a core type is the CORRECT, expected outcome for
    this branch, not the bug. Same shape as
    `test_test_credential_success_records_last_test_ok` above; kept as its
    own explicit regression so a future change to the plugin-owner lookup
    can't quietly break the core branch without a dedicated failure."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Core-owned, validates ok",
            credential_type="test_validate_ok",
            field_values={"token": "shhh"},
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        await run_test_credential(db, tenant_id=tenant, credential_id=cred_id)
        await db.commit()

    async with app_session(tenant) as db:
        reloaded = await get_credential(db, tenant_id=tenant, credential_id=cred_id)
        assert reloaded.last_test_ok is True


async def test_resolve_credential_field_raises_field_not_set_for_a_blank_optional_field(
    app_session: AppSessionFactory,
) -> None:
    """Bug 2 regression (Task 15 report, "Misleading error when an optional
    field is left blank"). `s3_api`'s `endpoint` is documented optional
    ("leave empty for AWS") -- a credential created without it is complete
    and valid, so resolving THAT specific field must raise the distinct
    `CredentialFieldNotSet` (naming the field), never the same
    `CredentialNotFound` a genuinely missing/wrong-tenant credential ROW
    raises."""
    tenant = uuid.uuid4()
    async with app_session(tenant) as db:
        cred = await create_credential(
            db,
            tenant_id=tenant,
            name="Verify S3 (no endpoint)",
            credential_type="s3_api",
            # `endpoint` deliberately omitted -- exactly what leaving the
            # optional field blank in the UI submits.
            field_values={"access_key": "AKIA_TEST"},
        )
        await db.commit()
        cred_id = cred.id

    async with app_session(tenant) as db:
        with pytest.raises(CredentialFieldNotSet) as exc_info:
            await resolve_credential_field(
                db, tenant_id=tenant, credential_id=cred_id, field_key="endpoint"
            )
        assert exc_info.value.field_key == "endpoint"
        assert "endpoint" in str(exc_info.value)
        # The message must not read like the credential itself is missing.
        assert "no stored credential" not in str(exc_info.value)

    # A genuinely missing credential ROW must still raise the ORIGINAL
    # `CredentialNotFound` -- the two failure modes must stay distinct in
    # both directions, not just collapse into one broader exception.
    async with app_session(tenant) as db:
        with pytest.raises(CredentialNotFound):
            await resolve_credential_field(
                db, tenant_id=tenant, credential_id=uuid.uuid4(), field_key="endpoint"
            )
