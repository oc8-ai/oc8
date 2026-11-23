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
import logging
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.capas.loader import TRUSTED, import_entry_point
from oc8.connections.checks import reason_for_exception
from oc8.credentials.errors import CredentialCheckFailed
from oc8.credentials.registry import find_credential_type_owner, get_credential_type
from oc8.net.outbound import OutboundBlocked, guard_outbound_host, guard_outbound_url
from oc8.secrets.service import SecretNotFound, delete_secret, resolve_secret, store_secret

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
    """Refused to delete: something still references this credential.
    `forceable` False: a reference `force` does not override (a data source
    reads the login by id and would be left naming nothing)."""

    def __init__(self, references: list[str], *, forceable: bool = True) -> None:
        self.references = references
        self.forceable = forceable
        if forceable:
            message = f"credential is in use by: {', '.join(references)}"
        else:
            message = (
                f"credential is used by {', '.join(references)} and cannot be deleted, "
                "not even with force: choose another credential there first"
            )
        super().__init__(message)


class IncompleteCredential(ValueError):
    """A required field of the credential's type is empty, or would become
    empty. `missing` names the field keys, in the type's field order."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"required fields missing: {', '.join(missing)}")


class UrlWithoutScheme(ValueError):
    """An address field's value does not start with https:// or http:// (E2E
    N4: `api.opaas.ai` was saved, and the model check failed later with
    nothing saying why). `fields` names the field keys."""

    def __init__(self, fields: list[str]) -> None:
        self.fields = fields
        super().__init__(f"address needs https://: {', '.join(fields)}")


def refuse_urls_without_scheme(cred_type: Any, values: dict[str, Any]) -> None:
    """Raise `UrlWithoutScheme` (422 `credential_url_scheme`) for each `url`
    field in `values` that is filled but names no http(s) address."""
    bad = []
    for field in cred_type.fields:
        value = values.get(field.key)
        if field.kind != "url" or not isinstance(value, str) or not value.strip():
            continue
        parts = urlsplit(value.strip())
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            bad.append(field.key)
    if bad:
        raise UrlWithoutScheme(bad)


def missing_required(
    cred_type: Any, values: dict[str, Any], secret_refs: dict[str, str]
) -> list[str]:
    """The required field keys of `cred_type` without a value: empty after
    strip in `values`, and (for a secret) no stored ref in `secret_refs`."""
    missing: list[str] = []
    for field in cred_type.fields:
        if not field.required or secret_refs.get(field.key):
            continue
        raw = values.get(field.key)
        if raw is None or not str(raw).strip():
            missing.append(field.key)
    return missing


async def require_complete(db: AsyncSession, cred: m.Credential) -> None:
    """Raise `IncompleteCredential` (422 `credential_incomplete`) when the
    login leaves a required field of its type empty -- for a surface that
    chooses an existing login. A type that is gone is not checked here."""
    from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type

    try:
        cred_type = await get_credential_type(
            db, tenant_id=cred.tenant_id, name=cred.credential_type
        )
    except CredentialTypeNotFound:
        return
    missing = missing_required(cred_type, cred.field_values or {}, cred.secret_refs or {})
    if missing:
        raise IncompleteCredential(missing)


def _secret_ref(credential_id: uuid.UUID, field_key: str) -> str:
    return f"credential/{credential_id}/{field_key}"


#: The secret name a credential's own field is stored under.
secret_ref_for = _secret_ref


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
    refuse_urls_without_scheme(cred_type, field_values)
    plain_values = keep_origins(
        cred_type, {k: v for k, v in field_values.items() if k not in secret_keys}
    )
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
    given_secrets = {k: field_values.get(k) for k in secret_keys}
    missing = missing_required(cred_type, {**plain_values, **given_secrets}, {})
    if missing:
        raise IncompleteCredential(missing)
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
        # Only what this edit changes: a stored address is not re-judged.
        refuse_urls_without_scheme(cred_type, field_values)
        plain_updates = keep_origins(
            cred_type, {k: v for k, v in field_values.items() if k not in secret_keys}
        )
        merged = {**cred.field_values, **plain_updates}
        # A blank secret leaves the stored one in place, so only a new
        # non-blank value or an existing ref makes a secret present.
        new_secrets = {k: field_values.get(k) for k in secret_keys if field_values.get(k)}
        missing = missing_required(cred_type, {**merged, **new_secrets}, dict(cred.secret_refs))
        if missing:
            raise IncompleteCredential(missing)
        account_changed = merged != cred.field_values
        cred.field_values = merged
        if account_changed:
            # Any non-secret field may name the account (a host, a user, a
            # tenant): the login may now reach another one, so whatever
            # listens on it starts afresh. A new secret alone does not.
            # Deliberately broad (the safe direction: never a replay, never a
            # missed new id); the cost is that events which arrived since the
            # last poll, at most one poll interval's worth, are taken into
            # the new baseline and do not fire.
            from oc8.events.sources import LoginTarget, reset_cursors_on_login

            await reset_cursors_on_login(db, LoginTarget("credential", cred.id))
        secret_refs = dict(cred.secret_refs)
        for key in secret_keys:
            value = field_values.get(key)
            # Omitted or blank means "leave unchanged" -- a PATCH here never
            # requires re-submitting every secret just to rename a credential.
            if not value:
                continue
            # Always under the login's own name: a ref it does not own (one
            # an older setup wrote) is never overwritten.
            ref = _secret_ref(cred.id, key)
            await store_secret(db, tenant_id=tenant_id, name=ref, value=value, kind="credential")
            secret_refs[key] = ref
        cred.secret_refs = secret_refs
    await db.flush()
    return cred


def owns_ref(credential_id: uuid.UUID, ref: str) -> bool:
    """Whether `ref` is a secret name this service creates for the credential
    (`credential/{id}/{field}`). Only such a secret may be overwritten or
    deleted through the credential; any other ref belongs to someone else."""
    return ref.startswith(f"credential/{credential_id}/")


async def delete_credential(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    credential_id: uuid.UUID,
    owner: OwnerFilter = "any",
    force: bool = False,
) -> None:
    """Delete the credential and the secrets only it refers to. Refused with
    `CredentialInUse` while something references it, unless `force`: then an
    agent's fixed choice of it is left naming nothing (the runtime refuses
    rather than picking another login). A data source naming it is never
    overridden, not even with `force` (`CredentialInUse.forceable` False)."""
    cred = await get_credential(db, tenant_id=tenant_id, credential_id=credential_id, owner=owner)
    sources = await data_sources_using(db, tenant_id=tenant_id, credential_id=credential_id)
    if sources:
        raise CredentialInUse([f"data source {name!r}" for name in sources], forceable=False)
    references = await _find_references(db, tenant_id=tenant_id, credential_id=credential_id)
    if references and not force:
        raise CredentialInUse(references)
    if references:
        await db.execute(
            update(m.AgentToolAccess)
            .where(
                m.AgentToolAccess.tenant_id == tenant_id,
                m.AgentToolAccess.credential_id == credential_id,
            )
            .values(credential_id=None)
        )
    # A trigger polling this login stops; its target goes with the row
    # (ON DELETE SET NULL) and nothing would poll it again.
    from oc8.events.sources import LoginTarget, disable_triggers_on_login

    await disable_triggers_on_login(db, tenant_id, LoginTarget("credential", credential_id))
    refs = [ref for ref in dict(cred.secret_refs or {}).values() if owns_ref(cred.id, ref)]
    await db.delete(cred)
    await db.flush()
    await delete_unshared_secrets(db, tenant_id=tenant_id, refs=refs)


async def delete_unshared_secrets(db: AsyncSession, *, tenant_id: uuid.UUID, refs: Any) -> None:
    """Delete the secrets behind `refs` that no remaining credential of the
    tenant still refers to; a secret already gone is fine. Call after the
    referring credential row is gone, with only the refs it owned
    (`owns_ref`)."""
    wanted = set(refs)
    if not wanted:
        return
    still_used: set[str] = set()
    for row_refs in (
        await db.execute(
            select(m.Credential.secret_refs).where(m.Credential.tenant_id == tenant_id)
        )
    ).scalars():
        still_used.update((row_refs or {}).values())
    for ref in sorted(wanted - still_used):
        try:
            await delete_secret(db, tenant_id=tenant_id, ref=ref)
        except SecretNotFound:
            continue


async def data_sources_using(
    db: AsyncSession, *, tenant_id: uuid.UUID, credential_id: uuid.UUID
) -> list[str]:
    """The names of the live data sources whose config names the credential."""
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
    wanted = str(credential_id)
    return [s.name for s in sources if (s.config or {}).get("credential") == wanted]


async def _find_references(
    db: AsyncSession, *, tenant_id: uuid.UUID, credential_id: uuid.UUID
) -> list[str]:
    """Named references to a credential, checked before allowing deletion.

    Task 12 adds the first check here: a DataSource whose config names this
    credential (the unified credentials framework's `s3_source` migration,
    `capas/s3_source/connector/connector.py`). Also counted: an agent's
    fixed choice (`AgentToolAccess.credential_id`), a login connection
    (`McpConnection.credential_id`) and the departments it is enabled for.

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
    references: list[str] = [
        f"data source {name!r}"
        for name in await data_sources_using(db, tenant_id=tenant_id, credential_id=credential_id)
    ]
    agents = await db.scalar(
        select(func.count())
        .select_from(m.AgentToolAccess)
        .where(
            m.AgentToolAccess.tenant_id == tenant_id,
            m.AgentToolAccess.credential_id == credential_id,
        )
    )
    if agents:
        references.append(f"{agents} agent(s)")
    connections = (
        await db.execute(
            select(m.McpConnection.name).where(
                m.McpConnection.tenant_id == tenant_id,
                m.McpConnection.credential_id == credential_id,
            )
        )
    ).scalars()
    references.extend(f"connection {name!r}" for name in connections)
    departments = await db.scalar(
        select(func.count())
        .select_from(m.CredentialDepartment)
        .where(
            m.CredentialDepartment.tenant_id == tenant_id,
            m.CredentialDepartment.credential_id == credential_id,
        )
    )
    if departments:
        references.append(f"{departments} department(s)")
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


def _origin(url: str) -> str:
    """`scheme://host[:port]` of `url`; a value that is no absolute URL is
    left as it is for the URL check to refuse."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


def keep_origins(cred_type: Any, values: dict[str, Any]) -> dict[str, Any]:
    """`values` with each `origin_only` field cut down to its origin."""
    fields = {f.key for f in cred_type.fields if f.kind == "url" and f.origin_only}
    return {
        key: _origin(value.strip()) if key in fields and isinstance(value, str) else value
        for key, value in values.items()
    }


def _guard_url_fields(cred_type: Any, values: dict[str, str]) -> None:
    """Refuse an address field that points at an internal or non-http target,
    before any validator makes a connection with it: `url` fields as URLs, a
    field named `host` (database, SMTP) as a bare host."""
    for field in cred_type.fields:
        value = values.get(field.key)
        if not value:
            continue
        try:
            if field.kind == "url":
                guard_outbound_url(value)
            elif field.key == "host":
                guard_outbound_host(value)
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
    except Exception as exc:
        # Anything the validator did not word itself (a plain ValueError
        # included) can carry upstream text or an internal address: log it,
        # show nothing but the class of failure.
        logger.warning("credential check %s failed: %r", credential_type, exc)
        raise CredentialCheckFailed(
            None, reason=reason_for_exception(exc), internal=f"{type(exc).__name__}: {exc}"
        ) from exc


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
    return keep_origins(cred_type, values)


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
