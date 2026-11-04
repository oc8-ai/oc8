// What each model step of a run used against its answer budget, and what the
// model thought, read out of the run's `stepTimings` (backend:
// oc8.runtime.step_record.step_timing_dto). Pure, so the fallbacks for old
// records and the "budget used up" rule are testable without a DOM.

export interface ModelStep {
  step: number;
  tokensIn: number | null;
  tokensOut: number | null;
  /** Null when the provider does not report it separately -- never 0. */
  reasoningTokens: number | null;
  /** The answer budget (max_tokens) the step's last model call had. */
  maxTokens: number | null;
  reasoning: string | null;
  /** The step's last call wrote as many tokens as its budget allowed. */
  budgetUsedUp: boolean;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value.trim() !== "" ? value : null;
}

/** One entry per step that recorded usage or reasoning, in step order.
 *  Records written before either existed are left out, not shown as zeros. */
export function modelSteps(stepTimings: Array<Record<string, unknown>> | undefined): ModelStep[] {
  const out: ModelStep[] = [];
  for (const entry of stepTimings ?? []) {
    const tokensIn = num(entry.tokensIn);
    const tokensOut = num(entry.tokensOut);
    const reasoningTokens = num(entry.reasoningTokens);
    const maxTokens = num(entry.maxTokens);
    const reasoning = str(entry.reasoning);
    if (tokensOut === null && maxTokens === null && reasoning === null) continue;
    out.push({
      step: num(entry.step) ?? out.length + 1,
      tokensIn,
      tokensOut,
      reasoningTokens,
      maxTokens,
      reasoning,
      // tokensOut adds up every call of the step (a retry included), so it
      // can exceed the last call's budget; at or above it means the budget
      // ran out at least once.
      budgetUsedUp: tokensOut !== null && maxTokens !== null && tokensOut >= maxTokens,
    });
  }
  return out;
}

export function formatTokens(n: number, lang: string): string {
  return new Intl.NumberFormat(lang === "de" ? "de-DE" : "en-US").format(n);
}
