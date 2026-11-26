import { toast } from "sonner";
import type { Condition, GuardrailValue, UsageLimit } from "@/components/guardrail-preset-picker";
import {
  ToolGuardrailTable,
  type ToolGuardrailRow,
  describeGuardrailSaveError,
} from "@/components/tool-guardrail-table";
import { useMcpConnections } from "@/lib/hooks";
import { useT } from "@/lib/i18n";

type FrameTools = Record<string, Record<string, unknown>>;

/** The tool-access guardrail editor of a department frame. Shared by a
 *  department's Guardrails tab and the personal-departments default frame:
 *  the caller supplies the tools and how to save them. */
export function FrameGuardrailsEditor({
  tools,
  deviationCounts,
  agentCount,
  saveTools,
  saving,
}: {
  tools: FrameTools;
  deviationCounts: Record<string, number>;
  agentCount: number;
  saveTools: (tools: FrameTools) => Promise<void>;
  saving: boolean;
}) {
  const t = useT();
  const { data: connections = [] } = useMcpConnections();
  const toolKeys = Object.keys(tools);
  const connectionByName = new Map(connections.map((c) => [c.name, c] as const));
  // A key that exists but is disabled is not a row above, so it must stay
  // addable here -- otherwise a once-disabled tool can never come back.
  const addableNames = connections.map((c) => c.name).filter((n) => !tools[n]?.enabled);

  function toGuardrailValue(key: string): GuardrailValue {
    const raw = (tools[key] ?? {}) as Record<string, unknown>;
    return {
      read: raw.read !== undefined ? Boolean(raw.read) : true,
      modify: Boolean(raw.modify),
      approvalActions: Array.isArray(raw.approval_actions)
        ? (raw.approval_actions as string[])
        : [],
      approvalEur: typeof raw.approval_eur === "number" ? (raw.approval_eur as number) : null,
      only: Array.isArray(raw.only) ? (raw.only as string[]) : [],
      conditions: Array.isArray(raw.conditions) ? (raw.conditions as Condition[]) : [],
      limits: Array.isArray(raw.limits) ? (raw.limits as UsageLimit[]) : [],
    };
  }

  function persistOne(key: string, next: GuardrailValue): Promise<boolean> {
    const merged = {
      ...tools,
      [key]: {
        // Spread the tool's own previous entry first -- fields this panel
        // doesn't model (default_connection_id, the department's own default
        // login for agents with no pin of their own) must survive an edit
        // here, not silently disappear. A fixed literal deletes any field
        // it forgets, the same class of bug `toDepartmentFrame`'s own doc
        // comment used to warn about before this panel replaced it.
        ...((tools[key] ?? {}) as Record<string, unknown>),
        enabled: true,
        read: next.read,
        modify: next.modify,
        approval_eur: next.approvalEur,
        approval_actions: next.approvalActions,
        only: next.only,
        conditions: next.conditions,
        limits: next.limits,
      },
    };
    return saveTools(merged).then(
      () => {
        toast.success(t("Guardrails saved", "Guardrails gespeichert"), { description: key });
        return true;
      },
      (error: unknown) => {
        toast.error(describeGuardrailSaveError(error, t));
        return false;
      },
    );
  }

  function addTool(name: string, policy: GuardrailValue | null) {
    persistOne(
      name,
      policy ?? {
        read: true,
        modify: false,
        approvalActions: [],
        approvalEur: null,
        only: [],
        conditions: [],
        limits: [],
      },
    );
  }

  const rows: ToolGuardrailRow[] = toolKeys
    .filter((k) => tools[k]?.enabled)
    .map((key) => ({
      toolKey: key,
      connection: connectionByName.get(key),
      ceilingPolicy: null,
      ownValue: toGuardrailValue(key),
      status: null,
      deviationCount: { count: deviationCounts[key] ?? 0, total: agentCount },
    }));

  return (
    <>
      <ConfigSectionHeader
        hint={t("tool access and guardrails", "Tool-Zugriff und Guardrails")}
        title={t("Guardrails", "Guardrails")}
      />
      {/* The table carries the "Add tool" button, so it must render even with
          zero rows -- otherwise a fresh department can never get its first
          tool, and every connection assigned to it stays unusable. */}
      {rows.length === 0 && (
        <p className="mt-3 rounded-md border border-dashed border-border/70 bg-background/30 p-4 text-center text-xs text-muted-foreground">
          {t("No tools enabled yet.", "Noch keine Tools aktiviert.")}
        </p>
      )}
      <div className="mt-3">
        <ToolGuardrailTable
          level="department"
          rows={rows}
          addableNames={addableNames}
          connections={connections}
          onSave={persistOne}
          onAdd={addTool}
          saving={saving}
        />
      </div>
    </>
  );
}

function ConfigSectionHeader({ hint, title }: { hint: string; title: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-widest text-muted-foreground">{hint}</div>
      <h3 className="mt-0.5 font-serif text-lg">{title}</h3>
    </div>
  );
}
