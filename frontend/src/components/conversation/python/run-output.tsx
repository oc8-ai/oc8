// "Run" under a Python block in an answer and in the artifact panel's Code
// tab, and what the run printed. Python runs only when the answer's previews
// may run scripts (see pythonRunMode); otherwise the reason shows instead of
// the button. Nothing is loaded before the first click: the runner is a
// dynamic import and fetches Pyodide itself.
//
// The output is untrusted text from the sandbox (checked by readOutput) and
// is rendered as text only: images as <img> of PNG data URLs, tables as text
// cells.

import { useEffect, useRef, useState } from "react";
import { Paperclip, Play, Send, Square } from "lucide-react";

import { downloadFileAttachment } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePreviewSettings } from "@/lib/preview-settings";
import { cn } from "@/lib/utils";

import type { PreviewMode } from "../artifacts/context";
import { LIMITS, resultMessage, type PythonResult } from "./output";
import { pythonRunMode, usePythonScope } from "./run-context";

const BUTTON =
  "inline-flex min-h-9 items-center gap-1 rounded-md border border-border px-2 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

type State =
  | { status: "idle" }
  | { status: "loading"; loaded: number; total: number | null }
  | { status: "running" }
  | { status: "done"; result: PythonResult }
  | { status: "stopped" }
  | { status: "failed"; error: string };

function megabytes(n: number): string {
  return (n / 1024 / 1024).toFixed(1);
}

/** A run's output: stdout, stderr (muted error style), the last value,
 *  figures and tables. */
export function RunOutput({ result }: { result: PythonResult }) {
  const t = useT();
  const empty =
    !result.stdout &&
    !result.stderr &&
    result.result === null &&
    result.images.length === 0 &&
    result.tables.length === 0;
  return (
    <div className="space-y-2" data-testid="python-output">
      {result.timedOut && (
        <p role="status" className="text-xs text-destructive">
          {t(
            `Stopped after ${LIMITS.timeoutMs / 1000} seconds.`,
            `Nach ${LIMITS.timeoutMs / 1000} Sekunden angehalten.`,
          )}
        </p>
      )}
      {result.stdout && (
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-xs text-foreground">
          {result.stdout}
        </pre>
      )}
      {result.stderr && (
        <pre
          data-testid="python-stderr"
          className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-xs text-destructive/80"
        >
          {result.stderr}
        </pre>
      )}
      {result.result !== null && (
        <pre className="max-h-80 overflow-auto whitespace-pre-wrap font-mono text-xs text-muted-foreground">
          {result.result}
        </pre>
      )}
      {result.images.map((src, i) => (
        <img
          key={i}
          src={src}
          alt={t(`Figure ${i + 1}`, `Abbildung ${i + 1}`)}
          className="max-w-full rounded-md border border-border bg-background"
        />
      ))}
      {result.tables.map((table, i) => (
        <div key={i} className="max-h-80 overflow-auto rounded-md border border-border">
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr>
                {table.columns.map((c, j) => (
                  <th key={j} className="border-b border-border px-2 py-1 text-left font-medium">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((value, j) => (
                    <td key={j} className="border-b border-border px-2 py-1 font-mono">
                      {value}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
      {empty && !result.timedOut && (
        <p className="text-xs text-muted-foreground">{t("No output.", "Keine Ausgabe.")}</p>
      )}
    </div>
  );
}

/** Why Python cannot run for this answer. */
function reasonFor(mode: PreviewMode, t: ReturnType<typeof useT>): string | null {
  switch (mode) {
    case "restricted":
      return t(
        "Python cannot run here because this answer contains restricted content.",
        "Python kann hier nicht laufen, weil diese Antwort eingeschränkte Inhalte enthält.",
      );
    case "off":
      return t(
        "Python cannot run: scripts in previews are turned off for your organization.",
        "Python kann nicht laufen: Skripte in Vorschauen sind für deine Organisation ausgeschaltet.",
      );
    case "unknown":
      return t(
        "Python cannot run: the preview setting could not be read.",
        "Python kann nicht laufen: Die Vorschau-Einstellung konnte nicht gelesen werden.",
      );
    case "unsaved":
      return t(
        "Python can run once the answer is saved.",
        "Python kann laufen, sobald die Antwort gespeichert ist.",
      );
    default:
      return null;
  }
}

/** Run, Stop, the files to mount, and the output, for `code`. A new code or
 *  mode starts over (a run in progress is stopped). */
export function PythonRun({ code, mode }: { code: string; mode: PreviewMode }) {
  return <RunBody key={`${mode}\u0000${code}`} code={code} mode={mode} />;
}

function RunBody({ code, mode }: { code: string; mode: PreviewMode }) {
  const t = useT();
  const scope = usePythonScope();
  const [state, setState] = useState<State>({ status: "idle" });
  const [picked, setPicked] = useState<string[]>([]);
  const [showFiles, setShowFiles] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  if (mode === "pending") return null;
  const reason = reasonFor(mode, t);
  if (reason) {
    return (
      <p role="note" className="px-1 text-xs text-muted-foreground">
        {reason}
      </p>
    );
  }

  const busy = state.status === "loading" || state.status === "running";
  const files = scope?.files ?? [];

  async function run() {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setState({ status: "loading", loaded: 0, total: null });
    try {
      const chosen = files.filter((f) => picked.includes(f.id));
      const [{ runPython }, mounts] = await Promise.all([
        import("./runner"),
        Promise.all(
          chosen.map(async (f) => ({
            name: f.filename,
            bytes: await (await downloadFileAttachment(f.id)).arrayBuffer(),
          })),
        ),
      ]);
      const result = await runPython(code, {
        files: mounts.filter((m) => m.bytes.byteLength <= LIMITS.fileBytes),
        signal: abort.signal,
        onProgress: ({ loaded, total }) => {
          if (!abort.signal.aborted) setState({ status: "loading", loaded, total });
        },
        onStarted: () => {
          if (!abort.signal.aborted) setState({ status: "running" });
        },
      });
      if (!abort.signal.aborted) setState({ status: "done", result });
    } catch (error) {
      if (abort.signal.aborted) return;
      setState({
        status: "failed",
        error: error instanceof Error ? error.message : String(error),
      });
    }
  }

  function stop() {
    controller.current?.abort();
    controller.current = null;
    setState({ status: "stopped" });
  }

  const message =
    state.status === "done"
      ? resultMessage(state.result, t("Result of the Python code:", "Ergebnis des Python-Codes:"))
      : "";

  return (
    <div className="space-y-2 px-1" data-testid="python-run">
      <div className="flex flex-wrap items-center gap-2">
        {busy ? (
          <button type="button" className={BUTTON} onClick={stop}>
            <Square className="size-3.5" aria-hidden />
            {t("Stop", "Stoppen")}
          </button>
        ) : (
          <button type="button" className={BUTTON} onClick={() => void run()}>
            <Play className="size-3.5" aria-hidden />
            {t("Run", "Ausführen")}
          </button>
        )}
        {files.length > 0 && (
          <button
            type="button"
            className={BUTTON}
            aria-expanded={showFiles}
            onClick={() => setShowFiles((v) => !v)}
            disabled={busy}
          >
            <Paperclip className="size-3.5" aria-hidden />
            {picked.length > 0
              ? t(`Files (${picked.length})`, `Dateien (${picked.length})`)
              : t("Files", "Dateien")}
          </button>
        )}
        {state.status === "loading" && (
          <span role="status" className="text-shimmer text-xs text-muted-foreground">
            {state.total
              ? t(
                  `Loading Python… ${megabytes(state.loaded)} of ${megabytes(state.total)} MB`,
                  `Python wird geladen … ${megabytes(state.loaded)} von ${megabytes(state.total)} MB`,
                )
              : t("Loading Python…", "Python wird geladen …")}
          </span>
        )}
        {state.status === "running" && (
          <span role="status" className="text-shimmer text-xs text-muted-foreground">
            {t("Running…", "Läuft…")}
          </span>
        )}
        {state.status === "stopped" && (
          <span role="status" className="text-xs text-muted-foreground">
            {t("Stopped.", "Angehalten.")}
          </span>
        )}
        {message && scope?.send && (
          <button
            type="button"
            className={cn(BUTTON, "ml-auto")}
            onClick={() => scope.send?.(message)}
          >
            <Send className="size-3.5" aria-hidden />
            {t("Send result to chat", "Ergebnis in den Chat senden")}
          </button>
        )}
      </div>
      {showFiles && files.length > 0 && (
        <fieldset className="space-y-1 rounded-md border border-border px-2 py-1">
          <legend className="px-1 text-xs text-muted-foreground">
            {t(
              "Chat files the code can read (read-only, in its working folder)",
              "Chat-Dateien, die der Code lesen kann (schreibgeschützt, im Arbeitsordner)",
            )}
          </legend>
          {files.map((f) => (
            <label key={f.id} className="flex min-h-9 items-center gap-2 text-xs text-foreground">
              <input
                type="checkbox"
                checked={picked.includes(f.id)}
                onChange={(e) =>
                  setPicked((prev) =>
                    e.target.checked ? [...prev, f.id] : prev.filter((id) => id !== f.id),
                  )
                }
              />
              <span className="truncate font-mono">{f.filename}</span>
            </label>
          ))}
        </fieldset>
      )}
      {state.status === "failed" && (
        <p role="alert" className="text-xs text-destructive">
          {t("Python could not run: ", "Python konnte nicht laufen: ")}
          {state.error}
        </p>
      )}
      {state.status === "done" && <RunOutput result={state.result} />}
    </div>
  );
}

/** Run under a ```python block of a saved or live answer (`messageId`). */
export function InlinePythonRun({ code, messageId }: { code: string; messageId?: string }) {
  const scope = usePythonScope();
  const settings = usePreviewSettings(true);
  const mode = pythonRunMode(settings, scope?.restrictedOf(messageId));
  return <PythonRun code={code} mode={mode} />;
}
