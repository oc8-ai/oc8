// "Classify tools" on a connection whose tools come from its own server: tick
// the tools that only read (PUT /mcp/connections/{id}/tool-scopes). Every
// other tool, and any the server offers later, counts as modifying -- so a
// read-only guardrail still offers the reading tools instead of none at all.
import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { useT } from "@/lib/i18n";
import { useSaveToolScopes, useToolScopes } from "@/lib/tool-scopes";

export function ToolScopesDialog({
  connectionId,
  onClose,
}: {
  connectionId: string;
  onClose: () => void;
}) {
  const t = useT();
  const { data: snapshot, isLoading } = useToolScopes(connectionId);
  const save = useSaveToolScopes(connectionId);
  const [readOnly, setReadOnly] = useState<Set<string> | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (snapshot && readOnly === null) setReadOnly(new Set(snapshot.read));
  }, [snapshot, readOnly]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !event.defaultPrevented && !save.isPending) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, save.isPending]);

  function toggle(tool: string) {
    setSaved(false);
    setReadOnly((current) => {
      const next = new Set(current ?? []);
      if (next.has(tool)) next.delete(tool);
      else next.add(tool);
      return next;
    });
  }

  function onSave() {
    if (!snapshot || save.isPending) return;
    setError(null);
    const read = snapshot.tools.filter((tool) => readOnly?.has(tool));
    const modify = snapshot.tools.filter((tool) => !readOnly?.has(tool));
    save.mutate(
      { read, modify },
      {
        onSuccess: () => setSaved(true),
        onError: (err: unknown) =>
          setError(
            err instanceof Error
              ? err.message
              : t("Could not save.", "Konnte nicht gespeichert werden."),
          ),
      },
    );
  }

  const tools = snapshot?.tools ?? [];

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("Classify tools", "Werkzeuge einstufen")}
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-md"
    >
      <div className="w-full max-w-lg overflow-hidden rounded-2xl border border-border bg-panel shadow-2xl">
        <header className="flex items-center justify-between border-b border-border px-6 py-4">
          <div>
            <h3 className="font-serif text-xl leading-none">
              {t("Classify tools", "Werkzeuge einstufen")}
            </h3>
            {snapshot && <p className="mt-1 text-xs text-muted-foreground">{snapshot.name}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("Close", "Schließen")}
            className="grid h-8 w-8 place-items-center rounded-md text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </header>
        <div className="max-h-[70vh] space-y-4 overflow-y-auto px-6 py-5 text-sm">
          {isLoading && <p className="text-muted-foreground">{t("Loading…", "Lädt…")}</p>}
          {snapshot && tools.length === 0 && (
            <p className="text-muted-foreground">
              {t(
                "The server has not listed any tools yet. Test the connection first, then classify its tools.",
                "Der Server hat noch keine Werkzeuge gemeldet. Teste zuerst die Verbindung, dann kannst du die Werkzeuge einstufen.",
              )}
            </p>
          )}
          {tools.length > 0 && (
            <>
              <p className="text-xs text-muted-foreground">
                {t(
                  "Tick the tools that only read. Every other tool, and any the server adds later, counts as changing data — a read-only guardrail does not offer it.",
                  "Hake die Werkzeuge an, die nur lesen. Jedes andere Werkzeug, auch eines, das der Server später dazubekommt, gilt als ändernd – ein Nur-Lesen-Guardrail bietet es nicht an.",
                )}
              </p>
              <ul className="divide-y divide-border rounded-lg border border-border">
                {tools.map((tool) => {
                  const id = `tool-scope-${tool}`;
                  const read = readOnly?.has(tool) ?? false;
                  return (
                    <li key={tool} className="flex items-center justify-between gap-3 px-3 py-2">
                      <label htmlFor={id} className="flex items-center gap-2 font-mono text-xs">
                        <input
                          id={id}
                          type="checkbox"
                          checked={read}
                          onChange={() => toggle(tool)}
                          aria-label={tool}
                        />
                        {tool}
                      </label>
                      <span className="text-[11px] text-muted-foreground">
                        {read ? t("reads", "liest") : t("changes", "ändert")}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          {error && (
            <p
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive"
            >
              {error}
            </p>
          )}
          {saved && (
            <p className="text-xs text-[color:var(--status-running)]">
              {t(
                "Saved. It applies from the next run.",
                "Gespeichert. Das gilt ab dem nächsten Run.",
              )}
            </p>
          )}
        </div>
        <footer className="flex justify-end gap-2 border-t border-border px-6 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t("Close", "Schließen")}
          </button>
          {tools.length > 0 && (
            <button
              type="button"
              onClick={onSave}
              disabled={save.isPending}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
            >
              {t("Save", "Speichern")}
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}
