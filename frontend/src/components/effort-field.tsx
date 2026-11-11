// The effort an admin configures on a model or an agent (spec 2026-11-11 §8):
// one of the three levels, stored as low/medium/high so a provider that reads
// the free-form value keeps getting what it understands, or a custom value
// forwarded verbatim.
import { useState } from "react";

import { useT } from "@/lib/i18n";
import {
  effortChoice,
  levelLabel,
  type EffortChoice,
  type ReasoningLevel,
} from "@/lib/reasoning-levels";

const PRESETS: ReadonlyArray<{
  value: Exclude<EffortChoice, "" | "custom">;
  level: ReasoningLevel;
}> = [
  { value: "low", level: "fast" },
  { value: "medium", level: "balanced" },
  { value: "high", level: "thorough" },
];

const FIELD =
  "w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50 disabled:cursor-not-allowed disabled:opacity-60";

export function EffortField({
  value,
  onChange,
  emptyLabel,
  disabled = false,
  id,
  hint,
}: {
  value: string;
  onChange: (value: string) => void;
  /** What "nothing set" means here: "Not set" on a model, "Inherited" on an agent. */
  emptyLabel: string;
  disabled?: boolean;
  /** Id of the select, so a visible caption can point at it. */
  id?: string;
  /** A small note under the select, e.g. where the shown value comes from. */
  hint?: string;
}) {
  const t = useT();
  // Picking "Custom value" keeps the text box open even while it is empty
  // or holds a word that happens to be a level.
  const [custom, setCustom] = useState(() => effortChoice(value) === "custom");
  const choice: EffortChoice = custom ? "custom" : effortChoice(value);
  return (
    <div className="space-y-2">
      <select
        id={id}
        aria-label={t("Effort", "Effort")}
        value={choice}
        disabled={disabled}
        onChange={(e) => {
          const next = e.target.value as EffortChoice;
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
          placeholder={t("e.g. max", "z. B. max")}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          className={FIELD}
        />
      )}
      {hint && <p className="text-[11px] text-muted-foreground">{hint}</p>}
    </div>
  );
}
