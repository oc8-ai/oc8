// frontend/src/components/conversation/data-source-browser.tsx
// The + menu's "Choose from data source": a Sheet (right on desktop, from
// the bottom under 640 px) to pick a source, walk its folders by breadcrumb
// and pick a file as a `source_file` ref ("<source>:<file>"). A source that
// can't be browsed is offered as a whole.
import { useState, useSyncExternalStore } from "react";
import { ChevronRight, FileText, Folder } from "lucide-react";

import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useBrowseSource, useContextSearchResults, type ContextItem } from "@/lib/conversation-api";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { kindIcon } from "./context-icons";

const NARROW = "(max-width: 639px)";

function subscribeNarrow(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const mql = window.matchMedia(NARROW);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

function isNarrow(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(NARROW).matches;
}

const ROW =
  "flex min-h-11 w-full min-w-0 items-center gap-2 rounded-md px-2 text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function DataSourceBrowser({
  open,
  onPick,
  onClose,
  returnFocus,
}: {
  open: boolean;
  onPick: (item: ContextItem) => void;
  onClose: () => void;
  /** Where focus goes once it closes; the trigger (a + menu item) is gone. */
  returnFocus?: () => void;
}) {
  const t = useT();
  const narrow = useSyncExternalStore(subscribeNarrow, isNarrow, () => false);
  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent
        side={narrow ? "bottom" : "right"}
        onCloseAutoFocus={(e) => {
          if (!returnFocus) return;
          e.preventDefault();
          returnFocus();
        }}
        className={cn(
          "flex flex-col gap-4 overflow-y-auto p-5",
          narrow ? "max-h-[85dvh] rounded-t-lg" : "w-full sm:max-w-md",
        )}
      >
        <div className="space-y-1 pr-8">
          <SheetTitle className="font-serif text-xl font-semibold">
            {t("Choose from a data source", "Aus einer Datenquelle wählen")}
          </SheetTitle>
          <SheetDescription className="text-xs">
            {t(
              "Pick a file to attach to your next message.",
              "Wähl eine Datei für deine nächste Nachricht.",
            )}
          </SheetDescription>
        </div>
        {/* Mounted only while open, so every opening starts at the sources. */}
        <BrowserPanel onPick={onPick} />
      </SheetContent>
    </Sheet>
  );
}

interface Crumb {
  path: string;
  name: string;
}

function BrowserPanel({ onPick }: { onPick: (item: ContextItem) => void }) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [source, setSource] = useState<ContextItem | null>(null);
  const [crumbs, setCrumbs] = useState<Crumb[]>([]);
  const { items: sources, isLoading: sourcesLoading } = useContextSearchResults(
    source ? null : query,
    ["data_source"],
    { recents: true },
  );
  const path = crumbs[crumbs.length - 1]?.path ?? "/";
  const browse = useBrowseSource(source?.id ?? null, path);
  const status = (browse.error as { status?: number } | null | undefined)?.status;
  const SourceIcon = kindIcon("data_source");

  function openSource(item: ContextItem) {
    setSource(item);
    setCrumbs([{ path: "/", name: item.label }]);
  }

  if (!source) {
    return (
      <div className="min-w-0 space-y-3">
        <input
          type="search"
          aria-label={t("Search sources", "Quellen suchen")}
          value={query}
          maxLength={100}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("Search sources …", "Quellen suchen …")}
          className="h-9 w-full rounded-md border border-border bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        {sourcesLoading ? (
          <div className="space-y-2" aria-busy="true" aria-label={t("Loading", "Lädt")}>
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} className="h-11 rounded-md" />
            ))}
          </div>
        ) : (
          <ul className="space-y-1">
            {sources.length === 0 && (
              <li className="px-2 py-3 text-sm text-muted-foreground">
                {t(
                  "No data source you can open matches that.",
                  "Keine Datenquelle, die du öffnen darfst, passt dazu.",
                )}
              </li>
            )}
            {sources.map((s) => (
              <li key={s.id} className="flex min-w-0 items-center gap-2">
                {s.connected === false ? (
                  <>
                    <span className={cn(ROW, "flex-1 text-muted-foreground hover:bg-transparent")}>
                      <SourceIcon className="size-4 shrink-0" aria-hidden />
                      <span className="min-w-0 flex-1 truncate">{s.label}</span>
                      <span className="shrink-0 text-xs">
                        {t("Not connected", "Nicht verbunden")}
                      </span>
                    </span>
                    <a
                      href="/knowledge"
                      aria-label={t(`Connect ${s.label}`, `${s.label} verbinden`)}
                      className="inline-flex min-h-9 shrink-0 items-center rounded-md px-2 text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {t("Connect", "Verbinden")}
                    </a>
                  </>
                ) : (
                  <button type="button" onClick={() => openSource(s)} className={ROW}>
                    <SourceIcon className="size-4 shrink-0" aria-hidden />
                    <span className="min-w-0 flex-1 truncate">{s.label}</span>
                    <ChevronRight className="size-4 shrink-0" aria-hidden />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-3">
      <nav aria-label={t("Folders", "Ordner")}>
        <ol className="flex min-w-0 flex-wrap items-center gap-1 text-sm">
          <li>
            <button
              type="button"
              onClick={() => {
                setSource(null);
                setCrumbs([]);
              }}
              className="inline-flex min-h-9 items-center rounded-md px-2 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {t("All sources", "Alle Quellen")}
            </button>
          </li>
          {crumbs.map((c, i) => {
            const last = i === crumbs.length - 1;
            return (
              <li key={`${i}:${c.path}`} className="flex min-w-0 items-center gap-1">
                <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                {last ? (
                  <span aria-current="page" className="min-w-0 truncate px-2 font-medium">
                    {c.name}
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => setCrumbs(crumbs.slice(0, i + 1))}
                    className="inline-flex min-h-9 min-w-0 items-center rounded-md px-2 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <span className="truncate">{c.name}</span>
                  </button>
                )}
              </li>
            );
          })}
        </ol>
      </nav>
      {status === 409 ? (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {t(
              "This source can't be browsed — pin it as a whole.",
              "Diese Quelle lässt sich nicht durchsuchen — hefte sie als Ganzes an.",
            )}
          </p>
          <button
            type="button"
            onClick={() => onPick(source)}
            className="inline-flex h-9 items-center rounded-md border border-border bg-card px-3 text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {t("Pin whole source", "Ganze Quelle anheften")}
          </button>
        </div>
      ) : browse.error ? (
        <p role="alert" className="text-sm text-status-error">
          {browseError(status, browse.error, t)}
        </p>
      ) : browse.isLoading || !browse.data ? (
        <div className="space-y-2" aria-busy="true" aria-label={t("Loading", "Lädt")}>
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-9 rounded-md" />
          ))}
        </div>
      ) : (
        <>
          <ul className="space-y-1">
            {browse.data.entries.length === 0 && (
              <li className="px-2 py-3 text-sm text-muted-foreground">
                {t("This folder is empty.", "Dieser Ordner ist leer.")}
              </li>
            )}
            {browse.data.entries.map((entry) => (
              <li key={entry.id}>
                <button
                  type="button"
                  onClick={() =>
                    entry.isFolder
                      ? setCrumbs([...crumbs, { path: entry.id, name: entry.name }])
                      : onPick({
                          kind: "source_file",
                          id: `${source.id}:${entry.id}`,
                          label: entry.name,
                        })
                  }
                  className={ROW}
                >
                  {entry.isFolder ? (
                    <Folder className="size-4 shrink-0" aria-hidden />
                  ) : (
                    <FileText className="size-4 shrink-0" aria-hidden />
                  )}
                  <span className="min-w-0 flex-1 truncate">{entry.name}</span>
                  {entry.isFolder && (
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                  )}
                </button>
              </li>
            ))}
          </ul>
          {browse.data.truncated && (
            <p className="px-2 text-xs text-muted-foreground">
              {t(
                "More entries exist – search more specifically",
                "Weitere Einträge vorhanden – such gezielter",
              )}
            </p>
          )}
        </>
      )}
    </div>
  );
}

function browseError(
  status: number | undefined,
  error: unknown,
  t: (en: string, de?: string) => string,
): string {
  switch (status) {
    case 403:
      return t(
        "You need access to knowledge to browse sources.",
        "Du brauchst Zugriff auf Wissen, um Quellen zu durchsuchen.",
      );
    case 404:
      return t(
        "This source is no longer available to you. Go back and pick another.",
        "Diese Quelle ist für dich nicht mehr verfügbar. Geh zurück und wähl eine andere.",
      );
    case 422:
      return t(
        "This folder can't be opened. Go back and pick another.",
        "Dieser Ordner lässt sich nicht öffnen. Geh zurück und wähl einen anderen.",
      );
    case 502:
      // Already scrubbed by the server: it names the failure, never the
      // connector's own words.
      return error instanceof Error && error.message
        ? error.message
        : t(
            "The source did not answer. Try again.",
            "Die Quelle hat nicht geantwortet. Versuch es noch einmal.",
          );
    default:
      return t(
        "The folder could not be loaded. Try again.",
        "Der Ordner konnte nicht geladen werden. Versuch es noch einmal.",
      );
  }
}
