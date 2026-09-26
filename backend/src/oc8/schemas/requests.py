"""Request bodies for mutating endpoints."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import AwareDatetime, ConfigDict, EmailStr, Field, field_validator

from oc8.schemas.base import CamelModel
from oc8.schemas.dto import WidgetInstanceDTO


def _normalized_address(value: str) -> str:
    """One spelling per person: trimmed, lowercased.

    `org_member.subject` IS the sign-in identity, and every comparison it takes
    part in is exact -- the no-op check and the uniqueness check in
    `change_own_email`, `subject_uuid_for` (which the messenger door resolves
    people by), the login lookup. Left as typed, `Ada@x.com` and `ada@x.com`
    are two accounts for one person, each able to hold the unique
    `(tenant_id, subject)` row the other one wanted. Addresses are
    case-insensitive in practice at every provider anybody self-hosts against,
    so the value that reaches the database is canonicalised once, here, rather
    than at each of the four places that compare it.
    """
    return value.strip().lower()


class CreateChatSessionRequest(CamelModel):
    agent_id: uuid.UUID


class SendChatMessageRequest(CamelModel):
    message: str = Field(min_length=1, max_length=20_000)
    attachment_ids: list[uuid.UUID] = []


class RenameChatSessionRequest(CamelModel):
    title: str = Field(min_length=1, max_length=200)


class CreateAgentRequest(CamelModel):
    name: str
    department_id: uuid.UUID
    role_title: str = ""
    mission: str = ""
    model_config_id: uuid.UUID | None = None
    is_team_lead: bool = False
    narrowing: dict[str, Any] = {}
    # Display fields for the office/agent views (llm, provider, tools, guardrails, ...).
    presentation: dict[str, Any] = {}
    #: Optional at hire time -- omitted or null both leave the agent on the
    #: default runtime, exactly like today. A plain string, not a uuid: names
    #: either a Capa's id (a plugin runtime) or one of the two built-in
    #: sentinels (BUILTIN_IN_PROCESS_RUNTIME_REF / BUILTIN_ISOLATED_RUNTIME_REF,
    #: oc8.runtime.registry), which have no Capa row. Validated by the same
    #: `assign_runtime` helper `PUT /agents/{id}/runtime` uses, so hiring with
    #: a bad value fails the same way changing it later would.
    runtime_plugin_id: str | None = None


class NarrowingRequest(CamelModel):
    narrowing: dict[str, Any] = {}


class GuardrailInterpretRequest(CamelModel):
    connection_name: str = Field(min_length=1)
    function: str = Field(min_length=1)
    definition: str = Field(min_length=1, max_length=2_000)


class GuardrailInterpretFromInstructionRequest(CamelModel):
    connection_name: str = Field(min_length=1)


class ModelConfigRequest(CamelModel):
    model_config_id: uuid.UUID
    #: Per-agent sampling overrides, written into agent.definition["model_params"]
    #: (modelrouter/sampling.py's resolve_params reads this as its narrowest-first
    #: source). None/absent on a field means "inherit the assigned ModelConfig's
    #: own value" -- these four are independent, matching ModelConfigWrite's own
    #: per-field semantics, not "unset the whole override on any partial save".
    temperature: float | None = None
    max_tokens: int | None = None
    effort: str | None = None
    #: Free-form raw parameters merged into model_params["extra"] (resolve_params,
    #: each adapter's payload builder). None/absent leaves the agent's existing
    #: raw overrides untouched; an empty dict clears them back to inheriting the
    #: assigned ModelConfig's own extra params.
    extra: dict[str, Any] | None = None
    #: Per-agent override of the run-loop step budget (agent.engine._max_steps),
    #: written to agent.definition["max_steps"] -- a top-level key, unlike the
    #: four sampling fields above, which live under definition["model_params"].
    #: None/absent leaves the agent's existing override untouched; 0 or a
    #: negative number clears it back to inheriting settings.agent_max_steps.
    max_steps: int | None = None


class InstructionsRequest(CamelModel):
    instructions: str = ""


class AgentRenameRequest(CamelModel):
    name: str = Field(min_length=1, max_length=200)


class ModelConfigWrite(CamelModel):
    provider: str
    model: str
    locality: str = "cloud"
    display_name: str | None = None
    #: How much this model can hold, in tokens. Recorded rather than discovered:
    #: the endpoints oc8 speaks to do not publish it -- /v1/models answers with
    #: ids and nothing else -- and without it every size question has to fall
    #: back to a conservative guess. 0 or None means "not recorded".
    context_window: int | None = None
    #: The completion budget resolve_params falls back to for any agent on
    #: this model that sets no per-agent override (modelrouter/sampling.py).
    #: 0 or None means "use the framework default" (1536), not "unlimited" --
    #: a reasoning-heavy model can spend that whole budget on hidden
    #: reasoning tokens before writing anything visible, which reads as the
    #: run failing outright on a large task, not as a slow one.
    max_tokens: int | None = None
    #: Gates whether chat image attachments are sent to this model at all.
    #: None/absent leaves it unchanged on update, matching max_tokens's own
    #: None-means-"don't touch" semantics.
    supports_vision: bool | None = None
    #: Forwarded to the provider verbatim (modelrouter/sampling.py's
    #: resolve_params, modelrouter/adapters/anthropic.py). Not validated
    #: against a fixed set of values -- the provider owns what it accepts,
    #: and that changes on the provider's own schedule. Blank clears it.
    effort: str | None = None
    #: Free-form top-level request fields forwarded verbatim after every named
    #: field (modelrouter/sampling.py's resolve_params, each adapter's payload
    #: builder) -- e.g. OpenRouter's `provider`/`top_p`. None leaves the
    #: model's existing raw params untouched on update; {} clears them.
    extra: dict[str, Any] | None = None
    used_by_copilot: bool | None = None
    #: A specific Credential to bind to this ModelConfig (id as string). None
    #: leaves the tenant-wide "first credential of this provider's type"
    #: convention as the fallback (resolve_model_key/resolve_model_base_url).
    credential_id: str | None = None


class ModelDiscoverRequest(CamelModel):
    provider: str
    #: Same optional per-credential override resolve_model_key/
    #: resolve_model_base_url take -- lets discovery use the credential the
    #: wizard has selected (possibly just-created, not yet saved onto any
    #: ModelConfig) instead of only the tenant-wide bound one.
    credential_id: str | None = None


class ModelPriceWrite(CamelModel):
    provider: str
    model_pattern: str
    price_in_usd_per_1m: float
    price_out_usd_per_1m: float


class AssignSkillRequest(CamelModel):
    skill_version_id: uuid.UUID


class CreateKnowledgeBaseRequest(CamelModel):
    name: str
    description: str = ""
    embedding_model: str = "nomic-embed-text"
    #: ``internal`` (default) or a capa vector-index ``type_id``.
    index_type: str = "internal"
    #: Non-secret mapping (collection, table, field keys). Never secrets.
    index_config: dict[str, Any] = {}
    #: Required when ``index_type`` is not ``internal``.
    credential_id: uuid.UUID | None = None
    classification: str = "internal"


class UpdateKnowledgeBaseRequest(CamelModel):
    """Edit a knowledge base's editable metadata (Design System Consistency
    plan, Task 5). All fields optional and applied only when present, same
    tri-state-adjacent shape as `UpdateSourceRequest`.

    `embedding_model` is here too, but the route only honours it while the
    base has never ingested a chunk: switching models after real content
    exists would leave old and new chunks embedded by different models in
    the same base, and a vector search can't meaningfully compare across
    them. Once chunks exist, changing models means deleting and recreating
    the base."""

    name: str | None = None
    description: str | None = None
    embedding_model: str | None = None


class IngestDocumentRequest(CamelModel):
    filename: str
    content_type: str
    content: str


class CreateGrantRequest(CamelModel):
    kb_id: uuid.UUID
    grantee_type: Literal["department", "agent"]
    grantee_id: uuid.UUID


class CreateComponentGrantRequest(CamelModel):
    component_key: str
    grantee_type: Literal["department", "agent"]
    grantee_id: uuid.UUID


class CreateSourceRequest(CamelModel):
    connector_type: str
    name: str
    config: dict[str, Any] = {}
    kb_id: uuid.UUID
    classification: str = "internal"
    oauth_connection_id: uuid.UUID | None = None


class SyncSourceRequest(CamelModel):
    kb_id: uuid.UUID


class UpdateSourceRequest(CamelModel):
    """Edit a data source's editable metadata (Design System Consistency plan,
    Task 4). Deliberately narrow: `connector_type` and `config` are not here --
    changing what a source connects to is a different, riskier operation than
    renaming it or reclassifying it, and belongs in a dedicated flow (or
    delete-and-recreate) if it is ever added. All fields optional and applied
    only when present, so a client editing just the name does not clobber the
    others."""

    name: str | None = None
    classification: str | None = None
    schedule_cron: str | None = None
    # Deliberately narrower than a full config replace: when present, this is
    # MERGED onto the existing config (never overwrites keys the caller didn't
    # send), so a client can change just e.g. `credential` without knowing or
    # resending `bucket`/`prefix`. Whether a given key is safe to change this
    # way is enforced by the connector's own config_schema on the frontend
    # (only credentialType-marked properties get an editable picker here);
    # nothing server-side currently blocks changing a non-credential key too.
    config: dict[str, Any] | None = None


class RuntimeAssignRequest(CamelModel):
    """Assign a runtime, or clear it with an explicit null.

    Strict on both counts, because an absent id MEANS "clear": with a default and
    ignored extras, a misspelt field name (`runtimeRef`) did the opposite of what
    the caller asked -- it unassigned the runtime and audited that as deliberate,
    while the agent silently fell back to the default runtime. Now the field must
    be present and no other may be, so the only way to clear is to say so.
    """

    model_config = ConfigDict(extra="forbid")

    #: A plain string, not a uuid -- see CreateAgentRequest.runtime_plugin_id's
    #: docstring for why (built-in sentinels have no Capa row).
    runtime_plugin_id: str | None


class LifecycleRequest(CamelModel):
    action: str  # start | pause | stop


class ApprovalDecisionRequest(CamelModel):
    decision: str  # approve | reject
    reason: str = ""
    #: The key of one of the agent's proposed options, when it offered any.
    #: `reason` stays free text and travels either way, so an operator can pick
    #: an option, write an instruction, or do both.
    option: str | None = None


class CreateMcpLoginRequest(CamelModel):
    """A login: a Credential paired with a McpConnection, tenant-global by
    default (agent tool login selection design). Distinct from
    `CreateMcpConnectionRequest` below, which stays department-scoped and
    untouched for the OAuth/legacy tool packs that still use it.

    Either `field_values` creates a brand-new Credential, or `credential_id`
    reuses one that already exists -- e.g. one a capa's own setup form
    created (Odoo's bundled "system" field) -- so the operator is not asked
    to retype values already sitting in the credential store. When
    `credential_id` is set, `field_values` is ignored.

    `department_id` is `None` by default, which creates a tenant-wide login
    (the only shape that existed before this field). Setting it scopes the
    login to one department, letting a second login share the same `name`
    (tool key) as long as it belongs to a different department -- e.g. two
    distinct Odoo logins for two different departments."""

    name: str
    credential_type: str
    field_values: dict[str, Any] = {}
    credential_id: uuid.UUID | None = None
    scopes: list[str] | dict[str, Any] = []
    department_id: uuid.UUID | None = None


class CreateMcpConnectionRequest(CamelModel):
    name: str
    transport: str = "stdio"  # stdio | sse | http
    server_url: str = ""
    command: str = ""
    args: list[str] = []
    department_id: uuid.UUID | None = None
    scopes: list[str] = []
    # Environment values are split deliberately: ordinary connection settings
    # (for example a host name) may be stored here, while secret_env only
    # stores secret *names*. Values are resolved inside the backend at launch.
    env: dict[str, str] = {}
    secret_env: dict[str, str] = {}


class UpdateMcpConnectionRequest(CamelModel):
    """Operator-only update for an existing MCP connection.

    Optional fields let a plugin materialise an unconfigured connection and
    later complete it in a guided setup without creating a duplicate.
    """

    name: str | None = None
    command: str | None = None
    args: list[str] | None = None
    department_id: uuid.UUID | None = None
    scopes: list[str] | None = None
    env: dict[str, str] | None = None
    secret_env: dict[str, str] | None = None


class RunAgentRequest(CamelModel):
    task: str
    mcp_connection_id: uuid.UUID | None = None


class SetBudgetRequest(CamelModel):
    department_id: uuid.UUID | None = None
    soft_limit_tokens: int | None = None
    hard_limit_tokens: int | None = None
    dollar_budget_usd: float | None = None


class CreateTriggerRequest(CamelModel):
    kind: Literal["cron", "event", "webhook"]
    task_text: str
    cron_expression: str | None = None
    event_source: str | None = None
    event_type: str | None = None


class UpdateTriggerRequest(CamelModel):
    task_text: str | None = None
    enabled: bool | None = None
    cron_expression: str | None = None


class CreateSecretRequest(CamelModel):
    name: str
    value: str
    kind: str = "generic"


class CreateCredentialRequest(CamelModel):
    name: str
    credential_type: str
    field_values: dict[str, Any] = {}


class UpdateCredentialRequest(CamelModel):
    name: str | None = None
    field_values: dict[str, Any] | None = None


class UpdatePolicyRequest(CamelModel):
    """Partial update for a SupervisionPolicy (§8.6): all fields optional.

    Only fields present in the request body (``model_dump(exclude_unset=True)``)
    are applied — omitted fields leave the stored policy untouched.
    """

    checkpoint_every: dict[str, Any] | None = None
    drift_thresholds: dict[str, Any] | None = None
    allowed_interventions: list[Literal["steer", "rewind", "pause_escalate", "reassign"]] | None = (
        None
    )
    judge_model_config_id: uuid.UUID | None = None
    sampling_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    judge_rubric: dict[str, Any] | None = None


class StartOAuthRequest(CamelModel):
    """Optional scope override; defaults to the provider's declared scopes."""

    scopes: list[str] | None = None


class DevicePollRequest(CamelModel):
    """Body for `POST /models/chatgpt-subscription/device/poll`.

    Deliberately stateless on the server: `expires_at` is computed
    client-side from `device/start`'s `expiresIn` and echoed back on every
    call, so the endpoint can answer "expired" before ever calling OpenAI --
    see `poll_chatgpt_device_login`'s docstring in catalog.py.
    """

    device_auth_id: str
    user_code: str
    #: A real datetime, not a string parsed by hand in the handler: a
    #: malformed value used to raise `ValueError` and a timezone-naive one
    #: `TypeError` (comparing it to an aware `now()`), each a bare 500 on an
    #: authenticated endpoint. `AwareDatetime` rejects both at validation time
    #: with a 422. The frontend already sends `new Date(...).toISOString()`,
    #: which is aware ("...Z") and needs no change.
    expires_at: AwareDatetime


class CreateServiceConnectionRequest(CamelModel):
    """A non-interactive credential a tenant admin submits directly -- no
    redirect, no consent screen. `tenant_id` here is the customer's OWN Azure
    AD tenant, not oc8's tenant concept; kept as its own field name
    (`azure_tenant_id`) on the wire so nobody misreads it as oc8's principal.tenant_id."""

    azure_tenant_id: str
    client_id: str
    client_secret: str


class CreateGoogleServiceConnectionRequest(CamelModel):
    """A non-interactive credential a tenant admin submits directly for
    Google's service-account JWT-bearer grant -- no redirect, no consent
    screen. `service_account_key_json` is the whole downloaded key file
    (JSON), not just the private key, so the provisioner can read
    `client_email` out of it too."""

    service_account_key_json: str
    shared_drive_ids: str = ""
    delegated_mailboxes: str = ""
    default_mailbox: str = ""


class CreateMemberRequest(CamelModel):
    """Enrol a person this tenant has not seen yet (§5).

    `subject` is the token's `sub`, verbatim -- the same string the IdP will send
    when they first sign in, which is what makes this an upsert on
    `(tenant_id, subject)` rather than a second row for the same human.
    """

    subject: str = Field(min_length=1)
    display_name: str = ""
    #: TRI-STATE, and that is the fix for a silent 200: `None` means "leave it as
    #: it is", `true` grants, `false` REVOKES.
    #:
    #: It defaulted to `False`, and `upsert_member` only ever widened, so
    #: `{"subject": …, "allDepartments": false}` returned 200 with the flag still
    #: set -- an administrator taking company-wide decide authority away from a
    #: demoted CEO was told it worked and nothing had happened. The design calls
    #: this grant "durable, audited and revocable" (§2) and `models/identity.py`
    #: repeats "revocable"; nothing in `src/` wrote `False` anywhere.
    #:
    #: Tri-state rather than a plain `bool` because the two failure modes are
    #: symmetrical: with a `False` default, a POST that only meant to set a
    #: display name would silently strip a CEO of his company-wide view.
    all_departments: bool | None = None
    #: Optional so a person who will sign in through the IdP still enrols with
    #: no local credential at all. When given, hashed and written to
    #: `password_hash` in the same request that creates the row, so an
    #: administrator who types a password here does not need a second call
    #: to `PUT /members/{id}/password` before the person can sign in.
    password: str | None = Field(default=None, min_length=8, max_length=1024)


class SetMemberPasswordRequest(CamelModel):
    """Set or reset a member's local password (§5). Admin-only.

    Mirrors `PasswordSetupRequest`'s bounds exactly -- the same password that
    would be accepted at first-run setup must be accepted here, or an
    administrator hits a rule this form never explained.
    """

    password: str = Field(..., min_length=8, max_length=1024)


class RenameMemberSubjectRequest(CamelModel):
    """Change a member's sign-in identity (§5).

    Safe because Community's local password login is the only identity
    provider this edition ships: `subject` is the member's own email, an
    administrator-owned value with nothing external it has to keep matching
    on every request.
    """

    subject: str = Field(..., min_length=1, max_length=255)


class UpdateDisplayNameRequest(CamelModel):
    """Self-service display-name change (`PUT /auth/me/display-name`).

    No password confirmation: a display name is cosmetic and carries no
    authority, which mirrors `CreateMemberRequest` accepting `display_name`
    with no extra gate. The two requests that DO move the sign-in identity
    (`ChangeOwnPasswordRequest`, `ChangeOwnEmailRequest`) demand the current
    password instead.
    """

    display_name: str = Field(..., min_length=1, max_length=255)


class ChangeOwnPasswordRequest(CamelModel):
    """Self-service password change (`PUT /auth/me/password`).

    `current_password` is mandatory and is the whole difference from the
    admin-only `SetMemberPasswordRequest`: a caller here proves they still
    know the OLD password before setting a new one, so a borrowed session
    cannot silently lock the real owner out of their own account.

    `new_password` mirrors `SetMemberPasswordRequest`/`PasswordSetupRequest`
    bounds exactly -- a password accepted at first-run setup must be accepted
    here too.
    """

    current_password: str = Field(..., min_length=1, max_length=1024)
    new_password: str = Field(..., min_length=8, max_length=1024)


class ChangeOwnEmailRequest(CamelModel):
    """Self-service email (sign-in identity) change (`PUT /auth/me/email`).

    Same current-password gate as `ChangeOwnPasswordRequest`, for a sharper
    reason: the email IS the login, so an unguarded change would let whoever
    holds a session move the account to an address they control. Whether the
    new address applies at once or only after a confirmation link depends on
    whether the tenant has a mail server configured -- see the endpoint.

    `new_email` is an `EmailStr`, not a length-checked `str`. On the
    no-mail-server branch this value is written straight into
    `org_member.subject` and committed, so "nonsense" was a valid new LOGIN --
    and one nobody can mail a correction to. It is lowercased on the way in;
    see `_normalized_address`.
    """

    current_password: str = Field(..., min_length=1, max_length=1024)
    new_email: EmailStr = Field(..., max_length=255)

    _normalize_new_email = field_validator("new_email")(_normalized_address)


class ConfirmEmailChangeRequest(CamelModel):
    """The mailed link coming back (`POST /auth/email/confirm`).

    One field, and no `email` beside it on purpose: the address to move to is
    read off the stored token row, never off the request. A body that also
    carried the target address would let whoever holds a link redirect it
    somewhere else, which is the entire thing the confirmation exists to stop.

    No bearer token accompanies this: the link is the credential, and it is
    proof the caller reads the address the change is moving TO.
    """

    token: str = Field(..., min_length=1, max_length=512)


class ForgotPasswordRequest(CamelModel):
    """Ask for a reset link (`POST /auth/password/forgot`).

    Deliberately the same shape as `PasswordLoginRequest` minus the password:
    email only, no tenant field, because Community is single-instance and the
    organization is resolved server-side by `_get_singleton_organization`.

    Validated and lowercased like `ChangeOwnEmailRequest.new_email`, so the
    same person typing the same address two different ways reaches the same
    account. A malformed address is a 422 -- that is a fact about the request,
    not about who has an account here, so it leaks nothing the generic 202 was
    protecting.
    """

    email: EmailStr = Field(..., max_length=255)

    _normalize_email = field_validator("email")(_normalized_address)


class ResetPasswordRequest(CamelModel):
    """Spend a reset link on a new password (`POST /auth/password/reset`).

    No `current_password` -- there is none to know; possession of the mailed
    token IS the proof, which is why the token is single-use and short-lived.
    `new_password` mirrors `PasswordSetupRequest`/`ChangeOwnPasswordRequest`
    bounds exactly: a password accepted at first-run setup must be accepted
    here, or somebody locked out of their account meets a rule for the first
    time at the worst possible moment.
    """

    token: str = Field(..., min_length=1, max_length=512)
    new_password: str = Field(..., min_length=8, max_length=1024)


class GrantSeatRequest(CamelModel):
    """Put somebody in a department, at one of exactly two seat roles.

    `seat_role` is validated against `SEAT_PERMISSIONS` in the route rather than
    typed as a Literal here, so the closed vocabulary lives in one place -- the
    day a third seat role is added, it is added there and to the CHECK
    constraint, and this body needs no edit to stay in step.
    """

    seat_role: str
    #: Tri-state, and the tri-state is load-bearing. `None` means "leave
    #: unchanged" -- a plain `bool = False` default would make an ordinary,
    #: unrelated seat_role promotion/demotion silently STRIP an already-granted
    #: toggle the moment the caller omits the field. See `grant_seat`.
    agent_manage: bool | None = None


class AnswerClarificationRequest(CamelModel):
    """The sentence the agent is waiting for.

    Non-empty, because an empty answer resumes the run with nothing to act on:
    the agent asked because it could not proceed, and "" is not an answer, it is
    the same question again with a person's time spent on it.
    """

    answer: str = Field(min_length=1)


class CreateTaskRequest(CamelModel):
    """A member putting new work directly on a department's team lead, from
    the My Work task board rather than a chat session.
    """

    department_id: str
    instructions: str = Field(min_length=1)
    title: str | None = None


class CreateRoleRequest(CamelModel):
    """Compose a tenant-defined role: a NAME and a SET of permission strings.

    Nothing else. No parent, no inheritance, no per-object grant, no constraint
    expression -- the stored artefact is always a flat set, and the two columns
    that would allow anything else are pinned shut by CHECK constraints rather
    than by a comment.

    `based_on` pre-ticks the boxes in the UI and is deliberately NOT STORED.
    Storing it would make it a parent, and a parent is inheritance: the day
    somebody edits `operator` in code, every role "based on" it would change
    underneath its author without an audit event naming the change.

    There is no `kind` field, and sending one changes nothing. Tenant-defined
    AGENT roles are deferred (`agent:manage`, a second editor); until then there
    must be no path from this form into the population whose vocabulary is
    `tool:read|write|send`, including by adding a key to the JSON.
    """

    name: str = Field(min_length=1)
    description: str = ""
    permissions: list[str] = Field(default_factory=list)
    based_on: str | None = None


class UpdateRoleRequest(CamelModel):
    """FULL replacement of what a role grants, and of the sentence describing it.

    Replacement rather than a patch, because a role IS its set: a PATCH shape
    ("add these, remove those") makes the screen's Save button send a diff
    against a version it read some seconds ago, and two administrators editing
    one role would each land half an edit with no way to notice.

    That last sentence was true of this shape as well until the write took a row
    lock: the replacement is a DELETE and then INSERTs, and two of them under
    READ COMMITTED merged into their union -- each administrator landing half an
    edit, exactly as promised here, through the transaction rather than through
    the wire format. See `roles/service.load_role`'s `for_update`.

    `name` is absent on purpose -- renaming is refused. The name is what the
    audit trail says, and a rename would retroactively change what every earlier
    entry appears to be about. Delete-and-recreate is the sanctioned path, and
    the unique index is unconditional so the old name is never handed on.

    **Both fields are REQUIRED, and that is the difference between a replacement
    and a default.** They carried `= ""` and `= Field(default_factory=list)`, so
    a body that simply omitted `permissions` -- a client bug, a hand-written
    curl, a proxy dropping a key -- was indistinguishable on the wire from "take
    all twenty-one grants away from forty people", and was answered 200 with an
    audit event recording the revocation as intended. Full replacement is the
    right shape; full replacement of a field the caller never named is not.
    """

    description: str
    permissions: list[str]


class SetMemberRoleRequest(CamelModel):
    """Give one person a role, or take the override away.

    `None` is not "no permissions": it clears `org_member.role_id`, and the
    person resolves through their token exactly as they did before this feature
    existed. That is the difference between demoting somebody and un-demoting
    them, and it is why the column is nullable rather than defaulted.
    """

    role_id: uuid.UUID | None = None


class BulkAssignRoleRequest(CamelModel):
    """The same role for many people, in ONE transaction.

    The five-hundred-person tenant's onboarding shape. One transaction because a
    commit per person inside `tenant_session` would unbind `app.tenant_id` and
    every assignment after the first would silently affect zero rows -- and one
    audit event per assignment regardless, because "40 people were given a role"
    is not an answer to "when did Anna get this".
    """

    member_ids: list[uuid.UUID] = Field(min_length=1)
    role_id: uuid.UUID | None = None


class PushSubscriptionKeys(CamelModel):
    p256dh: str
    auth: str


class PushSubscribeRequest(CamelModel):
    endpoint: str
    keys: PushSubscriptionKeys


class PutDashboardLayoutRequest(CamelModel):
    widgets: list[WidgetInstanceDTO] = Field(max_length=50)
    template_id: str | None = None


class CreateDashboardPresetRequest(CamelModel):
    name: str = Field(min_length=1, max_length=80)
    widgets: list[WidgetInstanceDTO] = Field(max_length=50)
    scope: Literal["personal", "tenant"] = "personal"

    @field_validator("name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value
