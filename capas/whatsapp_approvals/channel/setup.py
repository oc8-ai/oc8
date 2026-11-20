"""Setup-time proof that a submitted access token and phone number actually work
together (§13 setup_validate). Called once, when an admin submits the setup
form -- before either value is ever handed to `channel.build()`.
"""

from __future__ import annotations

import httpx

from oc8.credentials.errors import CredentialCheckFailed

from .channel import API


async def validate(values: dict[str, str]) -> None:
    # Fixed wording only: Meta's error text and transport errors are not
    # ours to show (they are logged by the credential service).
    token = values.get("access_token", "")
    phone_number_id = values.get("phone_number_id", "")
    if not token:
        raise CredentialCheckFailed("no access token submitted", reason="invalid")
    if not phone_number_id:
        raise CredentialCheckFailed("no phone number id submitted", reason="invalid")
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(
                f"{API}/{phone_number_id}",
                params={"fields": "verified_name"},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise CredentialCheckFailed("Could not reach WhatsApp", reason="unreachable") from exc
    if response.status_code != 200:
        status = response.status_code
        raise CredentialCheckFailed(
            f"WhatsApp rejected this token or phone number id (HTTP {status})",
            reason="auth" if status in (401, 403) else "invalid",
        )
