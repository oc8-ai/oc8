// A turn's tool steps, inline, one quiet line each (spec §2.1). The one file
// in the conversation module that reads a run's toolCalls -- see
// src/test/steps-never-in-the-message-stream.test.ts.
import { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";

import { formatMs } from "@/lib/format";
import { useLang, useT } from "@/lib/i18n";
import { buildRunSteps, skippedKnowledgeBases, type RunStepRow } from "@/lib/run-steps";
import { resolveToolLabel, useToolLabels } from "@/lib/tool-labels";
import { cn } from "@/lib/utils";

import { TOOL_PREVIEWS, toolKind } from "./tool-previews";

export interface StepListRun {
  state: string;
  steps: number;
  toolCalls: Array<Record<string, unknown>>;
  stepTimings?: Array<Record<string, unknown>>;
}

function tokensFor(
  step: number | null,
  timings: Array<Record<string, unknown>> | undefined,
): { tokensIn: number; tokensOut: number } | null {
  const entry = timings?.find((s) => s.step === step);
  if (!entry || typeof entry.tokensIn !== "number" || typeof entry.tokensOut !== "number")
    return null;
  return { tokensIn: entry.tokensIn, tokensOut: entry.tokensOut };
}

function Step({
  row,
  timings,
  labelsOnly,
}: {
  row: RunStepRow;
  timings?: Array<Record<string, unknown>>;
  labelsOnly: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const Preview = TOOL_PREVIEWS[toolKind(row)];
  const tokens = tokensFor(row.step, timings);
  // A question to the reader is parked, not refused: the runtime records it
  // like a denied call, but it is no error.
  const question = row.tool === "ask_user";
  return (
    <li className="text-xs text-muted-foreground">
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "min-w-0 flex-1",
            !question && row.state === "failed" && "text-status-error",
            !question && row.state === "denied" && "text-status-error",
          )}
        >
          <Preview row={row} />
        </span>
        {row.durationMs !== null && (
          <span className="shrink-0 font-mono tabular-nums">{formatMs(row.durationMs)}</span>
        )}
        {!labelsOnly && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-label={t(`Details: ${row.label}`, `Details: ${row.label}`)}
            className="inline-flex min-h-9 shrink-0 items-center gap-0.5 rounded-md px-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronRight
              className={cn("size-3.5 transition-transform duration-150", open && "rotate-90")}
              aria-hidden
            />
            {t("Details", "Details")}
          </button>
        )}
      </div>
      {open && !labelsOnly && (
        <div className="mt-1 space-y-1 rounded-md border border-border bg-muted/40 p-2 font-mono text-xs">
          <pre className="whitespace-pre-wrap break-words">{JSON.stringify(row.args, null, 2)}</pre>
          {row.result && (
            <pre className="whitespace-pre-wrap break-words">
              {row.result}
              {row.resultShortened ? " …" : ""}
            </pre>
          )}
          {row.reason && <p>{row.reason}</p>}
          <p>
            {row.durationMs !== null ? formatMs(row.durationMs) : "—"}
            {tokens
              ? ` · ${t(`${tokens.tokensIn} → ${tokens.tokensOut} tokens`, `${tokens.tokensIn} → ${tokens.tokensOut} Tokens`)}`
              : ""}
          </p>
        </div>
      )}
    </li>
  );
}

/** `labelsOnly`: a colleague's run (Ruling 15) -- each step's label and
 *  duration, never a Details expander with its arguments or result. */
export function StepList({ run, labelsOnly = false }: { run: StepListRun; labelsOnly?: boolean }) {
  const t = useT();
  const { lang } = useLang();
  const connection = useMemo(
    () =>
      (run.toolCalls.find((c) => typeof c.connection === "string" && c.connection)?.connection as
        | string
        | undefined) ?? null,
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
  if (steps.rows.length === 0) return null;
  // A pinned or released base this model could not search, said once: it
  // otherwise looks merely empty.
  const skipped = skippedKnowledgeBases(steps.rows);
  return (
    <>
      <ol className="space-y-1" aria-label={t("Steps", "Schritte")}>
        {steps.rows.map((row) => (
          <Step key={row.key} row={row} timings={run.stepTimings} labelsOnly={labelsOnly} />
        ))}
      </ol>
      {skipped.map((name) => (
        <p key={name} className="text-xs text-muted-foreground">
          {t(
            `${name} can't be searched with this model – it needs a local embedding model.`,
            `${name} kann mit diesem Modell nicht durchsucht werden – dafür braucht es ein lokales Embedding-Modell.`,
          )}
        </p>
      ))}
    </>
  );
}
