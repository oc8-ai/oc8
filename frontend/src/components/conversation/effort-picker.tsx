// frontend/src/components/conversation/effort-picker.tsx
// How hard the agent thinks in this chat (spec 2026-11-11 §3). The same
// dropdown pattern as the model picker: shown only while the chat's effective
// model can think, a pick shows at once, and a refused one reverts with an
// inline sentence (DESIGN.md "Error").
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Brain, Check, ChevronDown } from "lucide-react";

import { ApiError } from "@/lib/api";
import {
  useSessionModels,
  useSetSessionEffort,
  type ChatModelOptionDTO,
} from "@/lib/conversation-api";
import { useT } from "@/lib/i18n";
import {
  levelHint,
  levelLabel,
  REASONING_LEVELS,
  type ReasoningLevel,
} from "@/lib/reasoning-levels";
import { cn } from "@/lib/utils";

/** The option the chat runs on: its own pick, else "Auto", which carries the
 *  agent's default model's capability. */
export function effectiveOption(
  options: ChatModelOptionDTO[],
  modelValue: string | null,
): ChatModelOptionDTO | undefined {
  return options.find((o) => o.id === modelValue) ?? options.find((o) => o.id === null);
}

const CHOICES: ReadonlyArray<ReasoningLevel | null> = [null, ...REASONING_LEVELS];

export function EffortPicker({
  sessionId,
  agentId,
  modelValue,
  value,
  disabled = false,
  onPick,
}: {
  sessionId: string | null;
  /** A chat that does not exist yet lists its agent's options. */
  agentId?: string;
  /** The chat's model pick (null = Auto), to know whether it can think. */
  modelValue: string | null;
  /** The chat's level, or null for the default. */
  value: ReasoningLevel | null;
  disabled?: boolean;
  /** Given, a pick is the caller's to hold (a new chat writes it before its
   *  first message); left out, the pick is written at once. */
  onPick?: (level: ReasoningLevel | null) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  // The pick in flight, shown until the server answers. `undefined` = none.
  const [pending, setPending] = useState<ReasoningLevel | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const scope = `${sessionId ?? ""}|${value ?? ""}`;
  const [seenScope, setSeenScope] = useState(scope);
  const request = useRef(0);
  if (seenScope !== scope) {
    setSeenScope(scope);
    setPending(undefined);
    setError(null);
  }
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const items = useRef<Array<HTMLButtonElement | null>>([]);
  const { data: options = [], isLoading } = useSessionModels(sessionId, true, agentId);
  const setEffort = useSetSessionEffort(sessionId);
  const shown = pending === undefined ? value : pending;
  const selectedIndex = Math.max(0, CHOICES.indexOf(shown));
  const [active, setActive] = useState(selectedIndex);

  useEffect(() => {
    if (open) {
      setActive(selectedIndex);
      items.current[selectedIndex]?.focus();
    }
    // Only on opening: moving with the arrows must not snap back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    request.current += 1;
  }, [scope]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const effective = effectiveOption(options, modelValue);
  // A reader cannot list the chat's models; the chat's own pick still shows.
  const visible = effective ? effective.supportsReasoning === true : !isLoading && value !== null;
  if (!visible) return null;

  const fallback = effective?.defaultEffort ?? "balanced";
  const defaultText =
    fallback === "custom" ? t("Custom setting", "Eigene Einstellung") : levelLabel(fallback, t);
  const label = shown === null ? defaultText : levelLabel(shown, t);

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }

  function pick(index: number) {
    const level = CHOICES[index];
    if (level === undefined) return;
    close();
    if (level === shown) return;
    setError(null);
    if (onPick) {
      onPick(level);
      return;
    }
    setPending(level);
    const mine = ++request.current;
    setEffort.mutate(level, {
      onSuccess: () => {
        if (mine === request.current) setPending(undefined);
      },
      onError: (err: unknown) => {
        if (mine !== request.current) return;
        setPending(undefined);
        setError(
          err instanceof ApiError && err.status === 422
            ? t(
                "That reasoning level isn't available. Pick another one.",
                "Diese Denkstufe ist nicht verfügbar. Wähl eine andere.",
              )
            : t(
                "The reasoning level couldn't be changed. Try again.",
                "Die Denkstufe konnte nicht geändert werden. Versuch es noch einmal.",
              ),
        );
      },
    });
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = (active + (e.key === "ArrowDown" ? 1 : -1) + CHOICES.length) % CHOICES.length;
      setActive(next);
      items.current[next]?.focus();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      pick(active);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") {
      setOpen(false);
    }
  }

  return (
    <div ref={root} className="relative">
      <button
        ref={trigger}
        type="button"
        disabled={disabled || (!sessionId && !agentId)}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={t(`Reasoning: ${label}`, `Denkstufe: ${label}`)}
        className="inline-flex h-9 items-center gap-1 rounded-md px-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60"
      >
        <Brain className="size-3.5" aria-hidden />
        {label}
        <ChevronDown className="size-3.5" aria-hidden />
      </button>
      {open && (
        <div
          role="radiogroup"
          aria-label={t("Reasoning", "Denkstufe")}
          onKeyDown={onKeyDown}
          onBlur={(e) => {
            if (!root.current?.contains(e.relatedTarget as Node | null)) setOpen(false);
          }}
          className="absolute bottom-full left-0 z-50 mb-1 w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-border bg-popover p-1 shadow-lg"
        >
          {CHOICES.map((level, i) => (
            <button
              key={level ?? "default"}
              ref={(el) => {
                items.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={i === selectedIndex}
              tabIndex={i === active ? 0 : -1}
              onClick={() => pick(i)}
              className={cn(
                "flex min-h-11 w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                i === active && "bg-muted",
              )}
            >
              <span className="mt-0.5 size-4 shrink-0">
                {i === selectedIndex && <Check className="size-4" aria-hidden />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block">
                  {level === null
                    ? `${t("Default", "Standard")} · ${defaultText}`
                    : levelLabel(level, t)}
                </span>
                {level !== null && (
                  <span className="block text-xs text-muted-foreground">{levelHint(level, t)}</span>
                )}
              </span>
            </button>
          ))}
        </div>
      )}
      {error && !open && (
        <p role="alert" className="absolute bottom-full left-0 mb-1 w-64 text-xs text-status-error">
          {error}
        </p>
      )}
    </div>
  );
}
