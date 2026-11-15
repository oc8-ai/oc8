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

export interface BuilderDatasetDTO {
  key: string;
  label: string;
  grain: string;
  fieldCount: number;
}

/** The dashboard a proposal creates or changes. */
export interface BuilderBoardDTO {
  /** The board changed, or once applied the board created; null before. */
  boardId: string | null;
  mode: "create" | "update";
  title: string;
  datasets: BuilderDatasetDTO[];
  blockCount: number;
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
  /** "board" for a dashboard-only proposal: no new agent, so `name`,
   *  `roleTitle` and `mission` are empty. */
  kind: "agent" | "board";
  board: BuilderBoardDTO | null;
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

/** Why a dashboard proposal was refused (`builder_tools._REFUSALS`,
 *  `builder_notice.REASONS`); null for a code that is not about dashboards. */
function boardReasonText(code: string, t: Translate): string | null {
  switch (code) {
    case "department_changed":
      return t(
        "The dashboard moved to another department meanwhile. Reject this request so it can be requested again.",
        "Das Dashboard ist inzwischen in eine andere Abteilung gewechselt. Lehne diese Anfrage ab, damit sie neu angefragt werden kann.",
      );
    case "board_unknown":
      return t(
        "This dashboard no longer exists or is out of reach.",
        "Dieses Dashboard gibt es nicht mehr oder es ist nicht erreichbar.",
      );
    case "agent_unknown":
      return t(
        "The agent that should fill the dashboard no longer exists or is out of reach.",
        "Der Agent, der das Dashboard füllen soll, existiert nicht mehr oder ist nicht erreichbar.",
      );
    case "agent_other_department":
      return t(
        "A dashboard can only go to an agent of its own department.",
        "Ein Dashboard kann nur an einen Agenten seiner eigenen Abteilung gehen.",
      );
    case "board_version_unknown":
      return t(
        "That earlier version of the dashboard does not exist.",
        "Diese frühere Version des Dashboards gibt es nicht.",
      );
    case "board_changed":
      return t(
        "The dashboard changed since this proposal. Ask the Copilot to propose the change again from its current version.",
        "Das Dashboard hat sich seit diesem Vorschlag geändert. Bitte den Copilot, die Änderung auf dem aktuellen Stand neu vorzuschlagen.",
      );
    case "board_spec_invalid":
      return t("The dashboard's layout is not valid.", "Der Aufbau des Dashboards ist ungültig.");
    case "artifact_not_available":
      return t(
        "Artifact blocks are available in Enterprise only.",
        "Artefakt-Blöcke gibt es nur in Enterprise.",
      );
    // A change to a dashboard may only add (§6.6): what is there stays.
    case "dataset_removed":
      return t(
        "A dashboard change may only add datasets; an existing dataset can't be removed.",
        "Eine Dashboard-Änderung darf nur Datensätze hinzufügen; ein bestehender Datensatz kann nicht entfernt werden.",
      );
    case "field_removed":
      return t(
        "A dashboard change may only add fields; an existing field can't be removed.",
        "Eine Dashboard-Änderung darf nur Felder hinzufügen; ein bestehendes Feld kann nicht entfernt werden.",
      );
    case "grain_changed":
      return t(
        "A dataset's grain can't change; add a new dataset instead.",
        "Die Granularität eines Datensatzes kann sich nicht ändern; leg stattdessen einen neuen Datensatz an.",
      );
    case "field_type_changed":
      return t(
        "A field's type can't change; add a new field instead.",
        "Der Typ eines Felds kann sich nicht ändern; leg stattdessen ein neues Feld an.",
      );
    case "currency_changed":
      return t(
        "A field's currency can't change; add a new field instead.",
        "Die Währung eines Felds kann sich nicht ändern; leg stattdessen ein neues Feld an.",
      );
    case "board_builder_only":
      return t(
        "Dashboards can only be proposed by the Copilot.",
        "Dashboards kann nur der Copilot vorschlagen.",
      );
    default:
      return null;
  }
}

/** Why a step failed, from its `failedReason` code; null for an unknown one. */
function stepReasonText(reason: string, t: Translate): string | null {
  const board = boardReasonText(reason, t);
  if (board) return board;
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

/** Why the agent (or, for `kind` "board", the dashboard) was not saved, in
 *  the member's words. */
export function builderRefusalText(
  code: string,
  step: number | null,
  t: Translate,
  reason?: string | null,
  kind: "agent" | "board" = "agent",
): string {
  const board = boardReasonText(code, t);
  if (board) return board;
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
      return kind === "board"
        ? t("This dashboard was already saved.", "Dieses Dashboard wurde schon gespeichert.")
        : t("This agent was already created.", "Dieser Agent wurde schon angelegt.");
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
      if (kind === "board") {
        return why
          ? t(
              `Step ${n} could not be applied: ${why} Nothing was saved.`,
              `Schritt ${n} ließ sich nicht anwenden: ${why} Es wurde nichts gespeichert.`,
            )
          : t(
              `Step ${n} could not be applied. Nothing was saved.`,
              `Schritt ${n} ließ sich nicht anwenden. Es wurde nichts gespeichert.`,
            );
      }
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
      if (kind === "board") {
        return t(
          "The dashboard was not saved. Try again.",
          "Das Dashboard wurde nicht gespeichert. Versuch es noch einmal.",
        );
      }
      return t(
        "The agent was not created. Try again.",
        "Der Agent wurde nicht angelegt. Versuch es noch einmal.",
      );
  }
}
