// The effort an admin configures on a model or an agent (spec 2026-11-11 §8):
// one of the three levels, stored as low/medium/high so a provider that reads
// the free-form value keeps getting what it understands, or a custom value
// forwarded verbatim.
import { useState } from "react";

import { useT } from "@/lib/i18n";
import { levelLabel, type ReasoningLevel } from "@/lib/reasoning-levels";

type Choice = "" | "low" | "medium" | "high" | "custom";

const PRESETS: ReadonlyArray<{ value: Exclude<Choice, "" | "custom">; level: ReasoningLevel }> = [
  { value: "low", level: "fast" },
  { value: "medium", level: "balanced" },
  { value: "high", level: "thorough" },
];

/** Which entry of the select a stored value is. A level key reads as its
 *  low/medium/high value; anything else is a custom value. */
export function effortChoice(value: string): Choice {
  const key = value.trim().toLowerCase();
  if (key === "") return "";
  if (key === "low" || key === "fast") return "low";
  if (key === "medium" || key === "balanced") return "medium";
  if (key === "high" || key === "thorough") return "high";
  return "custom";
}

const FIELD =
  "w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60";

export function EffortField({
  value,
  onChange,
  emptyLabel,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  /** What "nothing set" means here: "Not set" on a model, "Inherited" on an agent. */
  emptyLabel: string;
  disabled?: boolean;
}) {
  const t = useT();
  // Picking "Custom value" keeps the text box open even while it is empty
  // or holds a word that happens to be a level.
  const [custom, setCustom] = useState(() => effortChoice(value) === "custom");
  const choice: Choice = custom ? "custom" : effortChoice(value);
  return (
    <div className="space-y-2">
      <select
        aria-label={t("Effort", "Effort")}
        value={choice}
        disabled={disabled}
        onChange={(e) => {
          const next = e.target.value as Choice;
          if (next === "custom") {
            setCustom(true);
            return;
          }
          setCustom(false);
          onChange(next);
        }}
        className={FIELD}
      >
        <option value="">{emptyLabel}</option>
        {PRESETS.map((p) => (
          <option key={p.value} value={p.value}>
            {levelLabel(p.level, t)}
          </option>
        ))}
        <option value="custom">{t("Custom value", "Eigener Wert")}</option>
      </select>
      {choice === "custom" && (
        <input
          type="text"
          aria-label={t("Custom effort value", "Eigener Effort-Wert")}
          placeholder="e.g. max"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className={FIELD}
        />
      )}
    </div>
  );
}
