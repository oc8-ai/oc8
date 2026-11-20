"""Thin async Google API client for the MCP bridge process.

Deliberately NOT reusing `oc8.oauth`'s token-minting: this module runs in a
SEPARATE process (the stdio bridge, launched by `oc8.agent.mcp_env` -- see
plugin.toml's `command`/`args`), with no database session. The core process
mints tokens (both the self-identity `"oauth:"` token and, per delegated
mailbox, an `"oauth-delegated:"` token -- see mcp_env.py) BEFORE launching this
bridge and passes them via env vars. A token bound this way is valid for the
lifetime of one bridge process, which matches an access token's own ~1 hour
lifetime closely enough that no in-bridge refresh logic is needed -- same
discipline microsoft365's own mcp_bridge/graph.py already established.

Two identities exist in this bridge's environment:

* the service account's OWN identity (`GOOGLE_TOKEN`) -- Drive/Docs/Sheets/
  Slides, none of which need domain-wide delegation (Shared Drive membership
  is enough);
* zero or more DELEGATED mailboxes (`GOOGLE_DELEGATED_MAILBOXES`, a
  comma-joined list, positionally paired with `GOOGLE_DELEGATED_TOKEN_0`,
  `_1`, ...) -- Gmail/Calendar, which need a `sub`-claimed token per mailbox.
  `resolve_mailbox_token` is how Gmail/Calendar tools reach the right one.

With `OC8_OAUTH_GRANT=authorization_code` the self token IS one person's
account: Gmail and Calendar act as `OC8_OAUTH_ACCOUNT` only.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

API_GMAIL = "https://gmail.googleapis.com/gmail/v1"
API_CALENDAR = "https://www.googleapis.com/calendar/v3"
API_DRIVE = "https://www.googleapis.com/drive/v3"
API_DOCS = "https://docs.googleapis.com/v1"
API_SHEETS = "https://sheets.googleapis.com/v4"
API_SLIDES = "https://slides.googleapis.com/v1"


def get_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


def _delegated_mailboxes() -> list[str]:
    raw = os.environ.get("GOOGLE_DELEGATED_MAILBOXES", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


#: Set by the core when GOOGLE_TOKEN is one person's own sign-in.
GRANT_ENV = "OC8_OAUTH_GRANT"
ACCOUNT_ENV = "OC8_OAUTH_ACCOUNT"


def _personal_account() -> str | None:
    if os.environ.get(GRANT_ENV, "") != "authorization_code":
        return None
    return os.environ.get(ACCOUNT_ENV, "").strip() or None


def resolve_mailbox_address(mailbox: str | None) -> str:
    """The actual mailbox address a call should act as: `mailbox` if given,
    else this connection's configured `GOOGLE_DEFAULT_MAILBOX`. Casefolds both
    sides of the authorization check (email local-parts are case-insensitive
    in practice, and Google treats them so) but returns the ORIGINAL casing,
    never the casefolded form -- Gmail/Calendar URLs and attendee-matching both
    want the real address, not a normalized one."""
    own = _personal_account()
    if own is not None:
        # A person's own connection acts as that one account, never another.
        if mailbox and mailbox.casefold() != own.casefold():
            raise RuntimeError(f"this connection acts as {own} only; omit mailbox or pass {own}")
        return own
    target = mailbox or os.environ.get("GOOGLE_DEFAULT_MAILBOX", "")
    if not target:
        raise RuntimeError(
            "no mailbox given and no default_mailbox configured for this connection -- "
            "pass mailbox, or set a default mailbox in the plugin's setup form"
        )
    mailboxes = _delegated_mailboxes()
    folded = {m.casefold(): m for m in mailboxes}
    if target.casefold() not in folded:
        raise RuntimeError(
            f"mailbox {target!r} is not one of this connection's authorized delegated "
            f"mailboxes ({', '.join(mailboxes) or 'none configured'})"
        )
    return target


def resolve_mailbox_token(mailbox: str | None) -> str:
    """The bearer token for `resolve_mailbox_address(mailbox)`."""
    if _personal_account() is not None:
        resolve_mailbox_address(mailbox)
        return self_token()
    target = resolve_mailbox_address(mailbox)
    mailboxes = _delegated_mailboxes()
    folded = {m.casefold(): i for i, m in enumerate(mailboxes)}
    index = folded[target.casefold()]
    token = os.environ.get(f"GOOGLE_DELEGATED_TOKEN_{index}", "")
    if not token:
        raise RuntimeError(f"no token available for mailbox {target!r}")
    return token


def self_token() -> str:
    token = os.environ.get("GOOGLE_TOKEN", "")
    if not token:
        raise RuntimeError("GOOGLE_TOKEN is not set")
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _unwrap(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Google API error (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    if not resp.content:
        return {}
    body: dict[str, Any] = resp.json()
    return body


async def get_json(
    api: str, path: str, *, token: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    async with get_client() as client:
        resp = await client.get(f"{api}{path}", headers=_headers(token), params=params)
    return _unwrap(resp)


async def post_json(
    api: str,
    path: str,
    *,
    token: str,
    json: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with get_client() as client:
        resp = await client.post(
            f"{api}{path}", headers=_headers(token), json=json, params=params
        )
    return _unwrap(resp)


async def patch_json(
    api: str,
    path: str,
    *,
    token: str,
    json: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    async with get_client() as client:
        resp = await client.patch(
            f"{api}{path}", headers=_headers(token), json=json, params=params
        )
    return _unwrap(resp)


async def delete(
    api: str, path: str, *, token: str, params: dict[str, Any] | None = None
) -> None:
    async with get_client() as client:
        resp = await client.delete(
            f"{api}{path}", headers=_headers(token), params=params
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Google API error (HTTP {resp.status_code}): {resp.text[:200]}"
        )


async def get_bytes(
    api: str, path: str, *, token: str, params: dict[str, Any] | None = None
) -> bytes:
    """Drive's `alt=media` (and the export endpoint) return the bytes/text
    directly with a plain 200 -- confirmed against `gdrive_source`'s own
    production use of this exact call, no redirect (unlike Microsoft Graph's
    `/content`, see design doc §9). No `follow_redirects` needed here."""
    async with get_client() as client:
        resp = await client.get(f"{api}{path}", headers=_headers(token), params=params)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Google API error (HTTP {resp.status_code}): {resp.text[:200]}"
        )
    return resp.content
