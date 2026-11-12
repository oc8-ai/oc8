"""Create/list a login: a Credential paired 1:1 with a McpConnection,
tenant-global by default and optionally scoped to one department (agent tool
login selection design). Distinct from `api/v1/mcp.py`'s existing
department-scoped connection CRUD, which stays untouched for the OAuth/legacy
tool packs that still use it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1.mcp import _manifest_connection
from oc8.authz.permissions import INTEGRATION, MANAGE, VIEW, perm
from oc8.connections.credential_env import credential_env_targets, credential_mcp_env
from oc8.credentials.registry import CredentialTypeNotFound, get_credential_type
from oc8.credentials.service import CredentialNotFound, create_credential, get_credential
from oc8.schemas.dto import McpLoginDTO
from oc8.schemas.requests import CreateMcpLoginRequest
from oc8.secrets.keyprovider import SecretStoreUnavailable

router = APIRouter()


def _to_dto(c: m.McpConnection) -> McpLoginDTO:
    return McpLoginDTO(
        id=str(c.id),
        name=c.name,
        credential_id=str(c.credential_id),
        department_id=str(c.department_id) if c.department_id else None,
        connected=c.connected,
        scopes=c.scopes,
        health=c.health or {},
    )


@router.post(
    "/mcp/logins",
    response_model=McpLoginDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(INTEGRATION, MANAGE)))],
)
async def create_login(
    body: CreateMcpLoginRequest, db: DbSession, principal: CurrentPrincipal
) -> McpLoginDTO:
    try:
        cred_type = await get_credential_type(
            db, tenant_id=principal.tenant_id, name=body.credential_type
        )
    except CredentialTypeNotFound as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"unknown credential type: {exc}"
        ) from exc
    dept: m.Department | None = None
    if body.department_id is not None:
        dept = await db.get(m.Department, body.department_id)
        if dept is None or dept.deleted_at is not None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown department")
    # Only conflict with another LOGIN-backed connection (credential_id IS NOT
    # NULL) sharing the same NAME *and* the same department_id -- an existing,
    # untouched OAuth/department-scoped connection may coincidentally share
    # this name (a different, older mechanism this plan doesn't touch) and
    # must not block a login from being created. Scoping by department_id
    # (NULL-safe: two NULLs conflict, but NULL and a real department, or two
    # different departments, do not) is what lets two distinct logins share a
    # tool key -- e.g. two different Odoo logins, one per department -- while
    # still refusing a second tenant-wide login of the same name. Checked
    # before create_credential runs, so a name collision never leaves behind
    # an orphaned Credential row with no paired connection. The DB has no
    # UniqueConstraint enforcing this (unlike Credential's own
    # uq_credential_tenant_name) because McpConnection is shared with the
    # pre-existing, unconstrained OAuth flow -- this check is what a later
    # by-name lookup (e.g. agents_write.py's login-backed connection
    # resolution, Task 4) depends on to stay a bounded query instead of
    # silently accumulating unrelated logins under one name.
    department_filter = (
        m.McpConnection.department_id.is_(None)
        if body.department_id is None
        else m.McpConnection.department_id == body.department_id
    )
    existing = (
        await db.execute(
            select(m.McpConnection.id).where(
                m.McpConnection.tenant_id == principal.tenant_id,
                m.McpConnection.name == body.name,
                m.McpConnection.credential_id.is_not(None),
                department_filter,
            )
        )
    ).first()
    if existing is not None:
        scope = "for this tenant" if body.department_id is None else "for this department"
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a login named {body.name!r} already exists {scope}"
        )
    if body.credential_id is not None:
        # Reuse a credential that already exists -- e.g. one a capa's own
        # setup form created -- instead of asking the operator to retype
        # values already sitting in the credential store.
        try:
            cred = await get_credential(
                db,
                tenant_id=principal.tenant_id,
                credential_id=body.credential_id,
                owner="organisation",
            )
        except CredentialNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such credential") from exc
        if cred.credential_type != body.credential_type:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"credential {cred.name!r} is a {cred.credential_type!r}, "
                f"not a {body.credential_type!r}",
            )
    else:
        # `Credential.name` has its own tenant-wide uniqueness constraint
        # (uq_credential_tenant_name), independent of the McpConnection
        # conflict check above -- department-scoping the LOGIN does nothing
        # to relax it, so two departments both minting a brand-new "odoo"
        # credential would otherwise collide there instead. Suffixed with
        # the department's name (not id) so the Credentials list stays
        # readable, and only when department-scoped: a tenant-wide login
        # keeps the exact name it always has, since only one can ever exist.
        cred_name = f"{body.name} ({dept.name})" if dept is not None else body.name
        try:
            cred = await create_credential(
                db,
                tenant_id=principal.tenant_id,
                name=cred_name,
                credential_type=body.credential_type,
                field_values=body.field_values,
            )
        except SecretStoreUnavailable as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"a credential named {cred_name!r} already exists"
            ) from exc
        await db.flush()
    # A login is credential storage, not a spawn recipe -- the command/args/
    # focus_spec/etc. a real MCP server needs to start still only exist on
    # the manifest-materialised connection sharing this NAME (the same
    # by-name convention `_resolve_mcp_connection` and `set_department_
    # tools`'s cascade already use). Without this, a pinned login's own
    # bare `config` (env/secret_env only) reached the runtime as `cfg.get(
    # "command", "")` -> "" -> a subprocess spawn on an empty path, which
    # surfaces as `PermissionError(13, 'Permission denied')` with no hint
    # of the real cause (live bug report). `credential_id.is_(None)` picks
    # the manifest row, never another login sharing this same tenant-global
    # name.
    source_conn = (
        await db.execute(
            select(m.McpConnection)
            .where(
                m.McpConnection.tenant_id == principal.tenant_id,
                m.McpConnection.name == body.name,
                m.McpConnection.credential_id.is_(None),
            )
            .order_by(m.McpConnection.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    source_cfg = source_conn.config if source_conn and isinstance(source_conn.config, dict) else {}

    # ENV_VAR_NAME -> credential field key (ToolPackConnection.
    # credential_env_fields, e.g. odoo_mcp's {"ODOO_URL": "url", ...}) --
    # only resolvable off the manifest object itself, since materialise.py
    # never copies this field onto the row's own `config`. Inverted below to
    # field key -> list of ENV_VAR_NAMEs, the direction the loop needs.
    #
    # A list, not a single name: odoo_mcp maps BOTH `ODOO_PASSWORD` and
    # `ODOO_API_KEY` to the same `password` field (mcp-server-odoo treats
    # them as two different auth modes to try, not two names for one var --
    # see tool_pack.toml's own comment). A `{v: k for k, v in ...}` inversion
    # silently keeps only the LAST env name written for a given field and
    # drops the rest -- live-observed 2026-08-26: a login created after that
    # mapping was added got ODOO_API_KEY only, never ODOO_PASSWORD, because
    # dict inversion collapsed the two entries into one.
    credential_env_fields: dict[str, str] = {}
    if source_conn is not None:
        resolved = _manifest_connection(source_conn)
        if resolved is not None:
            credential_env_fields = resolved[0].credential_env_fields
    # Derive config["secret_env"]/["env"] from the credential's OWN stored
    # refs/values -- resolve_mcp_env's existing per-entry loop (agent/mcp_env.py)
    # already knows how to resolve a plain secret-store ref the same way
    # resolve_credential_field does (both ultimately call
    # oc8.secrets.service.resolve_secret), so no change to that resolution
    # code is needed, only to what gets written here. A field with no entry
    # in credential_env_fields (or no matching manifest connection at all)
    # falls back to its own key, so a login with no matching manifest
    # connection (credential_type registered but not yet wired to any tool
    # pack) still gets a usable Credential + connection pair.
    env, secret_env = credential_mcp_env(cred, cred_type, credential_env_fields)
    # Everything from the manifest connection's own config -- command, args,
    # _plugin_name, focus_spec, value_spec, outward_tools, _connection_key,
    # all carried over so the runtime can actually start this server -- with
    # env/secret_env rebuilt rather than copied. `env` starts from the
    # manifest's own env so NON-credential launch env (e.g. a PYTHONPATH a
    # `python -m <bridge>` server needs to be importable at all) survives: a
    # login whose env was only the credential's fields started that server
    # without it and every run died with "No module named ..." (live bug).
    # The names this credential manages (credential_env_targets: the mapped
    # env names plus each unmapped field's own key) are dropped first -- those
    # are the shared plugin's demo/default values -- and the credential's own
    # values are overlaid, so they always win. `secret_env` stays ONLY this
    # credential's refs: the manifest's placeholder secret refs never carry
    # over.
    managed = credential_env_targets(cred_type, credential_env_fields)
    manifest_env = source_cfg.get("env")
    launch_env = (
        {k: v for k, v in manifest_env.items() if k not in managed}
        if isinstance(manifest_env, dict)
        else {}
    )
    config = {k: v for k, v in source_cfg.items() if k not in ("env", "secret_env")}
    config["env"] = {**launch_env, **env}
    config["secret_env"] = secret_env
    conn = m.McpConnection(
        tenant_id=principal.tenant_id,
        department_id=body.department_id,
        credential_id=cred.id,
        name=body.name,
        server_url="",
        transport="stdio",
        scopes=body.scopes,
        config=config,
        connected=False,
    )
    db.add(conn)
    await db.flush()
    dto = _to_dto(conn)
    await db.commit()
    return dto


@router.get(
    "/mcp/logins",
    response_model=list[McpLoginDTO],
    dependencies=[Depends(require_permission(perm(INTEGRATION, VIEW)))],
)
async def list_logins(
    db: DbSession,
    principal: CurrentPrincipal,
    credential_type: str | None = Query(default=None, alias="credentialType"),
) -> list[McpLoginDTO]:
    stmt = select(m.McpConnection).where(
        m.McpConnection.tenant_id == principal.tenant_id,
        m.McpConnection.credential_id.is_not(None),
    )
    rows = (await db.execute(stmt.order_by(m.McpConnection.name))).scalars().all()
    if credential_type is not None:
        cred_ids = set(
            (
                await db.execute(
                    select(m.Credential.id).where(
                        m.Credential.tenant_id == principal.tenant_id,
                        m.Credential.credential_type == credential_type,
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = [r for r in rows if r.credential_id in cred_ids]
    return [_to_dto(r) for r in rows]
