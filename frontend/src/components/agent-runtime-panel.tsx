import { AlertTriangle, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { RuntimePicker, runtimeDisplayCopy } from "@/components/runtime-picker";
import { useRuntimes } from "@/lib/hooks";
import { useUpdateAgentRuntime } from "@/lib/hooks-agent-detail";
import { useT } from "@/lib/i18n";

/** Mirrors the `missing` entries in the backend's `runtime_capability_violation`
 * 422 body -- see `_violation_body` in `backend/src/oc8/api/v1/agents_write.py`.
 * That body arrives here as `Error.message` (api.ts's `toError` JSON-stringifies
 * any non-string `detail`), so it is parsed back out below rather than guessed at. */
interface RuntimeViolation {
  kind: string;
  missingCapability: string | null;
  reason: string;
}

function parseRuntimeViolations(message: string): RuntimeViolation[] | null {
  try {
    const parsed: unknown = JSON.parse(message);
    if (
      parsed !== null &&
      typeof parsed === "object" &&
      (parsed as Record<string, unknown>).error === "runtime_capability_violation" &&
      Array.isArray((parsed as Record<string, unknown>).missing)
    ) {
      return (parsed as { missing: RuntimeViolation[] }).missing;
    }
  } catch {
    // Not a JSON body -- some other failure (404 "not found", 400 "not
    // executable", network error). The caller falls back to a generic toast.
  }
  return null;
}

/** Runtime row on the agent detail page (Configuration tab): shows the
 * current runtime resolved against `GET /runtimes`, and lets an operator
 * change it via the shared `RuntimePicker` -- the same component the
 * onboarding wizard's model step uses, so the two never drift on rendering
 * rules (single option vs. several, unavailable/disabled entries). */
export function AgentRuntimePanel({
  agentId,
  runtimeRef,
  mayManage,
}: {
  agentId: string;
  runtimeRef: string | null;
  mayManage: boolean;
}) {
  const t = useT();
  const runtimesQuery = useRuntimes();
  const runtimes = runtimesQuery.data ?? [];
  const updateRuntime = useUpdateAgentRuntime(agentId);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [draft, setDraft] = useState<string | null>(runtimeRef);
  const [violations, setViolations] = useState<RuntimeViolation[] | null>(null);

  // `runtimeRef === null` means "never explicitly set" -- GET /runtimes no
  // longer has an `id: null` entry to match (both built-ins now carry a real
  // sentinel id, oc8.runtime.registry), so that case falls back to whichever
  // entry `isDefault` marks: the one `resolve_runtime`'s own agent_isolation
  // fallback actually gives this agent. Without this fallback, the majority
  // of agents (which have never touched their runtime) would show nothing
  // here at all.
  const implicitDefault = runtimes.find((r) => r.isDefault);
  const current = runtimeRef ? runtimes.find((r) => r.id === runtimeRef) : implicitDefault;
  // `runtimeRef` is set but nothing in the fetched list matches it: the
  // plugin behind it was disabled (or removed) after it was assigned here.
  // `resolve_runtime` raises on this agent's next run, so this must not
  // render as if the assignment were still fine. Gated on `isSuccess` (not
  // merely "the array has entries") so this can't fire while the first
  // fetch is still in flight, and on `runtimes.length > 0` so a genuinely
  // empty/failed response renders as the honest loaded-but-empty state
  // below rather than as "this specific runtime is gone".
  const unresolved =
    runtimeRef !== null && runtimesQuery.isSuccess && runtimes.length > 0 && !current;

  function openPicker() {
    // Pre-select the implicit default (not null) when runtimeRef was never
    // set, so the picker opens showing what the agent actually runs on
    // today instead of no selection at all.
    setDraft(runtimeRef ?? current?.id ?? null);
    setViolations(null);
    setPickerOpen(true);
  }

  function submit() {
    setViolations(null);
    updateRuntime.mutate(
      { runtimePluginId: draft },
      {
        onSuccess: () => {
          setPickerOpen(false);
          toast.success(t("Runtime updated", "Runtime aktualisiert"));
        },
        onError: (error: Error) => {
          const parsed = parseRuntimeViolations(error.message);
          if (parsed) {
            setViolations(parsed);
            return;
          }
          toast.error(t("Couldn't update runtime", "Runtime konnte nicht aktualisiert werden"), {
            description: error.message,
          });
        },
      },
    );
  }

  // The rescue action for `unresolved`: the plugin behind `runtimeRef` was
  // disabled or removed, `GET /runtimes` now returns only the built-in
  // default, so `RuntimePicker`'s one-option rule (by design) renders a
  // static row `draft` can never change -- Save would just re-send the same
  // stale id and 404 again. This bypasses the picker entirely and clears the
  // assignment directly, returning the agent to whatever the built-in
  // default resolves to.
  function resetToDefault() {
    updateRuntime.mutate(
      { runtimePluginId: null },
      {
        onSuccess: () => {
          toast.success(
            t("Runtime reset to the built-in default", "Runtime auf Standard zurückgesetzt"),
          );
        },
        onError: (error: Error) => {
          toast.error(t("Couldn't reset runtime", "Runtime konnte nicht zurückgesetzt werden"), {
            description: error.message,
          });
        },
      },
    );
  }

  return (
    <Panel className="p-5">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="micro-label">{t("execution environment", "Ausführungsumgebung")}</div>
          <h3 className="mt-0.5 font-serif text-lg">{t("Runtime", "Runtime")}</h3>
        </div>
        <button
          type="button"
          onClick={openPicker}
          disabled={!mayManage || !runtimesQuery.isSuccess || runtimes.length === 0}
          className="shrink-0 rounded-md border border-border bg-background/40 px-3 py-1.5 text-xs text-muted-foreground transition hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("Change", "Ändern")}
        </button>
      </div>

      {current &&
        (() => {
          const { label, summary } = runtimeDisplayCopy(current, t);
          return (
            <div className="mt-4">
              <div className="text-sm font-medium">{label}</div>
              <p className="mt-0.5 text-xs text-muted-foreground">{summary}</p>
              {current.capabilities.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {current.capabilities.map((cap) => (
                    <span
                      key={cap}
                      className="rounded-full border border-border bg-background/40 px-1.5 py-0.5 text-micro text-muted-foreground"
                    >
                      {cap}
                    </span>
                  ))}
                </div>
              )}
            </div>
          );
        })()}

      {unresolved && (
        <div className="mt-4 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-xs">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-warning-text" />
          <div className="flex-1">
            <p className="text-foreground/90">
              {t("This runtime is no longer available.", "Diese Runtime ist nicht mehr verfügbar.")}
            </p>
            <code className="mt-1 block font-mono text-muted-foreground">{runtimeRef}</code>
            {mayManage && (
              <button
                type="button"
                onClick={resetToDefault}
                disabled={updateRuntime.isPending}
                className="mt-2 rounded-md border border-status-warning/50 bg-background/40 px-2.5 py-1 text-xs font-medium text-foreground/90 transition hover:bg-background/70 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {updateRuntime.isPending
                  ? t("Resetting…", "Wird zurückgesetzt…")
                  : t("Use the built-in default", "Standard verwenden")}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Query still in flight: genuinely different from "loaded, but
       * empty" below -- neither can be inferred from `runtimes` alone. */}
      {runtimesQuery.isLoading && (
        <p className="mt-4 text-sm text-muted-foreground">{t("Loading…", "Wird geladen…")}</p>
      )}

      {runtimesQuery.isError && (
        <p className="mt-4 text-sm text-status-error">
          {t(
            "Couldn't load runtime information.",
            "Runtime-Informationen konnten nicht geladen werden.",
          )}
        </p>
      )}

      {runtimesQuery.isSuccess && runtimes.length === 0 && (
        <p className="mt-4 text-sm text-muted-foreground">
          {t("No runtimes are available right now.", "Derzeit sind keine Runtimes verfügbar.")}
        </p>
      )}

      {pickerOpen && (
        <div
          className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setPickerOpen(false)}
        >
          <div
            className="w-full max-w-lg overflow-hidden rounded-xl border border-border bg-panel shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <div>
                <div className="micro-label">{t("Runtime", "Runtime")}</div>
                <h2 className="font-serif text-xl">{t("Change runtime", "Runtime ändern")}</h2>
              </div>
              <button
                type="button"
                onClick={() => setPickerOpen(false)}
                className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-3 p-5">
              <RuntimePicker
                value={draft}
                onChange={setDraft}
                runtimes={runtimes}
                disabled={updateRuntime.isPending}
              />
              {violations && (
                <div
                  role="alert"
                  className="rounded-md border border-status-error/40 bg-status-error/10 p-3 text-xs text-foreground/90"
                >
                  <p className="font-medium text-status-error">
                    {t(
                      "This runtime can't be used here:",
                      "Diese Runtime kann hier nicht verwendet werden:",
                    )}
                  </p>
                  <ul className="mt-1.5 list-disc space-y-1 pl-4">
                    {violations.map((v, i) => (
                      <li key={i}>{v.reason}</li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setPickerOpen(false)}
                  className="rounded-md border border-border px-3 py-2 text-sm text-muted-foreground hover:text-foreground"
                >
                  {t("Cancel", "Abbrechen")}
                </button>
                <button
                  type="button"
                  onClick={submit}
                  disabled={updateRuntime.isPending}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
                >
                  {updateRuntime.isPending
                    ? t("Saving…", "Wird gespeichert…")
                    : t("Save", "Speichern")}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
