"""Render an approval as a WhatsApp message, and read a button reply back.

The second channel exists to prove the seam: core did not change to gain it.
Everything that differs from Telegram lives here, and the differences are real
rather than cosmetic.

**The 24-hour window.** WhatsApp permits free-form messages only within 24 hours
of the person last writing to the business. Outside it, nothing but a
pre-approved template may be sent — and a template's text is fixed at approval
time by Meta, so it can say that a decision is waiting and never what it is.
This channel therefore declares `unsolicited = False` and sends the pointer
form when it cannot know the window is open. That is not a workaround: an
approval raised at 3 a.m. genuinely cannot carry its content here.

**Three buttons, twenty characters.** Interactive replies are capped at three
buttons of twenty characters. Options beyond that would silently vanish, so they
are dropped deliberately and the plain approve/reject pair always survives.

**Signed webhooks.** Meta signs the raw body with the app secret
(`X-Hub-Signature-256`). It is checked against the bytes as received — parsing
first and re-serialising would compare a signature against something Meta never
sent.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from oc8.channels.notice import (
    ApprovalNotice,
    ChannelCapabilities,
    ChannelDecision,
    ChannelLink,
)

# never __name__: `channel/` is a shared folder name across plugins.
logger = logging.getLogger("oc8.plugin.whatsapp_approvals.channel")

CHANNEL_ID = "whatsapp"
API = "https://graph.facebook.com/v21.0"

#: Meta's interactive reply limits. Both are silent: a longer title is rejected
#: for the whole message, extra buttons simply do not appear.
_MAX_BUTTONS = 3
_MAX_BUTTON_TEXT = 20
_MAX_BODY = 1024


def render(notice: ApprovalNotice) -> str:
    """The message body, plain text — WhatsApp has no HTML, and its markdown is
    a formatting convention rather than a parser, so nothing needs escaping."""
    lines = [notice.title]
    if notice.content_withheld:
        lines += [
            "",
            "Der Inhalt dieser Anfrage darf nicht über WhatsApp gehen.",
            "Bitte in oc8 öffnen und dort entscheiden.",
        ]
        return "\n".join(lines)[:_MAX_BODY]
    if notice.amount_text:
        lines.append(notice.amount_text)
    if notice.detail:
        lines += ["", notice.detail]
    if notice.record_url:
        # Bare URL, same reasoning as the Telegram renderer: WhatsApp has no
        # markup and auto-links a plain URL.
        lines += ["", notice.record_url]
    return "\n".join(lines)[:_MAX_BODY]


def buttons(notice: ApprovalNotice) -> list[dict[str, Any]]:
    """Reply buttons, at most three, or none for a withheld notice.

    A withheld notice gets no buttons for the same reason it gets no detail: a
    button reading "Voll erstatten" describes the request perfectly well.
    """
    if notice.content_withheld:
        return []
    out: list[dict[str, Any]] = []
    for option in notice.options:
        if len(out) >= _MAX_BUTTONS - 1:
            # Room is kept for "Ablehnen": saying no must never require leaving
            # the chat, whatever else had to be dropped.
            logger.info("option %r does not fit WhatsApp's three buttons", option.key)
            continue
        out.append(
            {
                "type": "reply",
                "reply": {
                    "id": f"{notice.approval_id}:approve:{option.key}",
                    "title": option.label[:_MAX_BUTTON_TEXT],
                },
            }
        )
    if not out:
        out.append(
            {
                "type": "reply",
                "reply": {"id": f"{notice.approval_id}:approve", "title": "Freigeben"},
            }
        )
    out.append(
        {"type": "reply", "reply": {"id": f"{notice.approval_id}:reject", "title": "Ablehnen"}}
    )
    return out


def _entries(update: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every message inside a Cloud API webhook, whatever it is nested under.

    Meta wraps messages in entry -> changes -> value -> messages, and delivers
    status receipts through the same shape. Walking it defensively is cheaper
    than trusting a structure somebody else versions.
    """
    found: list[dict[str, Any]] = []
    for entry in update.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if isinstance(message, dict):
                    found.append(message)
    return found


def parse_reply(update: Mapping[str, Any]) -> ChannelDecision | ChannelLink | None:
    """A button reply -> a decision; a plain text -> a possible binding code."""
    for message in _entries(update):
        sender = message.get("from")
        if not sender:
            continue
        interactive = message.get("interactive")
        if isinstance(interactive, dict):
            reply = interactive.get("button_reply") or {}
            raw = str(reply.get("id") or "") if isinstance(reply, dict) else ""
            parts = raw.split(":")
            if len(parts) >= 2 and parts[1] in ("approve", "reject"):
                import uuid as _uuid

                try:
                    approval_id = _uuid.UUID(parts[0])
                except ValueError:
                    continue
                return ChannelDecision(
                    approval_id=approval_id,
                    verdict=parts[1],
                    option_key=parts[2] if len(parts) > 2 and parts[2] else None,
                    external_id=str(sender),
                )
        text = message.get("text")
        if isinstance(text, dict):
            body = str(text.get("body") or "").strip()
            if body and " " not in body:
                return ChannelLink(code=body, external_id=str(sender))
    return None


def verify_signature(*, headers: Mapping[str, str], body: bytes, app_secret: str) -> bool:
    """Meta's `X-Hub-Signature-256`, over the RAW body.

    Over the bytes as received, never over a re-serialised parse: a signature
    compared against something Meta did not send fails for everyone or, worse,
    passes for the wrong thing.
    """
    if not app_secret:
        logger.warning("whatsapp app secret is not configured; refusing the call")
        return False
    sent = headers.get("x-hub-signature-256") or headers.get("X-Hub-Signature-256") or ""
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sent, expected)


class WhatsAppChannel:
    """Contributed to core as an `ApprovalChannel`."""

    channel_id = CHANNEL_ID

    def __init__(
        self,
        *,
        token: str,
        phone_number_id: str,
        app_secret: str = "",
        max_classification: str = "public",
        template_name: str = "oc8_approval_waiting",
        template_language: str = "de",
    ) -> None:
        self._token = token
        self._phone_number_id = phone_number_id
        self._app_secret = app_secret
        self._max_classification = max_classification
        self._template_name = template_name
        self._template_language = template_language

    def capabilities(self) -> ChannelCapabilities:
        # `unsolicited=False` is the honest statement of WhatsApp's 24-hour
        # window: this channel cannot promise to reach somebody with content
        # when it has not heard from them today.
        return ChannelCapabilities(
            max_classification=self._max_classification,
            unsolicited=False,
            supports_options=True,
        )

    async def verify_inbound(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        return verify_signature(headers=headers, body=body, app_secret=self._app_secret)

    def parse_inbound(self, update: Mapping[str, Any]) -> ChannelDecision | ChannelLink | None:
        return parse_reply(update)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{API}/{self._phone_number_id}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def deliver(self, notice: ApprovalNotice, *, external_id: str) -> str | None:
        rows = buttons(notice)
        if not rows:
            # No buttons means nothing to interact with, so a plain text message
            # is the right shape -- and it is also what a withheld notice is.
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "to": external_id,
                "type": "text",
                "text": {"body": render(notice)},
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": external_id,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": render(notice)},
                    "action": {"buttons": rows},
                },
            }
        result = await self._post(payload)
        messages = result.get("messages") or []
        first = messages[0] if messages and isinstance(messages[0], dict) else {}
        handle = first.get("id")
        return str(handle) if handle else None

    async def notify_outside_window(self, *, external_id: str) -> str | None:
        """The only thing that may be sent when the 24-hour window is shut.

        A template whose text Meta approved in advance, so it says a decision is
        waiting and nothing about which. Kept separate from `deliver` because the
        two are not interchangeable: sending this when the window is open would
        withhold content for no reason, and sending `deliver` when it is shut
        would simply be rejected.
        """
        result = await self._post(
            {
                "messaging_product": "whatsapp",
                "to": external_id,
                "type": "template",
                "template": {
                    "name": self._template_name,
                    "language": {"code": self._template_language},
                },
            }
        )
        messages = result.get("messages") or []
        first = messages[0] if messages and isinstance(messages[0], dict) else {}
        handle = first.get("id")
        return str(handle) if handle else None

    async def withdraw(
        self, notice: ApprovalNotice, *, external_id: str, handle: str | None, outcome: str
    ) -> None:
        """WhatsApp cannot edit a sent message, so the close-out is a short
        follow-up rather than a rewrite.

        Sent only when we know this account was told in the first place --
        otherwise it is an answer to a question this person never saw.
        """
        if handle is None:
            return
        said = {"approved": "freigegeben", "rejected": "abgelehnt"}.get(outcome, outcome)
        try:
            await self._post(
                {
                    "messaging_product": "whatsapp",
                    "to": external_id,
                    "type": "text",
                    "text": {"body": f"{notice.title}\n\nEntschieden: {said}."},
                }
            )
        except Exception:
            logger.warning("could not close out a WhatsApp approval", exc_info=True)

    async def say(self, external_id: str, text: str) -> None:
        try:
            await self._post(
                {
                    "messaging_product": "whatsapp",
                    "to": external_id,
                    "type": "text",
                    "text": {"body": text},
                }
            )
        except Exception:
            logger.warning("could not send a WhatsApp reply", exc_info=True)


def build(config: Mapping[str, str]) -> WhatsAppChannel:
    """One tenant's channel from its resolved config. Raises rather than
    returning something that cannot send: an approval quietly not delivered is
    the failure this feature exists to prevent."""
    token = config.get("bot_token") or ""
    if not token:
        raise ValueError("whatsapp_approvals needs an access token (config secret_ref)")
    phone_number_id = config.get("phone_number_id") or ""
    if not phone_number_id:
        raise ValueError("whatsapp_approvals needs a phone_number_id")
    return WhatsAppChannel(
        token=token,
        phone_number_id=phone_number_id,
        app_secret=config.get("app_secret") or "",
        max_classification=config.get("max_classification") or "public",
        template_name=config.get("template_name") or "oc8_approval_waiting",
        template_language=config.get("template_language") or "de",
    )


def register(contrib: Any) -> None:
    contrib.add_channel(CHANNEL_ID, build)
