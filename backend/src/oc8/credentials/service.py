"""CRUD + test for the `credential` table (design §4). Every secret-kind
field's plaintext is written to the existing `oc8.secrets.service` vault
under a generated name (`credential/{credential.id}/{field_key}`) -- this
module adds no new encryption path, only a named, listable, testable shell
around it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import json
import logging
import uuid
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.loader import TRUSTED, import_entry_point
from oc8.credentials.registry import find_credential_type_owner, get_credential_type
from oc8.net.outbound import OutboundBlocked, guard_outbound_url
from oc8.secrets.service import resolve_secret, store_secret

logger = logging.getLogger(__name__)

#: Which owner a lookup accepts. "any": internal callers holding an id the
#: system itself stored (a running bridge, the mailer). "organisation": every
#: admin surface -- a personal credential does not exist there. A member id:
#: that member's own rows (the connections API).
OwnerFilter = uuid.UUID | Literal["any", "organisation"]


def _owner_matches(row: m.Credential, owner: OwnerFilter) -> bool:
    if owner == "any":
        return True
    if owner == "organisation":
        return row.owner_member_id is None
    return row.owner_member_id == owner


class CredentialNotFound(Exception):
    """The credential ROW itself does not exist for this tenant (wrong id,
    wrong tenant, or already deleted). Never raised for a field that was
    simply never set on an otherwise-real credential -- see
    `CredentialFieldNotSet` for that case."""


class CredentialFieldNotSet(Exception):
    """The credential row exists, but `field_key` was never given a value on
    it -- e.g. an optional field (`s3_api`'s `endpoint`, "leave empty for
    AWS") left blank at creation. Distinct from `CredentialNotFound` so a
    caller can tell "this credential doesn't exist" apart from "this
    credential exists but is missing one field" and produce an error message
    that says which one actually happened."""

    def __init__(self, credential_name: str, field_key: str) -> None:
        self.credential_name = credential_name
        self.field_key = field_key
        super().__init__(f"credential {credential_name!r} has no value set for field {field_key!r}")


class CredentialInUse(Exception):
    """Refused to delete: something still references this credential."""

    def __init__(self, references: list[str]) -> None:
        self.references = references
        super().__init__(f"credential is in use by: {', '.join(references)}")


def _secret_ref(credential_id: uuid.UUID, field_key: str) -> str:
    return f"credential/{credential_id}/{field_key}"


async def create_credential(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
    credential_type: str,
    field_values: dict[str, Any],
    owner_member_id: uuid.UUID | None = None,
) -> m.Credential:
    cred_type = await get_credential_type(db, tenant_id=tenant_id, name=credential_type)
    secret_keys = {f.key for f in cred_type.fields if f.kind == "password"}
    plain_values = {k: v for k, v in field_values.items() if k not in secret_keys}
    # Backfill a non-secret field's TOML-declared `default` when the caller's
    # `field_values` omits it entirely. A `CredentialPicker` create-form only
    # ever shows `field.default` as a placeholder -- it never commits that
    # value into what actually gets submitted unless the user retypes it
    # (frontend/src/components/credential-picker.tsx). Without this, a field
    # left untouched (e.g. s3_api's `region`, pre-filled "us-east-1" and easy
    # to mistake for already-set) silently ends up with NO key at all in
    # `field_values`, and `resolve_credential_field` later raises
    # `CredentialNotFound` -- a misleading error that hides the real cause.
    # Only for a brand-new credential: nothing has been set yet, so a
    # declared default can never clobber a real value the user chose.
    for field in cred_type.fields:
        if field.key in secret_keys or field.key in plain_values:
            continue
        if field.default:
            plain_values[field.key] = field.default
    cred = m.Credential(
        tenant_id=tenant_id,
        name=name,
        credential_type=credential_type,
        field_values=plain_values,
        secret_refs={},
        owner_member_id=owner_member_id,
    )
    db.add(cred)
    await db.flush()
    secret_refs: dict[str, str] = {}
    for key in secret_keys:
        value = field_values.get(key)
        if not value:
            continue
        ref = _secret_ref(cred.id, key)
        await store_secret(db, tenant_id=tenant_id, name=ref, value=value, kind="credential")
        secret_refs[key] = ref
    cred.secret_refs = secret_refs
    await db.flush()
    return cred


async def list_credentials(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_type: str | None = None,
    owner: OwnerFilter = "any",
) -> list[m.Credential]:
    stmt = select(m.Credential).where(m.Credential.tenant_id == tenant_id)
    if credential_type is not None:
        stmt = stmt.where(m.Credential.credential_type == credential_type)
    if owner == "organisation":
        stmt = stmt.where(m.Credential.owner_member_id.is_(None))
    elif owner != "any":
        stmt = stmt.where(m.Credential.owner_member_id == owner)
    rows = (await db.execute(stmt.order_by(m.Credential.name))).scalars().all()
    return list(rows)


async def get_credential(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_id: uuid.UUID,
    owner: OwnerFilter = "any",
) -> m.Credential:
    row = await db.get(m.Credential, credential_id)
    if row is None or row.tenant_id != tenant_id or not _owner_matches(row, owner):
        raise CredentialNotFound(str(credential_id))
    return row


async def update_credential(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_id: uuid.UUID,
    name: str | None = None,
    field_values: dict[str, Any] | None = None,
    owner: OwnerFilter = "any",
) -> m.Credential:
    cred = await get_credential(db, tenant_id=tenant_id, credential_id=credential_id, owner=owner)
    if name is not None:
        cred.name = name
    if field_values is not None:
        cred_type = await get_credential_type(db, tenant_id=tenant_id, name=cred.credential_type)
        secret_keys = {f.key for f in cred_type.fields if f.kind == "password"}
        plain_updates = {k: v for k, v in field_values.items() if k not in secret_keys}
        cred.field_values = {**cred.field_values, **plain_updates}
        secret_refs = dict(cred.secret_refs)
        for key in secret_keys:
            value = field_values.get(key)
            # Omitted or blank means "leave unchanged" -- a PATCH here never
            # requires re-submitting every secret just to rename a credential.
            if not value:
                continue
            ref = secret_refs.get(key) or _secret_ref(cred.id, key)
            await store_secret(db, tenant_id=tenant_id, name=ref, value=value, kind="credential")
            secret_refs[key] = ref
        cred.secret_refs = secret_refs
    await db.flush()
    return cred


async def delete_credential(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_id: uuid.UUID,
    owner: OwnerFilter = "any",
) -> None:
    cred = await get_credential(db, tenant_id=tenant_id, credential_id=credential_id, owner=owner)
    references = await _find_references(db, tenant_id=tenant_id, credential_id=credential_id)
    if references:
        raise CredentialInUse(references)
    await db.delete(cred)
    await db.flush()


async def _find_references(
    db: AsyncSession, *, tenant_id: uuid.UUID, credential_id: uuid.UUID
) -> list[str]:
    """Named references to a credential, checked before allowing deletion.

    Task 12 adds the first check here: a DataSource whose config names this
    credential (the unified credentials framework's `s3_source` migration,
    `capas/s3_source/connector/connector.py`).

    Task 13 (telegram_approvals) investigated adding a CapaInstallation check
    here and concluded none is needed for THIS migration's shape: a setup
    field of kind="credential" is resolved-and-copied by configure_plugin
    (Task 7) at submit time -- the resolved plaintext lands under the
    plugin's own fixed `secret_ref` in the secret store, and the
    CapaInstallation row itself never stores the raw credential id anywhere.
    There is therefore nothing to look up here for a plugin shaped like
    telegram_approvals. A future credential_type consumed by a field that
    DOES persist the raw credential id (rather than resolving it
    immediately) would need a real check added here, following the same
    pattern as the DataSource check above -- this function remains the
    single place to extend when that happens.
    """
    references: list[str] = []
    sources = (
        (
            await db.execute(
                select(m.DataSource).where(
                    m.DataSource.tenant_id == tenant_id, m.DataSource.deleted_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    cred_id_str = str(credential_id)
    for source in sources:
        if (source.config or {}).get("credential") == cred_id_str:
            references.append(f"data source {source.name!r}")
    return references


async def resolve_credential_field(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_id: uuid.UUID,
    field_key: str,
    owner: OwnerFilter = "any",
) -> str:
    """Raises `CredentialNotFound` when the credential ROW itself does not
    exist for this tenant, or `CredentialFieldNotSet` when it exists but
    `field_key` was never given a value (e.g. an optional field left blank)
    -- two different failure modes a caller needs to tell apart to report an
    accurate error (design note from Task 4's review, confirmed live by
    Task 15's E2E pass: a blank optional `endpoint` on an otherwise-valid S3
    credential must not be reported as "credential not found")."""
    cred = await get_credential(db, tenant_id=tenant_id, credential_id=credential_id, owner=owner)
    ref = cred.secret_refs.get(field_key)
    if ref is not None:
        return await resolve_secret(db, tenant_id=tenant_id, ref=ref)
    value = cred.field_values.get(field_key)
    if value is None:
        raise CredentialFieldNotSet(cred.name, field_key)
    return str(value)


class CredentialCheckFailed(ValueError):
    """A live check did not pass.

    `detail` is text that is safe to show a person: the validator's own
    ValueError, or one of ours. It is None whenever the failure came from
    anywhere else (a transport error, an HTTP error, a parse error): that text
    can carry an upstream response body or an internal address, so it is only
    logged. `reason` ("auth" | "unreachable" | "invalid") is the class of
    failure when we know it; `internal` is for classifying and logging only.
    """

    def __init__(
        self, detail: str | None, *, reason: str | None = None, internal: str = ""
    ) -> None:
        super().__init__(detail or "The check failed.")
        self.detail = detail
        self.reason = reason
        self.internal = internal or detail or ""


#: ValueError subclasses that come from parsing/serialising a foreign
#: response, not from a validator choosing its words.
_NOT_USER_FACING = (json.JSONDecodeError, UnicodeError)


def _guard_url_fields(cred_type: Any, values: dict[str, str]) -> None:
    """Refuse a url field that points at an internal or non-http address,
    before any validator makes a request with it."""
    for field in cred_type.fields:
        if field.kind != "url":
            continue
        value = values.get(field.key)
        if not value:
            continue
        try:
            guard_outbound_url(value)
        except OutboundBlocked as exc:
            raise CredentialCheckFailed(
                None, reason="unreachable", internal=f"unreachable: {exc}"
            ) from exc


async def _load_validator(
    db: AsyncSession, *, tenant_id: uuid.UUID, credential_type: str, cred_type: Any
) -> Any:
    """The type's own `validate_entry_point`. A core-owned type resolves it
    via a plain dotted import; a Capa-owned type's entry point names a module
    path relative to the owning plugin's folder, found through
    `find_credential_type_owner` (matching the DECLARED type name)."""
    discovered = await find_credential_type_owner(
        db, tenant_id=tenant_id, credential_type=credential_type
    )
    if discovered is not None and discovered.trust in TRUSTED:
        return import_entry_point(discovered.path, cred_type.validate_entry_point)
    module_path, _, attr = cred_type.validate_entry_point.partition(":")
    return getattr(importlib.import_module(module_path), attr)


async def _run_validate(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_type: str,
    cred_type: Any,
    values: dict[str, str],
) -> None:
    """Run the type's validator. Raises CredentialCheckFailed only."""
    if not cred_type.validate_entry_point:
        return
    await asyncio.to_thread(_guard_url_fields, cred_type, values)
    try:
        validate = await _load_validator(
            db, tenant_id=tenant_id, credential_type=credential_type, cred_type=cred_type
        )
        await validate(values)
    except CredentialCheckFailed:
        raise
    except _NOT_USER_FACING as exc:
        logger.warning("credential check %s failed: %r", credential_type, exc)
        raise CredentialCheckFailed(None, internal=repr(exc)) from exc
    except ValueError as exc:
        # Raised by the validator on purpose: its words are for the person.
        raise CredentialCheckFailed(str(exc)) from exc
    except Exception as exc:
        logger.warning("credential check %s failed: %r", credential_type, exc)
        raise CredentialCheckFailed(None, internal=f"{type(exc).__name__}: {exc}") from exc


async def check_credential_values(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_type: str,
    field_values: dict[str, Any],
) -> None:
    """Validate values that are not stored ("Verbindung prüfen"). Raises
    CredentialCheckFailed; stores nothing."""
    cred_type = await get_credential_type(db, tenant_id=tenant_id, name=credential_type)
    values = collect_required_values(cred_type, field_values)
    await _run_validate(
        db, tenant_id=tenant_id, credential_type=credential_type, cred_type=cred_type, values=values
    )


def collect_required_values(cred_type: Any, field_values: dict[str, Any]) -> dict[str, str]:
    """Trimmed values with declared defaults filled in; a required field left
    empty raises CredentialCheckFailed (reason "invalid")."""
    values: dict[str, str] = {}
    for field in cred_type.fields:
        raw = field_values.get(field.key)
        value = str(raw).strip() if raw is not None else ""
        if not value and field.default:
            value = field.default
        if value:
            values[field.key] = value
        elif field.required:
            raise CredentialCheckFailed(f"{field.label} is required", reason="invalid")
    return values


async def test_credential(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_id: uuid.UUID,
    owner: OwnerFilter = "any",
) -> None:
    """Raises on failure; on success, records last_tested_at/last_test_ok."""
    cred = await get_credential(db, tenant_id=tenant_id, credential_id=credential_id, owner=owner)
    cred_type = await get_credential_type(db, tenant_id=tenant_id, name=cred.credential_type)
    if not cred_type.validate_entry_point:
        cred.last_tested_at = dt.datetime.now(tz=dt.UTC)
        cred.last_test_ok = True
        await db.flush()
        return
    values: dict[str, str] = {}
    for field in cred_type.fields:
        try:
            values[field.key] = await resolve_credential_field(
                db, tenant_id=tenant_id, credential_id=credential_id, field_key=field.key
            )
        except CredentialFieldNotSet:
            if field.required:
                raise
            # An optional field left blank (e.g. s3_api's `endpoint`, "leave
            # empty for AWS") is a complete, valid credential -- just omit it
            # from `values` so the entry point's own `values.get(key,
            # default)` convention (see capas/s3_source/credential.py's
            # `validate_s3`) applies, the same way the connector itself
            # already tolerates it not being in `config`.
            continue
    try:
        await _run_validate(
            db,
            tenant_id=tenant_id,
            credential_type=cred.credential_type,
            cred_type=cred_type,
            values=values,
        )
    except CredentialCheckFailed:
        cred.last_tested_at = dt.datetime.now(tz=dt.UTC)
        cred.last_test_ok = False
        await db.flush()
        raise
    cred.last_tested_at = dt.datetime.now(tz=dt.UTC)
    cred.last_test_ok = True
    await db.flush()
