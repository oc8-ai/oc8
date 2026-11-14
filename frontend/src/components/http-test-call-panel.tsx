// One real call of one endpoint of an HTTP API connection, with the answer
// (or why there was none) in words a person can act on. Used by the custom
// server wizard and by the endpoints editor.
import { useState } from "react";
import { FlaskConical } from "lucide-react";
import type { HttpTestCallResult } from "@/lib/http-tools";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export interface TestableTool {
  name: string;
  method: string;
  paramSchema: Record<string, unknown>;
}

function needsArguments(tool: TestableTool): boolean {
  const required = tool.paramSchema.required;
  return Array.isArray(required) && required.length > 0;
}

/** A GET endpoint that needs no arguments is the safest first call. */
export function defaultTestTool(tools: readonly TestableTool[]): string {
  const read = tools.filter((t) => t.method.toUpperCase() === "GET");
  return (read.find((t) => !needsArguments(t)) ?? read[0] ?? tools[0])?.name ?? "";
}

export function HttpTestCallPanel({
  tools,
  run,
}: {
  tools: readonly TestableTool[];
  run: (tool: string, args: Record<string, unknown>) => Promise<HttpTestCallResult>;
}) {
  const t = useT();
  const [tool, setTool] = useState(() => defaultTestTool(tools));
  const [argsText, setArgsText] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<HttpTestCallResult | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  const MESSAGES: Record<NonNullable<HttpTestCallResult["errorClass"]>, string> = {
    auth: t(
      "The API refused the login. Check the login.",
      "Die API hat die Anmeldung abgelehnt. Prüfe die Anmeldung.",
    ),
    token: t(
      "No access token: the token URL refused or did not answer. The API itself was not called.",
      "Kein Zugriffstoken: Die Token-URL hat abgelehnt oder nicht geantwortet. Die API selbst wurde nicht aufgerufen.",
    ),
    blocked: t("This address is not allowed.", "Diese Adresse ist nicht erlaubt."),
    unreachable: t("The API is not reachable.", "Die API ist nicht erreichbar."),
    http: t("The API answered with an error.", "Die API hat mit einem Fehler geantwortet."),
  };

  // auth and http with no status: the request never got an answer (refused
  // before sending, a redirect loop, a body that cannot be decoded), so the
  // API neither "refused the login" nor "answered with an error".
  const notSent = result !== null && !result.ok && result.status === null;
  const neutral =
    notSent && (result.errorClass === "auth" || result.errorClass === "http" || !result.errorClass);
  const selected = tools.find((x) => x.name === tool);
  const changesData =
    selected !== undefined && !["GET", "HEAD"].includes(selected.method.toUpperCase());

  async function onRun() {
    let args: unknown;
    try {
      args = JSON.parse(argsText || "{}");
    } catch {
      args = null;
    }
    if (args === null || typeof args !== "object" || Array.isArray(args)) {
      setProblem(
        t("The arguments must be a JSON object.", "Die Argumente müssen ein JSON-Objekt sein."),
      );
      return;
    }
    setProblem(null);
    setResult(null);
    setBusy(true);
    try {
      setResult(await run(tool, args as Record<string, unknown>));
    } catch (err) {
      setProblem(
        err instanceof Error
          ? err.message
          : t("The test call failed.", "Der Testaufruf ist fehlgeschlagen."),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        {t(
          "This sends one real request to the API with the login you entered.",
          "Das schickt eine echte Anfrage mit der eingegebenen Anmeldung an die API.",
        )}
      </p>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <label className="block text-xs text-muted-foreground">
          {t("Endpoint to test", "Zu testender Endpunkt")}
          <select
            aria-label={t("Endpoint to test", "Zu testender Endpunkt")}
            value={tool}
            onChange={(e) => setTool(e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm text-foreground"
          >
            {tools.map((x) => (
              <option key={x.name} value={x.name}>
                {x.method.toUpperCase()} · {x.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-xs text-muted-foreground">
          {t("Arguments (JSON)", "Argumente (JSON)")}
          <textarea
            aria-label={t("Arguments (JSON)", "Argumente (JSON)")}
            value={argsText}
            onChange={(e) => setArgsText(e.target.value)}
            rows={3}
            className="mt-1 w-full rounded-md border border-border bg-background/40 px-3 py-2 font-mono text-xs text-foreground"
          />
        </label>
      </div>
      {tools.length === 0 && (
        <p className="text-xs text-muted-foreground">
          {t("Give each endpoint a name first.", "Gib zuerst jedem Endpunkt einen Namen.")}
        </p>
      )}
      {changesData && (
        <p className="text-xs text-[color:var(--status-warning)]">
          {t(
            "This call can change data in the API.",
            "Dieser Aufruf kann Daten in der API ändern.",
          )}
        </p>
      )}
      <button
        type="button"
        onClick={() => void onRun()}
        disabled={busy || !tool}
        className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/40 px-3 py-2 text-sm font-medium transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <FlaskConical className="h-4 w-4" />
        {busy ? t("Calling…", "Wird aufgerufen…") : t("Run test call", "Testaufruf starten")}
      </button>
      {problem && <p className="text-xs text-[color:var(--status-error)]">{problem}</p>}
      {result && (
        <div className="space-y-2 rounded-md border border-border bg-background/20 p-3">
          <p
            className={cn(
              "text-xs font-medium",
              result.ok ? "text-[color:var(--status-running)]" : "text-[color:var(--status-error)]",
            )}
          >
            {result.ok
              ? t("The call worked.", "Der Aufruf hat funktioniert.")
              : neutral
                ? t("The call could not be made.", "Der Aufruf war nicht möglich.")
                : MESSAGES[result.errorClass ?? "http"]}
          </p>
          {neutral && result.message && (
            <p className="text-xs text-muted-foreground">{result.message}</p>
          )}
          {result.errorClass !== "blocked" && (
            <p className="text-[11px] text-muted-foreground">
              {result.status !== null && (
                <>
                  {t("Status", "Status")} {result.status} ·{" "}
                </>
              )}
              {result.durationMs} ms
            </p>
          )}
          {result.excerpt && (
            <pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded bg-background/40 p-2 font-mono text-[11px]">
              {result.excerpt}
            </pre>
          )}
          {result.truncated && (
            <p className="text-[11px] text-muted-foreground">
              {t(
                "Only the beginning of the answer is shown.",
                "Es wird nur der Anfang der Antwort gezeigt.",
              )}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
