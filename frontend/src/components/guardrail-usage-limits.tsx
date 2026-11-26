import { useState } from "react";
import { useT } from "@/lib/i18n";
import {
  limitKey,
  type GuardrailValue,
  type UsageLimit,
} from "@/components/guardrail-preset-picker";
import type { ToolUsage } from "@/lib/hooks-agent-detail";

// Usage limits ("at most 50 mails a day") on one tool's guardrail: which of
// its functions are counted together, how many calls a UTC day or hour allows,
// and -- at agent level, where the core counts them -- how many the agent has
// used in the current window. The core enforces them; this only edits
// `GuardrailValue.limits`, saved with the rest of the policy.
//
// A call counts once it reached the tool, even if the tool reported an error:
// a send that timed out may still have gone out.
//
// At agent level the department's limits (`ceiling`) are in force too, and the
// tighter max wins: an agent row whose department allows less shows the
// department's number as the one in force, and a department limit the agent
// does not repeat is shown read-only, so the editor never displays a number
// nobody enforces.
//
// A limit never ends up without a tool: one is added on a function of the
// connection the person picks, and its last tool cannot be removed (remove the
// limit).
export function GuardrailUsageLimits({
  value,
  onChange,
  toolCatalog,
  usage,
  ceiling,
}: {
  value: GuardrailValue;
  onChange: (next: GuardrailValue) => void;
  /** The connection's function names, offered when a limit is added. */
  toolCatalog: string[];
  /** This tool's current windows (`GET /agents/{id}/tool-usage`); agent level
   * only. Undefined hides the usage line, an empty list shows 0 used. */
  usage?: ToolUsage[];
  /** The department's limits on this tool; agent level only. */
  ceiling?: UsageLimit[];
}) {
  const t = useT();
  const limits = value.limits ?? [];
  // What the counters are keyed by when the editor opened: a limit whose
  // counted functions change gets a new counter, starting at 0.
  const [openedWith] = useState(() => new Set(limits.map(limitKey)));

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

  function departmentsOf(limit: UsageLimit): UsageLimit | undefined {
    const key = limitKey(limit);
    return ceiling?.find((c) => limitKey(c) === key);
  }

  function resetsLabel(limit: UsageLimit, row: ToolUsage | undefined): string {
    if (row) return `${new Date(row.resetsAt).toISOString().slice(11, 16)} UTC`;
    if (limit.per === "day") return "00:00 UTC";
    const next = new Date();
    next.setUTCHours(next.getUTCHours() + 1, 0, 0, 0);
    return `${next.toISOString().slice(11, 16)} UTC`;
  }

  function usageLine(limit: UsageLimit, inForce: number) {
    if (usage === undefined) return null;
    const row = usedOf(limit);
    return (
      <div className="mt-2 text-xs text-muted-foreground">
        {limit.per === "day"
          ? t("Used today", "Heute genutzt")
          : t("Used this hour", "In dieser Stunde genutzt")}
        {`: ${row?.used ?? 0} / ${inForce} · `}
        {t("resets at", "neu ab")} {resetsLabel(limit, row)}
      </div>
    );
  }

  function setByDepartment(max: number) {
    return (
      <span className="rounded-full border border-border/70 px-2 py-0.5 text-[10px] uppercase tracking-wider text-muted-foreground">
        {t("Set by department", "Von der Abteilung festgelegt")}: {max}
      </span>
    );
  }

  const repeated = new Set(limits.map(limitKey));
  const departmentOnly = (ceiling ?? []).filter((c) => !repeated.has(limitKey(c)));
  // The functions no limit shown here counts yet: what "Add limit" offers.
  const counted = new Set([...limits, ...departmentOnly].flatMap((limit) => limit.tools));
  const unlimited = toolCatalog.filter((name) => !counted.has(name));

  return (
    <div className="mt-5">
      <div className="mb-1 text-xs uppercase tracking-wider text-muted-foreground">
        {t("Usage limits", "Nutzungslimits")}
      </div>
      <p className="mb-2 text-xs text-muted-foreground">
        {t(
          "At most this many calls of the listed functions together, per UTC day or hour. A call counts once it reached the tool, even if it reported an error. The next call is refused until the count resets.",
          "Höchstens so viele Aufrufe der genannten Funktionen zusammen, pro UTC-Tag oder -Stunde. Ein Aufruf zählt, sobald er das Tool erreicht hat, auch wenn es einen Fehler meldet. Der nächste Aufruf wird abgelehnt, bis der Zähler neu beginnt.",
        )}
      </p>
      <div className="space-y-2">
        {limits.map((limit, index) => {
          const addable = toolCatalog.filter((name) => !limit.tools.includes(name));
          const department = departmentsOf(limit);
          const inForce = department ? Math.min(department.max, limit.max) : limit.max;
          const restarts = openedWith.size > 0 && !openedWith.has(limitKey(limit));
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
              {restarts && (
                <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
                  {t(
                    "These functions are counted separately: today's count starts again at 0.",
                    "Diese Funktionen werden getrennt gezählt: Der Zähler für heute beginnt wieder bei 0.",
                  )}
                </p>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <span className="text-muted-foreground">{t("at most", "höchstens")}</span>
                <input
                  type="number"
                  min={0}
                  max={department?.max}
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
                {department && department.max < limit.max && setByDepartment(department.max)}
                <button
                  type="button"
                  onClick={() => setLimits(limits.filter((_, i) => i !== index))}
                  className="ml-auto rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition hover:bg-accent hover:text-foreground"
                >
                  {t("Remove limit", "Limit entfernen")}
                </button>
              </div>
              {usageLine(limit, inForce)}
            </div>
          );
        })}
        {departmentOnly.map((limit) => (
          <div
            key={`department-${limitKey(limit)}`}
            className="rounded-md border border-dashed border-border/70 bg-background/20 p-3"
          >
            <div className="flex flex-wrap items-center gap-1.5">
              {limit.tools.map((tool) => (
                <span
                  key={tool}
                  className="rounded-full border border-border/70 px-2 py-0.5 font-mono text-xs text-muted-foreground"
                >
                  {tool}
                </span>
              ))}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              <span>
                {t("at most", "höchstens")} {limit.max}{" "}
                {limit.per === "day" ? t("per day", "pro Tag") : t("per hour", "pro Stunde")}
              </span>
              {setByDepartment(limit.max)}
            </div>
            {usageLine(limit, limit.max)}
          </div>
        ))}
      </div>
      {unlimited.length > 0 && (
        // A limit never exists without a function, and none is picked for
        // the person: the first one in the catalog is whatever the tool
        // happens to list first (for email, mail_delete).
        <select
          aria-label={t("Add a limit for…", "Limit hinzufügen für…")}
          value=""
          onChange={(e) => {
            if (e.target.value)
              setLimits([...limits, { tools: [e.target.value], max: 50, per: "day" }]);
          }}
          className="mt-2 rounded-md border border-dashed border-border bg-background/40 px-3 py-1.5 text-xs text-muted-foreground outline-none transition hover:bg-accent hover:text-foreground focus:border-primary/50"
        >
          <option value="">{t("+ Add limit…", "+ Limit hinzufügen…")}</option>
          {unlimited.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}
