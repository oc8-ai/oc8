"""The `mail.received` event: core polls a Microsoft 365 mailbox through
this poller and starts the agent for each new mail (`oc8.events.sources`).

Runs in the core process, with the login the agent is fixed to for this
capa's tools: the organisation's Microsoft connection. Core hands over a
fresh Graph token (`login_fields["access_token"]`, from `oc8.oauth.tokens`
as for the tools): app-only for an app registration (client_credentials),
whose app needs the `Mail.Read` (or `Mail.ReadWrite`) application
permission; or the account's own for an account signed in for the
organisation, which then always reads that account's own mailbox (the
Mailbox setting is ignored for it). The poller reads
the trigger's mailbox (`event_config["mailbox"]`, by default the setup's
`default_user`) through the Graph delta query on one folder.

Only one fixed host is ever called, Graph, through core's provider client
(`oc8.oauth.http`); a link Graph hands back to continue from is followed only
when it points at Graph itself, so the token never leaves for another host.
The mailbox and the folder are checked and quoted before they go into a path.

The cursor is `{mailbox, folder, link, since, seen}`: `link` is where the
next delta request goes (Graph's `@odata.nextLink` or `@odata.deltaLink`),
`since` the time the baseline was taken (less a margin for clock skew), and
`seen` the (id, received) pairs of the last 5,000 messages already handled
(`since` moves to the oldest receive time still kept). A delta round
lists new and changed messages alike: a message counts as new when it is not
removed, not a draft, received at or after `since`, and not seen before. The
baseline drains the first delta round without events, over several polls if
it has to (`baseline: true`), and marks what arrived meanwhile as seen. An
expired delta link (HTTP 410, or 400 with a sync-state code) takes a new
baseline.

What a poll hands over follows the `email` capa's rules: one event per mail
with the Graph message id as its delivery id (never the Message-ID, which
the sender chooses); loop protection for the mailbox's own address,
`Auto-Submitted` other than "no", `Precedence: bulk/junk/list` and delivery
reports; a mail that cannot be read becomes an event from its headers alone,
or none, and the cursor moves past it; at most `MAX_EVENTS_PER_TICK` mails a
poll, and no new mail is started after `POLL_BUDGET_SECONDS`. Rendering runs
in a thread. Errors name the HTTP status and Graph's error code, never a
token or the text of Graph's answer.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx

from oc8.events.sources import MAX_EVENTS_PER_TICK, PolledEvent, PollResult
from oc8.oauth.http import get_client

logger = logging.getLogger(__name__)

EVENT_TYPE: Final = "mail.received"

GRAPH: Final = "https://graph.microsoft.com/v1.0"
_GRAPH_HOST: Final = "graph.microsoft.com"

#: One HTTP request, at most. With the budget below a poll ends well inside
#: core's 60 s: the last mail started at 40 s takes two requests at most.
REQUEST_SECONDS = 8.0
#: No new mail is started after this; the cursor stops at the last one done.
POLL_BUDGET_SECONDS = 40.0
MAX_TEXT_CHARS: Final = 20_000
#: Message ids remembered as handled (changed later: still not new).
MAX_SEEN: Final = 5_000
#: A handled mail in the cursor: its id and when it was received.
Seen = tuple[str, str]
#: Delta page size asked for.
PAGE_SIZE: Final = 100
#: How far before the baseline a message may say it was received and still
#: be checked against what the baseline saw: clock skew between here and
#: Exchange.
_SINCE_MARGIN: Final = timedelta(minutes=5)
DEFAULT_FOLDER: Final = "inbox"

_DELTA_SELECT: Final = "id,receivedDateTime,isDraft"
_HEADER_SELECT: Final = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,internetMessageId,"
    "internetMessageHeaders,conversationId,isDraft"
)
_FULL_SELECT: Final = f"{_HEADER_SELECT},body,hasAttachments"
_ATTACHMENTS: Final = "attachments($select=id,name,contentType,size)"

_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_ADDRESS = re.compile(
    r"[^@\s<>()\[\],;:\"/\\?#%]+@[^@\s<>()\[\],;:\"/\\?#%]+\.[^@\s<>()\[\],;:\"/\\?#%]+"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")
_SYNC_STATE_CODES: Final = frozenset({"syncstatenotfound", "syncstateinvalid", "resyncrequired"})
_CODE = re.compile(r"[A-Za-z0-9_.]{1,64}")
#: Senders of delivery reports, whatever their content type says: Exchange
#: writes its own reports without internet headers.
_REPORT_SENDERS: Final = (
    "mailer-daemon",
    "postmaster",
    "microsoftexchange329e71ec88ae4615bbc36ab6ce41109e",
)


class MailboxError(RuntimeError):
    """A poll that failed, in words safe to show on the trigger."""


class GraphError(MailboxError):
    def __init__(self, status: int, code: str) -> None:
        hint = {
            401: " -- check the app registration's Mail.Read permission and admin consent",
            403: " -- check the app registration's Mail.Read permission and admin consent",
            404: " -- check the mailbox and the folder",
        }.get(status, "")
        named = f" ({code})" if code else ""
        super().__init__(f"Microsoft Graph answered HTTP {status}{named}{hint}.")
        self.status = status
        self.code = code

    @property
    def sync_state_lost(self) -> bool:
        """Graph no longer has the delta link's sync state: 410, or 400 with
        one of its sync-state codes."""
        return self.status == 410 or (self.status == 400 and self.code.lower() in _SYNC_STATE_CODES)


# --- settings --------------------------------------------------------------------


def _mailbox(value: Any) -> str:
    mailbox = value if isinstance(value, str) else ""
    if not mailbox:
        raise MailboxError(
            "No mailbox is set for this trigger: name one, or set a default mailbox "
            "in the Microsoft 365 setup."
        )
    if not (_ADDRESS.fullmatch(mailbox) or _GUID.fullmatch(mailbox)):
        raise MailboxError("The mailbox must be an email address or a Graph object ID.")
    return mailbox


def _folder(value: Any) -> str:
    folder = value if isinstance(value, str) and value else DEFAULT_FOLDER
    if (
        len(folder) > 255
        or _CONTROL.search(folder)
        or folder.strip() != folder
        or folder in (".", "..")
    ):
        raise MailboxError("The folder must be a folder name such as inbox, or a folder ID.")
    return folder


def _path(value: str) -> str:
    return quote(value, safe="@")


# --- HTTP ------------------------------------------------------------------------------


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    return await asyncio.wait_for(client.request(method, url, **kwargs), timeout=REQUEST_SECONDS)


def _token(login: Mapping[str, str]) -> str:
    token = login.get("access_token")
    if not isinstance(token, str) or not token:
        raise MailboxError("The login gave no access token: connect it again.")
    return token


def _watched_mailbox(login: Mapping[str, str], setting: Any) -> str:
    """The mailbox to read. An account signed in for the organisation
    (`authorization_code`) always reads its own, as the tools do
    (`mcp_bridge/_user.py`): the Mailbox setting is for app registrations and
    is ignored. An app-only login reads the setting."""
    if login.get("grant_type") == "authorization_code":
        return _mailbox(str(login.get("account_label") or "").strip())
    return _mailbox(setting)


def _graph_link(link: Any) -> str | None:
    """`link` when it is a Graph URL, else None: a token is only ever sent
    to Graph."""
    if not isinstance(link, str):
        return None
    parts = urlsplit(link)
    if (
        parts.scheme != "https"
        or parts.netloc != _GRAPH_HOST
        or not parts.path.startswith("/v1.0/")
    ):
        return None
    return link


def _error_code(resp: httpx.Response) -> str:
    try:
        code = resp.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return ""
    return code if isinstance(code, str) and _CODE.fullmatch(code) else ""


class _Graph:
    def __init__(self, client: httpx.AsyncClient, token: str, mailbox: str, folder: str) -> None:
        self.client = client
        self.token = token
        self.mailbox = mailbox
        self.folder = folder

    async def get(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        prefer: str | None = None,
    ) -> dict[str, Any]:
        if _graph_link(url) is None:
            raise MailboxError("Microsoft Graph returned an unexpected link to continue from.")
        headers = {"Authorization": f"Bearer {self.token}"}
        if prefer:
            headers["Prefer"] = prefer
        try:
            resp = await _send(self.client, "GET", url, params=params, headers=headers)
        except (httpx.HTTPError, TimeoutError) as exc:
            raise MailboxError(
                f"Microsoft Graph could not be reached ({type(exc).__name__})."
            ) from None
        if resp.status_code >= 400:
            raise GraphError(resp.status_code, _error_code(resp))
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise MailboxError("Microsoft Graph answered with something that is not a JSON object.")
        return body

    def first_delta(self) -> str:
        return (
            f"{GRAPH}/users/{_path(self.mailbox)}/mailFolders/"
            f"{quote(self.folder, safe='')}/messages/delta"
        )

    async def page(self, link: str, *, first: bool = False) -> dict[str, Any]:
        return await self.get(
            link,
            params={"$select": _DELTA_SELECT} if first else None,
            prefer=f"odata.maxpagesize={PAGE_SIZE}",
        )

    async def message(self, mid: str, *, full: bool) -> dict[str, Any]:
        url = f"{GRAPH}/users/{_path(self.mailbox)}/messages/{quote(mid, safe='')}"
        if full:
            return await self.get(
                url,
                params={"$select": _FULL_SELECT, "$expand": _ATTACHMENTS},
                prefer='outlook.body-content-type="text"',
            )
        return await self.get(url, params={"$select": _HEADER_SELECT})


def _next(page: Mapping[str, Any]) -> tuple[str, bool]:
    """Where the round goes on, and whether that is the delta link (the
    round is done)."""
    for key, done in (("@odata.deltaLink", True), ("@odata.nextLink", False)):
        if key in page:
            link = _graph_link(page[key])
            if link is None:
                raise MailboxError("Microsoft Graph returned an unexpected link to continue from.")
            return link, done
    raise MailboxError("Microsoft Graph ended a delta page without a link to continue from.")


def _items(page: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = page.get("value")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# --- the poll ------------------------------------------------------------------------------


def _received(item: Mapping[str, Any]) -> datetime | None:
    value = item.get("receivedDateTime")
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _candidate(item: Mapping[str, Any], since: datetime) -> tuple[str, datetime] | None:
    """The id and receive time of a message that may be new mail: not
    removed, not a draft, received at or after `since`."""
    mid = item.get("id")
    if not isinstance(mid, str) or not mid or "@removed" in item or item.get("isDraft") is True:
        return None
    received = _received(item)
    return (mid, received) if received is not None and received >= since else None


def _out_of_time(deadline: float, done: int, starved: bool) -> bool:
    """Whether to stop before the next mail. A poll out of time before its
    first mail hands the cursor back marked "starved"; the next one then
    takes one mail whatever the time, so the trigger always moves on."""
    if time.monotonic() < deadline:
        return False
    return not (done == 0 and starved)


def _cursor(
    graph: _Graph, link: str, since: datetime, seen: Sequence[Seen], **extra: Any
) -> dict[str, Any]:
    """`seen` keeps the last MAX_SEEN (id, received) pairs. What is trimmed
    is forgotten, so `since` moves to the oldest receive time still kept: a
    later change to a trimmed mail older than that is never taken for new
    mail. `since` is compared with >=, so a mail in the same second as a
    trimmed one may be handed over again -- core's idempotency key drops the
    repeat -- but is never lost."""
    kept = list(seen)[-MAX_SEEN:]
    if len(seen) > MAX_SEEN and kept:
        oldest = min(datetime.fromisoformat(received) for _mid, received in kept)
        since = max(since, oldest)
    return {
        "mailbox": graph.mailbox,
        "folder": graph.folder,
        "link": link,
        "since": since.isoformat(),
        "seen": [list(pair) for pair in kept],
        **extra,
    }


def _seen_pair(value: Any) -> bool:
    if not (isinstance(value, list) and len(value) == 2):
        return False
    mid, received = value
    if not (isinstance(mid, str) and isinstance(received, str)):
        return False
    try:
        return datetime.fromisoformat(received).tzinfo is not None
    except ValueError:
        return False


def _usable(cursor: Mapping[str, Any] | None, mailbox: str, folder: str) -> dict[str, Any] | None:
    """The cursor's state when it is ours for this mailbox and folder, else
    None (take a new baseline)."""
    if not isinstance(cursor, Mapping):
        return None
    link = _graph_link(cursor.get("link"))
    seen = cursor.get("seen")
    since = cursor.get("since")
    if (
        link is None
        or cursor.get("mailbox") != mailbox
        or cursor.get("folder") != folder
        or not isinstance(seen, list)
        or not all(_seen_pair(s) for s in seen)
        or not isinstance(since, str)
    ):
        return None
    try:
        when = datetime.fromisoformat(since)
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    pairs = [(mid, received) for mid, received in seen]
    return {"link": link, "since": when, "seen": pairs, "baseline": cursor.get("baseline") is True}


async def _baseline(
    graph: _Graph,
    deadline: float,
    link: str | None = None,
    since: datetime | None = None,
    seen: Sequence[Seen] = (),
) -> dict[str, Any]:
    """Drain the first delta round without events, up to the delta link, or
    as far as the time allows (the next poll goes on). What arrived since
    `since` is marked seen: it was there when the trigger began."""
    if since is None:
        since = datetime.now(tz=UTC).replace(microsecond=0) - _SINCE_MARGIN
    marked = list(seen)
    first = link is None
    current = link or graph.first_delta()
    while True:
        page = await graph.page(current, first=first)
        first = False
        marked += [
            (found[0], found[1].isoformat())
            for item in _items(page)
            if (found := _candidate(item, since))
        ]
        current, done = _next(page)
        if done:
            return _cursor(graph, current, since, marked)
        if time.monotonic() >= deadline:
            return _cursor(graph, current, since, marked, baseline=True)


async def _round(
    graph: _Graph, state: Mapping[str, Any], deadline: float, starved: bool, limit: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    link: str = state["link"]
    since: datetime = state["since"]
    seen: list[Seen] = list(state["seen"])
    known = {mid for mid, _received in seen}
    events: list[dict[str, Any]] = []
    done = 0
    while True:
        page = await graph.page(link)
        for item in _items(page):
            found = _candidate(item, since)
            if found is None or found[0] in known:
                continue
            mid, received = found
            if done >= limit or _out_of_time(deadline, done, starved):
                # Stop inside this page: the next poll asks for it again and
                # skips what is seen.
                extra = {"starved": True} if done == 0 else {}
                return _cursor(graph, link, since, seen, **extra), events
            event = await _event(graph, mid)
            if event is not None:
                events.append(event)
            seen.append((mid, received.isoformat()))
            known.add(mid)
            done += 1
        link, finished = _next(page)
        if finished or time.monotonic() >= deadline:
            return _cursor(graph, link, since, seen), events


async def poll(
    login_fields: dict[str, str], event_config: dict[str, Any], cursor: dict[str, Any] | None
) -> PollResult:
    mailbox = _watched_mailbox(login_fields, event_config.get("mailbox"))
    folder = _folder(event_config.get("folder"))
    token = _token(login_fields)
    deadline = time.monotonic() + POLL_BUDGET_SECONDS
    starved = isinstance(cursor, Mapping) and cursor.get("starved") is True
    async with get_client() as client:
        graph = _Graph(client, token, mailbox, folder)
        state = _usable(cursor, mailbox, folder)
        if state is None:
            return PollResult(next_cursor=await _baseline(graph, deadline))
        if state["baseline"]:
            return PollResult(
                next_cursor=await _baseline(
                    graph, deadline, state["link"], state["since"], state["seen"]
                )
            )
        try:
            next_cursor, events = await _round(graph, state, deadline, starved, MAX_EVENTS_PER_TICK)
        except GraphError as exc:
            if not exc.sync_state_lost:
                raise
            # The delta link expired (Graph keeps its sync state for a limited
            # time): start over from now, firing nothing for the gap.
            logger.warning("mail.received: the delta link for a mailbox expired; new baseline")
            fresh = await _baseline(graph, deadline)
            fresh["rebaselined_at"] = datetime.now(tz=UTC).isoformat()
            return PollResult(next_cursor=fresh)
    return PollResult(next_cursor=next_cursor, events=[PolledEvent(**e) for e in events])


def register(contrib: Any) -> None:
    # No credential type: the trigger listens on the organisation's Microsoft
    # connection the agent is fixed to (`oc8.events.sources.LoginTarget`).
    contrib.add_poller(EVENT_TYPE, poll)


# --- one mail ------------------------------------------------------------------------------


async def _event(graph: _Graph, mid: str) -> dict[str, Any] | None:
    """The event for one new mail, from its headers alone when it cannot be
    read whole, or None (skipped by loop protection, gone meanwhile, or
    unreadable). A rejected login fails the poll instead: that is no
    mail's fault."""
    try:
        msg: dict[str, Any] | None = await graph.message(mid, full=True)
    except GraphError as exc:
        if exc.status in (401, 403):
            raise
        if exc.status == 404:
            return None  # deleted or moved before it was read
        logger.warning(
            "mail %s could not be fetched (HTTP %s); trying its headers", mid, exc.status
        )
        msg = None
    except MailboxError:
        logger.warning("mail %s could not be fetched; trying its headers", mid)
        msg = None
    if msg is not None:
        try:
            return await asyncio.to_thread(mail_event, mid, msg, graph.mailbox, graph.folder)
        except Exception as exc:
            # The class name and the id only: the error text may quote the mail.
            logger.warning(
                "mail %s could not be read (%s); handing it on by its headers",
                mid,
                type(exc).__name__,
            )
    try:
        head = await graph.message(mid, full=False)
    except GraphError as exc:
        if exc.status in (401, 403):
            raise
        logger.warning("mail %s skipped: not even its headers could be read", mid)
        return None
    try:
        return await asyncio.to_thread(header_event, mid, head, graph.mailbox, graph.folder)
    except Exception:
        logger.warning("mail %s skipped: its headers could not be read", mid)
        return None


def _one_line(value: str, limit: int = 500) -> str:
    """A value as one line: control characters (line breaks above all)
    become spaces, so nothing can add a line to what the model reads."""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


def _headers(msg: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    raw = msg.get("internetMessageHeaders")
    for header in raw if isinstance(raw, list) else []:
        if isinstance(header, dict):
            name, value = header.get("name"), header.get("value")
            if isinstance(name, str) and isinstance(value, str):
                out.setdefault(name.strip().lower(), []).append(_one_line(value))
    return out


def _address(entry: Any) -> tuple[str, str]:
    email = entry.get("emailAddress") if isinstance(entry, dict) else None
    if not isinstance(email, dict):
        return "", ""
    name, address = email.get("name"), email.get("address")
    return (name if isinstance(name, str) else ""), (address if isinstance(address, str) else "")


def _person(entry: Any) -> str:
    name, address = _address(entry)
    if name and address and name != address:
        return f"{name} <{address}>"
    return address or name


def _people(entries: Any) -> str:
    return (
        ", ".join(p for p in (_person(e) for e in entries) if p)
        if isinstance(entries, list)
        else ""
    )


def skip_reason(msg: Mapping[str, Any], mailbox: str) -> str | None:
    """Why the poller must not start the agent for this mail (loop
    protection), or None."""
    sender = _address(msg.get("from"))[1].strip().lower()
    if sender and sender == mailbox.lower():
        return "own address"
    if sender.split("@", 1)[0].startswith(_REPORT_SENDERS):
        return "delivery report"
    if msg.get("isDraft") is True:
        return "draft"
    headers = _headers(msg)
    for auto in headers.get("auto-submitted", []):
        if auto.split(";", 1)[0].strip().lower() != "no":
            return "auto-submitted"
    if any(p.strip().lower() in ("bulk", "junk", "list") for p in headers.get("precedence", [])):
        return "bulk"
    if any(c.lower().startswith("multipart/report") for c in headers.get("content-type", [])):
        return "delivery report"
    return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _payload(mid: str, msg: Mapping[str, Any], mailbox: str, folder: str) -> dict[str, Any]:
    listed: list[dict[str, Any]] = []
    raw = msg.get("attachments")
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            size = item.get("size")
            listed.append(
                {
                    "name": _text(item.get("name")),
                    "type": _text(item.get("contentType")),
                    "size": size if isinstance(size, int) and not isinstance(size, bool) else 0,
                }
            )
    return {
        "id": mid,
        "mailbox": mailbox,
        "folder": folder,
        "message_id": _text(msg.get("internetMessageId")),
        "conversation_id": _text(msg.get("conversationId")),
        "from": _person(msg.get("from")),
        "to": _people(msg.get("toRecipients")),
        "cc": _people(msg.get("ccRecipients")),
        "date": _text(msg.get("receivedDateTime")),
        "subject": _text(msg.get("subject")),
        "attachments": listed,
    }


def body_text(msg: Mapping[str, Any]) -> str:
    body = msg.get("body")
    if not isinstance(body, dict):
        return ""
    content = _text(body.get("content"))
    if str(body.get("contentType", "")).lower() == "html":
        content = html_to_text(content)
    return content.replace("\r\n", "\n").replace("\r", "\n").strip()


def mail_event(
    mid: str, msg: Mapping[str, Any], mailbox: str, folder: str
) -> dict[str, Any] | None:
    """`{delivery_id, payload, summary}` for a new mail, or None when loop
    protection skips it."""
    if skip_reason(msg, mailbox):
        return None
    payload = _payload(mid, msg, mailbox, folder)
    return {
        "delivery_id": mid,
        "payload": payload,
        "summary": render_summary(payload, body_text(msg)),
    }


def header_event(
    mid: str, msg: Mapping[str, Any], mailbox: str, folder: str
) -> dict[str, Any] | None:
    if skip_reason(msg, mailbox):
        return None
    payload = _payload(mid, msg, mailbox, folder)
    note = (
        "(This mail could not be read; only its headers are shown. Open it with mail_get "
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
        f"Mail id: {flat(payload['id'], 1_000)} (pass it as messageId to mail_get, mail_reply, "
        "mail_move, mail_mark_read or mail_delete)",
        f"Mailbox: {flat(payload['mailbox'])} (pass it as userId)",
        f"Folder: {flat(payload['folder'])}",
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
        lines += ["", "Attachments (open the mail with mail_get to read them):"]
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
