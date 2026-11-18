"""A credential saved with a browser-bar address (before `origin_only` cut it
on save) still starts its bridge at the service's root."""

from __future__ import annotations

from types import SimpleNamespace

from oc8.capas.manifest import CredentialTypeSpec, SetupFieldSpec
from oc8.connections.credential_env import credential_mcp_env

_TYPE = CredentialTypeSpec(
    name="erp_login",
    display_name="ERP",
    fields=[
        SetupFieldSpec(key="url", label="URL", kind="url", origin_only=True),
        SetupFieldSpec(key="database", label="Database"),
        SetupFieldSpec(key="password", label="Key", kind="password"),
    ],
)


def test_a_stored_address_with_a_path_starts_the_bridge_at_the_root() -> None:
    cred = SimpleNamespace(
        field_values={"url": "https://erp.acme.test/odoo/crm?view=list", "database": "acme"},
        secret_refs={"password": "ref:1"},
    )

    env, secret_env = credential_mcp_env(
        cred, _TYPE, {"ERP_URL": "url", "ERP_DB": "database", "ERP_KEY": "password"}
    )

    assert env == {"ERP_URL": "https://erp.acme.test", "ERP_DB": "acme"}
    assert secret_env == {"ERP_KEY": "ref:1"}
