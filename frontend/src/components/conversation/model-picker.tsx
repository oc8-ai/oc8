// frontend/src/components/conversation/model-picker.tsx
// Per-chat model choice (spec §2.3). Only what the backend lists is offered;
// the backend refuses anything else and the engine re-checks at run time.
// A pick shows at once; a refused one reverts with an inline sentence
// (DESIGN.md "Error": a failed save reverts the control).
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Check, ChevronDown } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { useSessionModels, useSetSessionModel } from "@/lib/conversation-api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function ModelPicker({
  sessionId,
  value,
  disabled = false,
  ensureSession,
  onPick,
}: {
  sessionId: string | null;
  value: string | null;
  disabled?: boolean;
  /** A new chat lists its options once it exists: opening the picker
   *  creates it (as a file drop does). */
  ensureSession?: () => Promise<string>;
  /** Given, a pick is the caller's to hold (Ruling 51: a new chat writes it
   *  before its first message); left out, the pick is written at once. */
  onPick?: (modelConfigId: string | null) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  // The pick in flight, shown until the server answers. `undefined` = none.
  const [pending, setPending] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  // A pick belongs to the chat and model it was made against: when either
  // changes, the pick in flight and its error no longer apply.
  const scope = `${sessionId ?? ""}|${value ?? ""}`;
  const [seenScope, setSeenScope] = useState(scope);
  const request = useRef(0);
  if (seenScope !== scope) {
    setSeenScope(scope);
    setPending(undefined);
    setError(null);
  }
  const root = useRef<HTMLDivElement>(null);
  // The chat this picker created to list its options, until the caller
  // passes it back as `sessionId`.
  const [created, setCreated] = useState<string | null>(null);
  const [createdFor, setCreatedFor] = useState(sessionId);
  if (createdFor !== sessionId) {
    setCreatedFor(sessionId);
    setCreated(null);
  }
  const listFrom = sessionId ?? created;
  const { data: options = [], isLoading: loadingOptions } = useSessionModels(listFrom, true);
  const setModel = useSetSessionModel(sessionId);
  const trigger = useRef<HTMLButtonElement>(null);
  const items = useRef<Array<HTMLButtonElement | null>>([]);
  const shown = pending === undefined ? value : pending;
  const selectedIndex = Math.max(
    0,
    options.findIndex((o) => o.id === shown),
  );
  const [active, setActive] = useState(selectedIndex);
  const current = options[selectedIndex];
  // "Auto" names the model it resolves to, so the effective model is visible.
  const fallback = options.find((o) => o.isDefault);

  const hasOptions = options.length > 0;
  useEffect(() => {
    if (open && hasOptions) {
      setActive(selectedIndex);
      items.current[selectedIndex]?.focus();
    }
    // Only on opening (or once a new chat's options arrive): moving with the
    // arrows must not snap back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, hasOptions]);

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

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }

  function toggle() {
    if (!open && !listFrom && ensureSession) {
      ensureSession().then(setCreated, () =>
        setError(
          t(
            "The models couldn't be loaded. Try again.",
            "Die Modelle konnten nicht geladen werden. Versuch es noch einmal.",
          ),
        ),
      );
    }
    setOpen((v) => !v);
  }

  function pick(index: number) {
    const option = options[index];
    if (!option) return;
    close();
    if (option.id === shown) return;
    setError(null);
    if (onPick) {
      onPick(option.id);
      return;
    }
    setPending(option.id);
    const mine = ++request.current;
    setModel.mutate(option.id, {
      onSuccess: () => {
        if (mine === request.current) setPending(undefined);
      },
      onError: (err: unknown) => {
        if (mine !== request.current) return;
        setPending(undefined);
        setError(
          err instanceof ApiError && err.status === 422
            ? t(
                "That model isn't available for this chat. Pick another one.",
                "Dieses Modell ist für diesen Chat nicht freigegeben. Wähl ein anderes.",
              )
            : t(
                "The model couldn't be changed. Try again.",
                "Das Modell konnte nicht geändert werden. Versuch es noch einmal.",
              ),
        );
      },
    });
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (options.length === 0) return;
      const next = (active + (e.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
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

  const isAuto = !current || current.id === null;
  const label = isAuto ? t("Auto", "Auto") : current.label;
  const effective = isAuto && fallback ? `${label} (${fallback.label})` : label;
  return (
    <div ref={root} className="relative">
      <button
        ref={trigger}
        type="button"
        disabled={disabled || (!sessionId && !ensureSession)}
        onClick={toggle}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={t(`Model: ${effective}`, `Modell: ${effective}`)}
        className="inline-flex h-9 items-center gap-1 rounded-md px-2 text-xs text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60"
      >
        {label}
        {isAuto && fallback && <span className="hidden sm:inline">· {fallback.label}</span>}
        <ChevronDown className="size-3.5" aria-hidden />
      </button>
      {open && (
        <div
          role="radiogroup"
          aria-label={t("Model", "Modell")}
          onKeyDown={onKeyDown}
          onBlur={(e) => {
            if (!root.current?.contains(e.relatedTarget as Node | null)) setOpen(false);
          }}
          className="absolute bottom-full left-0 z-50 mb-1 w-64 rounded-lg border border-border bg-popover p-1 shadow-lg"
        >
          {options.length === 0 && (loadingOptions || !listFrom) && (
            <div className="space-y-1 p-1" aria-busy="true">
              <Skeleton className="h-9 w-full rounded-md" />
              <Skeleton className="h-9 w-full rounded-md" />
            </div>
          )}
          {options.map((o, i) => (
            <button
              key={o.id ?? "auto"}
              ref={(el) => {
                items.current[i] = el;
              }}
              type="button"
              role="radio"
              aria-checked={i === selectedIndex}
              tabIndex={i === active ? 0 : -1}
              onClick={() => pick(i)}
              className={cn(
                "flex min-h-11 w-full items-center gap-2 rounded-md px-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                i === active && "bg-muted",
              )}
            >
              <span className="size-4 shrink-0">
                {i === selectedIndex && <Check className="size-4" aria-hidden />}
              </span>
              <span className="min-w-0 flex-1 truncate">
                {o.id === null ? t("Auto", "Auto") : o.label}
              </span>
              {o.locality === "local" && <span className="text-micro">{t("Local", "Lokal")}</span>}
              {o.isDefault && (
                <span className="text-xs text-muted-foreground">{t("default", "Standard")}</span>
              )}
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
