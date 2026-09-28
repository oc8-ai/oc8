// TanStack Query hooks for the agent detail page. Split from hooks.ts to
// avoid colliding with in-flight edits there — this module owns its own
// query functions but reuses the ["agents", id] key that hooks.ts and the
// live event patcher (live/apply-event.ts, "agent.status") already target,
// so this data stays live without any changes to the shared file.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ToolPolicy } from "@/lib/hooks";
import type { Condition } from "@/components/guardrail-preset-picker";

// Mirrors backend AgentDetailDTO (backend/src/oc8/schemas/dto.py), camelCase
// over the wire via CamelModel. Extends the AgentDTO fields inline.
export interface AgentDetail {
  id: string;
  name: string;
  role: string;
  llm: string;
  provider: string;
  status: string; // running | warning | error | paused | waiting_for_task (see mapAgentStatus in live/apply-event.ts)
  tools: string[];
  lastAction: string;
  lastRun: string;
  tasksToday: number;
  guardrails: string[];
  schedule: string;
  avatarColor: string;
  departmentId: string | null;
  modelConfigId: string | null;
  isLead: boolean;
  mission: string;
  departmentName: string | null;
  effectiveTools: Record<string, ToolPolicy>;
  departmentFrameTools: Record<string, ToolPolicy>;
  // The agent's own narrowing["tools"] row, verbatim -- not intersected with
  // role_rights the way effectiveTools is. Use this (falling back to
  // departmentFrameTools), never effectiveTools, when resaving fields a
  // panel doesn't itself edit: effectiveTools dips whenever role_rights
  // does, and re-persisting that dip bakes it into narrowing permanently.
  narrowingTools: Record<string, ToolPolicy>;
  // Tool keys this agent has deliberately overridden via its own narrowing
  // save (the ONLY writer of this set is PUT /agents/{id}/narrowing) -- the
  // authoritative "has this agent diverged from the department default"
  // signal, NOT the same as a key merely being present in narrowingTools
  // (every save rewrites every currently-relevant key, touched or not).
  narrowingOverriddenKeys: string[];
  runtimeRef: string | null;
  // The run this agent is on right now, whoever started it. Without it the live
  // log can only follow a run started in this browser tab, so a scheduled run
  // happens invisibly.
  currentRunId: string | null;
  // Per-agent sampling overrides (resolve_params's narrowest-first source,
  // backend/src/oc8/modelrouter/sampling.py). null means "inherit the
  // assigned model's own value", not "use a framework default directly".
  temperature: number | null;
  maxTokens: number | null;
  effort: string | null;
  extra: Record<string, unknown> | null;
  // Per-agent override of the run-loop step budget (agent.engine._max_steps).
  // null means "inherit settings.agent_max_steps", not a literal framework
  // default value.
  maxSteps: number | null;
  /** Last successful Auto-router tier for this agent (fast|balanced|strong). */
  autoRouterAffinityTier?: string | null;
  // The version that actually runs. Mirrors lib/skills.ts's thin shape -- an id
  // plus the number, no client-side version state machine. Whether the working
  // copy has DRIFTED from it is a separate query (useAgentDraftStatus), because
  // it changes on a different schedule.
  currentVersionId: string | null;
  currentVersionNo: number | null;
}

// Reuses the ["agents", id] key already invalidated by the "agent.status"
// live patcher in live/apply-event.ts, so this query stays live.
export function useAgent(agentId: string) {
  return useQuery({
    queryKey: ["agents", agentId],
    queryFn: () => api.get<AgentDetail>(`/agents/${agentId}`),
    enabled: !!agentId,
  });
}

// PUT /agents/{id}/narrowing expects { narrowing: Record<string, unknown> }
// (NarrowingRequest in backend/src/oc8/schemas/requests.py:23).
export function useUpdateNarrowing(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { narrowing: Record<string, unknown> }) =>
      api.put<AgentDetail>(`/agents/${agentId}/narrowing`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents", agentId] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

// POST /agents/{id}/guardrails/interpret -- single-shot, non-conversational
// translation of a free-text guardrail definition into the same generic
// 4-state decision (plus structured Conditions for "with_limits") the manual
// editor writes (GuardrailInterpretRequest/DTO in backend/src/oc8/schemas/
// requests.py + dto.py). Never writes anything itself, and the LLM is never
// called again at runtime for this text: the caller shows this result to the
// operator, who must explicitly accept it before it is merged into the
// GuardrailValue draft that still goes through the normal useUpdateNarrowing
// save above.
export interface GuardrailInterpretation {
  decision: "self_sufficient" | "with_limits" | "approval_required" | "not_allowed";
  conditions: Condition[];
}

export function useInterpretGuardrail(agentId: string) {
  return useMutation({
    mutationFn: (body: { connectionName: string; function: string; definition: string }) =>
      api.post<GuardrailInterpretation>(`/agents/${agentId}/guardrails/interpret`, body),
  });
}

// POST /agents/{id}/guardrails/interpret-from-instruction -- the "Copilot"
// button on the guardrails table. Same one-shot, non-conversational contract
// as useInterpretGuardrail above, but reads the agent's own instructions
// instead of an operator-typed definition, and proposes rules for every
// restricted function of one connection in a single call. `results` only
// ever lists functions the model decided to restrict -- everything else
// stays "self_sufficient" (no entry), same silent-default convention as the
// backend's own narrowing/frame resolution.
export interface GuardrailBatchInterpretation {
  results: (GuardrailInterpretation & { function: string })[];
}

export function useInterpretGuardrailsFromInstruction(agentId: string) {
  return useMutation({
    mutationFn: (body: { connectionName: string }) =>
      api.post<GuardrailBatchInterpretation>(
        `/agents/${agentId}/guardrails/interpret-from-instruction`,
        body,
      ),
  });
}

// PUT /agents/{id}/runtime expects { runtimePluginId: string | null }
// (RuntimeAssignRequest in backend/src/oc8/schemas/requests.py:104) -- an
// absent/null id means "clear, use the built-in default", never "leave
// unchanged". Returns the updated AgentDetail; the ["agents", id] cache is
// still explicitly invalidated (not just replaced from the response) so any
// other component reading this query key re-renders too.
export function useUpdateAgentRuntime(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { runtimePluginId: string | null }) =>
      api.put<AgentDetail>(`/agents/${agentId}/runtime`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents", agentId] });
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}
