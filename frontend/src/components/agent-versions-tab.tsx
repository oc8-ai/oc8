// The Versions tab on the agent detail page (agent versioning design §5).
//
// A list, and two dialogs that are the same dialog with a different button: Diff
// shows what changed between a version and the one running now, and Roll back
// shows exactly the same comparison plus a confirm. That is deliberate rather
// than lazy -- "roll back to v1" and "what does v1 differ from now in" are the
// same question, and a confirm dialog that shows something other than what the
// Diff button shows would be teaching people that the two disagree.

import { useState } from "react";
import { GitCompare, History, Loader2, RotateCcw, X } from "lucide-react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { DiffTable, describePublishError } from "@/components/agent-publish-bar";
import { useMay } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";
import {
  useAgentVersionDiff,
  useAgentVersions,
  useRollbackAgentVersion,
} from "@/lib/agent-versions";
import { SkeletonRows } from "@/components/skeleton-rows";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

type Intent = "diff" | "rollback";

export function AgentVersionsTab({ agentId, mayManage }: { agentId: string; mayManage: boolean }) {
  const t = useT();
  const may = useMay();
  const { data, isPending } = useAgentVersions(agentId);
  const rollback = useRollbackAgentVersion(agentId);
  const [open, setOpen] = useState<{ versionNo: number; intent: Intent } | null>(null);

  const versions = data?.items ?? [];
  const currentVersionNo = versions.find((v) => v.isCurrent)?.versionNo ?? null;
  // BOTH gates, matching the backend's `require_permission` +
  // `authorize_agent_write` pair. A rollback IS a publish, so it is the same
  // permission -- spec §6.
  const mayPublish = mayManage && may("agent_version:publish");

  // `from` is the version being inspected and `to` is the one running now, so
  // the table reads "this is what changes if you do it". Only fetched while a
  // dialog is open.
  const diff = useAgentVersionDiff(agentId, open ? open.versionNo : null, currentVersionNo);

  function confirmRollback(versionNo: number) {
    rollback.mutate(versionNo, {
      onSuccess: (version) => {
        setOpen(null);
        toast.success(
          t(
            `Rolled back to v${versionNo}, published as v${version.versionNo}`,
            `Auf v${versionNo} zurückgesetzt, als v${version.versionNo} veröffentlicht`,
          ),
          {
            description: t(
              "A rollback publishes a new version rather than reactivating the old one, so the history stays in order.",
              "Ein Rollback veröffentlicht eine neue Version statt die alte wieder zu aktivieren – so bleibt der Verlauf in Reihenfolge.",
            ),
          },
        );
      },
      onError: (err) =>
        toast.error(t("Couldn't roll back", "Zurücksetzen fehlgeschlagen"), {
          description: describePublishError(err, t),
        }),
    });
  }

  if (isPending) {
    return (
      <Panel className="p-5">
        <SkeletonRows label={t("Loading…", "Wird geladen…")} rowClassName="h-12" />
      </Panel>
    );
  }

  if (versions.length === 0) {
    return (
      <Panel className="p-10 text-center">
        <History className="mx-auto mb-3 h-6 w-6 text-muted-foreground/60" />
        <p className="text-sm text-muted-foreground">
          {t(
            "This agent has no published versions yet.",
            "Dieser Agent hat noch keine veröffentlichten Versionen.",
          )}
        </p>
      </Panel>
    );
  }

  return (
    <>
      <Panel className="p-5">
        <div className="mb-3 text-xs uppercase tracking-wider text-muted-foreground">
          {t(
            "Published configuration · newest first",
            "Veröffentlichte Konfiguration · neueste zuerst",
          )}
        </div>
        <div className="overflow-x-auto">
          <Table className="text-sm">
            <TableHeader>
              <TableRow className="border-b border-border">
                <TableHead className="py-2 micro-label">{t("Version", "Version")}</TableHead>
                <TableHead className="py-2 micro-label">
                  {t("Published", "Veröffentlicht")}
                </TableHead>
                <TableHead className="py-2 micro-label">{t("Note", "Notiz")}</TableHead>
                <TableHead className="py-2 text-right micro-label" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {versions.map((version) => (
                <TableRow key={version.id} className="border-b border-border/50 align-top">
                  <TableCell className="py-2 pr-3 whitespace-nowrap">
                    <span className="font-mono">{`v${version.versionNo}`}</span>
                    {version.isCurrent && (
                      <span className="ml-2 inline-flex items-center rounded-full border border-status-running/50 bg-status-running/15 px-2 py-0.5 micro-label text-status-running-text">
                        {t("running", "läuft")}
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="py-2 pr-3 whitespace-nowrap text-muted-foreground">
                    {new Date(version.publishedAt).toLocaleString()}
                  </TableCell>
                  <TableCell className="py-2 pr-3">
                    {version.note ?? (
                      <span className="italic text-muted-foreground">
                        {t("no note", "keine Notiz")}
                      </span>
                    )}
                    {version.rolledBackFrom !== null && (
                      <div className="text-xs text-muted-foreground">
                        {t(
                          `rolled back from v${version.rolledBackFrom}`,
                          `zurückgesetzt von v${version.rolledBackFrom}`,
                        )}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="py-2 text-right whitespace-nowrap">
                    <button
                      type="button"
                      onClick={() => setOpen({ versionNo: version.versionNo, intent: "diff" })}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition hover:text-foreground"
                    >
                      <GitCompare className="h-3.5 w-3.5" /> {t("Diff", "Vergleich")}
                    </button>
                    {mayPublish && !version.isCurrent && (
                      <button
                        type="button"
                        onClick={() =>
                          setOpen({ versionNo: version.versionNo, intent: "rollback" })
                        }
                        className="ml-2 inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs text-muted-foreground transition hover:text-foreground"
                      >
                        <RotateCcw className="h-3.5 w-3.5" /> {t("Roll back", "Zurücksetzen")}
                      </button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Panel>

      {open && (
        <div
          className="fixed inset-0 z-40 grid place-items-center bg-black/60 p-4 backdrop-blur-sm"
          onClick={() => setOpen(null)}
        >
          <div
            role="dialog"
            aria-modal="true"
            className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded-xl border border-border bg-panel shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-border px-5 py-4">
              <div>
                <div className="micro-label">
                  {open.intent === "rollback"
                    ? t("Confirm rollback", "Zurücksetzen bestätigen")
                    : t("Compare", "Vergleich")}
                </div>
                <h2 className="font-serif text-xl">
                  {t(
                    `v${open.versionNo} → v${currentVersionNo ?? "?"} (running now)`,
                    `v${open.versionNo} → v${currentVersionNo ?? "?"} (läuft aktuell)`,
                  )}
                </h2>
              </div>
              <button
                type="button"
                onClick={() => setOpen(null)}
                aria-label={t("Close", "Schließen")}
                className="grid h-8 w-8 place-items-center rounded-md border border-border text-muted-foreground transition hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="p-5">
              {diff.isPending ? (
                <SkeletonRows
                  label={t("Loading…", "Wird geladen…")}
                  rows={4}
                  rowClassName="h-8"
                  className="py-2"
                />
              ) : (
                <DiffTable entries={diff.data?.entries ?? []} />
              )}
              {open.intent === "rollback" && (
                <div className="mt-5 border-t border-border pt-4">
                  <p className="text-xs text-muted-foreground">
                    {t(
                      `Rolling back publishes v${open.versionNo}'s configuration as a NEW version rather than reactivating the old row, so the history stays in order and the policy checks run again. Runs already in flight keep the version they started on.`,
                      `Beim Zurücksetzen wird die Konfiguration von v${open.versionNo} als NEUE Version veröffentlicht, statt die alte Zeile wieder zu aktivieren – so bleibt der Verlauf in Reihenfolge und die Richtlinienprüfungen laufen erneut. Laufende Läufe behalten ihre eigene Version.`,
                    )}
                  </p>
                  <div className="mt-4 flex justify-end gap-2">
                    <button
                      type="button"
                      onClick={() => setOpen(null)}
                      className="rounded-md border border-border px-3 py-2 text-sm text-muted-foreground transition hover:text-foreground"
                    >
                      {t("Cancel", "Abbrechen")}
                    </button>
                    <button
                      type="button"
                      onClick={() => confirmRollback(open.versionNo)}
                      disabled={rollback.isPending}
                      className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:brightness-110 disabled:opacity-50"
                    >
                      {rollback.isPending ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <RotateCcw className="h-4 w-4" />
                      )}
                      {t(`Roll back to v${open.versionNo}`, `Auf v${open.versionNo} zurücksetzen`)}
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
