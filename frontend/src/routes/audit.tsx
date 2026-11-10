import { createFileRoute } from "@tanstack/react-router";
import { AlertTriangle, ChevronDown, ChevronRight, Download } from "lucide-react";
import { Fragment, useEffect, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import {
  downloadAuditExport,
  useAuditEvents,
  useAuditIntegrity,
  useVerifyChain,
  type AuditFilters,
} from "@/lib/audit-hooks";
import { cn } from "@/lib/utils";
import { useT } from "@/lib/i18n";

export const Route = createFileRoute("/audit")({
  component: AuditPage,
});

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(id);
  }, [value, delayMs]);
  return debounced;
}

function relativeAge(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

function shortDate(iso: string, locale: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
}

function IntegrityBanner() {
  const t = useT();
  const { data: integrity } = useAuditIntegrity();
  const verify = useVerifyChain();

  const status = integrity?.status ?? "never";
  const color =
    status === "ok"
      ? "var(--status-running)"
      : status === "broken"
        ? "var(--status-error)"
        : "var(--muted-foreground)";

  // A truncation cannot be cleared by re-checking: the entries are gone, and
  // only restoring them from a backup brings them back (which the server's
  // count check then detects on its own). There is deliberately no
  // "mark as resolved" action — that primitive is exactly what this screen
  // exists to remove from the hands of whoever ordered the deletion.
  const isTruncation = status === "broken" && integrity?.breakKind === "truncation";
  // The one break kind a full check cannot clear either, and the only one with
  // a legitimate non-attack cause: turning the MAC on over an existing chain
  // leaves checkpoints without a signed marker, which is indistinguishable
  // from an attacker deleting one. Deliberately NOT a separate break kind —
  // the server cannot tell the two apart, so the copy carries the ambiguity
  // and names the operator command that resolves it.
  const isMacDowngrade = status === "broken" && integrity?.breakKind === "mac_downgrade";
  const brokenSeq = integrity?.brokenAtSeq ?? "?";

  // A count, not a seq range: for a deletion in the middle of the chain
  // (e.g. row 3 of 5), "entries recorded after seq N are missing" is false --
  // nothing after the last verified position is actually gone. The count is
  // exact regardless of WHICH rows are missing, so it is the only claim this
  // banner can make truthfully. brokenSeq stays in the copy only as "last
  // verified position", never as a bound on what went missing.
  const missingCount = integrity?.missingCount ?? null;
  const missingEn =
    missingCount === 1
      ? "1 entry that was previously present is missing"
      : `${missingCount ?? "some"} entries that were previously present are missing`;
  const missingDe =
    missingCount === 1
      ? "1 zuvor vorhandener Eintrag fehlt"
      : `${missingCount ?? "einige"} zuvor vorhandene Einträge fehlen`;

  const message =
    status === "ok"
      ? t(
          `verified through seq ${integrity?.verifiedThroughSeq ?? 0} · checked ${relativeAge(integrity?.verifiedAt ?? "")}`,
          `geprüft bis lfd. Nr. ${integrity?.verifiedThroughSeq ?? 0} · geprüft ${relativeAge(integrity?.verifiedAt ?? "")}`,
        )
      : status === "broken"
        ? isTruncation
          ? t("Entries are missing from this log.", "In diesem Protokoll fehlen Einträge.")
          : isMacDowngrade
            ? t(
                "This log's tamper-proof seal is missing or does not match.",
                "Das Manipulationssiegel dieses Protokolls fehlt oder stimmt nicht überein.",
              )
            : t(
                `integrity check failed — flagged entry: seq ${brokenSeq}`,
                `Integritätsprüfung fehlgeschlagen — markierter Eintrag: lfd. Nr. ${brokenSeq}`,
              )
        : status === "unverifiable"
          ? // Deliberately not phrased as a finding: the verifier could not
            // run, which says nothing at all about the chain. Reporting it as
            // a break would cry wolf; reporting it as "never verified" would
            // hide a configuration fault behind a benign-looking state.
            t(
              "could not be checked — the tamper-proofing key is unavailable",
              "konnte nicht geprüft werden — der Schlüssel für den Manipulationsschutz ist nicht verfügbar",
            )
          : t("never verified", "noch nie geprüft");

  const detail =
    status !== "broken"
      ? null
      : isTruncation
        ? t(
            `${missingEn}. Last verified position: seq ${brokenSeq}. Re-verifying will not clear this — the missing entries have to be restored from a backup. Treat this as a security event and escalate.`,
            `${missingDe}. Zuletzt geprüfte Position: lfd. Nr. ${brokenSeq}. Eine erneute Prüfung ändert daran nichts — die fehlenden Einträge müssen aus einer Sicherung wiederhergestellt werden. Behandle dies als Sicherheitsvorfall und eskaliere.`,
          )
        : isMacDowngrade
          ? t(
              "Re-checking alone will not clear this. If tamper-proofing was only just switched on for this deployment, an administrator has to run the one-off `oc8 audit-adopt-checkpoints` command to seal the existing logs, and then a full check here. If it was already switched on, the seal was removed or the entries were rewritten — treat this as a security event and escalate.",
              "Eine erneute Prüfung allein ändert daran nichts. Falls der Manipulationsschutz für diese Installation gerade erst aktiviert wurde, muss eine Administratorin den einmaligen Befehl `oc8 audit-adopt-checkpoints` ausführen, um die vorhandenen Protokolle zu versiegeln, und danach hier eine vollständige Prüfung starten. War er bereits aktiv, wurde das Siegel entfernt oder die Einträge wurden umgeschrieben — behandle dies als Sicherheitsvorfall und eskaliere.",
            )
          : t(
              `Entry seq ${brokenSeq} is the first that no longer hashes correctly. A full check re-verifies the whole chain, so it is the way to confirm a legitimate restore.`,
              `Der Eintrag lfd. Nr. ${brokenSeq} ist der erste, dessen Hash nicht mehr stimmt. Die vollständige Prüfung überprüft die gesamte Kette und bestätigt so eine rechtmäßige Wiederherstellung.`,
            );

  // Write-once server-side, so it stays visible even after a successful full
  // re-verification returns the banner to green. A cleared status is the
  // operator's acknowledgement, not evidence that nothing ever happened.
  // Only outside the broken state: while the banner already says the check
  // failed, a second line saying a break was detected is just noise.
  const firstBreakAt = status === "broken" ? null : (integrity?.firstBreakAt ?? null);
  const priorBreakNote = firstBreakAt
    ? t(
        `A break was previously detected on ${shortDate(firstBreakAt, "en-GB")}.`,
        `Am ${shortDate(firstBreakAt, "de-DE")} wurde bereits eine Unterbrechung festgestellt.`,
      )
    : null;

  const runVerify = (full: boolean) =>
    verify.mutate(full, {
      onError: (e) =>
        toast.error(
          e instanceof Error ? e.message : t("Verification failed", "Prüfung fehlgeschlagen"),
        ),
    });

  const isBroken = status === "broken";

  const quickLabel = verify.isPending ? t("Verifying…", "Prüfe…") : t("Verify now", "Jetzt prüfen");
  const fullLabel = verify.isPending
    ? t("Verifying…", "Prüfe…")
    : t("Full check", "Vollständige Prüfung");

  const outlineClass =
    "rounded-md border border-border bg-panel px-3 py-1.5 text-sm text-foreground transition hover:bg-primary/10 disabled:opacity-50";
  const dangerPrimaryClass =
    "rounded-md border border-[color:var(--status-error)]/50 bg-[color:var(--status-error)]/15 px-3 py-1.5 text-sm font-medium text-[color:var(--status-error)] transition hover:bg-[color:var(--status-error)]/25 disabled:opacity-50";

  return (
    <div
      className="rounded-xl border px-5 py-4 shadow-[0_1px_0_0_oklch(1_0_0/6%)_inset,0_10px_30px_-20px_oklch(0_0_0/60%)]"
      style={{
        borderColor: `color-mix(in oklab, ${color} 35%, transparent)`,
        background: `color-mix(in oklab, ${color} 8%, transparent)`,
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: color, boxShadow: `0 0 8px ${color}` }}
          />
          <span className="text-sm" style={{ color }}>
            {message}
          </span>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => runVerify(false)}
            disabled={verify.isPending}
            className={outlineClass}
          >
            {quickLabel}
          </button>
          <button
            type="button"
            onClick={() => runVerify(true)}
            disabled={verify.isPending}
            // Emphasised only where it is the actual FIRST step of the remedy.
            // A truncation needs the entries back from a backup; a failed seal
            // needs the seal re-issued out of band first (a full check is only
            // step two there, and the detail copy says so in order). Leading
            // with it would send the operator down the wrong path.
            className={
              isBroken && !isTruncation && !isMacDowngrade ? dangerPrimaryClass : outlineClass
            }
          >
            {fullLabel}
          </button>
        </div>
      </div>
      {!isTruncation && (
        <p className="mt-2 pl-[18px] text-xs text-muted-foreground">
          {t(
            "Quick check only covers entries since the last checkpoint; full check re-verifies the entire chain from the beginning.",
            "Die Schnellprüfung erfasst nur Einträge seit dem letzten Prüfpunkt; die vollständige Prüfung überprüft die gesamte Kette von Beginn an.",
          )}
        </p>
      )}
      {detail && (
        <p
          className={cn("pl-[18px] text-xs text-muted-foreground", isTruncation ? "mt-2" : "mt-1")}
        >
          {detail}
        </p>
      )}
      {priorBreakNote && (
        <p className="mt-1 pl-[18px] text-xs text-muted-foreground">{priorBreakNote}</p>
      )}
    </div>
  );
}

function AuditPage() {
  const t = useT();
  const { data: integrity, isError: integrityIsError, error: integrityError } = useAuditIntegrity();
  const [draftFilters, setDraftFilters] = useState<AuditFilters>({});
  const filters = useDebouncedValue(draftFilters, 300);
  const [openId, setOpenId] = useState<string | null>(null);
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isError: eventsIsError,
    error: eventsError,
  } = useAuditEvents(filters);

  const events = data?.pages.flatMap((p) => p.events) ?? [];
  const broken = integrity?.status === "broken" ? integrity.brokenAtSeq : null;

  const setFilter = (key: keyof AuditFilters) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setDraftFilters((f) => ({ ...f, [key]: e.target.value || undefined }));

  const onExport = async (format: "csv" | "jsonl") => {
    try {
      await downloadAuditExport(draftFilters, format);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t("Export failed", "Export fehlgeschlagen"));
    }
  };

  if (integrityIsError || eventsIsError) {
    const err = integrityError ?? eventsError;
    const errDetail = err instanceof Error && err.message ? err.message : null;
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-3 rounded-xl border border-[color:var(--status-error)]/35 bg-[color:var(--status-error)]/8 px-5 py-4 shadow-[0_1px_0_0_oklch(1_0_0/6%)_inset,0_10px_30px_-20px_oklch(0_0_0/60%)]">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-[color:var(--status-error)]" />
          <div>
            <p className="text-sm text-[color:var(--status-error)]">
              {t("Could not load the audit log.", "Prüfprotokoll konnte nicht geladen werden.")}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {errDetail ??
                t(
                  "You may not have permission to view this page, or the server could not be reached.",
                  "Möglicherweise fehlt dir die Berechtigung für diese Seite, oder der Server war nicht erreichbar.",
                )}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <IntegrityBanner />

      <div className="flex flex-wrap items-end gap-2">
        <input
          value={draftFilters.category ?? ""}
          onChange={setFilter("category")}
          placeholder={t("Category", "Kategorie")}
          className="w-36 rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary/50"
        />
        <input
          value={draftFilters.action ?? ""}
          onChange={setFilter("action")}
          placeholder={t("Action", "Aktion")}
          className="w-36 rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary/50"
        />
        <input
          value={draftFilters.actorType ?? ""}
          onChange={setFilter("actorType")}
          placeholder={t("Actor type", "Akteurtyp")}
          className="w-36 rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary/50"
        />
        <input
          value={draftFilters.decision ?? ""}
          onChange={setFilter("decision")}
          placeholder={t("Decision", "Entscheidung")}
          className="w-36 rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none placeholder:text-muted-foreground focus:border-primary/50"
        />
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          {/* The server interprets these bounds as UTC, while the table below
              renders timestamps in the viewer's local time -- say so, rather
              than leaving an operator silently off by their offset. */}
          {t("From (UTC)", "Von (UTC)")}
          <input
            type="date"
            value={draftFilters.from ?? ""}
            onChange={setFilter("from")}
            className="rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none focus:border-primary/50"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          {t("To (UTC)", "Bis (UTC)")}
          <input
            type="date"
            value={draftFilters.to ?? ""}
            onChange={setFilter("to")}
            className="rounded-md border border-border bg-panel px-3 py-1.5 text-sm outline-none focus:border-primary/50"
          />
        </label>
        <div className="ml-auto flex gap-2">
          <button
            type="button"
            onClick={() => void onExport("csv")}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground"
          >
            <Download className="h-3.5 w-3.5" /> CSV
          </button>
          <button
            type="button"
            onClick={() => void onExport("jsonl")}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-panel px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground"
          >
            <Download className="h-3.5 w-3.5" /> JSONL
          </button>
        </div>
      </div>

      <Panel className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-5 py-3 font-medium" />
                <th className="px-5 py-3 font-medium">{t("Time", "Zeit")}</th>
                <th className="px-5 py-3 font-medium">{t("Category", "Kategorie")}</th>
                <th className="px-5 py-3 font-medium">{t("Action", "Aktion")}</th>
                <th className="px-5 py-3 font-medium">{t("Actor type", "Akteurtyp")}</th>
                <th className="px-5 py-3 font-medium">{t("Decision", "Entscheidung")}</th>
                <th className="px-5 py-3 font-medium">{t("Responsible", "Verantwortlich")}</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev) => {
                const isOpen = openId === ev.id;
                const flagged = broken !== null && ev.seq >= broken;
                return (
                  <Fragment key={ev.id}>
                    <tr
                      onClick={() => setOpenId(isOpen ? null : ev.id)}
                      className={cn(
                        "cursor-pointer border-b border-border/60 last:border-none transition hover:bg-primary/5",
                        flagged && "border-l-2 border-l-[color:var(--status-error)]",
                      )}
                    >
                      <td className="px-5 py-3">
                        {isOpen ? (
                          <ChevronDown className="h-4 w-4 text-muted-foreground" />
                        ) : (
                          <ChevronRight className="h-4 w-4 text-muted-foreground" />
                        )}
                      </td>
                      <td className="whitespace-nowrap px-5 py-3 font-mono text-xs text-muted-foreground">
                        {new Date(ev.ts).toLocaleString()}
                      </td>
                      <td className="px-5 py-3">{ev.category}</td>
                      <td className="px-5 py-3">{ev.action}</td>
                      <td className="px-5 py-3">{ev.actorType}</td>
                      <td className="px-5 py-3">{ev.decision ?? "—"}</td>
                      <td className="px-5 py-3 font-mono text-xs text-muted-foreground">
                        {ev.responsibleId ?? "—"}
                      </td>
                    </tr>
                    {isOpen && (
                      <tr className="border-b border-border/60 bg-background/30">
                        <td colSpan={7} className="space-y-2 px-14 py-3 text-xs">
                          <pre className="overflow-x-auto whitespace-pre-wrap break-all text-muted-foreground">
                            {JSON.stringify(ev.resource, null, 2)}
                          </pre>
                          <div className="font-mono text-muted-foreground">
                            hash: {ev.hash}
                            <br />
                            prevHash: {ev.prevHash}
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
              {events.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-5 py-8 text-center text-muted-foreground">
                    {t(
                      "No audit events for this filter.",
                      "Keine Prüfprotokoll-Einträge für diesen Filter.",
                    )}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {hasNextPage && (
          <div className="flex justify-center border-t border-border p-3">
            <button
              type="button"
              onClick={() => fetchNextPage()}
              disabled={isFetchingNextPage}
              className="rounded-md border border-border bg-panel px-3 py-1.5 text-sm text-muted-foreground transition hover:text-foreground disabled:opacity-50"
            >
              {isFetchingNextPage ? t("Loading…", "Lädt…") : t("Load more", "Mehr laden")}
            </button>
          </div>
        )}
      </Panel>
    </div>
  );
}
