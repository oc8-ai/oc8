"""Response DTOs mirroring the frontend interfaces (src/lib/*.ts)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from oc8.schemas.base import CamelModel


class AgentDTO(CamelModel):
    id: str
    name: str
    role: str
    llm: str
    provider: str
    status: str  # running | warning | error | paused | waiting_for_task
    tools: list[str]
    last_action: str
    last_run: str
    tasks_today: int
    guardrails: list[str]
    schedule: str
    avatar_color: str
    department_id: str | None = None
    model_config_id: str | None = None
    is_lead: bool = False
    deleted_at: str | None = None
    #: Demo-seed locale overlays (`{de: "…"}`). Empty on live agents.
    role_translations: dict[str, str] = {}
    last_action_translations: dict[str, str] = {}
    last_run_translations: dict[str, str] = {}
    schedule_translations: dict[str, str] = {}
    mission_translations: dict[str, str] = {}
    guardrails_translations: dict[str, list[str]] = {}


class DepartmentDTO(CamelModel):
    id: str
    name: str
    icon: str
    goal: str
    okr: str
    kpi_label: str
    kpi_value: str
    activity: int
    accent: str
    prompt_caching_enabled: bool
    deleted_at: str | None = None
    name_translations: dict[str, str] = {}
    goal_translations: dict[str, str] = {}
    okr_translations: dict[str, str] = {}
    kpi_label_translations: dict[str, str] = {}


class TaskDTO(CamelModel):
    id: str
    department_id: str
    title: str
    agent_id: str | None = None
    column: str  # backlog | in_progress | waiting | done
    meta: str | None = None
    title_translations: dict[str, str] = {}
    meta_translations: dict[str, str] = {}


class ActivityDTO(CamelModel):
    id: str
    agent_id: str | None = None
    status: str  # success | warning | error | info
    message: str
    time: str
    detail: str | None = None
    cache_hit: bool = False
    message_translations: dict[str, str] = {}
    detail_translations: dict[str, str] = {}


class BoardDTO(CamelModel):
    """A page of a department's board, plus what it is NOT showing.

    The totals are not decoration: without them a truncated board looks exactly
    like a finished one, and the operator has no way to tell that 40 more tasks
    exist below the fold.
    """

    tasks: list[TaskDTO] = []
    totals: dict[str, int] = {}


class ApprovalOptionDTO(CamelModel):
    key: str
    label: str
    detail: str = ""


class ApprovalDTO(CamelModel):
    id: str
    agent_id: str
    title: str
    detail: str
    amount: str | None = None
    time: str
    status: str  # pending | approved | rejected | expired
    #: What kind of decision this is. "decision" ones carry options and are
    #: answered with one of them; everything else is a plain yes/no on a held
    #: action.
    action_type: str = ""
    #: The alternatives the agent proposed, and the one it would pick. The point
    #: is that a human can decide here rather than opening the source system.
    options: list[ApprovalOptionDTO] = []
    recommendation: str | None = None
    #: Which option was picked, once decided.
    decision_option: str | None = None
    reason: str | None = None
    # Whether deciding this approval actually resumed the suspended run. False on
    # a decision that could not be carried out (no run left to resume), so the UI
    # can say "recorded, but the held action never ran" instead of implying the
    # agent picked the work back up. Only meaningful on a decision response; a
    # listed approval reports False.
    resumed: bool = False

    # ------------------------------------------------------------------ §5, new
    #
    # Everything below is additive. Nothing above it changed name or meaning --
    # `time` in particular is left exactly as it was (it comes from
    # `payload["time"]`, which only the demo seed writes, so it is `""` on every
    # real row) because a client reading it must not start seeing a different
    # string in the same release that gives it `createdAt`.

    #: Whose approval this is. `null` means TENANT-WIDE, which today is only ever
    #: the tenant-scope budget incident, and only somebody unrestricted is shown
    #: one at all.
    department_id: str | None = None
    #: Resolved for the screen, which shows a department chip when the caller
    #: covers more than one. Empty when the department row is gone -- a renamed or
    #: archived department must not delete the approval from the queue.
    department_name: str = ""
    agent_name: str = ""
    task_id: str | None = None
    task_title: str = ""
    #: ISO-8601. What "vor 2 Std." on the row is computed from. There was no
    #: timestamp on this DTO at all, so the inbox could not say how long anything
    #: had been waiting.
    created_at: str = ""
    #: The person who answered it, by name. Empty while pending, and empty for a
    #: row decided before `decided_by` was ever written (it is declared in
    #: migration 0001 and was assigned nowhere in `src/` until this slice).
    decided_by_name: str = ""
    #: The heart of the detail pane: exactly which call is being held, and with
    #: which arguments. Both come out of `payload`, which is NEVER serialized
    #: wholesale -- `_announce` writes `channel_handles` into it, i.e. which
    #: messenger accounts were told, and that is nobody's business on a screen.
    tool_name: str | None = None
    tool_arguments: dict[str, Any] = {}
    title_translations: dict[str, str] = {}
    detail_translations: dict[str, str] = {}
    #: `ApprovalRequest.reason_context` verbatim: `{"code": ..., ...}` when
    #: this was raised from a PDP `authorize_tool_call` decision, letting the
    #: pane render an i18n template instead of `detail`'s raw English string.
    #: `None` for every approval not raised that way -- `detail` remains the
    #: only "why" for those.
    reason_context: dict[str, Any] | None = None


class ClarificationDTO(CamelModel):
    """A question an agent parked mid-run, waiting for a person.

    The other half of the workspace queue, and the half that had no endpoint and
    no screen at all: the only way to answer one was `POST /runs/{id}/answer`,
    gated on `run:control`, which no seat carries.
    """

    id: str
    run_id: str
    agent_id: str
    agent_name: str
    #: Resolved through the agent -- `Clarification.agent_id` is NOT NULL and
    #: `Agent.department_id` is NOT NULL, so this is never null for a row that is
    #: listed at all.
    department_id: str
    department_name: str = ""
    question: str
    status: str  # open | answered
    created_at: str = ""


class TaskBoardRowDTO(CamelModel):
    """One card on the My Work task board -- the cross-department view, unlike
    `TaskDTO`/`BoardDTO` which are already scoped to one known department and
    so never needed a department name of their own.
    """

    id: str
    title: str
    state: str
    column: str  # backlog | in_progress | waiting | done
    department_id: str
    department_name: str = ""
    agent_id: str | None = None
    agent_name: str | None = None
    requested_by_member_id: str | None = None
    parent_task_id: str | None = None
    delegation_depth: int = 0
    created_at: str = ""


class ClarificationAnswerDTO(CamelModel):
    """What answering one gives back.

    Deliberately small. The run it re-queued is named so the screen can follow
    it, but nothing about the run's own state is echoed: it is published onto the
    run stream after the commit and a worker may already have moved it on, so any
    state reported here would be a claim this request cannot stand behind.
    """

    id: str
    run_id: str
    status: str
    answer: str


class SeatDTO(CamelModel):
    """One person standing in one department."""

    department_id: str
    #: Empty only when the department row is gone. A seat the screen can render
    #: only as a uuid is a subtitle nobody can read.
    department_name: str = ""
    seat_role: str  # dept_viewer | dept_approver
    #: WRITE authority over Agent, in this department. Independent of
    #: seat_role. Never a resource:action string -- see
    #: `OrgMemberDepartment.agent_manage`.
    agent_manage: bool


class MemberDTO(CamelModel):
    id: str
    subject: str
    display_name: str = ""
    #: Sees and decides in every department, including ones created after the row
    #: was written. Written at an authenticated moment and revocable; never
    #: inferred.
    all_departments: bool = False
    #: When this tenant first saw the person -- the row is minted on their first
    #: workspace request. Deliberately NOT called `lastSeenAt`: nothing records a
    #: last request anywhere, and a "last seen" that never moves is worse than no
    #: column, because an administrator would use it to decide who to offboard.
    first_seen_at: str = ""
    seats: list[SeatDTO] = []
    #: The role an administrator ASSIGNED this person, and its name. `null` /
    #: `""` is the token floor -- not "no permissions", and the screen must not
    #: render it as one.
    #:
    #: On the wire because until it was, nothing in the product could answer
    #: "what does Anna hold": the only view of an assignment was a role's holder
    #: list, so the question cost one panel expansion per role, and the picker
    #: offered somebody whose current role it could not show and then replaced it
    #: silently.
    role_id: str | None = None
    role_name: str = ""
    #: Set only by `POST /members` when it minted an invite link for a
    #: passwordless member (community mode, no password given). `null` on
    #: every other response that carries a `MemberDTO` -- `GET /members`
    #: included -- because nothing else mints one.
    invite_link: str | None = None
    #: Whether that link was actually emailed. `False` alongside a non-null
    #: `invite_link` means "no mail server configured (or the send failed)" --
    #: the link itself is still returned so an administrator can copy/share it
    #: by hand; a failed or skipped send never fails the request that created
    #: the member.
    invite_sent: bool = False


class MeDTO(CamelModel):
    """The caller, and where they stand.

    `subject` / `role` / `kind` / `scopes` all keep their names and meanings; what
    this route used to return was the raw `Principal`, so the ONE rename on the
    wire is `tenant_id` -> `tenantId`, which every other body in this API already
    uses and which no in-repo consumer reads (`src/lib/hooks.ts` reads `subject`,
    `role` and `kind`). It is named in the release note rather than hidden: an
    out-of-repo client reading `tenant_id` sees it disappear.

    `seats` and `viewsAllDepartments` together are what let the screen tell its
    two empty states apart. "Nichts wartet auf dich" and "Du bist keiner
    Abteilung zugeordnet" are the same blank page today, and one of them is the
    system working while the other is a person locked out of their own job.
    """

    subject: str
    tenant_id: str
    role: str
    kind: str
    #: Plugin-token scopes, verbatim from the `Principal` this route used to
    #: return. Empty for every human. Kept because dropping it was a silent
    #: subtraction from a response somebody outside this repository may read, and
    #: an empty list costs nothing.
    scopes: list[str] = []
    display_name: str = ""
    #: `null` only for a principal that cannot stand in a department at all (a
    #: plugin token). Every operator has one from their first request on, which is
    #: also what keeps `approval_request.decided_by` from being NULL.
    member_id: str | None = None
    views_all_departments: bool = False
    #: SIGNS OFF everywhere, which is not the same person as `viewsAllDepartments`
    #: and must not be folded into it. `authz/scope.py` keeps the two apart for
    #: one holder -- the `auditor`, who is unrestricted through
    #: `approval:view_any` and holds `approval:decide` nowhere -- and the screen
    #: could not see the distinction, so it drew Approve and Reject on every row
    #: for a role the backend 403s on every click.
    decides_all_departments: bool = False
    seats: list[SeatDTO] = []
    #: Drives the frontend's /welcome redirect. None only for a principal
    #: with no member/scope at all (the existing PermissionError branch in
    #: `me()`) -- never a human admin, so the redirect check never sees it.
    onboarding_status: str | None = None


class IntegrationDTO(CamelModel):
    id: str
    name: str
    category: str
    connected: bool
    used_by: list[str] = []
    desc: str
    hue: int


class ModelDTO(CamelModel):
    id: str
    provider: str
    name: str
    status: str  # healthy | degraded | error | unknown
    cost_tier: str
    latency: str
    assigned_to: list[str] = []
    note: str
    model: str = ""
    locality: str = "cloud"
    display_name: str | None = None
    context_window: int | None = None
    #: The completion budget resolve_params falls back to when the agent
    #: sets none of its own (modelrouter/sampling.py). None means "framework
    #: default" (1536), not "unlimited".
    max_tokens: int | None = None
    supports_vision: bool = False
    #: Forwarded to the provider verbatim; see ModelConfigWrite.effort.
    effort: str | None = None
    #: Free-form raw parameters forwarded verbatim; see ModelConfigWrite.extra.
    extra: dict[str, Any] | None = None
    used_by_copilot: bool = False
    credential_id: str | None = None
    #: Set only when `status` is "error"/"unknown" -- the real reason from
    #: the last POST /models/{id}/test check (see catalog.py).
    health_error: str | None = None
    health_checked_at: str | None = None


class ModelDiscoverResponse(CamelModel):
    models: list[str]


class ModelPriceDTO(CamelModel):
    id: str
    provider: str
    model_pattern: str
    price_in_usd_per_1m: float
    price_out_usd_per_1m: float
    effective_from: str
    active: bool


class ReconciliationDTO(CamelModel):
    provider: str
    report_date: str
    oc8_calculated_cost_micros: int
    provider_reported_cost_micros: int | None
    fetched_at: str


class SkillDTO(CamelModel):
    id: str
    name: str
    description: str
    category: str
    origin: str
    version: str
    author: str
    tools: list[str]
    knowledge: list[str] = []
    guardrails: list[str]
    instructions: str
    used_by_agents: int
    installs: int | None = None
    price: str | float | None = None
    updated_at: str
    current_version_id: str | None = None
    deleted_at: str | None = None
    name_translations: dict[str, str] = {}
    description_translations: dict[str, str] = {}
    instructions_translations: dict[str, str] = {}
    guardrails_translations: dict[str, list[str]] = {}


class DataSourceDTO(CamelModel):
    id: str
    kind: str
    name: str
    connected: bool
    last_sync: str | None = None
    doc_count: int | None = None
    schedule: str | None = None
    sensitivity: str | None = None
    scope: str | None = None
    connector_type: str
    #: `ok` | `held`. A hold nothing can see is a hold nobody acts on: core
    #: refused an attested listing as implausible and is doing nothing about this
    #: source until a human looks, and the API is the only place an operator
    #: could learn that. The note says why in words, and names the endpoint that
    #: clears it -- "held" on its own sends an operator hunting for a bug in the
    #: sweep.
    reconcile_state: str | None = None
    reconcile_note: str | None = None
    #: `ok` | `failed` | null (never synced yet). Separate from `connected`
    #: (transport/credentials reachable), which a failed sync does not move.
    last_sync_status: str | None = None
    #: Set only while `last_sync_status == "failed"`.
    last_sync_error: str | None = None
    deleted_at: str | None = None
    #: Non-secret connector config only (e.g. `bucket`, `prefix`, and a
    #: `credential`-typed property's CREDENTIAL ID -- never a resolved
    #: secret value; a credential-typed key only ever holds an id, per the
    #: Unified Credentials Framework's own write-only-secrets discipline).
    #: Lets the edit UI show/change which credential a source uses without
    #: reopening its full config for editing.
    config: dict[str, Any] = {}


class KnowledgeDocumentDTO(CamelModel):
    """One document of a knowledge base. A document is a group of chunks sharing
    a `(dataSourceId, sourceUri)`, not a row, so this has no id of its own --
    `sourceUri` is what the delete and restore routes take."""

    source_uri: str
    data_source_id: str | None = None
    kb_id: str
    chunks: int
    created_at: str | None = None
    deleted_at: str | None = None
    deleted_reason: str | None = None
    reduced_at: str | None = None


class KbChunkDTO(CamelModel):
    id: str
    kb_id: str
    source_uri: str
    content: str
    classification: str
    chunk_metadata: dict[str, Any]
    created_at: str


class SimilarChunkDTO(CamelModel):
    id: str
    kb_id: str
    source_uri: str
    content: str
    classification: str
    chunk_metadata: dict[str, Any]
    created_at: str
    similarity: float


class DocumentRemovalDTO(CamelModel):
    """The receipt for erasing one document.

    `auditSeq` addresses the ledger entry that names the digest of what was
    destroyed, so "prove it" is a lookup rather than a support ticket, and
    `sourcesTouched` reports the blast radius of an omitted `dataSourceId`
    rather than hiding it.
    """

    source_uri: str
    sources_touched: int
    documents: int
    chunks: int
    sha256: str | None = None
    audit_seq: int | None = None


class DocumentRestoreDTO(CamelModel):
    """What came back. No digest and no `auditSeq` of a destruction, because a
    restorable tombstone is one where nothing was destroyed."""

    source_uri: str
    chunks: int


class SourceRemovalDTO(CamelModel):
    """The same receipt for a whole source or base. The digest is over the sorted
    distinct URI list, not over content -- see `tombstone._reduce_scope`."""

    source_id: str
    documents: int
    chunks: int
    sha256: str | None = None
    audit_seq: int | None = None


class BaseRemovalDTO(CamelModel):
    """A base delete's receipt. Its own type rather than `SourceRemovalDTO` with
    a kb id in `sourceId`: a field that names the wrong kind of object is how a
    caller ends up passing it back to the wrong endpoint."""

    kb_id: str
    documents: int
    chunks: int
    sha256: str | None = None
    audit_seq: int | None = None


class SourceUnlinkedFromBaseDTO(CamelModel):
    """A source-from-base unlink's receipt -- narrower than either removal
    above: neither the source nor the base was deleted, only their content
    together in this one pairing."""

    kb_id: str
    data_source_id: str
    documents: int
    chunks: int
    sha256: str | None = None
    audit_seq: int | None = None


class KnowledgeBaseDTO(CamelModel):
    id: str
    name: str
    description: str
    source_ids: list[str] = []
    docs: int
    chunks: int
    embedding_model: str
    sensitivity: str
    updated: str
    status: str  # current | updating | error
    linked_departments: list[str] = []
    linked_agents: list[str] = []
    roles: list[str] = []
    local_only: bool = False
    deleted_at: str | None = None
    name_translations: dict[str, str] = {}
    description_translations: dict[str, str] = {}


class GrantDTO(CamelModel):
    id: str
    kb_id: str
    grantee_type: str
    grantee_id: str


class ComponentGrantDTO(CamelModel):
    id: str
    component_key: str
    grantee_type: str
    grantee_id: str


class IngestionJobDTO(CamelModel):
    id: str
    status: str
    stats: dict[str, Any] = {}


class ConditionDTO(CamelModel):
    """Read-side mirror of `authz.pdp.Condition`, one entry of
    `ToolPolicyDTO.conditions` -- see `ConditionWriteDTO` (departments.py) for
    the write-side counterpart this is deliberately kept field-for-field
    identical to.
    """

    attribute: str
    datatype: str
    operator: str
    value: Any = None
    then: str


class ToolPolicyDTO(CamelModel):
    enabled: bool
    read: bool
    modify: bool
    approval_eur: int | None = None
    # Both default empty/None rather than being required: `**ToolPolicy.to_json()`
    # (agent_tools_dto's frame path) and the raw frame JSON dict (this DTO's
    # narrowing/effective-tools path) both always carry them, but a caller
    # constructing one by hand for a test should not have to.
    approval_actions: list[str] = []
    only: list[str] | None = None
    connection_id: str | None = None
    #: Generic "with limits" rules -- see `ConditionDTO`. Silently dropped by
    #: pydantic's default extra="ignore" until this field existed, even
    #: though `ToolPolicy.to_json()` always emits the key -- callers building
    #: `**ToolPolicy.to_json()` need this present or `conditions` never
    #: reaches the frontend at all.
    conditions: list[ConditionDTO] = []


class AgentDetailDTO(AgentDTO):
    mission: str = ""
    department_name: str | None = None
    effective_tools: dict[str, ToolPolicyDTO] = {}
    department_frame_tools: dict[str, ToolPolicyDTO] = {}
    #: The agent's own `narrowing["tools"]` row, verbatim -- NOT intersected
    #: with `role_rights` the way `effective_tools` is. The Configuration and
    #: Guardrails tabs must resave whichever fields they don't own (read/
    #: write/send/approval_eur/approval_actions/only) from THIS, never from
    #: `effective_tools`: a role_rights dip (a bad role reference, a role
    #: mid-edit) would otherwise get silently baked into the agent's stored
    #: narrowing forever the next time either tab saves, since a save always
    #: rewrites the full `tools` payload including fields it isn't editing.
    narrowing_tools: dict[str, ToolPolicyDTO] = {}
    #: Tool keys this agent has deliberately overridden via its own
    #: `PUT /agents/{id}/narrowing` save -- the ONLY writer of this set (see
    #: `agents_write.py`'s `set_narrowing`). Mere presence in `narrowing_tools`
    #: is NOT the same signal: every save rewrites every currently-relevant
    #: key, touched or not, so a key can appear there without ever having been
    #: deliberately changed. This is the authoritative "has this agent
    #: diverged from the department default for this tool" answer -- the same
    #: one `departments.py`'s `_deviation_counts` (§ Abweichungen) uses.
    narrowing_overridden_keys: list[str] = []
    #: Per-`effective_tools` key, which level actually produced that key's
    #: current value -- "agent" / "department" / "capa_default" (see
    #: `authz.pdp.tool_policy_source`; guardrails UX Source column). A key
    #: absent here (should not happen for anything present in
    #: `effective_tools`) has no known provenance rather than a guessed one.
    tool_policy_sources: dict[str, str] = {}
    runtime_ref: str | None = None
    #: The run this agent is on right now, whoever started it. Without it the
    #: detail screen can only show a live log for a run started in that same
    #: browser tab, so a scheduled run happens invisibly.
    current_run_id: str | None = None
    #: Per-agent sampling overrides (agent.definition["model_params"]),
    #: narrowest-first ahead of the assigned ModelConfig's own defaults --
    #: see modelrouter/sampling.py's resolve_params(). None means "inherit
    #: the model's value", not "use a framework default directly".
    temperature: float | None = None
    max_tokens: int | None = None
    effort: str | None = None
    extra: dict[str, Any] | None = None
    #: Per-agent override of the run-loop step budget (agent.definition
    #: ["max_steps"], a top-level key -- see engine._max_steps). None means
    #: "inherit settings.agent_max_steps", not a framework default value.
    max_steps: int | None = None


class AgentInstructionRevisionDTO(CamelModel):
    """One `agent.instructions.updated` audit_event, reshaped for the agent
    detail page's Instructions tab -- paperclip's `agent_config_revisions`
    pattern, but read off the existing tamper-evident audit chain instead of
    a dedicated table (oc8 already has one generic append-only log; adding a
    second, narrower one would just be two places to keep in sync)."""

    ts: str
    before: str
    after: str
    by: str | None = None


class AgentInstructionHistoryDTO(CamelModel):
    revisions: list[AgentInstructionRevisionDTO] = []
    #: Total revisions across every page -- lets the Instructions tab number
    #: versions by true position (vN downwards) without loading all of them.
    total_count: int = 0
    #: Cursor for the next older page (an audit_event.seq); null once the
    #: oldest revision has been returned.
    next_before_seq: int | None = None


class MemoryRecordDTO(CamelModel):
    """One `memory_record` row, for the agent/department Memory tabs (§10).
    `status` is always "approved" for the agent/department tiers this DTO
    serves -- only company-tier writes can be "pending"/"rejected", and that
    tier has no browsing UI yet -- carried anyway so the shape doesn't need
    a breaking change when it does."""

    id: str
    content: str
    status: str
    created_at: str
    written_by: str


class MemoryListDTO(CamelModel):
    records: list[MemoryRecordDTO] = []


class PrincipalUsageDTO(CamelModel):
    group: str
    tokens_in: int
    tokens_out: int
    provider_cost_micros: int
    saved_tokens_in: int
    saved_tokens_out: int
    saved_cost_micros: int


class GuardrailPresetDTO(CamelModel):
    """A named permission set the connection's plugin ships (§ guardrail
    presets). Data only, mirroring `oc8.capas.manifest.GuardrailPreset` --
    the picker offers it as a ceiling, the operator still applies it as an
    ordinary policy."""

    key: str
    label: str
    #: Every translation of `label` this connection's plugin ships, keyed by
    #: locale (design: capa-i18n). Missing a locale means the browser falls
    #: back to `label` itself for that locale, never a stale copy.
    label_translations: dict[str, str] = {}
    summary: str
    summary_translations: dict[str, str] = {}
    recommended: bool
    read: bool
    modify: bool
    approval_actions: list[str] = []
    approval_eur: int | None = None
    #: The ONLY tool names this preset puts within reach; empty means all of
    #: them. NOT optional decoration: `autonomous_with_limit` is safe precisely
    #: because it withholds `delete_record`, whose deletions carry no amount and
    #: so can never meet its euro threshold. A DTO that dropped this would hand
    #: the browser a preset that applies as "read+write+send above EUR 1000,
    #: everything reachable" -- the unattended-deletion configuration, under a
    #: name promising a limit.
    only: list[str] = []


class GuardrailAdjustableDTO(CamelModel):
    """One number on a `GuardrailDTO` the wizard lets an operator change
    before applying it. Mirrors `oc8.capas.guardrails.GuardrailAdjustable`
    field-for-field -- a read-only projection, not a reuse of the
    plugin-internal model, so the API contract stays independent of the
    manifest's on-disk shape."""

    field: str
    label: str
    label_translations: dict[str, str] = {}
    unit: str | None = None
    min: float | int | None = None
    max: float | int | None = None


class GuardrailDTO(CamelModel):
    """One named, documented ERP scenario from a plugin's `guardrails/*.toml`
    library entries (design §3-4). Mirrors `oc8.capas.guardrails.Guardrail`
    field-for-field, same reasoning as `GuardrailAdjustableDTO` above."""

    key: str
    label: str
    label_translations: dict[str, str] = {}
    summary: str
    summary_translations: dict[str, str] = {}
    use_case: str
    read: bool = False
    modify: bool = False
    approval_eur: float | None = None
    approval_actions: list[str] = []
    #: The ONLY tool names this guardrail puts within reach; empty means all
    #: of the connection's tools. See `GuardrailPresetDTO.only` for the full
    #: rationale (a euro threshold cannot gate a deletion).
    only: list[str] = []
    adjustable: list[GuardrailAdjustableDTO] = []


class GuardrailAttributeDTO(CamelModel):
    """One named, typed value a connection's tools expose for a "with limits"
    Condition -- mirrors `oc8.capas.manifest.GuardrailAttribute`. The
    Conditions editor may only ever build a rule against an attribute
    listed here for the tool being edited; it never invents a field name."""

    key: str
    label: str
    label_translations: dict[str, str] = {}
    datatype: str = "number"
    enum_values: list[str] = []
    tools: list[str] = []


class McpLoginDTO(CamelModel):
    """A Credential-backed McpConnection -- name, department_id (None for a
    tenant-wide login, set when the login is scoped to one department),
    scopes, and connection health, never the credential's own field values
    (those are visible via GET /credentials, per that framework's own
    write-only-secrets discipline)."""

    id: str
    name: str
    credential_id: str
    department_id: str | None = None
    connected: bool
    scopes: list[str] | dict[str, object]
    health: dict[str, object] = {}


class McpConnectionDTO(CamelModel):
    id: str
    name: str
    transport: str
    server_url: str
    command: str = ""
    args: list[str] = []
    department_id: str | None = None
    connected: bool
    scopes: list[str] = []
    health: dict[str, object] = {}
    #: Presets the connection's plugin ships, resolved from its manifest on
    #: disk -- empty for a connection whose plugin ships none or is no longer
    #: installed. Carried here so the browser learns them from the response it
    #: already fetches, not a second round trip.
    guardrail_presets: list[GuardrailPresetDTO] = []
    #: The connection's plugin's own `guardrails/*.toml` library (design §3-4),
    #: resolved from disk exactly as `guardrail_presets` is -- `None` for a
    #: plugin that ships no such folder (the common case) or is no longer
    #: installed, never an empty list standing in for "none". Additive to
    #: `guardrail_presets`, which keeps serialising unchanged as the
    #: connection's fallback generic presets.
    guardrail_library: list[GuardrailDTO] | None = None
    #: Whether the manifest connection declares a `value_spec`. Only then can
    #: a euro-threshold field in a preset ever affect a decision; the spec
    #: itself stays server-side.
    has_value_spec: bool = False
    #: Named, typed attributes this connection's tools expose for "with
    #: limits" Conditions, resolved from the manifest exactly like
    #: `guardrail_presets` -- empty for a connection whose plugin declares
    #: none (predates the generic condition model) or is no longer installed.
    guardrail_attributes: list[GuardrailAttributeDTO] = []
    #: The plugin this connection was created from (the same `_plugin_name`
    #: stamp `materialise.py` writes at enable time), or None for a connection
    #: an operator created by hand via "Add connection" rather than through a
    #: plugin's setup flow.
    plugin_name: str | None = None
    #: Which credential_types/*.toml entry a login for this connection must be
    #: (`ToolPackConnection.credential_type`, resolved from the manifest on
    #: disk exactly like `guardrail_presets`) -- lets a "New login" flow pick
    #: the right credential type automatically instead of asking the operator
    #: to choose from every registered type, most of which are irrelevant to
    #: this tool. None for a connection whose plugin doesn't declare one yet
    #: (predates the Unified Credentials Framework) or is no longer installed.
    credential_type: str | None = None


class ConnectionToolNamesDTO(CamelModel):
    names: list[str] = []
    #: Same names split by `authz.pdp.required_right`'s classification, so a
    #: guardrail editor can show the one checkbox (Read xor Modify) that
    #: actually applies to a given tool instead of both -- a tool is never
    #: both at once (`required_right` fails closed to "modify" for anything
    #: unlisted in the connection's `scopes`, so `read` here is a strict
    #: subset of `names`, never the other way around).
    read: list[str] = []
    modify: list[str] = []


class RenderedComponentDTO(CamelModel):
    """One render_component call's durable record -- the raw dict stored in
    `agent_run.context["rendered_components"]` / `chat_message.
    rendered_components` (control_tools.py's `ControlOutcome.
    rendered_component`) is `{"component_key": ..., "props": ...}`, plain
    Python, not itself a CamelModel. Typing every consumer's field as THIS
    model (instead of a raw `dict[str, object]`) is what makes
    `component_key` actually reach the wire as `componentKey` -- a field
    typed as a bare dict bypasses CamelModel's alias generator entirely, so
    the frontend's `c.componentKey` lookup silently read `undefined` and
    rendered nothing, on every durable (non-live-WS) path."""

    component_key: str
    props: dict[str, object] = {}


class TodoDTO(CamelModel):
    """One item from the agent's most recent `todo_write` call (control_tools.py's
    `ControlOutcome.todos`). The list is whole-list-replace, not append-only --
    this is always the agent's current full todo list, not a history of edits."""

    content: str
    status: str


class RunDTO(CamelModel):
    id: str
    agent_id: str
    state: str
    phase: str | None = None
    output: str | None = None
    steps: int = 0
    tool_calls: list[dict[str, object]] = []
    task_id: str | None = None
    question: str | None = None
    # Durable copy of every render_component call this run made (agent/engine.py's
    # RunResult.rendered_components / internal_agent.py's ctx["rendered_components"]).
    # Unlike the live-only `run.component_rendered` WS event, this survives a page
    # reload or an unattended run nobody watched live.
    rendered_components: list[RenderedComponentDTO] = []
    # Same durability reasoning as rendered_components, for the agent's most
    # recent todo_write call (agent/engine.py's RunResult.todos / internal_agent
    # .py's ctx["todos"]). Empty means either the agent never called todo_write,
    # or it cleared the list on its last call -- both render as "no todos".
    todos: list[TodoDTO] = []


class ChatSessionDTO(CamelModel):
    id: str
    agent_id: str
    title: str
    created_at: str
    last_message_at: str | None = None


class FileAttachmentDTO(CamelModel):
    id: str
    filename: str
    content_type: str
    size_bytes: int
    is_image: bool
    created_at: str


class ChatMessageDTO(CamelModel):
    id: str
    session_id: str
    role: str
    content: str
    run_id: str | None = None
    rendered_components: list[RenderedComponentDTO] = []
    created_at: str
    attachments: list[FileAttachmentDTO] = []


class ReportDTO(CamelModel):
    """One finished run that rendered at least one component -- the "My
    work" Reports list reads this, not `RunDTO` directly, since a report is
    read across MANY agents/runs at once rather than one run in focus."""

    run_id: str
    agent_id: str
    agent_name: str
    created_at: str
    rendered_components: list[RenderedComponentDTO] = []


class WorkspaceFileDTO(CamelModel):
    id: str
    filename: str
    content_type: str
    size_bytes: int
    run_id: str
    created_at: str


class WorkspaceFilesDTO(CamelModel):
    files: list[WorkspaceFileDTO] = []


class BudgetDTO(CamelModel):
    id: str
    department_id: str | None
    soft_limit_tokens: int | None
    hard_limit_tokens: int | None
    dollar_budget_usd: float | None = None
    dollar_reference_provider: str | None = None
    dollar_reference_model: str | None = None


class BudgetStatusDTO(CamelModel):
    scope: str  # "tenant" | "department"
    department_id: str | None
    soft_limit_tokens: int | None
    hard_limit_tokens: int | None
    current_tokens: int
    soft_exceeded: bool
    hard_exceeded: bool


class TriggerDTO(CamelModel):
    id: str
    agent_id: str
    kind: str  # cron | event | webhook
    task_text: str
    enabled: bool
    cron_expression: str | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    event_source: str | None = None
    event_type: str | None = None
    #: kind='webhook' only -- the full POST /webhooks/{token} URL, built
    #: server-side from the same base URL external OAuth redirects use
    #: (oauth_redirect_base_url). Not one-time-reveal: unlike a password, an
    #: operator needs to come back and re-copy this into the external
    #: system's config, possibly more than once.
    webhook_url: str | None = None


class AuthConfig(CamelModel):
    mode: str  # "dev" | "community"
    auth_server_url: str
    realm: str
    client_id: str
    #: Whether this instance already has an administrator, so the browser can
    #: show the login form instead of the first-run setup form. Without it the
    #: login page had to guess, guessed "not initialized" every time, and an
    #: operator whose session expired was asked to create an account that
    #: already existed -- discovering the truth only from the 422 that
    #: followed. Only meaningful for `community`; `false` for `dev`, which
    #: does not have a setup form at all.
    initialized: bool = False
    #: True when `OC8_DEMO=true`: the ACME showcase seed (bilingual mock data)
    #: is what this instance is for. The UI can show a Demo badge; it does not
    #: by itself enable unauthenticated login -- that still needs `mode=dev`.
    demo: bool = False
    #: The identity provider's external SSO logout endpoint (RP-initiated
    #: logout), if it has one. None for the dev/community provider, which has
    #: no external session to terminate.
    sso_logout_url: str | None = None


class SecretDTO(CamelModel):
    """Secret metadata only — the plaintext value is never serialized or returned."""

    id: str
    name: str
    kind: str
    key_version: str
    created_at: str


class CredentialDTO(CamelModel):
    """Credential metadata -- never the secret field values. Non-secret
    field values ARE included (they were never secret in the first place;
    an S3 credential's region is not sensitive)."""

    id: str
    name: str
    credential_type: str
    field_values: dict[str, Any]
    last_tested_at: str | None
    last_test_ok: bool | None
    created_at: str
    updated_at: str


class CredentialTypeDTO(CamelModel):
    name: str
    display_name: str
    display_name_translations: dict[str, str] = {}
    fields: list[dict[str, Any]]


class AuditEventDTO(CamelModel):
    id: str
    seq: int
    ts: str
    actor_type: str
    actor_id: str | None = None
    category: str
    action: str
    resource: dict[str, Any]
    decision: str | None = None
    reason: str | None = None
    responsible_type: str | None = None
    responsible_id: str | None = None
    hash: str
    prev_hash: str


class AuditPageDTO(CamelModel):
    events: list[AuditEventDTO]
    next_before_seq: int | None = None


class AuditIntegrityDTO(CamelModel):
    status: str  # ok | broken | unverifiable | never
    verified_through_seq: int
    head_hash: str | None = None
    verified_at: str | None = None
    broken_at_seq: int | None = None
    # hash_mismatch | truncation, None unless status is "broken". The two need
    # different operator advice: a full re-verification is the remedy path for
    # a hash mismatch after a legitimate restore, but it can never clear a
    # truncation -- only the missing rows coming back does that.
    break_kind: str | None = None
    # Write-once residue: when a break was FIRST observed. Survives a later
    # successful full verification (which may legitimately set status back to
    # "ok"), so the operator screen can keep showing that integrity was once
    # in doubt.
    first_break_at: str | None = None
    # COUNT, not a seq. A seq range implies a claim about WHICH entries are
    # gone -- true for a head/trailing truncation, false for a deletion in the
    # middle of the chain (broken_at_seq there is just "the last verified
    # position", not "everything after this is missing"). The count is exact
    # either way: verified_count minus how many of those rows are still
    # present. None unless break_kind is "truncation".
    missing_count: int | None = None
    event_count: int


class OAuthStartDTO(CamelModel):
    authorization_url: str


class OAuthConnectionDTO(CamelModel):
    """Connection metadata only — token material is never serialized."""

    id: str
    provider: str
    account_label: str
    scopes: list[str]
    status: str
    client_source: str
    expires_at: str | None = None
    created_at: str


class DeviceLoginStartDTO(CamelModel):
    """What `POST /models/chatgpt-subscription/device/start` hands the
    frontend to show the user-facing code and start polling (Task 12).
    `expires_in` is used client-side to compute the `expiresAt` deadline
    echoed back on every `/device/poll` call -- see `DeviceLoginPollDTO`."""

    device_auth_id: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceLoginPollDTO(CamelModel):
    """One poll attempt's outcome. Stateless on this end -- see
    `poll_chatgpt_device_login`'s docstring in catalog.py for why the
    `expired` deadline lives entirely on the request, not here."""

    status: Literal["pending", "complete", "expired", "error"]
    #: Set only when status == "complete".
    credential_id: str | None = None
    #: Set only when status == "error".
    error: str | None = None


class PermissionInfoDTO(CamelModel):
    """One right, in words the person ticking the box can act on.

    The refused ones are RETURNED with their reason rather than filtered out. A
    hidden control produces a support ticket asking where the setting went;
    a disabled one with a sentence beside it answers the question on the screen
    where it was asked.
    """

    permission: str
    label: str
    description: str = ""
    label_de: str = ""
    description_de: str = ""
    delegatable: bool
    #: Why a tenant-defined role may not hold it. Empty when it may.
    reason: str = ""


class RoleHolderDTO(CamelModel):
    """One person holding a role -- the blast radius of an edit, by name."""

    member_id: str
    subject: str
    display_name: str = ""


class RoleSummaryDTO(CamelModel):
    """A role on the list screen.

    `id` is null for a built-in role this tenant has no `role` row for. Both
    spellings exist in the wild -- provisioning writes five rows and Globex has
    one -- and the built-in ladder is identical either way because it is compiled
    in, so the list is built from code and the id is carried through only when
    there happens to be a row to address.
    """

    id: str | None = None
    name: str
    description: str = ""
    kind: str = "human"
    builtin: bool = False
    #: False for every built-in: their grants come from code, so an editor over
    #: one would save cleanly and change nothing at any gate.
    can_edit: bool = True
    #: How many people this edit would change. Shown BEFORE Save.
    holder_count: int = 0
    permissions: list[str] = []


class RoleDetailDTO(RoleSummaryDTO):
    """A role and the people holding it, which is what an edit is about.

    `holders` is a PREVIEW and `holder_count` is exact, and the asymmetry is the
    point: "how many people does this change" is the number an administrator is
    asked to decide on, and "which of them" is a list somebody reads. This DTO is
    the response model of `POST /roles`, `PUT /roles/{id}`, `DELETE /roles/{id}`
    and `PUT /members/{id}/role`, so an unbounded list put four hundred names into
    the body of every O(1) write on the five-hundred-person tenant, and four
    hundred chips on the screen. The cap is `api/v1/roles.HOLDER_PREVIEW`; the
    screen says how many it is showing whenever the two differ.
    """

    holders: list[RoleHolderDTO] = []


class RuntimeOptionDTO(CamelModel):
    """One entry in the runtime picker `GET /runtimes` returns.

    `id` is null for the built-in default -- there is no plugin row to name --
    and that entry is ALWAYS present and ALWAYS `available`, so a tenant that
    has installed no runtime plugin at all still has something to hire an
    agent onto. A plugin whose implementation cannot be loaded stays in the
    list with `available=False` rather than being dropped, so an
    administrator who installed it can see it and learn why it does not work
    instead of wondering where it went.
    """

    id: str | None
    name: str
    label: str
    summary: str
    capabilities: list[str]
    is_default: bool
    available: bool
    unavailable_reason: str | None = None


class VapidPublicKeyDTO(CamelModel):
    public_key: str


class GuardrailInterpretationDTO(CamelModel):
    """The free-text interpreter's structured suggestion -- one of the same
    4 states the manual editor writes (`self_sufficient`/`with_limits`/
    `approval_required`/`not_allowed`), plus `conditions` when `decision ==
    "with_limits"`. This IS the artifact the operator reviews and must
    explicitly accept before it is merged into the draft policy; no euro-
    only shape survives here -- see `ConditionDTO`."""

    decision: str
    conditions: list[ConditionDTO] = []


class FunctionGuardrailInterpretationDTO(GuardrailInterpretationDTO):
    """One function's entry in a `GuardrailBatchInterpretationDTO` -- same
    shape as a single free-text interpretation, plus which function it's
    for."""

    function: str


class GuardrailBatchInterpretationDTO(CamelModel):
    """The instruction-driven interpreter's suggestions for a whole
    connection in one call -- one entry per function it decided to restrict.
    A function absent here got no suggestion (`self_sufficient`, i.e. leave
    it at whatever the department/inherited policy already allows); the
    operator still reviews and accepts each entry individually, exactly like
    a single `GuardrailInterpretationDTO`."""

    results: list[FunctionGuardrailInterpretationDTO] = []


class WidgetInstanceDTO(CamelModel):
    """Write-path shape (`PutDashboardLayoutRequest`, the dashboard
    templates below): `type` is validated against the currently known
    widget types, so `PUT /dashboard/layout` correctly rejects an
    unknown/misspelled one outright. See `WidgetInstanceReadDTO` for the
    read-path counterpart, which deliberately does NOT share this
    constraint.
    """

    id: str
    type: Literal["chat", "approvals", "reports", "budget", "activity", "tasks"]
    x: int
    y: int
    w: int
    h: int
    config: dict[str, Any] = Field(default_factory=dict)


class WidgetInstanceReadDTO(CamelModel):
    """Read-path shape for a stored widget instance: `type` is a plain
    `str`, not the write path's `Literal`. A widget instance can outlive its
    own type's registration -- a type renamed or retired while a member
    still has a tile of it stored -- and a `Literal` here would turn
    `GET /dashboard/layout` into a permanent 500 for that member, with no
    way to even load the dashboard to remove the offending tile. The
    frontend already renders a neutral fallback for an unrecognized type
    (`widget-frame.tsx`); this is what lets that fallback ever run.
    """

    id: str
    type: str
    x: int
    y: int
    w: int
    h: int
    config: dict[str, Any] = Field(default_factory=dict)


class DashboardLayoutDTO(CamelModel):
    widgets: list[WidgetInstanceReadDTO] = Field(max_length=50)
    template_id: str | None = None


class DashboardTemplateDTO(CamelModel):
    id: str
    name: dict[str, str]
    widgets: list[WidgetInstanceDTO]


class DashboardPresetDTO(CamelModel):
    id: str
    name: str
    widgets: list[WidgetInstanceReadDTO]
    scope: Literal["personal", "tenant"]
    #: Whether the CALLER is this preset's creator -- lets the frontend show a
    #: delete control without a second round trip. Deliberately not "may
    #: delete": a tenant-scoped preset is also deletable by anyone holding
    #: `settings:manage`, which the frontend already knows from `/governance`
    #: and can combine with this locally.
    mine: bool


class CoreLocaleDTO(CamelModel):
    locale: str
    native_name: str
    flag: str
    translations: dict[str, str]


class CoreI18nCatalogDTO(CamelModel):
    #: Every non-English locale a `.po` catalog exists for. English itself
    #: is never a key here -- it is the `msgid` source text the frontend
    #: already has inline, needing no lookup at all.
    locales: list[CoreLocaleDTO]
