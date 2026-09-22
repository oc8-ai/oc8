"""Per-member API keys, authenticating the outward MCP gateway.

A key is a personal credential a member issues for themself (self-service, not
tenant-wide `settings` admin config -- see `api/v1/totp.py` for the same
shape). It carries no scopes and no separate permission set of its own: at
call time the outward MCP route resolves the OWNING member's real, live
permissions the same way a session token would, so a key is exactly as
powerful as the person who created it, no more -- disabling their access
disables every key they hold too.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, DateTime, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class ApiKey(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "api_key"

    #: Owning member. Not unique -- a member may hold several keys.
    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: sha256(token), hex-encoded. The full token is shown once at creation
    #: and never stored -- this column exists only to look it back up.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    #: First few characters after the `oc8_ak_` prefix, kept for display
    #: ("oc8_ak_4f2a...") since the full secret can never be shown again.
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Origins allowed to use this key (checked against the request's Origin
    #: header). Empty list = unrestricted.
    allowed_origins: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set by the member who creates the key, not a tenant-wide policy. NULL
    #: means the key never expires.
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
