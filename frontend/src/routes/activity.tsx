import { createFileRoute } from "@tanstack/react-router";
import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { Panel } from "@/components/app-shell";
import { useActivity, useAgents } from "@/lib/hooks";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";

export const Route = createFileRoute("/activity")({
  component: ActivityPage,
});

const statuses = ["All", "success", "warning", "error", "info"] as const;

function ActivityPage() {
  const t = useT();
  const { data: activity = [] } = useActivity();
  // Filter dropdown over every agent, not a paginated list view.
  const { data: agentsPage } = useAgents({ pageSize: 200 });
  const agents = agentsPage?.items ?? [];
  const [agent, setAgent] = useState<string>("all");
  const [status, setStatus] = useState<(typeof statuses)[number]>("All");
  const [openId, setOpenId] = useState<string | null>(null);

  const items = activity.filter(
    (i) => (agent === "all" || i.agentId === agent) && (status === "All" || i.status === status),
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">{t("Filter:", "Filter:")}</span>
        <select
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          className="rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none focus:border-primary/50"
        >
          <option value="all">{t("All agents", "Alle Agenten")}</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
        <div className="flex gap-1">
          {statuses.map((s) => {
            const label =
              s === "All"
                ? t("All", "Alle")
                : s === "success"
                  ? t("success", "Erfolg")
                  : s === "warning"
                    ? t("warning", "Warnung")
                    : s === "error"
                      ? t("error", "Fehler")
                      : t("info", "Info");
            return (
              <button
                key={s}
                onClick={() => setStatus(s)}
                className={cn(
                  "rounded-md border px-2.5 py-1 text-xs",
                  status === s
                    ? "border-primary bg-primary/10 text-primary"
                    : "border-border bg-panel text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      <Panel>
        <ul className="divide-y divide-border">
          {items.map((it) => {
            const a = agents.find((x) => x.id === it.agentId);
            const color =
              it.status === "success"
                ? "var(--status-running)"
                : it.status === "warning"
                  ? "var(--status-warning)"
                  : it.status === "error"
                    ? "var(--status-error)"
                    : "var(--primary)";
            const isOpen = openId === it.id;
            return (
              <li key={it.id}>
                <button
                  onClick={() => setOpenId(isOpen ? null : it.id)}
                  className="grid w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 px-5 py-3 text-left hover:bg-primary/5"
                >
                  {isOpen ? (
                    <ChevronDown className="h-4 w-4 text-muted-foreground" />
                  ) : (
                    <ChevronRight className="h-4 w-4 text-muted-foreground" />
                  )}
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 text-sm">
                      <span
                        className="rounded-full px-2 py-0.5 text-[10px] font-medium"
                        style={{
                          color,
                          background: `color-mix(in oklab, ${color} 12%, transparent)`,
                          border: `1px solid color-mix(in oklab, ${color} 30%, transparent)`,
                        }}
                      >
                        {it.status === "success"
                          ? t("success", "Erfolg")
                          : it.status === "warning"
                            ? t("warning", "Warnung")
                            : it.status === "error"
                              ? t("error", "Fehler")
                              : t("info", "Info")}
                      </span>
                      <span className="font-medium">{a?.name ?? t("System", "System")}</span>
                      <span className="truncate text-muted-foreground">— {it.message}</span>
                    </div>
                  </div>
                  <span className="font-mono text-xs text-muted-foreground">{it.time}</span>
                </button>
                {isOpen && (
                  <div className="border-t border-border bg-background/30 px-14 py-3 text-xs text-muted-foreground">
                    {it.detail?.startsWith("http://") || it.detail?.startsWith("https://") ? (
                      <a
                        href={it.detail}
                        target="_blank"
                        rel="noreferrer"
                        className="text-primary underline"
                      >
                        {t("Open record", "Datensatz öffnen")}
                      </a>
                    ) : (
                      (it.detail ??
                        t("No further details for this entry.", "Keine weiteren Details zu diesem Eintrag."))
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </Panel>
    </div>
  );
}