"""The `mail.received` event: core polls a Gmail mailbox through this poller
and starts the agent for each new mail (`oc8.events.sources`).

Runs in the core process, with the login the agent is fixed to for this
capa's tools: the organisation's Google connection. Core hands over a fresh
access token (`login_fields["access_token"]`): for a service account one it
minted by domain-wide delegation for the trigger's mailbox
(`event_config["mailbox"]`, by default the setup's `default_mailbox`; the
setting is `acts_as` in plugin.toml) with the setup's `delegated_scope`; for
an account signed in for the organisation, that account's own token. The
poller reads the mailbox's history of added messages on one label.

The mailbox is the trigger's setting, which core checked against the setup's
`delegated_mailboxes` on save and checks again before every poll. The poller
checks it once more by the rule the tools use (`mcp_bridge/mailbox_policy.py`,
loaded by path under a name of its own, `_load_policy`): a person's own
sign-in reads that account only, and a service account only a mailbox in the
list core passes (`allowed_mailbox`; an empty list allows none).

Only one fixed host is ever called, the Gmail API, through core's provider
client (`oc8.oauth.http`). The mailbox and the label are checked before they
go into a path or a query.

The cursor is `{mailbox, label, history_id, seen}`. A baseline takes the
mailbox's current history id (`users.getProfile`) and fires nothing. A poll
lists the history after it (`users.history.list`, `messageAdded` records on
the label), oldest first. A mail is new when it still carries the label, is
not a draft, and is not among `seen`, the ids of the last 5,000 mails
handled. The cursor moves to the last history record fully handled, so a
poll that stops early goes on there next time; when Gmail no longer has the
history (HTTP 404, about a week), or the mailbox's history id is below the
cursor's (a restored mailbox), a new baseline is taken and the gap fires
nothing.

What a poll hands over follows the `email` capa's rules: one event per mail
with the Gmail message id as its delivery id (never the Message-ID, which
the sender chooses); loop protection for the mailbox's own address and its
sent mail, `Auto-Submitted` other than "no", `Precedence: bulk/junk/list`
and delivery reports; a mail that cannot be read becomes an event from its
headers alone, or none, and the cursor moves past it; at most
`MAX_EVENTS_PER_TICK` mails a poll, and no new mail is started after
`POLL_BUDGET_SECONDS`. Rendering runs in a thread. Errors name the HTTP
status, never a token or the text of Google's answer.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import importlib.util
import logging
import re
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from urllib.parse import quote

import httpx

from oc8.events.sources import MAX_EVENTS_PER_TICK, PolledEvent, PollResult
from oc8.oauth.http import get_client

logger = logging.getLogger(__name__)

EVENT_TYPE: Final = "mail.received"

GMAIL: Final = "https://gmail.googleapis.com/gmail/v1"

#: One HTTP request, at most. With the budget below a poll ends well inside
#: core's 60 s: the last mail started at 40 s takes two requests at most.
REQUEST_SECONDS = 8.0
#: No new mail is started after this; the cursor stops at the last one done.
POLL_BUDGET_SECONDS = 40.0
MAX_TEXT_CHARS: Final = 20_000
#: The most base64 of one text part ever decoded: far more than the text
#: shown, far less than a part can carry.
MAX_BODY_DATA: Final = 400_000
MAX_SEEN: Final = 5_000
PAGE_SIZE: Final = 100
DEFAULT_LABEL: Final = "INBOX"
#: MIME nesting walked at most.
_MAX_PARTS: Final = 500

_ADDRESS = re.compile(
    r"[^@\s<>()\[\],;:\"/\\?#%]+@[^@\s<>()\[\],;:\"/\\?#%]+\.[^@\s<>()\[\],;:\"/\\?#%]+"
)
_LABEL = re.compile(r"[A-Za-z0-9_-]{1,100}")
_HISTORY_ID = re.compile(r"[0-9]{1,20}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")
_REPORT_SENDERS: Final = ("mailer-daemon", "postmaster")
_METADATA_HEADERS: Final = (
    "From",
    "To",
    "Cc",
    "Subject",
    "Date",
    "Message-ID",
    "Auto-Submitted",
    "Precedence",
    "Content-Type",
)


class MailboxError(RuntimeError):
    """A poll that failed, in words safe to show on the trigger."""


class GmailError(MailboxError):
    def __init__(self, status: int) -> None:
        hint = {
            401: " -- check the domain-wide delegation for the Gmail scope",
            403: " -- check the domain-wide delegation for the Gmail scope",
            404: " -- check the mailbox and the label",
        }.get(status, "")
        super().__init__(f"Gmail answered HTTP {status}{hint}.")
        self.status = status


# --- settings --------------------------------------------------------------------------------


def _mailbox(value: Any) -> str:
    mailbox = value if isinstance(value, str) else ""
    if not mailbox:
        raise MailboxError(
            "No mailbox is set for this trigger: name one, or set a default mailbox in "
            "the Google Workspace setup."
        )
    if not _ADDRESS.fullmatch(mailbox):
        raise MailboxError("The mailbox must be an email address.")
    return mailbox


def _label(value: Any) -> str:
    label = value if isinstance(value, str) and value else DEFAULT_LABEL
    if not _LABEL.fullmatch(label):
        raise MailboxError("The label must be a Gmail label ID such as INBOX or Label_12.")
    return label


# --- the login -------------------------------------------------------------------------------

_POLICY: Final = "_oc8_google_workspace_mailbox_policy"


def _load_policy() -> ModuleType:
    """`mcp_bridge/mailbox_policy.py` under a name of its own: importing it as
    `mcp_bridge.mailbox_policy` would leave the generic name `mcp_bridge`
    cached in the core process, where another capa's bridge package of the
    same name could be handed ours (see `oc8.capas.loader`)."""
    loaded = sys.modules.get(_POLICY)
    if loaded is not None:
        return loaded
    path = Path(__file__).resolve().parents[1] / "mcp_bridge" / "mailbox_policy.py"
    spec = importlib.util.spec_from_file_location(_POLICY, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_POLICY] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_POLICY, None)
        raise
    return module


def _authorized(login: Mapping[str, str], mailbox: str) -> str:
    """The mailbox, if this login may act as it (`mailbox_policy`)."""
    policy = _load_policy()
    personal = (
        str(login.get("account_label") or "").strip() or None
        if login.get("grant_type") == "authorization_code"
        else None
    )
    try:
        return str(
            policy.authorized_mailbox(
                mailbox,
                delegated=policy.mailbox_list(str(login.get("allowed_mailbox") or "")),
                personal=personal,
            )
        )
    except policy.MailboxRefused as exc:
        raise MailboxError(f"The mailbox setting is refused: {exc}.") from None


def _token(login: Mapping[str, str]) -> str:
    token = login.get("access_token")
    if not isinstance(token, str) or not token:
        raise MailboxError("The login gave no access token: connect it again.")
    return token


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    return await asyncio.wait_for(client.request(method, url, **kwargs), timeout=REQUEST_SECONDS)


# --- Gmail -----------------------------------------------------------------------------------


class _Gmail:
    def __init__(self, client: httpx.AsyncClient, token: str, mailbox: str, label: str) -> None:
        self.client = client
        self.token = token
        self.mailbox = mailbox
        self.label = label
        self.base = f"{GMAIL}/users/{quote(mailbox, safe='@')}"

    async def get(self, path: str, params: Any = None) -> dict[str, Any]:
        try:
            resp = await _send(
                self.client,
                "GET",
                f"{self.base}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise MailboxError(f"Gmail could not be reached ({type(exc).__name__}).") from None
        if resp.status_code >= 400:
            raise GmailError(resp.status_code)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise MailboxError("Gmail answered with something that is not a JSON object.")
        return body

    async def history_id(self) -> str:
        found = (await self.get("/profile")).get("historyId")
        found = str(found) if isinstance(found, (str, int)) else ""
        if not _HISTORY_ID.fullmatch(found):
            raise MailboxError("Gmail answered without the mailbox's history id.")
        return found

    async def history(self, start: str, page_token: str | None) -> dict[str, Any]:
        params = {
            "startHistoryId": start,
            "historyTypes": "messageAdded",
            "labelId": self.label,
            "maxResults": str(PAGE_SIZE),
        }
        if page_token:
            params["pageToken"] = page_token
        return await self.get("/history", params)

    async def message(self, mid: str, *, full: bool) -> dict[str, Any]:
        path = f"/messages/{quote(mid, safe='')}"
        if full:
            return await self.get(path, {"format": "full"})
        return await self.get(
            path, [("format", "metadata"), *(("metadataHeaders", h) for h in _METADATA_HEADERS)]
        )


# --- the poll ----------------------------------------------------------------------------------


def _out_of_time(deadline: float, done: int, starved: bool) -> bool:
    """Whether to stop before the next mail. A poll out of time before its
    first mail hands the cursor back marked "starved"; the next one then
    takes one mail whatever the time, so the trigger always moves on."""
    if time.monotonic() < deadline:
        return False
    return not (done == 0 and starved)


def _cursor(gmail: _Gmail, history_id: str, seen: Sequence[str], **extra: Any) -> dict[str, Any]:
    return {
        "mailbox": gmail.mailbox,
        "label": gmail.label,
        "history_id": history_id,
        "seen": list(seen)[-MAX_SEEN:],
        **extra,
    }


def _usable(cursor: Mapping[str, Any] | None, mailbox: str, label: str) -> dict[str, Any] | None:
    if not isinstance(cursor, Mapping):
        return None
    history_id = cursor.get("history_id")
    seen = cursor.get("seen")
    if (
        cursor.get("mailbox") != mailbox
        or cursor.get("label") != label
        or not isinstance(history_id, str)
        or not _HISTORY_ID.fullmatch(history_id)
        or not isinstance(seen, list)
        or not all(isinstance(s, str) for s in seen)
    ):
        return None
    return {"history_id": history_id, "seen": seen}


class _HistoryBehind(Exception):
    """The mailbox's history id is below the cursor's: a restored or moved
    mailbox. The cursor would wait for ids that may never come."""


def _later(a: str, b: Any) -> str:
    """The later of two history ids (`b` may be anything Gmail sent)."""
    other = str(b) if isinstance(b, (str, int)) else ""
    if _HISTORY_ID.fullmatch(other) and int(other) > int(a):
        return other
    return a


def _added(record: Any) -> list[dict[str, Any]]:
    added = record.get("messagesAdded") if isinstance(record, dict) else None
    out: list[dict[str, Any]] = []
    for item in added if isinstance(added, list) else []:
        msg = item.get("message") if isinstance(item, dict) else None
        if isinstance(msg, dict):
            out.append(msg)
    return out


async def _round(
    gmail: _Gmail, state: Mapping[str, Any], deadline: float, starved: bool, limit: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    start: str = state["history_id"]
    seen: list[str] = list(state["seen"])
    known = set(seen)
    events: list[dict[str, Any]] = []
    done = 0
    last_done = start
    page_token: str | None = None
    while True:
        page = await gmail.history(start, page_token)
        current = page.get("historyId")
        current = str(current) if isinstance(current, (str, int)) else ""
        if _HISTORY_ID.fullmatch(current) and int(current) < int(start):
            raise _HistoryBehind
        records = page.get("history")
        for record in records if isinstance(records, list) else []:
            for msg in _added(record):
                mid = msg.get("id")
                labels = msg.get("labelIds")
                labels = labels if isinstance(labels, list) else []
                if not isinstance(mid, str) or not mid or mid in known:
                    continue
                if gmail.label not in labels or "DRAFT" in labels:
                    continue
                if done >= limit or _out_of_time(deadline, done, starved):
                    # Stop inside this record: the next poll lists it again
                    # and skips what is seen.
                    extra = {"starved": True} if done == 0 else {}
                    return _cursor(gmail, last_done, seen, **extra), events
                event = await _event(gmail, mid)
                if event is not None:
                    events.append(event)
                seen.append(mid)
                known.add(mid)
                done += 1
            last_done = _later(last_done, record.get("id") if isinstance(record, dict) else None)
        token = page.get("nextPageToken")
        if not isinstance(token, str) or not token:
            return _cursor(gmail, _later(last_done, page.get("historyId")), seen), events
        if time.monotonic() >= deadline:
            return _cursor(gmail, last_done, seen), events
        page_token = token


async def poll(
    login_fields: dict[str, str], event_config: dict[str, Any], cursor: dict[str, Any] | None
) -> PollResult:
    mailbox = _authorized(login_fields, _mailbox(event_config.get("mailbox")))
    label = _label(event_config.get("label"))
    token = _token(login_fields)
    deadline = time.monotonic() + POLL_BUDGET_SECONDS
    starved = isinstance(cursor, Mapping) and cursor.get("starved") is True
    async with get_client() as client:
        gmail = _Gmail(client, token, mailbox, label)
        state = _usable(cursor, mailbox, label)
        if state is None:
            return PollResult(next_cursor=_cursor(gmail, await gmail.history_id(), []))
        try:
            next_cursor, events = await _round(gmail, state, deadline, starved, MAX_EVENTS_PER_TICK)
        except (GmailError, _HistoryBehind) as exc:
            if isinstance(exc, GmailError) and exc.status != 404:
                raise
            # Gmail keeps history for about a week, and a restored mailbox
            # starts lower: start over from now, firing nothing for the gap.
            logger.warning("mail.received: the history of a mailbox expired; new baseline")
            fresh = _cursor(
                gmail,
                await gmail.history_id(),
                [],
                rebaselined_at=datetime.now(tz=UTC).isoformat(),
            )
            return PollResult(next_cursor=fresh)
    return PollResult(next_cursor=next_cursor, events=[PolledEvent(**e) for e in events])


def register(contrib: Any) -> None:
    # No credential type: the trigger listens on the organisation's Google
    # connection the agent is fixed to (`oc8.events.sources.LoginTarget`).
    contrib.add_poller(EVENT_TYPE, poll)


# --- one mail ----------------------------------------------------------------------------------


async def _event(gmail: _Gmail, mid: str) -> dict[str, Any] | None:
    """The event for one new mail, from its headers alone when it cannot be
    read whole, or None (skipped by loop protection, gone meanwhile, or
    unreadable). A rejected login fails the poll instead: that is no
    mail's fault."""
    try:
        msg: dict[str, Any] | None = await gmail.message(mid, full=True)
    except GmailError as exc:
        if exc.status in (401, 403):
            raise
        if exc.status == 404:
            return None  # deleted before it was read
        logger.warning(
            "mail %s could not be fetched (HTTP %s); trying its headers", mid, exc.status
        )
        msg = None
    except MailboxError:
        logger.warning("mail %s could not be fetched; trying its headers", mid)
        msg = None
    if msg is not None:
        try:
            return await asyncio.to_thread(mail_event, mid, msg, gmail.mailbox, gmail.label)
        except Exception as exc:
            # The class name and the id only: the error text may quote the mail.
            logger.warning(
                "mail %s could not be read (%s); handing it on by its headers",
                mid,
                type(exc).__name__,
            )
    try:
        head = await gmail.message(mid, full=False)
    except GmailError as exc:
        if exc.status in (401, 403):
            raise
        logger.warning("mail %s skipped: not even its headers could be read", mid)
        return None
    try:
        return await asyncio.to_thread(header_event, mid, head, gmail.mailbox, gmail.label)
    except Exception:
        logger.warning("mail %s skipped: its headers could not be read", mid)
        return None


def _one_line(value: str, limit: int = 500) -> str:
    """A value as one line: control characters (line breaks above all)
    become spaces, so nothing can add a line to what the model reads."""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _payload_part(msg: Mapping[str, Any]) -> dict[str, Any]:
    part = msg.get("payload")
    return part if isinstance(part, dict) else {}


def _part_headers(part: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    raw = part.get("headers")
    for header in raw if isinstance(raw, list) else []:
        if isinstance(header, dict):
            name, value = header.get("name"), header.get("value")
            if isinstance(name, str) and isinstance(value, str):
                out.setdefault(name.strip().lower(), []).append(value)
    return out


def _first(headers: Mapping[str, list[str]], name: str) -> str:
    values = headers.get(name, [])
    return _one_line(values[0], 1_000) if values else ""


def skip_reason(msg: Mapping[str, Any], mailbox: str) -> str | None:
    """Why the poller must not start the agent for this mail (loop
    protection), or None."""
    labels = msg.get("labelIds")
    labels = labels if isinstance(labels, list) else []
    if "SENT" in labels:
        return "own address"
    if "DRAFT" in labels:
        return "draft"
    root = _payload_part(msg)
    headers = _part_headers(root)
    sender = parseaddr(_first(headers, "from"))[1].strip().lower()
    if sender and sender == mailbox.lower():
        return "own address"
    if sender.split("@", 1)[0] in _REPORT_SENDERS:
        return "delivery report"
    for auto in headers.get("auto-submitted", []):
        if auto.split(";", 1)[0].strip().lower() != "no":
            return "auto-submitted"
    if any(p.strip().lower() in ("bulk", "junk", "list") for p in headers.get("precedence", [])):
        return "bulk"
    content_types = [_text(root.get("mimeType")), *headers.get("content-type", [])]
    if any(c.strip().lower().startswith("multipart/report") for c in content_types):
        return "delivery report"
    return None


def _parts(root: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every leaf part, in order, walked without recursion and bounded."""
    leaves: list[dict[str, Any]] = []
    stack: list[Any] = [root]
    walked = 0
    while stack and walked < _MAX_PARTS:
        part = stack.pop()
        walked += 1
        if not isinstance(part, dict):
            continue
        children = part.get("parts")
        if isinstance(children, list) and children:
            stack.extend(reversed(children))
        else:
            leaves.append(part)
    return leaves


def _charset(part: Mapping[str, Any]) -> str:
    for value in _part_headers(part).get("content-type", []):
        found = re.search(r"charset=\"?([A-Za-z0-9._-]{1,40})", value)
        if found:
            try:
                return codecs.lookup(found.group(1)).name
            except LookupError:
                return "utf-8"
    return "utf-8"


def _decode(part: Mapping[str, Any]) -> str:
    body = part.get("body")
    data = _text(body.get("data")) if isinstance(body, dict) else ""
    if not data:
        return ""
    chunk = data[:MAX_BODY_DATA]
    try:
        raw = base64.urlsafe_b64decode(chunk + "=" * (-len(chunk) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("a body part is not base64") from None
    return raw.decode(_charset(part), errors="replace")


def body_text(msg: Mapping[str, Any]) -> str:
    leaves = [p for p in _parts(_payload_part(msg)) if not _text(p.get("filename"))]
    for kind in ("text/plain", "text/html"):
        for part in leaves:
            if _text(part.get("mimeType")).lower() == kind:
                text = _decode(part)
                if kind == "text/html":
                    text = html_to_text(text)
                return text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return ""


def _attachments(msg: Mapping[str, Any]) -> list[dict[str, Any]]:
    listed: list[dict[str, Any]] = []
    for part in _parts(_payload_part(msg)):
        name = _text(part.get("filename"))
        if not name:
            continue
        body = part.get("body")
        size = body.get("size") if isinstance(body, dict) else 0
        listed.append(
            {
                "name": name,
                "type": _text(part.get("mimeType")),
                "size": size if isinstance(size, int) and not isinstance(size, bool) else 0,
            }
        )
    return listed


def _payload(
    mid: str, msg: Mapping[str, Any], mailbox: str, label: str, listed: list[dict[str, Any]]
) -> dict[str, Any]:
    headers = _part_headers(_payload_part(msg))
    return {
        "id": mid,
        "thread_id": _text(msg.get("threadId")),
        "mailbox": mailbox,
        "label": label,
        "message_id": _first(headers, "message-id"),
        "from": _first(headers, "from"),
        "to": _first(headers, "to"),
        "cc": _first(headers, "cc"),
        "date": _first(headers, "date"),
        "subject": _first(headers, "subject"),
        "attachments": listed,
    }


def mail_event(mid: str, msg: Mapping[str, Any], mailbox: str, label: str) -> dict[str, Any] | None:
    """`{delivery_id, payload, summary}` for a new mail, or None when loop
    protection skips it."""
    if skip_reason(msg, mailbox):
        return None
    text = body_text(msg)
    payload = _payload(mid, msg, mailbox, label, _attachments(msg))
    return {"delivery_id": mid, "payload": payload, "summary": render_summary(payload, text)}


def header_event(
    mid: str, msg: Mapping[str, Any], mailbox: str, label: str
) -> dict[str, Any] | None:
    if skip_reason(msg, mailbox):
        return None
    payload = _payload(mid, msg, mailbox, label, [])
    note = (
        "(This mail could not be read; only its headers are shown. Open it with gmail_get "
        "to try again.)"
    )
    return {"delivery_id": mid, "payload": payload, "summary": _render(payload, None, [note])}


class _Html(HTMLParser):
    _BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "hr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Html()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value, unit = (n / 1024, "KB") if n < 1024 * 1024 else (n / (1024 * 1024), "MB")
    return f"{value:.1f}".removesuffix(".0") + f" {unit}"


_SUMMARY_ATTACHMENTS: Final = 50


def render_summary(payload: Mapping[str, Any], text: str, notes: Sequence[str] = ()) -> str:
    """The plain text the agent's run receives for a new mail (see `_render`)."""
    return _render(payload, text, notes)


def _render(payload: Mapping[str, Any], text: str | None, notes: Sequence[str]) -> str:
    """The message id and the mailbox the tools take, the headers, the
    attachments, our notes, and last the mail's text under "Text:", every
    line of it quoted with "> ". Every value above the text is flattened to
    one line and capped, so nothing a mail says can pass for one of those
    lines."""

    def flat(value: Any, limit: int = 500) -> str:
        return _one_line(str(value if value is not None else ""), limit)

    lines = [
        f"Mail id: {flat(payload['id'], 200)} (pass it as messageId to gmail_get or "
        "gmail_modify_labels, and as threadId to gmail_reply)",
        f"Mailbox: {flat(payload['mailbox'])} (pass it as mailbox)",
        f"Label: {flat(payload['label'])}",
    ]
    for label, key in (
        ("From", "from"),
        ("To", "to"),
        ("Cc", "cc"),
        ("Date", "date"),
        ("Subject", "subject"),
        ("Message-ID", "message_id"),
    ):
        if payload[key]:
            lines.append(f"{label}: {flat(payload[key], 1_000)}")
    listed = payload["attachments"]
    if listed:
        lines += ["", "Attachments (open the mail with gmail_get to read them):"]
        for item in listed[:_SUMMARY_ATTACHMENTS]:
            lines.append(
                f"- {flat(item['name'], 200)} ({flat(item['type'], 255)}, "
                f"{_size(int(item['size']))})"
            )
        if len(listed) > _SUMMARY_ATTACHMENTS:
            lines.append(f"- ... and {len(listed) - _SUMMARY_ATTACHMENTS} more")
    notes = list(notes)
    if text is not None and len(text) > MAX_TEXT_CHARS:
        notes.append(
            f"(Text truncated: showing the first {MAX_TEXT_CHARS:,} of {len(text):,} characters.)"
        )
        text = text[:MAX_TEXT_CHARS]
    if notes:
        lines += ["", *notes]
    if text is not None:
        quoted = [f"> {line}" if line else ">" for line in text.splitlines()]
        lines += ["", "Text:", *quoted] if quoted else ["", "Text: (none)"]
    return "\n".join(lines)
