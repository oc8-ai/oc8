// Picks a source's scope -- the folders and files it indexes -- from the
// connector's own listing, before the source is saved. Generic: everything it
// shows comes from POST /knowledge/connectors/{typeId}/browse, nothing here
// knows which system it is listing.
//
// - Root entries come from path "/"; a folder is browsed lazily, on expand,
//   with its entry's id as the path.
// - An entry of kind "app" has no checkbox of its own: expanded, it offers an
//   "Everything" row first, stored as {id: <app id>, label, kind: "all"}. Ticking it
//   clears and disables every other tick inside that app.
// - An entry with a `note` (e.g. an app that is not installed) shows the note
//   instead of children and cannot be ticked.
import { Check, ChevronDown, ChevronRight, FileText, Folder, Layers } from "lucide-react";
import { createContext, useContext, useEffect, useRef, useState } from "react";
import { useT } from "@/lib/i18n";
import { useBrowseDraft, type BrowseEntry } from "@/lib/knowledge-connector-hooks";
import { cn } from "@/lib/utils";

export interface ScopeItem {
  id: string;
  label: string;
  kind: string;
}

/** The `kind` of the item that stands for everything inside an app. */
export const ALL_KIND = "all";

interface PickerCtx {
  typeId: string;
  config: Record<string, unknown>;
  value: ScopeItem[];
  /** App ids whose "Everything" row is ticked. */
  allApps: Set<string>;
  /** Which app each listed entry sits in, filled as folders load. */
  owners: Map<string, string>;
  toggle: (item: ScopeItem, on: boolean) => void;
  toggleAll: (appId: string, on: boolean) => void;
}

const Ctx = createContext<PickerCtx | null>(null);

/** How long the draft config must stay unchanged before it is browsed again. */
export const BROWSE_SETTLE_MS = 400;

/** `value`, once it has stopped changing for `ms` -- compared by content, so a
 *  new object with the same fields is no change. While not `live` (the picker
 *  is disabled and browses nothing) it follows `value` at once, so the first
 *  browse after enabling uses the config that enabled it. */
function useSettled<T>(value: T, ms: number, live: boolean): T {
  const key = JSON.stringify(value);
  const settled = useRef({ key, value });
  const latest = useRef(value);
  latest.current = value;
  const [, rerender] = useState(0);
  // Until the first live render (that one included), follow `value` at once.
  // Idempotent for a given render, so safe to do during it.
  const wasLive = useRef(false);
  if (!live || !wasLive.current) settled.current = { key, value };
  wasLive.current = live;
  useEffect(() => {
    if (!live || key === settled.current.key) return;
    const id = setTimeout(() => {
      settled.current = { key, value: latest.current };
      rerender((n) => n + 1);
    }, ms);
    return () => clearTimeout(id);
  }, [key, ms, live]);
  return settled.current.value;
}

function usePicker(): PickerCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("ScopePicker context missing");
  return ctx;
}

export function ScopePicker({
  typeId,
  config,
  value,
  onChange,
  disabled = false,
}: {
  typeId: string;
  /** The draft config to browse with, without the picker's own property. */
  config: Record<string, unknown>;
  value: ScopeItem[];
  onChange: (value: ScopeItem[]) => void;
  /** True until every other required property has a value. */
  disabled?: boolean;
}) {
  const t = useT();
  const owners = useRef(new Map<string, string>()).current;
  // Typing into another field changes the config on every keystroke; browse
  // only once it settles.
  const browseConfig = useSettled(config, BROWSE_SETTLE_MS, !disabled);
  const allApps = new Set(value.filter((v) => v.kind === ALL_KIND).map((v) => v.id));

  const toggle = (item: ScopeItem, on: boolean) => {
    const rest = value.filter((v) => v.id !== item.id);
    onChange(on ? [...rest, item] : rest);
  };
  const toggleAll = (appId: string, on: boolean) => {
    if (!on) {
      onChange(value.filter((v) => !(v.id === appId && v.kind === ALL_KIND)));
      return;
    }
    onChange([
      ...value.filter((v) => v.id !== appId && owners.get(v.id) !== appId),
      { id: appId, label: t("Everything", "Alles"), kind: ALL_KIND },
    ]);
  };

  if (disabled) {
    return (
      <div className="rounded-md border border-dashed border-border bg-background/20 px-3 py-2 text-[11px] text-muted-foreground">
        {t(
          "Fill in the other required fields first.",
          "Fülle zuerst die übrigen Pflichtfelder aus.",
        )}
      </div>
    );
  }

  return (
    <Ctx.Provider
      value={{ typeId, config: browseConfig, value, allApps, owners, toggle, toggleAll }}
    >
      <div className="max-h-72 overflow-y-auto rounded-md border border-border bg-background/40 p-2 text-sm text-foreground">
        <Level path="/" appId={null} depth={0} />
      </div>
    </Ctx.Provider>
  );
}

function Level({ path, appId, depth }: { path: string; appId: string | null; depth: number }) {
  const t = useT();
  const { typeId, config, owners } = usePicker();
  const { data, error, isLoading } = useBrowseDraft({ typeId, config, path });

  useEffect(() => {
    if (!appId || !data) return;
    for (const entry of data.entries) owners.set(entry.id, appId);
  }, [appId, data, owners]);

  const indent = { paddingLeft: `${depth * 16}px` };
  if (isLoading) {
    return (
      <p className="py-1 text-[11px] text-muted-foreground" style={indent}>
        {t("Loading…", "Lädt …")}
      </p>
    );
  }
  if (error) {
    return (
      <p className="py-1 text-[11px] text-destructive" style={indent}>
        {error instanceof Error
          ? error.message
          : t("This folder could not be listed.", "Dieser Ordner ließ sich nicht auflisten.")}
      </p>
    );
  }
  const entries = data?.entries ?? [];
  return (
    <ul className="space-y-0.5">
      {entries.map((entry) => (
        <Node key={entry.id} entry={entry} appId={appId} depth={depth} />
      ))}
      {entries.length === 0 && (
        <li className="py-1 text-[11px] text-muted-foreground" style={indent}>
          {t("Nothing here.", "Hier ist nichts.")}
        </li>
      )}
      {data?.truncated && (
        <li className="py-1 text-[11px] text-[color:var(--status-warning)]" style={indent}>
          {t(
            "More than 500 entries – pick a parent folder.",
            "Mehr als 500 Einträge – wähle einen übergeordneten Ordner.",
          )}
        </li>
      )}
    </ul>
  );
}

function Node({
  entry,
  appId,
  depth,
}: {
  entry: BrowseEntry;
  appId: string | null;
  depth: number;
}) {
  const t = useT();
  const { value, allApps, toggle, toggleAll } = usePicker();
  const [open, setOpen] = useState(false);
  const indent = { paddingLeft: `${depth * 16}px` };
  const isApp = entry.kind === "app";
  const Icon = isApp ? Layers : entry.isFolder ? Folder : FileText;

  if (entry.note) {
    return (
      <li className="py-1" style={indent}>
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <span className="w-4" />
          <Icon className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{entry.name}</span>
        </div>
        <p className="ml-[22px] text-[11px] text-muted-foreground">{entry.note}</p>
      </li>
    );
  }

  const coveredByAll = !!appId && allApps.has(appId);
  const checked = !coveredByAll && value.some((v) => v.id === entry.id && v.kind !== ALL_KIND);
  const expandable = isApp || entry.isFolder;
  const Chevron = open ? ChevronDown : ChevronRight;
  const allTicked = isApp && allApps.has(entry.id);

  return (
    <li>
      <div className="flex items-center gap-1.5 py-0.5" style={indent}>
        {expandable ? (
          <Chevron className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        ) : (
          <span className="w-3.5 shrink-0" />
        )}
        {!isApp && (
          <input
            type="checkbox"
            aria-label={entry.name}
            checked={checked}
            disabled={coveredByAll}
            onChange={(e) =>
              toggle(
                {
                  id: entry.id,
                  label: entry.name,
                  kind: entry.kind ?? (entry.isFolder ? "folder" : "file"),
                },
                e.target.checked,
              )
            }
            className="shrink-0"
          />
        )}
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden />
        {expandable ? (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((o) => !o)}
            className={cn("truncate text-left hover:text-primary", isApp && "font-medium")}
          >
            {entry.name}
          </button>
        ) : (
          <span className="truncate">{entry.name}</span>
        )}
        {allTicked && (
          <span className="inline-flex shrink-0 items-center gap-0.5 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] text-primary">
            <Check className="h-3 w-3" aria-hidden />
            {t("Everything: selected", "Alles: ausgewählt")}
          </span>
        )}
      </div>
      {open && (
        <>
          {isApp && (
            <div
              className="flex items-center gap-1.5 py-0.5"
              style={{ paddingLeft: `${(depth + 1) * 16}px` }}
            >
              <span className="w-3.5 shrink-0" />
              <input
                type="checkbox"
                aria-label={t("Everything", "Alles")}
                checked={allTicked}
                onChange={(e) => toggleAll(entry.id, e.target.checked)}
                className="shrink-0"
              />
              <span className="font-medium">{t("Everything", "Alles")}</span>
            </div>
          )}
          <Level path={entry.id} appId={isApp ? entry.id : appId} depth={depth + 1} />
        </>
      )}
    </li>
  );
}
