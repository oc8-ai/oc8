// The three reasoning levels (spec 2026-11-11 §2): their names and hints in
// one place, for the chat's control and the model and agent forms.
import type { ChatModelOptionDTO } from "@/lib/conversation-api";

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

export type EffortChoice = "" | "low" | "medium" | "high" | "custom";

/** Which entry of the select a stored value is. A level key reads as its
 *  low/medium/high value; anything else is a custom value. */
export function effortChoice(value: string): EffortChoice {
  const key = value.trim().toLowerCase();
  if (key === "") return "";
  if (key === "low" || key === "fast") return "low";
  if (key === "medium" || key === "balanced") return "medium";
  if (key === "high" || key === "thorough") return "high";
  return "custom";
}

/** The option the chat runs on: its own pick, else "Auto", which carries the
 *  agent's default model's capability. */
export function effectiveOption(
  options: ChatModelOptionDTO[],
  modelValue: string | null,
): ChatModelOptionDTO | undefined {
  return options.find((o) => o.id === modelValue) ?? options.find((o) => o.id === null);
}
