// One run's steps, in order, in business language. The only timeline: the
// chat mounts it (copilot-run-activity.tsx), the My Work activity widget
// mounts it, and the needs-me queue mounts it inside a row.
//
// INVARIANT (workplace design §3.2, this spec §2.4): a step never renders
// inside the message stream. Messages render text; this renders steps. A
// component that puts a tool call into a message list is a defect --
// src/test/steps-never-in-the-message-stream.test.ts enforces it.

import { useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  PauseCircle,
  ShieldX,
  TriangleAlert,
} from "lucide-react";

import { formatMs } from "@/lib/format";
import { useLang, useT } from "@/lib/i18n";
import { buildRunSteps, type RunStepRow, type StepState } from "@/lib/run-steps";
import { resolveToolLabel, useToolLabels } from "@/lib/tool-labels";
import { cn } from "@/lib/utils";

export interface TimelineRun {
  id: string;
  agentId?: string;
  state: string;
  steps: number;
  phase?: string | null;
  toolCalls: Array<Record<string, unknown>>;
  stepTimings?: Array<Record<string, unknown>>;
}

function StateGlyph({ state }: { state: StepState }) {
  const className = "mt-0.5 h-3.5 w-3.5 shrink-0";
  switch (state) {
    case "running":
      return (
        <Loader2 className={cn(className, "animate-spin text-[color:var(--status-running)]")} />
      );
    case "failed":
      return <TriangleAlert className={cn(className, "text-[color:var(--status-error)]")} />;
    case "denied":
      return <ShieldX className={cn(className, "text-[color:var(--status-error)]")} />;
    case "awaiting_approval":
      return <PauseCircle className={cn(className, "text-[color:var(--status-warning)]")} />;
    default:
      return <CheckCircle2 className={cn(className, "text-[color:var(--status-success)]")} />;
  }
}

function Row({ row, agentId }: { row: RunStepRow; agentId?: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const args = Object.entries(row.args);
  const hasDetail = args.length > 0 || !!row.result || !!row.reason;

  return (
    <li className="px-3 py-1">
      <button
        type="button"
        onClick={() => hasDetail && setOpen((v) => !v)}
        className="flex w-full items-start gap-2 text-left"
        aria-expanded={hasDetail ? open : undefined}
      >
        <StateGlyph state={row.state} />
        <span className="min-w-0 flex-1 truncate">{row.label}</span>
        {row.state !== "running" && row.durationMs !== null && (
          <span className="shrink-0 tabular-nums text-muted-foreground">
            {formatMs(row.durationMs)}
          </span>
        )}
        {hasDetail &&
          (open ? (
            <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          ))}
      </button>

      {open && (
        <div className="mt-1 space-y-2 border-l border-border pl-4">
          {args.length > 0 && (
            <dl className="space-y-0.5">
              {args.map(([key, value]) => (
                <div key={key} className="grid grid-cols-[minmax(0,8rem)_1fr] gap-2">
                  <dt className="truncate text-[10px] uppercase tracking-widest text-muted-foreground">
                    {key}
                  </dt>
                  <dd className="min-w-0 break-words font-mono text-[11px]">
                    {typeof value === "object" && value !== null
                      ? JSON.stringify(value)
                      : String(value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}

          {row.reason && (
            <p className="text-[11px] text-[color:var(--status-warning)]">
              {row.state === "awaiting_approval"
                ? t(
                    `Waiting for a decision: ${row.reason}`,
                    `Wartet auf eine Entscheidung: ${row.reason}`,
                  )
                : t(`Not allowed: ${row.reason}`, `Nicht erlaubt: ${row.reason}`)}
            </p>
          )}

          {row.result && (
            <div>
              <div className="text-[10px] uppercase tracking-widest text-muted-foreground">
                {t("What came back", "Was zurückkam")}
              </div>
              <p className="mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px] text-muted-foreground">
                {row.result}
              </p>
              {row.resultShortened && (
                <p className="mt-0.5 text-[10px] text-muted-foreground">
                  {t(
                    "Shortened — only the first part of a result is kept.",
                    "Gekürzt — es wird nur der Anfang eines Ergebnisses gespeichert.",
                  )}
                  {agentId && (
                    <>
                      {" "}
                      <a href={`/agents/${agentId}`} className="text-primary hover:underline">
                        {t("Open the agent", "Agenten öffnen")}
                      </a>
                    </>
                  )}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </li>
  );
}

function Group({
  rows,
  connection,
  agentId,
}: {
  rows: RunStepRow[];
  connection: string | null;
  agentId?: string;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);

  // A group of one is a row, not a group -- collapsing it would hide a step
  // behind a header that says the same thing.
  if (rows.length === 1) return <Row row={rows[0]} agentId={agentId} />;

  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1 text-left text-muted-foreground"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0" />
        )}
        <span className="min-w-0 flex-1 truncate">
          {connection
            ? t(`${connection} · ${rows.length} actions`, `${connection} · ${rows.length} Aktionen`)
            : t(`${rows.length} actions`, `${rows.length} Aktionen`)}
        </span>
      </button>
      {open && (
        <ul className="border-l border-border pl-2">
          {rows.map((row) => (
            <Row key={row.key} row={row} agentId={agentId} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function RunStepTimeline({
  run,
  defaultOpen = false,
  className,
}: {
  run: TimelineRun;
  defaultOpen?: boolean;
  className?: string;
}) {
  const t = useT();
  const { lang } = useLang();
  const [open, setOpen] = useState(defaultOpen);

  // One connection per run in practice (a run is bound to one), taken from
  // the calls themselves rather than from the run: the record is what the
  // label has to match.
  const connection = useMemo(
    () =>
      (run.toolCalls.find((call) => typeof call.connection === "string" && call.connection !== "")
        ?.connection as string | undefined) ?? null,
    [run.toolCalls],
  );
  const { data: catalogue } = useToolLabels(connection);

  const steps = useMemo(
    () =>
      buildRunSteps({
        state: run.state,
        steps: run.steps,
        toolCalls: run.toolCalls,
        stepTimings: run.stepTimings,
        label: (call) => resolveToolLabel(call, catalogue, t, lang),
      }),
    [run.state, run.steps, run.toolCalls, run.stepTimings, catalogue, t, lang],
  );

  const summary = steps.live
    ? run.phase
      ? t(
          `Working · ${run.phase} · step ${steps.stepCount}`,
          `Arbeitet · ${run.phase} · Schritt ${steps.stepCount}`,
        )
      : t(`Working · step ${steps.stepCount}`, `Arbeitet · Schritt ${steps.stepCount}`)
    : t(
        `${steps.stepCount} steps${steps.totalDurationMs !== null ? ` · ${formatMs(steps.totalDurationMs)}` : ""}`,
        `${steps.stepCount} Schritte${steps.totalDurationMs !== null ? ` · ${formatMs(steps.totalDurationMs)}` : ""}`,
      );

  return (
    <div className={cn("text-xs", className)} data-testid="run-step-timeline">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left"
        aria-expanded={open}
      >
        {steps.live ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
        ) : (
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-[color:var(--status-success)]" />
        )}
        <span className="min-w-0 flex-1 truncate">{summary}</span>
        <span className="shrink-0 text-muted-foreground">
          {open ? t("hide", "ausblenden") : t("show", "anzeigen")}
        </span>
      </button>

      {open && (
        <>
          {steps.detailUnavailable && (
            <p className="px-3 pb-2 text-muted-foreground">
              {t(
                "Step detail is not available for this run.",
                "Für diesen Lauf liegen keine Einzelschritte vor.",
              )}
            </p>
          )}
          {steps.truncated > 0 && (
            <p className="px-3 pb-1 text-muted-foreground">
              {t(
                `${steps.truncated} earlier steps are not shown.`,
                `${steps.truncated} frühere Schritte werden nicht gezeigt.`,
              )}
            </p>
          )}
          <ul className="pb-2">
            {steps.groups
              ? steps.groups.map((g, i) => (
                  <Group
                    key={`${g.connection ?? "core"}-${i}`}
                    rows={g.rows}
                    connection={g.connection}
                    agentId={run.agentId}
                  />
                ))
              : steps.rows.map((row) => <Row key={row.key} row={row} agentId={run.agentId} />)}
            {steps.live && (
              <li className="flex items-center gap-2 px-3 py-1 text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />
                <span className="min-w-0 flex-1 truncate">{t("Working…", "Arbeitet …")}</span>
              </li>
            )}
          </ul>
        </>
      )}
    </div>
  );
}
