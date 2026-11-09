// frontend/src/components/conversation/pinned-bar.tsx
// "Pinned" above the composer (C3): the chat's pinned context, each chip
// removable by the chat's owner. Labels are the server's, shown as plain text.
import { X } from "lucide-react";

import { refusedRefs, useSetPins, type ContextRefViewDTO } from "@/lib/conversation-api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { kindIcon } from "./context-icons";

export function PinnedBar({
  sessionId,
  pins,
  readOnly = false,
}: {
  sessionId: string;
  pins: ContextRefViewDTO[];
  /** An overseer's view: the pins show, nothing can be removed. */
  readOnly?: boolean;
}) {
  const t = useT();
  const setPins = useSetPins(sessionId);
  if (pins.length === 0) return null;
  const refused = refusedRefs(setPins.error);
  const refusedLabels = refused.map(
    (r) => pins.find((p) => p.kind === r.kind && p.id === r.id)?.label ?? r.id,
  );
  const error = !setPins.error
    ? null
    : refusedLabels.length > 0
      ? t(
          `Not available to you: ${refusedLabels.join(", ")}. Unpin it, then try again.`,
          `Für dich nicht verfügbar: ${refusedLabels.join(", ")}. Löse es und versuch es noch einmal.`,
        )
      : t(
          "Your pins were not saved. Try again.",
          "Deine angehefteten Einträge wurden nicht gespeichert. Versuch es noch einmal.",
        );
  return (
    <div className="space-y-1">
      <ul
        className="flex min-w-0 flex-wrap items-center gap-1"
        aria-label={t("Pinned", "Angeheftet")}
      >
        <li className="text-micro px-1" aria-hidden>
          {t("Pinned", "Angeheftet")}
        </li>
        {pins.map((p) => {
          const Icon = kindIcon(p.kind);
          return (
            <li
              key={`${p.kind}:${p.id}`}
              className={cn(
                "inline-flex h-9 min-w-0 max-w-full items-center gap-1 rounded-full border border-border pl-3 text-xs",
                readOnly && "pr-3",
                !p.available && "text-muted-foreground",
              )}
            >
              <Icon className="size-3.5 shrink-0" aria-hidden />
              <span className="min-w-0 truncate">{p.label}</span>
              {!p.available && (
                <span className="shrink-0">
                  · <span>{t("Source not available", "Quelle nicht verfügbar")}</span>
                </span>
              )}
              {!readOnly && (
                <button
                  type="button"
                  aria-label={t(`Unpin ${p.label}`, `${p.label} lösen`)}
                  onClick={() =>
                    setPins.mutate(
                      pins
                        .filter((x) => !(x.kind === p.kind && x.id === p.id))
                        .map(({ kind, id }) => ({ kind, id })),
                    )
                  }
                  className="grid size-9 shrink-0 place-items-center rounded-full text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              )}
            </li>
          );
        })}
      </ul>
      {error && (
        <p role="alert" className="px-1 text-xs text-status-error">
          {error}
        </p>
      )}
    </div>
  );
}
