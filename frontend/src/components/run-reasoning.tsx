// A run's model steps: what each step used against its answer budget, and
// the model's reasoning as a collapsible "Thought" block -- live for the step
// in progress ("run.reasoning_delta"), stored for every finished one
// (stepTimings). Mounted by the step timeline (chat, My Work, needs-me) and
// by the agent page's run panel.

import { useState } from "react";
import { Brain, ChevronDown, ChevronRight, TriangleAlert } from "lucide-react";

import type { LiveReasoning } from "@/lib/hooks";
import { useLang, useT } from "@/lib/i18n";
import { formatTokens, modelSteps, type ModelStep } from "@/lib/run-model-steps";
import { cn } from "@/lib/utils";

export function ReasoningBlock({
  text,
  live = false,
  defaultOpen = false,
}: {
  text: string;
  live?: boolean;
  defaultOpen?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div data-testid="reasoning-block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground"
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0" />
        )}
        <Brain className={cn("h-3 w-3 shrink-0", live && "animate-pulse")} />
        <span>{live ? t("Thinking…", "Denkt nach …") : t("Thought", "Nachgedacht")}</span>
      </button>
      {open && (
        <p className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words border-l border-border pl-3 text-[11px] italic leading-relaxed text-muted-foreground">
          {text}
        </p>
      )}
    </div>
  );
}

export function StepBudget({ step }: { step: ModelStep }) {
  const t = useT();
  const { lang } = useLang();
  const n = (value: number) => formatTokens(value, lang);
  const parts: string[] = [];
  if (step.tokensIn !== null)
    parts.push(t(`${n(step.tokensIn)} in`, `${n(step.tokensIn)} Eingabe`));
  if (step.tokensOut !== null) {
    parts.push(
      step.reasoningTokens !== null
        ? t(
            `${n(step.tokensOut)} out (${n(step.reasoningTokens)} reasoning)`,
            `${n(step.tokensOut)} Antwort (${n(step.reasoningTokens)} Reasoning)`,
          )
        : t(`${n(step.tokensOut)} out`, `${n(step.tokensOut)} Antwort`),
    );
  }
  if (step.maxTokens !== null) {
    parts.push(t(`budget ${n(step.maxTokens)}`, `Budget ${n(step.maxTokens)}`));
  }
  if (parts.length === 0) return null;
  return (
    <span
      className={cn(
        "tabular-nums",
        step.budgetUsedUp ? "text-[color:var(--status-warning)]" : "text-muted-foreground",
      )}
      title={
        step.budgetUsedUp
          ? t(
              "This step used its whole answer budget.",
              "Dieser Schritt hat sein ganzes Antwortbudget verbraucht.",
            )
          : undefined
      }
    >
      {parts.join(" · ")}
    </span>
  );
}

/** Every model step that recorded usage or reasoning, plus the live
 *  reasoning of the step in progress. Renders nothing for a run that has
 *  neither, so older runs look exactly as they did. */
export function RunModelSteps({
  stepTimings,
  liveReasoning,
  live,
  className,
}: {
  stepTimings?: Array<Record<string, unknown>>;
  liveReasoning?: LiveReasoning;
  live: boolean;
  className?: string;
}) {
  const t = useT();
  const steps = modelSteps(stepTimings);
  const finished = new Set(steps.map((s) => s.step));
  const showLive =
    live && !!liveReasoning && !!liveReasoning.text && !finished.has(liveReasoning.step);
  if (steps.length === 0 && !showLive) return null;

  return (
    <ul className={cn("space-y-1.5", className)} data-testid="run-model-steps">
      {steps.map((step) => (
        <li key={step.step} className="space-y-0.5">
          <div className="flex flex-wrap items-center gap-x-2 text-[11px]">
            <span className="text-muted-foreground">
              {t(`Step ${step.step}`, `Schritt ${step.step}`)}
            </span>
            <StepBudget step={step} />
          </div>
          {step.reasoning && <ReasoningBlock text={step.reasoning} />}
        </li>
      ))}
      {showLive && liveReasoning && (
        <li className="space-y-0.5">
          <div className="text-[11px] text-muted-foreground">
            {t(`Step ${liveReasoning.step}`, `Schritt ${liveReasoning.step}`)}
          </div>
          <ReasoningBlock text={liveReasoning.text} live defaultOpen />
        </li>
      )}
    </ul>
  );
}

/** Why a failed run failed. Falls back to a plain "failed" line only when the
 *  run recorded no reason at all. */
export function RunFailureReason({
  reason,
  className,
}: {
  reason?: string | null;
  className?: string;
}) {
  const t = useT();
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive",
        className,
      )}
      data-testid="run-failure-reason"
    >
      <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div className="min-w-0 space-y-0.5">
        <div className="font-medium">
          {t("This run failed.", "Dieser Lauf ist fehlgeschlagen.")}
        </div>
        {reason ? (
          <p className="whitespace-pre-wrap break-words text-destructive/90">{reason}</p>
        ) : (
          <p className="text-destructive/80">
            {t("No reason was recorded.", "Es wurde kein Grund festgehalten.")}
          </p>
        )}
      </div>
    </div>
  );
}
