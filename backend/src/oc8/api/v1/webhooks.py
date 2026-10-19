"""Generic inbound webhook ingress for kind='webhook' Triggers (§8.4 generic
webhooks).

Unlike `POST /events/{source}` (events.py -- GitHub-specific, HMAC-verified,
one Python adapter per source), a webhook-kind Trigger's own unguessable
token in the URL is its entire auth boundary. The pattern this copies is
n8n's Webhook node: one URL per trigger, any caller, any JSON body, no
per-source parsing. Built because oc8's first real target (Odoo's native
"Send Webhook Notification" automation action) sends a bare, unsigned POST
with no way to add a header or a signature at all -- so a signature-based
scheme is not just unnecessary here, it is unusable for that caller.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from oc8.api.deps import unguarded
from oc8.db.session import tenant_session
from oc8.tenants.activity import TenantInactive, tenant_may_run
from oc8.triggers.scheduler import fire_trigger, list_active_tenant_ids
from oc8.triggers.service import get_trigger_by_webhook_token

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post(
    "/webhooks/{token}",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(
            unguarded(
                "authenticated by the unguessable token in the URL itself, not by an "
                "operator role or a per-source signature -- see module docstring"
            )
        )
    ],
)
async def ingest_webhook(token: str, request: Request) -> dict[str, str]:
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        # A caller that doesn't send JSON at all (a bare form POST, a plain
        # ping) still fires the trigger -- the raw bytes ride along as
        # `_raw` rather than failing the request, since "not JSON" is not
        # "not a real event" for a source-agnostic endpoint.
        payload = {"_raw": body.decode("utf-8", errors="replace")}

    # No tenant context yet -- the token names it. Same all-tenant discovery
    # loop handle_inbound_event (triggers/handler.py) already uses for
    # kind='event' fan-out; here at most one tenant/trigger ever matches.
    # The activity gate is asked only for that one organisation, not for
    # every organisation on each unauthenticated request; an organisation
    # that may not run answers exactly like an unknown token.
    for tenant_id in await list_active_tenant_ids():
        async with tenant_session(tenant_id) as db:
            trigger = await get_trigger_by_webhook_token(db, token=token)
            if trigger is None:
                continue
            if not trigger.enabled or not await tenant_may_run(tenant_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or disabled webhook")
            try:
                await fire_trigger(db, trigger, tenant_id=tenant_id, payload=payload)
            except TenantInactive:
                # The gate changed between the check above and the intake's own.
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "unknown or disabled webhook"
                ) from None
            return {"status": "accepted"}
    raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or disabled webhook")
