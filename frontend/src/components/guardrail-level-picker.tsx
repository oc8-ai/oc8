import { useRef, useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { LEVELS, levelHint, levelLabel, type Level } from "@/lib/guardrail-levels";

// The level control (design review D7): a row shows only its current level as
// quiet text with a chevron -- a pop-up button, as in Apple's System
// Settings. It opens a popover holding a `radiogroup` of the four levels,
// each with one sentence of explanation. Arrow keys move, Enter picks, Esc
// closes and Radix returns focus to the button. A level the department or
// agent rules out stays focusable (`aria-disabled`, never `disabled`) and
// reads its reason, so a screen-reader user learns why it can't be picked.
export function GuardrailLevelPicker({
  value,
  disabledLevels,
  disabledReason,
  scope,
  onChange,
  name,
  busy = false,
}: {
  value: Level;
  disabledLevels: Level[];
  disabledReason: string;
  scope: "member" | "other";
  onChange: (level: Level) => void;
  /** What the level is for (e.g. the tool), prefixed to the button's
   * accessible name so a list of identical-looking buttons stays tellable. */
  name?: string;
  busy?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [focused, setFocused] = useState<Level>(value);
  const optionRefs = useRef<Partial<Record<Level, HTMLButtonElement | null>>>({});
  const label = levelLabel(value, scope, t);

  function focusLevel(level: Level) {
    setFocused(level);
    optionRefs.current[level]?.focus();
  }

  function pick(level: Level) {
    if (disabledLevels.includes(level)) return;
    setOpen(false);
    if (level !== value) onChange(level);
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const index = LEVELS.indexOf(focused);
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      event.preventDefault();
      focusLevel(LEVELS[(index + 1) % LEVELS.length]);
    } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      event.preventDefault();
      focusLevel(LEVELS[(index - 1 + LEVELS.length) % LEVELS.length]);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusLevel(LEVELS[0]);
    } else if (event.key === "End") {
      event.preventDefault();
      focusLevel(LEVELS[LEVELS.length - 1]);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      pick(focused);
    }
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (next) setFocused(value);
        setOpen(next);
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={name ? `${name}: ${label}` : label}
          aria-busy={busy || undefined}
          className="inline-flex min-h-11 items-center gap-1 rounded-md px-2 text-sm text-foreground transition-colors duration-200 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring motion-reduce:transition-none"
        >
          <span>{label}</span>
          <ChevronDown aria-hidden="true" className="h-4 w-4 text-muted-foreground" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-80 p-1"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          optionRefs.current[value]?.focus();
        }}
      >
        <div
          role="radiogroup"
          aria-label={name ? `${name}: ${t("Level", "Stufe")}` : t("Level", "Stufe")}
          onKeyDown={onKeyDown}
        >
          {LEVELS.map((level) => {
            const disabled = disabledLevels.includes(level);
            const checked = level === value;
            return (
              <button
                key={level}
                ref={(el) => {
                  optionRefs.current[level] = el;
                }}
                type="button"
                role="radio"
                aria-checked={checked}
                aria-disabled={disabled || undefined}
                tabIndex={level === focused ? 0 : -1}
                onFocus={() => setFocused(level)}
                onClick={() => pick(level)}
                className={cn(
                  "flex min-h-11 w-full items-start gap-2 rounded-sm px-2 py-2 text-left outline-none focus-visible:bg-accent focus-visible:ring-2 focus-visible:ring-ring",
                  disabled ? "cursor-not-allowed text-muted-foreground" : "hover:bg-accent",
                )}
              >
                <Check
                  aria-hidden="true"
                  className={cn("mt-0.5 h-4 w-4 shrink-0", checked ? "opacity-100" : "opacity-0")}
                />
                <span className="flex flex-col gap-0.5">
                  <span className="text-sm font-medium">{levelLabel(level, scope, t)}</span>
                  <span className="text-xs text-muted-foreground">
                    {disabled ? disabledReason : levelHint(level, t, scope)}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );
}
