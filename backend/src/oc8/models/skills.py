"""Skills: reusable capabilities and their assignments (tech-spec §6.5)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, LargeBinary, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, SoftDeleteMixin, TenantMixin


class Skill(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "skill"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    author: Mapped[str] = mapped_column(Text, nullable=False, default="")
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="local")
    trust_level: Mapped[str] = mapped_column(Text, nullable=False, default="first_party")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: The version holding the content as it was shipped (by a capa, or
    #: imported from a store) -- what "reset to shipped version" restores, and
    #: what a shipped update compares against. None for a local skill.
    shipped_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    #: The shipped description and category. An admin may change both; that
    #: customises the skill like a content edit, and a reset restores them.
    shipped_description: Mapped[str | None] = mapped_column(Text)
    shipped_category: Mapped[str | None] = mapped_column(Text)
    #: When an admin first changed a shipped skill's content in place. A
    #: customised skill keeps its content when a newer version ships.
    customized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: When a newer shipped version arrived for a customised skill and was kept
    #: aside (in `shipped_version_id`) instead of replacing the edit.
    shipped_update_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "trust_level IN ('first_party','verified','community')",
            name="ck_skill_trust",
        ),
        CheckConstraint("origin IN ('local','store')", name="ck_skill_origin"),
    )


class SkillVersion(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "skill_version"

    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    semver: Mapped[str] = mapped_column(Text, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    artifact_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (UniqueConstraint("skill_id", "semver", name="uq_skill_version"),)


class ImportedSkillFile(Base, PkMixin, TenantMixin, TimestampMixin):
    """One `references/`/`assets/`/`scripts/` file bundled with a skill that was
    imported directly from a repository (skills.importer), pinned to the exact
    `SkillVersion` it arrived with. A capa-installed skill's files live on disk
    instead and never get a row here -- see `reference_root`'s two shapes,
    documented on `agent.control_tools`'s `read_reference_file` dispatch."""

    __tablename__ = "imported_skill_file"

    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    rel_path: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "skill_version_id", "rel_path", name="uq_imported_skill_file_version_path"
        ),
    )


class SkillAssignment(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """Who may load a skill. The scope is which column is filled:

        agent_id set        -> this agent
        department_id set   -> every agent in that department
        neither             -> every agent of the tenant

    A skill could once be given to a single agent only, so a standard set for a
    company meant one assignment per skill per agent, redone by hand whenever
    somebody was hired.
    """

    __tablename__ = "skill_assignment"

    agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        # A row naming BOTH would read as "this agent" and "everyone here" at
        # once. Uniqueness is three partial indexes (migration 0040), because a
        # single UNIQUE over nullable columns accepts the same tenant-wide row
        # any number of times -- NULLs are distinct in Postgres.
        CheckConstraint(
            "agent_id IS NULL OR department_id IS NULL", name="ck_skill_assignment_scope"
        ),
    )
