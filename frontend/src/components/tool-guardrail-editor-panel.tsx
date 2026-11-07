import { useT } from "@/lib/i18n";
import { useConnectionToolNames, type McpConnection } from "@/lib/hooks";
import { GuardrailPresetPicker, type GuardrailValue } from "@/components/guardrail-preset-picker";
import { GuardrailFunctionRules } from "@/components/guardrail-function-rules";

// Renders inline, inside the table row `ToolGuardrailTable` expands into --
// no backdrop, no fixed positioning, no close button of its own. Editing a
// tool's guardrails this way keeps every other row in view, instead of
// covering the table with an overlay disconnected from the row it edits.
export function ToolGuardrailEditorPanel({
  toolKey,
  connection,
  ceiling,
  ceilingLabel,
  value,
  onChange,
  onCancel,
  onSave,
  saving,
  agentId,
}: {
  toolKey: string;
  connection: McpConnection | undefined;
  ceiling: GuardrailValue | null;
  // Heading over the ceiling summary; the member level's ceiling is the
  // department ∩ agent value, not the department's alone.
  ceilingLabel?: string;
  value: GuardrailValue;
  onChange: (next: GuardrailValue) => void;
  onCancel: () => void;
  onSave: () => void;
  saving: boolean;
  // Agent and member level (`ToolGuardrailTable`'s `level="agent"`/`"member"`) -- the
  // per-function inner table below needs an agent to call
  // `POST /agents/{id}/guardrails/interpret` against, which the
  // department-level ceiling editor has no equivalent for.
  agentId?: string;
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

  return (
    <div className="rounded-md border border-border/70 bg-background/20 p-4">
      {ceiling && (
        <div className="mb-4 rounded-md border border-border/70 bg-background/30 p-3">
          <div className="mb-1 text-[10px] uppercase tracking-widest text-muted-foreground">
            {ceilingLabel ?? t("Department allows (ceiling)", "Department erlaubt (Decke)")}
          </div>
          <div className="text-xs text-muted-foreground">
            {ceiling.read && t("Read", "Lesen")}
            {ceiling.read && ceiling.modify && ", "}
            {ceiling.modify && t("Modify", "Verändern")}
            {ceiling.approvalActions.length > 0 &&
              ` — ${t("approval for", "Freigabe für")}: ${ceiling.approvalActions.join(", ")}`}
            {ceiling.approvalEur != null && ` — ≥ ${ceiling.approvalEur} €`}
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
