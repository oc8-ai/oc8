"""Verify a Bot Framework Activity, and (later tasks) render one as an
Adaptive Card and post one back through the Connector API.

Everything Teams-shaped lives here, same as telegram_approvals'/
whatsapp_approvals' own channel.py. Bot Framework's inbound auth is a bearer
JWT signed by a key published at a well-known JWKS endpoint -- there is no
SDK dependency here, only pyjwt + cryptography, both already used elsewhere
in this codebase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as _uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

from oc8.channels.notice import (
    ApprovalNotice,
    ChannelCapabilities,
    ChannelDecision,
    ChannelFreeText,
    ChannelLink,
)

# Module logger, never __name__: `channel/` is a shared folder name across
# plugins.
logger = logging.getLogger("oc8.plugin.teams_approvals.channel")

CHANNEL_ID = "teams"

_JWKS_URL = "https://login.botframework.com/v1/.well-known/keys"
_JWKS_ISSUER = "https://api.botframework.com"
#: How long a fetched JWKS is trusted before a routine refresh. Independent
#: of the unknown-kid forced refresh below, which fires immediately
#: regardless of this TTL.
_JWKS_CACHE_TTL_SECONDS = 3600
#: A floor between two FORCED refreshes. `jwt.get_unverified_header` verifies
#: nothing, so any unauthenticated caller can hand us a syntactically valid
#: header naming a `kid` that does not exist and drive one outbound HTTPS GET
#: to login.botframework.com per request, straight past the TTL above. Real
#: key rotation is a once-in-weeks event, so waiting half a minute costs a
#: legitimate caller nothing and costs an abusive one everything.
_JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS = 30

_jwks_cache: dict[str, Any] = {}
_jwks_cache_at: float = 0.0
_jwks_forced_refresh_at: float = 0.0
#: Held across the fetch so a burst of concurrent misses collapses into one
#: outbound request instead of stampeding the endpoint.
_jwks_lock = asyncio.Lock()


async def _fetch_jwks() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(_JWKS_URL)
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


async def _get_jwks(*, force_refresh: bool = False) -> dict[str, Any]:
    global _jwks_cache, _jwks_cache_at, _jwks_forced_refresh_at
    async with _jwks_lock:
        now = time.monotonic()
        if not force_refresh and _jwks_cache and (now - _jwks_cache_at) < _JWKS_CACHE_TTL_SECONDS:
            return _jwks_cache
        if (
            force_refresh
            and _jwks_forced_refresh_at
            and (now - _jwks_forced_refresh_at) < _JWKS_FORCED_REFRESH_MIN_INTERVAL_SECONDS
        ):
            # Whatever we already have. It may still not hold the requested
            # kid -- which is fine: the caller's answer to a miss is to fail
            # closed, and that is the right answer for a made-up kid too.
            return _jwks_cache
        _jwks_cache = await _fetch_jwks()
        _jwks_cache_at = now
        if force_refresh:
            _jwks_forced_refresh_at = now
        return _jwks_cache


#: The only hosts the bot's own access token may ever be sent to. `serviceUrl`
#: arrives in an UNSIGNED request body (the inbound JWT covers no part of it),
#: so without this an attacker who got one activity past `verify_inbound`
#: could name their own host and be handed a live AAD token for it.
_ALLOWED_SERVICE_URL_SUFFIXES = (".botframework.com", ".trafficmanager.net")


def _is_allowed_service_url(url: str) -> bool:
    """HTTPS, on one of Microsoft's own Connector hosts. Anything else is not
    Bot Framework, whatever the Activity claims."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host.endswith(suffix) for suffix in _ALLOWED_SERVICE_URL_SUFFIXES)


async def _verify_activity_jwt(token: str, *, app_id: str) -> dict[str, Any] | None:
    """The token's claims when it verifies, None when it does not.

    Fails closed on anything unexpected -- a malformed token, an unknown key
    id, a wrong audience or issuer, an expired signature, an unreachable JWKS
    endpoint -- because an unauthenticated webhook that reaches the decision
    path is an open door to approving anything, same discipline as
    Telegram's/WhatsApp's own verify_inbound. The CLAIMS come back rather than
    a bare bool because the caller has one more check to make that only they
    can: Bot Framework's auth contract binds a token to a `serviceUrl`, and
    only the caller holds the request body to compare it against.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    kid = header.get("kid")
    if not kid:
        return None
    try:
        jwks = await _get_jwks()
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key_data is None:
            # The key may have rotated since our cache was built -- refresh
            # once and retry before giving up.
            jwks = await _get_jwks(force_refresh=True)
            key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    except (httpx.HTTPError, ValueError):
        # login.botframework.com timed out, answered 5xx, or answered with
        # something that is not JSON. `ApprovalChannel.verify_inbound`
        # promises a bool, and the webhook route does not catch anything, so
        # letting this out turned a transient Microsoft outage into a 500 with
        # a traceback instead of the documented "not verified".
        logger.warning("could not read Bot Framework's JWKS; failing closed", exc_info=True)
        return None
    if key_data is None:
        return None
    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
        claims: dict[str, Any] = jwt.decode(
            token,
            key=public_key,  # type: ignore[arg-type]
            algorithms=["RS256"],
            audience=app_id,
            issuer=_JWKS_ISSUER,
        )
    except jwt.PyJWTError:
        return None
    return claims


def render_body(notice: ApprovalNotice) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "text": notice.title,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        }
    ]
    if notice.content_withheld:
        blocks.append(
            {
                "type": "TextBlock",
                "text": (
                    "Der Inhalt dieser Anfrage darf nicht über Teams gehen. "
                    "Bitte in oc8 öffnen und dort entscheiden."
                ),
                "wrap": True,
            }
        )
        return blocks
    if notice.amount_text:
        blocks.append(
            {"type": "TextBlock", "text": notice.amount_text, "weight": "Bolder", "wrap": True}
        )
    if notice.detail:
        blocks.append({"type": "TextBlock", "text": notice.detail, "wrap": True})
    if notice.expires_at is not None:
        blocks.append(
            {
                "type": "TextBlock",
                "text": f"Läuft ab: {notice.expires_at:%d.%m.%Y %H:%M}",
                "wrap": True,
                "isSubtle": True,
            }
        )
    return blocks


def render_actions(notice: ApprovalNotice) -> list[dict[str, Any]]:
    """A redacted notice gets NO buttons on purpose: a button labelled "Voll
    erstatten" tells you what the request is about even with the text
    removed, which is exactly what withholding it was for."""
    if notice.content_withheld:
        return []
    actions: list[dict[str, Any]] = []
    if notice.record_url:
        # Teams needs a title for an OpenUrl action, so this one string is
        # English (capa code is English-only; see the plan's Global
        # Constraints). A withheld notice returns above, before this.
        actions.append({"type": "Action.OpenUrl", "title": "Open record", "url": notice.record_url})
    for option in notice.options:
        actions.append(
            {
                "type": "Action.Submit",
                "title": option.label,
                "data": {"payload": f"{notice.approval_id}:approve:{option.key}"},
            }
        )
    if not actions:
        actions.append(
            {
                "type": "Action.Submit",
                "title": "✅ Freigeben",
                "data": {"payload": f"{notice.approval_id}:approve"},
            }
        )
    actions.append(
        {
            "type": "Action.Submit",
            "title": "✖️ Ablehnen",
            "data": {"payload": f"{notice.approval_id}:reject"},
        }
    )
    return actions


def render_card(notice: ApprovalNotice) -> dict[str, Any]:
    card: dict[str, Any] = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": render_body(notice),
    }
    actions = render_actions(notice)
    if actions:
        card["actions"] = actions
    return card


def render_outcome_card(notice: ApprovalNotice, *, outcome: str) -> dict[str, Any]:
    said = {"approved": "freigegeben", "rejected": "abgelehnt"}.get(outcome, outcome)
    body = render_body(notice)
    body.append(
        {"type": "TextBlock", "text": f"Entschieden: {said}", "wrap": True, "isSubtle": True}
    )
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }


def parse_payload(data: str) -> tuple[str, str, str | None] | None:
    """`<approval-id>:<verdict>[:<option>]` -> its parts, or None if it is
    not ours. Same encoding, and the same local-parser-per-plugin shape, as
    Telegram's own `parse_callback`."""
    parts = data.split(":")
    if len(parts) < 2 or parts[1] not in ("approve", "reject"):
        return None
    return parts[0], parts[1], parts[2] if len(parts) > 2 and parts[2] else None


def _conversation_reference(activity: Mapping[str, Any]) -> dict[str, Any]:
    """A flat, deterministic subset of the inbound Activity's own fields --
    everything `deliver`/`withdraw`/`say` (Task 11) need to reconstruct a
    Connector API call later, when there is no inbound Activity to read
    them from again. Flat and ID-only (not the raw `conversation`/`from`/
    `recipient` sub-objects) so the JSON is stable across activities that
    otherwise vary in which extra keys Teams includes.

    IDs only, and deliberately no display names: this dict IS the binding key
    (`_external_id_for` serialises it, and channels/binding.py matches on
    exact string equality), so a `botName`/`userName` in here meant that
    someone changing their Teams display name -- or the bot being renamed in
    Azure -- silently orphaned every binding for them. `_activity_base` reads
    both back with `.get(...) or ""`, which already handled their absence."""
    conversation = activity.get("conversation") or {}
    bot = activity.get("recipient") or {}
    user = activity.get("from") or {}
    return {
        "serviceUrl": activity.get("serviceUrl"),
        "channelId": activity.get("channelId"),
        "conversationId": conversation.get("id"),
        "botId": bot.get("id"),
        "userId": user.get("id"),
    }


def _external_id_for(activity: Mapping[str, Any]) -> str:
    return json.dumps(_conversation_reference(activity), sort_keys=True)


def _sender_id(activity: Mapping[str, Any]) -> str | None:
    sender = activity.get("from")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    return str(sender_id) if sender_id else None


def parse_activity(
    activity: Mapping[str, Any],
) -> ChannelDecision | ChannelLink | ChannelFreeText | None:
    if activity.get("type") != "message":
        return None
    sender_id = _sender_id(activity)
    if sender_id is None:
        return None
    value = activity.get("value")
    if isinstance(value, dict):
        parsed = parse_payload(str(value.get("payload") or ""))
        if parsed is not None:
            approval_id, verdict, option = parsed
            try:
                approval_uuid = _uuid.UUID(approval_id)
            except ValueError:
                return None
            return ChannelDecision(
                approval_id=approval_uuid,
                verdict=verdict,
                option_key=option,
                external_id=_external_id_for(activity),
            )
    text = str(activity.get("text") or "").strip()
    if not text:
        return None
    if " " not in text:
        return ChannelLink(code=text, external_id=_external_id_for(activity))
    return ChannelFreeText(text=text, external_id=_external_id_for(activity))


class TeamsChannel:
    """Contributed to core as an `ApprovalChannel`."""

    channel_id = CHANNEL_ID

    def __init__(
        self,
        *,
        app_id: str,
        app_password: str,
        tenant_id: str = "",
        max_classification: str = "public",
    ) -> None:
        self._app_id = app_id
        self._app_password = app_password
        self._tenant = tenant_id
        self._max_classification = max_classification
        self._token: str = ""
        self._token_expires_at: float = 0.0

    def capabilities(self) -> ChannelCapabilities:
        # `unsolicited=True`: like Telegram and unlike WhatsApp, a Bot
        # Framework bot may proactively message anyone it holds a
        # conversationReference for, with no 24-hour window.
        return ChannelCapabilities(
            max_classification=self._max_classification,
            unsolicited=True,
            supports_options=True,
        )

    async def verify_inbound(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        auth = headers.get("authorization") or headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            logger.warning("rejected a Teams webhook call with no bearer token")
            return False
        token = auth[len("Bearer ") :]
        # The JWT signs none of the body, so `serviceUrl` -- the host this
        # channel will later hand its own AAD token to -- is caller-controlled
        # until it has been matched against the token's own claim. Microsoft's
        # auth contract requires exactly this comparison, and without it a
        # replayed-but-valid token could redirect every outbound call.
        try:
            activity = json.loads(body)
        except ValueError:
            logger.warning("rejected a Teams webhook call whose body is not JSON")
            return False
        body_service_url = activity.get("serviceUrl") if isinstance(activity, dict) else None
        if not body_service_url:
            logger.warning("rejected a Teams webhook call with no serviceUrl in its body")
            return False
        claims = await _verify_activity_jwt(token, app_id=self._app_id)
        if claims is None:
            return False
        if claims.get("serviceUrl") != body_service_url:
            logger.warning(
                "rejected a Teams webhook call whose serviceUrl claim does not match its body"
            )
            return False
        return True

    def parse_inbound(
        self, update: Mapping[str, Any]
    ) -> ChannelDecision | ChannelLink | ChannelFreeText | None:
        return parse_activity(update)

    async def _access_token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        body = await mint_access_token(
            app_id=self._app_id, app_password=self._app_password, tenant=self._tenant
        )
        token = body.get("access_token")
        if not token:
            raise RuntimeError("Bot Framework token endpoint returned no access_token")
        self._token = str(token)
        expires_in = int(body.get("expires_in") or 0)
        self._token_expires_at = now + max(expires_in - _TOKEN_REFRESH_MARGIN_SECONDS, 0)
        return self._token

    async def _post(
        self, service_url: str, conversation_id: str, activity: dict[str, Any]
    ) -> dict[str, Any]:
        if not _is_allowed_service_url(service_url):
            raise ValueError(f"refusing to send a Teams activity to {service_url!r}")
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities",
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()
        result: dict[str, Any] = response.json() if response.content else {}
        return result

    async def _put(
        self, service_url: str, conversation_id: str, activity_id: str, activity: dict[str, Any]
    ) -> None:
        if not _is_allowed_service_url(service_url):
            raise ValueError(f"refusing to send a Teams activity to {service_url!r}")
        token = await self._access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.put(
                f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities/{activity_id}",
                json=activity,
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()

    async def deliver(self, notice: ApprovalNotice, *, external_id: str) -> str | None:
        conv_ref = json.loads(external_id)
        activity = _activity_base(conv_ref)
        activity["attachments"] = [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": render_card(notice),
            }
        ]
        result = await self._post(conv_ref["serviceUrl"], conv_ref["conversationId"], activity)
        activity_id = result.get("id")
        return str(activity_id) if activity_id else None

    async def withdraw(
        self, notice: ApprovalNotice, *, external_id: str, handle: str | None, outcome: str
    ) -> None:
        """Edit the card in place so the buttons are gone and the outcome is
        on it, mirroring Telegram's editMessageText. Falls back to a plain
        text send if the edit fails (e.g. the activity is too old to edit);
        never raises -- the decision is already recorded, and failing here
        must not undo it."""
        conv_ref = json.loads(external_id)
        if handle is not None:
            activity = _activity_base(conv_ref)
            activity["attachments"] = [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": render_outcome_card(notice, outcome=outcome),
                }
            ]
            try:
                await self._put(
                    conv_ref["serviceUrl"], conv_ref["conversationId"], handle, activity
                )
                return
            except Exception:
                logger.warning(
                    "could not edit a Teams card in place, falling back to text", exc_info=True
                )
        said = {"approved": "freigegeben", "rejected": "abgelehnt"}.get(outcome, outcome)
        activity = _activity_base(conv_ref)
        activity["text"] = f"{notice.title}\n\nEntschieden: {said}."
        try:
            await self._post(conv_ref["serviceUrl"], conv_ref["conversationId"], activity)
        except Exception:
            logger.warning("could not close out a Teams approval", exc_info=True)

    async def say(self, external_id: str, text: str) -> None:
        conv_ref = json.loads(external_id)
        activity = _activity_base(conv_ref)
        activity["text"] = text
        try:
            await self._post(conv_ref["serviceUrl"], conv_ref["conversationId"], activity)
        except Exception:
            logger.warning("could not send a Teams reply", exc_info=True)


_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"
#: Bot Framework's own multi-tenant placeholder, used when no single-tenant
#: registration was configured.
_DEFAULT_TENANT = "botframework.com"
#: Refresh a bit before the token actually expires rather than racing a
#: request against the exact expiry second.
_TOKEN_REFRESH_MARGIN_SECONDS = 60


async def mint_access_token(*, app_id: str, app_password: str, tenant: str) -> dict[str, Any]:
    """POST a client_credentials token request, returns the raw JSON body.
    Raises `httpx.HTTPStatusError` on a non-2xx response -- Microsoft's own
    signal that this app id/password pair is not a valid registration.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            _TOKEN_URL_TEMPLATE.format(tenant=tenant or _DEFAULT_TENANT),
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_password,
                "scope": _BOT_FRAMEWORK_SCOPE,
            },
        )
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def _activity_base(conv_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "from": {"id": conv_ref["botId"], "name": conv_ref.get("botName") or ""},
        "recipient": {"id": conv_ref["userId"], "name": conv_ref.get("userName") or ""},
        "conversation": {"id": conv_ref["conversationId"]},
        "channelId": conv_ref["channelId"],
    }


def build(config: Mapping[str, str]) -> TeamsChannel:
    """One tenant's channel, from that installation's resolved config.
    `bot_token` arrives already resolved to the app password -- core reads
    the secret store, not the plugin.
    """
    app_password = config.get("bot_token") or ""
    if not app_password:
        raise ValueError("teams_approvals needs a bot token (config secret_ref)")
    app_id = config.get("app_id") or ""
    if not app_id:
        raise ValueError("teams_approvals needs an app_id")
    return TeamsChannel(
        app_id=app_id,
        app_password=app_password,
        tenant_id=config.get("tenant_id") or "",
        max_classification=config.get("max_classification") or "public",
    )


def register(contrib: Any) -> None:
    contrib.add_channel(CHANNEL_ID, build)
