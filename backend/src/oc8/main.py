"""FastAPI application factory for the oc8 control plane."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from oc8 import __version__
from oc8.config import Settings, get_settings
from oc8.db.engine import dispose_engine
from oc8.edition import EditionExtension
from oc8.edition.runtime import COMMUNITY_RUNTIME_COMPOSITION, EditionRuntimeComposition

logger = logging.getLogger(__name__)


def warn_about_unreachable_links(settings: Settings) -> None:
    """Say so, once, when every mailed link would point at localhost.

    `frontend_base_url` is what the password-reset and email-confirmation
    mails are built from (`api/v1/auth.py`). Its default is
    `http://localhost:8080`, which is right for a laptop and wrong for every
    deployment behind a real domain -- and getting it wrong fails SILENTLY in
    the worst possible way: the send succeeds, the endpoint answers "check
    your inbox", and the person receives a mail whose link goes nowhere from
    their machine. Nothing raises, nothing 500s, and the only person who could
    notice is the one who cannot log in.

    A warning and not a refusal: `env == "dev"` is exempt outright, and a
    genuinely local self-hosted instance reached at localhost is a real
    deployment, not a mistake. One line at startup is what an operator needs
    to connect "my users say the link is broken" to the setting that caused it.
    """
    if settings.is_dev:
        return
    if "localhost" not in settings.frontend_base_url:
        return
    logger.warning(
        "OC8_FRONTEND_BASE_URL is %s: every password-reset and email-confirmation "
        "link this instance mails will point at localhost, which is unreachable "
        "for anybody reading that mail elsewhere. Set it to the URL this "
        "workspace is actually opened at. (Ignore this if localhost is correct here.)",
        settings.frontend_base_url,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from oc8.observability import setup_observability, shutdown_observability
    from oc8.observability.logs import setup_logging

    # FIRST, before anything else can have something to say: uvicorn configures
    # its own loggers but leaves root without a handler, so until this runs every
    # warning from oc8.* is discarded.
    setup_logging(get_settings())
    setup_observability(get_settings())
    # ...and immediately after, because a warning emitted before setup_logging
    # would be discarded exactly like the ones that motivated that call.
    warn_about_unreachable_links(get_settings())

    # Hook registries are per-tenant now (see oc8.hooks.registry.get_hook_registry)
    # and declare their core points lazily on first access -- there is no single
    # global registry to seed at startup.
    from oc8.events.dispatcher import get_dispatcher
    from oc8.realtime.manager import ConnectionManager
    from oc8.triggers.handler import handle_inbound_event

    get_dispatcher().register("github", "*", handle_inbound_event)
    app.state.realtime_manager = ConnectionManager(get_settings().redis_url)
    try:
        yield
    finally:
        await app.state.realtime_manager.close()
        await dispose_engine()
        shutdown_observability()


def create_app(
    settings: Settings | None = None,
    edition_extensions: Sequence[EditionExtension] = (),
    runtime_composition: EditionRuntimeComposition = COMMUNITY_RUNTIME_COMPOSITION,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="oc8 control plane",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def activate_edition_runtime(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Scope optional edition ports to the complete request execution."""
        with runtime_composition.activate():
            return await call_next(request)

    if settings.otel_enabled:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    from fastapi import Depends

    from oc8.api.deps import (
        deny_agent_principals,
        deny_totp_pending_principals,
        get_principal,
    )
    from oc8.api.v1 import api_router
    from oc8.api.v1.internal_agent import router as internal_agent_router

    # The operator API refuses agent tokens as a whole. The internal agent API is
    # mounted separately for exactly that reason: it is the ONE path under
    # /api/v1 an agent token may use, and it checks the token's run scope itself.
    # Guarding at the mount rather than per endpoint is deliberate -- the hole
    # this closes existed because nothing forced a new route to remember.
    app.include_router(internal_agent_router, prefix="/api/v1", tags=["internal"])
    app.include_router(
        api_router,
        prefix="/api/v1",
        dependencies=[
            Depends(deny_agent_principals),
            Depends(deny_totp_pending_principals),
        ],
    )

    # Editions are composed by their entry point, never discovered or imported
    # from Community. Their routers are authenticated operator routes and receive
    # the same principal denials at the mount boundary that `api_router` gets
    # above -- so a `totp:challenge` token (proves a password only, no second
    # factor yet) cannot reach an Enterprise route just because
    # `require_permission` there resolves authority from the DB and never itself
    # reads `principal.scopes`. Unlike the aggregate Community router, Enterprise
    # routers cannot inherit intentionally public routes, so there is no
    # fall-through to protect here.
    for extension in edition_extensions:
        for router in extension.routers():
            app.include_router(
                router,
                prefix="/api/v1",
                dependencies=[
                    Depends(deny_agent_principals),
                    Depends(deny_totp_pending_principals),
                    Depends(get_principal),
                ],
            )

    # Service-to-service edition routers (z.B. oc8-enterprises Tenant-
    # Provisioning-API) liegen NICHT unter /api/v1 und haben hier keine
    # Principal-Dependencies -- jeder verifiziert sein eigenes statisches
    # Service-Token-Credential intern, genau wie internal_agent_router sein
    # eigenes run-scoped Agent-Token verifiziert. Kein Prefix: der deployte
    # URL-Contract (fleet's HttpTenantProvisioner) ist {base_url}/admin/tenants,
    # nicht {base_url}/api/v1/admin/tenants.
    for extension in edition_extensions:
        for router in extension.service_routers():
            app.include_router(router)

    from oc8.realtime.ws import router as realtime_router

    # No dependency here: this router is a WebSocket, which cannot carry an HTTP
    # dependency. It refuses agent tokens itself, where it already verifies one.
    app.include_router(realtime_router, prefix="/api/v1")

    # The LLM gateway an agent runtime points at (§8.7 R1). Mounted under /llm so
    # it cannot collide with the operator API and a reverse proxy can expose the
    # two separately -- an agent container needs this, and nothing else.
    from oc8.api.llm_gateway import router as llm_router

    app.include_router(llm_router, prefix="/llm", tags=["llm-gateway"])

    # The tool gateway an agent runtime uses as its one MCP server (§8.7 R2). Same
    # reasoning as /llm: an agent container needs this and nothing else, so it is
    # separable from the operator API at the proxy.
    from oc8.api.mcp_gateway import router as mcp_router

    app.include_router(mcp_router, prefix="/mcp", tags=["tool-gateway"])

    # The outward-facing MCP server: whoever holds a member's own API key
    # (Settings -> API keys) can reach the same Copilot capability surface
    # from outside oc8. Verified by its own bespoke auth dependency, never by
    # an operator session token, so it is mounted apart from /mcp for the
    # same reason /mcp is mounted apart from /api/v1.
    from oc8.api.mcp_external import router as mcp_external_router

    app.include_router(mcp_external_router, prefix="/mcp/external", tags=["external-mcp"])

    return app


app = create_app()
