import { useT } from "@/lib/i18n";
import {
  limitKey,
  type GuardrailValue,
  type UsageLimit,
} from "@/components/guardrail-preset-picker";
import type { ToolUsage } from "@/lib/hooks-agent-detail";

// Usage limits ("at most 50 mails a day") on one tool's guardrail: which of
// its functions are counted together, how many successful calls a UTC day or
// hour allows, and -- at agent level, where the core counts them -- how many
// the agent has used in the current window. The core enforces them; this only
// edits `GuardrailValue.limits`, saved with the rest of the policy.
//
// A limit never ends up without a tool: one is added on a real function of
// the connection, and its last tool cannot be removed (remove the limit).
export function GuardrailUsageLimits({
  value,
  onChange,
  toolCatalog,
  usage,
}: {
  value: GuardrailValue;
  onChange: (next: GuardrailValue) => void;
  /** The connection's function names; the first limit added counts the first. */
  toolCatalog: string[];
  /** This tool's current windows (`GET /agents/{id}/tool-usage`); agent level
   * only. Undefined hides the usage line, an empty list shows 0 used. */
  usage?: ToolUsage[];
}) {
  const t = useT();
  const limits = value.limits ?? [];

  function setLimits(next: UsageLimit[]) {
    onChange({ ...value, limits: next });
  }

  function update(index: number, patch: Partial<UsageLimit>) {
    setLimits(limits.map((lim, i) => (i === index ? { ...lim, ...patch } : lim)));
  }

  function usedOf(limit: UsageLimit): ToolUsage | undefined {
    const key = limitKey(limit);
    return usage?.find((u) => limitKey({ tools: u.tools, max: 0, per: u.per }) === key);
  }

  function resetsLabel(limit: UsageLimit, row: ToolUsage | undefined): string {
    if (row) return `${new Date(row.resetsAt).toISOString().slice(11, 16)} UTC`;
    if (limit.per === "day") return "00:00 UTC";
    const next = new Date();
    next.setUTCHours(next.getUTCHours() + 1, 0, 0, 0);
    return `${next.toISOString().slice(11, 16)} UTC`;
  }

  const firstFree = toolCatalog[0];

  return (
    <div className="mt-5">
      <div className="mb-1 text-xs uppercase tracking-wider text-muted-foreground">
        {t("Usage limits", "Nutzungslimits")}
      </div>
      <p className="mb-2 text-xs text-muted-foreground">
        {t(
          "At most this many successful calls of the listed functions together, per UTC day or hour. The next call is refused until the count resets.",
          "Höchstens so viele erfolgreiche Aufrufe der genannten Funktionen zusammen, pro UTC-Tag oder -Stunde. Der nächste Aufruf wird abgelehnt, bis der Zähler neu beginnt.",
        )}
      </p>
      <div className="space-y-2">
        {limits.map((limit, index) => {
          const row = usedOf(limit);
          const addable = toolCatalog.filter((name) => !limit.tools.includes(name));
          return (
            <div
              key={`${limitKey(limit)}-${index}`}
              className="rounded-md border border-border/70 bg-background/30 p-3"
            >
              <div className="flex flex-wrap items-center gap-1.5">
                {limit.tools.map((tool) => (
                  <button
                    key={tool}
                    type="button"
                    disabled={limit.tools.length === 1}
                    aria-label={`${t("Stop counting", "Nicht mehr zählen")}: ${tool}`}
                    onClick={() =>
                      update(index, { tools: limit.tools.filter((name) => name !== tool) })
                    }
                    className="inline-flex items-center gap-1 rounded-full border border-border bg-background/40 px-2 py-0.5 font-mono text-xs text-foreground transition hover:bg-accent disabled:cursor-default disabled:hover:bg-background/40"
                  >
                    <span>{tool}</span>
                    {limit.tools.length > 1 && <span aria-hidden="true">×</span>}
                  </button>
                ))}
                {addable.length > 0 && (
                  <select
                    aria-label={t("Count also", "Auch zählen")}
                    value=""
                    onChange={(e) => {
                      if (e.target.value)
                        update(index, { tools: [...limit.tools, e.target.value] });
                    }}
                    className="rounded-md border border-border bg-background/40 px-2 py-0.5 text-xs text-muted-foreground outline-none focus:border-primary/50"
                  >
                    <option value="">{t("+ count also…", "+ auch zählen…")}</option>
                    {addable.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <span className="text-muted-foreground">{t("at most", "höchstens")}</span>
                <input
                  type="number"
                  min={0}
                  step={1}
                  aria-label={t("Maximum calls", "Höchstzahl Aufrufe")}
                  value={limit.max}
                  onChange={(e) => {
                    const max = Math.max(0, Math.floor(Number(e.target.value)));
                    if (Number.isFinite(max)) update(index, { max });
                  }}
                  className="w-24 rounded-md border border-border bg-background/40 px-3 py-1.5 text-sm text-foreground outline-none focus:border-primary/50"
                />
                <select
                  aria-label={t("Per", "Pro")}
                  value={limit.per}
                  onChange={(e) => update(index, { per: e.target.value as UsageLimit["per"] })}
                  className="rounded-md border border-border bg-background/40 px-2 py-1.5 text-sm text-foreground outline-none focus:border-primary/50"
                >
                  <option value="day">{t("per day", "pro Tag")}</option>
                  <option value="hour">{t("per hour", "pro Stunde")}</option>
                </select>
                <button
                  type="button"
                  onClick={() => setLimits(limits.filter((_, i) => i !== index))}
                  className="ml-auto rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:bg-accent hover:text-foreground"
                >
                  {t("Remove limit", "Limit entfernen")}
                </button>
              </div>
              {usage !== undefined && (
                <div className="mt-2 text-xs text-muted-foreground">
                  {limit.per === "day"
                    ? t("Used today", "Heute genutzt")
                    : t("Used this hour", "In dieser Stunde genutzt")}
                  {`: ${row?.used ?? 0} / ${limit.max} · `}
                  {t("resets at", "neu ab")} {resetsLabel(limit, row)}
                </div>
              )}
            </div>
          );
        })}
      </div>
      {firstFree !== undefined && (
        <button
          type="button"
          onClick={() => setLimits([...limits, { tools: [firstFree], max: 50, per: "day" }])}
          className="mt-2 rounded-md border border-dashed border-border px-3 py-1.5 text-xs text-muted-foreground transition hover:bg-accent hover:text-foreground"
        >
          {t("+ Add limit", "+ Limit hinzufügen")}
        </button>
      )}
    </div>
  );
}
