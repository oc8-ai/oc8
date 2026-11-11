// The three reasoning levels (spec 2026-11-11 §2): their names and hints in
// one place, for the chat's control and the model and agent forms.
export type ReasoningLevel = "fast" | "balanced" | "thorough";

export const REASONING_LEVELS: readonly ReasoningLevel[] = ["fast", "balanced", "thorough"];

export type Translate = (en: string, de?: string) => string;

export function levelLabel(level: ReasoningLevel, t: Translate): string {
  if (level === "fast") return t("Fast", "Schnell");
  if (level === "balanced") return t("Balanced", "Ausgewogen");
  return t("Thorough", "Gründlich");
}

export function levelHint(level: ReasoningLevel, t: Translate): string {
  if (level === "fast")
    return t("Answers right away, barely thinks", "Antwortet sofort, denkt kaum nach");
  if (level === "balanced") return t("The usual choice", "Der Normalfall");
  return t(
    "Thinks longer for tricky questions; slower and costs more",
    "Denkt länger nach, für knifflige Fragen; dauert länger und kostet mehr",
  );
}
