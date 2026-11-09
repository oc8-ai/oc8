"""A credential's values as a bridge environment, via the tool pack's
`credential_env_fields` (env var -> credential field key). Shared by MCP
logins and per-run injection so the mapping cannot drift between them."""

from __future__ import annotations

from oc8 import models as m
from oc8.capas.manifest import CredentialTypeSpec


def credential_mcp_env(
    cred: m.Credential, cred_type: CredentialTypeSpec, credential_env_fields: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """`(env, secret_env)`: plain values verbatim, secret fields as the
    credential's own secret-store refs (resolved only at launch). A field no
    mapping names keeps its own key. Several env names may share one field
    (odoo_mcp maps ODOO_PASSWORD and ODOO_API_KEY to `password`)."""
    by_field: dict[str, list[str]] = {}
    for env_name, field_key in credential_env_fields.items():
        by_field.setdefault(field_key, []).append(env_name)
    secret_keys = {f.key for f in cred_type.fields if f.kind == "password"}
    env: dict[str, str] = {}
    secret_env: dict[str, str] = {}
    for field in cred_type.fields:
        targets = by_field.get(field.key, [field.key])
        if field.key in secret_keys:
            ref = (cred.secret_refs or {}).get(field.key)
            if ref:
                for target in targets:
                    secret_env[target] = ref
        else:
            value = (cred.field_values or {}).get(field.key)
            if value:
                for target in targets:
                    env[target] = str(value)
    return env, secret_env
