// TanStack Query hooks over the oc8 control-plane API. Return shapes match the
// existing TypeScript interfaces so screens swap mock imports for these hooks.

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  deleteFile,
  fetchCapaIcon,
  listInstructionFiles,
  previewBackup,
  previewCapaExport,
  restoreBackup,
  uploadInstructionFile,
  type CapaExportItemInput,
  type FileAttachmentDTO,
} from "@/lib/api";
import type { RuntimeOption } from "@/components/runtime-picker";
import type { GuardrailLibraryEntry, GuardrailPreset } from "@/components/guardrail-preset-picker";
import { mcpTestLogKey, type McpTestLogLine } from "@/lib/live/apply-event";
import type {
  ActivityItem,
  Agent,
  DataSource,
  Department,
  Integration,
  KbChunk,
  KnowledgeBase,
  KnowledgeDocument,
  SimilarChunk,
  Task,
} from "@/lib/mock-data";
import type { Skill } from "@/lib/skills";
import { useLang } from "@/lib/i18n";
import {
  localizeActivity,
  localizeAgent,
  localizeDepartment,
  localizeKnowledgeBase,
  localizeLabeled,
  localizeSkill,
  localizeTask,
} from "@/lib/localize-demo";

export interface ToolPolicy {
  enabled: boolean;
  read: boolean;
  modify: boolean;
  approvalEur: number | null;
  approvalActions: string[];
  only: string[] | null;
  connectionId?: string | null;
  // Generic "with limits" rules -- see `Condition`
  // (@/components/guardrail-preset-picker). Mirrors `ToolPolicyDTO.conditions`.
  conditions?: import("@/components/guardrail-preset-picker").Condition[];
}

export interface AgentDetail extends Agent {
  mission: string;
  departmentName: string | null;
  effectiveTools: Record<string, ToolPolicy>;
  departmentFrameTools: Record<string, ToolPolicy>;
  missionTranslations?: Record<string, string>;
}

export interface ApprovalOption {
  key: string;
  label: string;
  detail: string;
}

export interface Approval {
  id: string;
  agentId: string;
  title: string;
  detail: string;
  amount: string | null;
  time: string;
  status: string;
  // "decision" approvals were raised by an agent that hit something it may not
  // decide alone; they carry the alternatives it proposed. Everything else is a
  // plain yes/no on an action it is holding.
  actionType?: string;
  options?: ApprovalOption[];
  recommendation?: string | null;
  decisionOption?: string | null;
  // Only meaningful on a decision response: whether the suspended run was
  // actually resumed. False means the decision was recorded but the held action
  // never ran (no run left to resume), which the operator must be told rather
  // than shown a bare "Approved".
  resumed?: boolean;

  // ---- department-scoped workspace (§5, additive) ----
  // `null` means TENANT-WIDE, which today is only the budget incident and is
  // shown only to somebody unrestricted. Do not treat it as "unknown".
  departmentId?: string | null;
  departmentName?: string;
  agentName?: string;
  taskId?: string | null;
  taskTitle?: string;
  // ISO-8601. `time` above is NOT this: it comes from payload["time"], which
  // only the demo seed writes, so it is "" on every real row. Age on a row has
  // to be computed from `createdAt` or it says nothing.
  createdAt?: string;
  decidedByName?: string;
  // What is actually being held. The whole reason the detail pane exists.
  toolName?: string | null;
  toolArguments?: Record<string, unknown>;
  titleTranslations?: Record<string, string>;
  detailTranslations?: Record<string, string>;
  // Structured "why" behind a PDP-raised approval -- `ApprovalDTO.reasonContext`
  // verbatim: `{code: "condition_matched" | "always_requires_approval" |
  // "value_threshold_exceeded", ...}`. `null`/absent for approvals not raised
  // via `authorize_tool_call` (e.g. agent decision requests) -- `detail`
  // remains the only "why" for those.
  reasonContext?: Record<string, unknown> | null;
}

/** A question an agent parked mid-run. The other half of the workspace queue. */
export interface Clarification {
  id: string;
  runId: string;
  agentId: string;
  agentName: string;
  departmentId: string;
  departmentName: string;
  question: string;
  status: string;
  createdAt: string;
}

/** One card on the My Work task board -- across every department the caller
 * can see, unlike the department detail page's board which is scoped to one. */
export interface TaskBoardRow {
  id: string;
  title: string;
  state: string;
  column: "backlog" | "in_progress" | "waiting" | "done";
  departmentId: string;
  departmentName: string;
  agentId: string | null;
  agentName: string | null;
  requestedByMemberId: string | null;
  parentTaskId: string | null;
  delegationDepth: number;
  createdAt: string;
}

export interface ClarificationAnswer {
  id: string;
  runId: string;
  status: string;
  answer: string;
}

/** One person standing in one department. */
export interface Seat {
  departmentId: string;
  departmentName: string;
  seatRole: string; // dept_viewer | dept_approver
  /** WRITE authority over Agent, in THIS department only. Independent of
   *  `seatRole` — a dept_viewer may hold it, a dept_approver may not — and
   *  never folded into a permission string: it never appears in
   *  `callerPermissions`. See `useMayManageAgent` (governance-hooks.ts). */
  agentManage: boolean;
}

export interface McpConnection {
  id: string;
  name: string;
  transport: string;
  serverUrl: string;
  command: string;
  args: string[];
  departmentId: string | null;
  connected: boolean;
  scopes: string[];
  health: Record<string, unknown>;
  // Presets the connection's plugin ships, resolved server-side from its
  // manifest -- empty for a plugin that ships none. See
  // `@/components/guardrail-preset-picker` for how these become a policy.
  guardrailPresets: GuardrailPreset[];
  // The connection's plugin's own `guardrails/*.toml` library (design §3-6),
  // resolved server-side exactly as `guardrailPresets` is -- `null` for a
  // plugin that ships no such folder (the common case today) or is no longer
  // installed, never an empty array standing in for "none". When present,
  // `GuardrailPresetPicker` replaces the generic `guardrailPresets` list
  // with this one grouped by `useCase` -- see that component.
  guardrailLibrary: GuardrailLibraryEntry[] | null;
  // Whether the manifest connection declares a `value_spec`, i.e. whether
  // an approval-euro threshold can ever affect a decision for this
  // connection.
  hasValueSpec: boolean;
  // The plugin this connection was set up from (matched server-side via the
  // config["_plugin_name"] stamp `CapaSetupDialog`'s flow writes) -- null
  // for a connection created by hand via "Add connection", never through a
  // Capa's setup flow. Lets the Capas page fold a connection's live status
  // into its owning Capa's own card instead of listing it separately.
  pluginName: string | null;
  // Which credential_types/*.toml entry a login for this connection must be
  // (the manifest's ToolPackConnection.credential_type, resolved server-side
  // exactly like guardrailPresets) -- null for a connection whose plugin
  // doesn't declare one yet or is no longer installed. Lets a "New login"
  // flow skip asking the operator to pick a credential type from every
  // registered one, most of which are irrelevant to this tool.
  credentialType: string | null;
  // Which attributes a Conditions editor may build a "with limits" rule
  // against for this connection -- mirrors `GuardrailAttributeDTO`
  // (backend/src/oc8/schemas/dto.py), CAPA-declared per connection/tool-action
  // (`ToolPackConnection.guardrail_attributes`, capas/manifest.py). The
  // editor must never offer an attribute absent here: this list IS the
  // "technically available and evaluable" boundary the generic Condition
  // model (design correction #2) requires.
  guardrailAttributes: GuardrailAttribute[];
}

export interface GuardrailAttribute {
  key: string;
  label: string;
  labelTranslations: Record<string, string>;
  datatype: "number" | "string" | "boolean" | "enum";
  enumValues: string[];
  // Scopes this attribute to specific tool names; empty means every tool on
  // this connection may expose it.
  tools: string[];
}

// Model config DTO (§ model registry). Provider/locality are free-form
// strings backed by the registry, not the mock catalog's fixed union — the
// `model` field is the free-text provider tag (e.g. "llama3.1:8b").
export interface ModelDTO {
  id: string;
  provider: string;
  name: string;
  status: string;
  costTier: string;
  latency: string;
  assignedTo: string[];
  note: string;
  model: string;
  locality: "cloud" | "local";
  displayName: string | null;
  // The completion budget resolve_params falls back to for any agent on this
  // model that sets no per-agent override. null means "framework default"
  // (1536), not "unlimited".
  maxTokens?: number | null;
  // Free-form reasoning-effort knob forwarded verbatim to providers that
  // support it (e.g. Anthropic). Not validated against a fixed set of
  // values -- the provider owns what it accepts and that changes on its
  // own schedule. null means "not set".
  effort?: string | null;
  // Free-form top-level request fields forwarded verbatim after every named
  // field (e.g. OpenRouter's `provider`/`top_p`) -- oc8 has no schema for
  // these, the provider does. null/undefined means "none set".
  extra?: Record<string, unknown> | null;
  usedByCopilot: boolean;
  supportsVision?: boolean;
  credentialId: string | null;
  healthError: string | null;
  healthCheckedAt: string | null;
}

export interface ModelProviderDTO {
  canonical: string;
  locality: string;
  available: boolean;
}

export interface OrganizationSettingsDTO {
  id: string;
  name: string;
  slug: string;
  tier: string;
  region: string;
  // Deliberately snake_case, unlike every field above it: settings.py's
  // `OrganizationSettings`/`OrganizationSettingsUpdate` are plain pydantic
  // BaseModel, not this codebase's CamelModel, so the wire key really is
  // `active_smtp_credential_id` -- sending `activeSmtpCredentialId` would be
  // silently dropped (unknown key) rather than rejected. `null`/omitted from
  // GET means no mail server is configured (task-8-brief.md). Optional here
  // (not on every other field) only so existing fixtures that predate this
  // task (e.g. backup-panel.test.tsx's ORG) don't have to be touched.
  active_smtp_credential_id?: string | null;
}

export interface ModelWriteBody {
  provider: string;
  model: string;
  locality: string;
  displayName?: string;
  maxTokens?: number;
  effort?: string;
  extra?: Record<string, unknown>;
  usedByCopilot?: boolean;
  supportsVision?: boolean;
  credentialId?: string | null;
}

export interface RunComponentDTO {
  componentKey: string;
  props: Record<string, unknown>;
}

export interface RunTodoDTO {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

export interface RunDTO {
  id: string;
  agentId: string;
  state: string; // queued | running | waiting_for_input | waiting_for_approval | done | failed
  question?: string;
  phase?: string | null;
  output?: string | null;
  steps: number;
  toolCalls: Array<Record<string, unknown>>;
  taskId?: string | null;
  // From the agent's most recent todo_write call (whole-list-replace, not an
  // append log — same durability as toolCalls/steps, present from the initial
  // GET /runs/{id} fetch, no WS-live patcher exists for it yet).
  todos?: RunTodoDTO[];
  // Never returned by the backend -- populated client-side only, by the
  // "run.output_delta" live-event patcher (lib/live/apply-event.ts) as chunks
  // arrive over the WS. Absent until the first delta lands, so a fresh
  // GET /runs/{id} (e.g. on navigation) starts a run with no transcript yet.
  transcript?: string[];
  // Same shape as transcript above: never returned by GET /runs/{id}, filled
  // in only by the "run.component_rendered" live-event patcher as the agent
  // calls render_component during the run.
  components?: RunComponentDTO[];
  // The model's own answer as it streams in (Stage 2 token streaming),
  // filled in by the "run.token_delta" patcher. Unlike `transcript` above
  // (an ARRAY of separate stdout lines, container runtimes only) this is
  // ONE concatenated string -- token fragments are meant to run together
  // into flowing prose, not sit on their own lines -- and it applies to
  // every runtime, in-process or isolated. Same "never from GET, WS-only"
  // rule: absent until the first fragment lands.
  liveAnswer?: string;
}

const keys = {
  departments: ["departments"] as const,
  agents: ["agents"] as const,
  agent: (id: string) => ["agents", id] as const,
  board: (id: string) => ["departments", id, "board"] as const,
  models: ["models"] as const,
  integrations: ["integrations"] as const,
  skills: ["skills"] as const,
  bases: ["knowledge", "bases"] as const,
  sources: ["knowledge", "sources"] as const,
  activity: ["activity"] as const,
  approvals: (status: string) => ["approvals", status] as const,
  clarifications: ["clarifications"] as const,
  tasks: (departmentId?: string) => ["tasks", departmentId ?? "all"] as const,
  mcp: ["mcp", "connections"] as const,
};

// ---- Shared search/filter/group/pagination contract (Design System
// Consistency plan, spec §1.1) -- consumed by every hook below that reads a
// `Page[T]` envelope, and re-exported for `roles-hooks.ts`'s `useAssignees`
// so the query-building logic exists in exactly one place. ----

export interface ListQueryParams {
  search?: string;
  filters?: Record<string, string>;
  groupBy?: string | null;
  includeArchived?: boolean;
  page?: number; // 1-indexed
  pageSize?: number;
}

export interface Page<T> {
  items: T[];
  totalCount: number;
}

// The `groupBy` field's WIRE name is not uniform across the 7 backend
// list-query endpoints (Tasks 3-9): `/agents`, `/departments`, and `/members`
// declare it `Query(alias="groupBy")`, while `/skills`, `/knowledge/bases`,
// `/knowledge/sources`, and `/capas/available` accept it unaliased as
// `group_by` (backend/src/oc8/api/v1/{catalog,knowledge,capas}.py). Every
// other field (search/limit/offset) is spelled the same everywhere, so only
// this one key is a per-call-site parameter rather than a hardcoded default.
function toQueryString(params: ListQueryParams = {}, groupByKey = "groupBy"): string {
  const q = new URLSearchParams();
  if (params.search) q.set("search", params.search);
  for (const [k, v] of Object.entries(params.filters ?? {})) {
    if (v) q.set(k, v);
  }
  if (params.groupBy) q.set(groupByKey, params.groupBy);
  if (params.includeArchived) q.set("includeArchived", "true");
  const pageSize = params.pageSize ?? 20;
  q.set("limit", String(pageSize));
  q.set("offset", String(((params.page ?? 1) - 1) * pageSize));
  return q.toString();
}

export { toQueryString };

export const useDepartments = (params: ListQueryParams = {}) => {
  const { lang } = useLang();
  return useQuery({
    queryKey: [...keys.departments, params] as const,
    // `groupBy` alias -- `/departments` matches `/agents`/`/members`.
    queryFn: () => api.get<Page<Department>>(`/departments?${toQueryString(params)}`),
    select: (page) => ({
      ...page,
      items: page.items.map((d) => localizeDepartment(d, lang)),
    }),
  });
};

/** "dev" | "community" -- which login mode this instance runs. The user
 *  detail page reads this to decide whether to show password controls: they
 *  render only in "community" mode, this edition's one real login door.
 *  `demo` is true when `OC8_DEMO=true` (ACME showcase seed). */
export const useAuthConfig = () =>
  useQuery({
    queryKey: ["auth", "config"] as const,
    queryFn: () =>
      api.get<{ mode: "dev" | "community"; demo?: boolean; initialized?: boolean }>("/auth/config"),
    staleTime: Infinity,
  });

export const useDepartment = (id: string) => {
  const { lang } = useLang();
  return useQuery({
    queryKey: ["departments", id],
    queryFn: () => api.get<Department>(`/departments/${id}`),
    select: (d) => localizeDepartment(d, lang),
  });
};

export const useAgents = (params: ListQueryParams = {}) => {
  const { lang } = useLang();
  return useQuery({
    queryKey: [...keys.agents, params] as const,
    // `groupBy` alias -- see `toQueryString`'s doc comment.
    queryFn: () => api.get<Page<Agent>>(`/agents?${toQueryString(params)}`),
    select: (page) => ({
      ...page,
      items: page.items.map((a) => localizeAgent(a, lang)),
    }),
  });
};

export const useAgent = (id: string) => {
  const { lang } = useLang();
  return useQuery({
    queryKey: keys.agent(id),
    queryFn: () => api.get<AgentDetail>(`/agents/${id}`),
    select: (a) => {
      const localized = localizeAgent(a, lang) as AgentDetail;
      if (lang !== "en" && a.missionTranslations) {
        const mission = a.missionTranslations[lang];
        if (mission) return { ...localized, mission };
      }
      return localized;
    },
  });
};

export interface WorkspaceFileDTO {
  id: string; // FileAttachment id -- download via GET /files/{id}
  filename: string;
  contentType: string;
  sizeBytes: number;
  runId: string; // which of the agent's runs produced this file
  createdAt: string;
}

export interface WorkspaceFilesDTO {
  files: WorkspaceFileDTO[];
}

export const useAgentWorkspaceFiles = (agentId: string) =>
  useQuery({
    queryKey: ["agents", agentId, "workspace", "files"],
    queryFn: () => api.get<WorkspaceFilesDTO>(`/agents/${agentId}/workspace/files`),
    enabled: !!agentId,
  });

export interface Board {
  tasks: Task[];
  // Per column, INCLUDING what this page left out — without it a truncated
  // board is indistinguishable from a finished one.
  totals: Record<string, number>;
}

// `limit` is per column, so a busy "done" column cannot crowd out the two tasks
// actually in progress.
export const useDepartmentBoard = (id: string, limit = 25) => {
  const { lang } = useLang();
  return useQuery({
    queryKey: [...keys.board(id), limit],
    queryFn: () => api.get<Board>(`/departments/${id}/board?limit=${limit}`),
    select: (board) => ({
      ...board,
      tasks: board.tasks.map((t) => localizeTask(t, lang)),
    }),
  });
};

export interface DepartmentToolsDTO {
  tools: Record<string, Record<string, unknown>>;
  //: tool key -> count of agents in this department whose narrowing
  //: overrides that key -- see backend departments.py's `_deviation_counts`.
  deviationCounts: Record<string, number>;
  //: total agents in the department, the "M" half of "N of M".
  agentCount: number;
}

export function useDepartmentTools(id: string) {
  return useQuery({
    queryKey: ["departments", id, "tools"],
    queryFn: () => api.get<DepartmentToolsDTO>(`/departments/${id}/tools`),
    enabled: !!id,
    // A short staleTime, not zero: `useSetDepartmentTools` already
    // invalidates this exact key on every successful save, so a save made
    // from this browser tab is never stale. This only avoids re-fetching
    // on every remount (e.g. switching away from and back to the
    // Guardrails tab) within the same short window.
    staleTime: 15 * 1000,
  });
}

export function useSetDepartmentTools(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (tools: Record<string, Record<string, unknown>>) =>
      api.put<DepartmentToolsDTO>(`/departments/${id}/tools`, { tools }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["departments", id, "tools"] }),
  });
}

export const useDepartmentAgents = (id: string) =>
  useQuery({
    queryKey: ["departments", id, "agents"],
    queryFn: () => api.get<Agent[]>(`/departments/${id}/agents`),
    enabled: !!id,
  });

export interface CurrentUser {
  subject: string;
  role: string;
  kind: string;
  // ---- department-scoped workspace (§5, additive; every field has a default
  // on the wire, so an older backend simply leaves them falsy) ----
  tenantId?: string;
  displayName?: string;
  /** `null` only for a principal that cannot stand in a department at all. */
  memberId?: string | null;
  /** SEES everywhere, including departments created after login. Not the same
   *  person as `decidesAllDepartments`: an `auditor` holds `approval:view_any`
   *  and no decide right at all. */
  viewsAllDepartments?: boolean;
  /** SIGNS OFF everywhere. Absent from an older backend, which is why every
   *  reader must treat it as `=== true` rather than as "not false". */
  decidesAllDepartments?: boolean;
  seats?: Seat[];
  /** Drives the /welcome redirect (design: gamified-onboarding-wizard-design.md). */
  onboardingStatus?: "pending" | "completed" | "skipped";
}

export function useAuth() {
  return useQuery({ queryKey: ["auth", "me"], queryFn: () => api.get<CurrentUser>("/me") });
}

// `useIsAdmin()` lived here and is DELETED, not deprecated. It was
// `data?.role === "org_admin"` — a comparison of the TOKEN's role string, which
// is the one place the frontend bypassed the permission model. Once a role can
// be assigned on a row, that comparison is wrong in both directions: an
// administrator demoted to a tenant role still saw every admin control (his
// token still says `org_admin`), and any assigned role however privileged
// silently lost all of them. Use `useMay(permission)` from
// `@/lib/governance-hooks`, which reads the RESOLVED set and keeps the same
// fail-safe-hide behaviour while the answer is unknown.

/** Where the caller stands, with the two answers the workspace has to tell apart.
 *
 * `unassigned` is NOT "the queue is empty": it is "nobody has put this person in
 * a department". They are the same blank screen today, and one of them is the
 * system working while the other is somebody locked out of their own job. It is
 * read from `/me` rather than inferred from a 403, because a seat-holder whose
 * queue is quiet gets a 200 with `[]` and must not be told he has no seat.
 *
 * `pending` keeps the screen from claiming either answer before `/me` has landed.
 */
export function useStanding(): {
  pending: boolean;
  seats: Seat[];
  unrestricted: boolean;
  decidesEverywhere: boolean;
  unassigned: boolean;
  displayName: string;
  role: string;
} {
  const { data, isPending, isSuccess } = useAuth();
  const seats = data?.seats ?? [];
  const unrestricted = data?.viewsAllDepartments === true;
  return {
    pending: isPending,
    seats,
    unrestricted,
    // Kept apart from `unrestricted` because the backend keeps them apart, and
    // for its one holder: an `auditor` sees every department and may decide in
    // none. Collapsing the two drew Approve and Reject on every row for the one
    // role whose whole value is that its account cannot have caused what it is
    // auditing, and every click 403'd.
    decidesEverywhere: data?.decidesAllDepartments === true,
    // `isSuccess`, not `!isPending`: a failed /me leaves the same empty `seats`
    // as a genuinely seatless person, and telling somebody "nobody has assigned
    // you to a department" because the network dropped is the exact class of lie
    // this pair of states exists to stop.
    unassigned: isSuccess && !unrestricted && seats.length === 0,
    displayName: data?.displayName || data?.subject || "",
    role: data?.role ?? "",
  };
}

export function useOrganizationSettings() {
  return useQuery({
    queryKey: ["settings", "organization"],
    queryFn: () => api.get<OrganizationSettingsDTO>("/settings/organization"),
  });
}

export function useUpdateOrganizationSettings() {
  const qc = useQueryClient();
  return useMutation({
    // `active_smtp_credential_id` stays optional (Pick, not Required) so the
    // existing OrganizationPanel save (name/region only) keeps omitting it --
    // per settings.py's `model_fields_set` check, omitting the key leaves the
    // mail-server pointer untouched, while including it with `null` clears it.
    mutationFn: (
      body: Pick<OrganizationSettingsDTO, "name" | "region"> &
        Partial<Pick<OrganizationSettingsDTO, "active_smtp_credential_id">>,
    ) => api.put<OrganizationSettingsDTO>("/settings/organization", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings", "organization"] }),
  });
}

export const useModels = () =>
  useQuery({ queryKey: keys.models, queryFn: () => api.get<ModelDTO[]>("/models") });

export const useModelProviders = () =>
  useQuery({
    queryKey: ["model-providers"],
    queryFn: () => api.get<ModelProviderDTO[]>("/models/providers"),
  });

export const useIntegrations = () =>
  useQuery({ queryKey: keys.integrations, queryFn: () => api.get<Integration[]>("/integrations") });

export const useSkills = (params: ListQueryParams = {}) => {
  const { lang } = useLang();
  return useQuery({
    queryKey: [...keys.skills, params] as const,
    // Unaliased `group_by` on the wire -- see `toQueryString`'s doc comment.
    queryFn: () => api.get<Page<Skill>>(`/skills?${toQueryString(params, "group_by")}`),
    select: (page) => ({
      ...page,
      items: page.items.map((s) => localizeSkill(s, lang)),
    }),
  });
};

export const useKnowledgeBases = (params: ListQueryParams = {}) => {
  const { lang } = useLang();
  return useQuery({
    queryKey: [...keys.bases, params] as const,
    // Unaliased `group_by` on the wire -- see `toQueryString`'s doc comment.
    queryFn: () =>
      api.get<Page<KnowledgeBase>>(`/knowledge/bases?${toQueryString(params, "group_by")}`),
    select: (page) => ({
      ...page,
      items: page.items.map((kb) => localizeKnowledgeBase(kb, lang)),
    }),
  });
};

/** Create a KB in the control plane (§11).  Sources and grants are separate
 * resources, so callers must not pretend that a local wizard selection has
 * already attached either one. */
export function useCreateKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description?: string; embeddingModel?: string }) =>
      api.post<KnowledgeBase>("/knowledge/bases", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.bases }),
  });
}

// PATCH /knowledge/bases/{id} -- name/description only. Not sensitivity,
// embeddingModel, or sourceIds: those are set at creation, changed by a sync,
// or (deliberately) not editable at all, matching the backend's
// UpdateKnowledgeBaseRequest.
export function useUpdateKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      kbId,
      ...body
    }: {
      kbId: string;
      name?: string;
      description?: string;
      embeddingModel?: string;
    }) => api.patch<KnowledgeBase>(`/knowledge/bases/${kbId}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.bases }),
  });
}

// DELETE /knowledge/bases/{kbId} -- irreversible (§12.5.1): tombstones the
// whole base and every chunk any source ever contributed to it, whichever
// sources those were. Does not touch the sources themselves -- they can
// still feed other bases, or be synced into a new one.
export function useDeleteKnowledgeBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (kbId: string) =>
      api.delete<{ kbId: string; documents: number; chunks: number }>(`/knowledge/bases/${kbId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.bases });
      qc.invalidateQueries({ queryKey: keys.sources });
    },
  });
}

// DELETE /knowledge/bases/{kbId}/sources/{sourceId} -- removes one source's
// content from one base, the reverse of useSyncSource's `kbId` param.
// Neither the source nor the base is deleted; a retry on an already-unlinked
// pair is a 200 no-op, not an error. Invalidates both lists: the base's own
// `sourceIds` changes, and the source's `docCount` may too (recomputed
// tenant-wide by the backend, not scoped to just this base).
export function useUnlinkSourceFromBase() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ kbId, sourceId }: { kbId: string; sourceId: string }) =>
      api.delete<{
        kbId: string;
        dataSourceId: string;
        documents: number;
        chunks: number;
        sha256: string | null;
        auditSeq: number | null;
      }>(`/knowledge/bases/${kbId}/sources/${sourceId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.bases });
      qc.invalidateQueries({ queryKey: keys.sources });
    },
  });
}

export interface GrantDTO {
  id: string;
  kbId: string;
  granteeType: "department" | "agent";
  granteeId: string;
}

// POST /knowledge/grants — idempotent create (backend returns the existing
// grant if the {kb, granteeType, granteeId} tuple already exists). There is
// no delete-grant endpoint, so this is intentionally one-directional: only
// wire this up to "enable" actions, never to "disable".
export function useCreateGrant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { kbId: string; granteeType: "department" | "agent"; granteeId: string }) =>
      api.post<GrantDTO>("/knowledge/grants", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.bases }),
  });
}

export interface ComponentGrantDTO {
  id: string;
  componentKey: string;
  granteeType: "department" | "agent";
  granteeId: string;
}

const COMPONENT_GRANTS_KEY = ["componentGrants"] as const;

// Unlike knowledge grants, component grants DO support revoke (DELETE
// /components/grants/{id}) -- the catalogue is a short, fixed list (4 keys),
// not a tenant-created resource, so "list once, filter client-side" is cheap
// enough that a toggle (grant/revoke) is the natural UI instead of a
// one-directional "assign" picker.
export function useComponentGrants() {
  return useQuery({
    queryKey: COMPONENT_GRANTS_KEY,
    queryFn: () => api.get<ComponentGrantDTO[]>("/components/grants"),
  });
}

export function useCreateComponentGrant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      componentKey: string;
      granteeType: "department" | "agent";
      granteeId: string;
    }) => api.post<ComponentGrantDTO>("/components/grants", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: COMPONENT_GRANTS_KEY }),
  });
}

export function useDeleteComponentGrant() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (grantId: string) => api.delete<void>(`/components/grants/${grantId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: COMPONENT_GRANTS_KEY }),
  });
}

export const useDataSources = (params: ListQueryParams = {}) =>
  useQuery({
    queryKey: [...keys.sources, params] as const,
    // Unaliased `group_by` on the wire -- see `toQueryString`'s doc comment.
    queryFn: () =>
      api.get<Page<DataSource>>(`/knowledge/sources?${toQueryString(params, "group_by")}`),
  });

// GET /knowledge/bases/{kb_id}/documents -- an already-existing, unpaginated
// bare array (list[KnowledgeDocumentDTO] on the wire), untouched by the
// KB content-view plan. Distinct from useKnowledgeBases/useDataSources'
// Page<T> envelope.
export const useKbDocuments = (kbId: string, opts: { includeDeleted?: boolean } = {}) =>
  useQuery({
    queryKey: ["kbDocuments", kbId, opts] as const,
    queryFn: () => {
      const q = new URLSearchParams();
      if (opts.includeDeleted) q.set("includeDeleted", "true");
      return api.get<KnowledgeDocument[]>(`/knowledge/bases/${kbId}/documents?${q.toString()}`);
    },
    enabled: Boolean(kbId),
  });

export interface KbChunkQueryParams {
  search?: string;
  sourceUri?: string;
  page?: number;
  pageSize?: number;
}

// GET /knowledge/bases/{kb_id}/chunks -- paginated Page<KbChunk> (Task 1).
export const useKbChunks = (kbId: string, params: KbChunkQueryParams = {}) =>
  useQuery({
    queryKey: ["kbChunks", kbId, params] as const,
    queryFn: () => {
      const pageSize = params.pageSize ?? 20;
      const q = new URLSearchParams();
      if (params.search) q.set("search", params.search);
      if (params.sourceUri) q.set("sourceUri", params.sourceUri);
      q.set("limit", String(pageSize));
      q.set("offset", String(((params.page ?? 1) - 1) * pageSize));
      return api.get<Page<KbChunk>>(`/knowledge/bases/${kbId}/chunks?${q.toString()}`);
    },
    enabled: Boolean(kbId),
  });

// GET /knowledge/bases/{kb_id}/chunks/{chunk_id}/similar -- bare SimilarChunk[]
// (Task 2). Disabled until a chunkId is picked (e.g. from a chunk detail view).
export const useSimilarChunks = (kbId: string, chunkId: string | null) =>
  useQuery({
    queryKey: ["similarChunks", kbId, chunkId] as const,
    queryFn: () => api.get<SimilarChunk[]>(`/knowledge/bases/${kbId}/chunks/${chunkId}/similar`),
    enabled: Boolean(kbId) && Boolean(chunkId),
  });

// Server-side filtering, not client-side: a global page of 50 can contain
// nothing from the agent whose screen you are on, so its feed looked empty while
// its own history sat just past the cut.
export const useActivity = (opts: { agentId?: string; limit?: number } = {}) => {
  const { lang } = useLang();
  const params = new URLSearchParams();
  if (opts.agentId) params.set("agentId", opts.agentId);
  params.set("limit", String(opts.limit ?? 50));
  const query = params.toString();
  return useQuery({
    queryKey: [...keys.activity, opts.agentId ?? "all", opts.limit ?? 50],
    queryFn: () => api.get<ActivityItem[]>(`/activity?${query}`),
    select: (rows) => rows.map((r) => localizeActivity(r, lang)),
  });
};

export const useApprovals = (status = "pending") => {
  const { lang } = useLang();
  return useQuery({
    queryKey: keys.approvals(status),
    queryFn: () => api.get<Approval[]>(`/approvals?status=${status}`),
    select: (rows) => rows.map((r) => localizeLabeled(r, lang)),
  });
};

/** The open questions this caller may answer for.
 *
 * Only `status=open` is served — the backend refuses anything else rather than
 * quietly returning the open ones, so there is no parameter here to get wrong.
 */
export const useClarifications = () =>
  useQuery({
    queryKey: keys.clarifications,
    queryFn: () => api.get<Clarification[]>("/clarifications?status=open"),
  });

/** The task board across every department this caller can see -- `GET /tasks`. */
export const useTaskBoard = (departmentId?: string) =>
  useQuery({
    queryKey: keys.tasks(departmentId),
    queryFn: () =>
      api.get<TaskBoardRow[]>(`/tasks${departmentId ? `?department_id=${departmentId}` : ""}`),
  });

// `staleTime` matters here specifically: this payload carries every
// connected plugin's full guardrail preset/library data (large for a
// plugin like odoo_mcp -- dozens of TOML-authored entries), and the
// backend resolves it by re-parsing each plugin's manifest on every
// request (its own discovery-scan result is cached for 5s, but the
// per-connection manifest parse on top of that isn't). Without a
// `staleTime`, TanStack Query's default of 0 refetches this on every
// remount -- e.g. every time an operator switches to a Guardrails tab --
// making a ~1-2s backend call feel like it happens on every click. Plugin
// data changes only when a plugin folder changes on disk, never at
// runtime (same reasoning as the backend's own discovery cache), so a
// multi-minute staleTime costs nothing in staleness for the reward of
// not re-paying that cost on every navigation.
export const useMcpConnections = () =>
  useQuery({
    queryKey: keys.mcp,
    queryFn: () => api.get<McpConnection[]>("/mcp/connections"),
    staleTime: 5 * 60 * 1000,
  });

export interface ConnectionToolNamesDTO {
  names: string[];
  read: string[];
  modify: string[];
}

// No `staleTime` meant every "Edit" click on a tool's guardrails refetched
// this from scratch -- same `_manifest_connection` re-parse-on-every-request
// cost `useMcpConnections` above already documents, just paid again here
// because this is a separate query key. A plugin's manifest only changes
// when its folder changes on disk (never at runtime), so the same
// generous staleTime applies for the same reason.
export const useConnectionToolNames = (name: string) =>
  useQuery({
    queryKey: ["connections", name, "tool-names"],
    queryFn: () => api.get<ConnectionToolNamesDTO>(`/mcp/connections/${name}/tool-names`),
    enabled: !!name,
    staleTime: 5 * 60 * 1000,
  });

/** The installed/enabled agent-runtime plugins, for the hire-time picker
 * (`AgentModelStep`) and the agent detail page. The built-in default entry
 * (`id: null`) is always present and always first, so callers never need to
 * special-case an empty list -- only a genuinely failed fetch produces one,
 * and `RuntimePicker` already renders nothing for that case, which is safe
 * because an omitted `runtimePluginId` on `POST /agents` already means "use
 * the default runtime". */
export const useRuntimes = () =>
  useQuery({ queryKey: ["runtimes"], queryFn: () => api.get<RuntimeOption[]>("/runtimes") });

// ---- mutations ----

export function useCreateAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) => api.post<AgentDetail>("/agents", body),
    // `keys.agents` alone leaves a newly hired agent invisible on the
    // department detail page's team list until a manual reload: that view
    // reads `useDepartmentAgents` under `["departments", id, "agents"]`, a
    // key `["agents", ...]` invalidation never touches (react-query matches
    // by key prefix, and these two keys don't share one).
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: keys.agents });
      if (data.departmentId) {
        qc.invalidateQueries({ queryKey: ["departments", data.departmentId, "agents"] });
      }
    },
  });
}

/** Clears an archived agent's `deletedAt`, bringing it back into the default
 * (non-archived) list view. */
export function useRestoreAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Agent>(`/agents/${id}/restore`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.agents }),
  });
}

/** Backend decides hard-delete vs. archive based on whether the agent has
 * any runs (see `DELETE /agents/{id}`) -- the caller doesn't need to know
 * which one happened, just that the agent is gone from the default list. */
export function useDeleteAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<{ outcome: "deleted" | "archived" }>(`/agents/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.agents }),
  });
}

export function useCreateDepartment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; goal?: string; icon?: string }) =>
      api.post<Department>("/departments", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.departments }),
  });
}

/** Hard-deletes when nothing depends on the department; archives (soft-
 * deletes) otherwise -- and cascades the same hard-delete-or-archive
 * decision onto every one of the department's own live agents (backend's
 * `delete_department`), so this is the one call that can remove a whole
 * department's agents along with it. Invalidates both caches since the
 * cascade touches agents, not just the department row. */
export function useDeleteDepartment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<{ outcome: string }>(`/departments/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.departments });
      qc.invalidateQueries({ queryKey: keys.agents });
    },
  });
}

/** Clears an archived department's `deletedAt`, bringing it back into the
 * default (non-archived) list view. */
export function useRestoreDepartment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Department>(`/departments/${id}/restore`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.departments }),
  });
}

export function useUpdateDepartment(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      name?: string;
      goal?: string;
      icon?: string;
      promptCachingEnabled?: boolean;
    }) => api.patch<Department>(`/departments/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.departments }),
  });
}

export function useCompleteOnboarding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ onboardingStatus: string }>("/onboarding/complete"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth", "me"] }),
  });
}

export function useSkipOnboarding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ onboardingStatus: string }>("/onboarding/skip"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth", "me"] }),
  });
}

export function useAgentLifecycle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, action }: { agentId: string; action: string }) =>
      api.post<Agent>(`/agents/${agentId}/lifecycle`, { action }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: keys.agents });
      qc.invalidateQueries({ queryKey: keys.agent(v.agentId) });
    },
  });
}

export function useCreateModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ModelWriteBody) => api.post<ModelDTO>("/models", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.models }),
  });
}

/** The wizard's "Fetch available models" action (§ Add model provider Step
 * 2) -- the live model list for one provider, resolved server-side via the
 * given credential (or the tenant-wide bound one/platform key if omitted).
 * No caching: a fresh click should always hit the provider again. */
export function useDiscoverModels() {
  return useMutation({
    mutationFn: (body: { provider: string; credentialId?: string }) =>
      api.post<{ models: string[] }>("/models/discover", body),
  });
}

/** One "Sign in with your ChatGPT subscription" attempt, as handed back by
 * `POST /models/chatgpt-subscription/device/start`.
 *
 * There is deliberately no `verificationUriComplete` here: the real OpenAI
 * flow (unlike RFC 8628's optional convenience field) offers no link with the
 * code pre-filled, so `verificationUri` is a FIXED page the person must open
 * and then type `userCode` into by hand. Any UI built on this must say so. */
export interface DeviceLoginStart {
  deviceAuthId: string;
  userCode: string;
  verificationUri: string;
  /** Seconds until the code dies. The CALLER turns this into an absolute
   * deadline and echoes it back on every poll -- see `DeviceLoginPoll`. */
  expiresIn: number;
  /** Seconds the provider wants between polls. */
  interval: number;
}

/** One poll attempt's outcome. `credentialId` is set only on "complete",
 * `error` only on "error"; "expired" and "error" both end the attempt. */
export interface DeviceLoginPoll {
  status: "pending" | "complete" | "expired" | "error";
  credentialId: string | null;
  error: string | null;
}

export function useStartChatGptDeviceLogin() {
  return useMutation({
    mutationFn: () => api.post<DeviceLoginStart>("/models/chatgpt-subscription/device/start", {}),
  });
}

/** One poll of a running device login. The endpoint is STATELESS -- it never
 * remembers a `deviceAuthId` between calls -- so `expiresAt` (computed
 * client-side from `device/start`'s `expiresIn`) has to be re-supplied every
 * time or the backend cannot tell an unconfirmed code from a dead one. */
export function usePollChatGptDeviceLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { deviceAuthId: string; userCode: string; expiresAt: string }) =>
      api.post<DeviceLoginPoll>("/models/chatgpt-subscription/device/poll", body),
    onSuccess: (result) => {
      // A completed login created a Credential row server-side; nothing else
      // invalidates ["credentials"] on this path (no useCreateCredential call
      // is involved), so without this the new account would be missing from
      // every other credential list on the page until an unrelated refetch.
      if (result.status === "complete") {
        qc.invalidateQueries({ queryKey: ["credentials"] });
      }
    },
  });
}

export function useUpdateModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: ModelWriteBody & { id: string }) =>
      api.patch<ModelDTO>(`/models/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.models }),
  });
}

export function useDeleteModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/models/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.models }),
  });
}

/** The Models table's "Test" action -- a real, free availability check
 * (§ discover_models(), the same lookup the wizard's "Fetch available
 * models" button uses) that writes ModelConfig.health server-side. */
export function useTestModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<ModelDTO>(`/models/${id}/test`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.models }),
  });
}

/** Assign an existing tenant model configuration to an agent. This is an
 * org-admin action on the API; invalidating both query shapes keeps the list
 * and agent-detail surfaces consistent after a hot swap. */
export function useSwitchAgentModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      modelConfigId,
      temperature,
      maxTokens,
      effort,
      extra,
      maxSteps,
    }: {
      agentId: string;
      modelConfigId: string;
      temperature?: number | null;
      maxTokens?: number | null;
      effort?: string | null;
      extra?: Record<string, unknown> | null;
      maxSteps?: number | null;
    }) =>
      api.patch<AgentDetail>(`/agents/${agentId}/model-config`, {
        modelConfigId,
        ...(temperature !== undefined ? { temperature } : {}),
        ...(maxTokens !== undefined ? { maxTokens } : {}),
        ...(effort !== undefined ? { effort } : {}),
        ...(extra !== undefined ? { extra } : {}),
        ...(maxSteps !== undefined ? { maxSteps } : {}),
      }),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: keys.agent(variables.agentId) });
      qc.invalidateQueries({ queryKey: keys.agents });
      qc.invalidateQueries({ queryKey: keys.models });
    },
  });
}

export interface AgentInstructionRevision {
  ts: string;
  before: string;
  after: string;
  by: string | null;
}

export interface AgentInstructionHistoryPage {
  revisions: AgentInstructionRevision[];
  totalCount: number;
  nextBeforeSeq: number | null;
}

export function useUpdateAgentInstructions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, instructions }: { agentId: string; instructions: string }) =>
      api.patch<AgentDetail>(`/agents/${agentId}/instructions`, { instructions }),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: keys.agent(variables.agentId) });
      qc.invalidateQueries({ queryKey: ["agents", variables.agentId, "instructions-history"] });
    },
  });
}

export function useRenameAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, name }: { agentId: string; name: string }) =>
      api.patch<AgentDetail>(`/agents/${agentId}/name`, { name }),
    onSuccess: (_data, variables) => {
      qc.invalidateQueries({ queryKey: keys.agent(variables.agentId) });
      qc.invalidateQueries({ queryKey: keys.agents });
    },
  });
}

// Cursor-paginated (before_seq, same shape as useAuditEvents/audit-hooks.ts)
// -- the Instructions tab's "Versions" panel loads one page at a time
// instead of the agent's whole edit history, so it stays fast once an
// agent has been edited a hundred times over.
const INSTRUCTION_HISTORY_PAGE_SIZE = 20;

export function useAgentInstructionHistory(agentId: string) {
  return useInfiniteQuery({
    queryKey: ["agents", agentId, "instructions-history"],
    initialPageParam: null as number | null,
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(INSTRUCTION_HISTORY_PAGE_SIZE) });
      if (pageParam) params.set("beforeSeq", String(pageParam));
      return api.get<AgentInstructionHistoryPage>(
        `/agents/${agentId}/instructions/history?${params.toString()}`,
      );
    },
    getNextPageParam: (last: AgentInstructionHistoryPage) => last.nextBeforeSeq,
  });
}

// The Instructions tab's own "Attached files" section -- reference files
// (PDF, Excel, images) attached to the agent's standing instructions rather
// than to a single chat turn (`FileAttachment(owner_type="agent_instructions")`
// on the backend, Task 11). Same query-key shape as useAgentMemory/
// useDeleteAgentMemory below: a plain list query plus mutations that
// invalidate it on success.
const instructionFilesKey = (agentId: string) => ["agents", agentId, "instruction-files"];

export function useAgentInstructionFiles(agentId: string) {
  return useQuery({
    queryKey: instructionFilesKey(agentId),
    queryFn: () => listInstructionFiles(agentId),
  });
}

export function useUploadInstructionFile(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => uploadInstructionFile(agentId, file),
    onSuccess: () => qc.invalidateQueries({ queryKey: instructionFilesKey(agentId) }),
  });
}

// `deleteFile` (api.ts) is owner-type-agnostic -- `DELETE /files/{id}` works
// on a chat attachment too -- but this hook is scoped to one agent's own
// instruction-files list so its cache invalidation only refetches that list.
export function useDeleteInstructionFile(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (fileId: string) => deleteFile(fileId),
    onSuccess: () => qc.invalidateQueries({ queryKey: instructionFilesKey(agentId) }),
  });
}

export type { FileAttachmentDTO };

export interface MemoryRecord {
  id: string;
  content: string;
  status: string;
  createdAt: string;
  writtenBy: string;
}

export interface MemoryList {
  records: MemoryRecord[];
}

export function useAgentMemory(agentId: string) {
  return useQuery({
    queryKey: ["agents", agentId, "memory"],
    queryFn: () => api.get<MemoryList>(`/agents/${agentId}/memory`),
  });
}

export function useDeleteAgentMemory(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (recordId: string) => api.delete<void>(`/agents/${agentId}/memory/${recordId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents", agentId, "memory"] }),
  });
}

export function useDepartmentMemory(departmentId: string) {
  return useQuery({
    queryKey: ["departments", departmentId, "memory"],
    queryFn: () => api.get<MemoryList>(`/departments/${departmentId}/memory`),
  });
}

export function useDeleteDepartmentMemory(departmentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (recordId: string) =>
      api.delete<void>(`/departments/${departmentId}/memory/${recordId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["departments", departmentId, "memory"] }),
  });
}

export function useCreateMcpConnection() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.post<McpConnection>("/mcp/connections", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.mcp }),
  });
}

// Re-runs the connection test (spawns/pings the MCP server, discovers tools)
// and returns the updated connection with fresh `connected`/`health`.
export function useTestMcpConnection(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<McpConnection>(`/mcp/connections/${id}/test`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.mcp }),
  });
}

export function useTestMcpConnectionById() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<McpConnection>(`/mcp/connections/${id}/test`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.mcp }),
  });
}

// The step-by-step "Test connection" log (McpTestLogDrawer): `enabled: false`
// because, unlike every other useQuery in this file, there is no GET this
// could fetch from -- the cache entry it reads is written ONLY by the
// mcp.test.log live patcher (apply-event.ts) as events arrive over the
// tenant's WebSocket. This hook exists purely to re-render on those writes.
export function useMcpTestLog(connectionId: string) {
  return useQuery({
    queryKey: mcpTestLogKey(connectionId),
    queryFn: () => [] as McpTestLogLine[],
    enabled: false,
    initialData: [] as McpTestLogLine[],
  });
}

export function useUpdateMcpConnection(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.patch<McpConnection>(`/mcp/connections/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.mcp }),
  });
}

// Same "id per mutate() call" shape as useTestMcpConnectionById -- a caller
// rendering a LIST of connections (the Capa detail modal's "Connection"
// section, one row per McpConnection) needs one shared mutation object, not
// a fresh hook instance per row's id.
export function useDeleteMcpConnectionById() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/mcp/connections/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.mcp }),
  });
}

export interface McpLoginDTO {
  id: string;
  name: string;
  credentialId: string;
  departmentId: string | null;
  connected: boolean;
  scopes: string[] | Record<string, string[]>;
  health: Record<string, unknown>;
}

export function useMcpLogins(credentialType?: string) {
  return useQuery({
    queryKey: ["mcp-logins", credentialType ?? "all"],
    queryFn: () =>
      api.get<McpLoginDTO[]>(
        credentialType
          ? `/mcp/logins?credentialType=${encodeURIComponent(credentialType)}`
          : "/mcp/logins",
      ),
  });
}

export function useCreateMcpLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      name: string;
      credentialType: string;
      // Either fieldValues creates a brand-new Credential, or credentialId
      // reuses one that already exists (e.g. one a capa's own setup form
      // created) -- see CreateMcpLoginRequest on the backend.
      fieldValues?: Record<string, unknown>;
      credentialId?: string;
      scopes: string[] | Record<string, string[]>;
      // Omitted/undefined creates a tenant-wide login; set it to scope the
      // login to one department, so a second login can reuse the same
      // `name` (tool key) for a different department.
      departmentId?: string;
    }) => api.post<McpLoginDTO>("/mcp/logins", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["mcp-logins"] });
      qc.invalidateQueries({ queryKey: ["credentials"] });
    },
  });
}

// Trigger a run. The backend now enqueues a durable run and returns it in the
// `queued` state; poll useRun(runId) for progress.
export function useRunAgent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      task,
      mcpConnectionId,
    }: {
      agentId: string;
      task: string;
      mcpConnectionId?: string;
    }) => api.post<RunDTO>(`/agents/${agentId}/run`, { task, mcpConnectionId }),
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: keys.agents });
      qc.invalidateQueries({ queryKey: keys.agent(v.agentId) });
      qc.invalidateQueries({ queryKey: keys.activity });
      qc.invalidateQueries({ queryKey: ["approvals"] });
    },
  });
}

// Track a durable run. WS `run.status` events drive updates to this query's
// cache entry, and the terminal-state agents invalidation (formerly done here
// via refetchInterval) is handled by the live event patcher.
export function useRun(runId: string | null) {
  return useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.get<RunDTO>(`/runs/${runId}`),
    enabled: !!runId,
  });
}

export interface ReportDTO {
  runId: string;
  agentId: string;
  agentName: string;
  createdAt: string;
  renderedComponents: RunComponentDTO[];
}

// Finished runs that rendered at least one component (chart/table/record
// card) -- the "My work" Reports section. Scoped server-side to the
// caller's visible agents, same as /agents itself.
export function useReports(agentId?: string) {
  return useQuery({
    queryKey: ["reports", agentId ?? "all"],
    queryFn: () => api.get<ReportDTO[]>(`/reports${agentId ? `?agentId=${agentId}` : ""}`),
  });
}

// Answer a run suspended in `waiting_for_input`. Operator/admin-gated on the
// backend; on success the run re-enqueues (state flips to `queued`), so the
// invalidation here re-fetches the run and the caller's waiting-for-input UI
// naturally disappears.
export function useAnswerRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (answer: string) => api.post<RunDTO>(`/runs/${runId}/answer`, { answer }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", runId] }),
  });
}

// Cooperative cancel (§7.2, POST /runs/{id}/cancel): records the operator's
// intent, doesn't stop the run synchronously -- a `queued` run is skipped
// before start, a `running` one stops at its next step boundary. The
// returned DTO's `state` may therefore still read "running" right after a
// successful call; invalidating re-fetches it and the live WS patch (or the
// next poll) carries the eventual "interrupted" transition through.
export function useCancelRun(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<RunDTO>(`/runs/${runId}/cancel`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", runId] }),
  });
}

export function useDecideApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      approvalId,
      decision,
      reason,
      option,
    }: {
      approvalId: string;
      decision: string;
      reason?: string;
      // Which of the agent's proposed options was picked. Free text travels
      // either way, so an operator can choose one, write an instruction, or both.
      option?: string | null;
    }) => api.post<Approval>(`/approvals/${approvalId}/decision`, { decision, reason, option }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["approvals"] }),
  });
}

/** Answer one parked question, so the agent that asked it can carry on.
 *
 * Not `POST /runs/{id}/answer`: that door is gated on `run:control`, which is in
 * no seat vocabulary, so the person the agent was waiting for was exactly the
 * person who could not reply. This one is gated on `clarification:answer`.
 *
 * The run is invalidated too — answering re-queues it, and an agent-detail page
 * left open would otherwise go on showing `waiting_for_input` forever.
 */
export function useAnswerClarification() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ clarificationId, answer }: { clarificationId: string; answer: string }) =>
      api.post<ClarificationAnswer>(`/clarifications/${clarificationId}/answer`, { answer }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: keys.clarifications });
      if (data?.runId) qc.invalidateQueries({ queryKey: ["run", data.runId] });
    },
  });
}

/** Put new work directly on a department's team lead -- the task board's own
 * orchestration entry point, alongside chatting with the Copilot. 409 means
 * the department has no team lead to receive it (see `workspace/tasks.py`). */
export function useCreateTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { departmentId: string; instructions: string; title?: string }) =>
      api.post<TaskBoardRow>("/tasks", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tasks"] }),
  });
}

// ---- §14a cross-department collaboration + §8.6 supervision ----
// Query/mutation hooks over the new backend endpoints. Screens can adopt these
// to replace their mock imports (shape-mapping done at the call site).

export interface HandoffDTO {
  id: string;
  handoffTypeId: string;
  type: string;
  sourceDepartmentId: string;
  targetDepartmentId: string;
  sourceDept: string;
  targetDept: string;
  status: string;
  gate: string;
  payload: Record<string, unknown>;
  createdBy: string;
  createdAt: string;
  targetTaskId?: string | null;
}

export interface HandoffTypeDTO {
  id: string;
  name: string;
  classification: string;
}

export function useHandoffs() {
  return useQuery({ queryKey: ["handoffs"], queryFn: () => api.get<HandoffDTO[]>("/handoffs") });
}

export function useHandoffTypes() {
  return useQuery({
    queryKey: ["handoff-types"],
    queryFn: () => api.get<HandoffTypeDTO[]>("/handoff-types"),
  });
}

export function useCreateHandoff() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      handoffTypeId: string;
      sourceDepartmentId: string;
      targetDepartmentId: string;
      payload: Record<string, unknown>;
      gate?: string;
    }) => api.post<HandoffDTO>("/handoffs", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["handoffs"] }),
  });
}

export function useHandoffAction() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      id,
      action,
      targetTaskId,
    }: {
      id: string;
      action: "accept" | "reject" | "complete";
      targetTaskId?: string;
    }) =>
      api.post<HandoffDTO>(
        `/handoffs/${id}/${action}`,
        action === "accept" ? { targetTaskId } : undefined,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["handoffs"] }),
  });
}

export interface BindingDTO {
  id: string;
  eventType: string;
  sourceDepartmentId: string;
  targetDepartmentId: string;
}

export function useContractBindings() {
  return useQuery({
    queryKey: ["contract-bindings"],
    queryFn: () => api.get<BindingDTO[]>("/contract-bindings"),
  });
}

export function useEmitEvent() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      departmentId,
      eventType,
      payload,
    }: {
      departmentId: string;
      eventType: string;
      payload: Record<string, unknown>;
    }) =>
      api.post<{ handoffs: string[] }>(`/departments/${departmentId}/emit`, { eventType, payload }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["handoffs"] }),
  });
}

export interface FlowRunDTO {
  id: string;
  flowVersionId: string;
  flowId: string;
  status: string;
  currentStages: unknown[];
  context: Record<string, unknown>;
  triggerEvent?: string | null;
}

export interface FlowListDTO {
  id: string;
  name: string;
  semver: string;
  spec: Record<string, unknown>;
  runCount: number;
}

export function useFlows() {
  return useQuery({ queryKey: ["flows"], queryFn: () => api.get<FlowListDTO[]>("/flows") });
}

export function useFlowRuns() {
  return useQuery({
    queryKey: ["flow-runs"],
    queryFn: () => api.get<FlowRunDTO[]>("/flow-runs"),
  });
}

// --- Cost / usage (§ token accounting) ------------------------------------
// Real recorded token usage aggregated from TokenUsageRecord. `providerCostMicros`
// is the provider's billed cost in USD micros (1e6 micros = 1 USD).
export interface UsageDTO {
  group: string;
  tokensIn: number;
  tokensOut: number;
  providerCostMicros: number;
  savedTokensIn: number;
  savedTokensOut: number;
  savedCostMicros: number;
}

export function useUsage(groupBy: "department" | "agent" = "department") {
  return useQuery({
    queryKey: ["usage", groupBy],
    queryFn: () => api.get<UsageDTO[]>(`/usage?group_by=${groupBy}`),
  });
}

export interface BudgetDTO {
  id: string;
  departmentId: string | null;
  softLimitTokens: number | null;
  hardLimitTokens: number | null;
  dollarBudgetUsd: number | null;
  dollarReferenceProvider: string | null;
  dollarReferenceModel: string | null;
}

export interface BudgetStatusDTO {
  scope: "tenant" | "department";
  departmentId: string | null;
  softLimitTokens: number | null;
  hardLimitTokens: number | null;
  currentTokens: number;
  softExceeded: boolean;
  hardExceeded: boolean;
}

export function useBudgets() {
  return useQuery({ queryKey: ["budgets"], queryFn: () => api.get<BudgetDTO[]>("/budgets") });
}

export function useBudgetStatus(departmentId: string | null) {
  return useQuery({
    queryKey: ["budget-status", departmentId ?? "tenant"],
    queryFn: () =>
      api.get<BudgetStatusDTO>(
        departmentId ? `/budgets/status?department_id=${departmentId}` : "/budgets/status",
      ),
  });
}

export function useSetBudget() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      departmentId,
      softLimitTokens,
      hardLimitTokens,
      dollarBudgetUsd,
    }: {
      departmentId: string | null;
      softLimitTokens?: number | null;
      hardLimitTokens?: number | null;
      dollarBudgetUsd?: number;
    }) =>
      api.put<BudgetDTO>("/budgets", {
        departmentId,
        softLimitTokens,
        hardLimitTokens,
        dollarBudgetUsd,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["budgets"] });
      qc.invalidateQueries({ queryKey: ["budget-status"] });
    },
  });
}

// --- Department-frame contracts: emits + intakes (§14a.2) -----------------
export interface ContractFieldDTO {
  name: string;
  type: string;
  required: boolean;
  example?: string | number | null;
}
export interface IntakeDTO {
  id: string;
  type: string;
  route: string;
  gate: string;
  fields: ContractFieldDTO[];
}
export interface DepartmentContractsDTO {
  emits: string[];
  intakes: IntakeDTO[];
}

export function useDepartmentContracts(departmentId: string) {
  return useQuery({
    queryKey: ["department-contracts", departmentId],
    queryFn: () => api.get<DepartmentContractsDTO>(`/departments/${departmentId}/contracts`),
    enabled: !!departmentId,
  });
}

export function useSetDepartmentContracts(departmentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: DepartmentContractsDTO) =>
      api.put<DepartmentContractsDTO>(`/departments/${departmentId}/contracts`, body),
    onSuccess: (data) => {
      qc.setQueryData(["department-contracts", departmentId], data);
    },
  });
}

export interface FlowVersionDTO {
  id: string;
  flowId: string;
  name: string;
  semver: string;
}

export function usePublishFlow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (spec: Record<string, unknown>) => api.post<FlowVersionDTO>("/flows", { spec }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["flows"] }),
  });
}

export function useStartFlow() {
  return useMutation({
    mutationFn: ({ versionId, context }: { versionId: string; context: Record<string, unknown> }) =>
      api.post<FlowRunDTO>(`/flow-versions/${versionId}/start`, { context }),
  });
}

export function useFlowRun(runId: string | null) {
  return useQuery({
    queryKey: ["flow-run", runId],
    queryFn: () => api.get<FlowRunDTO>(`/flow-runs/${runId}`),
    enabled: !!runId,
  });
}

export interface SupervisionPolicyDTO {
  id: string;
  departmentId: string;
  allowedInterventions: string[];
}

export interface InterventionDTO {
  id: string;
  supervisedAgentId: string;
  taskId: string;
  kind: string;
  outcome?: string | null;
}

export function useSupervisionPolicies() {
  return useQuery({
    queryKey: ["supervision-policies"],
    queryFn: () => api.get<SupervisionPolicyDTO[]>("/supervision/policies"),
  });
}

export function useSupervisionInterventions() {
  return useQuery({
    queryKey: ["supervision-interventions"],
    queryFn: () => api.get<InterventionDTO[]>("/supervision/interventions"),
  });
}

export interface AgentSupervisorDTO {
  supervisedAgentId: string;
  supervisorAgentId?: string | null;
  policyId?: string | null;
  driftGuard: boolean;
}

export function useAgentSupervisor(agentId: string) {
  return useQuery({
    queryKey: ["agent-supervisor", agentId],
    queryFn: () => api.get<AgentSupervisorDTO>(`/agents/${agentId}/supervisor`),
    enabled: !!agentId,
  });
}

export function useSetAgentSupervisor() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      supervisorAgentId,
      driftGuard,
    }: {
      agentId: string;
      supervisorAgentId: string | null;
      driftGuard: boolean;
    }) =>
      api.put<AgentSupervisorDTO>(`/agents/${agentId}/supervisor`, {
        supervisorAgentId,
        driftGuard,
      }),
    onSuccess: (_d, v) => qc.invalidateQueries({ queryKey: ["agent-supervisor", v.agentId] }),
  });
}

export interface TriggerDTO {
  id: string;
  agentId: string;
  kind: "cron" | "event" | "webhook";
  taskText: string;
  enabled: boolean;
  cronExpression: string | null;
  nextRunAt: string | null;
  lastRunAt: string | null;
  eventSource: string | null;
  eventType: string | null;
  //: kind='webhook' only -- the full POST /webhooks/{token} URL. Not
  //: one-time-reveal: fetched fresh on every GET /agents/{id}/triggers, so
  //: an operator can always come back and re-copy it.
  webhookUrl: string | null;
}

export function useAgentTriggers(agentId: string) {
  return useQuery({
    queryKey: ["agent-triggers", agentId],
    queryFn: () => api.get<TriggerDTO[]>(`/agents/${agentId}/triggers`),
    enabled: !!agentId,
  });
}

export function useCreateAgentTrigger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      taskText,
      kind = "cron",
      cronExpression,
    }: {
      agentId: string;
      taskText: string;
      //: "webhook" needs no cronExpression -- the token is generated
      //: server-side (agent tool login selection design's own "never
      //: client-supplied" rule applies here too: an unguessable value must
      //: not be something the caller could have picked).
      kind?: "cron" | "webhook";
      cronExpression?: string;
    }) => api.post<TriggerDTO>(`/agents/${agentId}/triggers`, { kind, taskText, cronExpression }),
    onSuccess: (_d, v) => qc.invalidateQueries({ queryKey: ["agent-triggers", v.agentId] }),
  });
}

export function useUpdateAgentTrigger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      agentId,
      triggerId,
      cronExpression,
    }: {
      agentId: string;
      triggerId: string;
      cronExpression: string;
    }) => api.patch<TriggerDTO>(`/triggers/${triggerId}`, { cronExpression }),
    onSuccess: (_d, v) => qc.invalidateQueries({ queryKey: ["agent-triggers", v.agentId] }),
  });
}

export function useDeleteAgentTrigger() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ triggerId }: { agentId: string; triggerId: string }) =>
      api.delete<void>(`/triggers/${triggerId}`),
    onSuccess: (_d, v) => qc.invalidateQueries({ queryKey: ["agent-triggers", v.agentId] }),
  });
}

// ---- Plugins on disk (§13) --------------------------------------------------
// Discovery is installation-wide; installation is per tenant, so `installed`
// here always means "installed for the tenant this token belongs to".

export interface DiscoveredCapa {
  pluginId: string;
  name: string;
  // Manifest.label -- a human-readable display name ("GitHub" instead of
  // "github_mcp"). Null when the manifest sets none; callers fall back to
  // `name`.
  label?: string | null;
  version: string;
  type: string;
  trust: string;
  summary: string;
  summaryTranslations: Record<string, string>;
  valid: boolean;
  error?: string | null;
  installed: boolean;
  installedVersion?: string | null;
  databaseId?: string | null;
  installationStatus?: string | null;
  // Why installationStatus === "disabled", or null otherwise. "plugin update
  // pending consent" (a Capa's own two-step Update flow) is a normal,
  // temporary state right after clicking Update -- see isArchivedInstall.
  disabledReason?: string | null;
  permissions: string[];
  capabilities: string[];
  surfaces: string[];
  setup?: PluginSetupSpec | null;
  // A capa-contributed row for a PERSONAL setting (distinct from `setup`
  // above, an admin's one-time config) -- see PersonalSettingsSpec's
  // docstring (backend/src/oc8/capas/manifest.py). `location` names WHERE it
  // renders; today only "approval_channels" (Profile page) is a real
  // listener.
  personalSettings?: {
    label: string;
    location: string;
    // Present only when the capa's `i18n/<locale>.po` catalogs have a
    // translation for `label` -- see capa-i18n's resolver
    // (backend/src/oc8/capas/i18n.py). Absent locales fall back to `label`.
    translations?: { label?: Record<string, string> };
  } | null;
}

export interface PluginSetupField {
  key: string;
  label: string;
  kind: "text" | "url" | "password" | "department" | "credential" | "select";
  required: boolean;
  default: string;
  placeholder: string;
  help: string;
  // Only present (and only meaningful) when kind === "credential": names
  // which credential_types entry this field resolves to (e.g.
  // "telegram_bot"). This whole `setup` object is passed through from the
  // plugin manifest as a raw dict (see DiscoveredPluginDTO.setup on the
  // backend), not through a CamelModel, so -- like `submit_label` below --
  // it keeps the backend's snake_case key rather than being camelCased.
  credential_type?: string;
  // Only present (and only meaningful) when kind === "select": the fixed
  // choices, in order. Same snake_case-passthrough reasoning as above.
  options?: string[];
}

export interface PluginSetupSpec {
  title: string;
  description: string;
  submit_label: string;
  fields: PluginSetupField[];
  // Absent for a plugin with nothing to connect over MCP (an approval
  // channel just needs its values stored and, if it declared one, proven by
  // a server-side check) -- see capa-setup-dialog.tsx's submit().
  mcp?: Record<string, unknown>;
  // Sibling translations map keyed the same snake_case way as the rest of
  // this raw-dict-passthrough object (see credential_type/options above) --
  // populated by capa-i18n's resolver when the capa's `i18n/<locale>.po`
  // catalogs have a match. Absent keys/locales fall back to the source text.
  translations?: {
    title?: Record<string, string>;
    description?: Record<string, string>;
    submit_label?: Record<string, string>;
    fields?: Record<string, { label?: Record<string, string>; help?: Record<string, string> }>;
  };
}

// Only installed Capas have a Plugin row (a database id) to fetch an icon
// for -- a not-yet-installed Capa Store entry always falls back to its
// type's generic icon on the caller's side. `staleTime: Infinity` because a
// plugin's own declared icon never changes without a redeploy; the object
// URL this creates is intentionally never revoked -- it's cheap, reused for
// the query's lifetime, and this page never renders enough Capas for that
// to matter.
export function useCapaIcon(pluginId: string | null | undefined) {
  return useQuery({
    queryKey: ["plugins", "icon", pluginId],
    queryFn: async () => {
      const blob = await fetchCapaIcon(pluginId!);
      return blob ? URL.createObjectURL(blob) : null;
    },
    enabled: !!pluginId,
    staleTime: Infinity,
  });
}

export function useAvailablePlugins(params: ListQueryParams = {}) {
  return useQuery({
    queryKey: ["plugins", "available", params] as const,
    // Unaliased `group_by` on the wire -- see `toQueryString`'s doc comment.
    queryFn: () =>
      api.get<Page<DiscoveredCapa>>(`/capas/available?${toQueryString(params, "group_by")}`),
    // The backend's own manifest-scan cache already has a 5s TTL (discovery.py),
    // so refetching more often than that only adds latency without ever
    // seeing newer data -- this is what made reopening the Capas page (up to
    // 200 rows) feel slow on every visit within a session, not just cold load.
    staleTime: 5_000,
  });
}

export function useInstallPluginFromDisk() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (pluginId: string) =>
      api.post<{ id: string; name: string; semver: string }>("/capas/install-from-disk", {
        pluginId,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["plugins", "available"] });
      qc.invalidateQueries({ queryKey: ["plugins"] });
    },
  });
}

export function useEnablePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      pluginId,
      grantedPermissions,
    }: {
      pluginId: string;
      grantedPermissions: string[];
    }) => api.post(`/capas/${pluginId}/enable`, { grantedPermissions }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["plugins", "available"] });
      qc.invalidateQueries({ queryKey: ["knowledge", "connectors"] });
    },
  });
}

export function useDisablePlugin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ pluginId, reason }: { pluginId: string; reason?: string }) =>
      api.post(`/capas/${pluginId}/disable`, { reason }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["plugins", "available"] });
      qc.invalidateQueries({ queryKey: ["knowledge", "connectors"] });
    },
  });
}

export function useConfigurePlugin(pluginId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (values: Record<string, string>) =>
      api.post<{ connectionId: string | null }>(`/capas/${pluginId}/setup`, { values }),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.mcp }),
  });
}

// ---- Skill authoring (§6.5) -------------------------------------------------

export interface CreateSkillBody {
  name: string;
  description: string;
  category: string;
  instructions: string;
  tools: string[];
  guardrails: string[];
}

export function useCreateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateSkillBody) => api.post<Skill>("/skills", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.skills }),
  });
}

export interface UpdateSkillBody {
  name?: string;
  description?: string;
  category?: string;
  instructions?: string;
}

export function useUpdateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: UpdateSkillBody & { id: string }) =>
      api.patch<Skill>(`/skills/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.skills }),
  });
}

/** Clears an archived skill's `deletedAt`, bringing it back into the default
 * (non-archived) list view. */
export function useRestoreSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<Skill>(`/skills/${id}/restore`),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.skills }),
  });
}

/** One skill found at a remote source, with oc8's verdict on it. */
export interface ImportCandidate {
  name: string;
  description: string;
  instruction: string;
  path: string;
  tokens: number;
  /** "fits" | "tight" | "too_big" — judged against the department's own model. */
  verdict: string;
  warnings: string[];
}

export interface ImportPreview {
  source: string;
  budgetTokens: number;
  skills: ImportCandidate[];
}

/**
 * Look at a source WITHOUT importing anything.
 *
 * Not a query: it hits a stranger's server, so it happens when somebody asks
 * for it and not because a component rendered.
 */
export function usePreviewSkillImport() {
  return useMutation({
    mutationFn: (body: {
      source: string;
      departmentId?: string | null;
      budgetTokens?: number | null;
    }) => api.post<ImportPreview>("/skills/import/preview", body),
  });
}

export interface ImportResult {
  imported: { id: string; name: string; tokens: number }[];
  skipped: { name: string; reason: string }[];
  budgetTokens: number;
}

export function useImportSkills() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      source: string;
      names: string[];
      departmentId?: string | null;
      budgetTokens?: number | null;
      acceptOversized?: boolean;
    }) => api.post<ImportResult>("/skills/import", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.skills }),
  });
}

export function usePublishSkillVersion() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      skillId,
      ...body
    }: { skillId: string; semver: string; instructions: string } & Partial<CreateSkillBody>) =>
      api.post<Skill>(`/skills/${skillId}/versions`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.skills }),
  });
}

export interface SkillAssignment {
  id: string;
  agentId: string;
  skillId: string;
  skillName: string;
  skillVersionId: string;
  semver: string;
  enabled: boolean;
}

export function useAgentSkills(agentId: string) {
  return useQuery({
    queryKey: ["agent-skills", agentId],
    queryFn: () => api.get<SkillAssignment[]>(`/agents/${agentId}/skills`),
    enabled: !!agentId,
  });
}

export function useAssignSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, skillVersionId }: { agentId: string; skillVersionId: string }) =>
      api.post<{ status: "assigned" | "already_assigned" }>(`/agents/${agentId}/skills`, {
        skillVersionId,
      }),
    onSuccess: (_d, v) => qc.invalidateQueries({ queryKey: ["agent-skills", v.agentId] }),
  });
}

export function useUnassignSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ agentId, assignmentId }: { agentId: string; assignmentId: string }) =>
      api.delete<void>(`/agents/${agentId}/skills/${assignmentId}`),
    onSuccess: (_d, v) => qc.invalidateQueries({ queryKey: ["agent-skills", v.agentId] }),
  });
}

// ---- Credentials framework --------------------------------------------------

export interface CredentialTypeFieldDTO {
  key: string;
  label: string;
  kind: "text" | "url" | "password" | "department" | "credential";
  required: boolean;
  default: string;
  placeholder: string;
  help: string;
  // Each field is passed through from the owning capa's manifest as a raw
  // dict (see CredentialTypeDTO.fields on the backend), not through a
  // CamelModel -- so, like PluginSetupField's snake_case passthrough above,
  // these translation maps keep the backend's snake_case keys. Populated by
  // capa-i18n's resolver when the capa's `i18n/<locale>.po` catalogs have a
  // match; absent keys/locales fall back to the source text.
  label_translations?: Record<string, string>;
  help_translations?: Record<string, string>;
  placeholder_translations?: Record<string, string>;
}

export interface CredentialTypeDTO {
  name: string;
  displayName: string;
  displayNameTranslations: Record<string, string>;
  fields: CredentialTypeFieldDTO[];
}

export interface CredentialDTO {
  id: string;
  name: string;
  credentialType: string;
  fieldValues: Record<string, unknown>;
  lastTestedAt: string | null;
  lastTestOk: boolean | null;
  createdAt: string;
  updatedAt: string;
}

export function useCredentials(credentialType?: string) {
  return useQuery({
    queryKey: ["credentials", credentialType ?? "all"],
    queryFn: () =>
      api.get<CredentialDTO[]>(
        credentialType ? `/credentials?type=${encodeURIComponent(credentialType)}` : "/credentials",
      ),
  });
}

export function useCredentialTypes() {
  return useQuery({
    queryKey: ["credential-types"],
    queryFn: () => api.get<CredentialTypeDTO[]>("/credential-types"),
  });
}

export function useCreateCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: {
      name: string;
      credentialType: string;
      fieldValues: Record<string, unknown>;
    }) => api.post<CredentialDTO>("/credentials", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["credentials"] }),
  });
}

export function useUpdateCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      credentialId,
      ...body
    }: {
      credentialId: string;
      name?: string;
      fieldValues?: Record<string, unknown>;
    }) => api.patch<CredentialDTO>(`/credentials/${credentialId}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["credentials"] }),
  });
}

export function useDeleteCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (credentialId: string) => api.delete<void>(`/credentials/${credentialId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["credentials"] }),
  });
}

export function useTestCredential() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (credentialId: string) =>
      api.post<{ ok: boolean }>(`/credentials/${credentialId}/test`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["credentials"] }),
  });
}

// ---- Approval channels (§5.6) ------------------------------------------------
// A channel (Telegram, WhatsApp, ...) needs two separate things once its capa
// is installed and its bot credential configured: an ADMIN did that setup once
// (CapaSetupDialog), but each PERSON who should personally receive/decide
// approvals still has to link THEIR OWN chat -- a one-time code they send to
// the bot. These hooks cover that personal linking flow, not the admin setup.

export interface AvailableChannelDTO {
  id: string;
}

export interface ChannelLinkCodeDTO {
  channel: string;
  code: string;
  expiresAt: string;
}

export interface ChannelBindingDTO {
  id: string;
  channel: string;
  userId: string;
  bound: boolean;
  createdAt: string;
}

export function useAvailableChannels() {
  return useQuery({
    queryKey: ["channels", "available"],
    queryFn: () => api.get<AvailableChannelDTO[]>("/channels"),
  });
}

export function useChannelBindings() {
  return useQuery({
    queryKey: ["channels", "bindings"],
    queryFn: () => api.get<ChannelBindingDTO[]>("/channels/bindings"),
  });
}

export function useRequestChannelLink() {
  return useMutation({
    mutationFn: (channel: string) =>
      api.post<ChannelLinkCodeDTO>(`/channels/${encodeURIComponent(channel)}/link`),
  });
}

export function useRevokeChannelBinding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (bindingId: string) => api.delete<void>(`/channels/bindings/${bindingId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["channels", "bindings"] }),
  });
}

// ---- Secret store (§12.3) ---------------------------------------------------
// Metadata only. The API has no resolve-over-HTTP endpoint and SecretDTO carries
// no value field, so a secret's plaintext can never reach the browser — not even
// the one that just stored it.

export interface SecretMeta {
  id: string;
  name: string;
  kind: string;
  keyVersion: string;
  createdAt: string;
}

export function useSecrets() {
  return useQuery({
    queryKey: ["secrets"],
    queryFn: () => api.get<SecretMeta[]>("/secrets"),
  });
}

export function useCreateSecret() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; value: string; kind?: string }) =>
      api.post<SecretMeta>("/secrets", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["secrets"] }),
  });
}

export function useDeleteSecret() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete<void>(`/secrets/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["secrets"] }),
  });
}

// ---- Company backup / restore -----------------------------------------------
// `exportBackup` (api.ts) is called directly from the panel, not wrapped in a
// hook — a browser download has nothing worth caching.

export function usePreviewBackup() {
  return useMutation({
    mutationFn: (file: File) => previewBackup(file),
  });
}

// ---- Capa export -------------------------------------------------------
// `downloadCapaExport` (api.ts) is called directly from the wizard, not
// wrapped in a hook -- same reasoning as `exportBackup` above: a browser
// download has nothing worth caching.

export function usePreviewCapaExport() {
  return useMutation({
    mutationFn: (items: CapaExportItemInput[]) => previewCapaExport(items),
  });
}

export function useRestoreBackup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      confirmName,
      passphrase,
    }: {
      file: File;
      confirmName: string;
      passphrase?: string;
    }) => restoreBackup(file, confirmName, passphrase),
    // A restore replaces essentially the entire tenant's data in place, so
    // every cached query is stale, not just a handful of keys — drop the
    // whole cache rather than trying to enumerate what changed.
    onSuccess: () => qc.invalidateQueries(),
  });
}

// --- oc8 Copilot (backend/src/oc8/api/v1/copilot.py) ---
// `copilot:manage` is tenant-wide and org_admin-only (a prepared proposal can
// name any agent in any department); `copilot:view` gates reading the list.
// CopilotDock itself only requires `copilot:use`, so callers of these hooks
// must gate them individually — see PendingProposals in copilot-dock.tsx for
// both the `enabled: can("copilot:view")` query gate and the
// `can("copilot:manage")` gate on the Apply/Reject buttons.

export interface CopilotOperationRef {
  label: string;
  // operation_references() (backend/src/oc8/copilot/capabilities.py) returns
  // a flat {agentId|pluginId|integrationId|configurationRef: string} record,
  // never an array.
  references: Record<string, string>;
}

export interface CopilotProposal {
  id: string;
  status: string;
  revision: number;
  operations: CopilotOperationRef[];
}

// The tenant's single standing Assistant agent (GET /assistant,
// backend/src/oc8/api/v1/chat.py -- get_or_create_assistant under the hood,
// so this also lazily provisions it on first call). One agent id per tenant,
// never changes under a session, so this never needs invalidating.
export function useAssistant() {
  return useQuery({
    queryKey: ["assistant"],
    queryFn: () => api.get<{ agentId: string }>("/assistant"),
    staleTime: Infinity,
  });
}

export function useCopilotProposal(proposalId: string | null) {
  return useQuery({
    queryKey: ["copilot", "proposal", proposalId],
    queryFn: () => api.get<CopilotProposal>(`/copilot/proposals/${proposalId}`),
    enabled: !!proposalId,
  });
}

// Everything the Assistant has drafted and nobody has answered yet
// (GET /copilot/proposals, backend/src/oc8/api/v1/copilot.py). "draft" is the
// only status `create_proposal` ever writes, so it is this system's word for
// "waiting for a human".
//
// Listed by status rather than correlated to a chat message on purpose: the
// `propose_change` tool answers the model with a German sentence naming the
// id, and a sentence is not an API. Listing also covers a proposal the
// Assistant raised over Telegram, which no web transcript mentions at all.
//
// Polled on the same 2s cadence the chat transcript uses while a run is in
// flight (hooks-chat.ts's useChatMessages), so a proposal made mid-answer
// appears without the reader having to reload.
export function useCopilotProposals(options?: { enabled?: boolean; poll?: boolean }) {
  return useQuery({
    queryKey: ["copilot", "proposals"],
    queryFn: () => api.get<CopilotProposal[]>("/copilot/proposals"),
    enabled: options?.enabled ?? true,
    refetchInterval: options?.poll ? 2000 : false,
  });
}

export function useApplyCopilotProposal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (proposalId: string) =>
      api.post<CopilotProposal>(`/copilot/proposals/${proposalId}/apply`),
    onSuccess: (proposal) => {
      qc.setQueryData(["copilot", "proposal", proposal.id], proposal);
      // A proposal, once applied, may have touched agents, departments,
      // budgets or almost anything else the copilot can see — invalidating
      // the whole cache is the same call `useRestoreBackup` makes for the
      // same reason: enumerating what changed costs more than refetching.
      // This sweep covers the pending list too.
      qc.invalidateQueries();
    },
  });
}

export function useRejectCopilotProposal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (proposalId: string) =>
      api.post<CopilotProposal>(`/copilot/proposals/${proposalId}/reject`),
    onSuccess: (proposal) => {
      qc.setQueryData(["copilot", "proposal", proposal.id], proposal);
      // Narrow, unlike apply: a rejection changes nothing but the proposal's
      // own status, so only the pending list has to be re-read.
      qc.invalidateQueries({ queryKey: ["copilot", "proposals"] });
    },
  });
}

// ---- Agent/department/tenant KPIs (Agent KPIs & Statistics plan, Task 6) --
// consumed by the Agent Overview tab (Task 7), the Department page (Task 8),
// and the Statistics page (Task 9). Backend: GET /agents/{id}/kpis,
// GET /departments/{id}/kpis, GET /kpis (Task 4,
// backend/src/oc8/api/v1/kpis.py) -- all three return the same
// `KPIDTO`-shaped fields (camelCase on the wire via `CamelModel`), so one
// `KPIData` interface covers all of them. ----

export interface KPIData {
  runCount: number;
  totalDurationMs: number | null;
  executionDurationMs: number | null;
  approvalWaitMs: number | null;
  responseTimeMs: number | null;
  avgToolCallDurationMs: number | null;
}

export interface KPIFilterParams {
  dateFrom?: string;
  dateTo?: string;
}

// Small local query-string helper for the KPI endpoints' own param shapes
// (dateFrom/dateTo, plus /kpis' extra agentId/departmentId/groupBy/status) --
// distinct from `toQueryString`, which is specific to the `ListQueryParams`/
// `Page<T>` search-filter-group-paginate contract these endpoints don't use.
// Filters out `undefined` (and `null`) so an unset filter never appears on
// the wire as the literal string "undefined".
function buildQuery(params?: Record<string, unknown>): string {
  if (!params) return "";
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null) q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

export function useAgentKpis(agentId: string, params?: KPIFilterParams) {
  return useQuery({
    queryKey: ["agents", agentId, "kpis", params] as const,
    queryFn: () =>
      api.get<KPIData>(`/agents/${agentId}/kpis${buildQuery(params as Record<string, unknown>)}`),
    enabled: !!agentId,
  });
}

export function useDepartmentKpis(departmentId: string, params?: KPIFilterParams) {
  return useQuery({
    queryKey: ["departments", departmentId, "kpis", params] as const,
    queryFn: () =>
      api.get<KPIData>(
        `/departments/${departmentId}/kpis${buildQuery(params as Record<string, unknown>)}`,
      ),
    enabled: !!departmentId,
  });
}

export interface TenantKPIFilterParams extends KPIFilterParams {
  agentId?: string;
  departmentId?: string;
  groupBy?: "agent" | "department" | "day" | "week" | "month";
  status?: string;
}

// Ungrouped (no `groupBy`): bare `KPIData`. Grouped: `{ rows: (KPIData &
// { groupKey: string })[] }` -- FastAPI's own `response_model` is unset on
// `GET /kpis` for the same reason (kpis.py's `tenant_kpis` docstring), since
// the two shapes can't be expressed as one type. Callers narrow on
// `"rows" in data` (or by checking whether they passed a `groupBy`).
export function useTenantKpis(params?: TenantKPIFilterParams) {
  return useQuery({
    queryKey: ["kpis", params] as const,
    queryFn: () =>
      api.get<KPIData | { rows: (KPIData & { groupKey: string })[] }>(
        `/kpis${buildQuery(params as Record<string, unknown>)}`,
      ),
    // A 422 here is the backend's MAX_BUCKETS/MAX_GROUPS cap rejection --
    // deterministic for the current filters, so retrying can only repeat the
    // same failure while delaying the "too many results, narrow your filter"
    // message the caller shows instead. Every other failure (network blip,
    // 5xx) keeps the default retry-3 behavior.
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 422) return false;
      return failureCount < 3;
    },
  });
}

// ---- Widget-based "My Work" dashboard (§ My Work Widget Dashboard plan) ----

export type WidgetType = "chat" | "approvals" | "reports" | "budget" | "activity" | "tasks";

export interface WidgetInstance {
  id: string;
  type: WidgetType;
  x: number;
  y: number;
  w: number;
  h: number;
  config: Record<string, unknown>;
}

export interface DashboardLayoutDTO {
  widgets: WidgetInstance[];
  templateId: string | null;
}

export interface DashboardTemplateDTO {
  id: string;
  name: { en: string; de: string };
  widgets: WidgetInstance[];
}

export function useDashboardLayout() {
  return useQuery({
    queryKey: ["dashboard", "layout"],
    queryFn: () => api.get<DashboardLayoutDTO | null>("/dashboard/layout"),
  });
}

export function useSaveDashboardLayout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: DashboardLayoutDTO) =>
      api.put<DashboardLayoutDTO>("/dashboard/layout", body),
    onSuccess: (data) => qc.setQueryData(["dashboard", "layout"], data),
  });
}

export function useDashboardTemplates() {
  return useQuery({
    queryKey: ["dashboard", "templates"],
    queryFn: () => api.get<DashboardTemplateDTO[]>("/dashboard/templates"),
  });
}

export interface DashboardPresetDTO {
  id: string;
  name: string;
  widgets: WidgetInstance[];
  scope: "personal" | "tenant";
  /** Whether the caller created this preset -- combine with `settings:manage`
   * (via `useMay()`) to decide whether a delete control should show for a
   * `scope: "tenant"` preset the caller didn't create themselves. */
  mine: boolean;
}

export function useDashboardPresets() {
  return useQuery({
    queryKey: ["dashboard", "presets"],
    queryFn: () => api.get<DashboardPresetDTO[]>("/dashboard/presets"),
  });
}

export function useSaveDashboardPreset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; widgets: WidgetInstance[]; scope: "personal" | "tenant" }) =>
      api.post<DashboardPresetDTO>("/dashboard/presets", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dashboard", "presets"] }),
  });
}

export function useDeleteDashboardPreset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/dashboard/presets/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dashboard", "presets"] }),
  });
}
