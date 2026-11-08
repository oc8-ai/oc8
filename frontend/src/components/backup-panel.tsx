import { AlertTriangle, Archive, Download, Loader2, ShieldAlert, Upload } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/app-shell";
import { exportBackup } from "@/lib/api";
import { useOrganizationSettings, usePreviewBackup, useRestoreBackup } from "@/lib/hooks";
import { useT } from "@/lib/i18n";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

/** The archive's own manifest carries an "excluded" list (design doc §2.3/§3:
 * audit log, metering, and run evidence blobs never become rows) — read from
 * there rather than hardcoding a second copy that could drift from what the
 * backend actually leaves out. */
function manifestExcluded(manifest: Record<string, unknown> | undefined): string[] {
  const value = manifest?.excluded;
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

export function BackupPanel() {
  const t = useT();
  const { data: organization } = useOrganizationSettings();

  const [exportPassphrase, setExportPassphrase] = useState("");
  const [exporting, setExporting] = useState(false);
  // Design doc §5.5: the restore button additionally requires a fresh export
  // to have been downloaded in this session. Restore is irreversible and
  // deletes every agent, run and knowledge chunk this company has --
  // requiring a fresh export first removes the most likely way to lose data
  // by accident. This is explicitly NOT enforced server-side: a client
  // cannot be trusted to have actually done it, and pretending otherwise
  // would be theatre. It is a UI accident-guard only, not a security control.
  const [hasExportedThisSession, setHasExportedThisSession] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [restorePassphrase, setRestorePassphrase] = useState("");
  const [confirmName, setConfirmName] = useState("");
  const [resultDismissed, setResultDismissed] = useState(false);

  const preview = usePreviewBackup();
  const restore = useRestoreBackup();

  async function handleExport() {
    setExporting(true);
    try {
      const { blob, filename } = await exportBackup(exportPassphrase || undefined);
      // The archive's bytes are in hand as soon as the request above
      // resolves -- that is the meaningful "a fresh export exists" moment,
      // not the browser's own save-as mechanics a couple of lines down.
      setHasExportedThisSession(true);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      toast.success(t("Backup downloaded", "Sicherung heruntergeladen"), {
        description: filename,
      });
    } catch (err) {
      toast.error(
        err instanceof Error
          ? err.message
          : t("Could not build the archive.", "Archiv konnte nicht erstellt werden."),
      );
    } finally {
      setExporting(false);
    }
  }

  function handleFileChange(selected: File | null) {
    setFile(selected);
    setConfirmName("");
    setRestorePassphrase("");
    restore.reset();
    setResultDismissed(false);
    if (selected) preview.mutate(selected);
    else preview.reset();
  }

  const blockingProblems = preview.data?.problems ?? [];
  const previewSucceeded = preview.isSuccess && file != null;
  const nameMatches =
    organization != null && confirmName.length > 0 && organization.name === confirmName;
  // The backend re-checks confirm_name (and every other invariant) itself
  // before touching a row — this is a courtesy that keeps an operator from
  // firing a request that is certain to fail, not the actual guard.
  const canRestore =
    previewSucceeded && blockingProblems.length === 0 && nameMatches && hasExportedThisSession;

  function handleRestore() {
    if (!file || !canRestore) return;
    restore.mutate(
      { file, confirmName, passphrase: restorePassphrase || undefined },
      {
        onSuccess: () => {
          toast.success(t("Restore complete", "Wiederherstellung abgeschlossen"));
        },
        onError: (err) => {
          toast.error(
            err instanceof Error
              ? err.message
              : t("Restore failed.", "Wiederherstellung fehlgeschlagen."),
          );
        },
      },
    );
  }

  return (
    <Panel className="p-5 md:col-span-2">
      <header className="flex items-start gap-3">
        <div className="grid h-9 w-9 place-items-center rounded-md bg-primary/15 text-primary">
          <Archive className="h-4 w-4" />
        </div>
        <div>
          <h3 className="font-serif text-lg leading-tight">
            {t("Backup and restore", "Sicherung und Wiederherstellung")}
          </h3>
          <p className="mt-0.5 max-w-2xl text-xs text-muted-foreground">
            {t(
              "Export your whole company to one file, or replace it in place from a previously exported file.",
              "Exportiere dein gesamtes Unternehmen in eine Datei oder ersetze es anhand einer zuvor exportierten Datei.",
            )}
          </p>
        </div>
      </header>

      {/* ---- Export ---------------------------------------------------- */}
      <section className="mt-5 rounded-lg border border-border bg-background/40 p-4">
        <h4 className="flex items-center gap-1.5 text-sm font-medium">
          <Download className="h-3.5 w-3.5" />
          {t("Export", "Exportieren")}
        </h4>
        <p className="mt-1 text-xs text-muted-foreground">
          {t(
            "Downloads every agent, department, run history and knowledge item this company holds as one file.",
            "Lädt jeden Agenten, jede Abteilung, jeden Lauf-Verlauf und jeden Wissenseintrag dieses Unternehmens als eine Datei herunter.",
          )}
        </p>

        <label htmlFor="backup-export-passphrase" className="mt-3 block micro-label">
          {t("Passphrase (optional)", "Passphrase (optional)")}
        </label>
        <input
          id="backup-export-passphrase"
          type="password"
          value={exportPassphrase}
          onChange={(e) => setExportPassphrase(e.target.value)}
          autoComplete="new-password"
          spellCheck={false}
          className="mt-1 w-full max-w-sm rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm"
        />
        <div className="mt-2 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-xs">
          <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-warning-text" />
          <p className="text-foreground/90">
            {t(
              "Every export, with or without a passphrase, carries every member's password hash. With a passphrase, the file also carries every provider credential and OAuth token this company holds, encrypted under it — anyone with the file AND the passphrase holds those credentials too. The passphrase cannot be recovered if lost; losing it only costs the secrets, everything else still restores.",
              "Jeder Export, mit oder ohne Passphrase, enthält den Passwort-Hash jedes Mitglieds. Mit Passphrase enthält die Datei zusätzlich jedes Provider-Zugangsdatum und jeden OAuth-Token dieses Unternehmens, damit verschlüsselt — wer Datei UND Passphrase besitzt, besitzt auch diese Zugangsdaten. Eine verlorene Passphrase ist nicht wiederherstellbar; ihr Verlust kostet nur die Zugangsdaten, alles andere lässt sich weiterhin wiederherstellen.",
            )}
          </p>
        </div>

        <button
          type="button"
          disabled={exporting}
          onClick={handleExport}
          className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition hover:opacity-90 disabled:opacity-40"
        >
          {exporting && <Loader2 className="h-3 w-3 animate-spin" />}
          {exporting
            ? t("Building archive…", "Archiv wird erstellt…")
            : t("Download backup", "Sicherung herunterladen")}
        </button>
      </section>

      {/* ---- Restore ----------------------------------------------------- */}
      <section className="mt-4 rounded-lg border border-border bg-background/40 p-4">
        <h4 className="flex items-center gap-1.5 text-sm font-medium">
          <Upload className="h-3.5 w-3.5" />
          {t("Restore", "Wiederherstellen")}
        </h4>
        <div className="mt-1 flex items-start gap-2 rounded-md border border-status-error/40 bg-status-error/10 p-3 text-xs">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-error" />
          <p className="text-foreground/90">
            {t(
              "This deletes every agent, run and knowledge item this company currently has and replaces them with the contents of the uploaded file. It cannot be undone.",
              "Dies löscht jeden Agenten, jeden Lauf und jeden Wissenseintrag, den dieses Unternehmen derzeit hat, und ersetzt sie durch den Inhalt der hochgeladenen Datei. Dies kann nicht rückgängig gemacht werden.",
            )}
          </p>
        </div>

        <div className="mt-2 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-xs">
          <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-warning-text" />
          <p className="text-foreground/90">
            {t(
              "If this archive was taken on a different oc8 instance, restoring it also replaces every member and role this company has with the source instance's — including your own membership. Because those rows carry sign-in identities from the source instance's own login provider, this can remove your own access to this company.",
              "Wurde dieses Archiv auf einer anderen oc8-Instanz erstellt, ersetzt die Wiederherstellung auch jedes Mitglied und jede Rolle dieses Unternehmens durch die der Quellinstanz — einschließlich deiner eigenen Mitgliedschaft. Da diese Zeilen Anmeldeidentitäten des Login-Anbieters der Quellinstanz tragen, kann dies deinen eigenen Zugriff auf dieses Unternehmen entfernen.",
            )}
          </p>
        </div>

        <label htmlFor="backup-restore-file" className="mt-3 block micro-label">
          {t("Archive file", "Archivdatei")}
        </label>
        <input
          id="backup-restore-file"
          type="file"
          accept=".tar.gz,.tgz,application/gzip"
          onChange={(e) => handleFileChange(e.target.files?.[0] ?? null)}
          className="mt-1 w-full max-w-sm text-xs"
        />

        {preview.isPending && (
          <p className="mt-3 flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            {t("Reading archive…", "Archiv wird gelesen…")}
          </p>
        )}

        {preview.isError && (
          <p className="mt-3 text-xs text-status-error">
            {preview.error instanceof Error
              ? preview.error.message
              : t("Could not read the archive.", "Archiv konnte nicht gelesen werden.")}
          </p>
        )}

        {preview.data && (
          <div className="mt-3">
            <p className="micro-label">
              {t(
                "Currently in this company → in the archive",
                "Derzeit in diesem Unternehmen → im Archiv",
              )}
            </p>
            <div className="mt-1.5 overflow-hidden rounded-md border border-border">
              <Table className="text-xs">
                <TableHeader>
                  <TableRow className="border-b border-border bg-background/60 text-left text-muted-foreground">
                    <TableHead className="px-3 py-1.5 micro-label">
                      {t("Table", "Tabelle")}
                    </TableHead>
                    <TableHead className="px-3 py-1.5 micro-label">
                      {t("Currently", "Derzeit")}
                    </TableHead>
                    <TableHead className="px-3 py-1.5 micro-label">
                      {t("In archive", "Im Archiv")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="divide-y divide-border">
                  {Object.entries(preview.data.table_counts).map(([table, counts]) => (
                    <TableRow key={table}>
                      <TableCell className="px-3 py-1.5 font-mono">{table}</TableCell>
                      <TableCell className="px-3 py-1.5">{counts.current}</TableCell>
                      <TableCell className="px-3 py-1.5">{counts.archive}</TableCell>
                    </TableRow>
                  ))}
                  {Object.keys(preview.data.table_counts).length === 0 && (
                    <TableRow>
                      <TableCell colSpan={3} className="px-3 py-1.5 text-muted-foreground">
                        {t("Nothing on either side.", "Nichts auf beiden Seiten.")}
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>

            {manifestExcluded(preview.data.manifest).length > 0 && (
              <p className="mt-2 text-xs text-muted-foreground">
                {t("Not part of any backup:", "Nicht Teil einer Sicherung:")}{" "}
                {manifestExcluded(preview.data.manifest).join(", ")}
              </p>
            )}

            {blockingProblems.length > 0 && (
              <div className="mt-2 rounded-md border border-status-error/40 bg-status-error/10 p-3 text-xs">
                <p className="font-medium text-status-error">
                  {t(
                    "This archive cannot be restored:",
                    "Dieses Archiv kann nicht wiederhergestellt werden:",
                  )}
                </p>
                <ul className="mt-1 list-inside list-disc text-foreground/90">
                  {blockingProblems.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
              </div>
            )}

            {preview.data.has_secrets && blockingProblems.length === 0 && (
              <div className="mt-3">
                <label htmlFor="backup-restore-passphrase" className="block micro-label">
                  {t("Passphrase", "Passphrase")}
                </label>
                <input
                  id="backup-restore-passphrase"
                  type="password"
                  value={restorePassphrase}
                  onChange={(e) => setRestorePassphrase(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  className="mt-1 w-full max-w-sm rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm"
                />
                <div className="mt-2 flex items-start gap-2 rounded-md border border-status-warning/40 bg-status-warning/10 p-3 text-xs">
                  <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-status-warning-text" />
                  <p className="text-foreground/90">
                    {t(
                      "This archive carries provider credentials and OAuth tokens, encrypted under this passphrase. Enter it exactly as set at export time — a wrong passphrase fails the whole restore before anything is touched.",
                      "Dieses Archiv enthält Provider-Zugangsdaten und OAuth-Tokens, verschlüsselt mit dieser Passphrase. Gib sie genau wie beim Export ein — eine falsche Passphrase lässt die gesamte Wiederherstellung fehlschlagen, bevor irgendetwas verändert wird.",
                    )}
                  </p>
                </div>
              </div>
            )}

            {blockingProblems.length === 0 && (
              <div className="mt-3">
                <label htmlFor="backup-confirm-name" className="block micro-label">
                  {t(
                    "Type this company's name to confirm",
                    "Gib den Namen dieses Unternehmens zur Bestätigung ein",
                  )}
                </label>
                <p className="mt-0.5 font-mono text-xs text-muted-foreground">
                  {organization?.name}
                </p>
                <input
                  id="backup-confirm-name"
                  type="text"
                  value={confirmName}
                  onChange={(e) => setConfirmName(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  className="mt-1 w-full max-w-sm rounded-md border border-border bg-background px-3 py-1.5 text-sm"
                />
              </div>
            )}

            {blockingProblems.length === 0 && !hasExportedThisSession && (
              <p className="mt-2 text-xs text-status-warning-text">
                {t(
                  "Download a fresh backup above first — restoring is irreversible, and this is the recovery path if it goes wrong.",
                  "Lade oben zuerst eine aktuelle Sicherung herunter — die Wiederherstellung ist unwiderruflich, und das ist der Weg zurück, falls dabei etwas schiefgeht.",
                )}
              </p>
            )}

            <button
              type="button"
              disabled={!canRestore || restore.isPending}
              onClick={handleRestore}
              className="mt-3 inline-flex items-center gap-1.5 rounded-md bg-status-error px-3 py-1.5 text-xs font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {restore.isPending && <Loader2 className="h-3 w-3 animate-spin" />}
              {t("Restore company from backup", "Unternehmen aus Sicherung wiederherstellen")}
            </button>
          </div>
        )}

        {restore.data && !resultDismissed && (
          <div className="mt-4 rounded-md border border-border bg-background/60 p-4 text-xs">
            <div className="flex items-start justify-between gap-3">
              <p className="font-medium">
                {t("Restore complete.", "Wiederherstellung abgeschlossen.")}
              </p>
              <button
                type="button"
                onClick={() => setResultDismissed(true)}
                className="shrink-0 text-muted-foreground transition hover:text-foreground"
              >
                {t("Dismiss", "Schließen")}
              </button>
            </div>
            <ul className="mt-2 space-y-0.5">
              {Object.entries(restore.data.tables).map(([table, count]) => (
                <li key={table} className="flex justify-between font-mono text-xs">
                  <span>{table}</span>
                  <span>{count}</span>
                </li>
              ))}
            </ul>
            <p className="mt-2 text-muted-foreground">
              {t("Credentials restored:", "Wiederhergestellte Zugangsdaten:")}{" "}
              {restore.data.secrets_restored}
            </p>
            {restore.data.excluded.length > 0 && (
              <p className="mt-1 text-muted-foreground">
                {t("Not restored:", "Nicht wiederhergestellt:")} {restore.data.excluded.join(", ")}
              </p>
            )}
          </div>
        )}
      </section>
    </Panel>
  );
}
