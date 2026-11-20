"""Mailbox access over IMAP or POP3 (incoming) and SMTP (outgoing).

One module serves three callers: the MCP bridge's tools (`tools.py`), the
`mail.received` poller (`events/source.py`) and the connection test
(`credential.py`). The latter two run in the core process and load this file
by path under a name of their own, so it imports nothing but the standard
library and nothing relative.

Every function here is blocking and meant to run in a thread
(`asyncio.to_thread`). Each socket operation has a timeout below the 60
seconds core gives a poll, so a dead server never holds a thread for long.

Rules that hold everywhere:

* **Encrypted only.** `ssl` (implicit TLS) or `starttls`. A server that does
  not offer STARTTLS is refused before any login is sent. Certificates are
  verified against the system trust store, host name included
  (`tls_context`).
* **No secret in any error.** Every failure is a `MailError` with wording of
  our own; a login failure never carries the server's reply, and any other
  server text has the passwords removed. Errors are raised without a chained
  cause, so a logged traceback carries nothing more.
* **Limits.** An attachment, read or sent, is at most 25 MB (sent attachments
  together too); a mail goes to at most 50 recipients; the text handed to
  the model is cut at 20,000 characters, with a note saying so.

Mail ids are stable for the mailbox: `<folder>:<UIDVALIDITY>:<UID>` on IMAP,
the UIDL on POP3. Attachment ids are their position in the mail, from 1.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import imaplib
import ipaddress
import mimetypes
import poplib
import re
import smtplib
import socket
import ssl
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage, Message
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from html.parser import HTMLParser
from typing import Any

#: Seconds for any one socket operation; well below core's 60-second poll.
TIMEOUT_SECONDS = 20.0
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_RECIPIENTS = 50
MAX_TEXT_CHARS = 20_000
#: A mail larger than this is not downloaded; only its headers are read.
MAX_MESSAGE_BYTES = 60 * 1024 * 1024
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
#: POP3 has no search: this many of the newest mails are searched locally.
POP3_SEARCH_WINDOW = 200
#: How many UIDLs a POP3 cursor remembers.
POP3_SEEN_CAP = 5_000
PROTOCOLS = ("imap", "pop3")
SECURITY = ("ssl", "starttls")
#: Marks everything this capa sends, so other systems' loop protection (and
#: ours) recognises it.
AUTO_SUBMITTED = "auto-replied"

#: Bridge environment variable -> `mail_account` field (tool_pack.toml's
#: `credential_env_fields` must say the same; a test holds them together).
ENV_FIELDS: dict[str, str] = {
    "MAIL_INCOMING_PROTOCOL": "incoming_protocol",
    "MAIL_INCOMING_HOST": "incoming_host",
    "MAIL_INCOMING_PORT": "incoming_port",
    "MAIL_INCOMING_SECURITY": "incoming_security",
    "MAIL_SMTP_HOST": "smtp_host",
    "MAIL_SMTP_PORT": "smtp_port",
    "MAIL_SMTP_SECURITY": "smtp_security",
    "MAIL_USERNAME": "username",
    "MAIL_PASSWORD": "password",
    "MAIL_SMTP_USERNAME": "smtp_username",
    "MAIL_SMTP_PASSWORD": "smtp_password",
    "MAIL_FROM_ADDRESS": "from_address",
    "MAIL_FROM_NAME": "from_name",
}

_POLICY = email.policy.default
_HEADER_FIELDS = "(FROM TO CC SUBJECT DATE MESSAGE-ID)"
_TEXT_TYPES = {
    "application/json",
    "application/xml",
    "application/csv",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
}


class MailError(Exception):
    """A failure whose message is safe to show anyone: our own wording, no
    secret. `reason` is "auth", "unreachable" or "invalid"."""

    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


class NotOnPop3(MailError):
    """POP3 has no folders, search, flags or move."""


#: Checks a host before we connect to it; raises `MailError` to refuse.
Guard = Callable[[str], None]


def tls_context(host: str) -> ssl.SSLContext:
    """The TLS policy for every connection: the system trust store, host name
    checked, TLS 1.2 at least. `host` is the server we are about to verify."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def default_guard(host: str) -> None:
    """The bridge's own check: never the link-local range (cloud metadata),
    multicast or unspecified addresses. Private networks stay reachable, since
    a company's own mail server often sits on one; the core-process callers
    pass core's stricter, operator-configured guard instead."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise MailError(
            f"The mail server {host} could not be found.", reason="unreachable"
        ) from None
    for info in infos:
        addr = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if addr.is_link_local or addr.is_multicast or addr.is_unspecified:
            raise MailError(
                f"The mail server {host} is not reachable from here.", reason="unreachable"
            )


# --- the login ----------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class Account:
    incoming_protocol: str
    incoming_host: str
    incoming_port: int
    incoming_security: str
    smtp_host: str
    smtp_port: int
    smtp_security: str
    username: str
    password: str
    smtp_username: str
    smtp_password: str
    from_address: str
    from_name: str

    def __repr__(self) -> str:
        return f"Account({self.username!r} at {self.incoming_host!r})"

    @property
    def secrets(self) -> list[str]:
        return [s for s in (self.password, self.smtp_password) if s]

    @property
    def own_addresses(self) -> set[str]:
        own = {self.from_address.lower()}
        if "@" in self.username:
            own.add(self.username.lower())
        return own

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Account:
        return cls.from_fields({field: env.get(name, "") for name, field in ENV_FIELDS.items()})

    @classmethod
    def from_fields(cls, values: Mapping[str, Any]) -> Account:
        """The login's fields, checked. Raises `MailError` naming the first
        bad setting (never its value when it is a password)."""

        def get(key: str) -> str:
            return str(values.get(key) or "").strip()

        protocol = get("incoming_protocol").lower()
        if protocol not in PROTOCOLS:
            raise MailError("The incoming protocol must be imap or pop3.")
        for key in ("incoming_security", "smtp_security"):
            if get(key).lower() not in SECURITY:
                raise MailError(
                    f"{key.replace('_', ' ').capitalize()} must be ssl or starttls; "
                    "unencrypted connections are not supported."
                )
        for key in ("incoming_host", "smtp_host", "username", "from_address"):
            if not get(key):
                raise MailError(f"{key.replace('_', ' ').capitalize()} is required.")
        password = str(values.get("password") or "")
        if not password:
            raise MailError("Password is required.")
        from_address = get("from_address")
        if not _is_address(from_address):
            raise MailError("The sender address is not a valid email address.")
        return cls(
            incoming_protocol=protocol,
            incoming_host=get("incoming_host"),
            incoming_port=_port(get("incoming_port"), "incoming port"),
            incoming_security=get("incoming_security").lower(),
            smtp_host=get("smtp_host"),
            smtp_port=_port(get("smtp_port"), "SMTP port"),
            smtp_security=get("smtp_security").lower(),
            username=get("username"),
            password=password,
            smtp_username=get("smtp_username") or get("username"),
            smtp_password=str(values.get("smtp_password") or "") or password,
            from_address=from_address,
            from_name=_one_line(get("from_name")),
        )


def _port(value: str, label: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise MailError(f"The {label} must be a number.") from None
    if not 0 < port < 65536:
        raise MailError(f"The {label} must be between 1 and 65535.")
    return port


def scrub(text: str, account: Account | None) -> str:
    """`text` with every password of the login removed."""
    if account is not None:
        for secret in account.secrets:
            text = text.replace(secret, "***")
    return text


def _fail(side: str, host: str, exc: BaseException, account: Account) -> MailError:
    """Our wording for a connection failure on the `side` ("incoming" or
    "outgoing") server."""
    if isinstance(exc, MailError):
        return exc
    if isinstance(exc, ssl.SSLCertVerificationError):
        return MailError(
            f"The {side} server's certificate could not be verified ({host}).",
            reason="unreachable",
        )
    if isinstance(exc, ssl.SSLError):
        return MailError(f"The TLS connection to the {side} server failed.", reason="unreachable")
    if isinstance(exc, TimeoutError | socket.timeout):
        return MailError(f"The {side} server did not answer in time.", reason="unreachable")
    if isinstance(exc, socket.gaierror):
        return MailError(f"The {side} server {host} could not be found.", reason="unreachable")
    if isinstance(exc, OSError):
        return MailError(f"The {side} server could not be reached.", reason="unreachable")
    detail = scrub(str(exc), account).strip()[:200]
    return MailError(
        f"The {side} server refused: {detail}" if detail else f"The {side} server refused."
    )


def _rejected(side: str) -> MailError:
    return MailError(f"The {side} server rejected the username or password.", reason="auth")


# --- addresses and text -------------------------------------------------------

_ADDRESS = re.compile(r"^[^@\s<>()\[\],;:\"]+@[^@\s<>()\[\],;:\"]+\.[^@\s<>()\[\],;:\"]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]+")


def _is_address(value: str) -> bool:
    return bool(_ADDRESS.match(value))


def _one_line(value: str, limit: int = 500) -> str:
    """A header value as one line: control characters (line breaks above all)
    become spaces, so a header cannot add lines to what the model reads."""
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


def _addresses(values: Sequence[str] | None, label: str) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        text = str(raw)
        if "\r" in text or "\n" in text:
            raise MailError(f"A {label} address contains a line break.")
        for name, addr in getaddresses([text]):
            if not _is_address(addr):
                raise MailError(f"{text!r} is not a valid address.")
            out.append(formataddr((name, addr)) if name else addr)
    return out


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # a malformed header: fall back to its raw bytes
        raw = msg.get_all(name, failobj=[])
        value = str(raw[0]) if raw else ""
    return _one_line(str(value or ""))


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
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return (
        text[:MAX_TEXT_CHARS]
        + f"\n\n[Text truncated: showing the first 20,000 of {len(text):,} characters.]",
        True,
    )


def _part_text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except (LookupError, ValueError, KeyError, AssertionError):
        pass
    payload = part.get_payload(decode=True)
    return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def _body_part(msg: EmailMessage) -> EmailMessage | None:
    try:
        return msg.get_body(preferencelist=("plain", "html"))  # type: ignore[return-value]
    except Exception:
        return None


def body_text(msg: EmailMessage) -> str:
    part = _body_part(msg)
    if part is None:
        return ""
    text = _part_text(part)
    if part.get_content_type() == "text/html":
        text = html_to_text(text)
    return text.strip()


def _leaves(part: EmailMessage) -> Iterator[EmailMessage]:
    if part.get_content_maintype() == "multipart":
        for sub in part.iter_parts():
            yield from _leaves(sub)  # type: ignore[arg-type]
    else:
        yield part


def _attachment_parts(msg: EmailMessage) -> list[EmailMessage]:
    body = _body_part(msg)
    out: list[EmailMessage] = []
    for leaf in _leaves(msg):
        if leaf is body:
            continue
        named = bool(leaf.get_filename())
        if (
            leaf.get_content_type() in ("text/plain", "text/html")
            and not named
            and leaf.get_content_disposition() != "attachment"
        ):
            continue  # the other half of a text/html alternative
        out.append(leaf)
    return out


def _part_bytes(part: EmailMessage) -> bytes:
    if part.get_content_maintype() == "message":
        inner = part.get_payload()
        first = inner[0] if isinstance(inner, list) and inner else None
        if isinstance(first, Message):
            return first.as_bytes(policy=_POLICY)
        if isinstance(inner, str):
            return inner.encode("utf-8", "replace")
        return b""
    payload = part.get_payload(decode=True)
    return payload if isinstance(payload, bytes) else b""


def _attachment_name(part: EmailMessage, index: int) -> str:
    name = _one_line(part.get_filename() or "", 200).replace("/", "_").replace("\\", "_")
    if name:
        return name
    if part.get_content_type() == "message/rfc822":
        return f"message-{index}.eml"
    return f"attachment-{index}{mimetypes.guess_extension(part.get_content_type()) or '.bin'}"


def attachments(msg: EmailMessage) -> list[dict[str, Any]]:
    return [
        {
            "id": str(index),
            "name": _attachment_name(part, index),
            "type": part.get_content_type(),
            "size": len(_part_bytes(part)),
        }
        for index, part in enumerate(_attachment_parts(msg), start=1)
    ]


def parse(raw: bytes) -> EmailMessage:
    msg = email.message_from_bytes(raw, policy=_POLICY)
    assert isinstance(msg, EmailMessage)
    return msg


def skip_reason(msg: EmailMessage, account: Account) -> str | None:
    """Why the poller must not start the agent for this mail (loop
    protection), or None. Mail from the mailbox itself, automatic mail
    (`Auto-Submitted` other than "no"), bulk and list mail, delivery reports."""
    sender = parseaddr(_header(msg, "From"))[1].lower()
    if sender and sender in account.own_addresses:
        return "own address"
    auto = _header(msg, "Auto-Submitted").split(";", 1)[0].strip().lower()
    if auto and auto != "no":
        return "auto-submitted"
    if _header(msg, "Precedence").strip().lower() in ("bulk", "junk", "list"):
        return "bulk"
    if msg.get_content_type() == "multipart/report":
        return "delivery report"
    return None


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value, unit = (n / 1024, "KB") if n < 1024 * 1024 else (n / (1024 * 1024), "MB")
    return f"{value:.1f}".removesuffix(".0") + f" {unit}"


# --- mail ids -----------------------------------------------------------------


@dataclass(frozen=True)
class ImapId:
    folder: str
    uidvalidity: int
    uid: int

    def __str__(self) -> str:
        return f"{self.folder}:{self.uidvalidity}:{self.uid}"

    @classmethod
    def parse(cls, value: str) -> ImapId:
        try:
            folder, uidvalidity, uid = str(value).rsplit(":", 2)
            parsed = cls(folder, int(uidvalidity), int(uid))
        except ValueError:
            raise MailError(
                f"{value!r} is not a valid mail id; take the id from mail_search or mail_get."
            ) from None
        if not folder or parsed.uid <= 0:
            raise MailError(f"{value!r} is not a valid mail id.")
        return parsed


# --- IMAP ---------------------------------------------------------------------


def _mutf7_encode(name: str) -> str:
    """A folder name in IMAP's modified UTF-7 (RFC 3501, 5.1.3)."""
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode()
            out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


def _mutf7_decode(name: str) -> str:
    def repl(match: re.Match[str]) -> str:
        chunk = match.group(1)
        if not chunk:
            return "&"
        padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
        try:
            return base64.b64decode(padded).decode("utf-16-be")
        except (binascii.Error, UnicodeDecodeError):
            return match.group(0)

    return re.sub(r"&([^-]*)-", repl, name)


def _quote(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise MailError("A value contains a line break.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _folder_arg(folder: str) -> str:
    return _quote(_mutf7_encode(folder))


@contextmanager
def imap_session(account: Account, guard: Guard | None = None) -> Iterator[imaplib.IMAP4]:
    host, port = account.incoming_host, account.incoming_port
    (guard or default_guard)(host)
    try:
        context = tls_context(host)
        conn: imaplib.IMAP4
        if account.incoming_security == "ssl":
            conn = imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=TIMEOUT_SECONDS)
        else:
            conn = imaplib.IMAP4(host, port, timeout=TIMEOUT_SECONDS)
            if "STARTTLS" not in conn.capabilities:
                with suppress(Exception):
                    conn.shutdown()
                raise MailError(
                    "The incoming server does not offer STARTTLS, so the password "
                    "would travel unencrypted. Use ssl or another port.",
                    reason="unreachable",
                )
            conn.starttls(ssl_context=context)
    except Exception as exc:
        raise _fail("incoming", host, exc, account) from None
    try:
        try:
            conn.login(account.username, account.password)
        except UnicodeEncodeError:
            user, secret = account.username.encode(), account.password.encode()
            conn.authenticate("PLAIN", lambda _: b"\0" + user + b"\0" + secret)
    except imaplib.IMAP4.error:
        with suppress(Exception):
            conn.shutdown()
        raise _rejected("incoming") from None
    except Exception as exc:
        with suppress(Exception):
            conn.shutdown()
        raise _fail("incoming", host, exc, account) from None
    try:
        yield conn
    except MailError:
        raise
    except Exception as exc:
        raise _fail("incoming", host, exc, account) from None
    finally:
        with suppress(Exception):
            conn.logout()


def _ok(typ: str, data: Any, message: str) -> Any:
    if typ != "OK":
        raise MailError(message)
    return data


def _select(conn: imaplib.IMAP4, folder: str, *, readonly: bool = True) -> int:
    typ, _ = conn.select(_folder_arg(folder), readonly=readonly)
    if typ != "OK":
        raise MailError(f"The folder {folder!r} does not exist.")
    _, data = conn.response("UIDVALIDITY")
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        raise MailError("The server did not report the folder's UIDVALIDITY.") from None


def _select_id(conn: imaplib.IMAP4, mail_id: ImapId, *, readonly: bool = True) -> None:
    if _select(conn, mail_id.folder, readonly=readonly) != mail_id.uidvalidity:
        raise MailError(
            "This mail id is no longer valid: the folder was reset on the server. "
            "Search again for a current id."
        )


def _fetch_items(data: list[Any]) -> list[tuple[bytes, bytes]]:
    return [(item[0], item[1]) for item in data if isinstance(item, tuple)]


_UID = re.compile(rb"UID (\d+)")
_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")


def _imap_size(conn: imaplib.IMAP4, uid: int) -> int:
    _, data = conn.uid("FETCH", str(uid), "(RFC822.SIZE)")
    for line in data or []:
        raw = line[0] if isinstance(line, tuple) else line
        if isinstance(raw, bytes) and (m := _SIZE.search(raw)):
            return int(m.group(1))
    raise MailError("Mail not found; it may have been moved or deleted.")


def _imap_raw(conn: imaplib.IMAP4, uid: int, *, section: str = "") -> bytes:
    typ, data = conn.uid("FETCH", str(uid), f"(BODY.PEEK[{section}])")
    items = _fetch_items(data or []) if typ == "OK" else []
    if not items:
        raise MailError("Mail not found; it may have been moved or deleted.")
    return items[0][1]


def _imap_message(conn: imaplib.IMAP4, mail_id: ImapId) -> bytes:
    size = _imap_size(conn, mail_id.uid)
    if size > MAX_MESSAGE_BYTES:
        raise MailError(f"This mail is too large to read here ({_size(size)}).")
    return _imap_raw(conn, mail_id.uid)


def _search_criteria(
    conn: imaplib.IMAP4,
    *,
    sender: str,
    to: str,
    subject: str,
    text: str,
    since: str,
    before: str,
    unread_only: bool,
) -> list[str]:
    plain: list[str] = []
    wide: list[tuple[str, str]] = []
    for key, value in (("FROM", sender), ("TO", to), ("SUBJECT", subject), ("TEXT", text)):
        if not value:
            continue
        if value.isascii():
            plain += [key, _quote(value)]
        else:
            wide.append((key, value))
    for key, value in (("SINCE", since), ("BEFORE", before)):
        if value:
            plain += [key, _imap_date(value)]
    if unread_only:
        plain.append("UNSEEN")
    if len(wide) > 1:
        raise MailError("Search with at most one value containing non-ASCII characters at a time.")
    if wide:
        # imaplib sends one literal, at the end of the command.
        key, value = wide[0]
        conn.literal = value.encode()  # type: ignore[assignment]  # imaplib sends bytes as-is
        return ["CHARSET", "UTF-8", *plain, key]
    return plain or ["ALL"]


def _imap_date(value: str) -> str:
    try:
        day = date.fromisoformat(value[:10])
    except ValueError:
        raise MailError(f"{value!r} is not a date; use YYYY-MM-DD.") from None
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    return f"{day.day:02d}-{months[day.month - 1]}-{day.year}"


def _imap_search(
    account: Account, guard: Guard | None, folder: str, limit: int, criteria: dict[str, Any]
) -> list[dict[str, Any]]:
    with imap_session(account, guard) as conn:
        uidvalidity = _select(conn, folder)
        args = _search_criteria(conn, **criteria)
        data = _ok(*conn.uid("SEARCH", *args), "The server refused the search.")
        uids = sorted({int(u) for u in b" ".join(data or [b""]).split()}, reverse=True)[:limit]
        if not uids:
            return []
        uid_set = ",".join(str(u) for u in uids)
        meta: dict[int, dict[str, Any]] = {}
        _, flags = conn.uid("FETCH", uid_set, "(UID FLAGS RFC822.SIZE)")
        for line in flags or []:
            raw = line[0] if isinstance(line, tuple) else line
            if not isinstance(raw, bytes) or not (m := _UID.search(raw)):
                continue
            f = _FLAGS.search(raw)
            s = _SIZE.search(raw)
            meta[int(m.group(1))] = {
                "unread": b"\\Seen" not in (f.group(1) if f else b""),
                "size": int(s.group(1)) if s else 0,
            }
        _, headers = conn.uid("FETCH", uid_set, f"(UID BODY.PEEK[HEADER.FIELDS {_HEADER_FIELDS}])")
        found: dict[int, dict[str, Any]] = {}
        for prefix, raw in _fetch_items(headers or []):
            if not (m := _UID.search(prefix)):
                continue
            uid = int(m.group(1))
            msg = parse(raw)
            found[uid] = {
                "id": str(ImapId(folder, uidvalidity, uid)),
                "from": _header(msg, "From"),
                "to": _header(msg, "To"),
                "subject": _header(msg, "Subject"),
                "date": _header(msg, "Date"),
                "message_id": _header(msg, "Message-ID"),
                **meta.get(uid, {"unread": False, "size": 0}),
            }
        return [found[u] for u in uids if u in found]


def _list_folders(account: Account, guard: Guard | None) -> list[dict[str, Any]]:
    with imap_session(account, guard) as conn:
        data = _ok(*conn.list(), "The server refused to list folders.")
    out: list[dict[str, Any]] = []
    pattern = re.compile(r'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$')
    pending: str | None = None
    for item in data or []:
        if isinstance(item, tuple):
            line = item[0].decode("utf-8", "replace")
            pending = item[1].decode("utf-8", "replace")
        elif isinstance(item, bytes):
            line, pending = item.decode("utf-8", "replace"), None
        else:
            continue
        m = pattern.match(line)
        if not m:
            continue
        name = pending if pending is not None else m.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        flags = m.group("flags").split()
        if "\\Noselect" in flags or "\\NonExistent" in flags:
            continue
        out.append(
            {
                "name": _mutf7_decode(name),
                "special_use": [f for f in flags if f.lower() in _SPECIAL_USE],
            }
        )
    return out


_SPECIAL_USE = {"\\all", "\\archive", "\\drafts", "\\flagged", "\\junk", "\\sent", "\\trash"}


# --- POP3 ---------------------------------------------------------------------


@contextmanager
def pop3_session(account: Account, guard: Guard | None = None) -> Iterator[poplib.POP3]:
    """A logged-in POP3 session. Deletions are committed when the block ends
    normally (QUIT); on an error the connection is dropped and nothing is
    deleted."""
    host, port = account.incoming_host, account.incoming_port
    (guard or default_guard)(host)
    try:
        context = tls_context(host)
        conn: poplib.POP3
        if account.incoming_security == "ssl":
            conn = poplib.POP3_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            conn = poplib.POP3(host, port, timeout=TIMEOUT_SECONDS)
            try:
                capabilities = conn.capa()
            except poplib.error_proto:
                capabilities = {}
            if "STLS" not in capabilities:
                with suppress(Exception):
                    conn.close()
                raise MailError(
                    "The incoming server does not offer STARTTLS (STLS), so the password "
                    "would travel unencrypted. Use ssl or another port.",
                    reason="unreachable",
                )
            conn.stls(context=context)
    except Exception as exc:
        raise _fail("incoming", host, exc, account) from None
    try:
        conn.user(account.username)
        conn.pass_(account.password)
    except poplib.error_proto:
        with suppress(Exception):
            conn.close()
        raise _rejected("incoming") from None
    except Exception as exc:
        with suppress(Exception):
            conn.close()
        raise _fail("incoming", host, exc, account) from None
    try:
        yield conn
    except MailError:
        with suppress(Exception):
            conn.close()
        raise
    except Exception as exc:
        with suppress(Exception):
            conn.close()
        raise _fail("incoming", host, exc, account) from None
    else:
        with suppress(Exception):
            conn.quit()


def _pop3_listing(conn: poplib.POP3) -> list[tuple[int, str, int]]:
    """`(number, uidl, size)` for every mail, oldest first."""
    _, uidl_lines, _ = conn.uidl()
    _, size_lines, _ = conn.list()
    sizes: dict[int, int] = {}
    for line in size_lines:
        number, _, size = line.decode("ascii", "replace").partition(" ")
        with suppress(ValueError):
            sizes[int(number)] = int(size.split()[0])
    out: list[tuple[int, str, int]] = []
    for line in uidl_lines:
        number, _, uidl = line.decode("ascii", "replace").partition(" ")
        with suppress(ValueError):
            out.append((int(number), uidl.strip(), sizes.get(int(number), 0)))
    return out


def _pop3_find(conn: poplib.POP3, uidl: str) -> tuple[int, int]:
    for number, found, size in _pop3_listing(conn):
        if found == uidl:
            return number, size
    raise MailError("Mail not found; it may have been deleted.")


def _pop3_raw(conn: poplib.POP3, number: int, *, headers_only: bool = False) -> bytes:
    if headers_only:
        _, lines, _ = conn.top(number, 0)
    else:
        _, lines, _ = conn.retr(number)
    return b"\r\n".join(lines) + b"\r\n"


def _pop3_message(conn: poplib.POP3, uidl: str) -> bytes:
    number, size = _pop3_find(conn, uidl)
    if size > MAX_MESSAGE_BYTES:
        raise MailError(f"This mail is too large to read here ({_size(size)}).")
    return _pop3_raw(conn, number)


def _pop3_unsupported(what: str) -> NotOnPop3:
    return NotOnPop3(
        f"This mailbox uses POP3, which has no {what}. POP3 only lists, reads and "
        "deletes the mail in the inbox."
    )


def _contains(haystack: str, needle: str) -> bool:
    return needle.casefold() in haystack.casefold()


def _pop3_search(
    account: Account, guard: Guard | None, limit: int, criteria: dict[str, Any]
) -> list[dict[str, Any]]:
    since = date.fromisoformat(criteria["since"][:10]) if criteria["since"] else None
    before = date.fromisoformat(criteria["before"][:10]) if criteria["before"] else None
    found: list[dict[str, Any]] = []
    with pop3_session(account, guard) as conn:
        listing = _pop3_listing(conn)[-POP3_SEARCH_WINDOW:]
        for number, uidl, size in reversed(listing):
            needs_body = bool(criteria["text"])
            if needs_body and size > MAX_MESSAGE_BYTES:
                continue
            msg = parse(_pop3_raw(conn, number, headers_only=not needs_body))
            if not _matches(msg, criteria, since, before):
                continue
            found.append(
                {
                    "id": uidl,
                    "from": _header(msg, "From"),
                    "to": _header(msg, "To"),
                    "subject": _header(msg, "Subject"),
                    "date": _header(msg, "Date"),
                    "message_id": _header(msg, "Message-ID"),
                    "size": size,
                }
            )
            if len(found) >= limit:
                break
    return found


def _matches(
    msg: EmailMessage, criteria: dict[str, Any], since: date | None, before: date | None
) -> bool:
    for key, header in (("sender", "From"), ("subject", "Subject")):
        if criteria[key] and not _contains(_header(msg, header), criteria[key]):
            return False
    if criteria["to"] and not (
        _contains(_header(msg, "To"), criteria["to"])
        or _contains(_header(msg, "Cc"), criteria["to"])
    ):
        return False
    if criteria["text"] and not (
        _contains(body_text(msg), criteria["text"])
        or _contains(_header(msg, "Subject"), criteria["text"])
    ):
        return False
    if since or before:
        try:
            sent = msg["Date"].datetime.date()
        except Exception:
            return False
        if since and sent < since:
            return False
        if before and sent >= before:
            return False
    return True


def pop3_new_uidls(
    listing: Sequence[str], cursor: Mapping[str, Any] | None, *, limit: int
) -> tuple[dict[str, Any], list[str]]:
    """The POP3 cursor arithmetic: `(next_cursor, new UIDLs to process)`.

    No usable cursor is a baseline: everything present is seen. Otherwise a
    UIDL is new when it is not in the seen list and does not come before the
    oldest seen one still on the server (so mail that fell out of the capped
    list stays old). At most `limit` are taken; the rest come next time."""
    seen_list = cursor.get("seen") if isinstance(cursor, Mapping) else None
    if not isinstance(seen_list, list):
        return {"seen": list(listing)[-POP3_SEEN_CAP:]}, []
    seen = {str(u) for u in seen_list}
    start = next((i for i, u in enumerate(listing) if u in seen), 0)
    new = [u for u in listing[start:] if u not in seen][:limit]
    taken = set(new)
    kept = [u for u in listing if u in seen or u in taken]
    return {"seen": kept[-POP3_SEEN_CAP:]}, new


# --- one mail, either protocol ------------------------------------------------


def _with_message(
    account: Account, guard: Guard | None, mail_id: str
) -> tuple[bytes, str | None, bool | None]:
    """The raw mail, its folder and whether it is unread (both None on POP3),
    in one session. Fetched with PEEK: reading it does not mark it read."""
    if account.incoming_protocol == "pop3":
        with pop3_session(account, guard) as conn:
            return _pop3_message(conn, str(mail_id)), None, None
    parsed_id = ImapId.parse(mail_id)
    with imap_session(account, guard) as conn:
        _select_id(conn, parsed_id)
        raw = _imap_message(conn, parsed_id)
        _, data = conn.uid("FETCH", str(parsed_id.uid), "(FLAGS)")
    unread: bool | None = None
    for line in data or []:
        flags = line[0] if isinstance(line, tuple) else line
        if isinstance(flags, bytes) and (f := _FLAGS.search(flags)):
            unread = b"\\Seen" not in f.group(1)
    return raw, parsed_id.folder, unread


# --- the tools ----------------------------------------------------------------


def list_folders(account: Account, *, guard: Guard | None = None) -> list[dict[str, Any]]:
    if account.incoming_protocol == "pop3":
        raise _pop3_unsupported("folders")
    return _list_folders(account, guard)


def search(
    account: Account,
    *,
    folder: str = "INBOX",
    sender: str = "",
    to: str = "",
    subject: str = "",
    text: str = "",
    since: str = "",
    before: str = "",
    unread_only: bool = False,
    limit: int = DEFAULT_SEARCH_LIMIT,
    guard: Guard | None = None,
) -> list[dict[str, Any]]:
    """Newest first, at most `limit` (1 to 100)."""
    limit = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), MAX_SEARCH_LIMIT))
    for value in (since, before):
        if value:
            _imap_date(value)
    criteria = {
        "sender": sender,
        "to": to,
        "subject": subject,
        "text": text,
        "since": since,
        "before": before,
    }
    if account.incoming_protocol == "pop3":
        if folder and folder.upper() != "INBOX":
            raise _pop3_unsupported("folders")
        if unread_only:
            raise _pop3_unsupported("read or unread flags")
        return _pop3_search(account, guard, limit, criteria)
    return _imap_search(
        account, guard, folder or "INBOX", limit, {**criteria, "unread_only": unread_only}
    )


def get(account: Account, mail_id: str, *, guard: Guard | None = None) -> dict[str, Any]:
    raw, folder, unread = _with_message(account, guard, mail_id)
    msg = parse(raw)
    text, truncated = truncate(body_text(msg))
    return {
        "id": str(mail_id),
        "folder": folder,
        "message_id": _header(msg, "Message-ID"),
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "cc": _header(msg, "Cc"),
        "reply_to": _header(msg, "Reply-To"),
        "date": _header(msg, "Date"),
        "subject": _header(msg, "Subject"),
        "in_reply_to": _header(msg, "In-Reply-To"),
        "unread": unread,
        "text": text,
        "text_truncated": truncated,
        "attachments": attachments(msg),
    }


def get_attachment(
    account: Account, mail_id: str, attachment_id: str, *, guard: Guard | None = None
) -> dict[str, Any]:
    """One attachment's content: as text for a text type, else base64."""
    raw, _, _ = _with_message(account, guard, mail_id)
    msg = parse(raw)
    parts = _attachment_parts(msg)
    try:
        index = int(str(attachment_id).strip())
        if index < 1:
            raise IndexError
        part = parts[index - 1]
    except (ValueError, IndexError):
        raise MailError(
            f"This mail has no attachment {attachment_id}; it has {len(parts)}. "
            "Take the attachment id from mail_get."
        ) from None
    data = _part_bytes(part)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise MailError(
            f"This attachment is {_size(len(data))}, larger than the "
            f"{_size(MAX_ATTACHMENT_BYTES)} that can be read."
        )
    mime = part.get_content_type()
    result: dict[str, Any] = {
        "id": str(mail_id),
        "attachment_id": str(index),
        "name": _attachment_name(part, index),
        "type": mime,
        "size": len(data),
    }
    if part.get_content_maintype() == "text" or mime in _TEXT_TYPES:
        charset = part.get_content_charset() or "utf-8"
        try:
            content = data.decode(charset, "replace")
        except LookupError:
            content = data.decode("utf-8", "replace")
        return {**result, "encoding": "text", "content": content}
    return {**result, "encoding": "base64", "content": base64.b64encode(data).decode()}


def _need_imap(account: Account, what: str) -> None:
    if account.incoming_protocol == "pop3":
        raise _pop3_unsupported(what)


def move(
    account: Account, mail_id: str, folder: str, *, guard: Guard | None = None
) -> dict[str, Any]:
    _need_imap(account, "folders to move mail between")
    parsed_id = ImapId.parse(mail_id)
    with imap_session(account, guard) as conn:
        typ, _ = conn.select(_folder_arg(folder), readonly=True)
        if typ != "OK":
            raise MailError(f"The folder {folder!r} does not exist.")
        _select_id(conn, parsed_id, readonly=False)
        _imap_size(conn, parsed_id.uid)
        uid = str(parsed_id.uid)
        if "MOVE" in conn.capabilities:
            typ, data = conn.xatom("UID", "MOVE", uid, _folder_arg(folder))
            _ok(typ, data, "The server refused to move the mail.")
        else:
            typ, data = conn.uid("COPY", uid, _folder_arg(folder))
            _ok(typ, data, "The server refused to move the mail.")
            _delete_uid(conn, uid)
        new_id = _copied_id(conn, data, folder)
    result: dict[str, Any] = {"status": "moved", "folder": folder}
    if new_id:
        result["id"] = new_id
    else:
        result["note"] = "Search the folder for the mail's new id."
    return result


_COPYUID = re.compile(rb"COPYUID (\d+) \S+ (\d+)")


def _copied_id(conn: imaplib.IMAP4, data: Any, folder: str) -> str | None:
    candidates: list[bytes] = []
    for value in [*(data or []), *conn.untagged_responses.get("OK", [])]:
        if isinstance(value, bytes):
            candidates.append(value)
    for value in candidates:
        if m := _COPYUID.search(value):
            return str(ImapId(folder, int(m.group(1)), int(m.group(2))))
    return None


def _delete_uid(conn: imaplib.IMAP4, uid: str) -> None:
    _ok(
        *conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)"),
        "The server refused to delete the mail.",
    )
    if "UIDPLUS" in conn.capabilities:
        _ok(*conn.xatom("UID", "EXPUNGE", uid), "The server refused to delete the mail.")
    else:
        _ok(*conn.expunge(), "The server refused to delete the mail.")


def mark_read(
    account: Account, mail_id: str, read: bool = True, *, guard: Guard | None = None
) -> dict[str, Any]:
    _need_imap(account, "read or unread flags")
    parsed_id = ImapId.parse(mail_id)
    with imap_session(account, guard) as conn:
        _select_id(conn, parsed_id, readonly=False)
        _imap_size(conn, parsed_id.uid)
        _ok(
            *conn.uid("STORE", str(parsed_id.uid), "+FLAGS" if read else "-FLAGS", "(\\Seen)"),
            "The server refused to change the mail's flags.",
        )
    return {"status": "marked read" if read else "marked unread", "id": str(mail_id)}


def delete(account: Account, mail_id: str, *, guard: Guard | None = None) -> dict[str, Any]:
    if account.incoming_protocol == "pop3":
        with pop3_session(account, guard) as conn:
            number, _ = _pop3_find(conn, str(mail_id))
            conn.dele(number)
        return {"status": "deleted", "id": str(mail_id)}
    parsed_id = ImapId.parse(mail_id)
    with imap_session(account, guard) as conn:
        _select_id(conn, parsed_id, readonly=False)
        _imap_size(conn, parsed_id.uid)
        _delete_uid(conn, str(parsed_id.uid))
    return {"status": "deleted", "id": str(mail_id)}


# --- sending ------------------------------------------------------------------


@contextmanager
def smtp_session(account: Account, guard: Guard | None = None) -> Iterator[smtplib.SMTP]:
    host, port = account.smtp_host, account.smtp_port
    (guard or default_guard)(host)
    try:
        context = tls_context(host)
        conn: smtplib.SMTP
        if account.smtp_security == "ssl":
            conn = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context)
            conn.ehlo()
        else:
            conn = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
            conn.ehlo()
            if not conn.has_extn("starttls"):
                with suppress(Exception):
                    conn.close()
                raise MailError(
                    "The outgoing server does not offer STARTTLS, so the password "
                    "would travel unencrypted. Use ssl or another port.",
                    reason="unreachable",
                )
            conn.starttls(context=context)
            conn.ehlo()
    except Exception as exc:
        raise _fail("outgoing", host, exc, account) from None
    try:
        try:
            conn.login(account.smtp_username, account.smtp_password)
        except UnicodeEncodeError:
            _smtp_login_utf8(conn, account)
    except smtplib.SMTPAuthenticationError:
        with suppress(Exception):
            conn.close()
        raise _rejected("outgoing") from None
    except smtplib.SMTPNotSupportedError:
        with suppress(Exception):
            conn.close()
        raise MailError("The outgoing server offers no login.", reason="auth") from None
    except Exception as exc:
        with suppress(Exception):
            conn.close()
        raise _fail("outgoing", host, exc, account) from None
    try:
        yield conn
    except MailError:
        raise
    except smtplib.SMTPRecipientsRefused:
        raise MailError("The outgoing server refused the recipients.") from None
    except Exception as exc:
        raise _fail("outgoing", host, exc, account) from None
    finally:
        with suppress(Exception):
            conn.quit()


def _smtp_login_utf8(conn: smtplib.SMTP, account: Account) -> None:
    """AUTH PLAIN with UTF-8, for a login smtplib cannot encode (it only
    sends ASCII)."""
    token = b"\0" + account.smtp_username.encode() + b"\0" + account.smtp_password.encode()
    code, _ = conn.docmd("AUTH", "PLAIN " + base64.b64encode(token).decode())
    if code != 235:
        raise smtplib.SMTPAuthenticationError(code, b"rejected")


@dataclass(frozen=True)
class Outgoing:
    to: list[str]
    cc: list[str]
    bcc: list[str]

    @property
    def everyone(self) -> list[str]:
        return [parseaddr(a)[1] for a in (*self.to, *self.cc, *self.bcc)]


def _recipients(
    to: Sequence[str] | None, cc: Sequence[str] | None, bcc: Sequence[str] | None
) -> Outgoing:
    out = Outgoing(_addresses(to, "To"), _addresses(cc, "Cc"), _addresses(bcc, "Bcc"))
    count = len(out.everyone)
    if count == 0:
        raise MailError("Name at least one recipient.")
    if count > MAX_RECIPIENTS:
        raise MailError(
            f"A mail may have at most {MAX_RECIPIENTS} recipients; this one has {count}."
        )
    return out


def _attachment_data(items: Sequence[Mapping[str, Any]] | None) -> list[tuple[str, str, bytes]]:
    """`(name, type, bytes)` for each attachment the agent passes: `name`
    plus either `text` or `content_base64`, optionally `type`."""
    out: list[tuple[str, str, bytes]] = []
    total = 0
    for item in items or []:
        if not isinstance(item, Mapping):
            raise MailError("Each attachment needs a name and text or content_base64.")
        name = _one_line(str(item.get("name") or ""), 200).replace("/", "_").replace("\\", "_")
        if not name:
            raise MailError("Each attachment needs a name.")
        if isinstance(item.get("text"), str):
            data = item["text"].encode("utf-8")
        elif isinstance(item.get("content_base64"), str):
            try:
                data = base64.b64decode(item["content_base64"], validate=True)
            except (binascii.Error, ValueError):
                raise MailError(f"The content of {name!r} is not valid base64.") from None
        else:
            raise MailError(f"The attachment {name!r} needs text or content_base64.")
        total += len(data)
        if len(data) > MAX_ATTACHMENT_BYTES or total > MAX_ATTACHMENT_BYTES:
            raise MailError(
                f"Attachments may be at most {_size(MAX_ATTACHMENT_BYTES)} together; "
                f"{name!r} goes over that."
            )
        mime = str(item.get("type") or "") or mimetypes.guess_type(name)[0] or ""
        if "/" not in mime or "\r" in mime or "\n" in mime:
            mime = "application/octet-stream"
        out.append((name, mime, data))
    return out


def _new_message(
    account: Account,
    recipients: Outgoing,
    subject: str,
    body: str,
    *,
    in_reply_to: str = "",
    references: str = "",
) -> EmailMessage:
    if "\r" in subject or "\n" in subject:
        raise MailError("The subject contains a line break.")
    msg = EmailMessage()
    msg["From"] = (
        formataddr((account.from_name, account.from_address))
        if account.from_name
        else account.from_address
    )
    if recipients.to:
        msg["To"] = ", ".join(recipients.to)
    if recipients.cc:
        msg["Cc"] = ", ".join(recipients.cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=account.from_address.rsplit("@", 1)[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg["Auto-Submitted"] = AUTO_SUBMITTED
    msg.set_content(body or "")
    return msg


def _attach(msg: EmailMessage, files: list[tuple[str, str, bytes]]) -> None:
    for name, mime, data in files:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)


def _deliver(
    account: Account, guard: Guard | None, msg: EmailMessage, recipients: Outgoing
) -> dict[str, Any]:
    with smtp_session(account, guard) as conn:
        conn.send_message(msg, from_addr=account.from_address, to_addrs=recipients.everyone)
    return {
        "status": "sent",
        "message_id": str(msg["Message-ID"]),
        "recipients": len(recipients.everyone),
    }


def send(
    account: Account,
    *,
    to: Sequence[str],
    subject: str,
    body: str,
    cc: Sequence[str] | None = None,
    bcc: Sequence[str] | None = None,
    attachments: Sequence[Mapping[str, Any]] | None = None,
    guard: Guard | None = None,
) -> dict[str, Any]:
    recipients = _recipients(to, cc, bcc)
    files = _attachment_data(attachments)
    msg = _new_message(account, recipients, subject, body)
    _attach(msg, files)
    return _deliver(account, guard, msg, recipients)


def _prefixed(prefix: str, subject: str) -> str:
    return subject if subject.lower().startswith(prefix.lower()) else f"{prefix} {subject}".strip()


def reply(
    account: Account,
    mail_id: str,
    *,
    body: str,
    reply_all: bool = False,
    attachments: Sequence[Mapping[str, Any]] | None = None,
    guard: Guard | None = None,
) -> dict[str, Any]:
    """Answer a mail: to its Reply-To (else From), with reply-all adding its
    To and Cc except this mailbox. Keeps the thread (`In-Reply-To`,
    `References`) and quotes the original text."""
    raw, _, _ = _with_message(account, guard, mail_id)
    original = parse(raw)
    own = account.own_addresses

    def others(header: str) -> list[str]:
        out = []
        for name, addr in getaddresses([_header(original, header)]):
            if addr and addr.lower() not in own:
                out.append(formataddr((name, addr)) if name else addr)
        return out

    to = others("Reply-To") or others("From")
    cc: list[str] = []
    if reply_all:
        taken = {parseaddr(a)[1].lower() for a in to}
        for addr in [*others("To"), *others("Cc")]:
            if parseaddr(addr)[1].lower() not in taken:
                taken.add(parseaddr(addr)[1].lower())
                cc.append(addr)
    if not to:
        raise MailError("This mail names nobody to reply to except this mailbox.")
    recipients = _recipients(to, cc, None)
    message_id = _header(original, "Message-ID")
    references = " ".join(x for x in (_header(original, "References"), message_id) if x)
    quoted, _ = truncate(body_text(original))
    quote_lines = "\n".join(f"> {line}" if line else ">" for line in quoted.splitlines())
    header = f"On {_header(original, 'Date')}, {_header(original, 'From')} wrote:"
    msg = _new_message(
        account,
        recipients,
        _prefixed("Re:", _header(original, "Subject")),
        f"{body}\n\n{header}\n{quote_lines}\n",
        in_reply_to=message_id,
        references=references,
    )
    _attach(msg, _attachment_data(attachments))
    return _deliver(account, guard, msg, recipients)


def forward(
    account: Account,
    mail_id: str,
    *,
    to: Sequence[str],
    body: str = "",
    cc: Sequence[str] | None = None,
    guard: Guard | None = None,
) -> dict[str, Any]:
    """Send a mail on, attached whole (message/rfc822) below `body`."""
    recipients = _recipients(to, cc, None)
    raw, _, _ = _with_message(account, guard, mail_id)
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise MailError(f"This mail is {_size(len(raw))}, too large to forward.")
    original = parse(raw)
    msg = _new_message(account, recipients, _prefixed("Fwd:", _header(original, "Subject")), body)
    msg.add_attachment(original)
    return _deliver(account, guard, msg, recipients)


# --- the connection test --------------------------------------------------------


def check_login(account: Account, *, guard: Guard | None = None) -> None:
    """Log in to the incoming server and to SMTP; send nothing."""
    if account.incoming_protocol == "pop3":
        with pop3_session(account, guard):
            pass
    else:
        with imap_session(account, guard):
            pass
    with smtp_session(account, guard):
        pass


# --- polling --------------------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    """A new mail the poller found. `raw` is the headers only when the mail is
    larger than `MAX_MESSAGE_BYTES` (`complete` False)."""

    id: str
    folder: str | None
    raw: bytes
    size: int
    complete: bool
    fallback_delivery_id: str


def poll(
    account: Account,
    folder: str,
    cursor: Mapping[str, Any] | None,
    limit: int,
    guard: Guard | None = None,
) -> tuple[dict[str, Any], list[Fetched]]:
    """`(next_cursor, new mails)`, at most `limit`, oldest first. A cursor of
    None (or one that does not fit) only records where the mailbox stands.

    IMAP: `{"uidvalidity", "last_uid"}`; a changed UIDVALIDITY records a new
    position, since the old UIDs mean nothing any more. POP3: `{"seen":
    [uidl, ...]}`, the last 5,000. Mail is read with PEEK, never marked read."""
    if account.incoming_protocol == "pop3":
        return _poll_pop3(account, cursor, limit, guard)
    return _poll_imap(account, folder or "INBOX", cursor, limit, guard)


def _cursor_int(cursor: Mapping[str, Any] | None, key: str) -> int | None:
    value = cursor.get(key) if isinstance(cursor, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _poll_imap(
    account: Account,
    folder: str,
    cursor: Mapping[str, Any] | None,
    limit: int,
    guard: Guard | None,
) -> tuple[dict[str, Any], list[Fetched]]:
    with imap_session(account, guard) as conn:
        uidvalidity = _select(conn, folder)
        last_uid = _cursor_int(cursor, "last_uid")
        if last_uid is None or _cursor_int(cursor, "uidvalidity") != uidvalidity:
            return {"uidvalidity": uidvalidity, "last_uid": _highest_uid(conn)}, []
        data = _ok(*conn.uid("SEARCH", f"UID {last_uid + 1}:*"), "The server refused the search.")
        uids = sorted(u for u in {int(x) for x in b" ".join(data or [b""]).split()} if u > last_uid)
        mails: list[Fetched] = []
        for uid in uids[:limit]:
            size = _imap_size(conn, uid)
            complete = size <= MAX_MESSAGE_BYTES
            raw = _imap_raw(conn, uid, section="" if complete else "HEADER")
            mails.append(
                Fetched(
                    id=str(ImapId(folder, uidvalidity, uid)),
                    folder=folder,
                    raw=raw,
                    size=size,
                    complete=complete,
                    fallback_delivery_id=f"{uidvalidity}:{uid}",
                )
            )
        next_last = uids[:limit][-1] if uids else last_uid
        return {"uidvalidity": uidvalidity, "last_uid": next_last}, mails


def _highest_uid(conn: imaplib.IMAP4) -> int:
    data = _ok(*conn.uid("SEARCH", "ALL"), "The server refused the search.")
    uids = [int(x) for x in b" ".join(data or [b""]).split()]
    return max(uids, default=0)


def _poll_pop3(
    account: Account, cursor: Mapping[str, Any] | None, limit: int, guard: Guard | None
) -> tuple[dict[str, Any], list[Fetched]]:
    with pop3_session(account, guard) as conn:
        listing = _pop3_listing(conn)
        next_cursor, new = pop3_new_uidls([u for _, u, _ in listing], cursor, limit=limit)
        by_uidl = {u: (n, s) for n, u, s in listing}
        mails: list[Fetched] = []
        for uidl in new:
            number, size = by_uidl[uidl]
            complete = size <= MAX_MESSAGE_BYTES
            mails.append(
                Fetched(
                    id=uidl,
                    folder=None,
                    raw=_pop3_raw(conn, number, headers_only=not complete),
                    size=size,
                    complete=complete,
                    fallback_delivery_id=uidl,
                )
            )
        return next_cursor, mails


def mail_event(fetched: Fetched, account: Account) -> dict[str, Any] | None:
    """`{delivery_id, payload, summary}` for a new mail, or None when loop
    protection skips it."""
    msg = parse(fetched.raw)
    if skip_reason(msg, account):
        return None
    listed = attachments(msg) if fetched.complete else []
    message_id = _header(msg, "Message-ID")
    payload: dict[str, Any] = {
        "id": fetched.id,
        "folder": fetched.folder,
        "message_id": message_id,
        "from": _header(msg, "From"),
        "to": _header(msg, "To"),
        "cc": _header(msg, "Cc"),
        "date": _header(msg, "Date"),
        "subject": _header(msg, "Subject"),
        "attachments": listed,
    }
    return {
        "delivery_id": message_id or fetched.fallback_delivery_id,
        "payload": payload,
        "summary": render_summary(payload, msg, fetched),
    }


_SUMMARY_ATTACHMENTS = 50


def render_summary(payload: Mapping[str, Any], msg: EmailMessage, fetched: Fetched) -> str:
    """The plain text the agent's run receives for a new mail: the mail id
    the tools take, the headers, the text (truncated) and the attachments
    with their ids."""
    lines = [
        f"Mail id: {payload['id']} (pass it to mail_get, mail_reply, mail_forward, "
        "mail_move, mail_mark_read or mail_delete)",
    ]
    if payload["folder"]:
        lines.append(f"Folder: {payload['folder']}")
    for label, key in (
        ("From", "from"),
        ("To", "to"),
        ("Cc", "cc"),
        ("Date", "date"),
        ("Subject", "subject"),
        ("Message-ID", "message_id"),
    ):
        if payload[key]:
            lines.append(f"{label}: {payload[key]}")
    lines.append("")
    if fetched.complete:
        text, _ = truncate(body_text(msg))
        lines.append(text or "(no text)")
    else:
        lines.append(
            f"(This mail is {_size(fetched.size)}, too large to read here; "
            "only its headers are shown.)"
        )
    listed = payload["attachments"]
    if listed:
        lines += [
            "",
            "Attachments (fetch one with mail_get_attachment, passing the mail id and "
            "the attachment id):",
        ]
        for item in listed[:_SUMMARY_ATTACHMENTS]:
            size = _size(item["size"])
            lines.append(f"- attachment id {item['id']}: {item['name']} ({item['type']}, {size})")
        if len(listed) > _SUMMARY_ATTACHMENTS:
            lines.append(f"- ... and {len(listed) - _SUMMARY_ATTACHMENTS} more")
    return "\n".join(lines)
