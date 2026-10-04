"""Render an approval as a Telegram message, and read a button press back.

Everything Telegram-shaped lives here. Core hands over an `ApprovalNotice` and
gets a `ChannelDecision`; it never learns what an inline keyboard is.

Three things this file is careful about, all of them consequences of Telegram
being somebody else's system:

* **the callback payload is an identifier, never a claim.** A button carries
  only `<approval-id>:<verdict>[:<option>]`, and core still resolves the sender
  to a binding before anything is written. Telegram will happily deliver an
  update from anyone who finds the bot.
* **the approval id is not a secret, and is not treated as one.** Knowing it
  gets you nothing without a binding, which is why the payload can be plain.
* **a message is edited, not deleted, when the question closes.** The approver
  should be able to scroll back and see what they were asked and what happened;
  deleting it leaves them with a decision they cannot account for.
"""

from __future__ import annotations

import hmac
import logging
import re
from collections.abc import Mapping
from typing import Any

import httpx

from oc8.channels.notice import (
    ApprovalNotice,
    ChannelCapabilities,
    ChannelDecision,
    ChannelFreeText,
    ChannelLink,
)

# never __name__: `channel/` is a shared folder name across plugins.
logger = logging.getLogger("oc8.plugin.telegram_approvals.channel")

CHANNEL_ID = "telegram"
API = "https://api.telegram.org"

#: Telegram rejects a callback_data over 64 bytes, and silently -- the button
#: simply never fires. A uuid plus a verdict is 43; that leaves 21 for an option
#: key, which is checked rather than hoped for.
_MAX_CALLBACK = 64

#: Telegram's own cap. A long approval detail is truncated rather than dropped:
#: a shortened question an approver can act on beats a message that never
#: arrives, and the full text is one tap away in oc8.
_MAX_TEXT = 4096


def _escape(text: str) -> str:
    """Telegram's HTML parse mode. Only these three, per its documentation --
    escaping more would show entities to the reader."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _to_telegram_html(text: str) -> str:
    """The Assistant's free-text replies (say()'s payload) come back
    Markdown-flavoured -- `**bold**`, `- ` bullets -- because that is what the
    model was trained to write, not because anything asked for Telegram's
    markup. Sent raw, the reader sees literal asterisks and dashes instead of
    the emphasis they were meant to convey. Escapes HTML first (so nothing in
    the model's own text is mistaken for markup), THEN promotes the one
    construct the model reliably uses to a real `<b>` tag, and normalises a
    leading list dash to a bullet. Deliberately narrow: single-asterisk
    italics and underscores are left alone -- snake_case, multiplication, and
    stray underscores are more common accidental matches than intentional
    italic markup in a data-summary reply."""
    escaped = _escape(text)
    bolded = _BOLD.sub(r"<b>\1</b>", escaped)
    return "\n".join(re.sub(r"^[-*]\s+", "• ", line) for line in bolded.splitlines())


def render(notice: ApprovalNotice) -> str:
    """The message body. Readable on a phone, in the reader's own language --
    which means the text core supplied, untranslated and unsummarised."""
    lines = [f"<b>{_escape(notice.title)}</b>"]
    if notice.content_withheld:
        lines += [
            "",
            "Der Inhalt dieser Anfrage darf nicht über Telegram gehen.",
            "Bitte in oc8 öffnen und dort entscheiden.",
        ]
        return "\n".join(lines)[:_MAX_TEXT]
    if notice.amount_text:
        lines.append(f"<b>{_escape(notice.amount_text)}</b>")
    if notice.detail:
        lines += ["", _escape(notice.detail)]
    if notice.record_url:
        # A bare URL, deliberately unlabelled: Telegram auto-links it, and this
        # renderer's surrounding copy is historically German while capa code is
        # English-only -- a URL needs neither language. Placed before the
        # expiry line so the _MAX_TEXT truncation below can never cut a link in
        # half (a half URL is worse than none).
        lines += ["", _escape(notice.record_url)]
    if notice.expires_at is not None:
        lines += ["", f"Läuft ab: {notice.expires_at:%d.%m.%Y %H:%M}"]
    return "\n".join(lines)[:_MAX_TEXT]


def keyboard(notice: ApprovalNotice) -> dict[str, Any] | None:
    """The buttons, or None when there is nothing to press.

    A redacted notice gets NO buttons on purpose: a button labelled "Voll
    erstatten" tells you what the request is about even with the text removed,
    which is exactly what withholding it was for.
    """
    if notice.content_withheld:
        return None
    rows: list[list[dict[str, str]]] = []
    for option in notice.options:
        data = f"{notice.approval_id}:approve:{option.key}"
        if len(data.encode()) > _MAX_CALLBACK:
            # Silently over the limit is a button that never fires. Skipping it
            # is honest: the option stays available in oc8.
            logger.warning("option %r does not fit a Telegram callback", option.key)
            continue
        rows.append([{"text": option.label[:64], "callback_data": data}])
    if not rows:
        rows = [
            [
                {"text": "✅ Freigeben", "callback_data": f"{notice.approval_id}:approve"},
                {"text": "✖️ Ablehnen", "callback_data": f"{notice.approval_id}:reject"},
            ]
        ]
    else:
        rows.append([{"text": "✖️ Ablehnen", "callback_data": f"{notice.approval_id}:reject"}])
    return {"inline_keyboard": rows}


def _sender_id(payload: dict[str, Any]) -> Any:
    """Telegram's `from.id`, or None. A message without one cannot be attributed
    to an account, so it cannot be attributed to a person either."""
    who = payload.get("from")
    return who.get("id") if isinstance(who, dict) else None


def parse_callback(data: str) -> tuple[str, str, str | None] | None:
    """`<approval-id>:<verdict>[:<option>]` -> its parts, or None if it is not ours.

    Returns strings, not a uuid: the caller has to look the approval up anyway
    and parsing it here would only move the failure earlier.
    """
    parts = data.split(":")
    if len(parts) < 2 or parts[1] not in ("approve", "reject"):
        return None
    return parts[0], parts[1], parts[2] if len(parts) > 2 and parts[2] else None


def decision_from_update(update: dict[str, Any]) -> tuple[ChannelDecision, str] | None:
    """A Telegram update -> (decision, callback id), or None if it is not one.

    The callback id is returned because Telegram expects it to be acknowledged
    within seconds; an unacknowledged button spins forever on the sender's phone
    and they press it again.
    """
    query = update.get("callback_query")
    if not isinstance(query, dict):
        return None
    parsed = parse_callback(str(query.get("data") or ""))
    if parsed is None:
        return None
    approval_id, verdict, option = parsed
    sender = _sender_id(query)
    if sender is None:
        return None
    import uuid as _uuid

    try:
        approval_uuid = _uuid.UUID(approval_id)
    except ValueError:
        return None
    return (
        ChannelDecision(
            approval_id=approval_uuid,
            verdict=verdict,
            option_key=option,
            external_id=str(sender),
        ),
        str(query.get("id") or ""),
    )


def link_code_from_update(update: dict[str, Any]) -> tuple[str, str] | None:
    """A `/start <code>` (or bare code) message -> (code, sender id).

    `/start <payload>` is how Telegram's own deep links work, so an operator can
    hand out a link instead of asking somebody to copy a code into a chat.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or "").strip()
    sender = _sender_id(message)
    if not text or sender is None:
        return None
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            return None
        text = parts[1].strip()
    if not text or " " in text:
        return None
    return text, str(sender)


class TelegramChannel:
    """Contributed to core as an `ApprovalChannel`."""

    channel_id = CHANNEL_ID

    def __init__(
        self, *, token: str, max_classification: str = "public", webhook_secret: str = ""
    ) -> None:
        self._token = token
        self._max_classification = max_classification
        self._webhook_secret = webhook_secret

    async def verify_inbound(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        """Telegram's own scheme: a secret this deployment chose, echoed back in
        `X-Telegram-Bot-Api-Secret-Token` on every call.

        Fails closed when no secret is configured. An unverified webhook that
        reaches the decision path lets anyone who learns the URL approve
        anything, and the URL is not a secret -- it is in Telegram's logs, in a
        proxy's, and in whatever the operator pasted it into.
        """
        if not self._webhook_secret:
            logger.warning("telegram webhook secret is not configured; refusing the call")
            return False
        sent = headers.get("x-telegram-bot-api-secret-token") or headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        return hmac.compare_digest(sent, self._webhook_secret)

    def parse_inbound(
        self, update: Mapping[str, Any]
    ) -> ChannelDecision | ChannelLink | ChannelFreeText | None:
        pressed = decision_from_update(dict(update))
        if pressed is not None:
            decision, _callback_id = pressed
            return decision
        linked = link_code_from_update(dict(update))
        if linked is not None:
            code, sender = linked
            return ChannelLink(code=code, external_id=sender)
        message = dict(update).get("message")
        if not isinstance(message, dict):
            return None
        text = str(message.get("text") or "").strip()
        sender = _sender_id(message)
        if not text or sender is None:
            return None
        return ChannelFreeText(text=text, external_id=str(sender))

    def capabilities(self) -> ChannelCapabilities:
        # `unsolicited=True`: unlike WhatsApp, Telegram lets a bot write to
        # anyone who has ever started it, with no window -- which is what makes
        # an escalation reminder at 3 a.m. possible here and not there.
        return ChannelCapabilities(
            max_classification=self._max_classification,
            unsolicited=True,
            supports_options=True,
        )

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{API}/bot{self._token}/{method}", json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram refused {method}: {body.get('description')}")
        return body.get("result") or {}

    async def poll(self, *, offset: int) -> tuple[list[dict[str, Any]], int]:
        """Fetch whatever Telegram has queued since `offset`, for a deployment
        with no public webhook URL to receive on (`oc8.channels.poll`).

        `timeout=0`: a short, non-blocking `getUpdates` call rather than
        Telegram's own long-polling wait -- this runs inside a shared
        scheduler tick alongside other tenants' work, so it must return
        promptly and let the tick's own interval set the polling cadence,
        not hold the connection open itself.

        Returns the raw updates and the next offset to persist -- one past
        the highest `update_id` seen, or `offset` unchanged if nothing was
        new. Telegram's own contract: passing that value back as `offset`
        on the next call is what marks these updates as read.
        """
        result: Any = await self._post("getUpdates", {"offset": offset, "timeout": 0})
        updates: list[dict[str, Any]] = result if isinstance(result, list) else []
        next_offset = offset
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int) and update_id + 1 > next_offset:
                next_offset = update_id + 1
        return updates, next_offset

    async def deliver(self, notice: ApprovalNotice, *, external_id: str) -> str | None:
        payload: dict[str, Any] = {
            "chat_id": external_id,
            "text": render(notice),
            "parse_mode": "HTML",
        }
        markup = keyboard(notice)
        if markup is not None:
            payload["reply_markup"] = markup
        result = await self._post("sendMessage", payload)
        message_id = result.get("message_id")
        return str(message_id) if message_id is not None else None

    async def withdraw(
        self, notice: ApprovalNotice, *, external_id: str, handle: str | None, outcome: str
    ) -> None:
        """Edit the message so the buttons are gone and the outcome is on it.

        Without a handle there is nothing to edit -- that happens when the
        approval was announced before this account was bound. Sending a fresh
        "this is decided" message instead would be noise about a question this
        person was never asked.
        """
        if handle is None:
            return
        said = {"approved": "freigegeben", "rejected": "abgelehnt"}.get(outcome, outcome)
        await self._post(
            "editMessageText",
            {
                "chat_id": external_id,
                "message_id": handle,
                "text": f"{render(notice)}\n\n<i>Entschieden: {_escape(said)}</i>",
                "parse_mode": "HTML",
            },
        )

    async def acknowledge(self, callback_id: str, text: str) -> None:
        """Stop the button spinning on the sender's phone, with a word on what
        happened. Best-effort: the decision is already recorded, and failing here
        must not undo it."""
        try:
            await self._post(
                "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]}
            )
        except Exception:
            logger.warning("could not acknowledge a Telegram callback", exc_info=True)

    async def say(self, external_id: str, text: str) -> None:
        """A reply — used to confirm a binding, refuse one, or carry the
        Assistant's own free-text answer for a chat-originated run. HTML parse
        mode via `_to_telegram_html`, same as `deliver`/`withdraw`: the static
        strings this also carries (binding confirm/refuse, park/failure
        notices) have no markup to convert and pass through unchanged."""
        try:
            await self._post(
                "sendMessage",
                {"chat_id": external_id, "text": _to_telegram_html(text), "parse_mode": "HTML"},
            )
        except Exception:
            logger.warning("could not send a Telegram reply", exc_info=True)


def build(config: Mapping[str, str]) -> TelegramChannel:
    """One tenant's channel, from that installation's resolved config.

    `bot_token` arrives already resolved -- core reads the secret store, not the
    plugin, so a plugin cannot reach another plugin's credentials. A missing
    token raises here rather than at send time: an approval quietly not being
    delivered is the failure this whole feature exists to prevent.
    """
    token = config.get("bot_token") or ""
    if not token:
        raise ValueError("telegram_approvals needs a bot token (config secret_ref)")
    return TelegramChannel(
        token=token,
        max_classification=config.get("max_classification") or "public",
        webhook_secret=config.get("webhook_secret") or "",
    )


def register(contrib: Any) -> None:
    contrib.add_channel(CHANNEL_ID, build)
