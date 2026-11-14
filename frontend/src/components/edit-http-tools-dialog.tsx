// "Edit endpoints" on an HTTP API connection: the custom server source that
// can edit it renders its own editor, the result replaces the connection's
// tools (PATCH /mcp/connections/{id}/http-tools), and one test call confirms it.
import { useState } from "react";
import { Check, X } from "lucide-react";
import { HttpTestCallPanel } from "@/components/http-test-call-panel";
import type { CustomServerSource } from "@/edition/contracts";
import {
  httpToolProblemText,
  postHttpTestCall,
  toolFields,
  useUpdateHttpTools,
  validateHttpTools,
  type CustomServerDraft,
  type HttpToolsSnapshot,
} from "@/lib/http-tools";
import { useT } from "@/lib/i18n";

export function EditHttpToolsDialog({
  source,
  snapshot,
  onClose,
}: {
  source: CustomServerSource;
  snapshot: HttpToolsSnapshot;
  onClose: () => void;
}) {
  const t = useT();
  const update = useUpdateHttpTools();
  const [saved, setSaved] = useState<HttpToolsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const Source = source.component;

  async function onComplete(draft: CustomServerDraft) {
    setError(null);
    if (draft.httpTools.length === 0) {
      setError(t("Choose at least one endpoint.", "Wähle mindestens einen Endpunkt."));
      return;
    }
    // The same rules the server applies, so a refusal names the endpoint.
    const problems = validateHttpTools(draft.httpTools.map(toolFields));
    const first = problems.findIndex((problem) => problem !== null);
    if (first >= 0) {
      setError(
        `${draft.httpTools[first].name || "?"}: ${httpToolProblemText(problems[first]!, t)}`,
      );
      return;
    }
    try {
      setSaved(
        await update.mutateAsync({
          connectionId: snapshot.connectionId,
          httpTools: draft.httpTools,
          scopes: draft.scopes,
          openapi: draft.openapi,
        }),
      );
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : t("Could not save the endpoints.", "Die Endpunkte konnten nicht gespeichert werden."),
      );
    }
  }

  return (
    <div
      role="dialog"
      aria-label={t("Edit endpoints", "Endpunkte bearbeiten")}
      className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-md"
    >
      <div className="w-full max-w-6xl overflow-hidden rounded-2xl border border-border bg-panel shadow-2xl">
        <header className="flex items-center justify-between border-b border-border px-6 py-4">
          <div>
            <h3 className="font-serif text-xl leading-none">
              {t("Edit endpoints", "Endpunkte bearbeiten")}
            </h3>
            <p className="mt-1 text-xs text-muted-foreground">{snapshot.name}</p>
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
        <div className="max-h-[78vh] space-y-4 overflow-y-auto px-6 py-6">
          {saved === null ? (
            <Source
              mode="edit"
              initialDraft={null}
              existing={snapshot}
              onComplete={(draft) => void onComplete(draft)}
              onCancel={onClose}
            />
          ) : (
            <>
              <p className="text-sm text-[color:var(--status-running)]">
                {t("The endpoints are saved.", "Die Endpunkte sind gespeichert.")}
              </p>
              <HttpTestCallPanel
                tools={saved.httpTools.map((tool) => ({
                  name: tool.name,
                  method: tool.method,
                  paramSchema: tool.paramSchema,
                }))}
                run={(tool, args) => postHttpTestCall(snapshot.connectionId, tool, args)}
              />
              <div className="flex justify-end">
                <button
                  type="button"
                  onClick={onClose}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
                >
                  <Check className="h-4 w-4" /> {t("Done", "Fertig")}
                </button>
              </div>
            </>
          )}
          {error && (
            <p role="alert" className="text-xs text-[color:var(--status-error)]">
              {error}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
