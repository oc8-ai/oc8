import { Fragment, useState } from "react";
import { Plus } from "lucide-react";
import { useT } from "@/lib/i18n";
import { ApiError } from "@/lib/api";
import type { McpConnection } from "@/lib/hooks";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";
import { ToolGuardrailEditorPanel } from "@/components/tool-guardrail-editor-panel";
import { AddToolPicker } from "@/components/add-tool-picker";
import { GuardrailLevelPicker } from "@/components/guardrail-level-picker";
import {
  LEVELS,
  disabledLevelsFor,
  hasLimits,
  levelOf,
  valueForLevel,
  type Level,
} from "@/lib/guardrail-levels";

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
  /** What the level above allows: the department frame on `level="agent"`,
   * the effective department ∩ agent value on `level="member"`. Levels looser
   * than this are greyed out and `valueForLevel` never goes past it. */
  ceilingPolicy: GuardrailValue | null;
  ownValue: GuardrailValue;
  status: "inherited" | "narrowed" | "agent-only" | null;
  deviationCount?: { count: number; total: number };
  loginPicker?: React.ReactNode;
  /** One sentence of effect under the row ("The sales agent asks you before
   * it sends a mail."), announced politely after a change. */
  effect?: string;
  /** Who set the value the row shows. */
  source?: "member" | "agent" | "department" | "capa_default";
  /** Member level only: who set the ceiling, for the "set by …" reason on
   * greyed-out levels. Defaults to the department. */
  ceilingSource?: "agent" | "department";
  /** Member level: the level and ceiling level the backend computed. They
   * include the member's own approval rules, which `ownValue`'s shape cannot
   * carry, so they win over `levelOf(ownValue)` / `levelOf(ceilingPolicy)`. */
  level?: Level;
  ceilingLevel?: Level;
  /** Backend-computed "with limits" -- same reason as `level`. */
  limits?: boolean;
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

function sourceLabel(
  source: NonNullable<ToolGuardrailRow["source"]>,
  t: (en: string, de: string) => string,
): string {
  switch (source) {
    case "member":
      return t("you", "von dir");
    case "agent":
      return t("agent", "Agent");
    case "department":
      return t("department", "Abteilung");
    case "capa_default":
      return t("default", "Standard");
  }
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
  onSaveLevel,
  onReset,
}: {
  // "member" is a member's own tightening on top of department ∩ agent
  // (D3a): same four levels, "Ask me first" wording, no "Add tool".
  level: "department" | "agent" | "member";
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
  // Agent and member level: passed through to ToolGuardrailEditorPanel so
  // its per-function inner table can call the interpret endpoint. Left
  // undefined at the department level, which has no per-agent identity to
  // interpret against (see that panel's own comment on this prop).
  agentId?: string;
  // Member level: a picked level is saved as `{level}`, never as a policy
  // built from `ownValue` (the backend maps it, approval rules included).
  // Same contract as `onSave`: resolves false on a failed save.
  onSaveLevel?: (toolKey: string, level: Level) => Promise<boolean>;
  // Member level: removes only the member's own entry ("Reset"), offered on
  // rows whose source is the member.
  onReset?: (toolKey: string) => void;
}) {
  const t = useT();
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [draft, setDraft] = useState<GuardrailValue | null>(null);
  // A picked level shows at once; on a failed save the row snaps back to
  // `ownValue` and says so in one sentence under it (never a toast alone).
  const [pending, setPending] = useState<Record<string, Level>>({});
  const [failed, setFailed] = useState<Record<string, boolean>>({});
  const scope = level === "member" ? "member" : "other";
  const columnCount = level === "agent" ? 5 : level === "department" ? 4 : 3;

  function ceilingOf(row: ToolGuardrailRow): GuardrailValue | null {
    return level === "department" ? null : row.ceilingPolicy;
  }

  function disabledLevels(row: ToolGuardrailRow): Level[] {
    if (row.ceilingLevel) {
      const max = LEVELS.indexOf(row.ceilingLevel);
      return LEVELS.filter((_, i) => i > max);
    }
    return disabledLevelsFor(ceilingOf(row));
  }

  function disabledReason(row: ToolGuardrailRow): string {
    if (level === "member" && row.ceilingSource === "agent") {
      return t("set by agent", "vom Agenten festgelegt");
    }
    return t("set by department", "von der Abteilung festgelegt");
  }

  async function pickLevel(row: ToolGuardrailRow, picked: Level) {
    setPending((p) => ({ ...p, [row.toolKey]: picked }));
    setFailed((f) => ({ ...f, [row.toolKey]: false }));
    let ok: boolean;
    if (onSaveLevel) {
      ok = await onSaveLevel(row.toolKey, picked);
    } else {
      const next = valueForLevel(picked, ceilingOf(row), row.ownValue);
      if (editingKey === row.toolKey) setDraft(next);
      ok = await onSave(row.toolKey, next);
    }
    setPending(({ [row.toolKey]: _done, ...rest }) => rest);
    if (!ok) setFailed((f) => ({ ...f, [row.toolKey]: true }));
  }

  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="border-b border-border text-[11px] uppercase tracking-wider text-muted-foreground">
            <th className="p-3">{t("Tool", "Tool")}</th>
            <th className="p-3">{t("Level", "Stufe")}</th>
            {level === "agent" && <th className="p-3">{t("Login", "Login")}</th>}
            {level === "agent" && <th className="p-3">{t("Status", "Status")}</th>}
            {level === "department" && <th className="p-3">{t("Deviations", "Abweichungen")}</th>}
            <th className="p-3" />
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isEditing = row.toolKey === editingKey;
            const current = pending[row.toolKey] ?? row.level ?? levelOf(row.ownValue);
            return (
              <Fragment key={row.toolKey}>
                <tr className="border-b border-border/60 last:border-0">
                  <td className="p-3 align-top">
                    <div className="font-medium">{row.toolKey}</div>
                    <div aria-live="polite" className="mt-1 text-xs text-muted-foreground">
                      {failed[row.toolKey] ? (
                        <span className="text-destructive">
                          {t(
                            "Couldn't save the new level, so it's back to what it was.",
                            "Die neue Stufe konnte nicht gespeichert werden, sie ist wieder wie vorher.",
                          )}
                        </span>
                      ) : (
                        row.effect && <span>{row.effect}</span>
                      )}
                      {row.source && (
                        <span className="ml-2 rounded-full border border-border px-2 py-0.5 text-[11px]">
                          {sourceLabel(row.source, t)}
                        </span>
                      )}
                    </div>
                  </td>
                  <td className="p-3 align-top">
                    <div className="flex flex-wrap items-center gap-x-2">
                      <GuardrailLevelPicker
                        name={row.toolKey}
                        value={current}
                        disabledLevels={disabledLevels(row)}
                        disabledReason={disabledReason(row)}
                        scope={scope}
                        busy={row.toolKey in pending}
                        onChange={(picked) => void pickLevel(row, picked)}
                      />
                      {(row.limits ?? hasLimits(row.ownValue)) && (
                        <span className="text-xs text-muted-foreground">
                          {t("with limits", "mit Grenzen")}
                        </span>
                      )}
                    </div>
                  </td>
                  {level === "agent" && <td className="p-3 align-top">{row.loginPicker ?? "—"}</td>}
                  {level === "agent" && (
                    <td className="p-3 align-top">
                      <StatusBadge status={row.status} t={t} />
                    </td>
                  )}
                  {level === "department" && (
                    <td className="p-3 align-top text-xs text-muted-foreground">
                      {row.deviationCount
                        ? t(
                            `${row.deviationCount.count} of ${row.deviationCount.total} agents`,
                            `${row.deviationCount.count} von ${row.deviationCount.total} Agents`,
                          )
                        : "—"}
                    </td>
                  )}
                  <td className="p-3 text-right align-top">
                    {/* While this row is expanded, the panel below owns Save/Cancel --
                        a second row-level toggle here would be a duplicate control
                        with the identical label and action. */}
                    {onReset && row.source === "member" && !isEditing && (
                      <button
                        type="button"
                        onClick={() => onReset(row.toolKey)}
                        className="min-h-11 rounded-md px-2.5 text-xs font-medium text-muted-foreground transition hover:bg-accent hover:text-foreground"
                      >
                        {t("Reset", "Zurücksetzen")}
                      </button>
                    )}
                    {!isEditing && (
                      <button
                        type="button"
                        onClick={() => {
                          setDraft(row.ownValue);
                          setEditingKey(row.toolKey);
                        }}
                        className="min-h-11 rounded-md px-2.5 text-xs font-medium text-muted-foreground transition hover:bg-accent hover:text-foreground"
                      >
                        {t("More", "Mehr")}
                      </button>
                    )}
                  </td>
                </tr>
                {isEditing && draft && (
                  <tr className="border-b border-border/60 last:border-0">
                    <td className="p-3" colSpan={columnCount}>
                      <ToolGuardrailEditorPanel
                        toolKey={row.toolKey}
                        connection={row.connection}
                        ceiling={ceilingOf(row)}
                        ceilingLabel={
                          level === "member"
                            ? t("Department and agent allow", "Abteilung und Agent erlauben")
                            : undefined
                        }
                        value={draft}
                        onChange={setDraft}
                        onCancel={() => setEditingKey(null)}
                        onSave={async () => {
                          const ok = await onSave(row.toolKey, draft);
                          if (ok) setEditingKey(null);
                        }}
                        saving={saving}
                        agentId={level === "department" ? undefined : agentId}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {/* Members tighten what they were given; adding a tool is the agent's
          and department's business. */}
      {level !== "member" && (
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
      )}

      {pickerOpen && level !== "member" && (
        <AddToolPicker
          addableNames={addableNames}
          connections={connections}
          departmentId={level === "agent" ? departmentId : undefined}
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
