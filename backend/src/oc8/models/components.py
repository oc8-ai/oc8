"""ComponentGrant: which agent or department may render which governed
component (see docs/superpowers/plans/2026-08-21-governed-components.md).

No longer consulted: every catalogue component is available to every agent
(components are presentation only). The table is kept so existing rows and
the migration history stay intact.

Deliberately the same department/agent grant shape as KnowledgeGrant
(oc8.models.knowledge) -- "may this agent use X" is the same question whether
X is a knowledge base or a UI layout, and oc8.knowledge.retrieval already
answers it with one IN clause over (agent.id, agent.department_id).
`component_key` names a fixed catalogue entry (oc8.agent.components.
COMPONENT_CATALOG), not a tenant-created row, which is the only reason this
is a separate table rather than another column on knowledge_grant.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, TenantMixin


class ComponentGrant(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "component_grant"

    component_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    grantee_type: Mapped[str] = mapped_column(Text, nullable=False)
    grantee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "grantee_type IN ('department','agent')", name="ck_component_grant_grantee"
        ),
        UniqueConstraint(
            "tenant_id",
            "component_key",
            "grantee_type",
            "grantee_id",
            name="uq_component_grant",
        ),
    )
