"""A credential's values as a bridge environment, via the tool pack's
`credential_env_fields` (env var -> credential field key). Shared by MCP
logins and per-run injection so the mapping cannot drift between them."""

from __future__ import annotations

from typing import Any

from oc8 import models as m
from oc8.capas.manifest import CredentialTypeSpec
from oc8.credentials.service import keep_origins


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
    # A credential saved before its address was cut to the origin.
    plain_values = keep_origins(cred_type, cred.field_values or {})
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
            value = plain_values.get(field.key)
            if value:
                for target in targets:
                    env[target] = str(value)
    return env, secret_env


def credential_env_targets(
    cred_type: CredentialTypeSpec, credential_env_fields: dict[str, str]
) -> set[str]:
    """Every env name a login of this type can set: the mapped ones and each
    unmapped field's own key. A person's login replaces all of them, so no
    value of another login (the department's) can survive next to it."""
    mapped = set(credential_env_fields)
    fields = set(credential_env_fields.values())
    return mapped | {f.key for f in cred_type.fields if f.key not in fields}


def with_login(
    cfg: dict[str, Any],
    cred: m.Credential,
    cred_type: CredentialTypeSpec,
    credential_env_fields: dict[str, str],
) -> dict[str, Any]:
    """`cfg` with the login's environment in place of whatever the row holds
    for the same names: the chosen login replaces the stored identity whole,
    so a variable it leaves empty keeps no other login's value (never a mix
    of two logins in one bridge)."""
    env = dict(cfg.get("env") or {})
    secret_env = dict(cfg.get("secret_env") or {})
    for target in credential_env_targets(cred_type, credential_env_fields):
        env.pop(target, None)
        secret_env.pop(target, None)
    cred_env, cred_secret_env = credential_mcp_env(cred, cred_type, credential_env_fields)
    return {**cfg, "env": {**env, **cred_env}, "secret_env": {**secret_env, **cred_secret_env}}
