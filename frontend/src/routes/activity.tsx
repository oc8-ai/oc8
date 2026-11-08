import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import { useState } from "react";
import { Panel } from "@/components/app-shell";
import { ChatMarkdown } from "@/components/chat-markdown";
import { RunFailureReason } from "@/components/run-reasoning";
import { RunStepTimeline } from "@/components/run-step-timeline";
import { useActivity, useAgents, useRun } from "@/lib/hooks";
import { RUN_STATE_COLOR } from "@/lib/run-state";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";

export interface ActivitySearch {
  /** A run to open on top of the feed -- where an answer's run chip
   *  ("oc8://run/<id>") lands. */
  run?: string;
}

export const Route = createFileRoute("/activity")({
  validateSearch: (search: Record<string, unknown>): ActivitySearch =>
    typeof search.run === "string" && search.run ? { run: search.run } : {},
  component: ActivityRoute,
});

function ActivityRoute() {
  const { run } = Route.useSearch();
  const navigate = useNavigate();
  return (
    <ActivityPage
      runId={run ?? null}
      onCloseRun={() => void navigate({ to: "/activity", search: {} })}
    />
  );
}

/** One run, opened from a link: who ran it, how it ended, its answer and its
 *  steps. */
function RunFocus({
  runId,
  agentName,
  onClose,
}: {
  runId: string;
  agentName: (agentId: string) => string | undefined;
  onClose?: () => void;
}) {
  const t = useT();
  const { data: run, isError } = useRun(runId);
  return (
    <Panel>
      <section aria-label={t("Run", "Lauf")} className="space-y-3 p-4">
        <div className="flex items-center gap-2 text-sm">
          <span className="font-medium">
            {run ? (agentName(run.agentId) ?? t("Agent", "Agent")) : t("Run", "Lauf")}
          </span>
          {run && (
            <span
              className="rounded-full px-2 py-0.5 text-xs"
              style={{ color: RUN_STATE_COLOR[run.state] }}
            >
              {run.state}
            </span>
          )}
          <span className="truncate font-mono text-xs text-muted-foreground">{runId}</span>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              aria-label={t("Close run", "Lauf schließen")}
              className="ml-auto rounded-md p-1 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="size-4" aria-hidden />
            </button>
          )}
        </div>
        {isError && (
          <p className="text-xs text-muted-foreground">
            {t("This run could not be loaded.", "Dieser Lauf konnte nicht geladen werden.")}
          </p>
        )}
        {run?.state === "failed" && <RunFailureReason reason={run.failureReason ?? run.output} />}
        {run?.output && run.state !== "failed" && <ChatMarkdown text={run.output} />}
        {run && <RunStepTimeline run={run} defaultOpen />}
      </section>
    </Panel>
  );
}

const statuses = ["All", "success", "warning", "error", "info"] as const;

export function ActivityPage({
  runId = null,
  onCloseRun,
}: {
  runId?: string | null;
  onCloseRun?: () => void;
} = {}) {
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
      {runId && (
        <RunFocus
          key={runId}
          runId={runId}
          agentName={(id) => agents.find((a) => a.id === id)?.name}
          onClose={onCloseRun}
        />
      )}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs uppercase tracking-wider text-muted-foreground">
          {t("Filter:", "Filter:")}
        </span>
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
                      t(
                        "No further details for this entry.",
                        "Keine weiteren Details zu diesem Eintrag.",
                      ))
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
