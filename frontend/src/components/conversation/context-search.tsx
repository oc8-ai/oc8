// frontend/src/components/conversation/context-search.tsx
// The + menu's "Knowledge base" and "oc8 object": a short picking flow over
// GET /chat/context/search. Fully by keyboard: the field keeps the focus and
// points at the active option (aria-activedescendant); arrows move, Enter
// picks, Esc closes. An unconnected data source cannot be picked; it is
// listed apart with "Connect".
import { useId, useState, type KeyboardEvent } from "react";

import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useContextSearchResults, type ContextItem } from "@/lib/conversation-api";
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
  /** Where focus goes once it closes; the trigger (a + menu item) is gone. */
  returnFocus?: () => void;
}

export function ContextSearchDialog({
  open,
  onClose,
  returnFocus,
  ...rest
}: ContextSearchDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent
        className="max-w-lg rounded-lg p-5"
        onCloseAutoFocus={(e) => {
          if (!returnFocus) return;
          e.preventDefault();
          returnFocus();
        }}
      >
        {/* Mounted only while open, so every opening starts from scratch. */}
        <SearchPanel {...rest} />
      </DialogContent>
    </Dialog>
  );
}

function SearchPanel({
  kinds,
  title,
  onPick,
}: Pick<ContextSearchDialogProps, "kinds" | "title" | "onPick">) {
  const t = useT();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const { items, isLoading } = useContextSearchResults(query, kinds, { recents: true });
  const pickable = items.filter((i) => i.connected !== false);
  const unconnected = items.filter((i) => i.connected === false);
  const index = Math.min(active, pickable.length - 1);
  const current = pickable[index];
  const optionId = (i: number) => `${listId}-${i}`;

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
        role="combobox"
        aria-expanded
        aria-autocomplete="list"
        aria-label={t("Search", "Suchen")}
        aria-controls={listId}
        aria-activedescendant={current ? optionId(index) : undefined}
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
      <p className="sr-only" aria-live="polite">
        {isLoading ? "" : t(`${pickable.length} results`, `${pickable.length} Treffer`)}
      </p>
      {isLoading ? (
        <div className="space-y-1" aria-busy="true" aria-label={t("Loading", "Lädt")}>
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} data-testid="search-skeleton" className="h-11 rounded-md" />
          ))}
        </div>
      ) : (
        <div className="max-h-80 space-y-2 overflow-y-auto">
          <ul id={listId} role="listbox" aria-label={t("Results", "Treffer")} className="space-y-1">
            {pickable.map((item, i) => {
              const Icon = kindIcon(item.kind);
              return (
                <li
                  key={`${item.kind}:${item.id}`}
                  id={optionId(i)}
                  role="option"
                  aria-selected={i === index}
                  onClick={() => onPick(item)}
                  onMouseMove={() => setActive(i)}
                  className={cn(
                    "flex min-h-11 min-w-0 cursor-pointer items-center gap-2 rounded-md px-2 text-sm hover:bg-muted",
                    i === index && "bg-muted",
                  )}
                >
                  <Icon className="size-4 shrink-0" aria-hidden />
                  <span className="min-w-0 flex-1 truncate">{item.label}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {kindLabel(item.kind, t)}
                  </span>
                </li>
              );
            })}
          </ul>
          {items.length === 0 && (
            <p className="px-2 py-3 text-sm text-muted-foreground">
              {t("Nothing you can open matches that.", "Nichts, was du öffnen darfst, passt dazu.")}
            </p>
          )}
          {unconnected.length > 0 && (
            <ul aria-label={t("Not connected", "Nicht verbunden")} className="space-y-1">
              {unconnected.map((item) => {
                const Icon = kindIcon(item.kind);
                return (
                  <li
                    key={`${item.kind}:${item.id}`}
                    className="flex min-h-11 min-w-0 items-center gap-2 px-2 text-sm text-muted-foreground"
                  >
                    <Icon className="size-4 shrink-0" aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{item.label}</span>
                    <span className="shrink-0 text-xs">
                      {t("Not connected", "Nicht verbunden")}
                    </span>
                    <a
                      href="/knowledge"
                      aria-label={t(`Connect ${item.label}`, `${item.label} verbinden`)}
                      className="inline-flex min-h-9 shrink-0 items-center rounded-md px-2 text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {t("Connect", "Verbinden")}
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
