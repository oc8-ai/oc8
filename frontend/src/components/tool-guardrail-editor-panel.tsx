import { Link } from "@tanstack/react-router";
import { Info } from "lucide-react";
import { useT } from "@/lib/i18n";
import {
  AUTONOMY_ANCHOR,
  actsOutwardUnasked,
  hasApprovalConditions,
  unaskedOutwardTools,
} from "@/lib/autonomy";
import type { AgentDetail } from "@/lib/hooks-agent-detail";
import { useConnectionToolNames, type McpConnection } from "@/lib/hooks";
import { GuardrailPresetPicker, type GuardrailValue } from "@/components/guardrail-preset-picker";
import { GuardrailFunctionRules } from "@/components/guardrail-function-rules";
import { GuardrailUsageLimits } from "@/components/guardrail-usage-limits";
import { useAgentToolUsage } from "@/lib/hooks-agent-detail";

// Today's usage is counted per agent, so only the agent view loads it -- in a
// child, so the department view (and its tests) never needs a query client
// for it.
function AgentUsageLimits({
  agentId,
  toolKey,
  ...props
}: Omit<Parameters<typeof GuardrailUsageLimits>[0], "usage"> & {
  agentId: string;
  toolKey: string;
}) {
  const usage = useAgentToolUsage(agentId);
  return (
    <GuardrailUsageLimits
      {...props}
      usage={usage.data ? usage.data.filter((u) => u.toolKey === toolKey) : undefined}
    />
  );
}

// Renders inline, inside the table row `ToolGuardrailTable` expands into --
// no backdrop, no fixed positioning, no close button of its own. Editing a
// tool's guardrails this way keeps every other row in view, instead of
// covering the table with an overlay disconnected from the row it edits.
export function ToolGuardrailEditorPanel({
  toolKey,
  connection,
  ceiling,
  value,
  onChange,
  onCancel,
  onSave,
  saving,
  agentId,
  agentAutonomy,
}: {
  toolKey: string;
  connection: McpConnection | undefined;
  ceiling: GuardrailValue | null;
  value: GuardrailValue;
  onChange: (next: GuardrailValue) => void;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
  // Agent level only (`ToolGuardrailTable`'s `level="agent"`) -- the
  // per-function inner table below needs an agent to call
  // `POST /agents/{id}/guardrails/interpret` against, which the
  // department-level ceiling editor has no equivalent for.
  agentId?: string;
  // Agent level only: whether the agent itself may act outward unasked. A
  // preset that allows outward tools with no approval only works then.
  agentAutonomy?: Pick<AgentDetail, "autonomy" | "autonomyGrants">;
}) {
  const t = useT();
  const toolNames = useConnectionToolNames(connection?.name ?? "");
  // `only` is a tool-name allowlist (same vocabulary as approval-action tags --
  // see GuardrailPreset's own doc comment in guardrail-preset-picker.tsx: "the
  // ONLY tool names this preset puts within reach"), so the same connection
  // tool-name fetch doubles as suggestions for both -- no separate endpoint
  // or prop required. Passed straight into `GuardrailPresetPicker`, which
  // owns the one free-text approval-actions field and the "only" picker.
  const toolCatalog = toolNames.data?.names ?? [];
  const readTools = toolNames.data?.read ?? [];
  const stillAsksOutward =
    !!agentId &&
    !!agentAutonomy &&
    !actsOutwardUnasked(agentAutonomy) &&
    unaskedOutwardTools(value, toolKey, connection?.outwardTools ?? [], ceiling).length > 0;
  const onlySometimes = hasApprovalConditions(value, ceiling);

  return (
    <div className="rounded-md border border-border/70 bg-background/20 p-4">
      {ceiling && (
        <div className="mb-4 rounded-md border border-border/70 bg-background/30 p-3">
          <div className="mb-1 text-[10px] uppercase tracking-widest text-muted-foreground">
            {t("Department allows (ceiling)", "Department erlaubt (Decke)")}
          </div>
          <div className="text-xs text-muted-foreground">
            {ceiling.read && t("Read", "Lesen")}
            {ceiling.read && ceiling.modify && ", "}
            {ceiling.modify && t("Modify", "Verändern")}
            {ceiling.approvalActions.length > 0 &&
              ` — ${t("approval for", "Freigabe für")}: ${ceiling.approvalActions.join(", ")}`}
            {ceiling.approvalEur != null && ` — ≥ ${ceiling.approvalEur} €`}
            {(ceiling.limits ?? []).map(
              (lim) =>
                ` — ${t("at most", "höchstens")} ${lim.max} ${lim.per === "day" ? t("per day", "pro Tag") : t("per hour", "pro Stunde")}: ${lim.tools.join("/")}`,
            )}
          </div>
        </div>
      )}

      {agentId ? (
        connection && (
          <GuardrailFunctionRules
            agentId={agentId}
            connectionName={connection.name}
            toolCatalog={toolCatalog}
            readTools={readTools}
            guardrailAttributes={connection.guardrailAttributes}
            value={value}
            onChange={onChange}
            presets={connection.guardrailPresets}
            guardrailLibrary={connection.guardrailLibrary}
          />
        )
      ) : (
        <GuardrailPresetPicker
          presets={connection?.guardrailPresets ?? []}
          guardrailLibrary={connection?.guardrailLibrary ?? null}
          hasValueSpec={connection?.hasValueSpec ?? false}
          value={value}
          onChange={onChange}
          approvalSuggestions={toolCatalog}
          onlyOptions={toolCatalog}
        />
      )}

      {stillAsksOutward && agentId && (
        <div className="mt-4 flex items-start gap-2 rounded-md border border-primary/40 bg-primary/5 p-3 text-xs">
          <Info className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
          <span>
            {onlySometimes
              ? t(
                  "These guardrails let this agent reach people outside without approval in some cases, but it may still ask before each one until you make it autonomous and allow it to reach people outside.",
                  "Diese Guardrails erlauben in manchen Fällen, ohne Freigabe nach außen zu kommunizieren. Der Agent fragt aber womöglich trotzdem vor jeder Nachricht, bis du ihn auf selbstständig stellst und Kommunikation nach außen erlaubst.",
                )
              : t(
                  "These guardrails let this agent reach people outside without approval, but it will still ask before each one until you make it autonomous and allow it to reach people outside.",
                  "Diese Guardrails erlauben, ohne Freigabe nach außen zu kommunizieren. Der Agent fragt aber trotzdem vor jeder Nachricht, bis du ihn auf selbstständig stellst und Kommunikation nach außen erlaubst.",
                )}{" "}
            <Link
              to="/agents/$id"
              params={{ id: agentId }}
              search={{ tab: "config" }}
              hash={AUTONOMY_ANCHOR}
              className="font-medium text-primary underline"
            >
              {t("Open the autonomy setting", "Zur Einstellung Selbstständigkeit")}
            </Link>
          </span>
        </div>
      )}

      {agentId ? (
        <AgentUsageLimits
          agentId={agentId}
          toolKey={toolKey}
          value={value}
          onChange={onChange}
          toolCatalog={toolCatalog}
          ceiling={ceiling?.limits}
        />
      ) : (
        <GuardrailUsageLimits value={value} onChange={onChange} toolCatalog={toolCatalog} />
      )}

      <div className="mt-5 flex gap-2">
        <button
          type="button"
          onClick={onSave}
          disabled={saving}
          className="flex-1 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 disabled:opacity-60"
        >
          {saving ? t("Saving…", "Wird gespeichert…") : t("Save", "Speichern")}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-4 py-2 text-sm font-medium text-muted-foreground transition hover:bg-accent hover:text-foreground"
        >
          {t("Cancel", "Abbrechen")}
        </button>
      </div>
    </div>
  );
}
