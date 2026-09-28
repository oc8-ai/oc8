"""Models, integrations, and skills catalogs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update

from oc8 import models as m
from oc8.api.deps import CurrentPrincipal, DbSession, require_permission
from oc8.api.v1._listquery import apply_group_order, apply_search, paginate
from oc8.api.v1._serializers import integration_to_dto, model_to_dto, skill_to_dto
from oc8.authz.permissions import INTEGRATION, MANAGE, MODEL, SKILL, VIEW, perm
from oc8.config import get_settings
from oc8.credentials.service import create_credential
from oc8.modelrouter.auto_router import AUTO_MODEL, AUTO_PROVIDER, TIER_ORDER, is_auto_config
from oc8.modelrouter.discovery import DiscoveryError, discover_models
from oc8.modelrouter.keys import resolve_model_base_url, resolve_model_key
from oc8.modelrouter.registry import (
    available_provider_entries,
    provider_infos,
    resolve_provider,
)
from oc8.modelrouter.subscription_guard import (
    SubscriptionModelNotManualOnly,
    assert_credential_bind_safe,
)
from oc8.oauth.device_flow import poll_device_login, start_device_login
from oc8.oauth.errors import OAuthExchangeFailed
from oc8.oauth.tokens import (
    access_ref,
    expiry_from,
    extract_chatgpt_account_id,
    persist_tokens,
    refresh_ref,
)
from oc8.schemas.dto import (
    DeviceLoginPollDTO,
    DeviceLoginStartDTO,
    IntegrationDTO,
    ModelDiscoverResponse,
    ModelDTO,
    SkillDTO,
)
from oc8.schemas.paging import Page
from oc8.schemas.requests import DevicePollRequest, ModelConfigWrite, ModelDiscoverRequest

router = APIRouter()

# Shared row->DTO mapping used by GET/POST/PATCH so the DTO shape is defined once.
_model_to_dto = model_to_dto


async def _agent_slug_to_id(db: DbSession) -> dict[str, str]:
    agents = (await db.execute(select(m.Agent))).scalars().all()
    return {(a.presentation or {}).get("slug", str(a.id)): str(a.id) for a in agents}


@router.get(
    "/models",
    response_model=list[ModelDTO],
    dependencies=[Depends(require_permission(perm(MODEL, VIEW)))],
)
async def list_models(db: DbSession) -> list[ModelDTO]:
    models = (
        (await db.execute(select(m.ModelConfig).order_by(m.ModelConfig.created_at))).scalars().all()
    )
    agents = (await db.execute(select(m.Agent))).scalars().all()
    assigned: dict[str, list[str]] = {}
    for a in agents:
        if a.model_config_id:
            assigned.setdefault(str(a.model_config_id), []).append(str(a.id))
    return [_model_to_dto(mc, assigned.get(str(mc.id), [])) for mc in models]


@router.get("/models/providers", dependencies=[Depends(require_permission(perm(MODEL, VIEW)))])
async def list_providers(db: DbSession, _p: CurrentPrincipal) -> list[dict[str, object]]:
    """Only what THIS tenant may configure: built-ins plus its enabled plugins."""
    entries = await available_provider_entries(db, tenant_id=_p.tenant_id)
    # `available` must agree with what the completion path actually uses: a
    # tenant's own BYOK key (resolve_model_key), not just the platform env
    # key -- otherwise a tenant with a working key sees "key missing" here.
    tenant_keys: dict[str, str] = {}
    for entry in entries:
        key = await resolve_model_key(db, tenant_id=_p.tenant_id, provider=entry.canonical)
        if key:
            tenant_keys[entry.canonical] = key
    infos: list[dict[str, object]] = list(
        provider_infos(  # type: ignore[arg-type]
            get_settings(), tenant_keys=tenant_keys, entries=entries
        )
    )
    # Virtual auto-router: always offered. It has no completion key of its own;
    # concrete tiers carry credentials / BYOK.
    infos.append(
        {
            "canonical": AUTO_PROVIDER,
            "locality": "cloud",
            "available": True,
            "label": "Auto (complexity router)",
        }
    )
    return infos


def _auto_params_from_body(body: ModelConfigWrite, existing: dict | None = None) -> dict:
    """Build params for an auto ModelConfig from write fields."""
    params = dict(existing or {})
    if body.auto_tiers is not None:
        cleaned: dict[str, str] = {}
        for name, cfg_id in body.auto_tiers.items():
            key = str(name).strip().lower()
            if key not in TIER_ORDER:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"unknown auto tier {name!r}; expected one of {TIER_ORDER}",
                )
            cleaned[key] = str(cfg_id)
        if not cleaned:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "auto_tiers must map at least one of fast|balanced|strong",
            )
        params["tiers"] = cleaned
    if body.auto_shadow_only is not None:
        params["shadow_only"] = bool(body.auto_shadow_only)
    if body.auto_cascade_verify is not None:
        params["cascade_verify"] = bool(body.auto_cascade_verify)
    if body.auto_preference_router is not None:
        params["preference_router"] = bool(body.auto_preference_router)
    if body.auto_preference_examples is not None:
        params["preference_examples"] = list(body.auto_preference_examples)
    if "tiers" not in params:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "provider=auto requires auto_tiers",
        )
    return params


async def _validate_auto_tier_targets(
    db: DbSession, tenant_id: uuid.UUID, tiers: dict[str, str]
) -> None:
    """Tier targets must exist, belong to this tenant, and not be nested auto configs."""
    for name, raw_id in tiers.items():
        try:
            cfg_id = uuid.UUID(str(raw_id))
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"auto tier {name!r} is not a valid model config id",
            ) from exc
        target = await db.get(m.ModelConfig, cfg_id)
        if target is None or target.tenant_id != tenant_id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"auto tier {name!r} points at an unknown model config",
            )
        if is_auto_config(target):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"auto tier {name!r} must not point at another auto model",
            )


@router.post(
    "/models/discover",
    response_model=ModelDiscoverResponse,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def discover_provider_models(
    body: ModelDiscoverRequest, db: DbSession, _p: CurrentPrincipal
) -> ModelDiscoverResponse:
    """The live "what models can I pick from" list for one provider (§ wizard
    Step 2's "Fetch available models"). Resolves the same way completion
    requests do -- `credential_id` (a credential the wizard may have just
    created, not yet saved onto any ModelConfig) wins, else the tenant-wide
    bound credential, else the platform env key -- so a successful fetch here
    is a real signal the same key will work at completion time too."""
    entry = await resolve_provider(db, tenant_id=_p.tenant_id, name=body.provider)
    if entry is None:
        if body.provider.strip().lower() == AUTO_PROVIDER:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Auto does not expose a model list; configure concrete tier models instead.",
            )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown provider: {body.provider}"
        )
    credential_id = uuid.UUID(body.credential_id) if body.credential_id else None
    api_key = await resolve_model_key(
        db, tenant_id=_p.tenant_id, provider=entry.canonical, credential_id=credential_id
    )
    base_url = await resolve_model_base_url(
        db, tenant_id=_p.tenant_id, provider=entry.canonical, credential_id=credential_id
    )
    try:
        models = await discover_models(
            entry.canonical, settings=get_settings(), base_url=base_url, api_key=api_key
        )
    except DiscoveryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except KeyError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This provider doesn't support automatic model discovery yet.",
        ) from None
    return ModelDiscoverResponse(models=models)


async def _validated(
    body: ModelConfigWrite, db: DbSession, tenant_id: uuid.UUID
) -> tuple[str, str]:
    """This is the gate for plugin-contributed providers. The completion path
    has no db session, so entitlement is decided here, at configuration time: a
    tenant can only name a provider that is built in or comes from a plugin it
    enabled. Unknown and not-entitled deliberately give the same answer."""
    if body.provider.strip().lower() == AUTO_PROVIDER:
        if body.locality not in ("cloud", "local"):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "locality must be cloud|local"
            )
        # Fixed sentinel model name for auto rows (tiers hold the real models).
        return AUTO_PROVIDER, (body.model.strip() or AUTO_MODEL)
    entry = await resolve_provider(db, tenant_id=tenant_id, name=body.provider)
    if entry is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown provider: {body.provider}"
        )
    if body.locality not in ("cloud", "local"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "locality must be cloud|local")
    return entry.canonical, body.model


async def _apply_copilot_flag(
    db: DbSession, tenant_id: uuid.UUID, cfg: m.ModelConfig, requested: bool | None
) -> None:
    """Single-select semantics: setting this model's flag clears every other
    model's flag in the same tenant. Enforced here (not a DB constraint) so it
    holds regardless of whether the caller is the wizard or the Models page."""
    if requested:
        await db.execute(
            update(m.ModelConfig)
            .where(m.ModelConfig.tenant_id == tenant_id, m.ModelConfig.id != cfg.id)
            .values(used_by_copilot=False)
        )
        cfg.used_by_copilot = True
    elif requested is False:
        cfg.used_by_copilot = False


@router.post(
    "/models",
    response_model=ModelDTO,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def create_model(
    body: ModelConfigWrite,
    db: DbSession,
    _p: CurrentPrincipal,
) -> ModelDTO:
    canon, model = await _validated(body, db, _p.tenant_id)
    if canon == AUTO_PROVIDER:
        params = _auto_params_from_body(body)
        await _validate_auto_tier_targets(db, _p.tenant_id, params["tiers"])
        cfg = m.ModelConfig(
            tenant_id=_p.tenant_id,
            provider=canon,
            model=model,
            locality=body.locality,
            display_name=body.display_name or "Auto",
            credential_id=None,
            params=params,
        )
    else:
        cfg = m.ModelConfig(
            tenant_id=_p.tenant_id,
            provider=canon,
            model=model,
            locality=body.locality,
            display_name=body.display_name,
            credential_id=uuid.UUID(body.credential_id) if body.credential_id else None,
            params={"effort": body.effort.strip()} if body.effort and body.effort.strip() else {},
        )
    db.add(cfg)
    await db.flush()  # cfg.id must exist before the auto-activation count query below
    if canon != AUTO_PROVIDER and (body.supports_vision or body.extra):
        # jsonb: replaced whole, or SQLAlchemy never notices the mutation.
        params = dict(cfg.params or {})
        if body.supports_vision:
            params["supports_vision"] = True
        if body.extra:
            params["extra"] = body.extra
        cfg.params = params
    existing_count = (
        await db.execute(
            select(func.count())
            .select_from(m.ModelConfig)
            .where(m.ModelConfig.tenant_id == _p.tenant_id)
        )
    ).scalar_one()
    if existing_count == 1:
        # This is the tenant's only model -- it must be the Copilot's model
        # regardless of what the caller requested, even an explicit false (a
        # form checkbox left unchecked submits false, not omitted).
        await _apply_copilot_flag(db, _p.tenant_id, cfg, True)
    elif body.used_by_copilot is not None:
        await _apply_copilot_flag(db, _p.tenant_id, cfg, body.used_by_copilot)
    await db.flush()
    return _model_to_dto(cfg, [])


@router.patch(
    "/models/{model_id}",
    response_model=ModelDTO,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def update_model(
    model_id: uuid.UUID,
    body: ModelConfigWrite,
    db: DbSession,
    _p: CurrentPrincipal,
) -> ModelDTO:
    cfg = await db.get(m.ModelConfig, model_id)
    if cfg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model config not found")
    was_auto = is_auto_config(cfg)
    canon, model = await _validated(body, db, _p.tenant_id)
    cfg.provider = canon
    cfg.model = model
    if canon == AUTO_PROVIDER:
        auto_fields = {
            "auto_tiers",
            "auto_shadow_only",
            "auto_cascade_verify",
            "auto_preference_router",
            "auto_preference_examples",
        }
        if auto_fields & body.model_fields_set or not (cfg.params or {}).get("tiers"):
            params = _auto_params_from_body(body, existing=cfg.params if was_auto else None)
            await _validate_auto_tier_targets(db, _p.tenant_id, params["tiers"])
            cfg.params = params
        cfg.credential_id = None
    if "locality" in body.model_fields_set:
        cfg.locality = body.locality
    if "display_name" in body.model_fields_set:
        cfg.display_name = body.display_name
    if canon == AUTO_PROVIDER:
        # Auto rows only carry router params; skip concrete-model fields below.
        if "used_by_copilot" in body.model_fields_set:
            await _apply_copilot_flag(db, _p.tenant_id, cfg, body.used_by_copilot)
        await db.flush()
        return _model_to_dto(cfg, [])
    if was_auto:
        # Leaving Auto: drop router-only keys so they don't leak onto a concrete model.
        params = dict(cfg.params or {})
        for key in (
            "tiers",
            "shadow_only",
            "cascade_verify",
            "preference_router",
            "preference_examples",
            "preference_learned",
        ):
            params.pop(key, None)
        cfg.params = params
    if "context_window" in body.model_fields_set:
        # jsonb: replaced whole, or SQLAlchemy never notices the mutation.
        params = dict(cfg.params or {})
        if body.context_window:
            params["context_window"] = int(body.context_window)
        else:
            params.pop("context_window", None)
        cfg.params = params
    if "max_tokens" in body.model_fields_set:
        # jsonb: replaced whole, or SQLAlchemy never notices the mutation.
        params = dict(cfg.params or {})
        if body.max_tokens:
            params["max_tokens"] = int(body.max_tokens)
        else:
            params.pop("max_tokens", None)
        cfg.params = params
    if "supports_vision" in body.model_fields_set:
        # jsonb: replaced whole, or SQLAlchemy never notices the mutation.
        params = dict(cfg.params or {})
        if body.supports_vision:
            params["supports_vision"] = True
        else:
            params.pop("supports_vision", None)
        cfg.params = params
    if "effort" in body.model_fields_set:
        # jsonb: replaced whole, or SQLAlchemy never notices the mutation.
        params = dict(cfg.params or {})
        if body.effort and body.effort.strip():
            params["effort"] = body.effort.strip()
        else:
            params.pop("effort", None)
        cfg.params = params
    if "extra" in body.model_fields_set:
        # jsonb: replaced whole, or SQLAlchemy never notices the mutation.
        params = dict(cfg.params or {})
        if body.extra:
            params["extra"] = body.extra
        else:
            params.pop("extra", None)
        cfg.params = params
    if "used_by_copilot" in body.model_fields_set:
        await _apply_copilot_flag(db, _p.tenant_id, cfg, body.used_by_copilot)
    if "credential_id" in body.model_fields_set:
        new_credential_id = uuid.UUID(body.credential_id) if body.credential_id else None
        # Rebinding this ModelConfig's credential is a fifth way a personal
        # ChatGPT subscription could end up running unattended -- no agent is
        # named on this route, so `assert_manual_only_compatible` (the
        # agent-side guard) is never reached. Checked BEFORE the assignment:
        # unlike the trigger routes there is nothing that has to be flushed
        # first for the check to see it.
        try:
            await assert_credential_bind_safe(
                db, model_config_id=cfg.id, credential_id=new_credential_id
            )
        except SubscriptionModelNotManualOnly as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        cfg.credential_id = new_credential_id
    await db.flush()
    return _model_to_dto(cfg, [])


@router.post(
    "/models/{model_id}/test",
    response_model=ModelDTO,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def test_model(model_id: uuid.UUID, db: DbSession, _p: CurrentPrincipal) -> ModelDTO:
    """A real, free availability check (§ Models table "healthy" pill, which
    used to default to that string unconditionally -- ModelConfig.health was
    never written by anything). Mirrors `test_connection` in mcp.py, but
    checking a live LLM's actual completion capability costs the provider's
    own tokens on every click; this asks `discover_models()` -- the same
    free `GET {base_url}/models`-style lookup the wizard's "Fetch available
    models" button already uses -- whether `cfg.model` is still in that
    provider's list. Proves credential/base_url/model-name are all still
    valid together without spending a token."""
    cfg = await db.get(m.ModelConfig, model_id)
    if cfg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model config not found")
    now = datetime.now(tz=UTC).isoformat()
    if is_auto_config(cfg):
        # Auto is a policy layer; health is "ok" when every configured tier exists.
        from oc8.modelrouter.auto_router import AutoRouterError, tier_map

        try:
            tiers = tier_map(cfg)
        except AutoRouterError as exc:
            cfg.health = {"status": "error", "checkedAt": now, "error": str(exc)[:500]}
        else:
            missing = []
            for name, cfg_id in tiers.items():
                target = await db.get(m.ModelConfig, cfg_id)
                if target is None:
                    missing.append(name)
            if missing:
                cfg.health = {
                    "status": "error",
                    "checkedAt": now,
                    "error": f"missing tier targets: {', '.join(missing)}",
                }
            else:
                cfg.health = {"status": "healthy", "checkedAt": now}
        await db.commit()
        return _model_to_dto(cfg, [])
    api_key = await resolve_model_key(
        db, tenant_id=_p.tenant_id, provider=cfg.provider, credential_id=cfg.credential_id
    )
    base_url = (cfg.params or {}).get("base_url") or await resolve_model_base_url(
        db, tenant_id=_p.tenant_id, provider=cfg.provider, credential_id=cfg.credential_id
    )
    try:
        available = await discover_models(
            cfg.provider, settings=get_settings(), base_url=base_url, api_key=api_key
        )
    except DiscoveryError as exc:
        cfg.health = {"status": "error", "checkedAt": now, "error": str(exc)[:500]}
    except KeyError:
        cfg.health = {
            "status": "unknown",
            "checkedAt": now,
            "error": "Automatic verification isn't supported for this provider yet.",
        }
    else:
        if cfg.model in available:
            cfg.health = {"status": "healthy", "checkedAt": now}
        else:
            cfg.health = {
                "status": "error",
                "checkedAt": now,
                "error": f'"{cfg.model}" was not found in the provider\'s current model list.',
            }
    await db.commit()
    return _model_to_dto(cfg, [])


@router.delete(
    "/models/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def delete_model(
    model_id: uuid.UUID,
    db: DbSession,
    _p: CurrentPrincipal,
) -> None:
    cfg = await db.get(m.ModelConfig, model_id)
    if cfg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "model config not found")
    assigned = (
        await db.execute(
            select(func.count()).select_from(m.Agent).where(m.Agent.model_config_id == model_id)
        )
    ).scalar_one()
    if assigned:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"assigned to {assigned} agent(s); reassign before deleting",
        )
    await db.delete(cfg)


@router.get(
    "/integrations",
    response_model=list[IntegrationDTO],
    dependencies=[Depends(require_permission(perm(INTEGRATION, VIEW)))],
)
async def list_integrations(db: DbSession) -> list[IntegrationDTO]:
    slug_to_id = await _agent_slug_to_id(db)
    rows = (
        (await db.execute(select(m.Integration).order_by(m.Integration.created_at))).scalars().all()
    )
    return [integration_to_dto(i, slug_to_id) for i in rows]


@router.get(
    "/skills",
    response_model=Page[SkillDTO],
    dependencies=[Depends(require_permission(perm(SKILL, VIEW)))],
)
async def list_skills(
    db: DbSession,
    search: str | None = None,
    category: str | None = None,
    author: str | None = None,
    group_by: str | None = None,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False, alias="includeArchived"),
) -> Page[SkillDTO]:
    stmt = select(m.Skill)
    if not include_archived:
        stmt = stmt.where(m.Skill.deleted_at.is_(None))
    if category:
        stmt = stmt.where(m.Skill.category == category)
    if author:
        stmt = stmt.where(m.Skill.author == author)
    stmt = apply_search(
        stmt, model=m.Skill, columns=[m.Skill.name, m.Skill.description], search=search
    )
    stmt = apply_group_order(
        stmt,
        model=m.Skill,
        group_by=group_by,
        group_fields={"category": m.Skill.category, "author": m.Skill.author},
        default_order=m.Skill.name,
    )
    rows, total = await paginate(db, stmt, limit=limit, offset=offset)
    version_ids = {s.current_version_id for s in rows if s.current_version_id is not None}
    by_id: dict[uuid.UUID, m.SkillVersion] = {}
    if version_ids:
        versions = (
            (await db.execute(select(m.SkillVersion).where(m.SkillVersion.id.in_(version_ids))))
            .scalars()
            .all()
        )
        by_id = {v.id: v for v in versions}
    items = [
        skill_to_dto(s, by_id.get(s.current_version_id) if s.current_version_id else None)
        for s in rows
    ]
    return Page(items=items, total_count=total)


@router.post(
    "/models/chatgpt-subscription/device/start",
    response_model=DeviceLoginStartDTO,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def start_chatgpt_device_login() -> DeviceLoginStartDTO:
    """Kicks off "Sign in with your ChatGPT subscription" (device-code auth
    §2). No request body and no persistence here -- the device_auth_id this
    returns is not remembered server-side at all; see
    `poll_chatgpt_device_login`'s docstring for why.

    `start_device_login()` raises `OAuthExchangeFailed` on a 404 (device-code
    sign-in disabled for this ChatGPT account -- an explicitly documented,
    realistic case; see that function's own docstring), any other 4xx/5xx, a
    non-JSON body, or a malformed response. Mapped to 400 here rather than
    left to bubble into a bare 500, matching this codebase's one existing
    convention for `OAuthError`-family exceptions surfaced to a caller
    (`api/v1/oauth.py`'s `start_oauth`/`create_service_connection`/
    `create_google_service_connection`, all three `OAuthError` -> 400) --
    not `DiscoveryError`'s 400 in this same file, which is a different
    exception family that happens to share the status code."""
    try:
        result = await start_device_login()
    except OAuthExchangeFailed as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return DeviceLoginStartDTO(
        device_auth_id=result.device_auth_id,
        user_code=result.user_code,
        verification_uri=result.verification_uri,
        expires_in=result.expires_in,
        interval=result.interval,
    )


#: The label a ChatGPT connection gets when the id_token carried no
#: `chatgpt_account_id` claim -- the one case where the label does not
#: identify the account.
_UNKNOWN_ACCOUNT_LABEL = "ChatGPT subscription"
_CHATGPT_PROVIDER = "openai_chatgpt"
_SUBSCRIPTION_CREDENTIAL_TYPE = "openai_chatgpt_subscription"


async def create_chatgpt_subscription_credential(
    db: DbSession, *, tenant_id: uuid.UUID, oauth_connection_id: uuid.UUID, account_label: str
) -> m.Credential:
    return await create_credential(
        db,
        tenant_id=tenant_id,
        name=await _free_credential_name(db, tenant_id=tenant_id, preferred=account_label),
        credential_type=_SUBSCRIPTION_CREDENTIAL_TYPE,
        field_values={"oauth_connection_id": str(oauth_connection_id)},
    )


async def _free_credential_name(db: DbSession, *, tenant_id: uuid.UUID, preferred: str) -> str:
    """`preferred`, or a suffixed variant when that name is already taken.

    `uq_credential_tenant_name` is a hard unique constraint, and a collision
    here surfaces as a bare 500 that throws away tokens that were just minted
    -- see `poll_chatgpt_device_login`. Reconnecting a KNOWN account never
    reaches this (it reuses the account's existing credential row); this is
    only for the leftovers -- a same-named credential of some other type, or a
    connection whose credential row was deleted out from under it.
    """
    taken = (
        await db.execute(
            select(m.Credential.id)
            .where(m.Credential.tenant_id == tenant_id, m.Credential.name == preferred)
            .limit(1)
        )
    ).first()
    return preferred if taken is None else f"{preferred} ({uuid.uuid4().hex[:8]})"


async def _existing_chatgpt_connection(
    db: DbSession, *, tenant_id: uuid.UUID, account_label: str
) -> m.OAuthConnection | None:
    return (
        await db.execute(
            select(m.OAuthConnection).where(
                m.OAuthConnection.tenant_id == tenant_id,
                m.OAuthConnection.provider == _CHATGPT_PROVIDER,
                m.OAuthConnection.account_label == account_label,
            )
        )
    ).scalar_one_or_none()


async def _credential_for_connection(
    db: DbSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> m.Credential | None:
    """The subscription credential already pointing at this connection, if any
    -- so a reconnect returns the SAME credential id every ModelConfig in this
    tenant is already bound to, instead of a duplicate row nothing uses."""
    return (
        await db.execute(
            select(m.Credential)
            .where(
                m.Credential.tenant_id == tenant_id,
                m.Credential.credential_type == _SUBSCRIPTION_CREDENTIAL_TYPE,
                m.Credential.field_values["oauth_connection_id"].astext == str(connection_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post(
    "/models/chatgpt-subscription/device/poll",
    response_model=DeviceLoginPollDTO,
    dependencies=[Depends(require_permission(perm(MODEL, MANAGE)))],
)
async def poll_chatgpt_device_login(
    body: DevicePollRequest, db: DbSession, principal: CurrentPrincipal
) -> DeviceLoginPollDTO:
    """One poll attempt. Deliberately stateless between calls: OpenAI's poll
    endpoint gives no distinct "expired" signal of its own (403/404 covers
    both "not yet confirmed" and "no longer valid"), and this endpoint keeps
    no session naming when a given `device_auth_id` was started. So the
    frontend (Task 12) computes `expiresAt` client-side from `device/start`'s
    `expiresIn` and echoes it back on every call; this checks that deadline
    BEFORE ever calling OpenAI, so an already-expired attempt costs no
    network call at all."""
    if datetime.now(tz=UTC) > body.expires_at:
        return DeviceLoginPollDTO(status="expired", credential_id=None, error=None)

    result = await poll_device_login(body.device_auth_id, body.user_code)
    if result.status != "complete":
        return DeviceLoginPollDTO(status=result.status, credential_id=None, error=result.error)

    assert result.tokens is not None
    account_id = (
        extract_chatgpt_account_id(result.tokens.id_token) if result.tokens.id_token else None
    )
    account_label = account_id or _UNKNOWN_ACCOUNT_LABEL

    # Signing in AGAIN with the same account is the documented recovery path
    # -- this provider ROTATES refresh tokens and treats reuse as terminal
    # (`_refresh_device_code` flips such a connection to `needs_reauth`), so
    # "connect again" is the only remedy an operator has. Creating a second
    # row unconditionally made that remedy a 500: BOTH
    # `uq_oauth_conn_tenant_provider_account` and `uq_credential_tenant_name`
    # collide on the second login, and the freshly-minted tokens are thrown
    # away with no way out. So an existing connection for this account is
    # RE-USED and re-armed in place, keeping its id -- which is what every
    # ModelConfig's bound credential already points at.
    existing = await _existing_chatgpt_connection(
        db, tenant_id=principal.tenant_id, account_label=account_label
    )
    if existing is not None:
        conn = existing
        conn.status = "active"
        conn.grant_type = "device_code"
        conn.access_secret_ref = access_ref(conn.id)
        if result.tokens.refresh_token:
            conn.refresh_secret_ref = refresh_ref(conn.id)
        if account_id:
            conn.provider_metadata = {
                **(conn.provider_metadata or {}),
                "chatgpt_account_id": account_id,
            }
    else:
        conn = m.OAuthConnection(
            tenant_id=principal.tenant_id,
            provider=_CHATGPT_PROVIDER,
            # With no `chatgpt_account_id` in the id_token the label names no
            # account at all, so a second such login is NOT known to be the
            # same account -- it gets its own row under a disambiguated label
            # rather than colliding on (or silently merging into) the fixed
            # literal. The lookup above still tries the bare literal first, so
            # a row created before this suffix existed is still reconnectable.
            account_label=account_label
            if account_id
            else f"{_UNKNOWN_ACCOUNT_LABEL} ({uuid.uuid4().hex[:8]})",
            grant_type="device_code",
            client_source="tenant",
            status="active",
            # Placeholder -- filled in below once conn.id exists (matches the
            # authorization_code callback's own construct-then-fill pattern,
            # oauth.py's `_oauth_callback`).
            access_secret_ref="",
            provider_metadata={"chatgpt_account_id": account_id} if account_id else {},
        )
        db.add(conn)
        await db.flush()  # conn.id must exist before access_ref(conn.id)/persist_tokens below
        conn.access_secret_ref = access_ref(conn.id)
        if result.tokens.refresh_token:
            conn.refresh_secret_ref = refresh_ref(conn.id)

    await persist_tokens(
        db, tenant_id=principal.tenant_id, connection_id=conn.id, tokens=result.tokens
    )
    conn.expires_at = expiry_from(result.tokens.expires_in)
    await db.flush()

    credential = await _credential_for_connection(
        db, tenant_id=principal.tenant_id, connection_id=conn.id
    ) or await create_chatgpt_subscription_credential(
        db,
        tenant_id=principal.tenant_id,
        oauth_connection_id=conn.id,
        account_label=conn.account_label,
    )
    return DeviceLoginPollDTO(status="complete", credential_id=str(credential.id), error=None)
