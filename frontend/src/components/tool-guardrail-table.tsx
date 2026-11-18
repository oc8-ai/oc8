import { Fragment, useState } from "react";
import { Plus } from "lucide-react";
import { useT } from "@/lib/i18n";
import { ApiError } from "@/lib/api";
import type { McpConnection } from "@/lib/hooks";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";
import { ToolGuardrailEditorPanel } from "@/components/tool-guardrail-editor-panel";
import { AddToolPicker } from "@/components/add-tool-picker";

// `api.ts`'s `ApiError.message` is `JSON.stringify(detail)` whenever the
// backend's `detail` isn't already a plain string -- both `PUT
// /departments/{id}/tools` and `PUT /agents/{id}/narrowing` return one of
// two structured shapes on 422, and a bare "couldn't save" toast leaves the
// operator unable to tell "the department's own ceiling doesn't allow this"
// (a normal, expected tightening-rule rejection they can act on) apart from
// a real bug. Falls back to the raw message for anything else -- including
// a genuine network failure, which has no JSON to parse in the first place.
export function describeGuardrailSaveError(
  error: unknown,
  t: (en: string, de: string) => string,
): string {
  // A 403 here is always `require_agent_write`/`authorize_agent_write`
  // (`api/deps.py`) refusing the write outright -- its `detail` is an
  // engineer-facing string like "requires permission: agent:manage", never
  // the operator's business language. Shown as a fixed, translated line
  // instead of leaking that string, same as the RLS-denial 403s these share
  // a shape with.
  if (error instanceof ApiError && error.status === 403) {
    return t(
      "This action is blocked by an OC8 security policy.",
      "Diese Aktion ist durch eine OC8-Sicherheitsrichtlinie gesperrt.",
    );
  }
  const raw = error instanceof Error ? error.message : String(error);
  let detail: unknown;
  try {
    detail = JSON.parse(raw);
  } catch {
    return raw || t("Couldn't save guardrails", "Guardrails konnten nicht gespeichert werden");
  }
  if (detail && typeof detail === "object" && "error" in detail) {
    const kind = (detail as { error: unknown }).error;
    const violations = (detail as { violations?: unknown }).violations;
    if (kind === "narrowing_exceeds_frame" && Array.isArray(violations) && violations.length > 0) {
      const first = violations[0] as { tool_key?: string; reason?: string };
      return t(
        `The department doesn't allow this for ${first.tool_key}: ${first.reason}`,
        `Das Department erlaubt das nicht für ${first.tool_key}: ${first.reason}`,
      );
    }
    if (kind === "value_spec_not_supported" && Array.isArray(violations) && violations.length > 0) {
      const first = violations[0] as { connection?: string; field?: string };
      return t(
        `${first.connection} doesn't support a euro threshold for this tool`,
        `${first.connection} unterstützt keine €-Schwelle für dieses Tool`,
      );
    }
  }
  return raw || t("Couldn't save guardrails", "Guardrails konnten nicht gespeichert werden");
}

export interface ToolGuardrailRow {
  toolKey: string;
  connection: McpConnection | undefined;
  ceilingPolicy: GuardrailValue | null;
  ownValue: GuardrailValue;
  status: "inherited" | "narrowed" | "agent-only" | null;
  deviationCount?: { count: number; total: number };
  loginPicker?: React.ReactNode;
}

function StatusBadge({
  status,
  t,
}: {
  status: ToolGuardrailRow["status"];
  t: (en: string, de: string) => string;
}) {
  if (status === "inherited") {
    return (
      <span className="rounded-full border border-border bg-background/40 px-2 py-0.5 text-[11px] text-muted-foreground">
        {t("Same as department", "Wie Department")}
      </span>
    );
  }
  if (status === "narrowed") {
    return (
      <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
        {t("Narrowed", "Eingeschränkt")}
      </span>
    );
  }
  if (status === "agent-only") {
    return (
      <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] text-primary">
        {t("This agent only", "Nur dieser Agent")}
      </span>
    );
  }
  return null;
}

export function ToolGuardrailTable({
  level,
  rows,
  addableNames,
  connections,
  onSave,
  onAdd,
  saving,
  departmentId,
  agentId,
}: {
  level: "department" | "agent";
  rows: ToolGuardrailRow[];
  addableNames: string[];
  connections: McpConnection[];
  // Resolves `true` on success, `false` on failure -- the row's editor
  // stays open on `false` so a rejected save (e.g. the department's own
  // ceiling doesn't allow it) doesn't also discard the edit the operator
  // was mid-way through.
  onSave: (toolKey: string, next: GuardrailValue) => Promise<boolean>;
  onAdd: (name: string, policy: GuardrailValue | null, connectionId?: string | null) => void;
  saving: boolean;
  // Agent level only: passed through to AddToolPicker so it can resolve a
  // login inline before "Add" persists (see AddToolPicker's own comment).
  // Left undefined for the department level, where saving never requires a
  // connection_id.
  departmentId?: string | null;
  // Agent level only: passed through to ToolGuardrailEditorPanel so its
  // per-function inner table can call the interpret endpoint. Left
  // undefined at the department level, which has no per-agent identity to
  // interpret against (see that panel's own comment on this prop).
  agentId?: string;
}) {
  const t = useT();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [draft, setDraft] = useState<GuardrailValue | null>(null);

  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-border text-[11px] uppercase tracking-wider text-muted-foreground">
            <th className="p-3">{t("Tool", "Tool")}</th>
            <th className="p-3">{t("Rights", "Rechte")}</th>
            <th className="p-3">{t("Approval needed for", "Freigabe nötig für")}</th>
            {level === "agent" && <th className="p-3">{t("Login", "Login")}</th>}
            {level === "agent" && <th className="p-3">{t("Status", "Status")}</th>}
            {level === "department" && <th className="p-3">{t("Deviations", "Abweichungen")}</th>}
            <th className="p-3" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isEditing = row.toolKey === editingKey;
            return (
              <Fragment key={row.toolKey}>
                <tr className="border-b border-border/60 last:border-0">
                  <td className="p-3 font-medium">{row.toolKey}</td>
                  <td className="p-3 text-xs text-muted-foreground">
                    {[
                      row.ownValue.read && t("Read", "Lesen"),
                      row.ownValue.modify && t("Modify", "Verändern"),
                    ]
                      .filter(Boolean)
                      .join(", ") || "—"}
                  </td>
                  <td className="p-3 text-xs text-muted-foreground">
                    {row.ownValue.approvalActions.join(", ") || "—"}
                    {row.ownValue.approvalEur != null && ` ≥ ${row.ownValue.approvalEur} €`}
                  </td>
                  {level === "agent" && <td className="p-3">{row.loginPicker ?? "—"}</td>}
                  {level === "agent" && (
                    <td className="p-3">
                      <StatusBadge status={row.status} t={t} />
                    </td>
                  )}
                  {level === "department" && (
                    <td className="p-3 text-xs text-muted-foreground">
                      {row.deviationCount
                        ? t(
                            `${row.deviationCount.count} of ${row.deviationCount.total} agents`,
                            `${row.deviationCount.count} von ${row.deviationCount.total} Agents`,
                          )
                        : "—"}
                    </td>
                  )}
                  <td className="p-3 text-right">
                    {/* While this row is expanded, the panel below owns Save/Cancel --
                        a second row-level toggle here would be a duplicate control
                        with the identical label and action. */}
                    {!isEditing && (
                      <button
                        type="button"
                        onClick={() => {
                          setDraft(row.ownValue);
                          setEditingKey(row.toolKey);
                        }}
                        className="rounded-md border border-border px-2.5 py-1 text-xs font-medium text-foreground transition hover:bg-accent"
                      >
                        {t("Edit", "Bearbeiten")}
                      </button>
                    )}
                  </td>
                </tr>
                {isEditing && draft && (
                  <tr className="border-b border-border/60 last:border-0">
                    <td className="p-3" colSpan={level === "agent" ? 6 : 5}>
                      <ToolGuardrailEditorPanel
                        toolKey={row.toolKey}
                        connection={row.connection}
                        ceiling={level === "agent" ? row.ceilingPolicy : null}
                        value={draft}
                        onChange={setDraft}
                        onCancel={() => setEditingKey(null)}
                        onSave={async () => {
                          const ok = await onSave(row.toolKey, draft);
                          if (ok) setEditingKey(null);
                        }}
                        saving={saving}
                        agentId={level === "agent" ? agentId : undefined}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      <div className="border-t border-border p-3">
        <button
          type="button"
          onClick={() => setPickerOpen(true)}
          disabled={addableNames.length === 0}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Plus className="h-3.5 w-3.5" /> {t("Add tool", "Tool hinzufügen")}
        </button>
      </div>

      {pickerOpen && (
        <AddToolPicker
          addableNames={addableNames}
          connections={connections}
          departmentId={level === "agent" ? departmentId : undefined}
          agentId={level === "agent" ? agentId : undefined}
          onAdd={(name, policy, connectionId) => {
            onAdd(name, policy, connectionId);
            setPickerOpen(false);
          }}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}
