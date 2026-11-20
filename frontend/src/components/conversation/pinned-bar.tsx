// frontend/src/components/conversation/pinned-bar.tsx
// "Pinned" above the composer (C3): the chat's pinned context, each chip
// removable by the chat's owner. Labels are the server's, shown as plain text.
// The pin writes are the chat's one `useSetPins` mutation, shared with
// "Pin to this chat", so nothing can be changed while a write is running.
import { X } from "lucide-react";

import type { ContextRefViewDTO, PinChange } from "@/lib/conversation-api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { kindIcon } from "./context-icons";

export interface PinWriter {
  mutate: (change: PinChange) => void;
  isPending: boolean;
}

export function PinnedBar({
  pins,
  pinning,
  readOnly = false,
}: {
  pins: ContextRefViewDTO[];
  pinning: PinWriter;
  /** An overseer's view: the pins show, nothing can be removed. */
  readOnly?: boolean;
}) {
  const t = useT();
  if (pins.length === 0) return null;
  return (
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
                disabled={pinning.isPending}
                onClick={() => pinning.mutate({ remove: { kind: p.kind, id: p.id } })}
                className="grid size-9 shrink-0 place-items-center rounded-full text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              >
                <X className="size-3.5" aria-hidden />
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}
