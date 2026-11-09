"""Setup-time proof that a submitted bot token actually works (§13 setup_validate).

Called once, when an admin submits the setup form -- before the token is ever
handed to `channel.build()`. A wrong token found here costs one rejected form;
found in production it costs an approval that silently never reaches anyone.
"""

from __future__ import annotations

import httpx

from oc8.credentials.errors import CredentialCheckFailed

from .channel import API


async def validate(values: dict[str, str]) -> None:
    # Fixed wording only: a transport error carries the request URL, which
    # holds the token, and Telegram's own text is not ours to show.
    token = values.get("bot_token", "")
    if not token:
        raise CredentialCheckFailed("no bot token submitted", reason="invalid")
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{API}/bot{token}/getMe")
        except httpx.HTTPError as exc:
            raise CredentialCheckFailed("Could not reach Telegram", reason="unreachable") from exc
    try:
        ok = response.json().get("ok")
    except ValueError:
        ok = False
    if response.status_code != 200 or not ok:
        raise CredentialCheckFailed("Telegram rejected this bot token", reason="auth")
