// Everything waiting on this person, in one list. Three tables, one
// experience: approvals, clarifications, and runs parked with nobody's name
// on them. Splitting them across widgets is why nobody notices a parked run.
//
// Ordered by how much work each item blocks -- an approval holding a
// half-finished task outranks a standalone question -- and each row carries
// the step timeline of the run that raised it, so the approver sees the path
// that led to the request without leaving the tile.
//
// Deciding still happens in the approvals widget's panes; this is the queue,
// not a second decision surface. A row links to /workspace for that.

import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, HelpCircle, PauseCircle, ShieldCheck } from "lucide-react";

import { useApprovals, useClarifications, useRun, useRuns } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import { RunStepTimeline } from "@/components/run-step-timeline";
import { cn } from "@/lib/utils";

type Kind = "approval" | "clarification" | "parked";

interface QueueRow {
  kind: Kind;
  id: string;
  title: string;
  runId: string | null;
  agentId?: string | null;
  createdAt: string;
  /** Higher blocks more work. An item holding a run outranks one that holds
   *  nothing, and a run parked with no request against it at all is the one
   *  nobody is looking at, so it sits at the very top. */
  weight: number;
}

const PARKED_STATES = "waiting_for_approval,waiting_for_input";

export function NeedsMeWidget({
  agentId,
}: {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
  /** Only rows raised by this agent (the Copilot page's Waiting tab). */
  agentId?: string;
}) {
  const t = useT();
  const approvalsQuery = useApprovals("pending");
  const clarificationsQuery = useClarifications();
  const parkedQuery = useRuns({ state: PARKED_STATES, limit: 20 });
  const [openId, setOpenId] = useState<string | null>(null);

  const rows = useMemo<QueueRow[]>(() => {
    const approvals = approvalsQuery.data ?? [];
    const clarifications = clarificationsQuery.data ?? [];
    const parked = parkedQuery.data ?? [];

    const claimed = new Set<string>();
    const out: QueueRow[] = [];

    for (const a of approvals) {
      if (a.runId) claimed.add(a.runId);
      out.push({
        kind: "approval",
        id: a.id,
        title: a.title,
        runId: a.runId ?? null,
        agentId: a.agentId,
        createdAt: a.createdAt ?? "",
        weight: a.runId ? 2 : 1,
      });
    }
    for (const c of clarifications) {
      claimed.add(c.runId);
      out.push({
        kind: "clarification",
        id: c.id,
        title: c.question,
        runId: c.runId,
        agentId: c.agentId,
        createdAt: c.createdAt,
        weight: 2,
      });
    }
    // A run sitting in a waiting state with no approval and no question
    // against it: nobody has been asked, so nobody is going to notice.
    for (const run of parked) {
      if (claimed.has(run.id)) continue;
      out.push({
        kind: "parked",
        id: run.id,
        title: t(
          "A run is parked and nobody was asked",
          "Ein Lauf steht und niemand wurde gefragt",
        ),
        runId: run.id,
        agentId: run.agentId,
        createdAt: "",
        weight: 3,
      });
    }

    const scoped = agentId ? out.filter((r) => r.agentId === agentId) : out;
    return scoped.sort(
      (a, b) =>
        b.weight - a.weight || b.createdAt.localeCompare(a.createdAt) || a.id.localeCompare(b.id),
    );
  }, [approvalsQuery.data, clarificationsQuery.data, parkedQuery.data, agentId, t]);

  const loading =
    approvalsQuery.isPending || clarificationsQuery.isPending || parkedQuery.isPending;

  if (loading && rows.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-xs text-muted-foreground">
        {t("Loading…", "Wird geladen…")}
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-4 text-xs text-muted-foreground">
        {t("Nothing is waiting for you.", "Nichts wartet auf dich.")}
      </div>
    );
  }

  return (
    <div className="h-full divide-y divide-border overflow-y-auto text-xs">
      {rows.map((row) => (
        <div key={`${row.kind}-${row.id}`}>
          <button
            type="button"
            onClick={() =>
              setOpenId((v) => (v === `${row.kind}-${row.id}` ? null : `${row.kind}-${row.id}`))
            }
            className="flex w-full items-start gap-2 px-3 py-2 text-left"
            aria-expanded={openId === `${row.kind}-${row.id}`}
          >
            {row.kind === "clarification" ? (
              <HelpCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
            ) : row.kind === "parked" ? (
              <PauseCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--status-warning)]" />
            ) : (
              <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--status-warning)]" />
            )}
            <span className="min-w-0 flex-1 truncate font-medium">{row.title}</span>
            {openId === `${row.kind}-${row.id}` ? (
              <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            )}
          </button>
          {openId === `${row.kind}-${row.id}` && (
            <div className="pb-2">
              {row.runId ? (
                <RowTimeline runId={row.runId} />
              ) : (
                <p className="px-3 text-muted-foreground">
                  {t("No run is behind this one.", "Dahinter steht kein Lauf.")}
                </p>
              )}
              <a
                href="/workspace"
                className={cn("mt-1 inline-block px-3 text-primary hover:underline")}
              >
                {row.kind === "clarification"
                  ? t("Answer it", "Beantworten")
                  : t("Decide it", "Entscheiden")}
              </a>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

/** Fetched per expanded row, never for the whole list: a queue of twenty
 *  rows would otherwise be twenty run fetches for nineteen rows nobody
 *  opened. `useRun` writes the same ["run", id] cache the WS patchers keep
 *  current, so an open row stays live. */
function RowTimeline({ runId }: { runId: string }) {
  const t = useT();
  const { data: run } = useRun(runId);
  if (!run) {
    return (
      <p className="px-3 text-muted-foreground">{t("Loading the steps…", "Lädt die Schritte …")}</p>
    );
  }
  return (
    <RunStepTimeline
      run={{
        id: run.id,
        agentId: run.agentId,
        state: run.state,
        steps: run.steps,
        phase: run.phase,
        toolCalls: run.toolCalls,
        stepTimings: run.stepTimings,
      }}
      defaultOpen
    />
  );
}
