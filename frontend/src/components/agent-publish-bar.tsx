// The bar above the agent detail tabs when the working copy has drifted from the
// version that actually runs (agent versioning design §5).
//
// It renders NOTHING when clean. That is the important half: an always-present
// "Publish" affordance on a page where editing is the normal activity trains
// people to click it without reading, and the whole point of a publish step is
// that it is a deliberate act with a gate behind it.
//
// `dirty` comes from the server, never from comparing local state: the backend
// compares payload hashes and refuses a no-op publish with a 409, so a
// client-side idea of "dirty" would eventually offer a button the API rejects.

import { useState } from "react";
import { AlertCircle, GitCompare, Loader2, Rocket, X } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";
import {
  useAgentDraftStatus,
  useAgentVersionDiff,
  usePublishAgentVersion,
  type AgentVersionFieldDiff,
} from "@/lib/agent-versions";

/** A JSON value rendered for a diff cell. Objects are stringified rather than
 *  expanded: a `narrowing.odoo` entry is a small flat object and one line of
 *  JSON is more readable in a two-column diff than a nested tree. */
export function DiffValue({ value }: { value: unknown }) {
  const t = useT();
  if (value === null || value === undefined) {
    return <span className="italic text-muted-foreground">{t("not set", "nicht gesetzt")}</span>;
  }
  if (typeof value === "object") {
    return <code className="break-all font-mono text-xs">{JSON.stringify(value)}</code>;
  }
  return <span className="break-words">{String(value)}</span>;
}

export function DiffTable({ entries }: { entries: AgentVersionFieldDiff[] }) {
  const t = useT();
  if (entries.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        {t("No differences.", "Keine Unterschiede.")}
      </p>
    );
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-border">
          <th className="py-2 text-left font-medium text-muted-foreground">{t("Field", "Feld")}</th>
          <th className="py-2 text-left font-medium text-muted-foreground">
            {t("Running now", "Läuft aktuell")}
          </th>
          <th className="py-2 text-left font-medium text-muted-foreground">
            {t("After publishing", "Nach Veröffentlichung")}
          </th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr key={entry.field} className="border-b border-border/50 align-top">
            <td className="py-2 pr-3 font-mono text-xs">{entry.field}</td>
            <td className="py-2 pr-3 text-muted-foreground">
              <DiffValue value={entry.before} />
            </td>
            <td className="py-2">
              <DiffValue value={entry.after} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** `api.ts`'s `ApiError` carries only `.status` and a `.message` that is
 *  `JSON.stringify(detail)` whenever the backend's `detail` field is an
 *  object rather than a plain string (see `toError` in `lib/api.ts`) -- there
 *  is no separate `.body` property. All three refusals this reads
 *  (`stale_version`, `no_changes_to_publish`, `publish_hook_rejected`) send
 *  an object `detail`, so parsing `.message` back into JSON recovers it. */
function parseErrorDetail(err: unknown): Record<string, unknown> | null {
  if (!(err instanceof Error)) return null;
  try {
    const parsed: unknown = JSON.parse(err.message);
    return parsed !== null && typeof parsed === "object"
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** Names what a rollback's `version_references_missing` (422) found gone,
 *  from the backend's `missing` dict (`agents/versioning.py`'s
 *  `missing_references`, keyed by payload field -- NOT camelCased, since an
 *  `HTTPException` detail dict is plain JSON, not run through `CamelModel`).
 *  Falls back to "something" rather than silently describing nothing when the
 *  shape doesn't match what's expected -- still readable, just less precise. */
function describeMissingReferences(
  missing: unknown,
  t: (en: string, de: string) => string,
): string {
  const rec =
    missing !== null && typeof missing === "object" ? (missing as Record<string, unknown>) : {};
  const count = (key: string) => (Array.isArray(rec[key]) ? (rec[key] as unknown[]).length : 0);
  const kb = count("knowledge_grants");
  const sv = count("skill_assignments");
  const mc = count("model_config_id");
  const parts: string[] = [];
  if (kb > 0) {
    parts.push(
      t(
        `${kb} knowledge base${kb === 1 ? "" : "s"}`,
        `${kb} Wissensdatenbank${kb === 1 ? "" : "en"}`,
      ),
    );
  }
  if (sv > 0) {
    parts.push(
      t(`${sv} skill version${sv === 1 ? "" : "s"}`, `${sv} Skill-Version${sv === 1 ? "" : "en"}`),
    );
  }
  if (mc > 0) {
    parts.push(t("its model configuration", "seine Modellkonfiguration"));
  }
  return parts.length > 0 ? parts.join(", ") : t("something", "etwas");
}

/** Turns the backend's structured 409/422 bodies into a sentence that says what
 *  to DO. A generic "couldn't publish"/"couldn't roll back" is the failure this
 *  exists to avoid: the refusals have different remedies (refetch, change
 *  something, talk to whoever owns the gate, or pick an older version) and only
 *  one of them is retrying. Shared by publish AND rollback -- a rollback IS a
 *  publish on the backend (spec §2.7) and hits the same error vocabulary, plus
 *  two refusals ONLY rollback can raise (a stale target's dangling references,
 *  or a restored narrowing the department frame no longer permits). */
export function describePublishError(err: unknown, t: (en: string, de: string) => string): string {
  const detail = parseErrorDetail(err);
  const code = typeof detail?.error === "string" ? detail.error : "";
  if (code === "stale_version") {
    const current = detail?.current ?? "?";
    return t(
      `Somebody else published v${current} while you were editing. Reload before publishing.`,
      `Jemand anderes hat v${current} veröffentlicht, während Sie bearbeitet haben. Bitte neu laden.`,
    );
  }
  if (code === "no_changes_to_publish") {
    return t(
      "Nothing has changed since the running version.",
      "Seit der laufenden Version hat sich nichts geändert.",
    );
  }
  if (code === "publish_hook_rejected") {
    const reason = typeof detail?.reason === "string" ? detail.reason : "";
    return t(
      `A policy check refused this configuration: ${reason}`,
      `Eine Richtlinienprüfung hat diese Konfiguration abgelehnt: ${reason}`,
    );
  }
  if (code === "version_references_missing") {
    const what = describeMissingReferences(detail?.missing, t);
    return t(
      `This version points at ${what} that no longer exist. Pick a different version, or recreate what was deleted first.`,
      `Diese Version verweist auf ${what}, die nicht mehr existieren. Wählen Sie eine andere Version, oder stellen Sie das Gelöschte zuerst wieder her.`,
    );
  }
  if (code === "narrowing_exceeds_frame") {
    const violations = Array.isArray(detail?.violations)
      ? (detail.violations as Array<{ tool_key?: unknown; reason?: unknown }>)
      : [];
    const first = violations[0];
    const firstText =
      first && typeof first.tool_key === "string" && typeof first.reason === "string"
        ? `${first.tool_key} (${first.reason})`
        : "";
    const more = violations.length > 1 ? ` +${violations.length - 1}` : "";
    return t(
      `This version's access no longer fits the department's current permissions${firstText ? `: ${firstText}${more}` : ""}.`,
      `Der Zugriff dieser Version passt nicht mehr zu den aktuellen Berechtigungen der Abteilung${firstText ? `: ${firstText}${more}` : ""}.`,
    );
  }
  return err instanceof Error ? err.message : String(err);
}

export function AgentPublishBar({ agentId, mayManage }: { agentId: string; mayManage: boolean }) {
  const t = useT();
  const may = useMay();
  const { data: status } = useAgentDraftStatus(agentId);
  const publish = usePublishAgentVersion(agentId);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [note, setNote] = useState("");

  const currentVersionNo = status?.currentVersionNo ?? null;
  // Only fetched once Review is open: the bar is on every agent page load and a
  // diff nobody asked for is a query per page view for a dialog most people
  // never open.
  const diff = useAgentVersionDiff(agentId, reviewOpen ? currentVersionNo : null);

  // BOTH gates, matching the backend: `agent_version:publish` is tenant-wide
  // (`require_permission`) and `mayManage` is the department narrow
  // (`authorize_agent_write`). A button that is live and then 403s is worse
  // than one that is absent.
  const mayPublish = mayManage && may("agent_version:publish");

  if (!status?.dirty) return null;

  const count = status.changedFields.length;

  function submit() {
    publish.mutate(
      {
        note: note.trim() || undefined,
        expectedCurrentVersionNo: currentVersionNo,
      },
      {
        onSuccess: (version) => {
          setReviewOpen(false);
          setNote("");
          toast.success(
            t(`Published v${version.versionNo}`, `v${version.versionNo} veröffentlicht`),
            {
              description: t(
                "New runs of this agent will use this configuration. Runs already in flight keep the one they started on.",
                "Neue Läufe dieses Agenten nutzen diese Konfiguration. Laufende Läufe behalten ihre eigene.",
              ),
            },
          );
        },
        onError: (err) =>
          toast.error(t("Couldn't publish", "Veröffentlichen fehlgeschlagen"), {
            description: describePublishError(err, t),
          }),
      },
    );
  }

  return (
    <>
      <Panel className="flex flex-wrap items-center justify-between gap-3 border-[color:var(--status-warning)]/40 bg-[color:var(--status-warning)]/10 p-4">
        <div className="flex min-w-0 items-center gap-2 text-sm">
          <AlertCircle className="h-4 w-4 shrink-0 text-[color:var(--status-warning)]" />
          <span>
            {count === 1
              ? t("1 unpublished change", "1 unveröffentlichte Änderung")
              : t(`${count} unpublished changes`, `${count} unveröffentlichte Änderungen`)}
            {currentVersionNo !== null && (
              <span className="ml-2 text-muted-foreground">
                {t(`running: v${currentVersionNo}`, `läuft: v${currentVersionNo}`)}
              </span>
            )}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={() => setReviewOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-accent"
          >
            <GitCompare className="h-3.5 w-3.5" /> {t("Review", "Prüfen")}
          </button>
          {mayPublish && (
            <button
              type="button"
              onClick={submit}
              disabled={publish.isPending}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:brightness-110 disabled:opacity-50"
            >
              {publish.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Rocket className="h-3.5 w-3.5" />
              )}
              {t("Publish", "Veröffentlichen")}
            </button>
          )}
        </div>
      </Panel>

      {reviewOpen && (
        <div
          className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setReviewOpen(false)}
        >
          <div
            className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded-xl border border-border bg-panel shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <div>
                <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
                  {t("Unpublished changes", "Unveröffentlichte Änderungen")}
                </div>
                <h2 className="font-serif text-xl">
                  {currentVersionNo !== null
                    ? t(
                        `v${currentVersionNo} → working copy`,
                        `v${currentVersionNo} → Arbeitsstand`,
                      )
                    : t("Never published", "Noch nie veröffentlicht")}
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setReviewOpen(false)}
                aria-label={t("Close", "Schließen")}
                className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="p-5">
              {diff.isPending ? (
                <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  {t("Loading…", "Wird geladen…")}
                </div>
              ) : (
                <DiffTable entries={diff.data?.entries ?? []} />
              )}
              {mayPublish && (
                <div className="mt-5 border-t border-border pt-4">
                  <label className="block text-xs uppercase tracking-wider text-muted-foreground">
                    {t("Note (optional)", "Notiz (optional)")}
                  </label>
                  <input
                    type="text"
                    value={note}
                    maxLength={500}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder={t(
                      "Why this change, for whoever reads the history",
                      "Warum diese Änderung – für wen später den Verlauf liest",
                    )}
                    className="mt-2 w-full rounded-md border border-border bg-background/40 px-3 py-2 text-sm outline-none focus:border-primary/50"
                  />
                  <div className="mt-4 flex justify-end">
                    <button
                      type="button"
                      onClick={submit}
                      disabled={publish.isPending}
                      className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 disabled:opacity-50"
                    >
                      <Rocket className="h-4 w-4" /> {t("Publish", "Veröffentlichen")}
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
