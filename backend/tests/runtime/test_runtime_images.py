from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest

from oc8 import models as m
from oc8.credentials.core_types import CORE_CREDENTIAL_TYPES
from oc8.runtime.images import (
    RegistryLoginError,
    declared_runtime_images,
    registry_auth_for_runtime,
    runtime_image,
)
from oc8.sandbox.types import RegistryAuth
from oc8.secrets.service import store_secret
from tests.conftest import AppSessionFactory


@pytest.fixture(autouse=True)
def _kek(monkeypatch: pytest.MonkeyPatch) -> None:
    from oc8 import config

    monkeypatch.setattr(
        config.get_settings(),
        "secret_kek",
        base64.b64encode(bytes(range(32))).decode(),
        raising=False,
    )


def _runtime_plugin(folder: Path, *, image: str, credential_key: str = "") -> None:
    folder.mkdir()
    credential = f'registry_credential = "{credential_key}"\n' if credential_key else ""
    (folder / "plugin.toml").write_text(
        f"""
[plugin]
name = "{folder.name}"
version = "0.1.0"
type = "runtime_adapter"
trust = "first_party"
summary = "Example runtime"

[plugin.sandbox]
image = "{image}"
{credential}
""",
        encoding="utf-8",
    )


def test_a_runtime_image_falls_back_when_the_capa_declares_none(tmp_path: Path) -> None:
    folder = tmp_path / "plain_runtime"
    folder.mkdir()
    (folder / "plugin.toml").write_text(
        """
[plugin]
name = "plain_runtime"
version = "0.1.0"
type = "runtime_adapter"
trust = "first_party"
summary = "No image of its own"
""",
        encoding="utf-8",
    )
    assert runtime_image("plain_runtime", "fallback:1", paths=[str(tmp_path)]) == "fallback:1"


def test_declared_runtime_images_come_from_runtime_capas_only(tmp_path: Path) -> None:
    _runtime_plugin(tmp_path / "custom_runtime", image="registry.example/custom:1")
    other = tmp_path / "not_a_runtime"
    other.mkdir()
    (other / "plugin.toml").write_text(
        """
[plugin]
name = "not_a_runtime"
version = "0.1.0"
type = "connector"
trust = "first_party"
summary = "Not a runtime"

[plugin.sandbox]
image = "registry.example/ignored:1"
""",
        encoding="utf-8",
    )
    assert declared_runtime_images([str(tmp_path)]) == {"registry.example/custom:1"}
    assert (
        runtime_image("custom_runtime", "fallback:1", paths=[str(tmp_path)])
        == "registry.example/custom:1"
    )


def test_container_registry_is_a_core_credential_with_one_secret() -> None:
    cred = CORE_CREDENTIAL_TYPES["container_registry"]
    assert [field.key for field in cred.fields] == ["registry", "username", "password"]
    assert [field.key for field in cred.fields if field.kind == "password"] == ["password"]


@pytest.mark.asyncio
async def test_registry_login_is_read_from_setup_and_absent_when_unset(
    app_session: AppSessionFactory, tmp_path: Path
) -> None:
    _runtime_plugin(
        tmp_path / "custom_runtime",
        image="ghcr.io/acme/runtime:1",
        credential_key="registry_login",
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        capa = m.Capa(tenant_id=tenant, name="custom_runtime", type="runtime_adapter")
        s.add(capa)
        await s.flush()
        assert (
            await registry_auth_for_runtime(
                s, tenant_id=tenant, plugin_name="custom_runtime", paths=[str(tmp_path)]
            )
            is None
        )
        s.add(
            m.CapaInstallation(
                tenant_id=tenant,
                capa_id=capa.id,
                status="enabled",
                config={"registry": "ghcr.io", "username": "acme"},
            )
        )
        await store_secret(
            s,
            tenant_id=tenant,
            name="plugin:custom_runtime:registry_login",
            value="token-value",
            kind="plugin_credential",
        )
        await s.flush()
        auth = await registry_auth_for_runtime(
            s, tenant_id=tenant, plugin_name="custom_runtime", paths=[str(tmp_path)]
        )
        assert auth == RegistryAuth(username="acme", password="token-value", registry="ghcr.io")
        assert "token-value" not in repr(auth)


@pytest.mark.asyncio
async def test_a_username_without_a_stored_token_is_an_error(
    app_session: AppSessionFactory, tmp_path: Path
) -> None:
    _runtime_plugin(
        tmp_path / "custom_runtime",
        image="ghcr.io/acme/runtime:1",
        credential_key="registry_login",
    )
    tenant = uuid.uuid4()
    async with app_session(tenant) as s:
        capa = m.Capa(tenant_id=tenant, name="custom_runtime", type="runtime_adapter")
        s.add(capa)
        await s.flush()
        s.add(
            m.CapaInstallation(
                tenant_id=tenant,
                capa_id=capa.id,
                status="enabled",
                config={"username": "acme", "registry": "ghcr.io"},
            )
        )
        await s.flush()
        with pytest.raises(RegistryLoginError, match="no stored token"):
            await registry_auth_for_runtime(
                s, tenant_id=tenant, plugin_name="custom_runtime", paths=[str(tmp_path)]
            )
