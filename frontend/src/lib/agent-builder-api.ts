// The Copilot Agent Builder's REST hooks: the "Agent anlegen" card and the
// agent ideas for the empty chat. Backend: api/v1/copilot_builder.py.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, retryUnlessRefused } from "@/lib/api";
import type { Translate } from "@/lib/tool-labels";

export type BuilderStatus =
  | "draft"
  | "awaiting_approval"
  | "applied"
  | "rejected"
  | "expired"
  | "superseded";

export interface BuilderChoiceDTO {
  id: string;
  label: string;
}

export interface BuilderTriggerDTO {
  kind: string;
  cronExpression: string | null;
  eventSource: string | null;
  eventType: string | null;
  taskText: string;
}

export interface BuilderLimitDTO {
  connectionName: string;
  function: string;
  decision: string;
}

export interface AgentBuilderDTO {
  id: string;
  status: BuilderStatus;
  sessionId: string | null;
  /** Null while the agent goes into a department the proposal creates. */
  departmentId: string | null;
  departmentName: string;
  /** The proposal creates `departmentName` for its agent. */
  newDepartment: boolean;
  name: string;
  roleTitle: string;
  mission: string;
  model: BuilderChoiceDTO | null;
  skills: BuilderChoiceDTO[];
  triggers: BuilderTriggerDTO[];
  systems: string[];
  limits: BuilderLimitDTO[];
  expiresAt: string | null;
  mayCreate: boolean;
  mayRequest: boolean;
  readOnly: boolean;
  refusal: string | null;
  failedStep: number | null;
  /** Why that step failed (a fixed code), when the server said. */
  failedReason: string | null;
  rejectedReason: string | null;
  agent: { id: string; name: string; status: string } | null;
}

export interface LocalizedText {
  en: string;
  de: string;
}

export interface AgentIdeaDTO {
  key: string;
  title: LocalizedText;
  subtitle: LocalizedText;
  prompt: LocalizedText;
}

export const builderKeys = {
  one: (proposalId: string) => ["copilot", "builder", proposalId] as const,
  ideas: ["copilot", "agent-ideas"] as const,
};

export function useAgentBuilder(proposalId: string | null) {
  return useQuery({
    queryKey: builderKeys.one(proposalId ?? ""),
    queryFn: () =>
      api.get<AgentBuilderDTO>(`/copilot/builder/${encodeURIComponent(proposalId ?? "")}`),
    enabled: !!proposalId,
    retry: retryUnlessRefused,
  });
}

function useBuilderAction(proposalId: string, action: "apply" | "request") {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.post<AgentBuilderDTO>(`/copilot/builder/${encodeURIComponent(proposalId)}/${action}`, {}),
    onSuccess: (dto) => qc.setQueryData(builderKeys.one(proposalId), dto),
    // A refusal is stored on the proposal; read it back.
    onError: () => qc.invalidateQueries({ queryKey: builderKeys.one(proposalId) }),
  });
}

export function useApplyAgentBuilder(proposalId: string) {
  return useBuilderAction(proposalId, "apply");
}

export function useRequestAgentBuilder(proposalId: string) {
  return useBuilderAction(proposalId, "request");
}

export function useAgentIdeas(enabled: boolean) {
  return useQuery({
    queryKey: builderKeys.ideas,
    queryFn: () => api.get<AgentIdeaDTO[]>("/copilot/agent-ideas"),
    enabled,
    staleTime: 60_000,
  });
}

/** The code of an `agent_builder:<code>` refusal, or null for anything else. */
export function builderRefusalCode(error: unknown): string | null {
  if (error instanceof ApiError && error.message.startsWith("agent_builder:")) {
    return error.message.slice("agent_builder:".length);
  }
  return null;
}

/** Why a step failed, from its `failedReason` code; null for an unknown one. */
function stepReasonText(reason: string, t: Translate): string | null {
  switch (reason) {
    case "skill_requirements_missing":
      return t(
        "The skill needs a system this agent does not have. Add the system or leave the skill out.",
        "Der Skill braucht ein System, das dieser Agent nicht hat. Nimm das System dazu oder lass den Skill weg.",
      );
    case "runtime_lacks_skills":
      return t(
        "The agent's runtime cannot run skills.",
        "Die Laufzeit des Agenten kann keine Skills ausführen.",
      );
    case "trigger_invalid":
      return t(
        "The schedule or event is not valid.",
        "Der Zeitplan oder das Ereignis ist ungültig.",
      );
    case "model_manual_only":
      return t(
        "This model may only answer chats, not run on a schedule or an event.",
        "Dieses Modell darf nur im Chat antworten, nicht nach Zeitplan oder auf ein Ereignis.",
      );
    case "system_outside_frame":
      return t(
        "A system is switched off or restricted for this department.",
        "Ein System ist für diese Abteilung abgeschaltet oder eingeschränkt.",
      );
    case "system_login_needed":
      return t(
        "A system needs a login chosen for this agent first. Set it up under Connections.",
        "Für ein System muss erst ein Login für diesen Agenten gewählt werden. Richte es unter Verbindungen ein.",
      );
    case "system_no_amount_limit":
      return t(
        "A system cannot report amounts, so it cannot take an amount limit.",
        "Ein System kann keine Beträge melden, also auch kein Betragslimit bekommen.",
      );
    default:
      return null;
  }
}

/** Why the agent was not created, in the member's words. */
export function builderRefusalText(
  code: string,
  step: number | null,
  t: Translate,
  reason?: string | null,
): string {
  switch (code) {
    case "name_taken":
      return t("That name is already taken.", "Diesen Namen gibt es schon.");
    case "department_unknown":
      return t("That department no longer exists.", "Diese Abteilung gibt es nicht mehr.");
    case "connection_not_allowed":
      return t(
        "A system is no longer connected or not released for this department.",
        "Ein System ist nicht mehr verbunden oder für diese Abteilung nicht freigegeben.",
      );
    case "model_not_allowed":
      return t(
        "The model is no longer available in the chat.",
        "Das Modell ist im Chat nicht mehr verfügbar.",
      );
    case "skill_not_allowed":
      return t("A skill is no longer available.", "Ein Skill ist nicht mehr verfügbar.");
    case "skill_needs_system":
      return t(
        "A skill needs a system this agent would not have.",
        "Ein Skill braucht ein System, das dieser Agent nicht hätte.",
      );
    case "secret_value":
      return t(
        "The text contains a credential. Remove it and set it up under Credentials.",
        "Der Text enthält ein Zugangsdatum. Entfern es und richte es unter Zugangsdaten ein.",
      );
    case "already_applied":
      return t("This agent was already created.", "Dieser Agent wurde schon angelegt.");
    case "expired":
      return t("This proposal has expired.", "Dieser Vorschlag ist abgelaufen.");
    case "not_open":
      return t("This proposal is no longer open.", "Dieser Vorschlag ist nicht mehr offen.");
    case "superseded_concurrently":
      return t(
        "Replaced by a newer proposal meanwhile.",
        "Inzwischen durch einen neueren Vorschlag ersetzt.",
      );
    case "step_failed": {
      const why = reason ? stepReasonText(reason, t) : null;
      const n = (step ?? 0) + 1;
      return why
        ? t(
            `Step ${n} could not be applied: ${why} Nothing was created.`,
            `Schritt ${n} ließ sich nicht anwenden: ${why} Es wurde nichts angelegt.`,
          )
        : t(
            `Step ${n} could not be applied. Nothing was created.`,
            `Schritt ${n} ließ sich nicht anwenden. Es wurde nichts angelegt.`,
          );
    }
    default:
      return t(
        "The agent was not created. Try again.",
        "Der Agent wurde nicht angelegt. Versuch es noch einmal.",
      );
  }
}
