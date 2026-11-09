"""Third-party OAuth connections — one row per connected account (tech-spec §11.2).

Token material is NOT stored here. `access_secret_ref` / `refresh_secret_ref`
name rows in the §12.3 `secret` table; `expires_at` is not secret and stays a
plaintext column so expiry can be checked without a decrypt.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class OAuthConnection(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "oauth_connection"

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    account_label: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    access_secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_secret_ref: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    # Which credentials minted this token. A later refresh MUST reuse the same
    # ones -- re-running the tenant->platform precedence chain would break every
    # existing connection the moment a tenant adds its own app.
    client_source: Mapped[str] = mapped_column(Text, nullable=False)
    # Which credential shape minted this connection's tokens. Every row before
    # this column existed used the 3-legged authorization_code dance, so that
    # stays the default -- a client_credentials row (Microsoft app-only Graph
    # auth) or a service_account row (Google's JWT-bearer assertion grant) is
    # always created with this set explicitly. See get_access_token's grant_type
    # branch (oauth/tokens.py) for what each value actually does at mint time.
    grant_type: Mapped[str] = mapped_column(Text, nullable=False, default="authorization_code")
    # Only set for provider="microsoft": Microsoft's own token endpoint is
    # tenant-scoped by URL (https://login.microsoftonline.com/{tenant}/...), so
    # this is not just descriptive -- _post_token needs it to build the URL.
    azure_tenant_id: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: Whose account this is: NULL for the organisation (every row before
    #: migration 0107, and every admin-created one), otherwise the one member
    #: it belongs to. A personal connection is used only for that member's own
    #: runs, or by an agent explicitly bound to it (`agent_tool_access`).
    owner_member_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    # Small, non-secret, provider-defined facts a connection's own flow needs
    # to remember (design: ChatGPT subscription auth §2's chatgpt_account_id).
    # Generic and provider-agnostic on purpose -- see migration 0076.
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "owner_member_id",
            "account_label",
            name="uq_oauth_conn_tenant_provider_owner_account",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "status IN ('active','needs_reauth','revoked')", name="ck_oauth_conn_status"
        ),
        CheckConstraint(
            "client_source IN ('tenant','platform')", name="ck_oauth_conn_client_source"
        ),
        CheckConstraint(
            "grant_type IN "
            "('authorization_code','client_credentials','service_account','device_code')",
            name="ck_oauth_conn_grant_type",
        ),
    )
