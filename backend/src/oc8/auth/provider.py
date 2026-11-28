"""Identity providers.

The control plane depends only on the ``IdentityProvider`` interface. Community
ships exactly one implementation: the dev provider, which mints/verifies its
own HS256 JWTs for the built-in password/local login flow.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any, Protocol, cast

import jwt

from oc8.auth.principal import Principal, PrincipalKind
from oc8.config import get_settings


class IdentityProvider(Protocol):
    def mint(
        self,
        *,
        tenant_id: uuid.UUID,
        subject: str,
        role: str,
        kind: PrincipalKind = "operator",
        scopes: list[str] | None = None,
    ) -> str: ...

    def verify(self, token: str) -> Principal: ...

    def logout_url(self) -> str | None:
        """Optional: an external SSO endpoint to redirect to on logout.

        Returns None if the provider has no external session to terminate.
        """
        ...


class InvalidToken(Exception):
    pass


def _email_claim(data: Mapping[str, Any]) -> str | None:
    value = data.get("email")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _principal_from_claims(data: Mapping[str, Any]) -> Principal:
    try:
        return Principal(
            subject=cast(str, data["sub"]),
            tenant_id=uuid.UUID(cast(str, data["tenant_id"])),
            role=cast(str, data["role"]),
            kind=cast(PrincipalKind, data.get("kind", "operator")),
            scopes=cast(list[str], data.get("scopes", [])),
            email=_email_claim(data),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidToken("malformed claims") from exc


class DevIdentityProvider:
    """Signs short-lived HS256 tokens carrying the tenant claim."""

    def mint(
        self,
        *,
        tenant_id: uuid.UUID,
        subject: str,
        role: str,
        kind: PrincipalKind = "operator",
        scopes: list[str] | None = None,
        auth_time: int | None = None,
        password_fp: str | None = None,
    ) -> str:
        """`auth_time` is when the person signed in: now for a fresh sign-in,
        carried over unchanged by a renewal, which is what caps the chain."""
        settings = get_settings()
        now = dt.datetime.now(tz=dt.UTC)
        payload = {
            "sub": subject,
            "tenant_id": str(tenant_id),
            "role": role,
            "kind": kind,
            "scopes": scopes or [],
            "iat": int(now.timestamp()),
            "auth_time": auth_time if auth_time is not None else int(now.timestamp()),
            **({"pwd": password_fp} if password_fp else {}),
            "exp": int((now + dt.timedelta(seconds=settings.jwt_ttl_seconds)).timestamp()),
        }
        return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_alg)

    def verify(self, token: str) -> Principal:
        settings = get_settings()
        try:
            data = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        return _principal_from_claims(data)

    def logout_url(self) -> str | None:
        """Community has no external SSO session to terminate."""
        return None


_dev_provider: IdentityProvider = DevIdentityProvider()


def mint_password_session(
    *,
    tenant_id: uuid.UUID,
    subject: str,
    role: str,
    password_hash: str | None,
    auth_time: int | None = None,
) -> str:
    """A full operator session for a password sign-in.

    With the Community provider it names the member's password hash (see
    `password_fingerprint`), which `POST /auth/session/renew` requires; an
    edition's own provider mints its plain token and renews its own way.
    """
    from oc8.auth.password import password_fingerprint

    provider = get_identity_provider()
    if isinstance(provider, DevIdentityProvider):
        return provider.mint(
            tenant_id=tenant_id,
            subject=subject,
            role=role,
            auth_time=auth_time,
            password_fp=password_fingerprint(password_hash) if password_hash else None,
        )
    return provider.mint(tenant_id=tenant_id, subject=subject, role=role)


def get_identity_provider() -> IdentityProvider:
    """Community ships exactly one identity provider: the dev/password one."""
    return _dev_provider


def set_identity_provider(provider: IdentityProvider) -> None:
    global _dev_provider
    _dev_provider = provider
