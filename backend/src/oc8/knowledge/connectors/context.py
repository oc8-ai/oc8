"""What a connector is handed to authenticate: `SourceAuthContext`.

A connector never sees the token store, the secret store, a refresh token, or an
expiry. It asks for what it needs and gets a usable value:

* ``token()`` -- a fresh OAuth access token, refresh already handled, for sources
  backed by an ``oauth_connection``.
* ``secret(ref)`` -- a value from this tenant's secret store, by reference. This
  is how a connector reaches credentials that are not OAuth (an S3 access key,
  say). The *reference* lives in the source config; the *value* never does.

Both are tenant-scoped by construction: the context is built from the request's
tenant and cannot reach another tenant's tokens or secrets.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from oc8.credentials.service import (
    CredentialFieldNotSet,
    CredentialNotFound,
    resolve_credential_field,
)
from oc8.knowledge.connectors.base import ConnectorError
from oc8.oauth.tokens import get_access_token
from oc8.secrets.service import SecretNotFound, resolve_secret


class SourceAuthContext:
    def __init__(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID | None = None,
    ) -> None:
        self._db = db
        self._tenant_id = tenant_id
        self._connection_id = connection_id

    async def token(self) -> str:
        if self._connection_id is None:
            raise ConnectorError(
                "this source has no connected account — connect one and try again"
            )
        return await get_access_token(
            self._db, tenant_id=self._tenant_id, connection_id=self._connection_id
        )

    async def secret(self, ref: str) -> str:
        """A stored credential by reference. A missing reference is a config
        error the operator can act on, not an internal failure."""
        try:
            return await resolve_secret(self._db, tenant_id=self._tenant_id, ref=ref)
        except SecretNotFound:
            raise ConnectorError(
                f"no stored secret named {ref!r} — add it under Settings → Secrets"
            ) from None

    async def credential(self, credential_id: str, field_key: str) -> str:
        """A field of a named, reusable `Credential` -- the unified
        credentials framework's replacement for a raw `secret(ref)` lookup.
        `credential_id` is tenant-scoped and validated as a real UUID here,
        never trusted as-is from connector config."""
        try:
            parsed = uuid.UUID(credential_id)
        except ValueError:
            raise ConnectorError(f"invalid credential id: {credential_id!r}") from None
        try:
            return await resolve_credential_field(
                self._db, tenant_id=self._tenant_id, credential_id=parsed, field_key=field_key
            )
        except CredentialNotFound:
            raise ConnectorError(f"no stored credential with id {credential_id!r}") from None
        except CredentialFieldNotSet as exc:
            raise ConnectorError(
                f"credential {exc.credential_name!r} has no value set for field "
                f"{field_key!r} — edit it under Settings → Credentials"
            ) from None
