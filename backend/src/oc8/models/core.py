"""Core tenancy, org structure, RBAC, models, and agents (tech-spec §6.2)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, SoftDeleteMixin, TenantMixin


class Organization(Base, PkMixin, TimestampMixin):
    """The tenant boundary (tech-spec §4.1). Not tenant-scoped itself."""

    __tablename__ = "organization"

    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(Text, nullable=False, default="standard")
    region: Mapped[str] = mapped_column(Text, nullable=False, default="eu")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Drives the first-run wizard redirect (design:
    #: gamified-onboarding-wizard-design.md). 'pending' at creation; flips to
    #: 'completed' when all three wizard steps are done, or 'skipped' if the
    #: admin dismisses it. Either terminal state stops the /welcome redirect
    #: permanently -- never re-derived from agent count, which would kick an
    #: admin out of their own wizard mid-flow (finishing step 2 alone would
    #: make agent count leave zero).
    onboarding_status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    #: Currently unused: this was a hook for an external identity provider's
    #: own login to require an additional oc8-owned TOTP step-up on top of it
    #: (standalone 2FA design). This edition ships only the built-in local
    #: password login, which already gates its own TOTP directly in
    #: `password_login` / `totp_gate` and never reads this flag. Left in
    #: place, defaulted false, rather than migrated away, since nothing
    #: currently depends on it either way.
    totp_step_up_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    __table_args__ = (
        CheckConstraint("tier IN ('standard','enterprise','onprem')", name="ck_org_tier"),
        CheckConstraint(
            "onboarding_status IN ('pending','completed','skipped')",
            name="ck_organization_onboarding_status",
        ),
    )


class Department(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "department"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    team_lead_agent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    frame: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: A snapshot of `frame` exactly as the department_template plugin declared
    #: it at instantiation time -- never touched again afterwards, even as
    #: `frame` itself is edited. This is the "capa_default" rung of the
    #: guardrails Source/provenance ladder (design: OC8 Guardrails UX spec
    #: §"Source"): a tool policy key still equal to its own entry here is
    #: still the plugin's default; one that differs was hand-edited for this
    #: department. `None` for a department created by hand (no template
    #: behind it) or one that predates this column -- Source then reports
    #: every key as "department" rather than misreporting a default that was
    #: never captured.
    frame_capa_defaults: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Display-only fields backing the office view (icon, okr, kpi, accent, ...).
    presentation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    prompt_caching_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Bumped by the database for every update (migration 0096, same trigger
    # migration 0054 already wired up for agent/plugin/integration); Copilot
    # uses it as an optimistic concurrency token for department.update/
    # delete/restore.
    config_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # The department the tenant's oc8 Assistant lives in -- auto-provisioned by
    # `agent.assistant.get_or_create_assistant`, never configured by anyone.
    # Mirrors `Agent.is_tenant_assistant`, and for the same reason: the two
    # scoped repositories (`departments/repo.py`, `agents/repo.py`) filter this
    # pair out of every user-facing list, and an explicit flag is cheaper and
    # clearer than joining back through `team_lead_agent_id` on every read.
    is_assistant_department: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class Role(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """A named bundle of permissions -- for a PERSON or for an AGENT, and `kind`
    is what keeps those two populations apart.

    The separation is not tidiness. `authz/pdp.py` resolves an agent's role row
    back through the CODE dict BY NAME, so a tenant that creates a role literally
    named `agent_default` and points an agent at it hands that agent every tool
    right there is. `kind` is the discriminator that lets the two resolvers ask
    different questions of one table -- the agent path taking only `'agent'`
    rows and the human path only `'human'` -- so that naming a row cleverly
    reaches nothing. The column and its CHECK land here; the two resolvers read
    it in the slice that adds them.

    `builtin=True` means the row is a shadow of `BUILTIN_ROLE_PERMISSIONS`: its
    grants come from CODE, so `role_permission` rows written against it are
    inert and a stray row cannot widen `operator`.

    Soft-deleted, and the `deleted_at` check every reader owes this table is
    load-bearing rather than belt-and-braces: `SoftDeleteMixin` adds no query
    filter, so a deleted role nobody checks is a deleted role that still grants.
    """

    __tablename__ = "role"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    builtin: Mapped[bool] = mapped_column(nullable=False, default=False)
    #: 'human' | 'agent'. Defaulted rather than required so every existing row
    #: and every existing constructor keeps working -- and `'human'` is the safe
    #: default of the two, because a row that defaults wrong grants an agent
    #: NOTHING instead of granting it everything.
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="human", server_default=text("'human'")
    )
    #: What the admin who wrote it says it is for. Shown next to the name
    #: everywhere the role is offered, because "Freigabe Vertrieb" tells the next
    #: administrator nothing about what got ticked.
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    #: `org_member.id` of whoever authored it. Nullable: a role written by the
    #: CLI or by provisioning has no member behind it, and inventing one would be
    #: a lie in the audit trail. Same rule as `OrgMemberDepartment.granted_by`.
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        CheckConstraint("kind IN ('human','agent')", name="ck_role_kind"),
        # The target of the composite foreign keys on `role_permission` and
        # `org_member`. `(tenant_id, id)` rather than `(id)` so a grant or an
        # assignment cannot reference another tenant's role even if application
        # code loses the plot -- referential integrity checks bypass RLS, so this
        # is the only place that cross-tenant reference can be refused.
        UniqueConstraint("tenant_id", "id", name="uq_role_tenant_id"),
        # UNCONDITIONAL, not partial on `deleted_at IS NULL`. Renaming a role is
        # refused, so delete-and-recreate is the sanctioned way to change a name
        # -- and a partial index would free the old name on delete, so the next
        # role to take it would silently inherit every mention of it in the audit
        # trail. The name is tombstoned along with the row.
        Index("uq_role_tenant_name", "tenant_id", text("lower(name)"), unique=True),
    )


class RolePermission(Base, PkMixin, TenantMixin, TimestampMixin):
    """One permission string granted to one tenant-defined role.

    Rows rather than a jsonb list on the role, per §6's rule -- relational for
    anything queried or governed -- and the whole string rather than
    `Permission`'s split tuple, because the string is what the gate compares.
    Splitting `"knowledge:view"` into two columns on write and recomposing it on
    read would be a second composition site beside `perm()`, and the two would
    eventually disagree about `tool:read`.

    A row here is a REQUEST for a right, not the right itself. Nothing trusted
    at write time is trusted again at read time: the resolver intersects these
    rows with `DELEGATABLE_PERMISSIONS` on every read, so a row arriving by
    restore, by psql or from a future importer is inert rather than dangerous --
    and a permission that stops being delegatable goes inert everywhere on the
    next request, with no backfill to remember.
    """

    __tablename__ = "role_permission"

    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    #: `resource:action`, exactly as `authz.permissions.perm()` spells it and
    #: exactly as a route declares it.
    permission: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        # One row per granted permission. The migration creates the composite FK
        # to `role (tenant_id, id)` ON DELETE CASCADE; it is not declared here
        # because this table is built by migration 0047 rather than by 0001's
        # `create_all`, which is also why every other id column in this schema is
        # a bare `Uuid`.
        UniqueConstraint("role_id", "permission", name="uq_role_permission"),
    )


class Permission(Base, PkMixin, TenantMixin, TimestampMixin):
    """§5.2's full grant tuple. UNUSED, and now unusable by halves.

    Tenant grants live in `role_permission`. This table keeps the shape §5.2
    describes -- a grant naming one object, under a constraint expression -- and
    nothing evaluates either of those two dimensions. The two CHECKs below are
    what turn that deferral from a doc comment into something Postgres refuses:
    a `resource_id` or a non-empty `constraint_expr` written by a future
    importer, a migration, or an optimistic afternoon cannot silently become a
    grant that no evaluator reads and every gate ignores.

    Dropping the two CHECKs is the whole of the migration that turns this on.
    """

    __tablename__ = "permission"

    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    constraint_expr: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint("resource_id IS NULL", name="ck_permission_no_object"),
        CheckConstraint("constraint_expr = '{}'::jsonb", name="ck_permission_no_constraint"),
    )


class ModelConfig(Base, PkMixin, TenantMixin, TimestampMixin):
    __tablename__ = "model_config"

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    locality: Mapped[str] = mapped_column(Text, nullable=False, default="cloud")
    # A specific Credential this ModelConfig uses, instead of always resolving
    # the tenant-wide "first credential of this provider's type" convention
    # (resolve_model_key/resolve_model_base_url in oc8.modelrouter.keys). NULL
    # keeps that convention as the fallback. Same nullable, unconstrained,
    # indexed shape as McpConnection.credential_id (migration 0069) -- lets a
    # tenant bind multiple accounts of one provider (e.g. two OpenAI keys),
    # one per ModelConfig.
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    fallbacks: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    cost_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Display helpers for the models screen.
    display_name: Mapped[str | None] = mapped_column(Text)
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Exactly one ModelConfig per tenant may carry this flag -- enforced in
    #: the create/update service (catalog.py), not by a DB constraint, because
    #: enforcing it here would need a partial unique index scoped to
    #: `tenant_id` that most of this schema's other flags don't use either.
    used_by_copilot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (CheckConstraint("locality IN ('cloud','local')", name="ck_model_locality"),)


class Agent(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "agent"

    department_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    role_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mission: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Bumped by the database for every update; Copilot uses it as an optimistic
    # concurrency token rather than relying on transaction-scoped timestamps.
    config_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    model_config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    narrowing: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Backend-only provenance tracking (agent tool login selection design,
    # Task 5 fix round 2) -- which tool keys inside `narrowing["tools"]` an
    # OPERATOR has explicitly set via `PUT /agents/{id}/narrowing`
    # (agents_write.py's `set_narrowing`, the ONLY writer of this column).
    # Deliberately separate from `narrowing` itself: `narrowing["tools"][key]`
    # merely being present is NOT a reliable "operator touched this" signal,
    # since `set_department_tools`'s cascade (departments.py) also writes
    # into that same JSON for agents who never touched it. This column tracks
    # WHO wrote a value, not WHAT the value is or whether it happens to equal
    # some department default -- the cascade must never add to this set; it
    # only ever reads it to decide whether a key is safe to overwrite.
    narrowing_overridden_keys: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    is_team_lead: Mapped[bool] = mapped_column(nullable=False, default=False)
    # The ONE agent per tenant that is the unified oc8 Assistant (chat +
    # Telegram entry point). Every is_tenant_assistant agent is also
    # is_team_lead; the reverse is not -- this flag is what control_tools.py's
    # _delegate() checks to allow CROSS-department delegation, which an
    # ordinary team lead must never get.
    is_tenant_assistant: Mapped[bool] = mapped_column(nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="stopped")
    # Why the agent is paused, so a resume clears only the right kind (§15.4 A2):
    # "budget" (scope budget hard-stop) vs "supervision" (drift escalation). NULL
    # when not paused.
    pause_reason: Mapped[str | None] = mapped_column(Text)
    paused_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    runtime_ref: Mapped[str | None] = mapped_column(Text)
    trust_level: Mapped[str] = mapped_column(Text, nullable=False, default="first_party")
    # Display helpers backing the office/agent screens.
    presentation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','idle','waiting_for_approval','paused',"
            "'error','stopped','pending_approval')",
            name="ck_agent_status",
        ),
        CheckConstraint(
            "trust_level IN ('first_party','verified','community')",
            name="ck_agent_trust",
        ),
        # One tenant Assistant per tenant, and the database is the one that
        # says so: `get_or_create_assistant` is check-then-insert with five
        # concurrent entrypoints, so without this two tabs on a fresh tenant
        # each create an Assistant (and a department) and later lookups answer
        # with an arbitrary one. Partial on `deleted_at IS NULL` so an archived
        # Assistant does not block provisioning its replacement. Mirrored by
        # migration 0082 for databases that predate it.
        Index(
            "uq_agent_tenant_assistant",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_tenant_assistant AND deleted_at IS NULL"),
        ),
    )
