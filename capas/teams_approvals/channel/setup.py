"""Setup-time proof that a submitted app id and app password actually work
together (§13 setup_validate). Called once, when an admin submits the
credential form -- before either value is ever handed to `channel.build()`.
A wrong registration found here costs one rejected form; found in
production it costs an approval that silently never reaches anyone.
"""

from __future__ import annotations

import httpx

from oc8.credentials.errors import CredentialCheckFailed

from .channel import mint_access_token


async def validate(values: dict[str, str]) -> None:
    # Fixed wording only: Microsoft's error text and transport errors are
    # not ours to show (they are logged by the credential service).
    app_id = values.get("app_id", "")
    app_password = values.get("app_password", "")
    if not app_id:
        raise CredentialCheckFailed("no app id submitted", reason="invalid")
    if not app_password:
        raise CredentialCheckFailed("no app password submitted", reason="invalid")
    try:
        await mint_access_token(
            app_id=app_id, app_password=app_password, tenant=values.get("tenant_id", "")
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise CredentialCheckFailed(
            f"Microsoft rejected this app registration (HTTP {status})",
            reason="auth" if status in (400, 401, 403) else "invalid",
        ) from exc
    except httpx.HTTPError as exc:
        raise CredentialCheckFailed("Could not reach Microsoft", reason="unreachable") from exc
