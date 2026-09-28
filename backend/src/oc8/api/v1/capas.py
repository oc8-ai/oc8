"""Unified plugin registry endpoints (§13)."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission, require_scope, unguarded
from oc8.api.v1._serializers import department_to_dto
from oc8.auth import Principal
from oc8.authz.permissions import MANAGE, PLUGIN, VIEW, perm
from oc8.capas.discovery import discover_plugins, find_plugin
from oc8.capas.i18n import translations_for
from oc8.capas.lifecycle import (
    ConsentError,
    QuarantinedError,
    disable_plugin,
    enable_plugin,
)
from oc8.capas.loader import TRUSTED, LoaderError, import_entry_point
from oc8.capas.manifest import (
    Manifest,
    ManifestError,
    PluginSetupSpec,
    SetupFieldSpec,
    SetupOAuthProvision,
    parse_manifest,
)
from oc8.capas.service import (
    MissingDependencyError,
    PluginError,
    SetupValidationError,
    install_plugin,
    install_with_dependencies,
    instantiate_agent,
    instantiate_department,
    validate_setup_values,
)
from oc8.credentials.registry import get_credential_type
from oc8.credentials.service import (
    CredentialFieldNotSet,
    CredentialNotFound,
    get_credential,
    resolve_credential_field,
)
from oc8.knowledge.connectors.base import ConnectorError
from oc8.knowledge.sources import SourceRejected, validate_source_config
from oc8.oauth.errors import OAuthError
from oc8.oauth.provisioning import provision_oauth_connection, provisioned_fields
from oc8.schemas.base import CamelModel
from oc8.schemas.dto import DepartmentDTO
from oc8.schemas.paging import Page
from oc8.secrets.keyprovider import SecretStoreUnavailable
from oc8.secrets.service import store_secret

router = APIRouter()


class InstallRequest(CamelModel):
    manifest: dict[str, Any]
    author: str = ""
    origin: str = "local"
    trust_level: str | None = None


class InstantiateRequest(CamelModel):
    department_id: uuid.UUID
    name: str | None = None


class InstantiateDepartmentRequest(CamelModel):
    name: str


class CapaExportItem(BaseModel):
    kind: Literal["department", "agent", "skill", "tool_pack"]
    id: uuid.UUID
    name: str
    version: str = "1.0.0"
    summary: str = ""


class CapaExportRequest(BaseModel):
    items: list[CapaExportItem]
    dry_run: bool = False


class CapaExportPreviewItem(BaseModel):
    folder_name: str
    manifest_toml: str
    warnings: list[str]
    #: Sibling files the export produced alongside plugin.toml (e.g. a
    #: skill's `skills/<name>.toml` body) -- relative path -> content. Empty
    #: for agent/department exports, which have no sibling files of their own.
    extra_files: dict[str, str] = {}


class CapaExportPreviewResponse(BaseModel):
    items: list[CapaExportPreviewItem]
    errors: list[str]


class PluginVersionDTO(CamelModel):
    id: str
    plugin_id: str
    name: str
    type: str
    semver: str
    trust_level: str


class PluginDTO(CamelModel):
    id: str
    name: str
    type: str
    trust_level: str
    current_version_id: str | None = None


class DiscoveredPluginDTO(CamelModel):
    """A plugin folder found on disk, annotated with the calling tenant's
    installed state. Discovery is installation-wide; installation is per tenant."""

    plugin_id: str
    name: str
    # Human-readable display name (Manifest.label, e.g. "GitHub" instead of
    # "github_mcp") -- null when the manifest sets none, so the UI can fall
    # back to `name` itself rather than showing an empty string.
    label: str | None = None
    version: str
    type: str
    trust: str
    summary: str
    valid: bool
    error: str | None = None
    #: Every translation of `summary` this capa ships, keyed by locale
    #: (design: capa-i18n). Missing a locale means the browser falls back to
    #: `summary` itself.
    summary_translations: dict[str, str] = {}
    installed: bool = False
    installed_version: str | None = None
    database_id: str | None = None
    installation_status: str | None = None
    #: Why `installation_status == "disabled"`, in words -- null otherwise.
    #: "plugin update pending consent" (install_from_disk's own two-step
    #: consent flow) is a normal, expected, temporary state right after an
    #: Update click, unlike a deliberate operator disable or a quarantine --
    #: the UI reads this to NOT treat that one reason as archived.
    disabled_reason: str | None = None
    permissions: list[str] = []
    capabilities: list[str] = []
    surfaces: list[str] = []
    setup: dict[str, Any] | None = None
    # A capa-contributed row for a PERSONAL setting (Manifest.personal_settings)
    # -- distinct from `setup` above, which is the admin's one-time config form.
    # Null for every capa that declares none, which is most of them.
    personal_settings: dict[str, Any] | None = None
    source_format: str = "oc8"
    warnings: list[str] = []


def _resolve_personal_settings_translations(
    personal_settings: dict[str, Any] | None, i18n: dict[str, dict[str, str]]
) -> dict[str, Any] | None:
    """Attach every locale's translation of `personal_settings.label` under a
    `translations` key, alongside the raw manifest dict `personal_settings`
    already is. `None` in, `None` out -- most capas declare no personal
    setting at all."""
    if personal_settings is None:
        return None
    label = personal_settings.get("label")
    if not isinstance(label, str) or not label:
        return personal_settings
    translations = translations_for(i18n, label)
    if not translations:
        return personal_settings
    return {**personal_settings, "translations": {"label": translations}}


def _resolve_setup_translations(
    setup: dict[str, Any] | None, i18n: dict[str, dict[str, str]]
) -> dict[str, Any] | None:
    """Attach every locale's translation of `setup`'s own translatable text
    (`title`, `description`, `submit_label`, each field's `label`/`help`)
    under a `translations` key, alongside the raw manifest dict `setup`
    already is -- the setup form is rendered from this dict directly, never
    parsed through `PluginSetupSpec` here, so translations ride the same
    untyped shape rather than a second, structured representation."""
    if setup is None or not i18n:
        return setup
    top_level: dict[str, dict[str, str]] = {}
    for key in ("title", "description", "submit_label"):
        value = setup.get(key)
        if isinstance(value, str) and value:
            resolved = translations_for(i18n, value)
            if resolved:
                top_level[key] = resolved
    fields_by_key: dict[str, dict[str, dict[str, str]]] = {}
    for f in setup.get("fields", []):
        if not isinstance(f, dict):
            continue
        field_key = f.get("key")
        if not isinstance(field_key, str) or not field_key:
            continue
        for prop in ("label", "help"):
            value = f.get(prop)
            if isinstance(value, str) and value:
                resolved = translations_for(i18n, value)
                if resolved:
                    fields_by_key.setdefault(field_key, {})[prop] = resolved
    if not top_level and not fields_by_key:
        return setup
    translations: dict[str, Any] = dict(top_level)
    if fields_by_key:
        translations["fields"] = fields_by_key
    return {**setup, "translations": translations}


class InstallFromDiskRequest(CamelModel):
    plugin_id: str


class EnableRequest(CamelModel):
    granted_permissions: list[str] = []


class DisableRequest(CamelModel):
    reason: str | None = None


class InstallationDTO(CamelModel):
    plugin_id: str
    status: str
    granted_permissions: list[str]
    failure_count: int


class PluginSetupRequest(CamelModel):
    values: dict[str, str]


class PluginSetupResult(CamelModel):
    # None when the plugin's setup has no `mcp` block -- nothing was
    # materialised to point at, because there was nothing to materialise.
    connection_id: str | None = None


_PLUGIN_SURFACES: dict[str, list[str]] = {
    "connector": ["knowledge"],
    "model_adapter": ["models"],
    "runtime_adapter": ["agents"],
    "skill": ["skills"],
    "flow_template": ["flows"],
    "tool_pack": ["integrations"],
    "agent_template": ["agents"],
    "department_template": ["departments"],
    # An approval channel changes where a HUMAN is asked, so it belongs on the
    # approvals surface -- not with integrations, which is where an operator
    # looks for systems an AGENT reaches.
    "approval_channel": ["approvals"],
    "core_extension": ["automation"],
}


class InternalTaskDTO(CamelModel):
    id: str
    title: str
    status: str


@router.post(
    "/capas",
    response_model=PluginVersionDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def install(
    body: InstallRequest, db: DbSession, principal: CurrentPrincipal
) -> PluginVersionDTO:
    try:
        version = await install_plugin(
            db,
            tenant_id=principal.tenant_id,
            manifest_data=body.manifest,
            author=body.author,
            origin=body.origin,
            trust_level=body.trust_level,
        )
    except PluginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    plugin = await db.get(m.Capa, version.capa_id)
    assert plugin is not None
    return PluginVersionDTO(
        id=str(version.id),
        plugin_id=str(version.capa_id),
        name=plugin.name,
        type=plugin.type,
        semver=version.semver,
        trust_level=plugin.trust_level,
    )


@router.get(
    "/capas",
    response_model=list[PluginDTO],
    dependencies=[Depends(require_permission(perm(PLUGIN, VIEW)))],
)
async def list_plugins(db: DbSession, principal: CurrentPrincipal) -> list[PluginDTO]:
    rows = (await db.execute(select(m.Capa))).scalars().all()
    return [
        PluginDTO(
            id=str(p.id),
            name=p.name,
            type=p.type,
            trust_level=p.trust_level,
            current_version_id=str(p.current_version_id) if p.current_version_id else None,
        )
        for p in rows
    ]


# NOTE: the two /capas/available and /capas/install-from-disk routes must be
# declared BEFORE /capas/{capa_id}, or the path-parameter route matches first.
@router.get(
    "/capas/available",
    response_model=Page[DiscoveredPluginDTO],
    dependencies=[Depends(require_permission(perm(PLUGIN, VIEW)))],
)
async def list_available(
    db: DbSession,
    principal: CurrentPrincipal,
    search: str | None = None,
    # `type` matches the wire field name; DiscoveredPluginDTO.type is the domain field.
    type: str | None = None,
    group_by: str | None = None,
    # Same bounds as every sibling list route (agents/departments/knowledge/
    # skills): `limit=0` used to answer 200-with-nothing here while the others
    # answered 422, and an unbounded `limit` had no ceiling at all. The data
    # source is a bounded disk scan, so this is consistency, not protection.
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[DiscoveredPluginDTO]:
    """Plugin folders found on disk, each annotated with whether THIS tenant has
    installed it, plus any `origin="custom"` capa (the custom-MCP wizard) that
    has no disk folder at all -- those are synthesized straight from their
    installed `CapaVersion.manifest` in a second pass below. The Plugin query
    is tenant-scoped by RLS, exactly as in list_plugins -- another tenant's
    install must never show up as installed here.

    Unlike every other list-query endpoint in the Design System Consistency
    plan, this one cannot use `apply_search`/`apply_group_order`/`paginate`
    (backend/src/oc8/api/v1/_listquery.py) -- those operate on a SQLAlchemy
    `Select`, but `out` below comes from `discover_plugins()`, a disk scan,
    not a query. So search/group/pagination are applied to the already-built
    Python list instead, with the same semantics (case-insensitive substring
    search, group-then-default sort, slice for pagination) so the wire
    contract still matches every other endpoint: `Page[T]` with
    `{items, totalCount}`.
    """
    installed = {p.name: p for p in (await db.execute(select(m.Capa))).scalars().all()}
    discovered = list(discover_plugins())
    out: list[DiscoveredPluginDTO] = []
    for d in discovered:
        row = installed.get(d.plugin_id)
        installation = (
            (
                await db.execute(
                    select(m.CapaInstallation).where(m.CapaInstallation.capa_id == row.id)
                )
            ).scalar_one_or_none()
            if row is not None
            else None
        )
        version: str | None = None
        if row is not None and row.current_version_id is not None:
            pv = await db.get(m.CapaVersion, row.current_version_id)
            version = pv.semver if pv is not None else None
        out.append(
            DiscoveredPluginDTO(
                plugin_id=d.plugin_id,
                name=d.name,
                label=(d.manifest or {}).get("label"),
                version=d.version,
                type=d.type,
                trust=d.trust,
                summary=d.summary,
                summary_translations=translations_for(d.i18n, d.summary),
                valid=d.valid,
                error=d.error,
                installed=row is not None,
                installed_version=version,
                database_id=str(row.id) if row is not None else None,
                installation_status=installation.status if installation is not None else None,
                disabled_reason=installation.disabled_reason if installation is not None else None,
                permissions=list((d.manifest or {}).get("permissions", [])),
                capabilities=list((d.manifest or {}).get("capabilities", [])),
                surfaces=_PLUGIN_SURFACES.get(d.type, []),
                setup=_resolve_setup_translations((d.manifest or {}).get("setup"), d.i18n),
                personal_settings=_resolve_personal_settings_translations(
                    (d.manifest or {}).get("personal_settings"), d.i18n
                ),
                source_format=str((d.manifest or {}).get("source_format", "oc8")),
                warnings=list(d.warnings),
            )
        )
    # A capa installed via the custom-MCP wizard (origin="custom") has no disk
    # folder at all -- `discover_plugins()` never finds it, so without this it
    # would install and enable successfully yet never appear in this listing.
    # Its `CapaVersion.manifest` is the only source of truth for the fields a
    # disk-discovered `DiscoveredPlugin` would otherwise supply.
    disk_plugin_ids = {d.plugin_id for d in discovered}
    for row in installed.values():
        # Scoped to origin="custom" specifically (not just "absent from
        # disk"): a local/store capa whose folder was later removed or
        # renamed should not resurface here as if it were still installed.
        if row.origin != "custom" or row.name in disk_plugin_ids or row.current_version_id is None:
            continue
        pv = await db.get(m.CapaVersion, row.current_version_id)
        if pv is None:
            continue
        installation = (
            await db.execute(select(m.CapaInstallation).where(m.CapaInstallation.capa_id == row.id))
        ).scalar_one_or_none()
        manifest = pv.manifest or {}
        out.append(
            DiscoveredPluginDTO(
                plugin_id=row.name,
                name=row.name,
                label=manifest.get("label"),
                version=pv.semver,
                type=row.type,
                trust=row.trust_level,
                summary=str(manifest.get("summary", "")),
                valid=True,
                installed=True,
                installed_version=pv.semver,
                database_id=str(row.id),
                installation_status=installation.status if installation is not None else None,
                disabled_reason=(
                    installation.disabled_reason if installation is not None else None
                ),
                permissions=list(pv.permissions),
                capabilities=list(pv.capabilities),
                surfaces=_PLUGIN_SURFACES.get(row.type, []),
                setup=_resolve_setup_translations(manifest.get("setup"), {}),
                personal_settings=_resolve_personal_settings_translations(
                    manifest.get("personal_settings"), {}
                ),
                source_format=str(manifest.get("source_format", "oc8")),
            )
        )
    if search:
        needle = search.lower()
        out = [d for d in out if needle in d.name.lower() or needle in (d.summary or "").lower()]
    if type:
        out = [d for d in out if d.type == type]
    if group_by == "type":
        out = sorted(out, key=lambda d: (d.type, d.name))
    elif group_by is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{group_by!r} is not a groupable field")
    else:
        out = sorted(out, key=lambda d: d.name)
    total = len(out)
    page = out[offset : offset + limit]
    return Page(items=page, total_count=total)


@router.post(
    "/capas/{capa_id}/setup",
    response_model=PluginSetupResult,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def configure_plugin(
    capa_id: uuid.UUID,
    body: PluginSetupRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> PluginSetupResult:
    """Apply a plugin-declared setup form without vendor logic in core.

    The browser submits field values. This generic interpreter validates them
    against the installed manifest, encrypts password fields, and maps the
    remaining values into the plugin's materialised MCP connection.
    """
    plugin = await db.get(m.Capa, capa_id)
    if plugin is None or plugin.current_version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    installation = (
        await db.execute(select(m.CapaInstallation).where(m.CapaInstallation.capa_id == plugin.id))
    ).scalar_one_or_none()
    if installation is None or installation.status != "enabled":
        raise HTTPException(status.HTTP_409_CONFLICT, "plugin must be enabled before setup")
    version = await db.get(m.CapaVersion, plugin.current_version_id)
    assert version is not None
    manifest = parse_manifest(version.manifest)
    setup = manifest.setup
    if setup is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "plugin has no setup contract")

    fields = {field.key: field for field in setup.fields}
    try:
        validate_setup_values(setup, fields, body.values)
    except SetupValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    values = {key: body.values.get(key, field.default).strip() for key, field in fields.items()}
    # A later failure anywhere in this request -- the 409 below, an unresolved
    # env mapping, a setup_validate rejection -- rolls this whole transaction
    # back (tenant_session's except/rollback in db/session.py), so storing a
    # secret before its connection or hook is confirmed leaves nothing behind.
    secret_refs: dict[str, str] = {}
    # A password field a declared `oauth_provision` block consumes is stored by
    # the provisioner, under the OAuth connection's own `oauth/{id}/refresh` ref
    # -- which is the copy `_mint_client_credentials` actually reads. Writing a
    # second copy here under `plugin:{name}:{key}` gave a long-lived client
    # secret two homes, one of them with no reader at all.
    provisioned = (
        provisioned_fields(setup.oauth_provision.provider)
        if setup.oauth_provision is not None
        else frozenset()
    )
    # Every NON-secret field a kind="credential" setup field drags along from
    # its credential_type (teams_bot's app_id/tenant_id, whatsapp_api's
    # phone_number_id). They live only in `values`, never in the setup form's
    # own `fields` map, so `_configure_without_connection` -- which persists
    # by walking `fields` -- had no way to know they existed and dropped them,
    # leaving channels/registry.py with a config the plugin's build() rejects.
    credential_plain_keys: set[str] = set()
    for field in setup.fields:
        if field.kind not in ("password", "credential") or not values[field.key]:
            continue
        secret_ref = field.secret_ref or f"plugin:{manifest.name}:{field.key}"
        # Which raw key `provisioned_fields()` would name -- for kind="credential"
        # this is corrected below to the credential's OWN secret field key
        # (e.g. "client_secret"), not this setup field's wrapping key (e.g.
        # "app"), which differ once several fields are bundled behind one
        # credential (google_workspace/microsoft365).
        provisioned_key = field.key
        if field.kind == "credential":
            try:
                credential_id = uuid.UUID(values[field.key])
            except ValueError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"setup field {field.key!r}: not a valid credential id",
                ) from exc
            cred_type = await get_credential_type(
                db, tenant_id=principal.tenant_id, name=field.credential_type
            )
            secret_fields = [f for f in cred_type.fields if f.kind == "password"]
            if len(secret_fields) != 1:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    f"credential_type {field.credential_type!r} has "
                    f"{len(secret_fields)} secret fields; a setup field of "
                    "kind='credential' requires exactly one",
                )
            try:
                resolved_value = await resolve_credential_field(
                    db,
                    tenant_id=principal.tenant_id,
                    credential_id=credential_id,
                    field_key=secret_fields[0].key,
                )
            except CredentialNotFound as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"setup field {field.key!r}: no such credential",
                ) from exc
            except CredentialFieldNotSet as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"setup field {field.key!r}: credential {exc.credential_name!r} has no "
                    f"value set for {exc.field_key!r}",
                ) from exc
            # Every field the credential_type declares rides along
            # automatically -- written into `values` under the credential
            # type's OWN field key, exactly as if each had been its own
            # plain setup field. This is what lets "the login" be one
            # reusable credential instead of a secret half in the credential
            # store and a plain half left sitting in the setup form -- and,
            # for the secret field itself, what lets a consumer reading
            # `values` synchronously later in THIS SAME request
            # (`oauth_provision`'s provider-specific minting call, e.g.
            # `_provision_microsoft` reading `values["client_secret"]`
            # verbatim) see the real resolved secret rather than the
            # credential id this field was submitted as. Every OTHER (async,
            # bridge-launch-time) consumer reads `secret_refs`/the stored ref
            # instead, so writing the plaintext here is safe.
            try:
                cred_row = await get_credential(
                    db, tenant_id=principal.tenant_id, credential_id=credential_id
                )
            except CredentialNotFound as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"setup field {field.key!r}: no such credential",
                ) from exc
            for other in cred_type.fields:
                values[other.key] = (
                    resolved_value
                    if other.key == secret_fields[0].key
                    else str(cred_row.field_values.get(other.key, ""))
                )
            credential_plain_keys.update(
                other.key for other in cred_type.fields if other.kind != "password"
            )
            provisioned_key = secret_fields[0].key
        else:
            resolved_value = values[field.key]
        if provisioned_key in provisioned:
            # The OAuth provisioner stores its own copy under the OAuth
            # connection's own ref (see provisioned_fields' docstring) -- a
            # second copy here nothing reads is only blast radius.
            continue
        try:
            await store_secret(
                db,
                tenant_id=principal.tenant_id,
                name=secret_ref,
                value=resolved_value,
                kind="plugin_credential",
            )
        except SecretStoreUnavailable as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        secret_refs[field.key] = secret_ref

    if setup.mcp is None:
        return await _configure_without_connection(
            db,
            manifest=manifest,
            setup=setup,
            installation=installation,
            fields=fields,
            values=values,
            credential_plain_keys=frozenset(credential_plain_keys),
        )

    connections = (await db.execute(select(m.McpConnection))).scalars().all()
    candidates = [
        item
        for item in connections
        if (item.config or {}).get("_plugin_name") == manifest.name
        and (item.config or {}).get("_connection_key") == setup.mcp.connection_key
    ]
    if not candidates:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "plugin connection was not materialised; enable the current plugin version again",
        )

    env = dict(setup.mcp.env)
    for env_name, field_key in setup.mcp.env_fields.items():
        if field_key not in values:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"environment mapping references unknown field {field_key!r}",
            )
        if values[field_key]:
            env[env_name] = values[field_key]
        else:
            env.pop(env_name, None)
    secret_env: dict[str, str] = {}
    for env_name, field_key in setup.mcp.secret_env_fields.items():
        # "credential" is accepted here for the same reason the secret_refs
        # loop above already treats it like "password": a kind="credential"
        # field resolves to a secret (the referenced credential_type's own
        # single password field) and gets stored under secret_refs[field.key]
        # exactly the same way -- this is what lets an MCP-connected capa's
        # password field migrate onto the shared credential store without
        # changing anything else about how its secret_env mapping works.
        if field_key not in fields or fields[field_key].kind not in ("password", "credential"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"secret mapping references non-password/credential field {field_key!r}",
            )
        if field_key in secret_refs:
            secret_env[env_name] = secret_refs[field_key]

    target_department_id: uuid.UUID | None = None
    if setup.mcp.department_field:
        raw_department = values.get(setup.mcp.department_field, "")
        if raw_department:
            try:
                target_department_id = uuid.UUID(raw_department)
            except ValueError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid department id"
                ) from exc
            if await db.get(m.Department, target_department_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "department not found")

    # One connection PER DEPARTMENT. Setting the same tool pack up for a second
    # department used to move the single materialised connection and reset it to
    # disconnected, so configuring it for support silently cut sales off from the
    # same system -- no error, no warning, just a later run with no tools.
    conn = next((c for c in candidates if c.department_id == target_department_id), None)
    if conn is None:
        # An unbound connection is the freshly materialised one: adopt it rather
        # than leaving an orphan behind. Otherwise this department gets its own,
        # seeded from the MANIFEST so it carries the same value/focus specs the
        # first one got -- a copy of the sibling row would drift the moment the
        # plugin ships a new version.
        conn = next((c for c in candidates if c.department_id is None), None)
    if conn is None:
        template = dict(candidates[0].config or {})
        manifest_conn = next(
            (
                c
                for c in (manifest.tool_pack.connections if manifest.tool_pack else [])
                if c.key == setup.mcp.connection_key
            ),
            None,
        )
        base_config = dict(manifest_conn.config) if manifest_conn is not None else template
        base_config["_plugin_name"] = manifest.name
        base_config["_connection_key"] = setup.mcp.connection_key
        # Same source as base_config, for the same reason: a second department's
        # connection is never materialised by materialise.py, so a bare
        # `list(setup.mcp.scopes)` (empty for every tool pack in this repo) would
        # leave it with no read/send classification at all -- and pdp.py reads
        # "no classification" as "write", which every preset here forbids. That
        # department would get a connection with zero usable tools.
        base_scopes: list[str] | dict[str, Any] = list(setup.mcp.scopes)
        if not base_scopes:
            base_scopes = (
                manifest_conn.scopes if manifest_conn is not None else candidates[0].scopes
            )
        conn = m.McpConnection(
            tenant_id=principal.tenant_id,
            name=setup.mcp.name,
            transport=candidates[0].transport,
            server_url=candidates[0].server_url,
            scopes=base_scopes,
            config=base_config,
            connected=False,
            health={},
        )
        db.add(conn)
        await db.flush()
    conn.department_id = target_department_id

    cfg = dict(conn.config or {})
    # Only the entries this form actually MANAGES are replaced. A manifest may
    # declare env/secret_env the form knows nothing about -- microsoft365's
    # PYTHONPATH for its bridge, and its OAuth-backed GRAPH_ACCESS_TOKEN -- and
    # materialise.py copied those onto the row at enable time. Overwriting the
    # whole map (as this used to) wiped them on every submit, leaving a bridge
    # that could not import itself and had no token.
    managed_env = set(setup.mcp.env) | set(setup.mcp.env_fields)
    managed_secret_env = set(setup.mcp.secret_env_fields)
    env = {k: v for k, v in dict(cfg.get("env", {})).items() if k not in managed_env} | env
    secret_env = {
        k: v for k, v in dict(cfg.get("secret_env", {})).items() if k not in managed_secret_env
    } | secret_env
    cfg.update(
        {
            "command": setup.mcp.command,
            "args": list(setup.mcp.args),
            "env": env,
            "secret_env": secret_env,
        }
    )

    if setup.oauth_provision is not None:
        cfg["oauth_connection_id"] = str(
            await _provision_from_setup(
                db,
                manifest=manifest,
                provision=setup.oauth_provision,
                values=values,
                # The KEYS the browser actually sent, not the full set of
                # declared fields: only a field the admin submitted -- even
                # empty -- may be read as "clear this".
                submitted=set(body.values),
                tenant_id=principal.tenant_id,
            )
        )
        # Stale-value safety, same discipline as the env/secret_env merge above:
        # drop any PREVIOUSLY-written delegated entries before adding this
        # submission's set, so shrinking the identity list on a resubmit
        # actually removes the tokens for identities that were removed, rather
        # than leaving them to silently mint forever.
        if setup.oauth_provision.delegated_identities_field:
            stale_env_key = setup.oauth_provision.delegated_identities_env
            stale_prefix = f"{setup.oauth_provision.delegated_token_env_prefix}_"
            cfg["env"] = {k: v for k, v in cfg.get("env", {}).items() if k != stale_env_key}
            cfg["secret_env"] = {
                k: v for k, v in cfg.get("secret_env", {}).items() if not k.startswith(stale_prefix)
            }
            delegated_env, delegated_secret_env = _build_delegated_identity_config(
                setup.oauth_provision, values=values
            )
            cfg["env"].update(delegated_env)
            cfg["secret_env"].update(delegated_secret_env)
            # Plain, non-secret string: which scope `mint_delegated_token`
            # (via mcp_env.py's "oauth-delegated:" resolution) mints every
            # identity's token with. Manifest-declared, never a default baked
            # into core.
            cfg["delegated_scope"] = setup.oauth_provision.delegated_scope

    conn.name = setup.mcp.name
    # Only when the form's own spec declares scopes. `SetupMcpSpec.scopes`
    # defaults to `[]` and no tool pack in this repo sets it, so this line used
    # to wipe the {"read": [...], "send": [...]} classification materialise.py
    # wrote from the manifest -- after which authz/pdp.py classifies every tool
    # as "write" (unclassified == write, fail-closed) and every preset that sets
    # `write = false` denies the entire pack. Same "preserve what the form does
    # not manage" rule as the env/secret_env merge above.
    if setup.mcp.scopes:
        conn.scopes = list(setup.mcp.scopes)
    conn.config = cfg
    conn.connected = False
    conn.health = {}
    await db.commit()
    return PluginSetupResult(connection_id=str(conn.id))


def _build_delegated_identity_config(
    provision: SetupOAuthProvision, *, values: dict[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """The plain `env` entry (comma-joined identities, in order) plus the
    indexed `secret_env` entries (one `oauth-delegated:<identity>` ref per
    identity) a manifest's `delegated_identities_*` fields describe -- or two
    empty dicts if the manifest declares none of this (every plugin before
    google_workspace)."""
    if not provision.delegated_identities_field:
        return {}, {}
    raw = values.get(provision.delegated_identities_field, "")
    identities = [part.strip() for part in raw.split(",") if part.strip()]
    if not identities:
        return {}, {}
    env = {provision.delegated_identities_env: ",".join(identities)}
    secret_env = {
        f"{provision.delegated_token_env_prefix}_{i}": f"oauth-delegated:{identity}"
        for i, identity in enumerate(identities)
    }
    return env, secret_env


def _parse_id_list(raw: str) -> list[str]:
    return [part for part in (piece.strip() for piece in raw.split(",")) if part]


async def _provision_from_setup(
    db: DbSession,
    *,
    manifest: Manifest,
    provision: SetupOAuthProvision,
    values: dict[str, str],
    submitted: set[str],
    tenant_id: uuid.UUID,
) -> uuid.UUID:
    """The `[plugin.setup.oauth_provision]` half of a setup submission.

    Still no vendor logic in core: the manifest names a provider core already
    has in `oauth/providers.py`, and `oauth/provisioning.py` -- the same code
    the `/oauth/{provider}/service-connection` endpoint calls -- does the
    minting. What this adds is the second row a combined tool-pack/connector
    plugin needs: a DataSource pointing at the same connection, so one form
    connects both the tools and the knowledge base.
    """
    try:
        oauth_conn = await provision_oauth_connection(
            db, tenant_id=tenant_id, provider=provision.provider, values=values
        )
    except OAuthError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"could not authenticate: {exc}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    if not (provision.connector_type and provision.site_ids_field):
        return oauth_conn.id

    config_key = provision.source_ids_config_key
    existing = (
        await db.execute(
            select(m.DataSource).where(
                m.DataSource.tenant_id == tenant_id,
                m.DataSource.connector_type == provision.connector_type,
                m.DataSource.oauth_connection_id == oauth_conn.id,
            )
        )
    ).scalar_one_or_none()

    # Re-submitting the form is how an admin adds or removes a site/drive, so
    # each id list is replaced, not merged -- but the rest of the config
    # (maxFiles, whatever else a connector defines) is the operator's and
    # stays. site_ids and drive_ids are independent: emptying one leaves the
    # other's ids, and this connector's config, untouched.
    config = dict(existing.config or {}) if existing else {}
    touched = False
    if provision.site_ids_field in submitted:
        config[config_key] = _parse_id_list(values.get(provision.site_ids_field, ""))
        touched = True
    if provision.drive_ids_field and provision.drive_ids_field in submitted:
        config["driveIds"] = _parse_id_list(values.get(provision.drive_ids_field, ""))
        touched = True

    if not (config.get(config_key) or config.get("driveIds")):
        # Emptying every id field is the only way an admin has to say "stop
        # indexing this" -- the form's own help text invites it ("leave empty
        # to skip the knowledge base for now"). Guarding the whole block on a
        # non-empty list made that a no-op: the source kept its old ids and
        # kept syncing. Disconnected, not deleted: `tombstone_source` owns
        # deletion, and a retired source's ingested documents are still
        # accounted for by the row.
        if existing is not None and touched:
            existing.config = config
            existing.connected = False
            await db.flush()
        return oauth_conn.id

    # The same two checks POST /knowledge/sources runs, for the same reason:
    # a connector_type no plugin contributes would otherwise become a dead row,
    # and validate() is what actually proves the credentials reach the far
    # system -- for Microsoft 365 it is the GET /organization call that
    # distinguishes "the secret is valid" from "the Graph permissions were
    # admin-consented", which minting a token cannot tell apart.
    try:
        await validate_source_config(
            db,
            tenant_id=tenant_id,
            connector_type=provision.connector_type,
            config=config,
            oauth_connection_id=oauth_conn.id,
        )
    except (ConnectorError, SourceRejected) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"knowledge source rejected: {exc}"
        ) from exc

    if existing is not None:
        existing.config = config
        existing.connected = True
    else:
        db.add(
            m.DataSource(
                tenant_id=tenant_id,
                connector_type=provision.connector_type,
                name=f"{manifest.name} knowledge source",
                config=config,
                classification="internal",
                oauth_connection_id=oauth_conn.id,
                connected=True,
            )
        )
    await db.flush()
    return oauth_conn.id


async def _configure_without_connection(
    db: DbSession,
    *,
    manifest: Manifest,
    setup: PluginSetupSpec,
    installation: m.CapaInstallation,
    fields: dict[str, SetupFieldSpec],
    values: dict[str, str],
    credential_plain_keys: frozenset[str] = frozenset(),
) -> PluginSetupResult:
    """The other half of configure_plugin: a plugin with `setup` but no `mcp`
    block. There is no connection to adopt, so this only has two jobs -- keep
    the non-secret values somewhere channels/registry.py can read them back,
    and, if the plugin asked to be taken at its word, prove the credential it
    was just handed actually works before either of those become durable."""
    plain = {
        key: values[key]
        for key, field in fields.items()
        if field.kind not in ("password", "credential") and values[key]
    }
    # Plus the non-secret fields a kind="credential" field brought with it.
    # Walking `fields` alone misses them entirely -- they are credential_type
    # fields, not setup-form fields -- which is how teams_approvals' app_id and
    # whatsapp_approvals' phone_number_id were resolved, used for validate(),
    # and then silently dropped instead of being written to the installation
    # config the channel registry rebuilds the channel from. The credential's
    # own password field is never here: configure_plugin excludes it.
    plain.update({key: values[key] for key in credential_plain_keys if values.get(key)})
    if plain:
        installation.config = {**(installation.config or {}), **plain}

    validate_spec = setup.validate_entry_point
    if validate_spec:
        discovered = find_plugin(manifest.name)
        if discovered is None or discovered.trust not in TRUSTED:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "plugin's setup validator is not available (not found or not trusted)",
            )
        try:
            validate = import_entry_point(discovered.path, validate_spec)
        except LoaderError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        try:
            await validate(values)  # type: ignore[operator]
        except Exception as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    await db.commit()
    return PluginSetupResult(connection_id=None)

@router.post(
    "/capas/install-from-disk",
    response_model=PluginVersionDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def install_from_disk(
    body: InstallFromDiskRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> PluginVersionDTO:
    """Install a discovered plugin for the calling tenant. The request names a
    plugin ID, never a path -- the plugins path is operator configuration, so a
    caller can only ever install something the operator already placed on disk.
    Reading the manifest imports no plugin code."""
    found = find_plugin(body.plugin_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found on disk")
    if not found.valid or found.manifest is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, found.error or "invalid manifest")
    # Updating an enabled plugin is a two-step consent flow. Disable the old
    # version before switching current_version_id; the UI will then require an
    # explicit Enable for the new manifest and its permissions.
    existing_plugin = (
        await db.execute(select(m.Capa).where(m.Capa.name == found.name))
    ).scalar_one_or_none()
    if existing_plugin is not None:
        existing_installation = (
            await db.execute(
                select(m.CapaInstallation).where(m.CapaInstallation.capa_id == existing_plugin.id)
            )
        ).scalar_one_or_none()
        if existing_installation is not None and existing_installation.status == "enabled":
            await disable_plugin(
                db,
                tenant_id=principal.tenant_id,
                capa_id=existing_plugin.id,
                reason="plugin update pending consent",
            )
    try:
        # origin stays "local": ck_plugin_origin allows only local|store, and a
        # plugin read off this installation's filesystem IS local (as opposed to
        # fetched from a store). Provenance is anyway derivable -- /capas/available
        # cross-references installed rows against what discovery finds on disk.
        version = await install_with_dependencies(
            db, tenant_id=principal.tenant_id, found=found, origin="local"
        )
    except MissingDependencyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PluginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    assert version is not None  # the requested plugin is never in `_seen` on entry
    plugin = await db.get(m.Capa, version.capa_id)
    assert plugin is not None
    return PluginVersionDTO(
        id=str(version.id),
        plugin_id=str(version.capa_id),
        name=plugin.name,
        type=plugin.type,
        semver=version.semver,
        trust_level=plugin.trust_level,
    )


@router.get(
    "/capas/{capa_id}",
    response_model=PluginDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, VIEW)))],
)
async def get_plugin(capa_id: uuid.UUID, db: DbSession, principal: CurrentPrincipal) -> PluginDTO:
    p = await db.get(m.Capa, capa_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    return PluginDTO(
        id=str(p.id),
        name=p.name,
        type=p.type,
        trust_level=p.trust_level,
        current_version_id=str(p.current_version_id) if p.current_version_id else None,
    )


#: Extension -> Content-Type for `GET /capas/{capa_id}/icon`. Deliberately
#: closed: this is the only thing standing between a manifest's `icon` field
#: (author-controlled, not operator-controlled once a plugin is community
#: trust) and turning this endpoint into an arbitrary-file-read primitive.
_ICON_CONTENT_TYPES: dict[str, str] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _resolve_plugin_icon_path(folder: str, rel_path: str) -> str | None:
    """`folder`-relative `rel_path` resolved to a real path, or None if it
    contains a `..` segment or resolves outside `folder`. Same gate as
    `_resolve_workspace_path` in api/v1/run.py, adapted to a plugin's own
    on-disk directory instead of a run's workspace dir."""
    if ".." in rel_path.split("/"):
        return None
    root_real = os.path.realpath(folder)
    candidate_real = os.path.realpath(os.path.join(folder, rel_path))
    if candidate_real != root_real and not candidate_real.startswith(root_real + os.sep):
        return None
    return candidate_real


def _read_plugin_icon(folder: str, rel_path: str) -> bytes | None:
    """Resolve `rel_path` safely under `folder` and return its raw bytes, or
    None if it is outside the folder, doesn't exist, or isn't a regular file.
    A plain sync function so it can run inside `asyncio.to_thread`."""
    resolved = _resolve_plugin_icon_path(folder, rel_path)
    if resolved is None or not os.path.isfile(resolved):
        return None
    with open(resolved, "rb") as fh:
        return fh.read()


@router.get(
    "/capas/{capa_id}/icon",
    dependencies=[Depends(require_permission(perm(PLUGIN, VIEW)))],
)
async def plugin_icon(capa_id: uuid.UUID, db: DbSession) -> Response:
    """The plugin's manifest-declared icon file. An icon is exactly as
    sensitive as the plugin's own listing (`GET /capas/available`), so this
    is gated by the same `PLUGIN, VIEW` permission, no more and no less.

    404 whenever there is nothing safe to serve -- no such plugin, the plugin
    is no longer on disk, its manifest declares no `icon`, the declared name
    isn't an allowlisted image extension, or the resolved path doesn't exist
    inside the plugin's own folder -- rather than distinguishing those cases
    for the caller: the Capas UI's only reaction to any of them is the same
    generic per-type fallback icon.
    """
    plugin = await db.get(m.Capa, capa_id)
    if plugin is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    discovered = find_plugin(plugin.name)
    if discovered is None or not discovered.valid or discovered.manifest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found on disk")
    try:
        manifest = parse_manifest(discovered.manifest)
    except ManifestError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin manifest invalid") from exc
    if not manifest.icon:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin declares no icon")
    media_type = _ICON_CONTENT_TYPES.get(os.path.splitext(manifest.icon)[1].lower())
    if media_type is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "icon file type not allowed")
    content = await asyncio.to_thread(_read_plugin_icon, discovered.path, manifest.icon)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "icon file not found")
    # An icon is a static file on disk that only changes on redeploy -- with
    # ~200 Capas in the catalog, opening the Capas page without this fired
    # ~200 uncached GETs (each a DB lookup + a full discover_plugins() call +
    # a disk read) every single time. The frontend already caches the
    # decoded blob for the tab's lifetime (`useCapaIcon`'s `staleTime:
    # Infinity`); this is what makes a hard reload or a fresh tab cheap too.
    # `private`, not `public`: this route is gated by `PLUGIN, VIEW` and a
    # `public` directive lets any shared/intermediate cache (a proxy in
    # front of the API) serve a cached response to a DIFFERENT, unauthorized
    # caller without ever re-checking that permission -- `private` confines
    # the cache to the browser making the (already-authorized) request.
    return Response(
        content=content, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"}
    )


@router.post(
    "/capas/{capa_id}/instantiate",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def instantiate(
    capa_id: uuid.UUID,
    body: InstantiateRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> dict[str, str]:
    plugin = await db.get(m.Capa, capa_id)
    if plugin is None or plugin.current_version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin or version not found")
    version = await db.get(m.CapaVersion, plugin.current_version_id)
    assert version is not None
    try:
        agent = await instantiate_agent(
            db,
            tenant_id=principal.tenant_id,
            version=version,
            department_id=body.department_id,
            name=body.name,
        )
    except PluginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"agentId": str(agent.id)}


@router.post(
    "/capas/{capa_id}/instantiate-department",
    response_model=DepartmentDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def instantiate_department_endpoint(
    capa_id: uuid.UUID,
    body: InstantiateDepartmentRequest,
    db: DbSession,
    principal: CurrentPrincipal,
) -> DepartmentDTO:
    plugin = await db.get(m.Capa, capa_id)
    if plugin is None or plugin.current_version_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin or version not found")
    version = await db.get(m.CapaVersion, plugin.current_version_id)
    assert version is not None
    try:
        dept = await instantiate_department(
            db, tenant_id=principal.tenant_id, version=version, name=body.name
        )
    except PluginError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await db.commit()
    return department_to_dto(dept)


@router.post(
    "/capas/export",
    # The two response shapes (a raw ZIP `Response` vs. the JSON preview DTO)
    # are a Union FastAPI cannot turn into one response_model on its own --
    # response_model=None disables that inference so each branch just returns
    # what it actually is.
    response_model=None,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def export_capas(
    body: CapaExportRequest, db: DbSession, principal: CurrentPrincipal
) -> Response | CapaExportPreviewResponse:
    """Render a department/agent/skill selection into capa manifests, either as
    a JSON preview (`dry_run`) for the export wizard's Preview step, or as a
    downloadable ZIP -- the same `Manifest`/`build_*_export` pair either way,
    so what the wizard previews is exactly what the ZIP contains."""
    from oc8.capas.export import (
        ExportedCapa,
        ExportValidationError,
        build_agent_export,
        build_department_export,
        build_skill_export,
        build_tool_pack_export,
    )
    from oc8.capas.export_package import build_zip

    results: list[ExportedCapa] = []
    errors: list[str] = []
    seen_folder_names: set[str] = set()
    for item in body.items:
        if item.name in seen_folder_names:
            errors.append(f"duplicate capa name in this export: {item.name!r}")
            continue
        seen_folder_names.add(item.name)
        try:
            if item.kind == "department":
                exported = await build_department_export(
                    db,
                    tenant_id=principal.tenant_id,
                    department_id=item.id,
                    capa_name=item.name,
                    version=item.version,
                    summary=item.summary,
                )
            elif item.kind == "agent":
                exported = await build_agent_export(
                    db,
                    tenant_id=principal.tenant_id,
                    agent_id=item.id,
                    capa_name=item.name,
                    version=item.version,
                    summary=item.summary,
                )
            elif item.kind == "tool_pack":
                exported = await build_tool_pack_export(
                    db, tenant_id=principal.tenant_id, capa_id=item.id
                )
            else:
                exported = await build_skill_export(
                    db,
                    tenant_id=principal.tenant_id,
                    skill_id=item.id,
                    capa_name=item.name,
                    version=item.version,
                    summary=item.summary,
                )
        except ExportValidationError as exc:
            errors.append(str(exc))
            continue
        results.append(exported)

    # A dry_run preview returns 200 with `errors` populated -- the wizard's
    # Preview step is the designed inline-error surface (export.py's own
    # docstring: "the wizard's Preview step must show inline -- never a
    # silently dropped item") and can only render that if the response
    # actually reaches the frontend as data. A real (non-dry_run) export
    # still 422s: there is no ZIP to hand back for a selection that failed
    # validation, so raising is the only option.
    if errors and not body.dry_run:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"errors": errors})

    if body.dry_run:
        return CapaExportPreviewResponse(
            items=[
                CapaExportPreviewItem(
                    folder_name=r.folder_name,
                    manifest_toml=r.manifest_toml,
                    warnings=r.warnings,
                    extra_files=r.extra_files,
                )
                for r in results
            ],
            errors=errors,
        )

    data = build_zip(results)
    filename = "capa-export.zip" if len(results) != 1 else f"{results[0].folder_name}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/capas/{capa_id}/enable",
    response_model=InstallationDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def enable(
    capa_id: uuid.UUID, body: EnableRequest, db: DbSession, principal: CurrentPrincipal
) -> InstallationDTO:
    try:
        inst = await enable_plugin(
            db,
            tenant_id=principal.tenant_id,
            capa_id=capa_id,
            granted_permissions=body.granted_permissions,
        )
    except ConsentError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except QuarantinedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PluginError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return InstallationDTO(
        plugin_id=str(inst.capa_id),
        status=inst.status,
        granted_permissions=list(inst.granted_permissions),
        failure_count=inst.failure_count,
    )


@router.get(
    "/capas/internal/tasks/{task_id}",
    response_model=InternalTaskDTO,
    dependencies=[
        Depends(
            unguarded(
                "for a plugin principal, which carries scopes rather than an "
                "operator role; require_scope on this route is the gate"
            )
        )
    ],
)
async def internal_task(
    task_id: uuid.UUID,
    db: DbSession,
    principal: Principal = Depends(require_scope("api:tasks.read")),
) -> InternalTaskDTO:
    """Minimal tenant-scoped read demonstrating the plugin scope gate."""
    task = await db.get(m.Task, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return InternalTaskDTO(id=str(task.id), title=task.title, status=task.state)


@router.post(
    "/capas/{capa_id}/disable",
    response_model=InstallationDTO,
    dependencies=[Depends(require_permission(perm(PLUGIN, MANAGE)))],
)
async def disable(
    capa_id: uuid.UUID, body: DisableRequest, db: DbSession, principal: CurrentPrincipal
) -> InstallationDTO:
    try:
        inst = await disable_plugin(
            db, tenant_id=principal.tenant_id, capa_id=capa_id, reason=body.reason
        )
    except PluginError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return InstallationDTO(
        plugin_id=str(inst.capa_id),
        status=inst.status,
        granted_permissions=list(inst.granted_permissions),
        failure_count=inst.failure_count,
    )
