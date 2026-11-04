"""The people a tenant knows, and where each of them stands (§1).

Until now a human was five claims in a JWT and nothing else. There was no row
for a person anywhere in the schema, so the sentence "his department, and not
the whole company's" had no term it could be true in: `GET /approvals` filtered
on status alone and handed back every pending approval in the tenant.

Two tables, and the split matters. `org_member` is WHO -- one row per human the
tenant has seen, minted from the token's `sub`. `org_member_department` is a
SEAT: this person, this department, this seat role. A person's authority is the
union of their seats, read live from these rows on every request.

**Nothing here is in the token.** No new claim, no change to `auth/principal.py`.
A seat granted or revoked takes effect on the caller's next request rather than
on their next token refresh -- non-negotiable for authority that releases money,
and the reason `POST /members/.../departments/...` does not need to talk to
an external identity provider at all.

**Revoked, not deleted.** "Who could approve this, and until when" is the first
question an audit asks; a DELETE answers it with silence. Same reason
`ApprovalChannelBinding.revoked_at` exists.

**No `org_role` mirror column.** A denormalised copy of the token's role would be
two sources of truth for one fact, written on every request and consulted by one
caller -- stale in exactly the direction that costs money.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from oc8.db.base import Base, TimestampMixin
from oc8.models._mixins import PkMixin, SoftDeleteMixin, TenantMixin


class OrgMember(Base, PkMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """A human this tenant knows about. The IdP owns the login; this owns WHERE
    they stand."""

    __tablename__ = "org_member"

    #: The token's `sub`, verbatim and un-normalised. Whatever the IdP sends is
    #: what identifies the person, because anything else is a guess we would have
    #: to keep guessing the same way for ever.
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    #: The same person as a uuid, derived exactly as `api/v1/channels.py`'s
    #: `_subject_id` already derives it (`uuid5(NAMESPACE_URL, f"oc8:subject:{sub}")`,
    #: or the subject itself when it already parses as a uuid). It earns its own
    #: column because the messenger door has no token at all: an inbound Telegram
    #: message arrives carrying only `ApprovalChannelBinding.user_id`, and that
    #: value is this one. Without it, the person behind a binding is unresolvable
    #: and the channel path cannot be scoped.
    subject_uuid: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    display_name: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    #: The address this person signs in with, for display only. Copied from the
    #: token's `email` claim whenever it is present and differs; for local
    #: password sign-in it is the subject itself. NEVER an identity: nothing
    #: looks a person up by it, because an IdP lets people change it and two
    #: subjects can report the same address.
    email: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    #: "Sees and decides in every department, including ones created after this
    #: row was written." A boolean rather than a set holding every department id:
    #: a set has to be remembered every time a department is created, and the one
    #: time it is forgotten a CEO silently stops seeing a department's approvals.
    #:
    #: It is also the ONLY unrestricted term available on the messenger door,
    #: which has no token to carry `approval:decide_any`. It is written at an
    #: authenticated moment (`POST /channels/{channel}/link`), audited, and
    #: revocable -- never inferred from a phone number.
    all_departments: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: The tenant-defined role this person has been given, if an administrator
    #: has given them one. `role.id`, with a composite FK on `(tenant_id,
    #: role_id)` and `ON DELETE RESTRICT` -- deleting a role somebody holds must
    #: be a 409 that names the holders, never a silent demotion of forty people.
    #:
    #: **NULL is not "no permissions". NULL is "the token decides", which is what
    #: happens today.** This column is an OVERRIDE, not a union term: no member
    #: row and a NULL here resolve identically, to `permissions_for(token.role)`,
    #: byte for byte. A union could not demote anybody -- every human on every
    #: live tenant is minted `org_admin` -- so assigning "Praktikant" would have
    #: been a no-op that the screen reported as success.
    #:
    #: It does not contradict this module's rejection of an `org_role` mirror
    #: column: that would have been a per-request copy of an IdP fact, written on
    #: a read path and stale in the direction that costs money. This is a foreign
    #: key an administrator writes deliberately, and nothing writes it on a read.
    role_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    #: Argon2id password hash for local password authentication (Community's
    #: only identity provider). NULL means this member has no password set yet
    #: (e.g., a member minted from a dev-login token, or one invited but not
    #: yet given a password by an administrator) and so cannot use
    #: `POST /auth/login` until one is set. Only the initial admin (created via
    #: /auth/setup) has this set automatically.
    #: Never log or expose this value. Password verification uses constant-time comparison.
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    #: When this member became a mandatory-2FA org_admin with no
    #: TotpCredential yet -- the grace-period clock (standalone 2FA design).
    #: Set by `roles/service.py`'s `assign_role` and `api/v1/auth.py`'s
    #: `password_setup`; cleared if the member is demoted away from
    #: org_admin without ever enrolling. NULL for anyone who has enrolled,
    #: was never mandatory, or has never held org_admin.
    totp_grace_started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Both partial: a soft-deleted member must not block re-enrolling the
        # same person, and `deleted_at IS NULL` is what makes the row that
        # matters the only one the uniqueness applies to.
        #
        # Two indexes and not one, because both columns are looked up: `subject`
        # by the HTTP door (the token has the string), `subject_uuid` by the
        # messenger door (the binding has the uuid). A collision on either one
        # would mean two rows claiming the same person, and the resolver would
        # pick whichever the planner returned first.
        Index(
            "uq_org_member_subject",
            "tenant_id",
            "subject",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_org_member_subject_uuid",
            "tenant_id",
            "subject_uuid",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class OrgMemberDepartment(Base, PkMixin, TenantMixin, TimestampMixin):
    """A seat: this person holds this seat-role IN this department."""

    __tablename__ = "org_member_department"

    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    department_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    #: 'dept_viewer' | 'dept_approver' -- and nothing else, enforced by the CHECK
    #: below and by migration 0046. Deliberately NOT a built-in role name: the
    #: closed vocabulary in `authz/permissions.SEAT_PERMISSIONS` is what stops a
    #: seat from carrying nine tenant-wide `:manage` grants, which is the flaw
    #: that killed both source designs.
    seat_role: Mapped[str] = mapped_column(Text, nullable=False)
    #: `org_member.id` of whoever granted it. Nullable because a seat written by
    #: the CLI has no member behind it, and inventing one would be a lie in the
    #: audit trail.
    granted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: WRITE authority over Agent, in this ONE department. Independent of
    #: seat_role -- a dept_viewer may hold it, a dept_approver may not. Never a
    #: resource:action string: not in SEAT_PERMISSIONS, not in
    #: DEPARTMENT_SCOPABLE, not in DELEGATABLE_PERMISSIONS. Exists only as a
    #: column on a seat row.
    agent_manage: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )

    __table_args__ = (
        CheckConstraint(
            "seat_role IN ('dept_viewer','dept_approver')",
            name="ck_org_member_department_seat_role",
        ),
        # One live seat per person per department. Partial on `revoked_at IS
        # NULL`, so the history of seats held and taken away stays queryable --
        # a plain unique constraint would force the revoke path to DELETE, which
        # is the audit answer this table exists to avoid.
        #
        # No `tenant_id` in the key, and that is safe rather than an oversight:
        # `member_id` is a uuidv7 primary key, so it belongs to exactly one
        # tenant already. Adding tenant_id would widen the key without
        # forbidding anything.
        Index(
            "uq_org_member_department_live",
            "member_id",
            "department_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class TotpCredential(Base, PkMixin, TenantMixin, TimestampMixin):
    """One member's TOTP enrollment (standalone 2FA design). A separate table
    from `org_member`, not columns on it, so the secret ref and backup-code
    hashes stay out of the row every ordinary member query already touches.

    `secret_ref` names a row in the existing secret vault
    (`oc8.secrets.service.store_secret`/`resolve_secret`, `m.Secret`) -- the
    TOTP shared secret itself is never stored here, the same indirection
    `Credential.secret_refs` already uses for its own secret material.

    `enrolled_at` stays NULL until the first real code is confirmed
    (`POST /auth/totp/confirm`) -- an abandoned enrollment (a secret was
    generated and shown, but never confirmed) must never count as enrolled.
    """

    __tablename__ = "totp_credential"

    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    secret_ref: Mapped[str] = mapped_column(Text, nullable=False)
    enrolled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: List of {"hash": <argon2id>, "used_at": <iso datetime | None>}. Each
    #: hash is one-way (oc8.auth.password.hash_password) -- never needs
    #: decrypting back to plaintext, so no encrypted column type is used
    #: here at all, unlike secret_ref above.
    backup_codes: Mapped[list[dict[str, str | None]]] = mapped_column(
        JSONB, nullable=False, default=list
    )


class MemberDashboardLayout(Base, PkMixin, TenantMixin, TimestampMixin):
    """One member's own "My Work" grid arrangement -- widget positions,
    sizes, and per-widget config. Strictly personal: no sharing, no
    tenant-wide default, never read by anyone but the member who owns it
    (enforced entirely by scoping every query to `current_member.id`, the
    same ownership pattern `notifications.py`'s push subscriptions use --
    see that module's docstring).

    No `SoftDeleteMixin`: a layout carries no audit/compliance value once
    replaced, unlike an approval or a TOTP credential.
    """

    __tablename__ = "member_dashboard_layout"

    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    widgets: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    template_id: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)


class DashboardPreset(Base, PkMixin, TenantMixin, TimestampMixin):
    """A member-saved "My Work" arrangement, offered back in `TemplatePicker`
    alongside the built-in templates -- unlike `MemberDashboardLayout` above
    (the member's one ACTIVE arrangement), this is a named, inert snapshot
    that can be picked from repeatedly, by its creator (`scope="personal"`)
    or, if the creator held `settings:manage` at save time, by the whole
    tenant (`scope="tenant"`; see `dashboard.py::post_preset`'s permission
    check -- there is no ongoing gate on this table itself).

    No `SoftDeleteMixin`: deleting a preset is meant to be permanent, the
    same call a member makes cleaning up their own saved views.
    """

    __tablename__ = "dashboard_preset"

    member_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    widgets: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="personal")

    __table_args__ = (
        CheckConstraint("scope IN ('personal','tenant')", name="ck_dashboard_preset_scope"),
    )
