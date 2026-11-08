// The agent's Live tab (spec §3): one continuous Conversation per member and
// agent, with the agent's autonomous runs as turns and a composer that writes
// to the run that is running right now.
import { useState, type MouseEvent, type ReactNode } from "react";
import { Bot, CalendarClock, Send, Square, User, Webhook, Zap } from "lucide-react";

import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ApiError } from "@/lib/api";
import {
  useLiveTimeline,
  useRunSteering,
  useSendRunInput,
  useStopRun,
  type LiveRunDTO,
} from "@/lib/conversation-api";
import { useMayManageAgent } from "@/lib/governance-hooks";
import { useT } from "@/lib/i18n";

import { Conversation, type ExtraRunTurn } from "./conversation";
import { cronWords } from "./cron-words";
import { isPlainClick } from "./object-links";
import { RunInputs, runInputError, steeringTarget } from "./run-steering";
import { useObjectNavigation } from "./use-object-navigation";

const TERMINAL = new Set(["done", "failed", "interrupted"]);

type Translate = (en: string, de?: string) => string;

function triggerText(run: LiveRunDTO, t: Translate): [ReactNode, string] {
  const label = run.trigger.label;
  const named = (en: string, de: string) => t(en, de).replace("{label}", label ?? "");
  const icon = (Icon: typeof Bot) => <Icon className="size-3.5 shrink-0" aria-hidden />;
  if (run.source === "delegation") {
    return [
      icon(Send),
      label ? named("Delegated by {label}", "Delegiert von {label}") : t("Delegated", "Delegiert"),
    ];
  }
  // A run a member started (by hand or from a chat) names that member, not
  // its source -- the server's label is then the member's name.
  if (run.startedBy && label) return [icon(User), named("From {label}", "Von {label}")];
  switch (run.source) {
    case "cron":
      return [
        icon(CalendarClock),
        label ? `${t("Schedule", "Zeitplan")} · ${cronWords(label, t)}` : t("Schedule", "Zeitplan"),
      ];
    case "webhook":
      return [icon(Webhook), label ? `Webhook · ${label}` : "Webhook"];
    case "event":
      return [icon(Zap), label ? `${t("Event", "Ereignis")} · ${label}` : t("Event", "Ereignis")];
    case "manual":
    case "chat":
      return [icon(User), t("Started by hand", "Von Hand gestartet")];
  }
  return [icon(Bot), run.source];
}

/** The line above a timeline run: what started it, in words. */
export function TriggerLine({ run }: { run: LiveRunDTO }) {
  const t = useT();
  const [icon, text] = triggerText(run, t);
  return (
    <p className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      {icon}
      <span>{text}</span>
    </p>
  );
}

/** A link inside the app: the router takes it when there is one. */
function AppLink({ href, children }: { href: string; children: ReactNode }) {
  const navigate = useObjectNavigation();
  return (
    <a
      href={href}
      onClick={(e: MouseEvent<HTMLAnchorElement>) => {
        if (!navigate || !isPlainClick(e)) return;
        e.preventDefault();
        navigate(href);
      }}
      className="rounded-md text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {children}
    </a>
  );
}

function LiveSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[760px] space-y-6 p-6" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <div key={i} className="space-y-2">
          <Skeleton className="h-3 w-1/4 rounded-md" />
          <Skeleton className="h-4 w-3/4 rounded-md" />
        </div>
      ))}
    </div>
  );
}

export function LiveTimeline({
  agentId,
  departmentId,
  promptStarters,
}: {
  agentId: string;
  departmentId: string | null;
  promptStarters: string[];
}) {
  const t = useT();
  const mayManage = useMayManageAgent()(departmentId);
  const [allRuns, setAllRuns] = useState(false);
  const [inputError, setInputError] = useState<string | null>(null);
  const timeline = useLiveTimeline(agentId, allRuns);
  const { data, error } = timeline;
  const runs = (data?.items ?? []).flatMap((i) => (i.kind === "run" && i.run ? [i.run] : []));
  // The newest of the reader's own active runs -- a colleague's run is
  // never steered from here.
  const running = [...runs].reverse().find((r) => !r.redacted && !TERMINAL.has(r.state)) ?? null;
  const { data: steering } = useRunSteering(running?.id ?? null);
  const sendInput = useSendRunInput(running?.id ?? null);
  const stopRun = useStopRun();
  const { target, route } = steeringTarget(running ? steering : undefined, t);
  const refused = error instanceof ApiError && error.status === 403;

  const extraRuns: ExtraRunTurn[] = runs.map((run) => ({
    at: run.createdAt,
    run,
    labelsOnly: run.redacted,
    trigger: (
      <div className="flex flex-wrap items-center gap-2">
        <TriggerLine run={run} />
        {mayManage && (
          <AppLink
            href={`/agents/${encodeURIComponent(agentId)}?tab=livelog&runId=${encodeURIComponent(run.id)}`}
          >
            {t("Technical view", "Technische Ansicht")}
          </AppLink>
        )}
      </div>
    ),
    footer: (
      <>
        <RunInputs runId={run.id} runState={run.state} />
        {/* `pin=run:<id>` -- the Copilot pins the run (C3, Task 37). */}
        {TERMINAL.has(run.state) && !run.redacted && (
          <AppLink href={`/copilot?pin=${encodeURIComponent(`run:${run.id}`)}`}>
            {t("Continue", "Weiterführen")}
          </AppLink>
        )}
      </>
    ),
  }));

  const allRunsSwitch = (mayManage || data?.mayViewAllRuns) && (
    <div className="flex items-center justify-end gap-2 border-b border-border px-4 py-2">
      <label htmlFor="live-all-runs" className="text-xs text-muted-foreground">
        {t("All runs", "Alle Läufe")}
      </label>
      <Switch id="live-all-runs" checked={allRuns} onCheckedChange={setAllRuns} />
    </div>
  );

  if (!data) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        {allRunsSwitch}
        {error ? (
          <p role="alert" className="px-4 py-6 text-center text-sm text-status-error">
            {refused
              ? t(
                  "You may not see all runs of this agent. Switch All runs off to see yours.",
                  "Du darfst nicht alle Läufe dieses Agenten sehen. Schalte Alle Läufe aus, um deine zu sehen.",
                )
              : t(
                  "The timeline didn't load. Reload the page to try again.",
                  "Der Verlauf wurde nicht geladen. Lade die Seite neu, um es noch einmal zu versuchen.",
                )}
          </p>
        ) : (
          <LiveSkeleton />
        )}
      </div>
    );
  }

  const stop =
    running && steering?.mayWrite ? (
      <button
        type="button"
        onClick={() => stopRun.mutate(running.id)}
        aria-label={t("Stop the running run", "Laufenden Lauf stoppen")}
        className="inline-flex size-8 items-center justify-center rounded-md border border-border text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Square className="size-3.5" aria-hidden />
      </button>
    ) : null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {allRunsSwitch}
      <Conversation
        agentId={agentId}
        sessionId={data.sessionId}
        onSessionChange={() => undefined}
        variant="full"
        daySeparators
        extraRuns={extraRuns}
        hasOlder={timeline.hasOlder}
        onLoadOlder={timeline.fetchOlder}
        loadingOlder={timeline.loadingOlder}
        composerTarget={
          (target || inputError) && (
            <span className="inline-flex flex-col gap-0.5">
              {target}
              {inputError && (
                <span role="alert" className="text-status-error">
                  {inputError}
                </span>
              )}
            </span>
          )
        }
        composerTrailing={stop}
        onSendOverride={(submit) => {
          if (!running || !route) return false;
          setInputError(null);
          sendInput.mutate(submit.text, {
            onError: (e) => setInputError(runInputError(e, t)),
          });
          return true;
        }}
        emptyState={
          <p className="py-12 text-center text-sm text-muted-foreground">
            {promptStarters.length > 0
              ? t(
                  "This is where this agent works. Ask it something, or wait for its next run.",
                  "Hier arbeitet dieser Agent. Frag ihn etwas oder warte auf seinen nächsten Lauf.",
                )
              : t(
                  "This is where this agent works. Its runs and your messages show up here — write to start.",
                  "Hier arbeitet dieser Agent. Seine Läufe und deine Nachrichten erscheinen hier – schreib los.",
                )}
          </p>
        }
      />
    </div>
  );
}
