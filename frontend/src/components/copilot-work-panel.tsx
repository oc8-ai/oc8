import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { NeedsMeWidget } from "@/components/dashboard/widgets/needs-me-widget";
import { RunStepTimeline } from "@/components/run-step-timeline";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useCancelRun, type RunDTO } from "@/lib/hooks";
import {
  copilotKeys,
  useCopilotDelegations,
  useCopilotNotes,
  useCopilotProfile,
  useDeleteCopilotNote,
  useEndFollowup,
  useFollowups,
  useResponsibilities,
  useSetResponsibilityState,
  type FollowupDTO,
  type ResponsibilityDTO,
} from "@/lib/hooks-copilot";
import { useLang, useT } from "@/lib/i18n";

const ACTIVE_RUN_STATES = new Set([
  "queued",
  "running",
  "waiting_for_input",
  "waiting_for_approval",
]);

function ago(iso: string | null, lang: string): string | null {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: "auto" });
  const mins = Math.round(ms / 60000);
  if (Math.abs(mins) < 60) return rtf.format(mins, "minute");
  const hours = Math.round(mins / 60);
  if (Math.abs(hours) < 48) return rtf.format(hours, "hour");
  return rtf.format(Math.round(hours / 24), "day");
}

function inZone(iso: string | null, tz: string | null, lang: string): string | null {
  if (!iso) return null;
  try {
    return new Intl.DateTimeFormat(lang, {
      dateStyle: "medium",
      timeStyle: "short",
      ...(tz ? { timeZone: tz } : {}),
    }).format(new Date(iso));
  } catch {
    return new Date(iso).toLocaleString(lang);
  }
}

function dayInZone(iso: string | null, tz: string | null, lang: string): string | null {
  if (!iso) return null;
  try {
    return new Intl.DateTimeFormat(lang, {
      dateStyle: "medium",
      ...(tz ? { timeZone: tz } : {}),
    }).format(new Date(iso));
  } catch {
    return new Date(iso).toLocaleDateString(lang);
  }
}

/** Plain words for the schedules people actually create; anything else shows
 *  the raw cron (which is also the tooltip for every row). */
function cronWords(t: ReturnType<typeof useT>, cron: string): string {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* \*$/.exec(cron.trim());
  if (m) {
    const time = `${m[2].padStart(2, "0")}:${m[1].padStart(2, "0")}`;
    return t("Every day at {time}", "Jeden Tag um {time}").replace("{time}", time);
  }
  const w = /^(\d{1,2}) (\d{1,2}) \* \* ([0-6]|1-5)$/.exec(cron.trim());
  if (w) {
    const time = `${w[2].padStart(2, "0")}:${w[1].padStart(2, "0")}`;
    return w[3] === "1-5"
      ? t("Weekdays at {time}", "Werktags um {time}").replace("{time}", time)
      : t("Weekly at {time}", "Wöchentlich um {time}").replace("{time}", time);
  }
  return cron;
}

function stateLabel(t: ReturnType<typeof useT>, state: string): string {
  switch (state) {
    case "active":
      return t("active", "aktiv");
    case "waiting":
      return t("waiting", "wartet");
    case "paused":
      return t("paused", "pausiert");
    case "done":
      return t("done", "erledigt");
    case "cancelled":
      return t("cancelled", "abgebrochen");
    default:
      return state;
  }
}

function skipReasonLabel(t: ReturnType<typeof useT>, reason: string): string {
  switch (reason) {
    case "busy":
      return t("Skipped: you were mid-conversation", "Übersprungen: du warst im Gespräch");
    case "paused":
      return t("Skipped: Copilot was paused", "Übersprungen: Copilot war pausiert");
    case "not_active":
      return t("Skipped: the responsibility is not active", "Übersprungen: Aufgabe nicht aktiv");
    case "no_permission":
      return t("Skipped: no permission to use the Copilot", "Übersprungen: keine Berechtigung");
    case "no_member":
      return t(
        "Skipped: the member no longer exists",
        "Übersprungen: Mitglied existiert nicht mehr",
      );
    case "no_session":
      return t("Skipped: the chat session is gone", "Übersprungen: Chat-Sitzung fehlt");
    case "ended":
      return t("The schedule has ended", "Der Zeitplan ist beendet");
    default:
      return reason;
  }
}

function ResponsibilityRow({ r }: { r: ResponsibilityDTO }) {
  const t = useT();
  const { lang } = useLang();
  const setState = useSetResponsibilityState();
  const paused = r.state === "paused";
  const updated = ago(r.lastUpdateAt, lang);
  return (
    <div className="space-y-1.5 rounded-md border border-border p-3 text-sm">
      <div className="flex items-start gap-2">
        <span className="min-w-0 flex-1 font-medium">{r.title}</span>
        <Badge variant="secondary">{stateLabel(t, r.state)}</Badge>
      </div>
      <p className="text-xs text-muted-foreground">{r.goal}</p>
      {r.nextStep && (
        <p className="text-xs">
          <span className="text-muted-foreground">{t("Next step", "Nächster Schritt")}: </span>
          {r.nextStep}
        </p>
      )}
      {updated && (
        <p className="text-[11px] text-muted-foreground">
          {t("Updated", "Aktualisiert")} {updated}
        </p>
      )}
      <div className="flex flex-wrap gap-1.5 pt-1">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setState.mutate({ id: r.id, state: paused ? "active" : "paused" })}
        >
          {paused ? t("Resume", "Fortsetzen") : t("Pause", "Pausieren")}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setState.mutate({ id: r.id, state: "done" })}
        >
          {t("Done", "Erledigt")}
        </Button>
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button size="sm" variant="ghost">
              {t("Cancel", "Abbrechen")}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>
                {t("Cancel this responsibility?", "Diese Aufgabe abbrechen?")}
              </AlertDialogTitle>
              <AlertDialogDescription>
                {t(
                  "Its scheduled follow-ups are switched off too.",
                  "Die geplanten Wiedervorlagen werden ebenfalls abgeschaltet.",
                )}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>{t("Keep it", "Behalten")}</AlertDialogCancel>
              <AlertDialogAction onClick={() => setState.mutate({ id: r.id, state: "cancelled" })}>
                {t("Cancel responsibility", "Aufgabe abbrechen")}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
    </div>
  );
}

function DelegatedRun({ run }: { run: RunDTO }) {
  const t = useT();
  const cancel = useCancelRun(run.id);
  const qc = useQueryClient();
  const active = ACTIVE_RUN_STATES.has(run.state);
  return (
    <div className="space-y-1">
      <RunStepTimeline run={run} />
      {active && (
        <Button
          size="sm"
          variant="outline"
          disabled={cancel.isPending}
          onClick={() =>
            cancel.mutate(undefined, {
              onSuccess: () => qc.invalidateQueries({ queryKey: copilotKeys.delegations }),
            })
          }
        >
          {t("Stop", "Stoppen")}
        </Button>
      )}
    </div>
  );
}

function ActivityTab() {
  const t = useT();
  const { data: responsibilities = [] } = useResponsibilities();
  const { data: runs = [] } = useCopilotDelegations();
  const open = responsibilities.filter((r) => r.state !== "done" && r.state !== "cancelled");
  return (
    <div className="space-y-4 p-3">
      {open.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {t("Nothing in progress right now.", "Gerade ist nichts in Arbeit.")}
        </p>
      ) : (
        open.map((r) => <ResponsibilityRow key={r.id} r={r} />)
      )}
      {runs.length > 0 && (
        <div className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t("Delegated to the team", "An das Team delegiert")}
          </h3>
          {runs.map((run) => (
            <DelegatedRun key={run.id} run={run} />
          ))}
        </div>
      )}
    </div>
  );
}

function FollowupRow({ f }: { f: FollowupDTO }) {
  const t = useT();
  const { lang } = useLang();
  const end = useEndFollowup();
  const next = inZone(f.nextRunAt, f.timezone, lang);
  const ends = dayInZone(f.endsAt, f.timezone, lang);
  return (
    <div
      className={`space-y-1 rounded-md border border-border p-3 text-sm ${f.enabled ? "" : "opacity-60"}`}
    >
      <div className="font-medium">{f.responsibilityTitle}</div>
      <div className="text-xs text-muted-foreground" title={f.cronExpression ?? undefined}>
        {f.kind === "once" || !f.cronExpression
          ? t("Once", "Einmalig")
          : cronWords(t, f.cronExpression)}
      </div>
      {f.timezone && (
        <div className="text-xs text-muted-foreground">
          {t("Timezone", "Zeitzone")}: {f.timezone}
        </div>
      )}
      {next && (
        <div className="text-xs">
          {t("Next run", "Nächster Lauf")}: {next}
        </div>
      )}
      {ends && (
        <div className="text-xs">
          {t("Ends", "Endet")}: {ends}
        </div>
      )}
      {!f.enabled && f.lastSkipReason && (
        <div className="text-xs text-muted-foreground">{skipReasonLabel(t, f.lastSkipReason)}</div>
      )}
      <Button size="sm" variant="outline" onClick={() => end.mutate(f.id)}>
        {t("End schedule", "Zeitplan beenden")}
      </Button>
    </div>
  );
}

function ScheduledTab() {
  const t = useT();
  const { data: followups = [] } = useFollowups();
  return (
    <div className="space-y-2 p-3">
      {followups.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {t("No follow-ups are scheduled.", "Es sind keine Wiedervorlagen geplant.")}
        </p>
      ) : (
        followups.map((f) => <FollowupRow key={f.id} f={f} />)
      )}
    </div>
  );
}

function NotesTab() {
  const t = useT();
  const { data: notes = [] } = useCopilotNotes();
  const del = useDeleteCopilotNote();
  return (
    <div className="space-y-2 p-3">
      <p className="text-xs text-muted-foreground">
        {t(
          "Notes your Copilot keeps about you. Only you can see them.",
          "Notizen, die dein Copilot über dich führt. Nur du siehst sie.",
        )}
      </p>
      {notes.map((n) => (
        <div
          key={n.id}
          className="flex items-start gap-2 rounded-md border border-border p-2 text-sm"
        >
          <span className="min-w-0 flex-1 whitespace-pre-wrap">{n.content}</span>
          <Button size="sm" variant="ghost" onClick={() => del.mutate(n.id)}>
            {t("Delete", "Löschen")}
          </Button>
        </div>
      ))}
    </div>
  );
}

export function CopilotWorkPanel() {
  const t = useT();
  const { data: profile } = useCopilotProfile();
  const [tab, setTab] = useState("activity");
  return (
    <div className="flex h-full min-h-0 flex-col">
      <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col">
        <TabsList className="mx-3 mt-3 grid grid-cols-4">
          <TabsTrigger value="activity">{t("Activity", "Aktivität")}</TabsTrigger>
          <TabsTrigger value="scheduled">{t("Scheduled", "Geplant")}</TabsTrigger>
          <TabsTrigger value="waiting">{t("Waiting", "Wartet")}</TabsTrigger>
          <TabsTrigger value="notes">{t("Notes", "Notizen")}</TabsTrigger>
        </TabsList>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <TabsContent value="activity">
            <ActivityTab />
          </TabsContent>
          <TabsContent value="scheduled">
            <ScheduledTab />
          </TabsContent>
          <TabsContent value="waiting">
            {profile?.agentId ? (
              <NeedsMeWidget config={{}} onConfigChange={() => {}} agentId={profile.agentId} />
            ) : (
              <p className="p-3 text-xs text-muted-foreground">{t("Loading…", "Wird geladen…")}</p>
            )}
          </TabsContent>
          <TabsContent value="notes">
            <NotesTab />
          </TabsContent>
        </div>
      </Tabs>
      <p className="border-t border-border px-3 py-2 text-[11px] text-muted-foreground">
        {t(
          "Stopping work does not undo actions already taken.",
          "Stoppen macht bereits ausgeführte Aktionen nicht rückgängig.",
        )}
      </p>
    </div>
  );
}
