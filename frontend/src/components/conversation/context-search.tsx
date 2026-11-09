// frontend/src/components/conversation/context-search.tsx
// The + menu's "Knowledge base" and "oc8 object": a short picking flow over
// GET /chat/context/search. Fully by keyboard: arrows move, Enter picks, Esc
// closes. An unconnected data source cannot be picked; it offers "Connect".
import { useId, useState, type KeyboardEvent } from "react";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { useContextSearch, type ContextItem } from "@/lib/conversation-api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { kindIcon, kindLabel } from "./context-icons";

export interface ContextSearchDialogProps {
  open: boolean;
  /** Only these kinds; every kind the member may see when left out. */
  kinds?: string[];
  title?: string;
  onPick: (item: ContextItem) => void;
  onClose: () => void;
}

export function ContextSearchDialog({ open, onClose, ...rest }: ContextSearchDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-lg rounded-lg p-5">
        {/* Mounted only while open, so every opening starts from scratch. */}
        <SearchPanel {...rest} />
      </DialogContent>
    </Dialog>
  );
}

function SearchPanel({ kinds, title, onPick }: Omit<ContextSearchDialogProps, "open" | "onClose">) {
  const t = useT();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const items = useContextSearch(query, kinds, { recents: true });
  const pickable = items.filter((i) => i.connected !== false);
  const current = pickable[Math.min(active, pickable.length - 1)];

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (pickable.length === 0) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive(
        (i) => (Math.min(i, pickable.length - 1) + step + pickable.length) % pickable.length,
      );
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (current) onPick(current);
    }
  }

  return (
    <div className="min-w-0 space-y-3">
      <DialogTitle className="font-serif text-xl font-semibold">
        {title ?? t("Add context", "Kontext hinzufügen")}
      </DialogTitle>
      <DialogDescription className="text-xs">
        {t(
          "Attach it to your next message. You can pin it to the chat from there.",
          "Häng es an deine nächste Nachricht. Von dort kannst du es an den Chat heften.",
        )}
      </DialogDescription>
      <input
        type="search"
        autoFocus
        aria-label={t("Search", "Suchen")}
        aria-controls={listId}
        value={query}
        maxLength={100}
        onChange={(e) => {
          setQuery(e.target.value);
          setActive(0);
        }}
        onKeyDown={onKeyDown}
        placeholder={t("Search knowledge, files, agents …", "Wissen, Dateien, Agenten suchen …")}
        className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
      <ul id={listId} className="max-h-80 space-y-1 overflow-y-auto" aria-live="polite">
        {items.length === 0 && (
          <li className="px-2 py-3 text-sm text-muted-foreground">
            {t("Nothing you can open matches that.", "Nichts, was du öffnen darfst, passt dazu.")}
          </li>
        )}
        {items.map((item) => {
          const Icon = kindIcon(item.kind);
          const unconnected = item.connected === false;
          return (
            <li key={`${item.kind}:${item.id}`} className="flex min-w-0 items-center gap-2">
              <button
                type="button"
                onClick={() => onPick(item)}
                disabled={unconnected}
                aria-current={item === current || undefined}
                className={cn(
                  "flex min-h-11 min-w-0 flex-1 items-center gap-2 rounded-md px-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:text-muted-foreground disabled:hover:bg-transparent",
                  item === current && "bg-muted",
                )}
              >
                <Icon className="size-4 shrink-0" aria-hidden />
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
                <span className="shrink-0 text-xs text-muted-foreground">
                  {unconnected ? t("Not connected", "Nicht verbunden") : kindLabel(item.kind, t)}
                </span>
              </button>
              {unconnected && (
                <a
                  href="/knowledge"
                  aria-label={t(`Connect ${item.label}`, `${item.label} verbinden`)}
                  className="inline-flex min-h-9 shrink-0 items-center rounded-md px-2 text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {t("Connect", "Verbinden")}
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
